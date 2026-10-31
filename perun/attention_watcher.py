"""Watch the local OpenCode service for requests that are blocking on a human.

Two facts from the spike shape this module:

* ``GET /api/permission/request`` and ``GET /api/form`` are **location-scoped**,
  so one global poll is not enough — every loaded location must be queried with
  ``?location[directory]=…``.
* ``GET /api/session/active`` *is* global and costs ~1 ms, so it drives the poll:
  only locations with a running session are checked on every tick, plus a full
  sweep of ``GET /api/debug/location`` every five minutes for stragglers.

The watcher never sends anything.  It creates attention items; the Telegram
bridge (the process that owns the poller) turns them into cards.  That keeps a
Telegram outage from stopping detection, and a missing OpenCode service from
stopping room/proposal notices.

Detection is event-driven when the local service streams events (see
``start_event_ingest``): a ``permission.asked`` / ``form.created`` event wakes a
worker that runs one :func:`poll_once` after a short debounce, so a card appears
in about a second instead of on the next poll. The periodic poll stays as the
reconciliation path — it still closes requests that vanished, and it is the
only path when the stream is unavailable.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set

from . import attention, config, opencode_api, registry

LOG = logging.getLogger("perun.attention.watcher")

SWEEP_SECONDS = 300.0

# Events that mean "a human is now blocking a session". ``form.replied`` is
# included because a reply elsewhere may close a form this instance is showing.
INGEST_EVENT_TYPES = ("permission.asked", "form.created", "form.replied")
# A burst (one turn asking for three files) must produce one poll, not three.
INGEST_DEBOUNCE_SECONDS = 1.0

# Module state is process-local: the watcher only ever runs inside the Telegram
# bridge process, which is a singleton thanks to the bot-token PidLock.
_last_sweep = 0.0
_session_cache: Dict[str, Dict[str, Any]] = {}
# ``(target_kind, request_id) -> consecutive polls where it was missing``.
# A single empty response (service restart, load blip) must not close a card
# that still blocks a session; two in a row is treated as "resolved elsewhere".
_misses: Dict[tuple, int] = {}

# One poll at a time: the reconciliation loop and the event worker run in the
# same process and must not race on the same dedupe keys.
_poll_lock = threading.Lock()

# Event ingestion state (guarded by ``_ingest_lock``).
_ingest_lock = threading.Lock()
_ingest: Dict[str, Any] = {
    "thread": None, "stop": None, "wake": None, "token": None, "hub": None,
    "events": 0, "polls": 0,
}


def reset_cache() -> None:
    """Drop cached session metadata (tests, or after a service restart)."""
    global _last_sweep
    _last_sweep = 0.0
    _session_cache.clear()
    _misses.clear()


# --------------------------------------------------------------------------- #
# Event-driven ingestion
# --------------------------------------------------------------------------- #
def event_ingest_active() -> bool:
    """True while the SSE worker is running and will poll on events."""
    thread = _ingest.get("thread")
    return bool(thread is not None and thread.is_alive())


def ingest_stats() -> Dict[str, int]:
    """Events seen and polls they triggered (observability, tests)."""
    return {"events": int(_ingest.get("events") or 0),
            "polls": int(_ingest.get("polls") or 0)}


def _on_event(event: Dict[str, Any]) -> None:
    """Hub callback: wake the worker for an event that needs a human."""
    try:
        if str(event.get("type") or "") not in INGEST_EVENT_TYPES:
            return
    except AttributeError:  # a malformed frame must never break the stream
        return
    with _ingest_lock:
        _ingest["events"] = int(_ingest.get("events") or 0) + 1
        wake = _ingest.get("wake")
    if wake is not None:
        wake.set()


def _ingest_loop(stop: threading.Event, wake: threading.Event) -> None:
    while not stop.is_set():
        if not wake.wait(1.0):
            continue
        # Coalesce a burst: everything that arrives in the window is covered by
        # one poll, because a poll reads the live state rather than the events.
        wake.clear()
        stop.wait(INGEST_DEBOUNCE_SECONDS)
        wake.clear()
        try:
            poll_once()
            _ingest["polls"] = int(_ingest.get("polls") or 0) + 1
        except Exception:  # pragma: no cover - keep the worker alive
            LOG.debug("attention event poll failed", exc_info=True)


def start_event_ingest(hub: Optional["opencode_api.EventHub"] = None) -> bool:
    """Subscribe to the service's event stream and poll on relevant events.

    Returns True when the worker is running. Any failure (no service, no hub)
    returns False without raising: the caller then falls back to polling.
    """
    with _ingest_lock:
        if event_ingest_active():
            return True
        try:
            hub = hub or opencode_api.default_hub()
            token = hub.subscribe(opencode_api.EventHub.GLOBAL_SESSION, _on_event)
            hub.start()
        except Exception:  # pragma: no cover - depends on the local service
            LOG.debug("attention: could not subscribe to the event stream",
                      exc_info=True)
            return False
        stop = threading.Event()
        wake = threading.Event()
        thread = threading.Thread(
            target=_ingest_loop, args=(stop, wake), daemon=True,
            name="perun-attention-ingest",
        )
        _ingest.update(thread=thread, stop=stop, wake=wake, token=token, hub=hub)
        thread.start()
    LOG.info("attention: event-driven ingestion on (%s)",
             ", ".join(INGEST_EVENT_TYPES))
    return True


def stop_event_ingest() -> None:
    with _ingest_lock:
        stop = _ingest.get("stop")
        token = _ingest.get("token")
        hub = _ingest.get("hub")
        _ingest.update(thread=None, stop=None, wake=None, token=None, hub=None)
    if stop is not None:
        stop.set()
    if token and hub is not None:
        try:
            hub.unsubscribe(token)
        except Exception:  # pragma: no cover - stopping must not raise
            LOG.debug("attention: unsubscribe failed", exc_info=True)



def _session_info(api: "opencode_api.OpenCodeAPI", session_id: str) -> Dict[str, Any]:
    if session_id in _session_cache:
        return _session_cache[session_id]
    info: Dict[str, Any] = {}
    try:
        info = api.session(session_id) or {}
    except opencode_api.OpenCodeError:
        info = {}
    _session_cache[session_id] = info
    if len(_session_cache) > 200:
        _session_cache.clear()
        _session_cache[session_id] = info
    return info


def _session_title(info: Dict[str, Any], session_id: str) -> str:
    title = str(info.get("title") or "").strip()
    return title or session_id[:16]


def _session_directory(info: Dict[str, Any]) -> str:
    location = info.get("location") or {}
    return str(location.get("directory") or "").strip()


def _permission_item(api: "opencode_api.OpenCodeAPI", perm: Dict[str, Any],
                     project: Dict[str, Any], directory: str,
                     ttl_minutes: int = attention.DEFAULT_TTL_MINUTES) -> Optional[Dict[str, Any]]:
    request_id = str(perm.get("id") or "")
    session_id = str(perm.get("sessionID") or "")
    if not request_id or not session_id:
        return None
    info = _session_info(api, session_id)
    action = str(perm.get("action") or "use a tool")
    resources = [str(r) for r in (perm.get("resources") or [])]
    return attention.request(
        project,
        "permission",
        request_id,
        "permission",
        f"{action} {', '.join(resources[:3])}".strip(),
        options=attention.PERMISSION_OPTIONS,
        ref={
            "request_id": request_id,
            "session_id": session_id,
            "session_title": _session_title(info, session_id),
            "location": directory,
            "action": action,
            "resources": resources,
            "save": [str(s) for s in (perm.get("save") or [])],
            "message_id": str((perm.get("source") or {}).get("messageID") or ""),
        },
        ttl_minutes=ttl_minutes,
        dedupe_key=f"permission|{request_id}",
    )


def _form_item(api: "opencode_api.OpenCodeAPI", form: Dict[str, Any],
               project: Dict[str, Any], directory: str,
               ttl_minutes: int = attention.DEFAULT_TTL_MINUTES) -> Optional[Dict[str, Any]]:
    form_id = str(form.get("id") or "")
    session_id = str(form.get("sessionID") or "")
    if not form_id or not session_id:
        return None
    info = _session_info(api, session_id)
    fields = [
        {
            "key": str(f.get("key") or ""),
            "type": str(f.get("type") or "string"),
            "title": str(f.get("title") or ""),
            "required": bool(f.get("required")),
            "options": [str(o.get("value")) for o in (f.get("options") or [])
                        if isinstance(o, dict)],
        }
        for f in (form.get("fields") or [])
        if isinstance(f, dict) and f.get("key")
    ]
    return attention.request(
        project,
        "form",
        session_id,
        "form",
        str(form.get("title") or "A form needs your input"),
        ref={
            "form_id": form_id,
            "session_id": session_id,
            "session_title": _session_title(info, session_id),
            "location": directory,
            "fields": fields,
        },
        ttl_minutes=ttl_minutes,
        dedupe_key=f"form|{form_id}",
    )


def _reconcile(polled: Set[str], live_permissions: Set[str],
               live_forms: Set[str]) -> int:
    """Close items whose permission/form disappeared (answered in the TUI)."""
    closed = 0
    for kind, live in (("permission", live_permissions), ("form", live_forms)):
        for item in registry.open_attention_of_kind(kind):
            if item["target_kind"] != kind:
                continue
            ref = item.get("ref") or {}
            if str(ref.get("location") or "") not in polled:
                continue
            key = str(ref.get("request_id") if kind == "permission" else ref.get("form_id"))
            if not key:
                continue
            if key in live:
                _misses.pop((kind, key), None)
                continue
            misses = _misses.get((kind, key), 0) + 1
            _misses[(kind, key)] = misses
            if misses < 2:
                continue
            _misses.pop((kind, key), None)
            if registry.close_attention(
                item["id"], "cancelled",
                answer="resolved outside Perun",
                answered_by="opencode",
            ):
                registry.log_activity(
                    item.get("project_id"), "attention_expired",
                    f"att_{item['id']} closed: the {kind} request is gone",
                    {"attention_id": item["id"], "target_kind": kind},
                )
                closed += 1
    return closed


def poll_once(api: Optional["opencode_api.OpenCodeAPI"] = None) -> Dict[str, int]:
    """One detection pass. Returns counts for logging/tests."""
    with _poll_lock:
        return _poll_once_locked(api)


def _poll_once_locked(api: Optional["opencode_api.OpenCodeAPI"] = None) -> Dict[str, int]:
    global _last_sweep

    settings = config.Settings.load()
    if not getattr(settings, "attention_enabled", False):
        return {"created": 0, "locations": 0, "closed": 0}

    client = api or opencode_api.default_api()
    try:
        active = client.active_sessions()
    except opencode_api.OpenCodeError as exc:
        LOG.debug("attention watcher: opencode unavailable (%s)", exc)
        return {"created": 0, "locations": 0, "closed": 0}

    locations: List[str] = []
    for session_id in list(active):
        directory = _session_directory(_session_info(client, str(session_id)))
        if directory:
            locations.append(directory)

    if not locations or (time.time() - _last_sweep) > SWEEP_SECONDS:
        try:
            locations.extend(client.locations())
        except opencode_api.OpenCodeError:
            pass
        _last_sweep = time.time()

    try:
        ttl_minutes = int(getattr(settings, "attention_ttl_minutes",
                                  attention.DEFAULT_TTL_MINUTES)
                          or attention.DEFAULT_TTL_MINUTES)
    except (TypeError, ValueError):
        ttl_minutes = attention.DEFAULT_TTL_MINUTES

    polled = {loc for loc in {str(l) for l in locations} if loc}
    created = 0
    live_permissions: Set[str] = set()
    live_forms: Set[str] = set()

    for directory in sorted(polled):
        project = attention.project_for_location(directory)
        if not project:
            continue
        try:
            permissions = client.pending_permissions(directory)
        except opencode_api.OpenCodeError:
            permissions = []
        for perm in permissions:
            live_permissions.add(str(perm.get("id") or ""))
            item = _permission_item(client, perm, project, directory, ttl_minutes)
            if item and item.get("is_new"):
                created += 1
        try:
            forms = client.pending_forms(directory)
        except opencode_api.OpenCodeError:
            forms = []
        for form in forms:
            live_forms.add(str(form.get("id") or ""))
            item = _form_item(client, form, project, directory, ttl_minutes)
            if item and item.get("is_new"):
                created += 1

    closed = _reconcile(polled, live_permissions, live_forms)
    if created or closed:
        LOG.info("attention watcher: %d new item(s), %d closed, %d location(s)",
                 created, closed, len(polled))
    return {"created": created, "locations": len(polled), "closed": closed}
