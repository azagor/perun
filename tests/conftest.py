"""Pytest configuration — isolate Perun state for every test session.

``perun.config`` resolves ``HOME``/``DB_PATH`` at import time, so the env var
must be set **before** any ``perun`` import. ``conftest.py`` is imported by
pytest before test modules are collected, which gives us the right hook.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_TEST_HOME = Path(tempfile.mkdtemp(prefix="perun-test-"))
os.environ["PERUN_HOME"] = str(_TEST_HOME)
os.environ["PERUN_ROOTS"] = str(_TEST_HOME)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    """Recreate the schema and wipe all rows before each test."""
    from perun import db

    db.init_db()
    with db.db() as conn:
        for table in ("bridge_links", "activity", "doc_chunks", "memory",
                      "runs", "sessions", "projects"):
            conn.execute(f"DELETE FROM {table}")
    yield


@pytest.fixture()
def workdir():
    """A throwaway directory tree for discovery tests."""
    d = Path(tempfile.mkdtemp(prefix="perun-workdir-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def control_repo(monkeypatch, tmp_path):
    """Point Perun's control plane (the orchestrator project) at a throwaway repo.

    The control plane is identified by its path being ``config.REPO_ROOT``. Tests
    that exercise orchestrator routing redirect it to an empty temp repo so the
    real Perun checkout is never registered, indexed, or executed against.
    """
    from perun import config

    repo = tmp_path / "perun-repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("# Perun control-plane test repo\n", encoding="utf-8")
    (repo / "docs" / "CODEMAP.md").write_text("# test codemap\n", encoding="utf-8")
    monkeypatch.setattr(config, "REPO_ROOT", repo)
    return repo


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
