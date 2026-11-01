"""Routing tests for the Telegram bridge's project resolution.

The bridge must work with **zero** projects registered (nothing launched): with
no explicit selection it talks to the *orchestrator* (the control plane), which
always exists. A lone user project is still auto-selected for convenience, and
several projects fall back to the orchestrator. ``send`` is stubbed and the
orchestrator is faked so nothing hits the network or spawns a real process.
"""

from __future__ import annotations

import pytest

from perun import control, registry
from perun.telegram import PerunTelegramBridge


class FakeOrch:
    """Minimal stand-in for :class:`perun.orchestrator.Orchestrator`."""

    def __init__(self) -> None:
        self.started: list[dict] = []

    def start_run(self, slug, prompt, **kw):  # noqa: ANN001, ANN003
        uid = f"run{len(self.started) + 1}"
        self.started.append({"slug": slug, "prompt": prompt, **kw})
        proj = registry.get_project(slug)
        registry.create_run(
            uid, proj["id"], prompt,
            session_pk=kw.get("session_pk"),
            opencode_session_id=kw.get("opencode_session_id", ""),
            model=kw.get("model", ""), agent=kw.get("agent", ""),
        )
        registry.update_run(uid, status="done", output=f"reply::{slug}")
        return registry.get_run(uid)

    def is_final(self, uid: str) -> bool:  # noqa: ARG002
        return True

    def cancel(self, uid: str) -> bool:  # noqa: ARG002
        return False


@pytest.fixture()
def orch():
    return FakeOrch()


@pytest.fixture()
def bridge(control_repo, orch):  # noqa: ARG001 - control_repo patches config
    b = PerunTelegramBridge(orch, token="test-token", allowed=["1"])
    b.sent: list[str] = []
    b.markups: list[dict | None] = []

    def _send(chat_id, text, reply_to=None, reply_markup=None):  # noqa: ANN001
        b.sent.append(text)
        b.markups.append(reply_markup)

    b.send = _send  # type: ignore[assignment]
    b.api_calls: list[tuple[str, dict]] = []

    def _api(method, **kw):  # noqa: ANN003
        b.api_calls.append((method, kw))
        return {"ok": True, "result": {}}

    b.api = _api  # type: ignore[assignment]
    yield b
    b.client.close()


def _add(name: str, path: str) -> dict:
    return registry.add_project(name, path, kind="dir")


# ---- zero user projects (nothing launched) -------------------------------- #

def test_zero_projects_defaults_to_orchestrator(bridge):
    cur = bridge._current_project("1")
    assert cur is not None and control.is_control(cur)
    assert "No projects" not in bridge.sent


def test_zero_projects_status_reports_orchestrator(bridge):
    bridge.handle_command("1", "/status", 1)
    assert "perun" in bridge.sent[-1]
    assert "(none)" not in bridge.sent[-1]


def test_zero_projects_listing_includes_orchestrator_for_allowed_chat(bridge):
    bridge.handle_command("1", "/projects", 1)
    assert "perun" in bridge.sent[-1]
    assert "orchestrator" in bridge.sent[-1]


def test_zero_projects_listing_hides_orchestrator_for_unlisted_chat(bridge):
    """Runtime-paired / unknown chats must not see the hidden Perun project."""
    bridge.handle_command("99", "/projects", 1)
    assert "perun" not in bridge.sent[-1]
    assert "orchestrator" not in bridge.sent[-1]


    _add("Alpha", r"C:\alpha")
    _add("Beta", r"C:\beta")
    bridge.handle_command("1", "/projects", 1)
    markup = bridge.markups[-1]
    assert markup is not None
    assert "inline_keyboard" in markup
    labels = [btn["text"] for row in markup["inline_keyboard"] for btn in row]
    assert any("Alpha" in label for label in labels)
    assert any("Beta" in label for label in labels)
    callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert "project:alpha" in callbacks
    assert "project:beta" in callbacks
    # Slugs that would exceed Telegram's 64-byte callback_data limit are
    # encoded by id.  Base slugs are capped at 54 chars; the "-10" suffix
    # (57-char slug) pushes "project:<slug>" to 65 bytes, which triggers it.
    last = None
    for i in range(10):
        last = _add("a" * 80, rf"C:\long-{i}")
    bridge.handle_command("1", "/projects", 1)
    callbacks = [btn["callback_data"] for row in bridge.markups[-1]["inline_keyboard"] for btn in row]
    assert any(c == f"project:id:{last['id']}" for c in callbacks)


def test_projects_keyboard_marks_current_project(bridge):
    _add("Alpha", r"C:\alpha")
    _add("Beta", r"C:\beta")
    bridge.handle_command("1", "/project beta", 1)
    bridge.handle_command("1", "/projects", 1)
    labels = [btn["text"] for row in bridge.markups[-1]["inline_keyboard"] for btn in row]
    beta_label = next(label for label in labels if "Beta" in label)
    assert beta_label.startswith("▸")


def test_callback_query_selects_project(bridge):
    _add("Gamma", r"C:\gamma")
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "q1",
            "data": "project:gamma",
            "message": {"message_id": 5, "chat": {"id": "1"}},
        },
    }
    bridge._handle_update(update)
    assert bridge._current_project("1")["slug"] == "gamma"
    assert any(call == ("answerCallbackQuery", {"callback_query_id": "q1", "text": "Now talking to Gamma"}) for call in bridge.api_calls)
    assert "Now talking to *Gamma*" in bridge.sent[-1]


def test_callback_query_selects_project_by_id_for_long_slugs(bridge):
    long_slug = "a" * 80
    proj = _add(long_slug, rf"C:\{long_slug}")
    update = {
        "update_id": 2,
        "callback_query": {
            "id": "q2",
            "data": f"project:id:{proj['id']}",
            "message": {"message_id": 6, "chat": {"id": "1"}},
        },
    }
    bridge._handle_update(update)
    # The slug is bounded (see registry.add_project), but the callback must
    # still resolve the intended project by its id.
    assert bridge._current_project("1")["id"] == proj["id"]
    assert bridge._current_project("1")["slug"] == proj["slug"]


def test_callback_query_rejects_unauthorised_chat(bridge):
    _add("Gamma", r"C:\gamma")
    update = {
        "update_id": 3,
        "callback_query": {
            "id": "q3",
            "data": "project:gamma",
            "message": {"message_id": 7, "chat": {"id": "999"}},
        },
    }
    bridge._handle_update(update)
    assert any(
        call == ("answerCallbackQuery", {"callback_query_id": "q3", "text": "Not authorised."})
        for call in bridge.api_calls
    )


def test_free_form_message_routes_to_orchestrator(bridge, orch):
    bridge.run_message("1", "hello", 1)
    assert orch.started and orch.started[0]["slug"] == "perun"
    assert bridge.sent[-1] == "reply::perun"


def test_handle_update_deduplicates_messages(bridge):
    update = {
        "update_id": 10,
        "message": {
            "message_id": 42,
            "chat": {"id": "1"},
            "text": "duplicate me",
        },
    }
    bridge._handle_update(update)
    bridge._handle_update(update)
    # The second identical message should be ignored before reaching the queue.
    assert bridge.work.qsize() == 1


def test_orchestrator_command_selects_control(bridge):
    bridge.handle_command("1", "/orchestrator", 1)
    link = registry.get_bridge_link("1")
    assert link["project_id"] == control.ensure()["id"]
    assert "Perun" in bridge.sent[-1] or "orchestrator" in bridge.sent[-1].lower()


# ---- single user project (auto-selected, unchanged) ----------------------- #

def test_single_project_is_auto_selected(bridge):
    _add("Solo", r"C:\solo")
    assert bridge._current_project("1")["slug"] == "solo"
    assert bridge._is_current("1", registry.get_project("solo")) is True


# ---- several user projects (fall back to the orchestrator) ---------------- #

def test_multiple_projects_fall_back_to_orchestrator(bridge, orch):
    _add("A", r"C:\a")
    _add("B", r"C:\b")
    assert control.is_control(bridge._current_project("1"))
    bridge.run_message("1", "hi", 1)
    assert orch.started[0]["slug"] == "perun"


def test_project_command_selects_by_number(bridge):
    _add("A", r"C:\a")
    _add("B", r"C:\b")
    bridge.handle_command("1", "/project 2", 1)
    assert bridge._current_project("1")["slug"] == "b"


def test_project_command_unknown_is_reported(bridge):
    _add("A", r"C:\a")
    bridge.handle_command("1", "/project nope", 1)
    assert "No project matches" in bridge.sent[-1]


def test_new_command_clears_session(bridge):
    proj = _add("A", r"C:\a")
    registry.set_bridge_link("1", proj["id"], 42)
    bridge.handle_command("1", "/new", 1)
    assert registry.get_bridge_link("1")["session_pk"] is None


# ---- deterministic command guards ----------------------------------------- #

def test_help_lists_commands(bridge):
    bridge.handle_command("1", "/help", 1)
    assert "/projects" in bridge.sent[-1] and "/recall" in bridge.sent[-1]
    assert "/orchestrator" in bridge.sent[-1]


def test_remember_without_fact_shows_usage(bridge):
    bridge.handle_command("1", "/remember", 1)
    assert "Usage: `/remember <fact>`" in bridge.sent[-1]


def test_recall_without_query_shows_usage(bridge):
    bridge.handle_command("1", "/recall", 1)
    assert "Usage: `/recall <query>`" in bridge.sent[-1]


def test_unknown_command_is_reported(bridge):
    bridge.handle_command("1", "/bogus", 1)
    assert "Unknown command" in bridge.sent[-1]

def test_a_pending_question_is_answered_without_an_agent_run(bridge, monkeypatch):
    from types import SimpleNamespace

    from perun import config

    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: SimpleNamespace(routing_enabled=True,
                                                             routing_shadow=False)))
    seen: list[str] = []
    monkeypatch.setattr(bridge, "_handle_attention_command",
                        lambda chat_id, text, message_id: seen.append(text) or True)

    assert bridge._route_message("1", "co czeka?", 7) is True
    assert seen == ["/pending"]


def test_listing_projects_maps_to_the_existing_command(bridge, monkeypatch):
    from types import SimpleNamespace

    from perun import config

    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: SimpleNamespace(routing_enabled=True,
                                                             routing_shadow=False)))
    seen: list[str] = []
    monkeypatch.setattr(bridge, "_handle_attention_command",
                        lambda chat_id, text, message_id: False)
    monkeypatch.setattr(bridge, "_handle_text_command",
                        lambda chat_id, text, message_id, project_override=None:
                        seen.append(text) or True)

    assert bridge._route_message("1", "list projects", 8) is True
    assert seen == ["/projects"]


def test_real_work_still_reaches_the_agent(bridge, monkeypatch):
    from types import SimpleNamespace

    from perun import config

    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: SimpleNamespace(routing_enabled=True,
                                                             routing_shadow=False)))
    called: list[str] = []
    monkeypatch.setattr(bridge, "_handle_attention_command",
                        lambda *a, **k: called.append("inbox") or False)
    monkeypatch.setattr(bridge, "_handle_text_command",
                        lambda *a, **k: called.append("command") or True)

    assert bridge._route_message("1", "uruchom testy w perunie", 9) is False
    assert called == []

def test_now_reports_nothing_when_idle(bridge) -> None:
    bridge.handle_command("1", "/now", 1)
    assert bridge.sent and "Nothing is running" in bridge.sent[-1]


def test_health_reports_the_bridge_and_services(bridge, monkeypatch) -> None:
    from perun import opencode_api

    class FakeAPI:
        def info(self):
            return {"version": "2.2.0"}

    monkeypatch.setattr(opencode_api, "default_api", lambda: FakeAPI())
    bridge.handle_command("1", "/health", 1)

    report = bridge.sent[-1]
    assert "Health" in report
    assert "opencode service" in report and "2.2.0" in report
    assert "bridge" in report


def test_health_survives_an_unreachable_service(bridge, monkeypatch) -> None:
    from perun import opencode_api

    class DeadAPI:
        def info(self):
            raise opencode_api.OpenCodeUnavailable("no service")

    monkeypatch.setattr(opencode_api, "default_api", lambda: DeadAPI())
    bridge.handle_command("1", "/health", 1)

    report = bridge.sent[-1]
    assert "opencode service" in report
    assert "bridge" in report          # the rest of the report still renders


def test_a_whats_running_question_maps_to_the_command(bridge, monkeypatch) -> None:
    from types import SimpleNamespace

    from perun import config

    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: SimpleNamespace(routing_enabled=True,
                                                             routing_shadow=False)))
    seen: list[str] = []
    monkeypatch.setattr(bridge, "_handle_attention_command",
                        lambda *a, **k: False)
    monkeypatch.setattr(bridge, "_handle_text_command",
                        lambda chat_id, text, message_id, project_override=None:
                        seen.append(text) or True)

    assert bridge._route_message("1", "co się dzieje?", 11) is True
    assert seen == ["/now"]

def test_usage_reports_nothing_before_any_run(bridge) -> None:
    bridge.handle_command("1", "/usage", 1)
    assert bridge.sent and "No usage recorded" in bridge.sent[-1]


def test_usage_lists_totals_and_projects(bridge) -> None:
    from perun import registry

    project = registry.add_project("usage-demo", r"C:\usage-demo")
    session = registry.create_session(project["id"], "ses_usage")
    registry.update_session_usage(session["id"], {
        "tokens_in": 1200, "tokens_out": 300, "cache_read": 900, "cost": 0.0042,
    })

    bridge.handle_command("1", "/usage", 1)

    report = bridge.sent[-1]
    assert "Usage — last 24h" in report
    assert "1,200 in" in report and "300 out" in report
    assert "$0.0042" in report
    assert "usage-demo" in report


def test_a_cost_question_maps_to_the_usage_command(bridge, monkeypatch) -> None:
    from types import SimpleNamespace

    from perun import config

    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: SimpleNamespace(routing_enabled=True,
                                                             routing_shadow=False)))
    seen: list[str] = []
    monkeypatch.setattr(bridge, "_handle_attention_command", lambda *a, **k: False)
    monkeypatch.setattr(bridge, "_handle_text_command",
                        lambda chat_id, text, message_id, project_override=None:
                        seen.append(text) or True)

    assert bridge._route_message("1", "ile kosztowało?", 12) is True
    assert seen == ["/usage"]

def test_resume_lists_recent_runs(bridge) -> None:
    from perun import registry

    project = registry.add_project("resume-demo", r"C:\resume-demo")
    registry.set_bridge_link("1", project["id"], None)
    run = registry.create_run("resume_run_1", project["id"], "fix the widget")
    registry.update_run("resume_run_1", status="done")

    bridge.handle_command("1", "/resume", 1)
    assert "Resume" in bridge.sent[-1] and "fix the widget" in bridge.sent[-1]


def test_resume_binds_the_chat_to_that_session(bridge) -> None:
    from perun import registry

    project = registry.add_project("resume-bind", r"C:\resume-bind")
    registry.set_bridge_link("1", project["id"], None)
    session = registry.create_session(project["id"], "ses_resume")
    run = registry.create_run("resume_run_2", project["id"], "carry on",
                              session_pk=session["id"])
    registry.update_run("resume_run_2", status="done")

    bridge.handle_command("1", "/resume 1", 1)

    assert registry.get_bridge_link("1")["session_pk"] == session["id"]
    assert "Resumed" in bridge.sent[-1]


def test_resume_without_runs_says_so(bridge) -> None:
    from perun import registry

    registry.add_project("resume-empty", r"C:\resume-empty")
    bridge.handle_command("1", "/resume", 1)
    assert "No runs yet" in bridge.sent[-1]


def test_diff_reports_a_non_repository(bridge) -> None:
    from perun import registry

    project = registry.add_project("diff-plain", r"C:\diff-plain")
    registry.set_bridge_link("1", project["id"], None)
    bridge.handle_command("1", "/diff", 1)
    assert "not a git repository" in bridge.sent[-1]


def test_a_changes_question_maps_to_the_diff_command(bridge, monkeypatch) -> None:
    from types import SimpleNamespace

    from perun import config

    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: SimpleNamespace(routing_enabled=True,
                                                             routing_shadow=False)))
    seen: list[str] = []
    monkeypatch.setattr(bridge, "_handle_attention_command", lambda *a, **k: False)
    monkeypatch.setattr(bridge, "_handle_text_command",
                        lambda chat_id, text, message_id, project_override=None:
                        seen.append(text) or True)

    assert bridge._route_message("1", "git diff", 13) is True
    assert seen == ["/diff"]
