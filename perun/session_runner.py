"""Run one agent turn as an idempotent prompt in a durable OpenCode session.

The legacy runner spawned ``opencode run`` and watched the child process. That
made "a run" equal to "a process": if Perun died, the run died with it. OpenCode
v2 owns the turn in its background service, so the durable unit is the
*session* — Perun only admits the prompt, observes it, and records the result.

Admission is idempotent: the message id is ``msg_<run_uid>`` (the API requires
``msg_``), so a crash between "record" and "admit" re-admits the same turn
exactly once. Observation uses the service's event stream, demultiplexed by
session id through :class:`perun.opencode_api.EventHub`.

Ownership is a lease (``run_leases``) rather than a pid: a crash leaves a stale
lease that the reconciler takes over, and the previous owner stops writing as
soon as ``run_lease_owned()`` says the epoch moved.
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import time
from typing import Any, Dict, List, Optional

from . import opencode_api, registry
from .orchestrator import compose_prompt

LOG = logging.getLogger("perun.session_runner")

#: Event types worth subscribing to (everything else is noise for a run).
EVENT_TYPES = [
    "session.execution.started",
    "session.execution.succeeded",
    "session.execution.failed",
    "session.execution.interrupted",
    "session.text.delta",
    "session.text.ended",
    "session.tool.called",
    "session.tool.success",
    "session.idle",
]

MAX_TEXT_PARTS = 4000


class SessionRunner:
    """Adapter between the orchestrator and the OpenCode session API."""

    def __init__(self, orch: Any, *, api: Optional["opencode_api.OpenCodeAPI"] = None,
                 hub: Optional["opencode_api.EventHub"] = None,
                 owner: str = ""):
        self.orch = orch
        self._api = api
        self._hub = hub
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"

    # ---- ports ------------------------------------------------------- #
    def _client(self) -> "opencode_api.OpenCodeAPI":
        return self._api or opencode_api.default_api()

    def _events(self) -> "opencode_api.EventHub":
        hub = self._hub or opencode_api.default_hub()
        hub.start()
        return hub

    # ---- admission --------------------------------------------------- #
    def _session_id(self, api: "opencode_api.OpenCodeAPI", project: Dict[str, Any],
                    prompt: str, run: Dict[str, Any]) -> str:
        sid = str(run.get("opencode_session_id") or "").strip()
        if sid:
            return sid
        info = api.create_session(
            project["path"],
            model=str(run.get("model") or ""),
            agent=str(run.get("agent") or ""),
            title=prompt[:80],
        )
        sid = str(info.get("id") or "")
        if sid:
            registry.update_run(run["run_uid"], opencode_session_id=sid)
        return sid

    def execute(self, run_uid: str, project: Dict[str, Any], prompt: str,
                run: Dict[str, Any]) -> None:
        """Admit the prompt and observe the turn until it finishes."""
        api = self._client()
        lease = registry.claim_run_lease(run_uid, self.owner)
        if lease is None:
            # Another live process owns this run; it will finalize it.
            LOG.warning("run %s is owned by another observer; skipping", run_uid)
            return

        sid = ""
        reply = ""
        admitted = False
        status, error = "failed", "run did not start"
        try:
            sid = self._session_id(api, project, prompt, run)
            if not sid:
                self._finish(run_uid, "failed",
                             "could not create an OpenCode session", "", lease, sid)
                return

            composed = compose_prompt(
                project, prompt,
                new_session=not str(run.get("opencode_session_id") or ""),
                settings=self.orch.settings,
            )

            inbox: "queue.Queue[Dict[str, Any]]" = queue.Queue()
            hub = self._events()
            token = hub.subscribe(sid, inbox.put)
            try:
                # Record the admission *before* submitting: a crash in between
                # re-admits the same msg_<uid> (a no-op for the service).
                registry.update_run(run_uid, status="running",
                                    started_at=registry.now_iso(),
                                    admitted_at=registry.now_iso())
                self.orch.bus.publish(run_uid, {
                    "type": "start", "run": run_uid,
                    "project": project["slug"], "cwd": project["path"],
                })
                self.orch.bus.publish(run_uid, {
                    "type": "session", "run": run_uid, "session": sid,
                })
                api.prompt(
                    sid, composed, delivery="queue",
                    message_id=f"msg_{run_uid}", resume=True,
                )
                admitted = True
                status, error, reply = self._observe(
                    run_uid, project, sid, inbox, lease,
                    seed=self._existing_reply(api, sid) if run.get("_reattach") else "",
                )
            finally:
                hub.unsubscribe(token)
        except opencode_api.OpenCodeUnavailable as exc:
            if not admitted:
                # Nothing reached the session: let the caller fall back to the
                # CLI runner (no side effects to duplicate).
                registry.release_run_lease(run_uid, self.owner)
                raise
            status, error = "interrupted", f"lost the opencode service: {exc}"
        except opencode_api.OpenCodeError as exc:
            status, error = "failed", f"opencode: {exc}"
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("session runner failed for %s", run_uid)
            status, error = "failed", f"internal error: {exc}"

        self._finish(run_uid, status, error, reply, lease, sid)

    def attach(self, run_uid: str, project: Dict[str, Any],
               run: Dict[str, Any]) -> None:
        """Observe a turn that is already running (after a restart)."""
        api = self._client()
        sid = str(run.get("opencode_session_id") or "")
        if not sid:
            self.orch._mark_interrupted(run_uid, "admitted run has no session id")
            return
        lease = registry.claim_run_lease(run_uid, self.owner)
        if lease is None:
            LOG.info("run %s is already observed by another process", run_uid)
            return
        inbox: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        hub = self._events()
        token = hub.subscribe(sid, inbox.put)
        status, error, reply = "interrupted", "could not observe the session", ""
        try:
            self.orch.bus.publish(run_uid, {
                "type": "start", "run": run_uid,
                "project": project["slug"], "cwd": project["path"],
            })
            self.orch.bus.publish(run_uid, {
                "type": "session", "run": run_uid, "session": sid,
            })
            seed = self._existing_reply(api, sid)
            status, error, reply = self._observe(
                run_uid, project, sid, inbox, lease, seed=seed)
        except opencode_api.OpenCodeError as exc:
            status, error = "interrupted", f"could not observe the session: {exc}"
        finally:
            hub.unsubscribe(token)
        self._finish(run_uid, status, error, reply, lease, sid)

    # ---- observation ------------------------------------------------- #
    def _observe(self, run_uid: str, project: Dict[str, Any], sid: str,
                 inbox: "queue.Queue[Dict[str, Any]]", lease: int,
                 *, seed: str = "") -> tuple[str, str, str]:
        api = self._client()
        parts: Dict[str, List[str]] = {}
        order: List[str] = []
        if seed:
            parts["seed"] = [seed]
            order.append("seed")
        last_beat = 0.0
        deadline = time.time() + max(1, int(self.orch.settings.run_timeout))
        status, error = "failed", "run ended without a result"

        while True:
            if not registry.run_lease_owned(run_uid, self.owner, lease):
                LOG.warning("run %s: lease lost, stopping observer", run_uid)
                return "interrupted", "observation handed to another process", ""
            if self.orch.is_cancelled(run_uid):
                api.interrupt(sid)
                return "cancelled", "", self._reply_from(parts, order)
            now = time.time()
            if now >= deadline:
                api.interrupt(sid)
                return ("timeout", f"run exceeded {self.orch.settings.run_timeout}s",
                        self._reply_from(parts, order))
            if now - last_beat > 5.0:
                registry.touch_run_lease(run_uid, self.owner, lease)
                last_beat = now
            try:
                event = inbox.get(timeout=1.0)
            except queue.Empty:
                # The service may have finished while we were not subscribed (or
                # dropped the event): a non-active session with text is done.
                if not api.session_active(sid):
                    reply = self._reply_from(parts, order) or self._existing_reply(api, sid)
                    if reply:
                        return "done", "", reply
                    return "failed", "session ended with no output", ""
                continue

            etype = str(event.get("type") or "")
            data = event.get("data") or {}
            if etype == "session.text.delta":
                mid = str(data.get("assistantMessageID") or "m")
                delta = str(data.get("delta") or "")
                if mid not in parts:
                    parts[mid] = []
                    order.append(mid)
                parts[mid].append(delta)
                if delta:
                    self.orch._publish_text(run_uid, delta)
            elif etype == "session.text.ended":
                mid = str(data.get("assistantMessageID") or "m")
                text = str(data.get("text") or "")
                if mid not in parts:
                    order.append(mid)
                parts[mid] = [text]
            elif etype in ("session.tool.called", "session.tool.success"):
                tool = str(data.get("tool") or data.get("name") or "tool")
                self.orch.bus.publish(run_uid, {
                    "type": "tool", "run": run_uid, "tool": tool,
                    "status": "success" if etype.endswith("success") else "running",
                })
            elif etype == "session.execution.succeeded":
                status, error = "done", ""
                break
            elif etype == "session.execution.failed":
                status = "failed"
                error = str(data.get("error") or data.get("message") or "turn failed")
                break
            elif etype == "session.execution.interrupted":
                status, error = "cancelled", ""
                break
            elif etype == "session.idle":
                # Fallback: some paths end the turn without an execution event.
                reply = self._reply_from(parts, order)
                if reply:
                    status, error = "done", ""
                else:
                    status, error = "failed", "session went idle with no output"
                break

        return status, error, self._reply_from(parts, order)

    @staticmethod
    def _reply_from(parts: Dict[str, List[str]], order: List[str]) -> str:
        return "\n\n".join(
            "".join(parts[mid]).strip() for mid in order if parts.get(mid)
        ).strip()

    def _existing_reply(self, api: "opencode_api.OpenCodeAPI", sid: str) -> str:
        """Assistant text already in the session (used when reattaching)."""
        try:
            messages = api.messages(sid, limit=50)
        except opencode_api.OpenCodeError:
            return ""
        chunks: List[str] = []
        for message in messages:
            if str(message.get("type") or "") not in ("assistant", "message"):
                continue
            for part in message.get("parts") or message.get("content") or []:
                if isinstance(part, dict) and part.get("type") in (None, "text"):
                    text = part.get("text")
                    if text:
                        chunks.append(str(text))
        return "\n\n".join(chunks).strip()

    # ---- finishing --------------------------------------------------- #
    def _finish(self, run_uid: str, status: str, error: str, reply: str,
                lease: int, sid: str) -> None:
        try:
            registry.release_run_lease(run_uid, self.owner)
        except Exception:  # pragma: no cover - defensive
            LOG.debug("could not release lease for %s", run_uid, exc_info=True)
        self.orch._finalize(
            run_uid, status, None, error,
            cancelled=(status == "cancelled"),
            reply=reply or "",
            opencode_session_id=sid,
        )
