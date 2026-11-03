"""Tests for perun.daemon server lifecycle helpers."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from pathlib import Path

import psutil
import pytest

from perun import daemon


def test_pid_file_roundtrip(tmp_path, monkeypatch):
    pid_file = tmp_path / "perun.pid"
    monkeypatch.setattr(daemon, "PID_PATH", pid_file)

    assert daemon.read_pid() is None
    daemon.write_pid(12345)
    assert daemon.read_pid() == 12345
    daemon.clear_pid()
    assert daemon.read_pid() is None


def test_clear_pid_removes_stale_file(tmp_path, monkeypatch):
    pid_file = tmp_path / "perun.pid"
    monkeypatch.setattr(daemon, "PID_PATH", pid_file)

    daemon.write_pid(99999999)
    assert pid_file.exists()
    daemon.clear_pid()
    assert not pid_file.exists()


def test_clear_pid_keeps_current_pid(tmp_path, monkeypatch):
    pid_file = tmp_path / "perun.pid"
    monkeypatch.setattr(daemon, "PID_PATH", pid_file)

    daemon.write_pid(os.getpid())
    daemon.clear_pid()
    assert not pid_file.exists()


def test_is_running_current_process():
    assert daemon.is_running(os.getpid()) is True


def test_is_running_missing_process():
    assert daemon.is_running(99999999) is False


def test_stop_gracefully_terminates(tmp_path, monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert daemon.is_running(proc.pid) is True
        assert daemon.stop(proc.pid, timeout=5) is True
        assert daemon.is_running(proc.pid) is False
    finally:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


def test_kill_missing_process():
    assert daemon.kill(99999999) is True


def test_is_another_perun_running_matches_cmdline(monkeypatch, tmp_path):
    pid_file = tmp_path / "perun.pid"
    monkeypatch.setattr(daemon, "PID_PATH", pid_file)
    # Don't let a real Perun on port 8760 interfere with the stale-file test.
    monkeypatch.setattr(daemon, "_port_owner", lambda p: None)

    proc = subprocess.Popen([
        sys.executable, "-c",
        "import time; time.sleep(60)",
    ], executable=sys.executable)
    try:
        # Give psutil a moment to see the process.
        time.sleep(0.2)
        pid_file.write_text(str(proc.pid), encoding="utf-8")
        # The cmdline does not contain "perun serve", so it should be treated as stale.
        assert daemon.is_another_perun_running() is None
        assert not pid_file.exists()
    finally:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


def test_is_another_perun_running_falls_back_to_port_owner(monkeypatch, tmp_path):
    pid_file = tmp_path / "perun.pid"
    monkeypatch.setattr(daemon, "PID_PATH", pid_file)

    proc = subprocess.Popen([
        sys.executable, "-c",
        "import sys; sys.argv=['perun','serve']; import time; time.sleep(60)",
    ], executable=sys.executable)
    try:
        time.sleep(0.2)
        fake_settings = type('S', (), {'port': 8760})()
        # PID file is missing, but port owner looks like perun serve.
        monkeypatch.setattr(daemon, "_port_owner", lambda p: proc.pid if p == 8760 else None)
        assert daemon.is_another_perun_running(fake_settings) == proc.pid
    finally:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


def test_is_another_perun_running_port_owner_non_perun_is_ignored(monkeypatch, tmp_path):
    pid_file = tmp_path / "perun.pid"
    monkeypatch.setattr(daemon, "PID_PATH", pid_file)

    proc = subprocess.Popen([
        sys.executable, "-c",
        "import time; time.sleep(60)",
    ], executable=sys.executable)
    try:
        time.sleep(0.2)
        fake_settings = type('S', (), {'port': 8760})()
        monkeypatch.setattr(daemon, "_port_owner", lambda p: proc.pid if p == 8760 else None)
        # cmdline lacks "perun serve" so the port owner must be ignored.
        assert daemon.is_another_perun_running(fake_settings) is None
    finally:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


def test_stop_uses_taskkill_on_windows(monkeypatch):
    import subprocess as sp
    captured = []

    class FakeProc:
        pid = 123
        _running = True
        def is_running(self):
            return self._running
        def terminate(self):
            self._running = False

    monkeypatch.setattr(psutil, "Process", lambda pid: FakeProc())
    monkeypatch.setattr(os, "name", "nt")

    def fake_run(*args, **kwargs):
        captured.append(args)
        return type('R', (), {'returncode': 0})()

    monkeypatch.setattr(sp, "run", fake_run)
    assert daemon.stop(123, timeout=1) is True
    assert captured and captured[0][0][:3] == ["taskkill", "/PID", "123"]


def test_start_detached_starts_background_process(monkeypatch, tmp_path):
    marker = tmp_path / "started.marker"
    script = tmp_path / "daemon_marker.py"
    script.write_text(
        f"import time; open({str(marker)!r}, 'w').write('ok'); time.sleep(60)\n",
        encoding="utf-8",
    )
    pid = daemon.start_detached([sys.executable, str(script)])
    try:
        # Wait for the marker file to appear.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.1)
        assert marker.exists()
        assert psutil.Process(pid).is_running()
    finally:
        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass


def test_record_run_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "RUN_RECORD_PATH", tmp_path / "server.json")

    assert daemon.read_recorded_run() is None
    assert daemon.read_recorded_argv() is None

    daemon.record_run(["serve", "--bridge", "--port", "8760"], pid=12345)
    data = daemon.read_recorded_run()
    assert data["pid"] == 12345
    assert data["argv"] == ["serve", "--bridge", "--port", "8760"]
    assert daemon.read_recorded_argv() == ["serve", "--bridge", "--port", "8760"]


def test_record_run_defaults_to_sys_argv(tmp_path, monkeypatch):
    """With no explicit argv, the current process's argv (minus argv0) is used."""
    monkeypatch.setattr(daemon, "RUN_RECORD_PATH", tmp_path / "server.json")
    monkeypatch.setattr(sys, "argv", ["perun", "serve", "--bridge"])

    daemon.record_run(pid=1)
    assert daemon.read_recorded_argv() == ["serve", "--bridge"]


def test_record_run_survives_stop(tmp_path, monkeypatch):
    """The record describes how Perun was last launched; stop must not clear it."""
    monkeypatch.setattr(daemon, "RUN_RECORD_PATH", tmp_path / "server.json")
    monkeypatch.setattr(daemon, "PID_PATH", tmp_path / "perun.pid")

    daemon.record_run(["serve", "--bridge"], pid=os.getpid())
    daemon.write_pid(os.getpid())
    daemon.clear_pid()  # what stop does for the current process
    assert daemon.read_recorded_argv() == ["serve", "--bridge"]


def test_read_recorded_run_tolerates_garbage(tmp_path, monkeypatch):
    path = tmp_path / "server.json"
    monkeypatch.setattr(daemon, "RUN_RECORD_PATH", path)

    path.write_text("not json at all", encoding="utf-8")
    assert daemon.read_recorded_run() is None

    path.write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, wrong shape
    assert daemon.read_recorded_run() is None

    path.write_text('{"argv": "serve"}', encoding="utf-8")  # argv not a list
    assert daemon.read_recorded_run() is None

    path.write_text('{"argv": ["serve", 5]}', encoding="utf-8")  # non-str item
    assert daemon.read_recorded_run() is None


# --------------------------------------------------------------------------- #
# Degraded mode: psutil missing (a failed pip install must not break the CLI)
# --------------------------------------------------------------------------- #
def _hide_psutil(monkeypatch):
    """Simulate an installation where psutil was never installed."""
    monkeypatch.setattr(daemon, "psutil", None)
    monkeypatch.setattr(daemon, "HAS_PSUTIL", False)


def test_pid_checks_work_without_psutil(monkeypatch):
    _hide_psutil(monkeypatch)

    assert daemon.is_running(os.getpid()) is True
    assert daemon.is_running(99999999) is False


def test_stop_works_without_psutil(monkeypatch):
    _hide_psutil(monkeypatch)

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert daemon.is_running(proc.pid) is True
        assert daemon.stop(proc.pid, timeout=10) is True
        assert daemon.is_running(proc.pid) is False
    finally:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


def test_kill_works_without_psutil(monkeypatch):
    _hide_psutil(monkeypatch)

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert daemon.kill(proc.pid) is True
        assert daemon.is_running(proc.pid) is False
    finally:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


def test_port_owner_netstat_parses_listener(monkeypatch):
    output = (
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:8760         0.0.0.0:0              LISTENING       4242\n"
        "  TCP    127.0.0.1:9000         0.0.0.0:0              ESTABLISHED     99\n"
    )
    monkeypatch.setattr(
        daemon.subprocess, "run",
        lambda *a, **k: type("R", (), {"stdout": output})(),
    )

    assert daemon._port_owner_netstat(8760) == 4242
    assert daemon._port_owner_netstat(1111) is None


def test_port_owner_without_psutil_falls_back_to_netstat(monkeypatch):
    _hide_psutil(monkeypatch)
    monkeypatch.setattr(daemon.os, "name", "nt")
    monkeypatch.setattr(daemon, "_port_owner_netstat",
                        lambda p: 4242 if p == 8760 else None)

    assert daemon._port_owner(8760) == 4242
    assert daemon._port_owner(1111) is None


def test_tasklist_matching_uses_the_pid_field(monkeypatch):
    """CSV keeps a spaced image name intact; PID 123 must not match 1234."""
    _hide_psutil(monkeypatch)
    monkeypatch.setattr(daemon.os, "name", "nt")
    table = (
        '"python.exe","1234","Console","1","12,345 K"\n'
        '"my app.exe","99","Console","1","1,000 K"\n'
        "INFO: No tasks are running which match the specified criteria.\n"
    )
    monkeypatch.setattr(
        daemon.subprocess, "run",
        lambda *a, **k: type("R", (), {"stdout": table})(),
    )

    assert daemon.is_running(1234) is True
    assert daemon.is_running(99) is True
    assert daemon.is_running(123) is False


def test_tasklist_matching_tolerates_garbage():
    assert daemon._tasklist_pid_listed(7, "") is False
    assert daemon._tasklist_pid_listed(7, "not a table at all") is False
    assert daemon._tasklist_pid_listed(7, '"a.exe","7","Console\n') is True


def test_proc_stat_state_reads_the_state_letter():
    assert daemon._proc_stat_state("42 (python3) Z 1 2 3") == "Z"
    assert daemon._proc_stat_state("42 (a b) S 1 2 3") == "S"
    assert daemon._proc_stat_state("garbage") == ""


def test_a_zombie_is_not_a_live_process(monkeypatch):
    _hide_psutil(monkeypatch)
    # Stub the POSIX module surface so no real signal is sent on Windows.
    monkeypatch.setattr(
        daemon, "os", types.SimpleNamespace(name="posix", kill=lambda pid, sig: None))

    monkeypatch.setattr(daemon, "_posix_state", lambda pid: "Z")
    assert daemon.is_running(424242) is False

    monkeypatch.setattr(daemon, "_posix_state", lambda pid: "S")
    assert daemon.is_running(424242) is True


def test_cmdline_uses_cim_without_psutil(monkeypatch):
    _hide_psutil(monkeypatch)
    monkeypatch.setattr(daemon.os, "name", "nt")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return type("R", (), {"stdout": r"  C:\Python312\python.exe -m perun serve  "})()

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)

    assert daemon._cmdline(4242) == r"C:\Python312\python.exe -m perun serve"
    assert captured["argv"][0] == "powershell"
    assert "ProcessId=4242" in captured["argv"][-1]


def test_is_perun_process_requires_a_matching_command_line(monkeypatch):
    """Without psutil a live PID alone is not evidence: read the command line."""
    _hide_psutil(monkeypatch)
    monkeypatch.setattr(daemon, "_pid_alive", lambda pid: True)

    monkeypatch.setattr(
        daemon, "_cmdline",
        lambda pid: r"C:\Python312\python.exe -m perun serve --port 8760")
    assert daemon._is_perun_process(4242) is True

    monkeypatch.setattr(daemon, "_cmdline", lambda pid: r"C:\Windows\explorer.exe")
    assert daemon._is_perun_process(4242) is False

    # Identity unknown: never treat it as Perun (perun stop must not kill it).
    monkeypatch.setattr(daemon, "_cmdline", lambda pid: "")
    assert daemon._is_perun_process(4242) is False


def test_is_another_perun_running_ignores_a_recycled_pid_without_psutil(monkeypatch,
                                                                       tmp_path):
    """A stale PID file must not hand an unrelated process to perun stop."""
    _hide_psutil(monkeypatch)
    pid_file = tmp_path / "perun.pid"
    monkeypatch.setattr(daemon, "PID_PATH", pid_file)
    monkeypatch.setattr(daemon, "_port_owner", lambda p: None)
    monkeypatch.setattr(daemon, "_cmdline",
                        lambda pid: "python -c import time; time.sleep(60)")

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.2)
        pid_file.write_text(str(proc.pid), encoding="utf-8")
        assert daemon.is_another_perun_running() is None
        assert not pid_file.exists()  # stale file cleared, process left alone
        assert daemon.is_running(proc.pid) is True
    finally:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


def test_identity_is_confirmed_once_per_lookup(monkeypatch, tmp_path):
    """PID file and port owner are the same process: confirm identity once."""
    pid_file = tmp_path / "perun.pid"
    monkeypatch.setattr(daemon, "PID_PATH", pid_file)
    pid_file.write_text("4242", encoding="utf-8")

    calls = []

    def fake_confirm(pid):
        calls.append(pid)
        return True

    monkeypatch.setattr(daemon, "_is_perun_process", fake_confirm)
    monkeypatch.setattr(daemon, "_port_owner", lambda port: 4242)

    settings = type("S", (), {"port": 8760})()
    assert daemon.is_another_perun_running(settings) == 4242
    assert calls == [4242]
