"""Edge-case routing tests for the Telegram bridge's project resolution.

The bridge must work with **zero** projects registered (nothing launched) and
must not confuse "no projects" with "several projects". These tests exercise the
command/plain-message routing with ``send`` stubbed so nothing hits the network.
"""

from __future__ import annotations

import pytest

from perun import config, registry
from perun.orchestrator import Orchestrator
from perun.telegram import PerunTelegramBridge


@pytest.fixture()
def bridge():
    b = PerunTelegramBridge(Orchestrator(config.Settings.load()),
                            token="test-token", allowed=["1"])
    b.sent: list[str] = []
    b.send = lambda chat_id, text, reply_to=None: b.sent.append(text)  # type: ignore[assignment]
    yield b
    b.client.close()


def _add(name: str, path: str) -> dict:
    return registry.add_project(name, path, kind="dir")


# ---- zero projects (nothing launched) ------------------------------------- #

def test_zero_projects_run_message_asks_to_register(bridge):
    bridge.run_message("1", "hello", 1)
    assert "No projects registered yet" in bridge.sent[-1]
    assert "Multiple projects" not in bridge.sent[-1]


def test_zero_projects_status_reports_none(bridge):
    bridge.handle_command("1", "/status", 1)
    assert "(none)" in bridge.sent[-1]


def test_zero_projects_listing(bridge):
    bridge.handle_command("1", "/projects", 1)
    assert "No projects registered yet" in bridge.sent[-1]


def test_zero_projects_index_requires_a_project(bridge):
    bridge.handle_command("1", "/index", 1)
    assert "Select a project first" in bridge.sent[-1]


def test_current_project_is_none_with_zero_projects(bridge):
    assert bridge._current_project("1") is None


# ---- single project (auto-selected) --------------------------------------- #

def test_single_project_is_auto_selected(bridge):
    _add("Solo", r"C:\solo")
    assert bridge._current_project("1")["slug"] == "solo"
    assert bridge._is_current("1", registry.get_project("solo")) is True


# ---- several projects (selection required) -------------------------------- #

def test_multiple_projects_require_selection(bridge):
    _add("A", r"C:\a")
    _add("B", r"C:\b")
    assert bridge._current_project("1") is None
    bridge.run_message("1", "hi", 1)
    assert "Multiple projects registered" in bridge.sent[-1]


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


def test_remember_without_fact_shows_usage(bridge):
    bridge.handle_command("1", "/remember", 1)
    assert "Usage: `/remember <fact>`" in bridge.sent[-1]


def test_recall_without_query_shows_usage(bridge):
    bridge.handle_command("1", "/recall", 1)
    assert "Usage: `/recall <query>`" in bridge.sent[-1]


def test_unknown_command_is_reported(bridge):
    bridge.handle_command("1", "/bogus", 1)
    assert "Unknown command" in bridge.sent[-1]
