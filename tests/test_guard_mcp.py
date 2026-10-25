import json
import os
from pathlib import Path

import pytest

from perun import guard_mcp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("PERUN_ALLOWLIST", raising=False)
    monkeypatch.delenv("PERUN_PROJECT_PATH", raising=False)


def _set_allowlist_env(tmp_path, deps=None):
    data = {"root": str(tmp_path), "deps": deps or []}
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


def test_read_only_dep_blocks_write(tmp_path, monkeypatch):
    dep = tmp_path.parent / "shared_ro"
    dep.mkdir()
    (dep / "lib.py").write_text("x")
    monkeypatch.chdir(tmp_path)
    _set_allowlist_env(tmp_path, deps=[{"path": str(dep), "access": "ro"}])
    with pytest.raises(ValueError, match="read-only"):
        guard_mcp.guard_write_file(str(dep / "lib.py"), "y")


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
