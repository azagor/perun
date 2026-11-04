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


def test_a_stop_after_a_restart_refuses_an_unrelated_pid(fresh_tracker):
    """Pids are recycled; only a process carrying our marker may be killed."""
    project = registry.add_project("pt-guard", r"C:\pt-guard")
    registry.create_run("pt-guard-run", project["id"], "hi", standalone=True)
    proc = _sleep_proc()  # alive, but not one of ours
    registry.update_run("pt-guard-run", pid=proc.pid, status="running")
    try:
        assert fresh_tracker.terminate("pt-guard-run") is False
        assert proc.poll() is None
        assert registry.get_run("pt-guard-run")["status"] == "done"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_a_stop_after_a_restart_kills_a_marked_process(fresh_tracker):
    project = registry.add_project("pt-marked", r"C:\pt-marked")
    registry.create_run("pt-marked-run", project["id"], "hi", standalone=True)
    proc = _sleep_proc(PERUN_RUN_UID="pt-marked-run")
    registry.update_run("pt-marked-run", pid=proc.pid, status="running")
    try:
        assert fresh_tracker.terminate("pt-marked-run") is True
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()


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
import os
import subprocess
import sys
import time

import pytest

from perun import registry
from perun.process_tracker import ProcessTracker


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "tui-track").mkdir()
    return registry.add_project("tui-track", str(tmp_path / "tui-track"), kind="dir")


def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def test_register_persists_the_terminal_pid(project):
    proc = _sleeper()
    try:
        tracker = ProcessTracker()
        run = registry.create_run("run_tui_track", project["id"], "TUI", standalone=True)
        tracker.register(run["run_uid"], proc)
        assert registry.get_run(run["run_uid"])["pid"] == proc.pid
    finally:
        proc.kill()


def test_is_alive_falls_back_to_the_persisted_pid_after_a_restart(project):
    proc = _sleeper()
    try:
        run = registry.create_run("run_tui_alive", project["id"], "TUI", standalone=True)
        registry.update_run(run["run_uid"], status="running", pid=proc.pid)
        restarted = ProcessTracker()          # empty in-memory map
        assert restarted.is_alive(run["run_uid"]) is True

        registry.update_run(run["run_uid"], pid=999999)
        assert restarted.is_alive(run["run_uid"]) is False
    finally:
        proc.kill()


def test_terminate_without_a_tracked_entry_refuses_a_dead_pid(project):
    run = registry.create_run("run_tui_dead", project["id"], "TUI", standalone=True)
    registry.update_run(run["run_uid"], status="running", pid=999999)
    assert ProcessTracker().terminate(run["run_uid"]) is False


def test_a_live_stop_only_kills_a_marker_resolved_pid(fresh_tracker, monkeypatch):
    """The launch-time Windows Terminal guess is never a kill target.

    The heuristic matches "a terminal that appeared around launch time"; killing
    that window could close tabs belonging to other runs. A live Stop must use
    the marker (or our own Popen handle) instead.
    """
    proc = _sleep_proc()
    fresh_tracker.register("run-heuristic", proc)
    seen = {}

    def fake_resolver(entry, *, heuristic=True):
        seen["heuristic"] = heuristic
        return None

    monkeypatch.setattr(fresh_tracker, "_resolve_child_pid", fake_resolver)
    try:
        # No marker pid, but the Popen handle is ours - still stoppable.
        assert fresh_tracker.terminate("run-heuristic") is True
        assert seen["heuristic"] is False
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_a_live_entry_without_an_attributable_pid_is_only_finalized(project, fresh_tracker):
    """An unrelated live pid is never killed; the run row is still closed."""
    other = _sleeper()
    run = registry.create_run("run_tui_unattributed", project["id"], "TUI",
                              standalone=True)
    uid = run["run_uid"]
    registry.update_run(uid, status="running", pid=other.pid)
    fresh_tracker._entries[uid] = {
        "proc": None, "pid": other.pid, "shell": "wt", "run_uid": uid,
        "started_at": time.time(),
    }
    try:
        assert fresh_tracker.terminate(uid) is False
        assert other.poll() is None, "an unattributable pid must survive a Stop"
        assert registry.get_run(uid)["status"] == "done"
    finally:
        other.kill()
        other.wait(timeout=5)


def _shell_with_marked_child(uid: str):
    """Start a shell carrying the run marker, with a long-lived child under it.

    This is the shape Stop has to handle: ``cmd /k`` (or ``pwsh -NoExit``) stays
    alive after the agent exits, so closing the terminal means killing the shell,
    not the child.
    """
    code = "import time; time.sleep(30)"
    if os.name == "nt":
        # A single command-line string: cmd.exe does not understand the \" quoting
        # that list2cmdline adds around an embedded quoted executable.
        return subprocess.Popen(
            f'cmd.exe /c set "PERUN_RUN_UID={uid}" && "{sys.executable}" -c "{code}"'
        )
    return subprocess.Popen(
        ["/bin/sh", "-c", f'PERUN_RUN_UID={uid} "{sys.executable}" -c "{code}" ; :']
    )


def _first_child_pid(shell, timeout: float = 10.0):
    import psutil

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            kids = psutil.Process(shell.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            return None
        if kids:
            return kids[0].pid
        time.sleep(0.2)
    return None


def _kill_tree_now(pid: int) -> None:
    import psutil

    try:
        procs = psutil.Process(pid).children(recursive=True) + [psutil.Process(pid)]
    except psutil.NoSuchProcess:
        return
    for proc in procs:
        try:
            proc.kill()
        except Exception:
            pass


def test_terminal_owner_walks_up_to_the_marked_shell(fresh_tracker):
    uid = "owner-run-1"
    shell = _shell_with_marked_child(uid)
    try:
        child = _first_child_pid(shell)
        assert child, "the shell did not spawn its child"
        assert fresh_tracker._terminal_owner(child, uid) == shell.pid
    finally:
        _kill_tree_now(shell.pid)


def test_stop_targets_the_shell_not_the_agent(fresh_tracker):
    uid = "shell-target-1"
    shell = _shell_with_marked_child(uid)
    try:
        child = _first_child_pid(shell)
        assert child, "the shell did not spawn its child"
        entry = {
            "proc": shell, "pid": shell.pid,
            "shell": "cmd" if os.name == "nt" else "sh",
            "run_uid": uid, "started_at": time.time(),
        }
        assert fresh_tracker._kill_pid(entry) == shell.pid
        assert fresh_tracker._kill_pid(entry) != child
    finally:
        _kill_tree_now(shell.pid)


def test_a_stop_after_a_restart_closes_the_shell_hosting_the_agent(project):
    """The persisted pid is the agent; the shell around it owns the window."""
    uid = "restart-shell-run"
    shell = _shell_with_marked_child(uid)
    try:
        child = _first_child_pid(shell)
        assert child, "the shell did not spawn its child"
        registry.create_run(uid, project["id"], "TUI", standalone=True)
        registry.update_run(uid, status="running", pid=child)

        restarted = ProcessTracker()  # empty in-memory map: the server restarted
        assert restarted.terminate(uid) is True
        shell.wait(timeout=10)
        assert registry.get_run(uid)["status"] == "done"
    finally:
        _kill_tree_now(shell.pid)


# --------------------------------------------------------------------------- #
# Focus: resolve the window's shell pid without ever killing anything
# --------------------------------------------------------------------------- #
def test_run_terminal_pid_resolves_the_shell_and_never_kills(fresh_tracker, monkeypatch):
    fresh_tracker._entries["focus-live"] = {
        "run_uid": "focus-live", "proc": None, "pid": 111,
    }
    monkeypatch.setattr(fresh_tracker, "_kill_pid", lambda entry: 4242)
    monkeypatch.setattr(fresh_tracker, "_belongs_to_run", lambda pid, uid: True)
    killed = []
    monkeypatch.setattr(fresh_tracker, "_kill_tree", lambda pid: killed.append(pid))

    assert fresh_tracker.run_terminal_pid("focus-live") == 4242
    assert killed == []  # Focus is read-only


def test_run_terminal_pid_refuses_an_unmarked_pid(fresh_tracker, monkeypatch):
    fresh_tracker._entries["focus-bad"] = {
        "run_uid": "focus-bad", "proc": None, "pid": 111,
    }
    monkeypatch.setattr(fresh_tracker, "_kill_pid", lambda entry: 4242)
    monkeypatch.setattr(fresh_tracker, "_belongs_to_run", lambda pid, uid: False)

    assert fresh_tracker.run_terminal_pid("focus-bad") is None


def test_run_terminal_pid_falls_back_to_the_persisted_pid(fresh_tracker, project, monkeypatch):
    """A dead launcher must not hide a live terminal: the row's pid is the net."""
    proc = _sleep_proc()
    try:
        run = registry.create_run("focus-fallback", project["id"], "TUI",
                                  standalone=True)
        registry.update_run(run["run_uid"], status="running", pid=proc.pid)
        fresh_tracker._entries["focus-fallback"] = {
            "run_uid": "focus-fallback", "proc": None, "pid": 111,
        }
        monkeypatch.setattr(fresh_tracker, "_kill_pid", lambda entry: None)
        monkeypatch.setattr(fresh_tracker, "_belongs_to_run", lambda pid, uid: True)
        monkeypatch.setattr(fresh_tracker, "_terminal_owner", lambda pid, uid: 555)

        assert fresh_tracker.run_terminal_pid(run["run_uid"]) == 555
    finally:
        proc.kill()


def test_run_terminal_pid_after_a_restart_requires_the_marker(project, monkeypatch):
    proc = _sleeper()
    try:
        run = registry.create_run("focus-restart", project["id"], "TUI", standalone=True)
        registry.update_run(run["run_uid"], status="running", pid=proc.pid)
        restarted = ProcessTracker()  # empty in-memory map: the server restarted

        monkeypatch.setattr(restarted, "_belongs_to_run", lambda pid, uid: True)
        monkeypatch.setattr(restarted, "_terminal_owner", lambda pid, uid: 555)
        assert restarted.run_terminal_pid(run["run_uid"]) == 555

        monkeypatch.setattr(restarted, "_belongs_to_run", lambda pid, uid: False)
        assert restarted.run_terminal_pid(run["run_uid"]) is None
    finally:
        proc.kill()
