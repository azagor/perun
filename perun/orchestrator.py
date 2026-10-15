"""Agent orchestrator — start, stream, and cancel OpenCode sessions per project.

Each run executes ``opencode run --format json --auto ...`` with the working
directory set to the target project (the same proven pattern as the per-project
Telegram bridge), parses the JSON event stream (session id + text parts),
persists a run record, and fans events out to subscribers (the web UI consumes
them over SSE, the Telegram bridge over an in-process queue).

A thread-per-run model with a semaphore keeps resource use bounded while
allowing independent projects to run concurrently.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, Iterator

from . import config, control, registry

MAX_STORED_OUTPUT = 200_000
HISTORY_LIMIT = 4000

DEFAULT_SESSION_PROMPT = "Working session in progress."


def compose_prompt(project: dict[str, Any], prompt: str, *, new_session: bool,
                   settings: config.Settings | None = None) -> str:
    """Prepend per-session instructions to a fresh session's prompt.

    Three layers ride along, in order: the **orchestrator persona** (only for the
    control-plane project — see :mod:`perun.control`), the user's **custom
    orchestrator instructions** (again, control-plane only), and the project's
    own custom instructions. All are injected only when a **new** session
    starts; continuations inherit them from the session history, so they are
    never repeated on every turn.
    """
    if not new_session:
        return prompt
    blocks: list[str] = []
    if control.is_control(project):
        blocks.append(control.ORCHESTRATOR_INSTRUCTIONS.strip())
        custom = (getattr(settings, "orchestrator_instructions", "") or "").strip()
        if custom:
            blocks.append(f"Additional standing instructions from the user:\n\n{custom}")
    instructions = (project.get("instructions") or "").strip()
    if instructions:
        blocks.append(instructions)
    body = "\n\n".join(b for b in blocks if b)
    if not body:
        return prompt
    return (
        "[PERUN PROJECT INSTRUCTIONS]\n"
        "The following are project-level instructions. Follow them for every task "
        "in this session unless the user explicitly says otherwise.\n\n"
        f"{body}\n"
        "[END PERUN PROJECT INSTRUCTIONS]\n\n"
        f"{prompt}"
    )


# --------------------------------------------------------------------------- #
# Event bus (in-memory pub/sub for run streams)
# --------------------------------------------------------------------------- #
class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Queue]] = {}
        self._history: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._global_subs: list[Queue] = []

    def subscribe(self, channel: str, *, replay: bool = True) -> Queue:
        q: Queue = Queue()
        with self._lock:
            if replay:
                for ev in self._history.get(channel, []):
                    q.put(ev)
            self._subs.setdefault(channel, []).append(q)
        return q

    def unsubscribe(self, channel: str, q: Queue) -> None:
        with self._lock:
            subs = self._subs.get(channel)
            if subs and q in subs:
                subs.remove(q)

    def publish(self, channel: str, event: dict[str, Any]) -> None:
        with self._lock:
            hist = self._history.setdefault(channel, [])
            hist.append(event)
            if len(hist) > HISTORY_LIMIT:
                del hist[: len(hist) - HISTORY_LIMIT]
            targets = list(self._subs.get(channel, []))
            if channel != "activity":
                targets += list(self._global_subs)
        for q in targets:
            try:
                q.put(event)
            except Exception:
                pass

    def subscribe_all(self) -> Queue:
        q: Queue = Queue()
        with self._lock:
            self._global_subs.append(q)
        return q

    def unsubscribe_all(self, q: Queue) -> None:
        with self._lock:
            if q in self._global_subs:
                self._global_subs.remove(q)

    def close(self, channel: str) -> None:
        with self._lock:
            subs = self._subs.pop(channel, [])
        for q in subs:
            q.put({"type": "__eof__", "run": channel})


# --------------------------------------------------------------------------- #
# Process helpers (Windows-aware, mirrored from telegram_bridge.py)
# --------------------------------------------------------------------------- #
def resolve_opencode(bin_name: str) -> list[str]:
    path = shutil.which(bin_name)
    if path is None:
        return [bin_name]
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", path]
    return [path]


def terminate_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def parse_event(line: str) -> dict[str, Any] | None:
    """Parse one ``opencode run --format json`` line into a normalized event."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        return None
    out: dict[str, Any] = {"raw": evt}
    sid = evt.get("sessionID") or evt.get("session_id")
    if not sid and isinstance(evt.get("session"), dict):
        sid = evt["session"].get("id")
    if sid:
        out["session_id"] = sid
    etype = evt.get("type") or evt.get("event") or ""
    part = evt.get("part") or {}
    if etype == "text" and isinstance(part, dict) and part.get("type") == "text":
        out["kind"] = "text"
        out["text"] = part.get("text", "")
    elif etype == "reasoning":
        out["kind"] = "reasoning"
        out["text"] = (part.get("text") if isinstance(part, dict) else "") or ""
    elif etype == "tool" or (isinstance(part, dict) and part.get("type") == "tool"):
        tool = part.get("tool") or (part.get("name") if isinstance(part, dict) else "")
        state = part.get("state") if isinstance(part, dict) else None
        status = state.get("status") if isinstance(state, dict) else None
        out["kind"] = "tool"
        out["tool"] = tool
        out["status"] = status
        if isinstance(state, dict) and state.get("input"):
            try:
                out["text"] = json.dumps(state["input"])[:400]
            except Exception:
                pass
    elif etype in ("error", "session.error") or (isinstance(etype, str) and etype.startswith("provider.")):
        out["kind"] = "error"
        out["text"] = json.dumps(evt.get("error") or evt.get("message") or evt)[:1000]
        if isinstance(etype, str) and etype.startswith("provider."):
            out["provider_error"] = True
    else:
        out["kind"] = etype or "event"
    return out


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
class Orchestrator:
    def __init__(self, settings: config.Settings | None = None) -> None:
        self.settings = settings or config.Settings.load()
        self.bus = EventBus()
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._done: dict[str, threading.Event] = {}
        self._replies: dict[str, str] = {}
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(max(1, self.settings.max_concurrent_runs))

    # -- public API -------------------------------------------------------- #
    def start_run(self, project_ref: str, prompt: str, *,
                  session_pk: int | None = None,
                  opencode_session_id: str = "",
                  model: str = "",
                  agent: str = "",
                  title: str = "") -> dict[str, Any]:
        project = registry.get_project(project_ref)
        if not project:
            raise ValueError(f"no such project: {project_ref}")
        prompt = (prompt or "").strip() or DEFAULT_SESSION_PROMPT

        run_uid = uuid.uuid4().hex[:16]
        # Model/agent precedence: explicit call argument → the project's own
        # override → (control plane only) the dedicated orchestrator model →
        # the global default.
        requested_model = (model or "").strip()
        model_explicit = bool(requested_model)
        model = requested_model or project.get("model")
        if not model and control.is_control(project):
            model = self.settings.orchestrator_model
        # The global default_model is intentionally NOT forced onto project runs;
        # it is only used for the control plane (orchestrator). This lets regular
        # projects fall back to OpenCode's own default model instead of being
        # overridden by a possibly incompatible Perun setting.
        if not model and control.is_control(project):
            model = self.settings.default_model
        agent = (agent or "").strip() or project.get("agent")
        if not agent and control.is_control(project):
            agent = self.settings.orchestrator_agent
        agent = agent or self.settings.default_agent

        run = registry.create_run(
            run_uid, project["id"], prompt,
            session_pk=session_pk, opencode_session_id=opencode_session_id,
            model=model, model_explicit=model_explicit, agent=agent,
        )
        registry.log_activity(project["id"], "run",
                              f"Started agent run ({run_uid})",
                              {"prompt": prompt[:200]})
        self._done[run_uid] = threading.Event()
        self.bus.publish(run_uid, {"type": "queued", "run": run_uid,
                                   "project": project["slug"]})
        threading.Thread(target=self._worker, args=(run_uid, project, prompt, run),
                         daemon=True).start()
        return run

    def run_and_wait(self, project_ref: str, prompt: str, *, timeout: int | None = None,
                     **kwargs: Any) -> dict[str, Any]:
        run = self.start_run(project_ref, prompt, **kwargs)
        uid = run["run_uid"]
        done = self._done.get(uid)
        if done:
            done.wait(timeout or self.settings.run_timeout)
        final = registry.get_run(uid) or run
        return {"run": final, "reply": self._replies.get(uid, final.get("output", ""))}

    def cancel(self, run_uid: str) -> bool:
        with self._lock:
            proc = self._procs.get(run_uid)
            self._cancelled.add(run_uid)
        if proc and proc.poll() is None:
            terminate_tree(proc)
        # Even if the process already exited, force the run to a final state so
        # the dashboard never shows a zombie run.
        registry.update_run(run_uid, status="cancelled", ended_at=registry.now_iso(), error="cancelled by user")
        self.bus.publish(run_uid, {"type": "end", "run": run_uid, "status": "cancelled"})
        return True

    def is_final(self, run_uid: str) -> bool:
        run = registry.get_run(run_uid)
        return bool(run and run["status"] in ("done", "failed", "cancelled", "timeout", "provider_error"))

    def active(self) -> list[str]:
        with self._lock:
            return [uid for uid, p in self._procs.items() if p.poll() is None]

    # -- worker ------------------------------------------------------------ #
    def _worker(self, run_uid: str, project: dict[str, Any], prompt: str,
                run: dict[str, Any]) -> None:
        acquired = self._sem.acquire(timeout=self.settings.run_timeout * 2)
        if not acquired:
            self._finalize(run_uid, "failed", None, "queue timeout", cancelled=False)
            return
        try:
            self._execute(run_uid, project, prompt, run)
        except Exception as exc:  # pragma: no cover - defensive
            self._publish_text(run_uid, f"\n[perun] internal error: {exc}\n")
            self._finalize(run_uid, "failed", None, str(exc), cancelled=False)
        finally:
            self._sem.release()

    def _execute(self, run_uid: str, project: dict[str, Any], prompt: str,
                 run: dict[str, Any]) -> None:
        cwd = project["path"]
        oc_sid = run.get("opencode_session_id") or ""
        # Project-scoped custom instructions ride along with a new session.
        prompt = compose_prompt(project, prompt, new_session=not oc_sid,
                                settings=self.settings)
        argv = resolve_opencode("opencode") + ["run", "--format", "json", "--auto"]
        # Only pass --model when the caller (or the project) explicitly set one.
        # An empty/missing model lets OpenCode use its own default, which avoids
        # surprising region/model errors from Perun's global fallback.
        if run.get("model") and run.get("model_explicit"):
            argv += ["--model", run["model"]]
        if run.get("agent"):
            argv += ["--agent", run["agent"]]
        if oc_sid:
            argv += ["--session", oc_sid]
        if run_uid:
            argv += ["--title", f"Perun · {project['name']} · {run_uid[:6]}"]
        argv += ["--", prompt]

        log_path = config.RUNS_DIR / f"{run_uid}.log"
        config.ensure_dirs()
        self._publish_text(run_uid, "")  # trigger a start frame
        registry.update_run(run_uid, status="running", started_at=registry.now_iso())
        self.bus.publish(run_uid, {"type": "start", "run": run_uid,
                                   "project": project["slug"], "cwd": cwd})

        try:
            proc = subprocess.Popen(
                argv, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError as exc:
            self._finalize(run_uid, "failed", None, f"cannot launch opencode: {exc}",
                           cancelled=False)
            return

        with self._lock:
            self._procs[run_uid] = proc

        stderr_chunks: list[str] = []

        def drain_stderr() -> None:
            try:
                assert proc.stderr is not None
                for line in proc.stderr:
                    stderr_chunks.append(line)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        reply_parts: list[str] = []
        error_parts: list[str] = []
        provider_error_seen = False
        learned_sid = oc_sid
        timed_out = False
        start = time.time()

        try:
            assert proc.stdout is not None
            with log_path.open("w", encoding="utf-8", errors="replace") as logf:
                for line in proc.stdout:
                    logf.write(line)
                    if time.time() - start > self.settings.run_timeout:
                        timed_out = True
                        terminate_tree(proc)
                        break
                    if run_uid in self._cancelled:
                        terminate_tree(proc)
                        break
                    evt = parse_event(line)
                    if not evt:
                        continue
                    sid = evt.get("session_id")
                    if sid and sid != learned_sid:
                        learned_sid = sid
                        registry.update_run(run_uid, opencode_session_id=sid)
                        self.bus.publish(run_uid, {"type": "session", "run": run_uid,
                                                   "session_id": sid})
                    kind = evt.get("kind")
                    if kind == "text":
                        text = evt.get("text", "")
                        if text:
                            reply_parts.append(text)
                            self.bus.publish(run_uid, {"type": "text", "run": run_uid,
                                                       "text": text})
                    elif kind == "tool":
                        self.bus.publish(run_uid, {"type": "tool", "run": run_uid,
                                                   "tool": evt.get("tool", ""),
                                                   "status": evt.get("status", "")})
                    elif kind == "error" or (isinstance(kind, str) and kind.startswith("provider.")):
                        err_text = evt.get("text", "")
                        if err_text:
                            error_parts.append(err_text)
                        if evt.get("provider_error"):
                            provider_error_seen = True
                        self.bus.publish(run_uid, {"type": "error", "run": run_uid,
                                                   "text": err_text})
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            terminate_tree(proc)
            timed_out = True
        except Exception as exc:  # pragma: no cover
            terminate_tree(proc)
            self._finalize(run_uid, "failed", None, str(exc), cancelled=False)
            return

        stderr_thread.join(timeout=5)
        with self._lock:
            self._procs.pop(run_uid, None)
            was_cancelled = run_uid in self._cancelled

        reply = "\n\n".join(p for p in reply_parts if p).strip()
        self._replies[run_uid] = reply
        stderr_tail = "".join(stderr_chunks).strip()[-4000:]

        # Provider-side errors (quota, region, invalid model, …) are different
        # from agent crashes: a session may have been created successfully and
        # the user can continue it after fixing the provider issue.
        provider_error = bool(learned_sid) and provider_error_seen

        if was_cancelled:
            status = "cancelled"
        elif timed_out:
            status = "timeout"
        elif provider_error:
            status = "provider_error"
        elif proc.returncode != 0 and not reply:
            status = "failed"
        else:
            status = "done"

        error = ""
        if status in ("failed", "timeout", "provider_error") and stderr_tail:
            error = stderr_tail
        # A provider/agent error can arrive only as a stdout JSON event (e.g. a
        # rejected model). Persist it so the dashboard can explain a failed run.
        if not error and not reply and error_parts:
            error = "\n".join(error_parts)[-4000:]
        self._finalize(run_uid, status, proc.returncode, error, cancelled=was_cancelled,
                       reply=reply, opencode_session_id=learned_sid)
        if status in ("failed", "provider_error") and not reply:
            self.bus.publish(run_uid, {"type": "error", "run": run_uid,
                                       "text": stderr_tail or error or "agent exited non-zero"})

    # -- helpers ----------------------------------------------------------- #
    def _publish_text(self, run_uid: str, text: str) -> None:
        self.bus.publish(run_uid, {"type": "text", "run": run_uid, "text": text})

    def _finalize(self, run_uid: str, status: str, exit_code: int | None,
                  error: str, *, cancelled: bool, reply: str = "",
                  opencode_session_id: str = "") -> None:
        run = registry.get_run(run_uid)
        fatal = status in ("failed", "cancelled", "timeout", "provider_error")
        registry.update_run(
            run_uid, status=status, exit_code=exit_code,
            ended_at=registry.now_iso(),
            output=(reply or (run or {}).get("output", ""))[:MAX_STORED_OUTPUT],
            error=error[:4000] if (fatal and error) else ("" if not fatal else error[:4000]),
            **({"opencode_session_id": opencode_session_id} if opencode_session_id else {}),
        )
        # Keep the session row in sync.
        run = registry.get_run(run_uid) or run
        if run:
            sid = run.get("session_pk")
            if not sid and run.get("opencode_session_id"):
                existing = registry.find_session_by_opencode(
                    run["project_id"], run["opencode_session_id"])
                if existing:
                    sid = existing["id"]
                    registry.update_run(run_uid, session_pk=sid)
                else:
                    created = registry.create_session(
                        run["project_id"], run["opencode_session_id"],
                        title=(run.get("prompt") or "")[:80])
                    sid = created["id"]
                    registry.update_run(run_uid, session_pk=sid)
            if sid:
                s = registry.get_session(sid) or {}
                registry.update_session(
                    sid,
                    opencode_session_id=run.get("opencode_session_id") or s.get("opencode_session_id", ""),
                    message_count=(s.get("message_count") or 0) + 1,
                    status="error" if status == "failed" else "active",
                    last_error=error[:500],
                )
            proj = registry.get_project(run["project_id"])
            label = proj["slug"] if proj else str(run["project_id"])
            registry.log_activity(
                run["project_id"], "run",
                f"Run {run_uid} {status}", {"status": status, "exit_code": exit_code})

        self.bus.publish(run_uid, {"type": "end", "run": run_uid, "status": status,
                                   "exit_code": exit_code, "reply": reply,
                                   "error": "" if not fatal else error[:1000]})
        done = self._done.get(run_uid)
        if done:
            done.set()

    def stream(self, run_uid: str) -> Iterator[dict[str, Any]]:
        """Blocking generator of events for a run (used by SSE endpoints)."""
        q = self.bus.subscribe(run_uid)
        try:
            while True:
                try:
                    ev = q.get(timeout=60)
                except Empty:
                    yield {"type": "ping", "run": run_uid}
                    if self.is_final(run_uid):
                        break
                    continue
                yield ev
                if ev.get("type") in ("end", "__eof__"):
                    break
        finally:
            self.bus.unsubscribe(run_uid, q)
