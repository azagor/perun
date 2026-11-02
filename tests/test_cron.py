"""Cron validation and next-fire tests for coroutine triggers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from perun import cron


def _local(epoch, tz="Europe/Warsaw"):
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(epoch, ZoneInfo(tz))


def test_validate_accepts_a_daily_expression():
    assert cron.validate("0 12 * * *", "Europe/Warsaw") is None


def test_validate_rejects_wrong_field_count():
    assert "5-field" in (cron.validate("* * * *", "Europe/Warsaw") or "")


def test_validate_rejects_an_expression_that_never_fires():
    err = cron.validate("0 0 30 2 *", "Europe/Warsaw")
    assert err and ("never" in err or "invalid" in err)


def test_next_after_is_strictly_after_the_anchor():
    # 2026-11-02 10:00 UTC == 11:00 Europe/Warsaw
    anchor = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc).timestamp()
    nxt = cron.next_after("0 12 * * *", "Europe/Warsaw", anchor)
    assert _local(nxt) == datetime(2026, 11, 2, 12, 0,
                                   tzinfo=_local(nxt).tzinfo)


def test_daily_schedule_keeps_local_noon_across_spring_dst():
    # 2026-03-28 12:00 Warsaw is +01:00; 29 March is the spring transition.
    anchor = datetime(2026, 3, 28, 12, 0,
                      tzinfo=__import__("zoneinfo").ZoneInfo("Europe/Warsaw")).timestamp()
    nxt = cron.next_after("0 12 * * *", "Europe/Warsaw", anchor)
    local = _local(nxt)
    assert (local.hour, local.minute) == (12, 0)
    assert local.utcoffset().total_seconds() == 2 * 3600


def test_fall_back_does_not_fire_daily_0200_twice():
    # The first 02:00 on 25 October 2026 is +02:00; the clock repeats at +01:00.
    from zoneinfo import ZoneInfo

    first = datetime(2026, 10, 25, 2, 0,
                     tzinfo=ZoneInfo("Europe/Warsaw")).timestamp()
    nxt = cron.next_after("0 2 * * *", "Europe/Warsaw", first)
    local = _local(nxt)
    assert local.date().isoformat() == "2026-10-26"
    assert (local.hour, local.minute) == (2, 0)


def test_fall_back_keeps_an_hourly_schedule_hourly():
    # The repeated 02:00 is real elapsed time, so an hourly job still fires.
    from zoneinfo import ZoneInfo

    first = datetime(2026, 10, 25, 2, 0,
                     tzinfo=ZoneInfo("Europe/Warsaw")).timestamp()
    nxt = cron.next_after("0 * * * *", "Europe/Warsaw", first)
    local = _local(nxt)
    assert nxt - first == 3600
    assert local.hour == 2
    assert local.utcoffset().total_seconds() == 3600


def test_next_n_returns_the_requested_count():
    anchor = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc).timestamp()
    fires = cron.next_n("30 9 * * 1-5", "Europe/Warsaw", anchor, 5)
    assert len(fires) == 5
    assert all(b > a for a, b in zip([anchor] + fires, fires))


def test_interval_has_a_one_minute_floor():
    assert cron.interval_seconds("60") == 60
    with pytest.raises(cron.CronError):
        cron.interval_seconds("59")
    with pytest.raises(cron.CronError):
        cron.interval_seconds("nope")


def test_unknown_timezone_is_rejected():
    assert "timezone" in (cron.validate("0 12 * * *", "Mars/Olympus") or "")
