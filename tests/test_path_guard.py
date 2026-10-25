import os

from perun import path_guard


def test_root_is_allowed(tmp_path):
    ok, reason = path_guard.check(str(tmp_path), str(tmp_path))
    assert ok
    assert reason == ""


def test_child_of_root_is_allowed(tmp_path):
    child = tmp_path / "src" / "api.py"
    child.parent.mkdir(parents=True)
    child.write_text("x")
    ok, reason = path_guard.check(str(tmp_path), str(child))
    assert ok
    assert reason == ""


def test_relative_target_resolves_against_project_root(tmp_path, monkeypatch):
    """A relative target must be rooted at the project, not at the cwd.

    Proposals from chat use relative paths such as "README.md"; resolving them
    with Path.resolve() straight away silently made every proposal target the
    Perun checkout instead of the shared project.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    ok, reason = path_guard.check(str(project), "README.md")
    assert ok, reason

    ok, reason = path_guard.check(str(project), "src/new_module.py")
    assert ok, reason


def test_relative_target_cannot_escape_project_root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    monkeypatch.chdir(project)

    ok, reason = path_guard.check(str(project), "../outside.txt")
    assert not ok
    assert "outside the project allowlist" in reason


def test_relative_external_dep_resolves_against_project_root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    dep = project / "shared_lib"
    dep.mkdir()
    (dep / "lib.py").write_text("x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    ok, reason = path_guard.check(
        str(project), str(dep / "lib.py"), [{"path": "shared_lib", "access": "rw"}]
    )
    assert ok, reason


def test_outside_root_is_blocked(tmp_path):
    outside = tmp_path.parent / "other" / "file.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("x")
    ok, reason = path_guard.check(str(tmp_path), str(outside))
    assert not ok
    assert "outside the project allowlist" in reason


def test_external_dep_is_allowed(tmp_path):
    dep = tmp_path / "shared_lib"
    dep.mkdir()
    (dep / "lib.py").write_text("x")
    deps = [{"name": "shared_lib", "path": str(dep), "access": "rw"}]
    ok, reason = path_guard.check(str(tmp_path), str(dep / "lib.py"), deps)
    assert ok
    assert reason == ""


def test_external_dep_read_only_blocks_write(tmp_path):
    dep = tmp_path.parent / f"shared_lib_ro_{tmp_path.name}"
    dep.mkdir()
    deps = [{"name": "shared_lib", "path": str(dep), "access": "ro"}]
    ok, reason = path_guard.check(
        str(tmp_path), str(dep / "lib.py"), deps, require_write=True
    )
    assert not ok
    assert "read-only" in reason


def test_validate_targets_returns_errors(tmp_path):
    outside = tmp_path.parent / "evil.py"
    outside.write_text("x")
    errors = path_guard.validate_targets(str(tmp_path), [str(tmp_path / "ok.py"), str(outside)])
    assert len(errors) == 1
    assert "outside the project allowlist" in errors[0]
