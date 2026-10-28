"""Regression tests for MCP memory tools.

Loading mcp/perun_mcp.py directly because an installed `mcp` package shadows
the repo's `mcp/` directory under the normal import machinery.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from perun import control, memory, registry

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_PATH = REPO_ROOT / "mcp" / "perun_mcp.py"

spec = importlib.util.spec_from_file_location("perun_mcp_local", MCP_PATH)
assert spec and spec.loader
mcp_mod = importlib.util.module_from_spec(spec)
sys.modules["perun_mcp_local"] = mcp_mod
spec.loader.exec_module(mcp_mod)


@pytest.fixture
def mcp_project(tmp_path, monkeypatch):
    """Register a temp project so _resolve_project can find it."""
    p = registry.add_project("mcpmem", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return p


def test_memory_store_defaults_to_project_when_cwd_matches(mcp_project):
    result = mcp_mod.perun_memory_store("a project fact")
    assert "project scope" in result
    stats = memory.stats(mcp_project["id"])
    assert stats["facts_project"] == 1


def test_memory_store_with_global_scope_is_global(mcp_project):
    result = mcp_mod.perun_memory_store("a global fact", global_scope=True)
    assert "global scope" in result


def test_memory_store_without_project_or_cwd_is_global(monkeypatch, tmp_path):
    """Orchestrator-style calls with no project context are global by design."""
    # A directory that is no project and has no project ancestor, on any OS
    # (``C:\\`` is not a thing on POSIX).
    outside = tmp_path / "not-a-project"
    outside.mkdir()
    monkeypatch.chdir(outside)
    before = memory.stats(None)["facts_global"]
    result = mcp_mod.perun_memory_store("cross-project convention")
    assert "global scope" in result
    after = memory.stats(None)["facts_global"]
    assert after == before + 1


def test_perunchat_room_memory_returns_formatted_results():
    room = "priv:mcp"
    memory.remember(
        "alice: project update",
        project_id=-1, scope="room", topic="perunchat", source="perunchat",
        meta={"room": room, "sender": "alice", "type": "transcript"},
        room_id=room,
    )
    result = mcp_mod.perunchat_room_memory("project update", room, k=5)
    assert "alice" in result
    assert "project update" in result
    assert "transcript" in result


def test_perunchat_room_memory_no_match():
    result = mcp_mod.perunchat_room_memory("nothing", "priv:empty", k=5)
    assert "No matching room memory" in result


def test_perunchat_room_memory_rejects_invalid_k():
    result = mcp_mod.perunchat_room_memory("q", "priv:test", k=0)
    assert "k must be at least 1" in result
    result = mcp_mod.perunchat_room_memory("q", "priv:test", k=9)
    assert "k must be at most 8" in result


def test_perunchat_room_memory_requires_room():
    result = mcp_mod.perunchat_room_memory("q", "", k=5)
    assert "room id is required" in result


def test_perunchat_room_memory_project_room_resolves_project_id(tmp_path):
    project = registry.add_project("mcp-room-proj", str(tmp_path))
    room = "grp:perun:mcp-room-proj:chat"
    registry.set_project_chat(project["id"], room, "perun", "active", 1)
    memory.remember(
        "bob: project update",
        project_id=project["id"], scope="project", topic="perunchat", source="perunchat",
        meta={"room": room, "sender": "bob", "type": "transcript"},
        room_id=room,
    )
    # The tool resolves the project id from the room binding.
    result = mcp_mod.perunchat_room_memory("update", room, k=5)
    assert "bob" in result
    assert "project update" in result


def test_perunchat_room_memory_public_room_is_empty():
    """Public rooms intentionally store no long-term transcript memory."""
    result = mcp_mod.perunchat_room_memory("hello", "pub:global:test", k=5)
    assert "No matching room memory" in result


def test_memory_store_accepts_kind_and_valid_to(mcp_project):
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    result = mcp_mod.perun_memory_store("temporary rule", kind="rule", valid_to=future)
    assert "Stored" in result


def test_memory_forget_and_supersede(mcp_project):
    result = mcp_mod.perun_memory_store("original fact", kind="rule")
    assert "Stored" in result
    fact_id = int(result.split("id=")[1].split(")")[0])
    # Forget it.
    assert "Forgot" in mcp_mod.perun_memory_forget(fact_id)
    assert "No matching memory" in mcp_mod.perun_memory_search("original fact", k=5)


def test_perunchat_room_memory_falls_back_to_meta_room():
    """Pre-v7 rows with room in meta but no room_id column remain searchable."""
    room = "priv:legacy"
    # Insert a row with empty room_id but room in meta by bypassing remember().
    from perun import db
    with db.db() as conn:
        conn.execute(
            "INSERT INTO memory (project_id, scope, topic, content, content_hash, ts, meta, room_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (-1, "room", "perunchat", "alice: legacy fact",
             memory.content_hash("alice: legacy fact"), registry.now_iso(),
             '{"room": "priv:legacy", "sender": "alice", "type": "transcript"}', "")
        )
    result = mcp_mod.perunchat_room_memory("legacy fact", room, k=5)
    assert "alice" in result
    assert "legacy fact" in result
