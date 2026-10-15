"""Agent discovery tests."""

from __future__ import annotations

from pathlib import Path

from perun import agents, config


def test_builtins_returned():
    ids = {a["id"] for a in agents.list_agents()}
    assert {"build", "plan", "general", "explore"} <= ids


def test_global_custom_agent_discovered(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "reviewer.md").write_text(
        "---\nname: Reviewer\ndescription: Code reviewer\nmode: subagent\n---\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", tmp_path)
    found = agents.list_agents()
    reviewer = next((a for a in found if a["id"] == "reviewer"), None)
    assert reviewer is not None
    assert reviewer["name"] == "Reviewer"
    assert reviewer["mode"] == "subagent"
    assert reviewer["description"] == "Code reviewer"


def test_project_local_agent_discovered(tmp_path):
    project_agents = tmp_path / ".opencode" / "agents"
    project_agents.mkdir(parents=True)
    (project_agents / "team-reviewer.md").write_text(
        "---\nname: Team Reviewer\ndescription: Team-specific reviewer\n---\n", encoding="utf-8"
    )
    found = agents.list_agents(tmp_path)
    local = next((a for a in found if a["id"] == "team-reviewer"), None)
    assert local is not None
    assert local["name"] == "Team Reviewer"
