"""Discovery tests against synthetic directory trees."""

from __future__ import annotations

import os
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


def test_register_seeds_manifest_instructions_once(workdir):
    """Manifest instructions create default.md, but re-discovery never clobbers it."""
    p = _mk(workdir / "manifested", "opencode.json")
    (p / "perun.md").write_text(
        "---\nname: Manifested\ninstructions: Always run tests.\n---\n",
        encoding="utf-8",
    )
    discovery.register(discovery.detect(p))
    target = p / ".perun" / "instructions" / "default.md"
    assert target.is_file()
    assert "Always run tests." in target.read_text(encoding="utf-8")

    # A user edit must survive the next discovery pass.
    target.write_text(
        "---\nname: Edited\nenabled: true\n---\n\nuser edits win\n",
        encoding="utf-8",
    )
    discovery.register(discovery.detect(p))
    assert "user edits win" in target.read_text(encoding="utf-8")


def test_register_preserves_configured_description_when_manifest_omits_it(workdir):
    """Turning on sharing must not erase a description supplied at creation.

    ``shared_projects.apply_payload()`` saves ``perun.md`` with only the shared
    fields. Discovery treats that manifest as authoritative for declared fields,
    but an omitted description must not clear the registry value.
    """
    p = _mk(workdir / "described")
    info = discovery.detect(p)
    info["description"] = "User supplied description"
    proj, created = discovery.register(info)
    assert created is True
    assert proj["description"] == "User supplied description"

    # Enabling sharing writes a manifest that does not repeat the description.
    (p / "perun.md").write_text("---\nshared: true\n---\n", encoding="utf-8")
    proj2, created2 = discovery.register(discovery.detect(p))
    assert created2 is False
    assert proj2["description"] == "User supplied description"


def test_discovery_does_not_duplicate_a_hidden_project(workdir):
    """A hidden row is still registered.

    The path lookup used to be built from the *visible* list only, so the
    hidden control project looked new. Re-registering it either aborted the
    scan with a duplicate-path error or, when the resolved spelling differed,
    created a **visible duplicate** of the control project.
    """
    import os

    hidden_dir = _mk(workdir / "hidden-control", "opencode.json")
    visible_dir = _mk(workdir / "visible", "opencode.json")
    # A visible project makes the path lookup non-empty, which is what exposed
    # the bug: the hidden row was then missing from it.
    registry.add_project("Visible", str(visible_dir), kind="opencode")
    hidden = registry.add_project("HiddenControl", str(hidden_dir), kind="opencode")
    registry.update_project(hidden["slug"], hidden=1)

    discovery.discover_and_sync([str(workdir)], max_depth=1)

    def same_path(value):
        return os.path.normcase(os.path.realpath(os.path.abspath(value)))

    target = same_path(str(hidden_dir))
    matched = [p for p in registry.list_projects(include_hidden=True)
               if same_path(p["path"]) == target]
    assert len(matched) == 1, matched
    assert matched[0]["hidden"] is True
    # The control project must never reappear as a visible project.
    assert "hidden-control" not in {p["slug"] for p in registry.list_projects()}
    assert "visible" in {p["slug"] for p in registry.list_projects()}


def test_a_visible_control_duplicate_is_hidden_on_startup(monkeypatch):
    """Installs that already carry the old visible duplicate are repaired."""
    from perun import config

    updated = []
    duplicate = os.path.join(str(config.REPO_ROOT), ".")
    monkeypatch.setattr(discovery.registry, "list_project_paths",
                        lambda **kw: [
                            {"slug": "perun", "path": str(config.REPO_ROOT),
                             "hidden": True},
                            {"slug": "perun-dup", "path": duplicate,
                             "hidden": False},
                        ])
    monkeypatch.setattr(discovery.registry, "update_project",
                        lambda slug, **kw: updated.append((slug, kw)))

    discovery.hide_control_duplicates(
        {"slug": "perun", "path": str(config.REPO_ROOT)})

    assert updated == [("perun-dup", {"hidden": 1})]


def test_discovery_leaves_the_control_project_alone(control_repo):
    """The control project is curated by register_self(), not by a directory scan.

    Scanning its path must not rename it (directory name vs "Perun") or create
    a second row.
    """
    from perun import control

    control.ensure()
    before = registry.get_project_by_path(str(control_repo))
    assert before["name"] == "Perun" and before["hidden"] is True

    discovery.discover_and_sync([str(control_repo.parent)], max_depth=1)

    after = registry.get_project_by_path(str(control_repo))
    assert after["slug"] == before["slug"]
    assert after["name"] == "Perun"
    assert after["hidden"] is True
    target = os.path.normcase(os.path.realpath(str(control_repo)))
    matches = [p for p in registry.list_projects(include_hidden=True)
               if os.path.normcase(os.path.realpath(p["path"])) == target]
    assert len(matches) == 1, matches


def test_the_private_companion_repo_is_not_a_user_project(tmp_path, monkeypatch):
    from perun import config

    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "perun")
    (tmp_path / "perun").mkdir()
    internal = tmp_path / "perun_internal"
    internal.mkdir()
    monkeypatch.delenv("PERUN_INTERNAL_ROOT", raising=False)

    assert config.internal_root() == internal
    assert config.is_internal_path(str(internal)) is True
    assert config.is_internal_path(str(tmp_path / "perun")) is False

    monkeypatch.setenv("PERUN_INTERNAL_ROOT", str(tmp_path / "other"))
    assert config.is_internal_path(str(tmp_path / "other")) is True


def test_discovery_skips_the_internal_companion(control_repo, monkeypatch):
    """The owner-only companion repo must never be registered as a project."""
    internal = control_repo.parent / "perun_internal"
    _mk(internal, "opencode.json")
    monkeypatch.delenv("PERUN_INTERNAL_ROOT", raising=False)

    discovery.discover_and_sync([str(control_repo.parent)], max_depth=1)

    paths = {p["path"] for p in registry.list_projects(include_hidden=True)}
    assert str(internal) not in paths


def test_a_visible_internal_companion_is_hidden(monkeypatch, tmp_path):
    """Existing installs that registered it get a second Perun card removed."""
    from perun import config

    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "perun")
    (tmp_path / "perun").mkdir()
    internal = tmp_path / "perun_internal"
    internal.mkdir()
    monkeypatch.delenv("PERUN_INTERNAL_ROOT", raising=False)

    updated = []
    monkeypatch.setattr(discovery.registry, "list_project_paths",
                        lambda **kw: [{"slug": "perun-2",
                                       "path": os.path.join(str(internal), "."),
                                       "hidden": 0}])
    monkeypatch.setattr(discovery.registry, "update_project",
                        lambda slug, **kw: updated.append((slug, kw)))

    discovery.hide_internal_companion()

    assert updated == [("perun-2", {"hidden": 1})]


def test_the_internal_companion_is_never_listed(tmp_path, monkeypatch):
    """Hidden is not enough: the owner workspace lists hidden projects too."""
    from perun import config

    monkeypatch.setattr(config, "REPO_ROOT", tmp_path / "perun")
    (tmp_path / "perun").mkdir()
    internal = tmp_path / "perun_internal"
    internal.mkdir()
    monkeypatch.delenv("PERUN_INTERNAL_ROOT", raising=False)

    project = registry.add_project("Companion", str(internal), kind="opencode")
    listed = {p["slug"] for p in registry.list_projects(include_hidden=True)}
    assert project["slug"] not in listed

    registry.update_project(project["slug"], hidden=1)
    listed = {p["slug"] for p in registry.list_projects(include_hidden=True)}
    assert project["slug"] not in listed
    # Still addressable by slug; only the listing is suppressed.
    assert registry.get_project(project["slug"]) is not None
