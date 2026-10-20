"""Server lifecycle helpers: PID file, running check, graceful stop/restart.

Perun is a single-machine service. To avoid accidental double-starts and to
make restarts safe, ``perun serve`` writes its PID to ``<PERUN_HOME>/perun.pid``.
``perun stop`` and ``perun restart`` use that file to find and gracefully shut
down the existing process before replacing it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from . import config

PID_PATH: Path = config.HOME / "perun.pid"
RUN_RECORD_PATH: Path = config.HOME / "server.json"


def _pid_path() -> Path:
    return PID_PATH


def _run_record_path() -> Path:
    return RUN_RECORD_PATH


def write_pid(pid: int | None = None) -> Path:
    """Write *pid* (or the current process PID) to the PID file atomically.

    The file is created owner-only (``0600``) to match the surrounding
    security posture for files under ``PERUN_HOME``.
    """
    pid = pid or os.getpid()
    config.ensure_dirs()
    tmp = _pid_path().with_suffix(".pid.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(str(pid))
    tmp.replace(_pid_path())
    if os.name != "nt":
        try:
            import stat
            os.chmod(_pid_path(), stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return _pid_path()


def read_pid() -> int | None:
    """Return the PID from the PID file, or None if missing/invalid."""
    try:
        text = _pid_path().read_text(encoding="utf-8").strip()
        return int(text)
    except (OSError, ValueError):
        return None


def clear_pid(force: bool = False) -> None:
    """Remove the PID file.

    By default it is removed only when it is missing, belongs to the current
    process, or points to a dead process. Pass ``force=True`` to remove it
    unconditionally (used when we have already verified the PID file is stale).
    """
    try:
        if force:
            _pid_path().unlink(missing_ok=True)
            return
        pid = read_pid()
        if pid is None or pid == os.getpid() or not is_running(pid):
            _pid_path().unlink(missing_ok=True)
    except OSError:
        pass


def record_run(argv: list[str] | None = None, pid: int | None = None) -> Path:
    """Record how the current server was launched, for ``perun restart``.

    Writes ``<PERUN_HOME>/server.json`` (owner-only, atomically, like the PID
    file) with the effective serve flags (``--host``/``--port``/``--reload``/
    ``--bridge``). ``perun restart`` replays these so a plain restart does not
    silently drop configuration such as the Telegram/PerunChat bridges.

    The record deliberately survives ``perun stop``: it describes how Perun was
    *last* launched, and restarting after a stop should still replay it. It is
    rewritten on every successful ``perun serve`` start.
    """
    if argv is None:
        argv = sys.argv[1:]
    config.ensure_dirs()
    payload = json.dumps(
        {"pid": pid or os.getpid(), "argv": list(argv), "recorded_at": time.time()}
    )
    path = _run_record_path()
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    tmp.replace(path)
    return path


def read_recorded_run() -> dict[str, Any] | None:
    """Return the recorded run dict (``{"pid", "argv", "recorded_at"}``).

    Tolerant by design: a missing, unreadable, or malformed record (including
    pre-v0.7.10 installs that never wrote one) simply yields None.
    """
    try:
        data = json.loads(_run_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    argv = data.get("argv")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return None
    return data


def read_recorded_argv() -> list[str] | None:
    """Return the argv of the last recorded ``perun serve`` start, or None."""
    data = read_recorded_run()
    return list(data["argv"]) if data else None


def is_running(pid: int) -> bool:
    """True if *pid* is a live process."""
    try:
        return psutil.Process(pid).is_running()
    except psutil.NoSuchProcess:
        return False


def _port_owner(port: int) -> int | None:
    """Return the PID listening on *port*, or None if the port is free."""
    try:
        for conn in psutil.net_connections(kind="inet"):
            laddr = getattr(conn, "laddr", None)
            if laddr is None:
                continue
            laddr_port = getattr(laddr, "port", None)
            if laddr_port == port and conn.status == psutil.CONN_LISTEN:
                return conn.pid
    except Exception:
        pass
    return None


def _looks_like_perun(proc: psutil.Process) -> bool:
    """True if *proc*'s command line looks like a ``perun serve`` instance."""
    try:
        cmd = " ".join(proc.cmdline()).lower()
        return "perun" in cmd and "serve" in cmd
    except Exception:
        return False


def is_another_perun_running(settings: "config.Settings | None" = None) -> int | None:
    """Return the PID of another Perun server process, or None.

    Reads the PID file and confirms the process is alive and looks like a
    ``perun serve`` instance (so a stale PID file from a crashed process is
    ignored and cleared). Also checks the configured listening port as a
    cross-reference, so a missing/stale PID file does not let us miss a server
    that is actually running.
    """
    if settings is None:
        settings = config.Settings.load()

    pid = read_pid()
    if pid is not None and pid != os.getpid():
        try:
            proc = psutil.Process(pid)
            if proc.is_running() and _looks_like_perun(proc):
                return pid
        except psutil.NoSuchProcess:
            pass
        # PID file is stale (dead PID or non-perun process).
        clear_pid(force=True)

    # Cross-check the listening port in case the PID file is missing or stale.
    port_owner = _port_owner(settings.port)
    if port_owner and port_owner != os.getpid():
        try:
            proc = psutil.Process(port_owner)
            if proc.is_running() and _looks_like_perun(proc):
                # A server is actually running on our port. Do not overwrite the
                # PID file here; leave it to the caller to decide.
                return port_owner
        except psutil.NoSuchProcess:
            pass
    return None


def stop(pid: int, timeout: float = 30.0) -> bool:
    """Gracefully stop *pid*, waiting up to *timeout* seconds.

    On POSIX this sends SIGTERM. On Windows we first try ``taskkill /T`` so
    the process tree receives a WM_CLOSE/CTRL+C friendly shutdown; if that
    fails or is unavailable we fall back to psutil.terminate().

    Returns True if the process exited, False otherwise.
    """
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True

    def _gone() -> bool:
        try:
            return not proc.is_running()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return True
        except Exception:
            # Best-effort: if we can't inspect the process, assume it exited.
            return True

    if os.name == "nt":
        try:
            tk = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            tk = None
        if tk is not None and tk.returncode == 0:
            # taskkill is asynchronous; wait for the process to exit.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if _gone():
                    return True
                time.sleep(0.2)
        # taskkill failed or process didn't exit in time; fall through.

    try:
        proc.terminate()
    except psutil.NoSuchProcess:
        return True
    except Exception:
        pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gone():
            return True
        time.sleep(0.2)

    return _gone()


def kill(pid: int) -> bool:
    """Force-kill *pid*. Returns True if it is no longer running."""
    try:
        psutil.Process(pid).kill()
    except psutil.NoSuchProcess:
        return True
    except Exception:
        pass
    try:
        return not psutil.Process(pid).is_running()
    except psutil.NoSuchProcess:
        return True


def start_detached(argv: list[str], **popen_kwargs: Any) -> int:
    """Start *argv* detached from the current console/terminal.

    Returns the new process PID. This is used by ``perun restart`` so the
    command can exit while the server keeps running.
    """
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # DETACHED_PROCESS runs without a console; incompatible with CREATE_NEW_CONSOLE.
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    kwargs.update(popen_kwargs)
    proc = subprocess.Popen(argv, **kwargs)
    return proc.pid


def write_pid_for(pid: int) -> Path:
    """Write *pid* to the PID file; intended for use by detached starters."""
    return write_pid(pid)
