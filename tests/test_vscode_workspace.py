"""VS Code workspace generation, detection and launch helpers."""

from __future__ import annotations

import json
from pathlib import Path

from perun import config, vscode_workspace


def _project(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return {"slug": "demo", "name": "Demo", "path": str(root)}


def test_status_reports_the_reported_project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    path = vscode_workspace.heartbeat_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": "0.1.8", "trusted": False, "project_slug": "findme",
    }), encoding="utf-8")

    st = vscode_workspace.status()
    assert st["extension_trusted"] is False
    assert st["extension_project"] == "findme"


def test_status_reports_an_untrusted_companion(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    path = vscode_workspace.heartbeat_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": "0.1.7", "trusted": False,
        "last_seen": "2026-11-13T00:00:00+00:00"}), encoding="utf-8")

    st = vscode_workspace.status(linked_projects=1, clients=0)
    assert st["extension_installed"] is True
    assert st["extension_trusted"] is False
    assert st["extension_version"] == "0.1.7"


def test_status_reports_a_trusted_companion(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    path = vscode_workspace.heartbeat_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "version": "0.1.7", "trusted": True}), encoding="utf-8")

    assert vscode_workspace.status()["extension_trusted"] is True


def test_status_tolerates_an_old_heartbeat_without_trust(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    path = vscode_workspace.heartbeat_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": "0.1.4"}), encoding="utf-8")

    assert vscode_workspace.status()["extension_trusted"] is None


def test_generate_writes_a_managed_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    project = _project(tmp_path)

    path = Path(vscode_workspace.generate(project))
    data = json.loads(path.read_text(encoding="utf-8"))

    assert path.name == "demo.code-workspace"
    assert data["folders"] == [{"path": project["path"]}]
    assert data["settings"]["perun.projectSlug"] == "demo"
    assert data["settings"]["perun.managedWorkspace"] is True
    assert vscode_workspace.is_managed_workspace(str(path)) is True


def test_a_workspace_can_live_inside_the_project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    project = _project(tmp_path)

    path = Path(vscode_workspace.generate(project, workspace_in_project=True))

    assert path == Path(project["path"]) / "perun.code-workspace"
    assert path.is_file()


def test_remove_only_deletes_a_managed_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    project = _project(tmp_path)
    managed = Path(vscode_workspace.generate(project))
    user_file = tmp_path / "user.code-workspace"
    user_file.write_text("{}", encoding="utf-8")

    assert vscode_workspace.remove_workspace(str(managed)) is True
    assert not managed.exists()
    assert vscode_workspace.remove_workspace(str(user_file)) is False
    assert user_file.exists()


def test_an_in_project_workspace_is_excluded_and_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    project = _project(tmp_path)
    git_info = Path(project["path"]) / ".git" / "info"
    git_info.mkdir(parents=True)
    (git_info / "exclude").write_text("# git excludes\n", encoding="utf-8")

    workspace = Path(vscode_workspace.generate(project, workspace_in_project=True))
    assert "perun.code-workspace" in (git_info / "exclude").read_text(encoding="utf-8")

    vscode_workspace.remove_git_exclude(project["path"], "perun.code-workspace")
    assert "perun.code-workspace" not in (git_info / "exclude").read_text(encoding="utf-8")
    assert workspace.is_file()


def test_detect_and_status_read_a_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    heartbeat = vscode_workspace.heartbeat_path()
    heartbeat.parent.mkdir(parents=True)
    heartbeat.write_text(json.dumps({"version": "0.1.0", "last_seen": "now"}),
                         encoding="utf-8")
    monkeypatch.setattr(vscode_workspace, "detect",
                        lambda force=False: {"path": "code", "version": "1.0"})

    status = vscode_workspace.status(linked_projects=2, clients=1)

    assert status["detected"] is True
    assert status["extension_installed"] is True
    assert status["extension_version"] == "0.1.0"
    assert status["linked_projects"] == 2
    assert "\\vscode\\" in status["install_command"]


def test_opening_a_missing_workspace_is_a_clear_error(tmp_path):
    result = vscode_workspace.open_workspace(str(tmp_path / "missing.code-workspace"))
    assert result["ok"] is False
    assert "not found" in result["error"]
