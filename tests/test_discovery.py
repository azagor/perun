"""Discovery tests against synthetic directory trees."""

from __future__ import annotations

from pathlib import Path

from perun import discovery, registry


def _mk(path: Path, *files: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel in files:
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x", encoding="utf-8")
    return path


def test_classify_opencode_via_markers(workdir):
    assert discovery.classify(_mk(workdir / "a", ".opencode/x")) == "opencode"
    assert discovery.classify(_mk(workdir / "b", "opencode.json")) == "opencode"
    assert discovery.classify(_mk(workdir / "c", "AGENTS.md")) == "opencode"
    assert discovery.classify(_mk(workdir / "d", "docs/CODEMAP.md")) == "opencode"


def test_classify_git_and_plain(workdir):
    assert discovery.classify(_mk(workdir / "g", ".git/HEAD")) == "git"
    assert discovery.classify(_mk(workdir / "plain")) == "dir"


def test_capabilities(workdir):
    p = _mk(workdir / "proj", "opencode.json", "AGENTS.md", "docs/CODEMAP.md",
            "scripts/memory.py", ".git/HEAD")
    caps = discovery.capabilities(p)
    assert caps["opencode"] and caps["git"] and caps["agents_md"]
    assert caps["codemap"] and caps["memory_scripts"]


def test_detect_reads_description(workdir):
    p = _mk(workdir / "withreadme", "opencode.json")
    (p / "README.md").write_text("# Title\n\nA great project.\n", encoding="utf-8")
    info = discovery.detect(p)
    assert info["description"] == "A great project."
    assert info["kind"] == "opencode"


def test_scan_skips_noise_dirs(workdir):
    _mk(workdir / "real", "opencode.json")
    _mk(workdir / "node_modules", "opencode.json")
    _mk(workdir / ".hidden", "opencode.json")
    found = discovery.scan([str(workdir)], max_depth=1)
    names = {i["name"] for i in found}
    assert "real" in names
    assert "node_modules" not in names
    assert ".hidden" not in names


def test_register_and_discover_and_sync(workdir):
    _mk(workdir / "alpha", "opencode.json")
    _mk(workdir / "beta", ".git/HEAD")
    _mk(workdir / "plaindir")  # not registered

    res = discovery.discover_and_sync([str(workdir)], max_depth=1)
    slugs = {p["slug"] for p in registry.list_projects()}
    assert "alpha" in slugs and "beta" in slugs
    assert "plaindir" not in slugs
    assert len(res["created"]) == 2

    # idempotent second pass — no new rows, only updates
    res2 = discovery.discover_and_sync([str(workdir)], max_depth=1)
    assert res2["created"] == []
    assert len(registry.list_projects()) == 2


def test_register_updates_by_path(workdir):
    p = _mk(workdir / "alpha", "opencode.json")
    info = discovery.detect(p)
    proj, created = discovery.register(info)
    assert created is True
    info2 = discovery.detect(p)
    info2["description"] = "changed"
    proj2, created2 = discovery.register(info2)
    assert created2 is False and proj2["slug"] == proj["slug"]
    assert proj2["description"] == "changed"


def test_classify_via_manifest(workdir):
    p = _mk(workdir / "man")
    (p / "perun.md").write_text("---\nname: Man\n---\n", encoding="utf-8")
    assert discovery.classify(p) == "opencode"
    assert discovery.capabilities(p)["manifest"] is True


def test_register_merges_manifest(workdir):
    p = _mk(workdir / "manifested", "opencode.json")
    (p / "perun.md").write_text(
        "---\nversion: 1\nname: Fancy Name\nkind: git\n"
        "description: Declared by manifest\ntags: [x, y]\n"
        "model: prov/model\n---\n\n# body\n",
        encoding="utf-8",
    )
    proj, created = discovery.register(discovery.detect(p))
    assert created is True
    assert proj["name"] == "Fancy Name"
    assert proj["kind"] == "git"
    assert proj["description"] == "Declared by manifest"
    assert proj["tags"] == "x,y"
    assert proj["model"] == "prov/model"
    assert proj["has_manifest"] is True
    assert proj["manifest"]["name"] == "Fancy Name"

    stored = registry.get_project_manifest(proj["slug"])
    assert stored["data"]["name"] == "Fancy Name"
    assert "body" in stored["body"]
