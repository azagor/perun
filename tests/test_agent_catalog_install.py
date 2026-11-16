"""Installing a roster from the pinned snapshot (the one write path Perun owns)."""

from __future__ import annotations

import io
import shutil
import json
import tarfile
from pathlib import Path

import pytest

from perun import agent_assignments, agent_catalog, config


def _index(*entries: tuple[str, str], commit: str = "cafebabe1234") -> dict:
    """``(slug, division)`` pairs; the archive path follows the division."""
    agents = [{"slug": slug, "name": slug.replace("-", " ").title(),
               "description": f"{slug} specialist", "division": division,
               "color": "purple", "emoji": "", "path": f"{division}/{slug}.md"}
              for slug, division in entries]
    return {"available": True, "source": "test", "commit": commit, "agents": agents,
            "by_slug": {a["slug"]: a for a in agents},
            "divisions": {}, "collisions": [], "error": ""}


def _archive(tmp_path: Path, files: dict[str, str]) -> Path:
    path = tmp_path / "roster.tar.gz"
    with tarfile.open(path, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"agency-agents-abc/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _source(name: str = "Code Reviewer", description: str = "Reviews — carefully: really") -> str:
    return (f"---\nname: {name}\ndescription: {description}\ncolor: purple\n---\n"
            f"# {name} Agent\n\nBe specific.\n")


@pytest.fixture(autouse=True)
def isolated_manifest(monkeypatch):
    """Clear the manifest and never shell out to `opencode` from this module."""
    monkeypatch.setattr(agent_catalog, "registered", lambda *a, **k: {
        "ok": False, "count": None, "ids": [], "error": "disabled in tests"})
    manifest = Path(config.HOME) / "agent_catalog.json"
    library = agent_assignments.library_dir()
    if manifest.exists():
        manifest.unlink()
    shutil.rmtree(library, ignore_errors=True)
    yield
    if manifest.exists():
        manifest.unlink()
    shutil.rmtree(library, ignore_errors=True)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    agent_catalog._index_cache.update(key=None, data=None)
    archive = _archive(tmp_path, {"engineering/code-reviewer.md": _source()})
    monkeypatch.setattr(agent_catalog, "roster_archive", lambda ref, force=False: archive)
    monkeypatch.setattr(agent_catalog, "load_index",
                        lambda force=False: _index(("code-reviewer", "engineering")))
    yield base / "agents", archive


def test_render_matches_upstream_frontmatter():
    fields, body = agent_catalog.parse_agent(_source("O'Brien, Dev", "Ships: fast 'n' safe"))
    text = agent_catalog.render_agent(fields, body)
    assert text.startswith("---\nname: 'O''Brien, Dev'\ndescription: 'Ships: fast ''n'' safe'\nmode: subagent\n")
    assert "color: '#9B59B6'" in text
    assert body in text


def test_colour_resolution_follows_upstream():
    assert agent_catalog.resolve_color("purple") == "#9B59B6"
    assert agent_catalog.resolve_color("  Slate ") == "#64748B"
    assert agent_catalog.resolve_color("#abc123") == "#ABC123"
    assert agent_catalog.resolve_color("abc123") == "#ABC123"
    assert agent_catalog.resolve_color("chartreuse") == agent_catalog.FALLBACK_COLOR
    assert agent_catalog.resolve_color("") == agent_catalog.FALLBACK_COLOR


def test_slug_rule_agrees_with_the_shipped_index():
    index = agent_catalog.load_index(force=True)
    assert index["available"] is True
    mismatched = [a["name"] for a in index["agents"] if agent_catalog.slugify(a["name"]) != a["slug"]]
    assert mismatched == []


def test_install_writes_a_converted_agent_and_records_it(env):
    agents_dir, _ = env
    result = agent_catalog.install(["engineering"])

    assert result["count"] == 1
    target = agents_dir / "code-reviewer.md"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert "mode: subagent" in text and 'color:' in text
    assert agent_catalog.discover()[0]["state"] == "adopted"
    assert agent_catalog.discover()[0]["mode_intent"] == "subagent"


def test_install_refuses_to_clobber_an_untracked_file(env):
    agents_dir, _ = env
    existing = agents_dir / "code-reviewer.md"
    existing.write_text("---\nname: Mine\n---\n", encoding="utf-8")

    result = agent_catalog.install(["engineering"])
    assert result["count"] == 0
    assert "not tracked" in result["skipped"][0]["reason"]
    assert existing.read_text(encoding="utf-8").startswith("---\nname: Mine")


def test_install_refuses_to_clobber_an_edited_agent(env):
    agents_dir, _ = env
    agent_catalog.install(["engineering"])
    target = agents_dir / "code-reviewer.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8")

    result = agent_catalog.install(["engineering"])
    assert result["count"] == 0
    assert "edited" in result["skipped"][0]["reason"]


def test_install_replaces_an_unmodified_agent(env):
    agents_dir, _ = env
    agent_catalog.install(["engineering"])
    target = agents_dir / "code-reviewer.md"
    first = target.read_text(encoding="utf-8")

    result = agent_catalog.install(["engineering"])
    assert result["count"] == 1
    assert target.read_text(encoding="utf-8") == first
    assert agent_catalog.discover()[0]["state"] == "adopted"


def test_install_refuses_a_selection_over_the_ceiling(env):
    with pytest.raises(agent_catalog.CatalogError) as exc:
        agent_catalog.install(["engineering"], limit=0)
    assert "ceiling" in str(exc.value)


def test_install_needs_a_project_for_a_project_scope(env):
    with pytest.raises(agent_catalog.CatalogError):
        agent_catalog.install(["engineering"], scope="project")


def test_available_marks_what_is_installed(env):
    agents_dir, _ = env
    before = agent_catalog.available()
    assert before["count"] == 1
    assert before["agents"][0]["installed"] is False

    agent_catalog.install(["engineering"])
    assert (agents_dir / "code-reviewer.md").is_file()
    after = agent_catalog.available()
    assert after["agents"][0]["installed"] is True


def test_a_download_failure_degrades_to_the_cached_snapshot(tmp_path, monkeypatch):
    cached = tmp_path / "cafebabe.tar.gz"
    cached.write_bytes(b"cached")
    monkeypatch.setattr(agent_catalog, "archive_path", lambda ref: cached)

    def boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(agent_catalog.urllib.request, "urlopen", boom)
    # force=True reaches the network; the cached snapshot is the fallback.
    assert agent_catalog.roster_archive("cafebabe", force=True) == cached


def test_a_download_failure_without_a_cache_explains_the_alternative(tmp_path, monkeypatch):
    missing = tmp_path / "missing.tar.gz"
    monkeypatch.setattr(agent_catalog, "archive_path", lambda ref: missing)

    def boom(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(agent_catalog.urllib.request, "urlopen", boom)
    with pytest.raises(agent_catalog.CatalogError) as exc:
        agent_catalog.roster_archive("cafebabe")
    assert "adopt" in str(exc.value)


def test_a_hostile_slug_in_the_index_never_becomes_a_path(tmp_path, monkeypatch):
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps({
        "source": "x", "commit": "abc", "divisions": {},
        "agents": [
            {"slug": "../../evil", "name": "Evil", "description": "d",
             "division": "x", "path": "x/e.md"},
            {"slug": "ok-agent", "name": "Ok", "description": "d",
             "division": "x", "path": "x/o.md"},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(agent_catalog, "INDEX_PATH", index_path)
    agent_catalog._index_cache.update(key=None, data=None)

    index = agent_catalog.load_index(force=True)
    assert list(index["by_slug"]) == ["ok-agent"]


def test_a_locked_file_is_skipped_and_earlier_writes_stay_tracked(tmp_path, monkeypatch):
    import os

    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    monkeypatch.setattr(agent_catalog, "load_index",
                        lambda force=False: _index(("first-agent", "engineering"),
                                                   ("second-agent", "engineering")))
    archive = _archive(tmp_path, {
        "engineering/first-agent.md": _source("First Agent"),
        "engineering/second-agent.md": _source("Second Agent"),
    })
    monkeypatch.setattr(agent_catalog, "roster_archive", lambda ref, force=False: archive)

    real_replace = os.replace

    def flaky(src, dst):
        if str(dst).endswith("second-agent.md"):
            raise PermissionError("locked by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(agent_catalog.os, "replace", flaky)
    result = agent_catalog.install(["engineering"])
    assert result["count"] == 1
    assert "could not be written" in result["skipped"][0]["reason"]
    # The manifest was still saved, so the written file is tracked.
    assert {e["slug"]: e["state"] for e in agent_catalog.discover()} == {
        "first-agent": "adopted"}
    # No temp file is left behind.
    assert not list((base / "agents").glob("*.tmp"))


def test_the_ceiling_counts_the_runtime_baseline(env, monkeypatch):
    """12 registered over 1 visible roster file means 11 built-ins; add them."""
    agents_dir, _ = env
    (agents_dir / "code-reviewer.md").write_text("---\nname: Old\n---\n", encoding="utf-8")
    monkeypatch.setattr(agent_catalog, "registered", lambda *a, **k: {
        "ok": True, "count": 12, "ids": ["build", "plan", "code-reviewer"], "error": ""})

    budget = agent_catalog.slots(measure=True)
    assert budget["baseline"] == 11
    assert budget["baseline_source"] == "runtime"
    assert budget["projected"] == 12

    with pytest.raises(agent_catalog.CatalogError) as exc:
        agent_catalog.install(["engineering"], limit=11)
    message = str(exc.value)
    assert "12 agents would be registered" in message
    assert "11 non-roster" in message


def test_the_baseline_is_not_trusted_when_the_runtime_sees_other_files(env, monkeypatch):
    """The runtime pointing at another directory must not inflate the budget."""
    agents_dir, _ = env
    (agents_dir / "code-reviewer.md").write_text("---\nname: Old\n---\n", encoding="utf-8")
    monkeypatch.setattr(agent_catalog, "registered", lambda *a, **k: {
        "ok": True, "count": 120, "ids": ["build", "plan"], "error": ""})

    budget = agent_catalog.slots(measure=True)
    assert budget["baseline"] is None
    assert budget["baseline_source"] == "files"
    assert budget["projected"] == 1


def test_a_project_install_skips_a_slug_that_exists_globally(tmp_path, monkeypatch):
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    project = tmp_path / "proj"
    (project / ".opencode" / "agents").mkdir(parents=True)
    (base / "agents" / "code-reviewer.md").write_text(
        "---\nname: Global Copy\n---\n", encoding="utf-8")
    monkeypatch.setattr(agent_catalog, "load_index",
                        lambda force=False: _index(("code-reviewer", "engineering")))
    archive = _archive(tmp_path, {"engineering/code-reviewer.md": _source()})
    monkeypatch.setattr(agent_catalog, "roster_archive", lambda ref, force=False: archive)

    result = agent_catalog.install(["engineering"], scope="project",
                                   project_path=project)
    assert result["count"] == 0
    assert "already installed globally" in result["skipped"][0]["reason"]
    assert not (project / ".opencode" / "agents" / "code-reviewer.md").exists()


def test_an_upgrade_keeps_a_backup_of_the_previous_file(env):
    agents_dir, _ = env
    agent_catalog.install(["engineering"])
    target = agents_dir / "code-reviewer.md"
    original = target.read_text(encoding="utf-8")

    result = agent_catalog.install(["engineering"])
    assert result["count"] == 1
    copies = list((Path(config.HOME) / "backups" / "agency").glob("code-reviewer.md-*"))
    assert any(p.read_text(encoding="utf-8") == original for p in copies)


def test_the_ceiling_defaults_to_the_shipped_value_and_is_configurable(env):
    assert agent_catalog.slots(measure=False)["limit"] == agent_catalog.DEFAULT_LIMIT
    assert agent_catalog.set_limit(150)["limit"] == 150
    # The setting survives alongside the custody entries it was saved with.
    agent_catalog.install(["engineering"])
    assert agent_catalog.slots(measure=False)["limit"] == 150
    assert {e["slug"] for e in agent_catalog.discover()} == {"code-reviewer"}

    with pytest.raises(agent_catalog.CatalogError) as exc:
        agent_catalog.install(["engineering"], limit=0)
    assert "ceiling" in str(exc.value)


@pytest.mark.parametrize("value", [0, 1001, -5])
def test_an_invalid_ceiling_is_rejected(value):
    with pytest.raises(agent_catalog.CatalogError):
        agent_catalog.set_limit(value)


def test_a_project_install_writes_into_the_project(tmp_path, monkeypatch):
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    project = tmp_path / "proj"
    (project / ".opencode" / "agents").mkdir(parents=True)
    monkeypatch.setattr(agent_catalog, "load_index",
                        lambda force=False: _index(("code-reviewer", "engineering")))
    archive = _archive(tmp_path, {"engineering/code-reviewer.md": _source()})
    monkeypatch.setattr(agent_catalog, "roster_archive", lambda ref, force=False: archive)

    result = agent_catalog.install(["engineering"], scope="project", project_path=project)
    assert result["scope"] == "project"
    target = project / ".opencode" / "agents" / "code-reviewer.md"
    assert target.is_file()
    assert not (base / "agents" / "code-reviewer.md").exists()
    entries = agent_catalog.discover(project)
    assert [(e["slug"], e["scope"], e["state"]) for e in entries] == [
        ("code-reviewer", "project", "adopted")]


def test_an_oversized_roster_member_is_skipped(tmp_path, monkeypatch):
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    monkeypatch.setattr(agent_catalog, "AGENT_MAX_BYTES", 16)
    monkeypatch.setattr(agent_catalog, "load_index",
                        lambda force=False: _index(("code-reviewer", "engineering")))
    archive = _archive(tmp_path, {"engineering/code-reviewer.md": _source()})
    monkeypatch.setattr(agent_catalog, "roster_archive", lambda ref, force=False: archive)

    result = agent_catalog.install(["engineering"])
    assert result["count"] == 0
    assert "missing from the roster archive" in result["skipped"][0]["reason"]


def test_installed_lists_compact_rows(env):
    agent_catalog.install(["engineering"])
    listing = agent_catalog.installed()
    assert listing["count"] == 1
    row = listing["agents"][0]
    assert set(row) == {"path", "slug", "division", "state", "mode",
                        "assignments", "paths"}
    assert row["assignments"] == ["global"]
    assert listing["by_division"] == {"engineering": 1}


def test_remove_by_division_skips_edited_agents(env):
    agents_dir, _ = env
    agent_catalog.install(["engineering"])
    result = agent_catalog.remove_many(division="engineering")
    assert result["count"] == 1
    assert not (agents_dir / "code-reviewer.md").exists()

    agent_catalog.install(["engineering"])
    target = agents_dir / "code-reviewer.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8")
    kept = agent_catalog.remove_many(division="engineering")
    assert kept["count"] == 0
    assert kept["skipped_count"] == 1
    assert target.exists()


def test_remove_many_needs_a_selection(env):
    with pytest.raises(agent_catalog.CatalogError):
        agent_catalog.remove_many()


def test_perun_agents_are_untouchable(env):
    agents_dir, _ = env
    target = agents_dir / "perun-docs.md"
    target.write_text("---\nname: Perun Docs\n---\n", encoding="utf-8")

    for action in (agent_catalog.accept, agent_catalog.forget,
                   agent_catalog.remove, agent_catalog.promote):
        with pytest.raises(agent_catalog.CatalogError) as exc:
            action(str(target))
        assert "Perun agent" in str(exc.value)
    assert target.exists()

    result = agent_catalog.adopt([str(target)])
    assert result["adopted"] == []
    assert "Perun agent" in result["skipped"][0]["reason"]


def test_install_refuses_a_roster_entry_that_collides_with_a_perun_agent(tmp_path, monkeypatch):
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    monkeypatch.setattr(agent_catalog, "load_index",
                        lambda force=False: _index(("perun-docs", "engineering")))
    archive = _archive(tmp_path, {"engineering/perun-docs.md": _source("Perun Docs")})
    monkeypatch.setattr(agent_catalog, "roster_archive", lambda ref, force=False: archive)

    result = agent_catalog.install(["engineering"])
    assert result["count"] == 0
    assert "Perun agent" in result["skipped"][0]["reason"]
    assert not (base / "agents" / "perun-docs.md").exists()


def test_removing_a_project_division_leaves_the_global_copies(tmp_path, monkeypatch):
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    project = tmp_path / "proj"
    (project / ".opencode" / "agents").mkdir(parents=True)
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _index(
        ("code-reviewer", "engineering"), ("ui-designer", "engineering")))

    global_copy = base / "agents" / "code-reviewer.md"
    global_copy.write_text(_source("Code Reviewer"), encoding="utf-8")
    project_copy = project / ".opencode" / "agents" / "ui-designer.md"
    project_copy.write_text(_source("UI Designer"), encoding="utf-8")
    agent_catalog.adopt([str(global_copy)])
    agent_catalog.adopt([str(project_copy)], project)
    assert {e["slug"]: e["scope"] for e in agent_catalog.discover(project)} == {
        "code-reviewer": "global", "ui-designer": "project"}

    result = agent_catalog.remove_many(division="engineering", project_path=project)
    assert result["count"] == 1
    assert global_copy.exists()
    assert not project_copy.exists()


def test_the_hash_cache_follows_the_file(tmp_path, monkeypatch):
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    target = base / "agents" / "code-reviewer.md"
    target.write_text("---\nname: A\n---\n", encoding="utf-8")

    first = agent_catalog._hash(target)
    assert agent_catalog._hash(target) == first  # served from the cache
    target.write_text("---\nname: A\n---\nchanged\n", encoding="utf-8")
    assert agent_catalog._hash(target) != first


def test_removing_many_writes_the_manifest_once(tmp_path, monkeypatch):
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    monkeypatch.setattr(agent_catalog, "load_index", lambda force=False: _index(
        ("code-reviewer", "engineering"), ("ui-designer", "engineering")))
    for slug in ("code-reviewer", "ui-designer"):
        (base / "agents" / f"{slug}.md").write_text(_source(slug.title()), encoding="utf-8")
    agent_catalog.adopt()

    saves = []
    original = agent_catalog._save_manifest
    monkeypatch.setattr(agent_catalog, "_save_manifest",
                        lambda payload: (saves.append(1), original(payload))[1])
    result = agent_catalog.remove_many(division="engineering")
    assert result["count"] == 2
    assert len(saves) == 1


def test_a_dry_run_lists_without_removing(env):
    agents_dir, _ = env
    agent_catalog.install(["engineering"])
    plan = agent_catalog.remove_many(division="engineering", dry_run=True)
    assert plan["dry_run"] is True
    assert len(plan["would_remove"]) == 1
    assert (agents_dir / "code-reviewer.md").exists()


def test_the_refusal_tells_the_operator_how_to_raise_the_ceiling(env, monkeypatch):
    agents_dir, _ = env
    (agents_dir / "code-reviewer.md").write_text(_source(), encoding="utf-8")
    monkeypatch.setattr(agent_catalog, "registered", lambda *a, **k: {
        "ok": True, "count": 12, "ids": ["build", "code-reviewer"], "error": ""})

    with pytest.raises(agent_catalog.CatalogError) as exc:
        agent_catalog.install(["engineering"], limit=11)
    assert "agent-catalog limit" in str(exc.value)


def test_the_default_ceiling_reflects_the_measured_runtime():
    # Measured on OpenCode v2.2.5: 307 agents registered from 300 files with
    # nothing dropped. A stale 119 would block installs this build handles.
    assert agent_catalog.DEFAULT_LIMIT >= 200
    assert "measured" in agent_catalog.LIMIT_SOURCE


def test_promote_refuses_an_edited_agent(env):
    agents_dir, _ = env
    agent_catalog.install(["engineering"])
    path = agents_dir / "code-reviewer.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8")

    with pytest.raises(agent_catalog.CatalogError) as exc:
        agent_catalog.promote(str(path), "all")
    assert "accept the current content" in str(exc.value)


def test_installed_reports_and_follows_the_mode(env):
    agents_dir, _ = env
    agent_catalog.install(["engineering"])
    assert agent_catalog.installed()["agents"][0]["mode"] == "subagent"

    row = agent_catalog.installed()["agents"][0]
    agent_catalog.promote(row["path"], "all")
    assert agent_catalog.installed()["agents"][0]["mode"] == "all"

    agent_catalog.promote(row["path"], "subagent")
    assert agent_catalog.installed()["agents"][0]["mode"] == "subagent"


def test_target_for_resolves_a_slug(env):
    agents_dir, _ = env
    agent_catalog.install(["engineering"])
    assert agent_catalog._target_for("code-reviewer") == (
        agents_dir / "code-reviewer.md").resolve()
    assert agent_catalog._target_for("code-reviewer.md") == (
        agents_dir / "code-reviewer.md").resolve()


def test_target_for_rejects_an_unknown_slug(env):
    with pytest.raises(agent_catalog.CatalogError) as exc:
        agent_catalog._target_for("no-such-agent")
    assert "unknown agent" in str(exc.value)


def test_agent_mode_is_empty_without_a_mode_line(tmp_path):
    path = tmp_path / "plain.md"
    path.write_text("---\nname: Plain\ndescription: no mode\n---\nbody\n", encoding="utf-8")
    assert agent_catalog._agent_mode(path) == ""



def test_a_library_install_is_unassigned(env):
    result = agent_catalog.install(["engineering"], scope="library")
    assert result["count"] == 1

    lib = agent_assignments.library_dir() / "code-reviewer.md"
    assert lib.is_file()
    assert not (config.OPENCODE_CONFIG_DIR / "agents" / "code-reviewer.md").exists()

    listing = agent_catalog.installed()
    row = next(r for r in listing["agents"] if r["slug"] == "code-reviewer")
    assert row["assignments"] == ["library"]
    assert "code-reviewer" in listing["unassigned"]


def test_assigning_from_the_library_materialises_a_copy(env):
    agent_catalog.install(["engineering"], scope="library")
    result = agent_assignments.assign(["code-reviewer"], "global")
    assert result["count"] == 1
    assert (config.OPENCODE_CONFIG_DIR / "agents" / "code-reviewer.md").is_file()

    row = next(r for r in agent_catalog.installed()["agents"]
               if r["slug"] == "code-reviewer")
    assert set(row["assignments"]) == {"library", "global"}
    assert "code-reviewer" not in agent_catalog.installed()["unassigned"]


def test_a_library_file_is_not_counted_as_registered(env):
    agent_catalog.install(["engineering"], scope="library")
    assert agent_catalog.slots(measure=False)["installed_global"] == 0
