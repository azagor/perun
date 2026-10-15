"""Tests for perun.process_tracker."""

import os
import subprocess
import sys
import time

import pytest

from perun import process_tracker, registry


@pytest.fixture
def fresh_tracker(monkeypatch):
    """Give each test an isolated ProcessTracker instance."""
    tracker = process_tracker.ProcessTracker()
    monkeypatch.setattr(process_tracker, "TRACKER", tracker)
    return tracker


def _sleep_proc(**env):
    """Start a long-running Python process that we can safely terminate."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if os.name == "nt"
        else 0,
        env={**os.environ, **env},
    )


def test_register_and_is_alive(fresh_tracker):
    proc = _sleep_proc()
    fresh_tracker.register("run-1", proc)
    assert fresh_tracker.is_alive("run-1") is True
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_resolve_by_marker(fresh_tracker):
    # If the Popen handle has exited but a child/marked process still carries
    # the run_uid in its environment, is_alive should find it.
    proc = _sleep_proc(PERUN_RUN_UID="marked-run")
    fresh_tracker.register("marked-run", proc)
    # Simulate the launcher having already exited by deleting the stored proc.
    fresh_tracker._entries["marked-run"]["proc"] = None
    assert fresh_tracker.is_alive("marked-run") is True
    fresh_tracker.terminate("marked-run")
    assert fresh_tracker.is_alive("marked-run") is False


def test_terminate_finalizes_run(tmp_path, fresh_tracker, monkeypatch):
    # Use an isolated registry/DB so the run record can be created and updated.
    from perun import config, db

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    # Create a minimal project + run record.
    project = registry.add_project(name="Test Project", path=str(tmp_path))
    registry.create_run("tui-1", project["id"], "hello")
    registry.update_run("tui-1", status="running", started_at=registry.now_iso())

    proc = _sleep_proc()
    fresh_tracker.register("tui-1", proc)
    assert fresh_tracker.is_alive("tui-1") is True

    fresh_tracker.terminate("tui-1")

    assert fresh_tracker.is_alive("tui-1") is False
    updated = registry.get_run("tui-1")
    assert updated["status"] == "done"
    assert updated["ended_at"]


def test_watcher_auto_finalizes(tmp_path, fresh_tracker, monkeypatch):
    from perun import config, db

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    project = registry.add_project(name="Watcher Project", path=str(tmp_path))
    registry.create_run("tui-2", project["id"], "hello")
    registry.update_run("tui-2", status="running", started_at=registry.now_iso())

    proc = _sleep_proc()
    fresh_tracker.register("tui-2", proc)
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()

    # The watcher runs every 3s; give it enough time to detect the dead process.
    time.sleep(5)

    updated = registry.get_run("tui-2")
    assert updated["status"] == "done"
