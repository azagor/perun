"""Tests for perun.daemon server lifecycle helpers."""

from __future__ import annotations

import os
import subprocess
import sys
import time
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
