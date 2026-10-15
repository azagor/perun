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


def test_memory_store_without_project_or_cwd_is_global(monkeypatch):
    """Orchestrator-style calls with no project context are global by design."""
    monkeypatch.chdir("C:\\")
    before = memory.stats(None)["facts_global"]
    result = mcp_mod.perun_memory_store("cross-project convention")
    assert "global scope" in result
    after = memory.stats(None)["facts_global"]
    assert after == before + 1
