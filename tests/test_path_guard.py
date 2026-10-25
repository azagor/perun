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
