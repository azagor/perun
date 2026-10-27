"""Regression tests for MCP filesystem helper tools.

Loading mcp/perun_mcp.py directly because an installed `mcp` package shadows
the repo's `mcp/` directory under the normal import machinery.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from perun import registry

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_PATH = REPO_ROOT / "mcp" / "perun_mcp.py"

spec = importlib.util.spec_from_file_location("perun_mcp_local", MCP_PATH)
assert spec and spec.loader
mcp_mod = importlib.util.module_from_spec(spec)
sys.modules["perun_mcp_local"] = mcp_mod
spec.loader.exec_module(mcp_mod)


@pytest.fixture
def fs_project(tmp_path, monkeypatch):
    """Register a temp project so its files are inside the allowed read scope."""
    p = registry.add_project("mcpfs", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return p


def test_file_exists_reports_existing_and_missing(fs_project, tmp_path):
    existing = tmp_path / "hello.txt"
    existing.write_text("world", encoding="utf-8")
    missing = tmp_path / "missing.txt"
    result = mcp_mod.perun_file_exists([str(existing), str(missing)])
    assert "hello.txt: exists (file)" in result
    assert "missing.txt" in result
    assert "path does not exist" in result


def test_file_exists_rejects_outside_scope(tmp_path):
    """A path under tmp_path that is NOT a registered project is outside scope."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    result = mcp_mod.perun_file_exists([str(outside)])
    assert "outside the allowed read scope" in result


def test_list_directory_shows_entries(fs_project, tmp_path):
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b").mkdir()
    result = mcp_mod.perun_list_directory(str(tmp_path))
    assert "[d] b" in result
    assert "[f] a.py" in result


def test_list_directory_rejects_file(fs_project, tmp_path):
    f = tmp_path / "not_dir.txt"
    f.write_text("x", encoding="utf-8")
    result = mcp_mod.perun_list_directory(str(f))
    assert "not a directory" in result


def test_read_file_with_line_numbers(fs_project, tmp_path):
    f = tmp_path / "lines.txt"
    f.write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = mcp_mod.perun_read_file(str(f), offset=2, limit=2)
    assert "lines 2-3 of 3" in result
    assert "2: two" in result
    assert "3: three" in result
    assert "1: one" not in result


def test_read_file_offset_out_of_range(fs_project, tmp_path):
    f = tmp_path / "short.txt"
    f.write_text("one\ntwo\n", encoding="utf-8")
    result = mcp_mod.perun_read_file(str(f), offset=10, limit=5)
    assert "offset 10 is out of range" in result


def test_read_file_rejects_directory(fs_project, tmp_path):
    result = mcp_mod.perun_read_file(str(tmp_path), offset=1, limit=10)
    assert "not a file" in result


def test_read_file_rejects_outside_scope(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    result = mcp_mod.perun_read_file(str(outside), offset=1, limit=10)
    assert "outside the allowed read scope" in result


def test_read_file_limits_clamped(fs_project, tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("\n".join(str(i) for i in range(5)), encoding="utf-8")
    result = mcp_mod.perun_read_file(str(f), offset=1, limit=5000)
    assert "limit must be at most 2000" in result


def test_read_file_can_read_repo_file():
    """The Perun repo root is in the allowed scope, so README.md is readable."""
    readme = REPO_ROOT / "README.md"
    result = mcp_mod.perun_read_file(str(readme), offset=1, limit=5)
    assert "Perun" in result
    assert "1:" in result
