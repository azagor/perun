"""Validation tests for MCP tools.

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
def mcp_project(tmp_path, monkeypatch):
    """Register a temp project so _resolve_project can find it."""
    p = registry.add_project("mcpsec", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return p


# --------------------------------------------------------------------------- #
# Instruction filename validation
# --------------------------------------------------------------------------- #
BAD_INSTRUCTION_FILENAMES = [
    ".",
    "..",
    "../evil.md",
    "a/../evil.md",
    "a\\..\\evil.md",
    "sub/file.md",
    "sub\\file.md",
    "no-extension",
    "name.txt",
]


@pytest.mark.parametrize("filename", BAD_INSTRUCTION_FILENAMES)
def test_get_instructions_rejects_bad_filename(mcp_project, filename):
    result = mcp_mod.perun_get_instructions(filename=filename)
    assert result.startswith("[perun]")
    assert "filename" in result.lower() or "safe" in result.lower()


@pytest.mark.parametrize("filename", BAD_INSTRUCTION_FILENAMES)
def test_set_instructions_rejects_bad_filename(mcp_project, filename):
    result = mcp_mod.perun_set_instructions("hello", filename=filename)
    assert result.startswith("[perun]")
    assert "filename" in result.lower() or "safe" in result.lower()


def test_set_instructions_allows_dotdot_stem(mcp_project):
    """A filename whose basename legitimately contains '..' as a substring is OK."""
    result = mcp_mod.perun_set_instructions("hello", filename="name..md")
    assert "Wrote instruction file" in result
    assert (Path(mcp_project["path"]) / ".perun" / "instructions" / "name..md").is_file()


def test_get_instructions_allows_dotdot_stem(mcp_project):
    mcp_mod.perun_set_instructions("hello", filename="name..md")
    result = mcp_mod.perun_get_instructions(filename="name..md")
    assert "Instruction file name..md" in result
    assert "hello" in result


def test_get_instructions_resolves_path_inside_project(mcp_project):
    mcp_mod.perun_set_instructions("hello", filename="default.md")
    result = mcp_mod.perun_get_instructions(filename="default.md")
    assert "Instruction file default.md" in result
    assert "hello" in result


def test_set_instructions_empty_filename_defaults_to_default_md(mcp_project):
    """Empty filename for set defaults to default.md, which is allowed."""
    result = mcp_mod.perun_set_instructions("hello", filename="")
    assert "Wrote instruction file default.md" in result
    assert (Path(mcp_project["path"]) / ".perun" / "instructions" / "default.md").is_file()


def test_set_instructions_frontmatter_is_yaml_safe(mcp_project):
    """A filename with YAML-special characters must not corrupt the front matter."""
    result = mcp_mod.perun_set_instructions("body text", filename="weird: name.md")
    assert "Wrote instruction file" in result
    target = Path(mcp_project["path"]) / ".perun" / "instructions" / "weird: name.md"
    assert target.is_file()
    from perun import orchestrator

    name, enabled, body = orchestrator._load_project_instruction_file(target)
    assert body.strip() == "body text"
    assert enabled is True
    assert name == "weird: name"


def test_removed_migration_tool_is_absent():
    """The one-off legacy migration tool was removed with the DB columns."""
    assert not hasattr(mcp_mod, "perun_migrate_project_instructions")


def test_update_project_writes_instruction_file(mcp_project):
    result = mcp_mod.perun_update_project(mcp_project["slug"], instructions="Be terse.")
    assert "instructions" in result
    target = Path(mcp_project["path"]) / ".perun" / "instructions" / "default.md"
    assert target.is_file()
    assert "Be terse." in target.read_text(encoding="utf-8")


def test_set_manifest_routes_instructions_to_file(mcp_project):
    """``perun_set_manifest`` seeds .perun/instructions/default.md, not the DB."""
    result = mcp_mod.perun_set_manifest(
        {"version": 1, "name": "mcpsec", "instructions": "MCP MANIFEST RULE"},
        project=mcp_project["slug"],
    )
    assert "Wrote manifest" in result
    target = Path(mcp_project["path"]) / ".perun" / "instructions" / "default.md"
    assert target.is_file()
    assert "MCP MANIFEST RULE" in target.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# projects filter: omit vs explicit empty
# --------------------------------------------------------------------------- #
def test_validate_projects_list_none_is_distinct_from_empty():
    assert mcp_mod._validate_projects_list(None) == (None, "")
    assert mcp_mod._validate_projects_list([]) == ([], "")
    assert mcp_mod._validate_projects_list("x")[0] is None


def test_global_grep_empty_projects_means_none_to_search():
    result = mcp_mod.perun_global_grep("test", projects=[])
    assert result == "[perun] No projects to search."


# --------------------------------------------------------------------------- #
# Search input validation
# --------------------------------------------------------------------------- #
def test_perun_grep_rejects_empty_pattern(mcp_project):
    assert mcp_mod.perun_grep("  ", project=mcp_project["slug"]).startswith("[perun] pattern is required")


@pytest.mark.parametrize("value", ["abc", None, {}])
def test_perun_grep_rejects_bad_max_results(mcp_project, value):
    result = mcp_mod.perun_grep("test", project=mcp_project["slug"], max_results=value)
    assert result.startswith("[perun]")
    assert "max_results" in result


def test_perun_ast_grep_rejects_empty_rule(mcp_project):
    assert mcp_mod.perun_ast_grep("  ", project=mcp_project["slug"]).startswith("[perun] rule is required")


@pytest.mark.parametrize("value", ["abc", None, {}])
def test_perun_ast_grep_rejects_bad_max_results(mcp_project, value):
    result = mcp_mod.perun_ast_grep("$F($$$ARGS)", project=mcp_project["slug"], max_results=value)
    assert result.startswith("[perun]")
    assert "max_results" in result


def test_perun_global_grep_rejects_empty_pattern():
    assert mcp_mod.perun_global_grep("  ").startswith("[perun] pattern is required")


def test_perun_global_grep_rejects_bad_projects_type():
    result = mcp_mod.perun_global_grep("test", projects="not-a-list")
    assert result.startswith("[perun]")
    assert "projects" in result


def test_perun_global_ast_grep_rejects_empty_rule():
    assert mcp_mod.perun_global_ast_grep("  ").startswith("[perun] rule is required")


def test_perun_global_ast_grep_rejects_bad_projects_type():
    result = mcp_mod.perun_global_ast_grep("$F($$$ARGS)", projects={"set"})
    assert result.startswith("[perun]")
    assert "projects" in result


def test_perun_unified_search_rejects_bad_projects_type():
    result = mcp_mod.perun_unified_search("test", projects="not-a-list")
    assert result.startswith("[perun]")
    assert "projects" in result


@pytest.mark.parametrize("value", ["abc", None, {}])
def test_perun_unified_search_rejects_bad_max_results(value):
    result = mcp_mod.perun_unified_search("test", max_results=value)
    assert result.startswith("[perun]")
    assert "max_results" in result


@pytest.mark.parametrize("value", ["abc", None, {}])
def test_perun_unified_search_rejects_bad_max_memory(value):
    result = mcp_mod.perun_unified_search("test", max_memory=value)
    assert result.startswith("[perun]")
    assert "max_memory" in result


def test_perun_memory_search_rejects_bad_k():
    result = mcp_mod.perun_memory_search("test", k="not-an-int")
    assert result.startswith("[perun]")
    assert "k" in result
