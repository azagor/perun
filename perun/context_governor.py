"""Boundary compaction for long sessions (opt-in).

The measured workload is dominated by very long sessions, where every turn
re-sends the whole conversation. Perun cannot make the provider cheaper, but it
can ask OpenCode to compact a session **before** the context overflows, using
OpenCode's own ``POST /session/{id}/compact``.

Three rules make this safe:

* it is off by default — compaction rewrites the conversation and invalidates
  the provider's prompt cache;
* it never touches a session that is currently active, and never compacts more
  often than the configured cooldown (in messages, counted from the usage
  snapshot);
* a compaction that does not shrink the live context by at least 10% counts as
  a failure; three consecutive failures disable the governor instead of burning
  money silently.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from . import config, db, opencode_api, registry

LOG = logging.getLogger("perun.context")

#: How often ``tick()`` may look at sessions.
CHECK_INTERVAL_SECONDS = 60.0

#: A compaction must remove at least this share of the live context to count
#: as effective (0.9 = 10% smaller).
MIN_REDUCTION = 0.9

_last_check = 0.0


def live_context_tokens(api: opencode_api.OpenCodeAPI, session_id: str) -> int:
    """Current context size, estimated from the newest assistant message.

    Each assistant message reports the input it was sent; the newest one is the
    conversation as the provider last saw it (``cache.read`` is part of the
    same prefix). Never raises.
    """
    try:
        rows, _info = api.message_page(session_id, limit=50)
    except Exception:
        return 0
    for row in reversed(rows or []):
        tokens = row.get("tokens") if isinstance(row, dict) else None
        if not isinstance(tokens, dict):
            continue
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        total = int(tokens.get("input") or 0) + int(cache.get("read") or 0)
        if total:
            return total
    return 0


def _events(session_pk: int, limit: int = 3) -> list[dict[str, Any]]:
    with db.db() as conn:
        rows = db.rows(
            conn,
            "SELECT * FROM context_events WHERE session_pk=? ORDER BY id DESC LIMIT ?",
            (int(session_pk), int(limit)),
        )
        return [dict(r) for r in rows]


def _ineffective(session_pk: int) -> bool:
    """Three consecutive compactions that did not shrink the context."""
    rows = [r for r in _events(session_pk, 3) if int(r.get("tokens_after") or 0)]
    if len(rows) < 3:
        return False
    return all(
        int(r["tokens_after"]) >= int(r.get("tokens_before") or 0) * MIN_REDUCTION
        for r in rows
    )


def maybe_compact(
    api: opencode_api.OpenCodeAPI,
    session: dict[str, Any],
    settings: config.Settings,
) -> dict[str, Any]:
    """Compact one session when the thresholds say so. Never raises."""
    pk = int(session.get("id") or 0)
    sid = str(session.get("opencode_session_id") or "").strip()
    if not pk or not sid:
        return {"ok": True, "skipped": "no_session"}
    try:
        if api.session_active(sid):
            return {"ok": True, "skipped": "active"}
    except Exception:
        return {"ok": True, "skipped": "service_unavailable"}

    tokens = live_context_tokens(api, sid)
    if tokens < int(settings.context_governor_tokens or 0):
        return {"ok": True, "skipped": "below_threshold"}

    messages = int(session.get("usage_messages") or 0)
    cost = float(session.get("cost_usd") or 0)
    last = registry.last_context_event(pk)
    if last:
        gap = messages - int(last.get("messages_before") or 0)
        if gap < int(settings.context_governor_turns or 0):
            return {"ok": True, "skipped": "cooldown"}
        # Close out the previous compaction with what the context looks like now.
        if not int(last.get("tokens_after") or 0):
            registry.update_context_event(
                int(last["id"]), tokens_after=tokens, messages_after=messages,
                cost_after=cost)
        if _ineffective(pk):
            settings.context_governor_enabled = False
            try:
                settings.save()
            except Exception:  # pragma: no cover - the in-memory flag still wins
                LOG.debug("could not persist governor self-disable", exc_info=True)
            LOG.warning("context governor disabled: compactions are not shrinking "
                        "session %s", pk)
            return {"ok": False, "disabled": True,
                    "reason": "compactions did not reduce the context"}

    event = registry.record_context_event(
        pk, "compact", tokens_before=tokens, cost_before=cost,
        messages_before=messages)
    try:
        api.compact(sid)
    except Exception as exc:
        registry.update_context_event(int(event.get("id") or 0),
                                      note=f"failed: {exc}"[:300])
        return {"ok": False, "reason": str(exc)[:300]}
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
    skipped: list[str] = []
    for session in registry.recent_sessions(limit=10):
        try:
            result = maybe_compact(api, session, s)
        except Exception as exc:  # pragma: no cover - governor must not break
            LOG.debug("governor check failed", exc_info=True)
            skipped.append(str(exc)[:120])
            continue
        if result.get("compacted"):
            compacted += 1
        elif result.get("skipped"):
            skipped.append(str(result["skipped"]))
        if result.get("disabled"):
            break
    return {"ok": True, "compacted": compacted,
            "skipped": skipped[:10], "enabled": bool(s.context_governor_enabled)}


__all__ = ["MIN_REDUCTION", "live_context_tokens", "maybe_compact", "tick"]
