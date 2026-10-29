import json
import os
from pathlib import Path

import pytest

from perun import guard_mcp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PERUN_ALLOWLIST", raising=False)
    monkeypatch.delenv("PERUN_PROJECT_PATH", raising=False)


def _set_allowlist_env(tmp_path, deps=None, targets=None):
    data = {"root": str(tmp_path), "deps": deps or []}
    if targets is not None:
        data["targets"] = [str(t) for t in targets]
    os.environ["PERUN_ALLOWLIST"] = json.dumps(data)


def test_guard_read_file_inside_allowlist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_allowlist_env(tmp_path)
    (tmp_path / "hello.txt").write_text("world")
    assert guard_mcp.guard_read_file("hello.txt") == "world"


def test_guard_read_file_outside_allowlist(tmp_path, monkeypatch):
    outside = tmp_path.parent / "forbidden.txt"
    outside.write_text("secret")
    monkeypatch.chdir(tmp_path)
    _set_allowlist_env(tmp_path)
    with pytest.raises(ValueError, match="outside the project allowlist"):
        guard_mcp.guard_read_file(str(outside))


def test_guard_write_file_creates_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_allowlist_env(tmp_path)
    result = guard_mcp.guard_write_file("new.txt", "content")
    assert "wrote" in result
    assert (tmp_path / "new.txt").read_text() == "content"


def test_guard_write_file_blocks_outside(tmp_path, monkeypatch):
    outside_dir = tmp_path.parent / "out"
    outside_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    _set_allowlist_env(tmp_path)
    with pytest.raises(ValueError, match="outside the project allowlist"):
        guard_mcp.guard_write_file(str(outside_dir / "x.txt"), "content")


def test_guard_scopes_writes_to_executing_proposal_targets(tmp_path, monkeypatch):
    """With standalone=False the guard recovers targets from the DB."""
    from perun import action_proposals, manifest, registry

    project_path = tmp_path / "guard-exec"
    project_path.mkdir()
    project = registry.add_project("guard-exec", str(project_path))
    data = manifest.from_project(project)
    data["shared"] = True
    data["participants"] = [{"agent_id": "frambo", "roles": ["rw"]}]
    manifest.save(str(project_path), data, body="# p")
    proposal = action_proposals.create(
        project, "frambo",
        {"intent": "x", "targets": ["allowed.txt"], "action_type": "edit"},
    )
    action_proposals.approve(proposal["id"], "reviewer")
    action_proposals._set_status(proposal["id"], "executing")

    monkeypatch.chdir(project_path)
    monkeypatch.setenv("PERUN_PROJECT_PATH", str(project_path))
    guard_mcp.guard_write_file("allowed.txt", "ok")
    assert (project_path / "allowed.txt").read_text(encoding="utf-8") == "ok"
    with pytest.raises(ValueError, match="approved target paths"):
        guard_mcp.guard_write_file("outside.txt", "no")
    assert not (project_path / "outside.txt").exists()


def test_guard_edit_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_allowlist_env(tmp_path)
    (tmp_path / "f.txt").write_text("old text")
    result = guard_mcp.guard_edit_file("f.txt", "old", "new")
    assert "edited" in result
    assert (tmp_path / "f.txt").read_text() == "new text"


def test_guard_list_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_allowlist_env(tmp_path)
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    entries = guard_mcp.guard_list_directory(".")
    names = {e["name"] for e in entries}
    assert "a.txt" in names
    assert "sub" in names


def test_guard_search_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_allowlist_env(tmp_path)
    (tmp_path / "foo.py").write_text("x")
    (tmp_path / "bar.txt").write_text("x")
    matches = guard_mcp.guard_search_files(".", "foo")
    assert len(matches) == 1
    assert matches[0]["name"] == "foo.py"


def test_sensitive_paths_are_denied_even_inside_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_allowlist_env(tmp_path)
    (tmp_path / ".env").write_text("TOKEN=secret")
    (tmp_path / "perun.md").write_text("---\nshared: true\n---")
    with pytest.raises(ValueError, match="protected or sensitive"):
        guard_mcp.guard_read_file(".env")
    with pytest.raises(ValueError, match="protected or sensitive"):
        guard_mcp.guard_write_file("perun.md", "tampered")
    with pytest.raises(ValueError, match="protected or sensitive"):
        guard_mcp.guard_write_file(".git/config", "tampered")


def test_guard_list_and_search_hide_sensitive_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _set_allowlist_env(tmp_path)
    (tmp_path / ".env").write_text("TOKEN=secret")
    (tmp_path / "visible.py").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("x")

    names = {e["name"] for e in guard_mcp.guard_list_directory(".")}
    assert "visible.py" in names
    assert ".env" not in names
    assert ".git" not in names
    assert "node_modules" not in names

    matches = guard_mcp.guard_search_files(".", "config")
    assert matches == []


def test_write_target_scope_is_enforced_but_reads_are_not(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "api.py"
    other = tmp_path / "other.py"
    _set_allowlist_env(tmp_path, targets=[target])
    other.write_text("readable")

    guard_mcp.guard_write_file("api.py", "new")
    assert target.read_text() == "new"
    assert guard_mcp.guard_read_file("other.py") == "readable"

    with pytest.raises(ValueError, match="outside the approved target paths"):
        guard_mcp.guard_write_file("other.py", "nope")


def test_read_only_dep_blocks_write(tmp_path, monkeypatch):
    dep = tmp_path.parent / "shared_ro"
    dep.mkdir()
    (dep / "lib.py").write_text("x")
    monkeypatch.chdir(tmp_path)
    _set_allowlist_env(tmp_path, deps=[{"path": str(dep), "access": "ro"}])
    with pytest.raises(ValueError, match="read-only"):
        guard_mcp.guard_write_file(str(dep / "lib.py"), "y")


def test_malformed_allowlist_falls_back_to_cwd_pass_through(tmp_path, monkeypatch):
    """A malformed PERUN_ALLOWLIST disables project-bound enforcement and
    falls back to the current working directory as the allowlist root."""
    from perun import manifest, registry

    project = registry.add_project("guard-malformed", str(tmp_path))
    data = manifest.from_project(project)
    data["shared"] = True
    data["participants"] = [{"agent_id": "frambo"}]
    manifest.save(tmp_path, data, body="# guard-malformed")

    os.environ["PERUN_ALLOWLIST"] = "not valid json {"
    os.environ["PERUN_PROJECT_PATH"] = str(tmp_path)
    monkeypatch.chdir(tmp_path)

    # Writes inside the working directory are allowed (fallback pass-through).
    guard_mcp.guard_write_file("inside.txt", "x")
    assert (tmp_path / "inside.txt").read_text() == "x"

    # Writes outside the working directory are still denied.
    outside = tmp_path.parent / "escaped.txt"
    with pytest.raises(ValueError, match="outside the project allowlist"):
        guard_mcp.guard_write_file(str(outside), "x")


def test_shared_project_derives_allowlist(tmp_path, monkeypatch):
    """When PERUN_ALLOWLIST is absent, a shared project manifest drives policy."""
    from perun import manifest, registry

    project = registry.add_project("guard-shared", str(tmp_path))
    data = manifest.from_project(project)
    data["shared"] = True
    data["participants"] = [{"agent_id": "frambo"}]
    manifest.save(tmp_path, data, body="# guard-shared")

    os.environ["PERUN_PROJECT_PATH"] = str(tmp_path)
    (tmp_path / "in.txt").write_text("ok")
    monkeypatch.chdir(tmp_path)
    assert guard_mcp.guard_read_file("in.txt") == "ok"

    outside = tmp_path.parent / "out.txt"
    outside.write_text("bad")
    with pytest.raises(ValueError, match="outside the project allowlist"):
        guard_mcp.guard_read_file(str(outside))
