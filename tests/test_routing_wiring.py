"""The deterministic router wired into the Telegram bridge.

Routing is opt-in (``Settings.routing_enabled``), so these tests cover both the
disabled path (behaviour identical to before) and the enabled path (clarify,
status, remember, and switching the bound project before a run).
"""

from __future__ import annotations

import pytest

from perun import config, registry
from perun.telegram import PerunTelegramBridge


class FakeOrch:
    def __init__(self) -> None:
        self.started: list[dict] = []

    def start_run(self, slug, prompt, **kw):  # noqa: ANN001, ANN003
        uid = f"run{len(self.started) + 1}"
        self.started.append({"slug": slug, "prompt": prompt, **kw})
        proj = registry.get_project(slug)
        registry.create_run(
            uid,
            proj["id"],
            prompt,
            session_pk=kw.get("session_pk"),
            opencode_session_id=kw.get("opencode_session_id", ""),
            model=kw.get("model", ""),
            agent=kw.get("agent", ""),
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


@pytest.fixture()
def routing_on(monkeypatch):
    real_load = config.Settings.load

    def load():
        settings = real_load()
        settings.routing_enabled = True
        return settings

    monkeypatch.setattr(config.Settings, "load", staticmethod(load))
    return load


def _add(tmp_path, name: str) -> dict:
    path = tmp_path / name
    path.mkdir()
    return registry.add_project(name, str(path), kind="dir")


# --- disabled: unchanged behaviour ---------------------------------------- #


def test_routing_disabled_starts_a_run_as_before(bridge, orch) -> None:
    bridge.run_message("1", "status", 1)
    assert len(orch.started) == 1
    assert orch.started[0]["prompt"] == "status"


# --- enabled: handled without a run --------------------------------------- #


def test_enabled_status_is_answered_without_a_run(bridge, orch, routing_on) -> None:
    bridge.run_message("1", "status", 1)
    assert orch.started == []
    assert bridge.sent, "a reply is expected"
    assert "status" in bridge.sent[-1].lower() or "perun" in bridge.sent[-1].lower()


def test_enabled_ambiguity_asks_instead_of_running(bridge, orch, routing_on, tmp_path) -> None:
    _add(tmp_path, "perun")
    _add(tmp_path, "perunchat")
    bridge.run_message("1", "porownaj perun i perunchat", 1)
    assert orch.started == []
    assert "Which project" in bridge.sent[-1]
    assert "perun" in bridge.sent[-1] and "perunchat" in bridge.sent[-1]


def test_enabled_remember_does_not_start_a_run(bridge, orch, routing_on) -> None:
    bridge.run_message("1", "zapamietaj ze wole poranne raporty", 1)
    assert orch.started == []
    assert bridge.sent


# --- enabled: routing a run to another project ---------------------------- #


def test_enabled_run_switches_the_bound_project(bridge, orch, routing_on, tmp_path) -> None:
    perun = _add(tmp_path, "perun")
    trading = _add(tmp_path, "trading-bot")
    registry.set_bridge_link("1", trading["id"], None)

    bridge.run_message("1", "uruchom testy w perunie", 1)

    assert len(orch.started) == 1
    assert orch.started[0]["slug"] == perun["slug"]
    assert "→ `perun`" in bridge.sent


def test_enabled_unknown_text_still_runs_in_the_bound_project(
    bridge, orch, routing_on, tmp_path
) -> None:
    trading = _add(tmp_path, "trading-bot")
    registry.set_bridge_link("1", trading["id"], None)

    bridge.run_message("1", "dzieki, do zobaczenia", 1)

    assert len(orch.started) == 1
    assert orch.started[0]["slug"] == "trading-bot"


# --- telemetry ------------------------------------------------------------- #


def test_routing_decision_is_logged_without_the_message_text(
    bridge, orch, routing_on, tmp_path
) -> None:  # noqa: ARG001
    perun = _add(tmp_path, "perun")
    registry.set_bridge_link("1", perun["id"], None)
    bridge.run_message("1", "uruchom testy w perunie", 1)

    rows = [
        row
        for row in registry.list_activity(project_id=perun["id"], limit=20)
        if row["kind"] == "routing_decision"
    ]
    assert rows, "a routing_decision activity row is expected"
    assert "uruchom" not in rows[0]["message"]
    assert "uruchom" not in (rows[0].get("meta") or "")


def test_shadow_mode_logs_but_does_not_act(bridge, orch, routing_on, tmp_path, monkeypatch):
    """Shadow mode must record the decision and still take the old path."""
    perun = _add(tmp_path, "perun")
    registry.set_bridge_link("1", perun["id"], None)

    real_load = config.Settings.load

    def load():
        settings = real_load()
        settings.routing_enabled = True
        settings.routing_shadow = True
        return settings

    monkeypatch.setattr(config.Settings, "load", staticmethod(load))

    bridge.run_message("1", "status", 1)

    # The old path runs the agent instead of printing status.
    assert len(orch.started) == 1
    assert orch.started[0]["prompt"] == "status"
    rows = [
        row
        for row in registry.list_activity(project_id=perun["id"], limit=20)
        if row["kind"] == "routing_decision"
    ]
    assert rows and rows[0]["message"].startswith("shadow:")
    assert rows[0]["meta"]["shadow"] is True


def test_bridge_notifies_a_chat_when_its_run_was_interrupted(bridge, tmp_path):
    """A restart must not leave the operator with silence."""
    from perun import registry

    project = registry.add_project("Interrupt", str(tmp_path / "interrupt"), kind="dir")
    run = registry.create_run(
        "run_interrupted_1", project["id"], "what is perun the trader",
        session_pk=77,
    )
    registry.update_run(
        "run_interrupted_1", status="interrupted",
        ended_at=registry.now_iso(), error="interrupted by a Perun restart",
    )
    registry.set_bridge_link("1", project["id"], 77)

    bridge.sent.clear()
    bridge._notify_interrupted_runs()

    assert any("interrupted by a Perun restart" in text for text in bridge.sent)

    # Second start must not repeat the notice.
    bridge.sent.clear()
    bridge._notify_interrupted_runs()
    assert bridge.sent == []


def test_bridge_does_not_notify_a_chat_that_has_no_matching_session(bridge, tmp_path):
    from perun import registry

    project = registry.add_project("InterruptOther", str(tmp_path / "interruptother"), kind="dir")
    registry.create_run("run_interrupted_2", project["id"], "x", session_pk=88)
    registry.update_run(
        "run_interrupted_2", status="interrupted",
        ended_at=registry.now_iso(), error="interrupted",
    )
    registry.set_bridge_link("1", project["id"], None)

    bridge.sent.clear()
    bridge._notify_interrupted_runs()
    assert bridge.sent == []


def test_enabled_ask_switches_to_the_named_project(bridge, orch, routing_on, tmp_path):
    """A question about another project must be answered in that project, not
    in whichever project the chat happened to be bound to."""
    perun = _add(tmp_path, "perun")
    trading = _add(tmp_path, "trading-bot")
    registry.set_bridge_link("1", perun["id"], None)

    bridge.run_message("1", "what is trading-bot?", 1)

    assert len(orch.started) == 1
    assert orch.started[0]["slug"] == "trading-bot"
    assert "→ `trading-bot`" in bridge.sent
    assert registry.get_bridge_link("1")["project_id"] == trading["id"]


def test_status_targets_the_named_project_without_rebinding(bridge, routing_on, tmp_path):
    perun = _add(tmp_path, "perun")
    _add(tmp_path, "trading-bot")
    registry.set_bridge_link("1", perun["id"], None)

    bridge.run_message("1", "status trading-bot", 1)

    assert "trading-bot" in bridge.sent[-1]
    assert registry.get_bridge_link("1")["project_id"] == perun["id"]


def test_remember_targets_the_named_project(bridge, routing_on, tmp_path, monkeypatch):
    perun = _add(tmp_path, "perun")
    trading = _add(tmp_path, "trading-bot")
    registry.set_bridge_link("1", perun["id"], None)
    captured: dict = {}

    def fake_remember(fact, **kw):  # noqa: ANN001, ANN003
        captured["fact"] = fact
        captured.update(kw)
        return {"ok": True}

    monkeypatch.setattr("perun.telegram.memory.remember", fake_remember)
    bridge.run_message("1", "zapamietaj ze trading-bot ma 12 gield", 1)

    assert captured["project_id"] == trading["id"]
    assert "12 gield" in captured["fact"]


def test_recall_searches_the_named_project(bridge, routing_on, tmp_path, monkeypatch):
    perun = _add(tmp_path, "perun")
    trading = _add(tmp_path, "trading-bot")
    registry.set_bridge_link("1", perun["id"], None)
    captured: dict = {}

    def fake_search(query, project_id=0, **kw):  # noqa: ANN001, ANN003
        captured["project_id"] = project_id
        return []

    monkeypatch.setattr("perun.telegram.memory.unified_search", fake_search)
    bridge.run_message("1", "co wiesz o trading-bocie", 1)

    assert captured["project_id"] == trading["id"]
