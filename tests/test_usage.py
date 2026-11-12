"""Token/cost capture: the service snapshot, the registry store, the totals."""

from __future__ import annotations

from perun import opencode_api, registry


def test_usage_sums_the_session_messages():
    api = opencode_api.OpenCodeAPI.__new__(opencode_api.OpenCodeAPI)
    api.message_page = lambda session_id, *, limit=200, cursor="": (  # type: ignore[assignment]
        [
            {"id": "m1", "tokens": {"input": 100, "output": 20, "cache": {"read": 900}}, "cost": 0.001},
            {"id": "m2", "tokens": {"input": 50, "output": 5, "cache": {"read": 400}}, "cost": 0.0005},
            {"id": "m3", "tokens": {}},          # a user message: no usage
            {"id": "m4", "content": "no tokens"},
        ], {})
    usage = api.usage("ses_x")

    assert {k: usage[k] for k in ("tokens_in", "tokens_out", "cache_read", "cost")} == {
        "tokens_in": 150, "tokens_out": 25, "cache_read": 1300, "cost": 0.0015}
    assert usage["complete"] is True


def test_usage_restarts_once_when_a_cursor_expires():
    api = opencode_api.OpenCodeAPI.__new__(opencode_api.OpenCodeAPI)
    state = {"c2": 0}

    def message_page(session_id, *, limit=200, cursor=""):
        if cursor == "c2":
            state["c2"] += 1
            if state["c2"] == 1:
                raise opencode_api.OpenCodeHTTPError(400, "cursor expired", "/message")
            return ([{"id": "b", "tokens": {"input": 2, "output": 0}, "cost": 2.0}], {})
        return ([{"id": "a", "tokens": {"input": 1, "output": 0}, "cost": 1.0}],
                {"next": "c2"})

    api.message_page = message_page  # type: ignore[assignment]

    usage = api.usage("ses_cursor")

    assert usage["complete"] is True
    assert usage["messages"] == 2
    assert usage["cost"] == 3.0


def test_usage_follows_every_cursor_page():
    api = opencode_api.OpenCodeAPI.__new__(opencode_api.OpenCodeAPI)
    pages = {
        "": ([
            {"id": "a", "tokens": {"input": 10, "output": 1}, "cost": 1.0,
             "model": {"providerID": "ollama-cloud", "id": "kimi-k3.1-pro"}},
        ], {"next": "c2"}),
        "c2": ([
            {"id": "b", "tokens": {"input": 20, "output": 2}, "cost": 2.0,
             "model": {"providerID": "ollama-cloud", "id": "kimi-k3.1-pro"}},
            {"id": "b", "tokens": {"input": 20, "output": 2}, "cost": 2.0,
             "model": {"providerID": "ollama-cloud", "id": "kimi-k3.1-pro"}},
        ], {"next": "c3"}),
        "c3": ([
            {"id": "c", "tokens": {"input": 30, "output": 3}, "cost": 3.0,
             "model": {"providerID": "opencode", "id": "deepseek"}},
        ], {}),
    }
    api.message_page = lambda session_id, *, limit=200, cursor="": pages[cursor]  # type: ignore[assignment]

    usage = api.usage("ses_long")

    assert usage["pages"] == 3
    assert usage["messages"] == 3          # duplicate page entry deduped
    assert usage["cost"] == 6.0
    assert usage["complete"] is True
    assert usage["models"]["ollama-cloud/kimi-k3.1-pro"]["cost"] == 3.0
    assert usage["models"]["opencode/deepseek"]["cost"] == 3.0


def test_a_service_without_usage_still_returns_zeroes():
    api = opencode_api.OpenCodeAPI.__new__(opencode_api.OpenCodeAPI)
    api.message_page = lambda session_id, *, limit=200, cursor="": ([], {})  # type: ignore[assignment]
    usage = api.usage("ses_x")
    assert usage["tokens_in"] == 0 and usage["cost"] == 0.0 and usage["models"] == {}


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


def test_backfill_uses_ended_at_for_a_long_run():
    from perun import orchestrator
    from perun import registry as reg

    project = reg.add_project("usage-long", r"C:\usage-long")
    s1 = reg.create_session(project["id"], "ses_long_recent")
    s2 = reg.create_session(project["id"], "ses_long_old")
    reg.create_run("run_recent", project["id"], "long TUI", session_pk=s1["id"])
    reg.update_run("run_recent", started_at="2020-01-01T00:00:00+00:00",
                   ended_at=reg.now_iso())
    reg.create_run("run_old", project["id"], "old TUI", session_pk=s2["id"])
    reg.update_run("run_old", started_at="2020-01-01T00:00:00+00:00",
                   ended_at="2020-01-02T00:00:00+00:00")

    orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    calls = []
    orch.capture_usage = lambda uid: calls.append(uid) or True  # type: ignore[assignment]

    assert orch.backfill_usage(hours=24, force=True) == 1
    assert calls == ["run_recent"]


def test_usage_by_model_can_be_scoped_to_one_project():
    from perun import registry as reg

    a = reg.add_project("usage-a", r"C:\usage-a")
    b = reg.add_project("usage-b", r"C:\usage-b")
    sa = reg.create_session(a["id"], "ses_a")
    sb = reg.create_session(b["id"], "ses_b")
    reg.update_session_usage(sa["id"], {
        "tokens_in": 1, "tokens_out": 0, "cost": 1.0,
        "models": {"ollama-cloud/kimi": {"tokens_in": 1, "tokens_out": 0, "cost": 1.0}},
        "complete": True,
    })
    reg.update_session_usage(sb["id"], {
        "tokens_in": 1, "tokens_out": 0, "cost": 2.0,
        "models": {"opencode/deepseek": {"tokens_in": 1, "tokens_out": 0, "cost": 2.0}},
        "complete": True,
    })

    rows = reg.usage_by_model(hours=24, project_id=a["id"])
    assert [row["model"] for row in rows] == ["ollama-cloud/kimi"]


def test_a_snapshot_records_whether_it_reached_the_last_page():
    project = registry.add_project("usage-complete", r"C:\usage-complete")
    session = registry.create_session(project["id"], "ses_complete")

    registry.update_session_usage(session["id"], {
        "tokens_in": 5, "tokens_out": 2, "cache_read": 0, "cost": 0.5,
        "models": {"ollama-cloud/kimi": {"tokens_in": 5, "tokens_out": 2, "cost": 0.5}},
        "complete": True, "pages": 4, "messages": 40,
    })

    row = registry.get_session(session["id"])
    assert row["usage_complete"] == 1
    assert row["usage_pages"] == 4
    assert row["usage_messages"] == 40
    assert registry.usage_totals(hours=24)["incomplete"] == 0
    assert registry.usage_by_model(hours=24)[0]["model"] == "ollama-cloud/kimi"

def test_capture_usage_records_a_tui_run_session(monkeypatch):
    """A TUI run is linked to its project, so its usage lands in the same totals."""
    from perun import orchestrator, registry

    project = registry.add_project("usage-tui", r"C:\usage-tui")
    session = registry.create_session(project["id"], "ses_tui")
    registry.create_run("tui_run_1", project["id"], "tui work",
                        session_pk=session["id"],
                        opencode_session_id="ses_tui")

    class FakeAPI:
        def usage(self, session_id, limit=100):
            assert session_id == "ses_tui"
            return {"tokens_in": 500, "tokens_out": 50, "cache_read": 0, "cost": 0.01}

    monkeypatch.setattr(opencode_api, "default_api", lambda: FakeAPI())
    assert orchestrator.Orchestrator().capture_usage("tui_run_1") is True

    totals = registry.usage_totals(project_id=project["id"])
    assert totals["tokens_in"] == 500 and round(float(totals["cost_usd"]), 4) == 0.01


def test_capture_usage_is_false_without_a_session(monkeypatch):
    from perun import orchestrator, registry

    project = registry.add_project("usage-nosession", r"C:\usage-nosession")
    registry.create_run("tui_run_2", project["id"], "no session")
    assert orchestrator.Orchestrator().capture_usage("tui_run_2") is False

def test_refresh_open_usage_captures_standalone_runs(monkeypatch):
    from perun import orchestrator, registry

    project = registry.add_project("usage-open", r"C:\usage-open")
    session = registry.create_session(project["id"], "ses_open")
    registry.create_run("tui_open_1", project["id"], "open tui",
                        session_pk=session["id"],
                        opencode_session_id="ses_open", standalone=1)
    registry.update_run("tui_open_1", status="running")

    class FakeAPI:
        def usage(self, session_id, limit=100):
            return {"tokens_in": 42, "tokens_out": 4, "cache_read": 0, "cost": 0.5}

    monkeypatch.setattr(opencode_api, "default_api", lambda: FakeAPI())
    orch = orchestrator.Orchestrator()
    assert orch.refresh_open_usage() == 1
    # Within the window: no second pass.
    assert orch.refresh_open_usage() == 0
    assert registry.usage_totals(project_id=project["id"])["tokens_in"] == 42

def test_provider_usage_attributes_spend_to_the_run_model():
    from perun import registry

    project = registry.add_project("usage-provider", r"C:\usage-provider")
    session = registry.create_session(project["id"], "ses_prov")
    registry.create_run("prov_run_1", project["id"], "hi", session_pk=session["id"],
                        model="ollama-cloud/deepseek-v4.1-flash")
    registry.update_session_usage(session["id"], {"tokens_in": 100, "tokens_out": 10,
                                                  "cost": 0.01})

    rows = {r["provider"]: r for r in registry.usage_by_provider()}
    assert "ollama-cloud" in rows
    assert rows["ollama-cloud"]["tokens"] == 110
    assert round(float(rows["ollama-cloud"]["cost"]), 4) == 0.01

def test_usage_breaks_down_by_model_when_a_session_switches_provider():
    api = opencode_api.OpenCodeAPI.__new__(opencode_api.OpenCodeAPI)
    api.message_page = lambda session_id, *, limit=200, cursor="": (  # type: ignore[assignment]
        [
            {"model": {"providerID": "ollama-cloud", "id": "deepseek"},
             "tokens": {"input": 100, "output": 10, "cache": {"read": 0}}, "cost": 0.01},
            {"model": {"providerID": "opencode", "id": "kimi"},
             "tokens": {"input": 50, "output": 5, "cache": {"read": 0}}, "cost": 0.02},
        ], {})

    usage = api.usage("ses_switch")

    assert usage["tokens_in"] == 150 and round(usage["cost"], 4) == 0.03
    assert usage["models"]["ollama-cloud/deepseek"]["tokens_in"] == 100
    assert usage["models"]["opencode/kimi"]["cost"] == 0.02


def test_spend_is_attributed_per_model_after_a_switch():
    from perun import registry

    project = registry.add_project("usage-switch", r"C:\usage-switch")
    session = registry.create_session(project["id"], "ses_switch")
    registry.create_run("switch_run", project["id"], "hi", session_pk=session["id"],
                        model="ollama-cloud/deepseek")
    registry.update_session_usage(session["id"], {
        "tokens_in": 150, "tokens_out": 15, "cost": 0.03,
        "models": {"ollama-cloud/deepseek": {"tokens_in": 100, "tokens_out": 10, "cost": 0.01},
                   "opencode/kimi": {"tokens_in": 50, "tokens_out": 5, "cost": 0.02}},
    })

    rows = {r["provider"]: r for r in registry.usage_by_provider()}
    assert rows["ollama-cloud"]["tokens"] == 110
    assert rows["opencode"]["tokens"] == 55
    assert round(rows["opencode"]["cost"], 4) == 0.02


def test_an_oversized_model_breakdown_is_stored_empty_not_truncated():
    """A cut mid-JSON would make the snapshot unparseable; ``{}`` is explicit."""
    from perun import registry

    project = registry.add_project("usage-oversize", r"C:\usage-oversize")
    session = registry.create_session(project["id"], "ses_oversize")
    registry.create_run("oversize_run", project["id"], "hi", session_pk=session["id"],
                        model="ollama-cloud/deepseek")

    huge = {f"ollama-cloud/model-{i}": {"tokens_in": 1, "tokens_out": 0, "cost": 0.0}
            for i in range(2000)}
    registry.update_session_usage(session["id"], {
        "tokens_in": 2000, "tokens_out": 0, "cost": 0.5, "models": huge,
    })

    with registry.db.db() as conn:
        stored = conn.execute("SELECT usage_models, tokens_in FROM sessions WHERE id=?",
                              (session["id"],)).fetchone()
    assert stored["usage_models"] == "{}"          # parseable, not a truncated blob
    assert stored["tokens_in"] == 2000             # the totals are still kept

    rows = {r["provider"]: r for r in registry.usage_by_provider()}
    assert rows["ollama-cloud"]["tokens"] == 2000  # fallback to the run model
    assert round(float(rows["ollama-cloud"]["cost"]), 4) == 0.5


def test_provider_usage_passes_the_published_quota_through_untouched(monkeypatch):
    """A provider-published budget is shown as-is; measured spend stays measured."""
    from perun import server

    limits = {"budgets": [{"id": "ollama-cloud", "vendors": ["ollama-cloud"],
                           "spent": 18.0, "limit": 20.0,
                           "resetsAt": "2026-12-01T00:00:00Z"}]}

    class FakeAPI:
        def usage_limits(self):
            return limits

    project = registry.add_project("usage-quota", r"C:\usage-quota")
    session = registry.create_session(project["id"], "ses_quota")
    registry.create_run("quota_run", project["id"], "hi", session_pk=session["id"],
                        model="ollama-cloud/deepseek")
    registry.update_session_usage(session["id"], {
        "tokens_in": 100, "tokens_out": 10, "cost": 5.0,
        "models": {"ollama-cloud/deepseek": {"tokens_in": 100, "tokens_out": 10,
                                             "cost": 5.0}}})
    monkeypatch.setattr(opencode_api, "default_api", lambda: FakeAPI())

    body = server.api_providers_usage()

    assert body["limits"] == limits
    row = next(r for r in body["measured"] if r["provider"] == "ollama-cloud")
    assert "budget_percent" not in row        # never dressed up as a quota
    assert round(float(row["cost"]), 4) == 5.0


def test_provider_budgets_turn_measured_spend_into_a_percentage(monkeypatch):
    """No provider quota: a configured monthly budget gives the percentage."""
    from perun import server

    project = registry.add_project("usage-budget", r"C:\usage-budget")
    session = registry.create_session(project["id"], "ses_budget")
    registry.create_run("budget_run", project["id"], "hi", session_pk=session["id"],
                        model="ollama-cloud/deepseek")
    registry.update_session_usage(session["id"], {"tokens_in": 100, "tokens_out": 10,
                                                  "cost": 5.0,
                                                  "models": {"ollama-cloud/deepseek":
                                                             {"tokens_in": 100, "tokens_out": 10,
                                                              "cost": 5.0}}})
    monkeypatch.setattr(server.SETTINGS, "provider_budgets", "ollama-cloud=20")

    body = server.api_providers_usage()

    row = next(r for r in body["measured"] if r["provider"] == "ollama-cloud")
    assert row["budget"] == 20.0
    assert row["budget_percent"] == 0.25
