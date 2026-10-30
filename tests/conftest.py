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
                      "runs", "sessions", "projects", "context_docs",
                      "secret_store", "project_chats", "action_proposals",
                      "pending_room_deletions", "hitl_nonces", "project_dependencies",
                      "package_transfers", "attention_requests",
                      "attention_deliveries", "attention_outbox", "attention_mutes"):
            conn.execute(f"DELETE FROM {table}")
    yield


@pytest.fixture(autouse=True)
def _roots_include_temp(monkeypatch):
    """Make the search path-gate portable across test trees.

    ``PERUN_ROOTS`` points at the isolated test home, but pytest ``tmp_path``
    and ``workdir`` trees live under the system temp dir — treat the system
    temp dir and the user home as configured roots for each test so single-
    project search tests behave the same on Windows and POSIX. Tests that
    patch ``perun.config.settings`` themselves override this wrapper
    (monkeypatch unwinds in reverse order).
    """
    import dataclasses

    import perun.config as cfg

    real = cfg.settings
    extra = (tempfile.gettempdir(), str(Path.home()))

    def patched():
        s = real()
        add = [e for e in extra if e not in s.roots]
        return dataclasses.replace(s, roots=[*s.roots, *add]) if add else s

    monkeypatch.setattr(cfg, "settings", patched)


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


@pytest.fixture(autouse=True)
def _api_token_header(monkeypatch):
    """Send the local API token on every ``TestClient`` request.

    The server requires ``X-Perun-Token`` for state-changing ``/api/*`` calls
    (the dashboard gets it injected into ``index.html``). Tests create their own
    ``TestClient`` instances, so the header default is applied at the class level
    instead of at ~20 call sites.
    """
    from starlette.testclient import TestClient

    from perun import config

    token = config.api_token()
    original_init = TestClient.__init__

    def patched_init(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        headers = dict(kwargs.get("headers") or {})
        headers.setdefault("X-Perun-Token", token)
        kwargs["headers"] = headers
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", patched_init)
    yield token


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TEST_HOME, ignore_errors=True)

