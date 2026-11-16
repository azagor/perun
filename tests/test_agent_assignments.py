"""Placement tests: global, per-project and unassigned copies.

The catalogue owns custody; these tests use its real adopt/manifest code with a
fake roster index, so assignment is exercised end to end without a network call
or an OpenCode process.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from perun import agent_assignments as assign
from perun import agent_catalog, config


def _index(*slugs: str) -> dict:
    agents = [{"slug": s, "name": s.replace("-", " ").title(),
               "description": f"{s} specialist", "division": "engineering",
               "color": "#3B82F6", "emoji": "", "path": f"engineering/{s}.md"}
              for s in slugs]
    return {"available": True, "source": "test", "commit": "deadbeef",
            "agents": agents, "by_slug": {a["slug"]: a for a in agents},
            "divisions": {}, "collisions": [], "error": ""}


def _agent(slug: str, mode: str = "subagent") -> str:
    return (f"---\nname: {slug.replace('-', ' ').title()}\n"
            f"description: {slug} specialist\nmode: {mode}\ncolor: '#3B82F6'\n---\nBody\n")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A global agent plus a project-only one, and two registered projects."""
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj2 = tmp_path / "proj2"
    (proj / ".opencode" / "agents").mkdir(parents=True)
    (proj2 / ".opencode" / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    monkeypatch.setattr(agent_catalog, "load_index",
                        lambda force=False: _index("code-reviewer", "ui-designer"))
    monkeypatch.setattr(assign, "project_paths",
                        lambda: {"proj": str(proj), "proj2": str(proj2)})
    monkeypatch.setattr(agent_catalog, "registered", lambda *a, **k: {
        "ok": False, "count": None, "ids": [], "error": "disabled in tests"})
    manifest = Path(config.HOME) / "agent_catalog.json"
    if manifest.exists():
        manifest.unlink()

    global_agent = base / "agents" / "code-reviewer.md"
    global_agent.write_text(_agent("code-reviewer"), encoding="utf-8")
    agent_catalog.adopt([str(global_agent)])

    local_agent = proj / ".opencode" / "agents" / "ui-designer.md"
    local_agent.write_text(_agent("ui-designer"), encoding="utf-8")
    agent_catalog.adopt([str(local_agent)], proj)

    yield {"global": global_agent, "base": base, "proj": proj, "proj2": proj2,
           "local": local_agent}
    if manifest.exists():
        manifest.unlink()


def test_assignment_of_classifies_each_scope(env, tmp_path):
    projects = {"proj": str(env["proj"]), "proj2": str(env["proj2"])}
    assert assign.assignment_of(env["global"], projects) == "global"
    assert assign.assignment_of(env["local"], projects) == "project:proj"
    assert assign.assignment_of(tmp_path / "elsewhere.md", projects) == "external"


def test_target_dir_resolves_and_rejects(env):
    projects = {"proj": str(env["proj"])}
    assert assign.target_dir("global", projects) == env["base"] / "agents"
    assert assign.target_dir("project:proj", projects) == (
        env["proj"] / ".opencode" / "agents")
    with pytest.raises(agent_catalog.CatalogError):
        assign.target_dir("project:nope", projects)
    with pytest.raises(agent_catalog.CatalogError):
        assign.target_dir("elsewhere", projects)


def test_assign_materialises_a_local_only_copy(env):
    result = assign.assign(["ui-designer"], "project:proj2")
    assert result["count"] == 1

    copy = env["proj2"] / ".opencode" / "agents" / "ui-designer.md"
    assert copy.is_file()
    assert copy.read_text(encoding="utf-8") == env["local"].read_text(encoding="utf-8")
    assert (env["proj2"] / ".opencode" / "agents" / ".gitignore").read_text().strip() == "*"


def test_a_project_assignment_is_a_noop_when_the_agent_is_global(env):
    result = assign.assign(["code-reviewer"], "project:proj2")
    assert result["count"] == 0
    assert "already assigned globally" in result["skipped"][0]["reason"]
    assert not (env["proj2"] / ".opencode" / "agents" / "code-reviewer.md").exists()


def test_unassign_removes_only_that_copy(env):
    assign.assign(["ui-designer"], "project:proj2")
    copy = env["proj2"] / ".opencode" / "agents" / "ui-designer.md"
    assert copy.is_file()

    result = assign.unassign(["ui-designer"], "project:proj2")
    assert result["count"] == 1
    assert not copy.exists()
    assert env["local"].is_file()


def test_promote_fans_out_to_every_copy(env):
    assign.assign(["ui-designer"], "project:proj2")
    copy = env["proj2"] / ".opencode" / "agents" / "ui-designer.md"

    result = agent_catalog.promote(env["local"], "all")
    assert result["mode"] == "all"
    assert "mode: all" in copy.read_text(encoding="utf-8")
    assert result["fan_out"]["count"] == 1


def test_remove_slug_removes_every_copy(env):
    assign.assign(["ui-designer"], "project:proj2")
    copy = env["proj2"] / ".opencode" / "agents" / "ui-designer.md"

    result = assign.remove_slug("ui-designer")
    assert result["count"] == 2
    assert not env["local"].exists() and not copy.exists()


def test_assign_refuses_to_clobber_an_untracked_target(env):
    stray = env["proj2"] / ".opencode" / "agents" / "ui-designer.md"
    stray.write_text("---\nname: Mine\n---\n", encoding="utf-8")

    result = assign.assign(["ui-designer"], "project:proj2")
    assert result["count"] == 0
    assert "not tracked" in result["skipped"][0]["reason"]
    assert stray.read_text(encoding="utf-8").startswith("---\nname: Mine")


def test_the_ceiling_refuses_an_assignment(env):
    with pytest.raises(agent_catalog.CatalogError) as exc:
        assign.assign(["ui-designer"], "project:proj2", limit=0)
    assert "ceiling" in str(exc.value)


def test_assign_refuses_a_perun_agent(env):
    protected = env["base"] / "agents" / "perun-docs.md"
    protected.write_text("---\nname: Perun Docs\nmode: primary\n---\nbody\n", encoding="utf-8")

    result = assign.assign(["perun-docs"], "global")
    assert result["count"] == 0
    assert "Perun agent" in result["skipped"][0]["reason"]
