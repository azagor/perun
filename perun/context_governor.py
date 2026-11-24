"""Boundary compaction for long sessions (opt-in).

The measured workload is dominated by very long sessions, where every turn
re-sends the whole conversation. Perun cannot make the provider cheaper, but it
can ask OpenCode to compact a session **before** the context overflows, using
OpenCode's own ``POST /session/{id}/compact``.

Safety rules:

* off by default — compaction rewrites the conversation and invalidates the
  provider's prompt cache;
* **shadow mode is on by default**: the governor records what it *would* do and
  never calls OpenCode until the operator turns shadow off;
* it never touches an active session, never compacts more often than the
  cooldown, never touches a session idle for longer than the configured window,
  and stops at the daily cap;
* three consecutive compactions that do not shrink the context disable it.

``settle()`` closes the previous compaction's after-values as soon as the session
is observed idle, *before* the compact decision, so an active session at the next
tick cannot prevent the measurement (and therefore the self-disable). The
measurement is anchored to the summariser message the compact call created, so a
turn that was already in flight is never mistaken for the post-compaction state.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config, db, opencode_api, registry

LOG = logging.getLogger("perun.context")

#: How often ``tick()`` may look at sessions.
CHECK_INTERVAL_SECONDS = 60.0

#: A compaction must remove at least this share of the live context to count
#: as effective (0.9 = 10% smaller).
MIN_REDUCTION = 0.9

#: Clock tolerance when matching the summariser row to the recorded event.
_COMPACTION_SLACK = 5.0

#: How long to wait for the summariser row before measuring anyway (seconds).
_COMPACTION_GRACE = 600.0

_last_check = 0.0

#: Last tick summary, surfaced by ``GET /api/local-ai`` so a silent failure is
#: visible in the dashboard.
_LAST_TICK: dict[str, Any] = {
    "ts": "", "checked": 0, "compacted": 0, "would": 0, "skipped": [],
}


def context_tokens(tokens: dict[str, Any]) -> int:
    """Context size from one assistant message's usage.

    Providers disagree about the fields: Anthropic-style ``input`` excludes the
    cached prefix, OpenAI-style includes it. When ``cache.read`` is larger than
    ``input`` the fields are disjoint and the context is their sum; otherwise
    ``input`` already covers the cached part.
    """
    inp = int(tokens.get("input") or 0)
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    cached = int(cache.get("read") or 0)
    if cached > inp:
        return inp + cached
    return inp or (inp + cached)


def _message_time(row: dict[str, Any]) -> float:
    """Creation time of one message in epoch seconds (0 when unknown)."""
    raw = row.get("time")
    if isinstance(raw, dict):
        raw = raw.get("created")
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    # OpenCode reports milliseconds since the epoch.
    return value / 1000.0 if value > 1e11 else value


def _measure(api: opencode_api.OpenCodeAPI, session_id: str, *,
             anchor: str = "") -> dict[str, Any]:
    """Newest assistant usage plus the compaction markers of one session.

    ``/message`` returns messages newest-first. The timestamps decide which row
    is newest (a reordered page cannot flip the answer), and the first matching
    row wins when a fake or an older service omits them. ``anchor`` is the id
    the compact call returned, so the caller can tell whether a normal turn has
    arrived since that exact summariser request.
    """
    empty: dict[str, Any] = {
        "tokens": 0, "assistant_at": 0.0, "compaction_at": 0.0,
        "assistant_index": -1, "anchor_index": -1, "page_size": 0,
    }
    try:
        rows, _info = api.message_page(session_id, limit=50)
    except Exception:
        return empty
    tokens = 0
    assistant_at = -1.0
    assistant_index = -1
    compaction_at = 0.0
    anchor_index = -1
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        if anchor and str(row.get("id") or "") == anchor:
            anchor_index = index
        when = _message_time(row)
        # A ``compaction`` row is the summariser's own request: its input is the
        # *old* context plus the summary prompt, not the post-compaction size.
        # Only a normal assistant turn reports the context that will be sent
        # from now on.
        if str(row.get("type") or "assistant") == "compaction":
            compaction_at = max(compaction_at, when)
            continue
        if str(row.get("type") or "assistant") != "assistant":
            continue
        usage = row.get("tokens")
        if not isinstance(usage, dict):
            continue
        total = context_tokens(usage)
        if total and when > assistant_at:
            assistant_at, tokens, assistant_index = when, total, index
    return {
        "tokens": tokens,
        "assistant_at": max(assistant_at, 0.0),
        "compaction_at": compaction_at,
        "assistant_index": assistant_index,
        "anchor_index": anchor_index,
        "page_size": len(rows or []),
    }


def live_context_tokens(api: opencode_api.OpenCodeAPI, session_id: str) -> int:
    """Current context size, estimated from the newest assistant message."""
    return int(_measure(api, session_id)["tokens"])


def _events(session_pk: int, limit: int = 3,
            action: str = "compact") -> list[dict[str, Any]]:
    with db.db() as conn:
        rows = db.rows(
            conn,
            "SELECT * FROM context_events WHERE session_pk=? AND action=? "
            "ORDER BY id DESC LIMIT ?",
            (int(session_pk), str(action), int(limit)),
        )
        return [dict(r) for r in rows]


def _ineffective(session_pk: int) -> bool:
    """Three consecutive real compactions that did not shrink the context."""
    rows = [r for r in _events(session_pk, 3)
            if int(r.get("tokens_after") or 0)
            # An event whose after-values never advanced is unmeasured (the
            # usage snapshot was stale), not evidence that compaction failed.
            and int(r.get("messages_after") or 0) > int(r.get("messages_before") or 0)]
    if len(rows) < 3:
        return False
    return all(
        int(r["tokens_after"]) >= int(r.get("tokens_before") or 0) * MIN_REDUCTION
        for r in rows
    )


def _daily_compactions() -> int:
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    return sum(1 for event in registry.context_events_since(start)
               if event.get("action") == "compact")


def _idle(session: dict[str, Any], minutes: int) -> bool:
    """Whether the session has had no usage snapshot for longer than *minutes*."""
    if int(minutes or 0) <= 0:
        return False
    raw = str(session.get("usage_at") or "")
    try:
        seen = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - seen > timedelta(minutes=int(minutes))


def _disable(settings: config.Settings, session: dict[str, Any], reason: str) -> None:
    settings.context_governor_enabled = False
    try:
        settings.save()
    except Exception:  # pragma: no cover - the in-memory flag still wins
        LOG.debug("could not persist governor self-disable", exc_info=True)
    LOG.warning("context governor disabled: %s", reason)
    try:
        registry.log_activity(
            int(session.get("project_id") or 0) or None,
            "context_governor",
            f"Context governor disabled: {reason}",
            {"session_pk": session.get("id")},
        )
    except Exception:  # pragma: no cover - activity is best effort
        LOG.debug("could not record the governor self-disable", exc_info=True)


def _iso_seconds(raw: str) -> float:
    """Parse an event timestamp into epoch seconds (0 when unreadable)."""
    try:
        seen = datetime.fromisoformat(str(raw))
    except ValueError:
        return 0.0
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return seen.timestamp()


def settle(session: dict[str, Any],
           api: opencode_api.OpenCodeAPI,
           measure: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fill the previous compaction's after-values once the session is idle."""
    pk = int(session.get("id") or 0)
    sid = str(session.get("opencode_session_id") or "").strip()
    if not pk or not sid:
        return {"settled": False}
    last = registry.last_context_event(pk)
    if not last or last.get("action") != "compact":
        return {"settled": False}
    # Already measured: tokens and a message delta are both recorded.
    if int(last.get("tokens_after") or 0) and int(last.get("messages_after") or 0):
        return {"settled": False}
    try:
        if api.session_active(sid):
            return {"settled": False, "active": True}
    except Exception:
        return {"settled": False}
    anchor = str(last.get("compaction_message_id") or "")
    if measure is None:
        measure = _measure(api, sid, anchor=anchor)
    tokens = int(measure["tokens"])
    if not tokens:
        # The only token-bearing row is the compaction request itself; wait
        # for the first normal assistant turn so "after" is a real post-state.
        return {"settled": False, "waiting": "next_turn"}
    anchor_index = int(measure["anchor_index"])
    if anchor and anchor_index >= 0:
        # The page is newest-first: a turn below the summariser row predates it.
        if int(measure["assistant_index"]) > anchor_index:
            return {"settled": False, "waiting": "next_turn"}
    else:
        # Legacy event, or the anchor scrolled out of the page: fall back to
        # timestamps. A full page means the anchor is older than everything in
        # it, so the newest turn is post-compaction either way.
        started = _iso_seconds(str(last.get("created_at") or ""))
        assistant_at = float(measure["assistant_at"])
        if assistant_at:
            # A turn that finished before the compaction was requested still
            # carries the old context, and one that is older than the summariser
            # row was written before OpenCode replaced the conversation.
            if started and assistant_at <= started:
                return {"settled": False, "waiting": "next_turn"}
            compaction_at = float(measure["compaction_at"])
            if compaction_at >= started - _COMPACTION_SLACK:
                if assistant_at <= compaction_at:
                    return {"settled": False, "waiting": "next_turn"}
            elif (started and int(measure["page_size"]) < 50
                    and time.time() - started < _COMPACTION_GRACE):
                # The summariser row is not visible yet, so the compaction has
                # not been applied and the newest turn is still pre-compaction.
                return {"settled": False, "waiting": "compaction"}
    messages = int(session.get("usage_messages") or 0)
    if messages <= int(last.get("messages_before") or 0):
        # Guard the cost/messages snapshot too: writing a "before" copy as
        # "after" would fake a $0.00 delta (the compaction row itself advances
        # the count, so the gates above decide whether a real turn exists).
        return {"settled": False, "waiting": "usage_snapshot"}
    registry.update_context_event(
        int(last["id"]),
        tokens_after=tokens,
        messages_after=messages,
        cost_after=float(session.get("cost_usd") or 0),
    )
    return {"settled": True, "tokens": tokens}


def maybe_compact(
    api: opencode_api.OpenCodeAPI,
    session: dict[str, Any],
    settings: config.Settings,
    measure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide for one session: skip, record a shadow event, or compact."""
    pk = int(session.get("id") or 0)
    sid = str(session.get("opencode_session_id") or "").strip()
    if not pk or not sid:
        return {"ok": True, "skipped": "no_session"}
    try:
        if api.session_active(sid):
            return {"ok": True, "skipped": "active"}
    except Exception:
        return {"ok": True, "skipped": "service_unavailable"}

    tokens = (int(measure["tokens"]) if measure is not None
              else live_context_tokens(api, sid))
    if tokens < int(settings.context_governor_tokens or 0):
        return {"ok": True, "skipped": "below_threshold"}

    if _idle(session, int(settings.context_governor_idle_minutes or 0)):
        return {"ok": True, "skipped": "idle"}

    messages = int(session.get("usage_messages") or 0)
    cost = float(session.get("cost_usd") or 0)
    last = registry.last_context_event(pk)
    if last and last.get("action") in ("compact", "would-compact"):
        gap = messages - int(last.get("messages_before") or 0)
        if gap < int(settings.context_governor_turns or 0):
            return {"ok": True, "skipped": "cooldown"}

    cap = int(settings.context_governor_max_per_day or 0)
    if cap and _daily_compactions() >= cap:
        return {"ok": True, "skipped": "daily_cap"}

    if settings.context_governor_shadow:
        event = registry.record_context_event(
            pk, "would-compact", tokens_before=tokens, cost_before=cost,
            messages_before=messages)
        LOG.info("would compact session %s at %d tokens (shadow)", sid, tokens)
        return {"ok": True, "shadow": True, "tokens": tokens,
                "event": int(event.get("id") or 0)}

    # The session can become active during the checks above; re-check right
    # before touching it.
    try:
        if api.session_active(sid):
            return {"ok": True, "skipped": "active"}
    except Exception:
        return {"ok": True, "skipped": "service_unavailable"}

    event = registry.record_context_event(
        pk, "compact", tokens_before=tokens, cost_before=cost,
        messages_before=messages)
    try:
        reply = api.compact(sid)
    except Exception as exc:
        registry.update_context_event(int(event.get("id") or 0),
                                      note=f"failed: {exc}"[:300])
        return {"ok": False, "reason": str(exc)[:300]}
    # Anchor the measurement to the summariser request the API just created, so
    # settle() can tell a post-compaction turn from an in-flight one.
    anchor = str((reply or {}).get("id") or "") if isinstance(reply, dict) else ""
    if anchor:
        try:
            registry.set_context_event_anchor(int(event.get("id") or 0), anchor)
        except Exception:  # pragma: no cover - measurement still falls back
            LOG.debug("could not record the compaction anchor", exc_info=True)
    LOG.info("compacted session %s at %d tokens", sid, tokens)
    return {"ok": True, "compacted": True, "tokens": tokens,
            "event": int(event.get("id") or 0)}


def tick(settings: config.Settings | None = None,
         api: opencode_api.OpenCodeAPI | None = None) -> dict[str, Any]:
    """One governor pass. Safe to call from the existing reconcile loop."""
    global _last_check
    s = settings or config.Settings.load()
    if not s.context_governor_enabled:
        return {"ok": True, "skipped": "disabled"}
    now = time.monotonic()
    if now - _last_check < CHECK_INTERVAL_SECONDS:
        return {"ok": True, "skipped": "interval"}
    _last_check = now
    api = api or opencode_api.default_api()

    compacted = 0
    would = 0
    skipped: list[str] = []
    sessions = registry.recent_sessions(limit=10)
    for session in sessions:
        # One message page per session per tick, shared by the measurement and
        # the decision (each carries full message content).
        pk = int(session.get("id") or 0)
        sid = str(session.get("opencode_session_id") or "").strip()
        anchor = ""
        if pk:
            try:
                last = registry.last_context_event(pk)
            except Exception:  # pragma: no cover - governor must not break
                last = None
            anchor = str((last or {}).get("compaction_message_id") or "")
        measure = _measure(api, sid, anchor=anchor) if sid else None
        # Close the previous compaction first, so the measurement cannot be
        # starved by an active session and the self-disable can fire.
        try:
            settle(session, api, measure=measure)
            if _ineffective(pk):
                _disable(s, session, "compactions did not reduce the context")
                break
        except Exception:  # pragma: no cover - governor must not break
            LOG.debug("governor settle failed", exc_info=True)
        try:
            result = maybe_compact(api, session, s, measure=measure)
        except Exception as exc:  # pragma: no cover - governor must not break
            LOG.debug("governor check failed", exc_info=True)
            skipped.append(str(exc)[:120])
            continue
        if result.get("compacted"):
            compacted += 1
        elif result.get("shadow"):
            would += 1
        elif result.get("skipped"):
            skipped.append(str(result["skipped"]))

    summary = {
        "ts": registry.now_iso(),
        "checked": len(sessions),
        "compacted": compacted,
        "would": would,
        "skipped": skipped[:10],
    }
    _LAST_TICK.update(summary)
    summary["enabled"] = bool(s.context_governor_enabled)
    summary["shadow"] = bool(s.context_governor_shadow)
    return {"ok": True, **summary}


def last_status() -> dict[str, Any]:
    """The last tick summary (empty until the first pass)."""
    return dict(_LAST_TICK)


__all__ = [
    "MIN_REDUCTION",
    "context_tokens",
    "last_status",
    "live_context_tokens",
    "maybe_compact",
    "settle",
    "tick",
]
