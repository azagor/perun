"""The control-plane agent: managed file, resolution, and its safety contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from perun import config, control, orchestrator, orchestrator_agent


@pytest.fixture()
def agent_home(tmp_path, monkeypatch):
    """Point the agent + state files at a temp home so nothing leaks."""
    cfg_dir = tmp_path / "opencode"
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "HOME", tmp_path / "perun-home")
    return cfg_dir / "agents" / f"{orchestrator_agent.ORCHESTRATOR_AGENT_ID}.md"


def test_ensure_creates_once_then_leaves_it_alone(agent_home):
    assert orchestrator_agent.ensure() == "created"
    assert agent_home.is_file()
    first = agent_home.read_text(encoding="utf-8")
    assert orchestrator_agent.ensure() == "unchanged"
    assert agent_home.read_text(encoding="utf-8") == first
    assert orchestrator_agent.status()["installed"] is True
    assert orchestrator_agent.status()["customized"] is False


def test_a_user_edit_is_never_overwritten(agent_home):
    orchestrator_agent.ensure()
    agent_home.write_text(
        agent_home.read_text(encoding="utf-8") + "\n- my rule\n", encoding="utf-8"
    )

    assert orchestrator_agent.ensure() == "customized"
    assert "my rule" in agent_home.read_text(encoding="utf-8")
    assert orchestrator_agent.status()["customized"] is True

    # An explicit reset is the only way back to the template.
    result = orchestrator_agent.reset()
    assert result["ok"] is True
    assert "my rule" not in agent_home.read_text(encoding="utf-8")


def test_a_foreign_file_is_treated_as_the_users(agent_home):
    agent_home.parent.mkdir(parents=True, exist_ok=True)
    agent_home.write_text("---\nmode: primary\n---\nhand written\n", encoding="utf-8")

    assert orchestrator_agent.ensure() == "customized"
    assert "hand written" in agent_home.read_text(encoding="utf-8")


def test_a_new_template_version_refreshes_an_untouched_file(agent_home, monkeypatch):
    orchestrator_agent.ensure()
    monkeypatch.setattr(orchestrator_agent, "ORCHESTRATOR_AGENT_MD",
                        orchestrator_agent.ORCHESTRATOR_AGENT_MD + "\n- newer rule\n")
    monkeypatch.setattr(orchestrator_agent, "AGENT_TEMPLATE_VERSION",
                        orchestrator_agent.AGENT_TEMPLATE_VERSION + 1)

    assert orchestrator_agent.ensure() == "updated"
    assert "newer rule" in agent_home.read_text(encoding="utf-8")


def test_a_customized_file_survives_a_template_bump(agent_home, monkeypatch):
    orchestrator_agent.ensure()
    agent_home.write_text("mine\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator_agent, "ORCHESTRATOR_AGENT_MD", "theirs\n")
    monkeypatch.setattr(orchestrator_agent, "AGENT_TEMPLATE_VERSION", 99)

    assert orchestrator_agent.ensure() == "customized"
    assert agent_home.read_text(encoding="utf-8") == "mine\n"


def test_available_knows_the_file(agent_home):
    assert orchestrator_agent.available("") is True          # OpenCode default
    assert orchestrator_agent.available("build") is True      # built-in
    assert orchestrator_agent.available(orchestrator_agent.ORCHESTRATOR_AGENT_ID) is False
    orchestrator_agent.ensure()
    assert orchestrator_agent.available(orchestrator_agent.ORCHESTRATOR_AGENT_ID) is True


def test_resolve_control_agent_prefers_the_configured_agent(agent_home):
    from types import SimpleNamespace

    orchestrator_agent.ensure()
    assert orchestrator.resolve_control_agent(
        SimpleNamespace(orchestrator_agent="build")) == "build"

    # default_agent is a coding preference; it must not become the overseer.
    assert orchestrator.resolve_control_agent(
        SimpleNamespace(orchestrator_agent="",
                        default_agent="control-agent")) == (
            orchestrator_agent.ORCHESTRATOR_AGENT_ID)

    # Nothing configured: Perun's own agent.
    assert orchestrator.resolve_control_agent(
        SimpleNamespace(orchestrator_agent="", default_agent="")) == (
            orchestrator_agent.ORCHESTRATOR_AGENT_ID)


def test_resolve_control_agent_falls_back_when_it_cannot_be_written(agent_home, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(orchestrator_agent, "ensure", lambda **kw: "failed")
    monkeypatch.setattr(orchestrator_agent, "available", lambda agent_id=None: False)
    assert orchestrator.resolve_control_agent(SimpleNamespace(orchestrator_agent="")) == ""


def test_compose_prompt_does_not_repeat_the_persona_for_our_agent(tmp_path):
    control_project = {"path": str(config.REPO_ROOT)}
    assert control.is_control(control_project)

    with_agent = orchestrator.compose_prompt(
        control_project, "status?", new_session=True,
        agent=orchestrator_agent.ORCHESTRATOR_AGENT_ID)
    without = orchestrator.compose_prompt(
        control_project, "status?", new_session=True, agent="build")

    persona = control.ORCHESTRATOR_INSTRUCTIONS.strip()
    assert persona not in with_agent          # the agent body carries it
    assert persona in without                # any other agent still gets it


def test_the_charter_keeps_its_safety_lines(agent_home):
    """The body replaces OpenCode's system prompt, so the guardrails live here."""
    text = orchestrator_agent.ORCHESTRATOR_AGENT_MD
    for phrase in ("consent", "never consent", "perun_delete_project",
                   "never invent", "credentials", "perun_start_session",
                   "perun_list_projects"):
        assert phrase in text, phrase
    # Full access is deliberate: no permission rules in the template.
    assert "permissions:" not in text
