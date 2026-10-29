"""Tests for the perun-proposals MCP server."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from perun import action_proposals, config, manifest, registry
from perun import proposals_mcp as proposals_mcp_module
from perun.proposals_mcp import propose, approve, list_proposals, get_proposal


_TOOL_MAP = {
    "propose": propose,
    "approve": approve,
    "list_proposals": list_proposals,
    "get_proposal": get_proposal,
}


@pytest.fixture
def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PERUN_HOME", str(tmp_path))
    config.CONFIG_PATH = tmp_path / "config.json"
    s = config.Settings.load()
    s.perunchat_api_key = "prn_test_secret"
    s.perunchat_api_endpoint = "ws://127.0.0.1:8765/v1/chat/ws"
    s.perunchat_auto_reply = True
    s.perunchat_auto_join = ["pub:global:test"]
    s.perunchat_trusted = ["frambo", "peruntheoverseer", "perun-local"]
    return s


def _make_project(tmp_path, participants=None, shared=True):
    path = tmp_path / "proj"
    path.mkdir(parents=True)
    project = registry.add_project("proj", str(path))
    data = manifest.from_project(project)
    data["shared"] = shared
    data["participants"] = participants or [{"agent_id": "frambo", "roles": ["rw"]}]
    manifest.save(str(path), data, body="# Proj")
    room_id = "grp:perun:proj:chat"
    registry.set_project_chat(project["id"], room_id, "peruntheoverseer", "active", 2)
    # Ensure opencode.json exists with MCP servers so the guard/proposals servers
    # can start; the actual orchestrator is not exercised here.
    from perun import projects
    projects.sync_opencode_config(project)
    return project, room_id


@pytest.fixture(autouse=True)
def _env_isolation(monkeypatch):
    """Clear proposal-related env vars before each test."""
    for key in ("PERUN_PROJECT_PATH", "PERUN_CHAT_SENDER", "PERUN_ROOM_ID"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def tool_runner(tmp_path, monkeypatch, _settings):
    """Helper that runs an MCP tool call with env set from a project."""
    def run(name: str, project, sender: str, args: dict):
        monkeypatch.setenv("PERUN_PROJECT_PATH", str(Path(project["path"]).resolve()))
        monkeypatch.setenv("PERUN_CHAT_SENDER", sender)
        monkeypatch.setattr(proposals_mcp_module, "_settings", lambda: _settings)
        fn = _TOOL_MAP.get(name)
        if fn is None:
            raise NameError(f"tool {name} not found")
        return fn(**args)
    return run


def test_list_proposals_falls_back_to_cwd_without_env(tmp_path, monkeypatch, _settings):
    """A project-local MCP server launched by OpenCode may not set PERUN_PROJECT_PATH."""
    project_path = tmp_path / "cwdproj"
    project_path.mkdir()
    registry.add_project("cwdproj", str(project_path))
    monkeypatch.chdir(project_path)
    monkeypatch.setattr(proposals_mcp_module, "_settings", lambda: _settings)
    assert list_proposals() == []


def test_project_path_degrades_when_cwd_is_gone(monkeypatch):
    """A deleted cwd must not crash the proposals server."""
    monkeypatch.delenv("PERUN_PROJECT_PATH", raising=False)
    real_getcwd = os.getcwd
    calls = {"n": 0}

    def _missing_cwd_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("cwd deleted")
        return real_getcwd()

    monkeypatch.setattr(proposals_mcp_module.os, "getcwd", _missing_cwd_once)
    assert proposals_mcp_module._project_path() == Path(real_getcwd()).resolve()


def test_propose_creates_pending_proposal(tool_runner, tmp_path, _settings):
    _settings.perunchat_trusted = ["frambo"]
    project, room = _make_project(tmp_path)

    result = tool_runner(
        "propose",
        project,
        "frambo",
        {"intent": "add README", "action_type": "create", "targets": ["README.md"]},
    )

    assert result["status"] == "pending"
    assert result["requester"] == "frambo"
    proposal = action_proposals.get(result["proposal_id"])
    assert proposal["intent"] == "add README"


def test_propose_rejects_untrusted_sender(tool_runner, tmp_path, _settings):
    _settings.perunchat_trusted = ["owner"]
    project, room = _make_project(tmp_path)

    with pytest.raises(ValueError, match="trust list"):
        tool_runner(
            "propose",
            project,
            "frambo",
            {"intent": "add README", "action_type": "create", "targets": ["README.md"]},
        )


def test_propose_owner_bypasses_trust_list(tool_runner, tmp_path, _settings):
    _settings.perunchat_trusted = []
    project, room = _make_project(tmp_path)

    result = tool_runner(
        "propose",
        project,
        "peruntheoverseer",
        {"intent": "add README", "action_type": "create", "targets": ["README.md"]},
    )

    assert result["requester"] == "peruntheoverseer"


def test_approve_peer_proposal_by_owner(tool_runner, tmp_path, _settings, monkeypatch):
    _settings.perunchat_trusted = ["frambo", "peruntheoverseer"]
    project, room = _make_project(tmp_path)

    # Frambo proposes.
    prop = tool_runner(
        "propose",
        project,
        "frambo",
        {"intent": "add README", "action_type": "create", "targets": ["README.md"]},
    )

    # Owner approves; execution is started. Monkey-patch start_execution so the
    # test does not need a real orchestrator.
    monkeypatch.setattr(
        action_proposals, "start_execution", lambda _pid, **kw: action_proposals.get(_pid)
    )
    result = tool_runner(
        "approve",
        project,
        "peruntheoverseer",
        {"proposal_id": prop["proposal_id"], "decision": "approve"},
    )

    assert result["status"] == "approved"
    assert result["approved_by"] == "peruntheoverseer"


def test_approve_rejects_self_approval(tool_runner, tmp_path, _settings):
    _settings.perunchat_trusted = ["frambo"]
    project, room = _make_project(tmp_path)

    prop = tool_runner(
        "propose",
        project,
        "frambo",
        {"intent": "add README", "action_type": "create", "targets": ["README.md"]},
    )

    with pytest.raises(ValueError, match="cannot be approved by its requester"):
        tool_runner(
            "approve",
            project,
            "frambo",
            {"proposal_id": prop["proposal_id"], "decision": "approve"},
        )


def test_reject_proposal(tool_runner, tmp_path, _settings):
    _settings.perunchat_trusted = ["frambo", "peruntheoverseer"]
    project, room = _make_project(tmp_path)

    prop = tool_runner(
        "propose",
        project,
        "frambo",
        {"intent": "add README", "action_type": "create", "targets": ["README.md"]},
    )

    result = tool_runner(
        "approve",
        project,
        "peruntheoverseer",
        {"proposal_id": prop["proposal_id"], "decision": "reject", "reason": "no need"},
    )

    assert result["status"] == "rejected"
    proposal = action_proposals.get(prop["proposal_id"])
    assert proposal["result"] == "no need"


def test_list_and_get_proposals(tool_runner, tmp_path, _settings):
    _settings.perunchat_trusted = ["frambo"]
    project, room = _make_project(tmp_path)

    prop = tool_runner(
        "propose",
        project,
        "frambo",
        {"intent": "add README", "action_type": "create", "targets": ["README.md"]},
    )

    listed = tool_runner("list_proposals", project, "frambo", {"status": "pending"})
    assert any(p["proposal_id"] == prop["proposal_id"] for p in listed)

    fetched = tool_runner(
        "get_proposal", project, "frambo", {"proposal_id": prop["proposal_id"]}
    )
    assert fetched["proposal_id"] == prop["proposal_id"]


def test_propose_auto_approves_when_policy_auto_approve(tool_runner, tmp_path, _settings, monkeypatch):
    """policy_auto_approve must be honored through the MCP tool path."""
    project, room = _make_project(tmp_path)
    monkeypatch.setattr(
        action_proposals.shared_projects, "is_policy_auto_approve", lambda _path: True
    )
    monkeypatch.setattr(
        action_proposals, "start_execution", lambda _pid, **kw: action_proposals.get(_pid)
    )

    result = tool_runner(
        "propose",
        project,
        "frambo",
        {"intent": "add README", "action_type": "create", "targets": ["README.md"]},
    )

    assert result["status"] == "approved"
    proposal = action_proposals.get(result["proposal_id"])
    assert proposal["approved_by"] == action_proposals.AUTO_APPROVER


def test_propose_rejects_unregistered_project(tool_runner, tmp_path, _settings, monkeypatch):
    """The MCP server must not silently register unknown projects."""
    unregistered = tmp_path / "unregistered"
    unregistered.mkdir(parents=True)
    monkeypatch.setenv("PERUN_PROJECT_PATH", str(unregistered.resolve()))
    monkeypatch.setenv("PERUN_CHAT_SENDER", "frambo")
    monkeypatch.setattr(proposals_mcp_module, "_settings", lambda: _settings)

    with pytest.raises(ValueError, match="project is not registered"):
        proposals_mcp_module.propose(
            intent="add README", action_type="create", targets=["README.md"]
        )


def test_proposals_mcp_tool_names_match_permissions():
    """The actual MCP tool names are the bare function names, not prefixed."""
    import asyncio

    tools = asyncio.run(proposals_mcp_module.mcp.list_tools())
    assert {t.name for t in tools} == {"propose", "approve", "list_proposals", "get_proposal"}
