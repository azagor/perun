"""Surface policy: default-deny, and the four explicit surfaces."""

from __future__ import annotations

import pytest

from perun import agent_policy

ROWS = [
    {"id": "build"}, {"id": "plan"},
    {"id": "perun-orchestrator"}, {"id": "perun-docs"}, {"id": "perunchat"},
    {"id": "code-reviewer"},
]


@pytest.fixture(autouse=True)
def placed(monkeypatch):
    monkeypatch.setattr(agent_policy, "assigned_ids", lambda slug="": {"code-reviewer"})


def ids(surface="", project=""):
    return {row["id"] for row in agent_policy.filter_agents(ROWS, surface, project)}


def test_default_surface_is_the_project_rules():
    assert ids() == {"build", "plan", "code-reviewer"}


def test_an_unknown_surface_fails_closed():
    assert ids("nonsense") == {"build", "plan", "code-reviewer"}


def test_control_offers_only_the_orchestrator_plus_builtins():
    assert ids("control") == {"build", "plan", "perun-orchestrator"}


def test_perunchat_offers_only_the_room_persona():
    assert ids("perunchat") == {"perunchat"}


def test_docs_offers_only_the_docs_agent():
    assert ids("docs") == {"perun-docs"}


def test_all_is_explicit_and_unfiltered():
    assert ids("all") == {row["id"] for row in ROWS}
