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
    b.send = lambda chat_id, text, reply_to=None: b.sent.append(text)  # type: ignore[assignment]
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


def test_zero_projects_listing_includes_orchestrator(bridge):
    bridge.handle_command("1", "/projects", 1)
    assert "perun" in bridge.sent[-1]
    assert "orchestrator" in bridge.sent[-1]


def test_free_form_message_routes_to_orchestrator(bridge, orch):
    bridge.run_message("1", "hello", 1)
    assert orch.started and orch.started[0]["slug"] == "perun"
    assert bridge.sent[-1] == "reply::perun"


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
