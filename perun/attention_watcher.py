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
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Set

from . import attention, config, opencode_api, registry

LOG = logging.getLogger("perun.attention.watcher")

SWEEP_SECONDS = 300.0

# Module state is process-local: the watcher only ever runs inside the Telegram
# bridge process, which is a singleton thanks to the bot-token PidLock.
_last_sweep = 0.0
_session_cache: Dict[str, Dict[str, Any]] = {}


def reset_cache() -> None:
    """Drop cached session metadata (tests, or after a service restart)."""
    global _last_sweep
    _last_sweep = 0.0
    _session_cache.clear()


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
                     project: Dict[str, Any], directory: str) -> Optional[Dict[str, Any]]:
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
        ttl_minutes=_ttl_minutes(),
        dedupe_key=f"permission|{request_id}",
    )


def _form_item(api: "opencode_api.OpenCodeAPI", form: Dict[str, Any],
               project: Dict[str, Any], directory: str) -> Optional[Dict[str, Any]]:
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
        ttl_minutes=_ttl_minutes(),
        dedupe_key=f"form|{form_id}",
    )


def _ttl_minutes() -> int:
    try:
        return int(config.Settings.load().attention_ttl_minutes)
    except Exception:  # pragma: no cover - defensive
        return attention.DEFAULT_TTL_MINUTES


def _reconcile(polled: Set[str], live_permissions: Set[str],
               live_forms: Set[str]) -> int:
    """Close items whose permission/form disappeared (answered in the TUI)."""
    closed = 0
    for kind, live in (("permission", live_permissions), ("form", live_forms)):
        for item in registry.open_attention(state="open", limit=200):
            if item["target_kind"] != kind:
                continue
            ref = item.get("ref") or {}
            if str(ref.get("location") or "") not in polled:
                continue
            key = str(ref.get("request_id") if kind == "permission" else ref.get("form_id"))
            if not key or key in live:
                continue
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
            item = _permission_item(client, perm, project, directory)
            if item and item.get("is_new"):
                created += 1
        try:
            forms = client.pending_forms(directory)
        except opencode_api.OpenCodeError:
            forms = []
        for form in forms:
            live_forms.add(str(form.get("id") or ""))
            item = _form_item(client, form, project, directory)
            if item and item.get("is_new"):
                created += 1

    closed = _reconcile(polled, live_permissions, live_forms)
    if created or closed:
        LOG.info("attention watcher: %d new item(s), %d closed, %d location(s)",
                 created, closed, len(polled))
    return {"created": created, "locations": len(polled), "closed": closed}
