"""Installing a roster from the pinned snapshot (the one write path Perun owns)."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from perun import agent_catalog, config


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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    manifest = Path(config.HOME) / "agent_catalog.json"
    if manifest.exists():
        manifest.unlink()
    agent_catalog._index_cache.update(key=None, data=None)
    archive = _archive(tmp_path, {"engineering/code-reviewer.md": _source()})
    monkeypatch.setattr(agent_catalog, "roster_archive", lambda ref, force=False: archive)
    monkeypatch.setattr(agent_catalog, "load_index",
                        lambda force=False: _index(("code-reviewer", "engineering")))
    yield base / "agents", archive
    if manifest.exists():
        manifest.unlink()


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
    manifest = Path(config.HOME) / "agent_catalog.json"
    if manifest.exists():
        manifest.unlink()
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
    try:
        assert result["count"] == 1
        assert "could not be written" in result["skipped"][0]["reason"]
        # The manifest was still saved, so the written file is tracked.
        assert {e["slug"]: e["state"] for e in agent_catalog.discover()} == {
            "first-agent": "adopted"}
        # No temp file is left behind.
        assert not list((base / "agents").glob("*.tmp"))
    finally:
        if manifest.exists():
            manifest.unlink()


def test_an_oversized_roster_member_is_skipped(tmp_path, monkeypatch):
    base = tmp_path / "opencode"
    (base / "agents").mkdir(parents=True)
    monkeypatch.setattr(config, "OPENCODE_CONFIG_DIR", base)
    manifest = Path(config.HOME) / "agent_catalog.json"
    if manifest.exists():
        manifest.unlink()
    monkeypatch.setattr(agent_catalog, "AGENT_MAX_BYTES", 16)
    monkeypatch.setattr(agent_catalog, "load_index",
                        lambda force=False: _index(("code-reviewer", "engineering")))
    archive = _archive(tmp_path, {"engineering/code-reviewer.md": _source()})
    monkeypatch.setattr(agent_catalog, "roster_archive", lambda ref, force=False: archive)

    result = agent_catalog.install(["engineering"])
    assert result["count"] == 0
    assert "missing from the roster archive" in result["skipped"][0]["reason"]
    if manifest.exists():
        manifest.unlink()
