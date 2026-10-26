"""Project scaffolding tests."""

from __future__ import annotations

import pytest

import json

from perun import scaffold, manifest


def test_create_dir_is_idempotent(tmp_path):
    target = tmp_path / "new"
    path, created = scaffold.create_dir(target)
    assert created is True
    assert path.is_dir()
    _, created2 = scaffold.create_dir(target)
    assert created2 is False


def test_create_dir_rejects_a_file(tmp_path):
    f = tmp_path / "x"
    f.write_text("y", encoding="utf-8")
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.create_dir(f)


def test_bootstrap_writes_skeleton_and_never_overwrites(tmp_path):
    p, _ = scaffold.create_dir(tmp_path / "proj")
    created = scaffold.bootstrap(p, "Proj")
    assert "opencode.json" in created
    assert "AGENTS.md" in created
    assert "perun.md" in created
    assert "docs/CHANGELOG.md" in created
    assert "docs/TECHNICAL_DOCUMENTATION.md" in created
    assert "docs/CODEMAP.md" in created
    assert ".gitignore" in created
    assert (p / ".opencode").is_dir()

    agents = (p / "AGENTS.md").read_text(encoding="utf-8")
    assert "Proj" in agents
    assert "perun.md" in agents
    assert "primary source of truth" in agents
    assert "docs/CHANGELOG.md" in agents

    perun = (p / "perun.md").read_text(encoding="utf-8")
    assert "docs/CODEMAP.md" in perun
    assert "docs/CHANGELOG.md" in perun

    # A second bootstrap must not clobber an existing README.
    (p / "README.md").write_text("mine", encoding="utf-8")
    created2 = scaffold.bootstrap(p, "Proj")
    assert "README.md" not in created2
    assert (p / "README.md").read_text(encoding="utf-8") == "mine"


def test_summary_reports_state(tmp_path):
    s = scaffold.summary(tmp_path)
    assert s["exists"] is True
    assert s["is_empty"] is True
    (tmp_path / "AGENTS.md").write_text("# x", encoding="utf-8")
    s2 = scaffold.summary(tmp_path)
    assert s2["agents_md"] is True
    assert s2["is_empty"] is False


def test_summary_missing_path(tmp_path):
    s = scaffold.summary(tmp_path / "nope")
    assert s["exists"] is False


def test_detect_anchors_finds_dependency_files(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pytest", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python", encoding="utf-8")
    anchors = scaffold.detect_anchors(tmp_path)
    labels = {a["label"] for a in anchors}
    assert "package.json" in labels
    assert "requirements.txt" in labels
    assert "Dockerfile" in labels


def test_backfill_only_creates_missing_files(tmp_path):
    p, _ = scaffold.create_dir(tmp_path / "existing")
    (p / "README.md").write_text("keep me", encoding="utf-8")
    (p / "perun.md").write_text("---\nversion: 1\n---\n", encoding="utf-8")
    created = scaffold.backfill(p, "Existing")
    assert "README.md" not in created
    assert "perun.md" not in created
    assert "AGENTS.md" in created
    assert "docs/CHANGELOG.md" in created
    assert (p / "README.md").read_text(encoding="utf-8") == "keep me"


def test_backfill_is_safe_on_empty_directory(tmp_path):
    p, _ = scaffold.create_dir(tmp_path / "empty")
    created = scaffold.backfill(p, "Empty")
    assert "AGENTS.md" in created
    assert "perun.md" in created
    assert "docs/CHANGELOG.md" in created
    assert "docs/TECHNICAL_DOCUMENTATION.md" in created
    assert "docs/CODEMAP.md" in created


def test_update_manifest_anchors_appends_new_dependency_anchors(tmp_path):
    p, _ = scaffold.create_dir(tmp_path / "proj")
    scaffold.bootstrap(p, "Proj")
    (p / "Dockerfile").write_text("FROM python:3.11", encoding="utf-8")
    result = scaffold.update_manifest_anchors(p)
    assert result["updated"] is True
    assert result["added"] == 1
    text = (p / "perun.md").read_text(encoding="utf-8")
    assert "Dockerfile" in text

    # A second call is idempotent.
    result2 = scaffold.update_manifest_anchors(p)
    assert result2["updated"] is False


def test_parse_dependencies_reads_common_files(tmp_path):
    p, _ = scaffold.create_dir(tmp_path / "proj")
    (p / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^18.0.0"}}),
        encoding="utf-8",
    )
    (p / "requirements.txt").write_text("pytest>=7.0\n", encoding="utf-8")
    deps = scaffold.parse_dependencies(p)
    assert "package.json" in deps
    assert any(name == "react" for name, _ in deps["package.json"])
    assert "requirements.txt" in deps
    assert any(name == "pytest" for name, _ in deps["requirements.txt"])


def test_validate_manifest_reports_missing_docs_and_anchors(tmp_path):
    p, _ = scaffold.create_dir(tmp_path / "proj")
    scaffold.bootstrap(p, "Proj")
    (p / "docs" / "CHANGELOG.md").unlink()
    result = scaffold.validate_manifest(p)
    assert result["ok"] is False
    assert any("CHANGELOG" in m for m in result["missing_docs"])


def test_find_local_project_references_detects_file_dependency(tmp_path):
    p, _ = scaffold.create_dir(tmp_path / "consumer")
    other, _ = scaffold.create_dir(tmp_path / "other")
    (p / "package.json").write_text(
        json.dumps({"dependencies": {"other": f"file:../other"}}),
        encoding="utf-8",
    )
    projects = [
        {"slug": "other", "name": "Other", "path": str(other)},
    ]
    refs = scaffold.find_local_project_references(p, projects)
    assert len(refs) == 1
    assert refs[0]["target_slug"] == "other"


def test_enrich_manifest_fills_a_minimal_manifest(tmp_path):
    """A shared-project bootstrap manifest has no docs/commands/anchors."""
    p, _ = scaffold.create_dir(tmp_path / "shared")
    # The minimal shape written by the shared-project payload path.
    (p / "perun.md").write_text(
        "---\n"
        "shared: true\n"
        "participants:\n"
        "- agent_id: frambo\n"
        "  roles:\n"
        "  - rw\n"
        "auto_approve: true\n"
        "docs: {}\n"
        "commands: {}\n"
        "anchors: []\n"
        "---\n",
        encoding="utf-8",
    )
    for rel in ("README.md", "AGENTS.md", "docs/CHANGELOG.md",
                "docs/TECHNICAL_DOCUMENTATION.md", "docs/CODEMAP.md"):
        (p / rel).parent.mkdir(parents=True, exist_ok=True)
        (p / rel).write_text(f"# {rel}\n", encoding="utf-8")

    result = scaffold.enrich_manifest(p, "Shared")

    assert result["updated"] is True
    snap = manifest.load(p)
    data = snap["data"]
    assert data["shared"] is True
    assert data["participants"] == [{"agent_id": "frambo", "roles": ["rw"]}]
    assert data["auto_approve"] is True
    assert data["docs"]["readme"] == "README.md"
    assert data["docs"]["codemap"] == "docs/CODEMAP.md"
    assert data["commands"].get("git-status") == "git status"
    paths = {a["path"] for a in data["anchors"]}
    assert {"README.md", "AGENTS.md", "docs/CHANGELOG.md"} <= paths

    # Idempotent: a second run changes nothing.
    assert scaffold.enrich_manifest(p, "Shared")["updated"] is False


def test_enrich_manifest_preserves_existing_values(tmp_path):
    p, _ = scaffold.create_dir(tmp_path / "proj")
    scaffold.bootstrap(p, "Proj")
    snap = manifest.load(p)
    data = snap["data"]
    data["docs"] = {"readme": "docs/OVERVIEW.md"}
    data["commands"] = {"test": "make test"}
    data["anchors"] = [{"label": "Custom", "kind": "file", "path": "CUSTOM.md"}]
    manifest.save(p, data, body=snap["body"])

    scaffold.enrich_manifest(p, "Proj")

    data = manifest.load(p)["data"]
    assert data["docs"]["readme"] == "docs/OVERVIEW.md"  # not overwritten
    assert data["docs"]["agents"] == "AGENTS.md"          # missing key filled
    assert data["commands"]["test"] == "make test"
    assert any(a["path"] == "CUSTOM.md" for a in data["anchors"])
    assert any(a["path"] == "README.md" for a in data["anchors"])


def test_backfill_enriches_an_existing_minimal_manifest(tmp_path):
    p, _ = scaffold.create_dir(tmp_path / "existing")
    (p / "perun.md").write_text(
        "---\nshared: true\ndocs: {}\ncommands: {}\nanchors: []\n---\n",
        encoding="utf-8",
    )

    created = scaffold.backfill(p, "Existing")

    assert "perun.md" not in created
    assert "perun.md (enriched)" in created
    data = manifest.load(p)["data"]
    assert data["docs"]["readme"] == "README.md"
    assert data["anchors"]


def test_create_new_project_bootstraps_an_existing_empty_directory(tmp_path):
    """Regression for ai_playground: only opencode.json + a bare perun.md."""
    from perun import projects

    target = tmp_path / "already-made"
    target.mkdir()

    result = projects.create(
        method="new",
        name="Already Made",
        path=str(target),
        bootstrap=True,
    )

    assert result["project"]["path"] == str(target)
    for rel in ("README.md", "AGENTS.md", "docs/CHANGELOG.md",
                "docs/TECHNICAL_DOCUMENTATION.md", "docs/CODEMAP.md"):
        assert (target / rel).is_file(), rel
    data = manifest.load(target)["data"]
    assert data["docs"]["readme"] == "README.md"
    assert data["anchors"]

