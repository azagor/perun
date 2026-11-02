"""Cron expression helpers for Perun coroutines.

Only standard five-field cron expressions are accepted (minute, hour,
day-of-month, month, day-of-week). The dashboard preview and the scheduler both
call the functions in this module, so they can never disagree about the next
fire time.

The actual parsing is delegated to ``croniter`` (a well-tested library) rather
than re-implementing calendar edge cases. This module adds the parts Perun
cares about:

* timezone resolution (``zoneinfo``, which needs ``tzdata`` on Windows),
* a bounded "does this ever fire?" validation,
* a DST fall-back guard so a daily 02:00 job does not fire twice on the one
  night the clock repeats (sub-daily schedules keep firing hourly, because the
  repeated hour is real elapsed time), and
* plain epoch-in/epoch-out values so SQLite and the scheduler stay simple.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, CroniterBadDateError, croniter

#: Reject pathological expressions before they reach croniter.
MAX_EXPR_LENGTH = 200

#: How far ahead validation looks for a real occurrence. Ten years covers a
#: leap-day expression created just after the previous leap day.
MAX_HORIZON_SECONDS = 10 * 366 * 86400


class CronError(ValueError):
    """A cron expression is malformed or can never fire."""


def resolve_tz(name: str | None) -> object:
    """Return a tzinfo for an IANA name, or the system's local timezone.

    The dashboard sends the browser's IANA zone (for example
    ``Europe/Warsaw``), so the normal path is a real ``ZoneInfo`` with correct
    DST transitions. An empty name falls back to the current local offset; that
    is fine for a fixed-offset machine but the UI should always send a name.
    """
    value = (name or "").strip()
    if not value or value.lower() in {"local", "system"}:
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"unknown timezone: {value}") from exc


def _clean(expr: str) -> str:
    value = " ".join(str(expr or "").split())
    if not value:
        raise CronError("cron expression is required")
    if len(value) > MAX_EXPR_LENGTH:
        raise CronError("cron expression is too long")
    fields = value.split(" ")
    if len(fields) != 5:
        raise CronError("use a 5-field cron expression (minute hour day month weekday)")
    if not croniter.is_valid(value):
        raise CronError("invalid cron expression")
    return value


def _sub_daily(expr: str) -> bool:
    """True when the expression can fire more than once per day.

    During a DST fall-back the wall clock repeats for one hour. A schedule
    pinned to a specific hour must not fire twice, but an hourly/minute
    schedule must keep firing every hour — that repeated hour is real time.
    """
    fields = expr.split(" ")
    hour = fields[1] if len(fields) > 1 else "*"
    return hour == "*" or hour.startswith("*/")


def next_after(expr: str, tz: str | None, after_epoch: float) -> float:
    """Return the next occurrence strictly after *after_epoch*, as an epoch.

    ``after_epoch`` is normally the previous scheduled slot, not the actual
    fire time. Anchoring on the slot keeps a delayed job from drifting.
    """
    value = _clean(expr)
    zone = resolve_tz(tz)
    after = datetime.fromtimestamp(float(after_epoch), zone)
    iterator = croniter(value, after, day_or=True, ret_type=datetime)
    try:
        candidate = iterator.get_next(datetime)
    except CroniterBadDateError as exc:
        raise CronError("cron expression never fires") from exc
    # DST fall-back: "0 2 * * *" exists twice on the night the clock repeats.
    # If the candidate has the same wall clock as the slot we are advancing
    # from, it is the second copy of the same local time; skip it so a daily
    # job still runs once that day.
    # Compare instants, not wall-clock aware datetimes: Python treats two
    # aware values with the same wall clock as equal even when their UTC
    # offsets differ (the fall-back duplicate).
    if (not _sub_daily(value)
            and candidate.replace(tzinfo=None) == after.replace(tzinfo=None)
            and candidate.timestamp() > after.timestamp()):
        try:
            candidate = iterator.get_next(datetime)
        except CroniterBadDateError as exc:
            raise CronError("cron expression never fires") from exc
    return float(candidate.timestamp())


def next_n(expr: str, tz: str | None, after_epoch: float, count: int = 5) -> List[float]:
    """Return up to *count* future occurrences, useful for the wizard preview."""
    if count < 1:
        return []
    value = _clean(expr)
    zone = resolve_tz(tz)
    after = datetime.fromtimestamp(float(after_epoch), zone)
    iterator = croniter(value, after, day_or=True, ret_type=datetime)
    out: List[float] = []
    previous = after
    while len(out) < min(int(count), 100):
        try:
            candidate = iterator.get_next(datetime)
        except CroniterBadDateError as exc:
            raise CronError("cron expression never fires") from exc
        if (not _sub_daily(value)
                and candidate.replace(tzinfo=None) == previous.replace(tzinfo=None)
                and candidate.timestamp() > previous.timestamp()):
            previous = candidate
            continue
        out.append(float(candidate.timestamp()))
        previous = candidate
    return out


def validate(expr: str, tz: str | None = None) -> str | None:
    """Return an error message, or ``None`` when *expr* is usable.

    The check includes "does this ever actually fire?" — ``0 0 30 2 *`` is
    syntactically valid but February never has a 30th, so it is rejected now
    instead of silently doing nothing forever.
    """
    try:
        value = _clean(expr)
        resolve_tz(tz)
        first = next_after(value, tz, time.time())
    except CronError as exc:
        return str(exc)
    except (CroniterBadCronError, ValueError) as exc:
        return f"invalid cron expression: {exc}"
    if first - time.time() > MAX_HORIZON_SECONDS:
        return "cron expression has no occurrence in the next 10 years"
    return None


def interval_seconds(value: str | int | float) -> int:
    """Parse an interval trigger expression (seconds) with a sane minimum."""
    try:
        seconds = int(float(value))
    except (TypeError, ValueError) as exc:
        raise CronError("interval must be a number of seconds") from exc
    if seconds < 60:
        raise CronError("interval must be at least 60 seconds")
    if seconds > 366 * 86400:
        raise CronError("interval must be at most one year")
    return seconds
