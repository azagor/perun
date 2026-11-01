"""Token/cost capture: the service snapshot, the registry store, the totals."""

from __future__ import annotations

from perun import opencode_api, registry


def test_usage_sums_the_session_messages():
    api = opencode_api.OpenCodeAPI.__new__(opencode_api.OpenCodeAPI)
    api.messages = lambda session_id, limit=50: [  # type: ignore[assignment]
        {"tokens": {"input": 100, "output": 20, "cache": {"read": 900}}, "cost": 0.001},
        {"tokens": {"input": 50, "output": 5, "cache": {"read": 400}}, "cost": 0.0005},
        {"tokens": {}},          # a user message: no usage
        {"content": "no tokens"},
    ]

    usage = api.usage("ses_x")

    assert usage == {"tokens_in": 150, "tokens_out": 25,
                     "cache_read": 1300, "cost": 0.0015}


def test_a_service_without_usage_still_returns_zeroes():
    api = opencode_api.OpenCodeAPI.__new__(opencode_api.OpenCodeAPI)
    api.messages = lambda session_id, limit=50: []  # type: ignore[assignment]
    assert api.usage("ses_x") == {"tokens_in": 0, "tokens_out": 0,
                                  "cache_read": 0, "cost": 0.0}


def test_session_usage_is_a_snapshot_not_a_running_total():
    project = registry.add_project("usage-snap", r"C:\usage-snap")
    session = registry.create_session(project["id"], "ses_snap")

    registry.update_session_usage(session["id"], {"tokens_in": 10, "tokens_out": 1,
                                                  "cache_read": 0, "cost": 0.1})
    registry.update_session_usage(session["id"], {"tokens_in": 20, "tokens_out": 2,
                                                  "cache_read": 5, "cost": 0.2})

    totals = registry.usage_totals(hours=24)
    assert totals["tokens_in"] == 20            # overwritten, not added
    assert totals["sessions"] == 1
    assert round(float(totals["cost_usd"]), 4) == 0.2


def test_totals_and_project_breakdown_ignore_sessions_without_usage():
    project = registry.add_project("usage-idle", r"C:\usage-idle")
    registry.create_session(project["id"], "ses_idle")   # no usage captured

    assert registry.usage_totals(hours=24)["sessions"] == 0
    assert registry.usage_by_project(hours=24) == []
