"""Tests for perun.project_roots safe cross-project path resolver."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from perun import project_roots


@pytest.fixture(autouse=True)
def isolate_user_roots(tmp_path, monkeypatch):
    """Point project-roots.json to a temp file so tests do not touch PERUN_HOME."""
    monkeypatch.setattr(project_roots, "_roots_file", lambda: tmp_path / "project-roots.json")
    # Clear any env overrides for names we test.
    for name in ("perun_test", "perun_chat", "perun"):
        monkeypatch.delenv(f"PERUN_PROJECT_{name.upper()}", raising=False)


def test_resolve_default_root():
    path = project_roots.resolve("perun", "AGENTS.md")
    assert path == Path(r"C:\my_apps\perun\AGENTS.md").resolve()


def test_resolve_cross_project():
    path = project_roots.resolve("perun_chat", "perunchat/api.py")
    assert path == Path(r"C:\my_apps\perun_chat\perunchat\api.py").resolve()


def test_resolve_rejects_escape():
    with pytest.raises(ValueError):
        project_roots.resolve("perun", "../../../etc/passwd")


def test_add_and_remove_user_root(tmp_path):
    project_roots.add_root("perun_test", str(tmp_path))
    assert project_roots.get_root("perun_test") == tmp_path.resolve()
    project_roots.remove_root("perun_test")
    with pytest.raises(KeyError):
        project_roots.get_root("perun_test")


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("PERUN_PROJECT_PERUN_CHAT", str(tmp_path))
    assert project_roots.get_root("perun_chat") == tmp_path.resolve()


def test_auto_register_new_root(tmp_path):
    new_project = tmp_path / "new-project"
    new_project.mkdir()
    assert project_roots.auto_register("new-project", str(new_project)) is True
    assert project_roots.get_root("new-project") == new_project.resolve()


def test_auto_register_inside_existing_root_is_noop(tmp_path):
    # perun_chat default root contains anything under it.
    nested = Path(r"C:\my_apps\perun_chat") / "nested"
    nested.mkdir(exist_ok=True)
    assert project_roots.auto_register("nested", str(nested)) is False
