from pathlib import Path

from perun import fs_audit, manifest, registry


def _make_shared_project(tmp_path, name="fs-audit"):
    (tmp_path / name).mkdir(parents=True)
    project = registry.add_project(name, str(tmp_path / name))
    data = manifest.from_project(project)
    data["shared"] = True
    data["participants"] = [{"agent_id": "frambo"}]
    manifest.save(tmp_path / name, data, body=f"# {name}")
    return project


def test_snapshot_captures_file_metadata(tmp_path):
    project = _make_shared_project(tmp_path)
    (Path(project["path"]) / "a.txt").write_text("a")
    snap = fs_audit.snapshot(project)
    assert snap["project_slug"] == project["slug"]
    assert str(Path(project["path"]) / "a.txt") in snap["files"]


def test_diff_reports_created_modified_deleted(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "keep.txt").write_text("keep")
    (root / "gone.txt").write_text("gone")
    before = fs_audit.snapshot(project)

    (root / "new.txt").write_text("new")
    (root / "keep.txt").write_text("changed")
    (root / "gone.txt").unlink()

    result = fs_audit.diff(project, before)
    assert str(root / "new.txt") in result["created"]
    assert str(root / "gone.txt") in result["deleted"]
    assert str(root / "keep.txt") in result["modified"]


def test_diff_reports_escape_next_to_project_root(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    before = fs_audit.snapshot(project)
    (root.parent / "escape.txt").write_text("escaped")

    result = fs_audit.diff(project, before)
    assert str(root.parent / "escape.txt") in result["escaped"]


def test_diff_empty_when_nothing_changes(tmp_path):
    project = _make_shared_project(tmp_path)
    before = fs_audit.snapshot(project)
    result = fs_audit.diff(project, before)
    assert result["created"] == []
    assert result["deleted"] == []
    assert result["modified"] == []
    assert result["escaped"] == []
    assert result["violations"] == []


def test_log_result_creates_violation_activity_for_escape(tmp_path):
    project = _make_shared_project(tmp_path)
    before = fs_audit.snapshot(project)
    (Path(project["path"]).parent / "escape.txt").write_text("escaped")
    result = fs_audit.diff(project, before)

    fs_audit.log_result(project["id"], "run-1", result)
    activity = registry.list_activity(project["id"])
    kinds = {a["kind"] for a in activity}
    assert "fs_audit_violation" in kinds


def test_log_result_creates_ok_activity(tmp_path):
    project = _make_shared_project(tmp_path)
    before = fs_audit.snapshot(project)
    result = fs_audit.diff(project, before)

    fs_audit.log_result(project["id"], "run-2", result)
    activity = registry.list_activity(project["id"])
    kinds = {a["kind"] for a in activity}
    assert "fs_audit_ok" in kinds
