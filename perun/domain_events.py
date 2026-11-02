"""Durable domain events for coroutine triggers.

A coroutine can listen for things that happen in Perun: a run finishes, a
proposal is approved, an operator-inbox item is created, a git commit is made,
or a PerunChat room message arrives.

Publishers must never depend on the scheduler being in the same process (the
PerunChat bridge may run as ``perun bridge`` while the scheduler lives in
``serve``), so this module is deliberately a *durable inbox*, not an in-memory
bus:

* ``publish()`` inserts one row into ``coroutine_events`` and returns.
* The scheduler polls ``pending()`` on its tick, matches events to active jobs,
  and marks them handled.
* A unique ``dedupe_key`` makes a gateway replay after a reconnect harmless.

There is no import of ``coroutines`` here: call sites can import this module
without creating a cycle.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import config, db, registry

logger = logging.getLogger(__name__)

#: A single event payload is capped so a hostile webhook cannot grow the DB
#: without bound. Room text is capped again by the bridge.
MAX_PAYLOAD_BYTES = 16 * 1024

#: Handled events are kept this long for the dashboard inbox.
RETENTION_SECONDS = 7 * 24 * 3600

#: Hard row cap; the oldest handled rows are pruned first.
MAX_EVENT_ROWS = 5000

_wake: Callable[[], None] | None = None
_wake_lock = threading.Lock()
_PENDING_CAP: tuple[float, int] = (0.0, 5000)


def _pending_cap() -> int:
    """The configured unhandled-event cap, cached for 30 seconds."""
    global _PENDING_CAP
    now = time.time()
    with _wake_lock:
        ts, value = _PENDING_CAP
        if now - ts < 30.0:
            return value
    try:
        value = int(getattr(config.Settings.load(),
                            "coroutine_max_pending_events", 5000) or 0)
    except Exception:
        value = 5000
    with _wake_lock:
        _PENDING_CAP = (now, value)
    return value


def set_waker(callback: Callable[[], None] | None) -> None:
    """Register the scheduler's wake function (optional)."""
    global _wake
    with _wake_lock:
        _wake = callback


def _notify() -> None:
    """Wake the scheduler after a commit; never let this raise into a caller."""
    with _wake_lock:
        callback = _wake
    if callback is None:
        return
    try:
        callback()
    except Exception:  # pragma: no cover - a wake failure must not lose an event
        logger.debug("coroutine event waker failed", exc_info=True)


def publish(
    source: str,
    dedupe_key: str,
    payload: dict[str, Any] | str | None = None,
    *,
    room_id: str = "",
    sender: str = "",
    ttl_seconds: int = RETENTION_SECONDS,
) -> int | None:
    """Store one durable event.

    Returns the new row id, or ``None`` when the event was a duplicate or could
    not be written. Publishing is best-effort by design: a failed event must
    never fail the run/proposal/chat action that produced it.
    """
    source = str(source or "").strip()
    dedupe_key = str(dedupe_key or "").strip()
    if not source or not dedupe_key:
        logger.debug("domain event without source/dedupe key ignored")
        return None
    cap = _pending_cap()
    if cap > 0:
        try:
            with db.db() as conn:
                row = db.one(
                    conn,
                    "SELECT COUNT(*) AS n FROM coroutine_events WHERE handled_at=''",
                )
            if int(row["n"] if row else 0) >= cap:
                logger.warning(
                    "coroutine event inbox is full (%s unhandled); dropping %s",
                    cap, dedupe_key,
                )
                return None
        except Exception:  # pragma: no cover - never fail a publisher
            pass
    raw = json.dumps(payload if payload is not None else {}, ensure_ascii=False,
                     default=str)
    if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raw = json.dumps({
            "_truncated": True,
            "preview": raw[: MAX_PAYLOAD_BYTES - 80],
        }, ensure_ascii=False)
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=max(60, int(ttl_seconds)))).isoformat()
    try:
        with db.db() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO coroutine_events
                   (source, dedupe_key, room_id, sender, payload,
                    received_at, handled_at, expires_at)
                   VALUES (?,?,?,?,?,?, '', ?)""",
                (source, dedupe_key, str(room_id or ""), str(sender or ""),
                 raw, now.isoformat(), expires),
            )
            event_id = int(cur.lastrowid) if cur.rowcount else None
    except Exception:  # pragma: no cover - storage is best effort
        logger.debug("could not store domain event %s", dedupe_key, exc_info=True)
        return None
    if event_id is not None:
        _notify()
    return event_id


def pending(limit: int = 50) -> list[dict[str, Any]]:
    """Unhandled events, oldest first."""
    try:
        with db.db() as conn:
            rows = db.rows(
                conn,
                """SELECT * FROM coroutine_events
                   WHERE handled_at='' ORDER BY id ASC LIMIT ?""",
                (max(1, min(int(limit), 500)),),
            )
        return [dict(r) for r in rows]
    except Exception:  # pragma: no cover - a missing table predates migration
        return []


def mark_handled(event_id: int) -> None:
    try:
        with db.db() as conn:
            conn.execute(
                "UPDATE coroutine_events SET handled_at=? WHERE id=? AND handled_at=''",
                (registry.now_iso(), int(event_id)),
            )
    except Exception:  # pragma: no cover
        logger.debug("could not mark event %s handled", event_id, exc_info=True)


def recent(limit: int = 50, *, coroutine_id: int | None = None) -> list[dict[str, Any]]:
    """Recent events for the detail view (optionally only deliveries for a job)."""
    try:
        with db.db() as conn:
            if coroutine_id:
                rows = db.rows(
                    conn,
                    """SELECT e.*, d.id AS delivery_id,
                              d.status AS delivery_status, d.run_uid,
                              d.error AS delivery_error, d.handled_at AS delivery_handled_at
                       FROM coroutine_events e
                       JOIN coroutine_event_deliveries d ON d.event_id = e.id
                       WHERE d.coroutine_id=?
                       ORDER BY e.id DESC LIMIT ?""",
                    (int(coroutine_id), max(1, min(int(limit), 200))),
                )
            else:
                rows = db.rows(
                    conn,
                    "SELECT * FROM coroutine_events ORDER BY id DESC LIMIT ?",
                    (max(1, min(int(limit), 200)),),
                )
        return [dict(r) for r in rows]
    except Exception:  # pragma: no cover
        return []


def prune() -> int:
    """Drop old events and cap the table. Returns the number of rows removed."""
    removed = 0
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=RETENTION_SECONDS)).isoformat()
    try:
        with db.db() as conn:
            cur = conn.execute(
                """DELETE FROM coroutine_events
                   WHERE (handled_at <> '' AND received_at < ?)
                      OR (expires_at <> '' AND expires_at < ?)""",
                (cutoff, now.isoformat()),
            )
            removed += int(cur.rowcount or 0)
            # Hard cap: delete the oldest handled rows beyond MAX_EVENT_ROWS.
            cur = conn.execute(
                """DELETE FROM coroutine_events
                   WHERE handled_at <> '' AND id NOT IN (
                       SELECT id FROM coroutine_events ORDER BY id DESC LIMIT ?
                   )""",
                (MAX_EVENT_ROWS,),
            )
            removed += int(cur.rowcount or 0)
            # Foreign keys are not enforced globally; drop deliveries whose
            # event was pruned so the table cannot grow without bound.
            conn.execute(
                """DELETE FROM coroutine_event_deliveries
                   WHERE event_id NOT IN (SELECT id FROM coroutine_events)"""
            )
    except Exception:  # pragma: no cover
        logger.debug("could not prune coroutine events", exc_info=True)
    return removed


__all__ = [
    "MAX_PAYLOAD_BYTES", "RETENTION_SECONDS", "mark_handled", "pending",
    "prune", "publish", "recent", "set_waker",
]
