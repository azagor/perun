"""Custody of third-party agent rosters: adopt, guard, remove, promote.

Perun never installs these agents — the upstream installer does — so the tests
work on files placed by hand, exactly as the real installer would leave them.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from perun import agent_catalog, config

# The autouse fixture below replaces this with a no-op so no test shells out to
# `opencode`; the two probe tests restore the real function.
_REAL_REGISTERED = agent_catalog.registered


def _fake_index(*slugs: str, commit: str = "deadbeefcafe") -> dict:
    agents = [
        {"slug": slug, "name": slug.replace("-", " ").title(),
         "description": f"{slug} specialist", "division": "engineering",
         "color": "#3B82F6", "emoji": "", "path": f"engineering/{slug}.md"}
        for slug in slugs
    ]
    return {
        "available": True, "source": "test", "commit": commit, "agents": agents,
        "by_slug": {a["slug"]: a for a in agents},
        "divisions": {"engineering": {"label": "Engineering", "count": len(agents)}},
        "collisions": [], "error": "",
    }


@pytest.fixture(autouse=True)
def catalog_env(tmp_path, monkeypatch):
    """Isolate the global agent dir, the manifest and the runtime probe."""
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    manifest = Path(config.HOME) / "agent_catalog.json"
    if manifest.exists():
        manifest.unlink()
    agent_catalog._index_cache.update(key=None, data=None)
    agent_catalog._measure_cache.update(key=None, ts=0.0, data=None)
    monkeypatch.setattr(agent_catalog, "registered", lambda *a, **k: {
        "ok": False, "count": None, "ids": [], "error": "not measured in tests"})
    yield base / "agents"
    if manifest.exists():
        manifest.unlink()


@pytest.fixture()
def global_agents(catalog_env):
    """The global ``agents/`` directory the fixture above created."""
    return catalog_env


def _write(directory: Path, slug: str, *, mode: str = "subagent",
           body: str = "You review code.") -> Path:
    path = Path(directory) / f"{slug}.md"
    path.write_text(
        f"---\nname: {slug.replace('-', ' ').title()}\n"
        f"description: {slug} specialist\nmode: {mode}\ncolor: '#3B82F6'\n---\n{body}\n",
        encoding="utf-8")
    return path


def test_the_shipped_index_covers_the_agency_roster():
    index = agent_catalog.load_index(force=True)
    assert index["available"] is True
    assert len(index["agents"]) > 200
    assert index["commit"]
    assert "code-reviewer" in index["by_slug"]
    assert "engineering" in index["divisions"]


def test_an_installed_agent_is_a_candidate_until_adopted(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer"))
    _write(global_agents, "code-reviewer")

    entry = agent_catalog.discover()[0]
    assert entry["state"] == "candidate"
    assert entry["division"] == "engineering"

    result = agent_catalog.adopt()
    assert result["adopted"]
    assert agent_catalog.discover()[0]["state"] == "adopted"


def test_a_file_outside_the_index_is_ignored(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer"))
    stranger = _write(global_agents, "my-own-agent")

    assert agent_catalog.discover() == []
    result = agent_catalog.adopt([stranger])
    assert result["adopted"] == []
    assert "not in the agent index" in result["skipped"][0]["reason"]


def test_editing_an_adopted_agent_marks_it_modified(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer"))
    path = _write(global_agents, "code-reviewer")
    agent_catalog.adopt()

    path.write_text(path.read_text(encoding="utf-8") + "\nOne more rule.\n", encoding="utf-8")
    assert agent_catalog.discover()[0]["state"] == "modified"


def test_remove_refuses_a_modified_agent(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer"))
    path = _write(global_agents, "code-reviewer")
    agent_catalog.adopt()
    path.write_text(path.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8")

    with pytest.raises(agent_catalog.CatalogError):
        agent_catalog.remove(str(path))
    assert path.exists()


def test_remove_backs_up_and_deletes_an_unmodified_agent(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer"))
    path = _write(global_agents, "code-reviewer")
    agent_catalog.adopt()

    result = agent_catalog.remove(str(path))
    assert result["backup"] and Path(result["backup"]).is_file()
    assert not path.exists()
    assert agent_catalog.discover() == []


def test_forget_leaves_the_file_in_place(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer"))
    path = _write(global_agents, "code-reviewer")
    agent_catalog.adopt()

    agent_catalog.forget(str(path))
    assert path.exists()
    assert agent_catalog.discover()[0]["state"] == "candidate"


def test_accept_rebaselines_a_deliberate_edit(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer"))
    path = _write(global_agents, "code-reviewer")
    agent_catalog.adopt()
    path.write_text(path.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8")

    agent_catalog.accept(str(path))
    assert agent_catalog.discover()[0]["state"] == "adopted"


def test_promote_makes_a_subagent_runnable_and_keeps_the_body(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer"))
    path = _write(global_agents, "code-reviewer", mode="subagent", body="Body stays intact.")
    agent_catalog.adopt()

    result = agent_catalog.promote(str(path), "all")
    assert result["mode"] == "all"
    text = path.read_text(encoding="utf-8")
    assert "mode: all" in text
    assert "Body stays intact." in text
    # The rewrite is a deliberate, recorded edit — not "modified".
    entry = agent_catalog.discover()[0]
    assert entry["state"] == "adopted"
    assert entry["mode_intent"] == "all"


def test_promote_inserts_mode_when_the_frontmatter_has_none(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer"))
    path = global_agents / "code-reviewer.md"
    path.write_text("---\nname: Code Reviewer\ndescription: x\n---\nbody\n", encoding="utf-8")
    agent_catalog.adopt()

    agent_catalog.promote(str(path), "all")
    assert path.read_text(encoding="utf-8").startswith("---\nname: Code Reviewer\nmode: all\n")


def test_an_adopted_agent_without_a_current_index_entry_is_unlisted(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer"))
    _write(global_agents, "code-reviewer")
    agent_catalog.adopt()

    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("other-agent"))
    assert agent_catalog.discover()[0]["state"] == "unlisted"


def test_adopt_refuses_a_path_outside_the_agent_directories(monkeypatch, tmp_path, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer"))
    outside = tmp_path / "code-reviewer.md"
    outside.write_text("---\nname: Code Reviewer\n---\n", encoding="utf-8")

    with pytest.raises(agent_catalog.CatalogError):
        agent_catalog._resolve_target(outside)
    result = agent_catalog.adopt([str(outside)])
    assert result["adopted"] == []


def test_preflight_names_what_a_reinstall_would_destroy(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer", "ui-designer"))
    first = _write(global_agents, "code-reviewer")
    _write(global_agents, "ui-designer")
    agent_catalog.adopt()
    first.write_text(first.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8")

    report = agent_catalog.preflight()
    assert report["backup_first"] is True
    assert str(first) in report["modified"]
    assert len(report["will_overwrite"]) == 2


def test_backup_copies_only_the_modified_files(monkeypatch, global_agents):
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _fake_index("code-reviewer", "ui-designer"))
    first = _write(global_agents, "code-reviewer")
    _write(global_agents, "ui-designer")
    agent_catalog.adopt()
    first.write_text(first.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8")

    result = agent_catalog.backup_modified()
    assert result["count"] == 1
    assert Path(result["copies"][0]["backup"]).is_file()


def test_team_file_lists_slugs_and_refuses_an_over_budget_selection(monkeypatch):
    monkeypatch.setattr(agent_catalog, "load_index",
                        lambda force=False: _fake_index("code-reviewer", "ui-designer"))
    result = agent_catalog.team_file(["engineering"])
    assert result["count"] == 2
    assert result["text"].split() == ["code-reviewer", "ui-designer"]

    with pytest.raises(agent_catalog.CatalogError) as exc:
        agent_catalog.team_file(["engineering"], limit=1)
    assert "ceiling" in str(exc.value)


def test_a_corrupt_index_degrades_instead_of_crashing(monkeypatch, tmp_path):
    index_path = tmp_path / "index.json"
    index_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(agent_catalog, "INDEX_PATH", index_path)
    agent_catalog._index_cache.update(key=None, data=None)

    index = agent_catalog.load_index(force=True)
    assert index["available"] is False
    assert "unreadable" in index["error"]
    # A status call still answers.
    assert agent_catalog.status(measure=False)["ok"] is True


def test_slots_count_installed_files_and_report_the_ceiling(monkeypatch, global_agents):
    _write(global_agents, "one")
    _write(global_agents, "two")
    monkeypatch.setattr(agent_catalog, "registered", lambda *a, **k: {
        "ok": True, "count": 12, "ids": ["one", "build"], "error": ""})

    budget = agent_catalog.slots(measure=True)
    assert budget["installed_global"] == 2
    assert budget["registered"] == 12
    assert budget["remaining"] == agent_catalog.DEFAULT_LIMIT - 12
    assert budget["unregistered"] == ["two"]


def test_registered_parses_the_runtime_listing(monkeypatch):
    payload = json.dumps([{"id": "build"}, {"id": "perun-docs"}, {"nope": 1}])

    def fake_run(argv, **kwargs):
        assert "debug" in argv and "agents" in argv
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    monkeypatch.setattr(agent_catalog.subprocess, "run", fake_run)
    monkeypatch.setattr(agent_catalog, "registered", _REAL_REGISTERED)
    agent_catalog._measure_cache.update(key=None, ts=0.0, data=None)
    probe = agent_catalog.registered(force=True)
    assert probe["ok"] is True
    assert probe["count"] == 2
    assert probe["ids"] == ["build", "perun-docs"]


def test_registered_degrades_when_opencode_is_missing(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("opencode")

    monkeypatch.setattr(agent_catalog.subprocess, "run", boom)
    monkeypatch.setattr(agent_catalog, "registered", _REAL_REGISTERED)
    agent_catalog._measure_cache.update(key=None, ts=0.0, data=None)
    probe = agent_catalog.registered(force=True)
    assert probe["ok"] is False
    assert probe["count"] is None
    assert "opencode" in probe["error"]
