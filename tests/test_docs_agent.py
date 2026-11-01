"""The restricted docs agent: managed file, permissions, and its reset."""

from __future__ import annotations

import json

import pytest

from perun import config, docs_agent


@pytest.fixture()
def agent_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", tmp_path / "opencode")
    monkeypatch.setattr(config, "HOME", tmp_path / "perun-home")
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    return tmp_path / "opencode" / "agents" / f"{docs_agent.DOCS_AGENT_ID}.md"


def test_install_creates_the_file_once_and_leaves_it_alone(agent_home):
    assert docs_agent.ensure() == "created"
    first = agent_home.read_text(encoding="utf-8")
    assert docs_agent.ensure() == "unchanged"
    assert agent_home.read_text(encoding="utf-8") == first
    assert docs_agent.status()["installed"] is True


def test_a_user_edit_is_never_overwritten(agent_home):
    docs_agent.ensure()
    agent_home.write_text(
        agent_home.read_text(encoding="utf-8") + "\n- my rule\n", encoding="utf-8")

    assert docs_agent.ensure() == "customized"
    assert "my rule" in agent_home.read_text(encoding="utf-8")
    assert docs_agent.status()["customized"] is True

    assert docs_agent.reset()["ok"] is True
    assert "my rule" not in agent_home.read_text(encoding="utf-8")


def test_a_corrupt_state_entry_does_not_break_install(agent_home, monkeypatch):
    monkeypatch.setattr(config, "HOME", agent_home.parents[2] / "perun-home")
    state = config.HOME / "agent_templates.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({docs_agent.DOCS_AGENT_ID: {"version": "two"}}),
                     encoding="utf-8")

    assert docs_agent.ensure() in ("created", "updated", "unchanged")
    assert docs_agent.status()["installed"] is True


def test_the_agent_can_only_write_documentation():
    text = docs_agent.DOCS_AGENT_MD
    # Deny by default, then the docs surface only.
    assert 'action: "*"' in text and "effect: deny" in text
    for allowed in ("docs/**", "AGENTS.md", "perun.md"):
        assert f'resource: "{allowed}"' in text
    # No shell/exec allow: the blanket deny is the only thing covering it.
    assert "effect: allow" in text
    for dangerous in ('resource: "bash"', 'resource: "shell"', 'resource: "edit"\n    effect: allow'):
        assert dangerous not in text


def test_the_agent_is_available_only_once_installed(agent_home):
    assert docs_agent.available() is False
    docs_agent.ensure()
    assert docs_agent.available() is True
    assert docs_agent.available("build") is True
