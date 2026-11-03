"""Server lifecycle helpers: PID file, running check, graceful stop/restart.

Perun is a single-machine service. To avoid accidental double-starts and to
make restarts safe, ``perun serve`` writes its PID to ``<PERUN_HOME>/perun.pid``.
``perun stop`` and ``perun restart`` use that file to find and gracefully shut
down the existing process before replacing it.
"""

from __future__ import annotations

import csv
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:  # optional: Perun must still start when the dependency is missing
    import psutil  # type: ignore
    HAS_PSUTIL = True
except Exception:  # pragma: no cover - depends on the interpreter
    psutil = None  # type: ignore[assignment]
    HAS_PSUTIL = False

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
    if os.name != "nt":
        # Belt and braces, matching write_pid(): the creation mode above is
        # already 0600, but write_pid() re-asserts it after the rename and the
        # record deserves the same guarantees.
        try:
            import stat
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
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


def _tasklist_pid_listed(pid: int, output: str) -> bool:
    """True if *pid* is the PID field of a ``tasklist /FO CSV /NH`` row.

    CSV keeps a quoted image name with spaces in one field, and comparing the
    PID field (rather than a substring) keeps PID 123 from matching PID 1234.
    """
    try:
        for row in csv.reader((output or "").splitlines()):
            if len(row) >= 2 and row[1].strip() == str(pid):
                return True
    except csv.Error:
        pass
    return False


def _proc_stat_state(text: str) -> str:
    """Process state letter from ``/proc/<pid>/stat`` content ('' if unknown)."""
    try:
        return text.rsplit(")", 1)[1].split()[0]
    except (IndexError, ValueError):
        return ""


def _posix_state(pid: int) -> str:
    """Best-effort process state on POSIX: ``/proc`` first, ``ps`` otherwise.

    A zombie is still a PID, but it no longer serves anything: treating it as
    dead keeps a crashed server from looking alive forever.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return _proc_stat_state(text)
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, timeout=10,
        ).stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[-1][:1] if lines else ""


def _cmdline(pid: int) -> str:
    """Best-effort command line for *pid* ('' when it cannot be read).

    Used to confirm that a PID really is ``perun serve`` before Perun stops it;
    an unidentified PID must never be killed just because it inherited a
    recycled PID from a stale ``perun.pid``.
    """
    if psutil is not None:
        try:
            return " ".join(psutil.Process(pid).cmdline())
        except Exception:
            return ""
    if os.name == "nt":
        # No psutil: Windows exposes another process's command line through
        # CIM. This costs one PowerShell start, but only on the degraded path.
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=20,
            ).stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return out.strip()
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        pass
    try:
        return (subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=10,
        ).stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _pid_alive(pid: int) -> bool:
    """True if *pid* is a live process (zombies count as dead).

    ``psutil`` is used when available; without it we fall back to what the
    platform gives us for free (``tasklist`` on Windows, ``/proc``/``ps`` and
    ``os.kill(pid, 0)`` on POSIX). A missing dependency must never make the CLI
    unusable.
    """
    if pid <= 0:
        return False
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return False
            try:
                return proc.status() != psutil.STATUS_ZOMBIE
            except Exception:
                return True
        except psutil.NoSuchProcess:
            return False
        except Exception:
            pass  # fall through to the dependency-free check
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10,
            ).stdout or ""
        except (OSError, subprocess.TimeoutExpired):
            return False
        return _tasklist_pid_listed(pid, out)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by somebody else
    except OSError:
        return False
    # A zombie still owns the PID but no longer serves anything.
    return not _posix_state(pid).upper().startswith("Z")


def is_running(pid: int) -> bool:
    """True if *pid* is a live process."""
    return _pid_alive(pid)


def _port_owner_netstat(port: int) -> int | None:
    """Windows-only ``netstat -ano`` fallback for :func:`_port_owner`."""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10,
        ).stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        try:
            local_port = int(parts[1].rsplit(":", 1)[-1])
            owner = int(parts[4])
        except (IndexError, ValueError):
            continue
        if local_port == port:
            return owner
    return None


def _port_owner(port: int) -> int | None:
    """Return the PID listening on *port*, or None if the port is free."""
    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="inet"):
                laddr = getattr(conn, "laddr", None)
                if laddr is None:
                    continue
                laddr_port = getattr(laddr, "port", None)
                if laddr_port == port and conn.status == psutil.CONN_LISTEN:
                    return conn.pid
            # psutil answered authoritatively: no need for the netstat fallback.
            return None
        except Exception:
            pass
    if os.name == "nt":
        return _port_owner_netstat(port)
    return None


def _is_perun_process(pid: int) -> bool:
    """True if *pid* is a live ``perun serve`` instance.

    The command line is always checked, with or without ``psutil``: a stale
    ``perun.pid`` whose PID was recycled must never make ``perun stop`` kill an
    unrelated process. When the command line cannot be read, the PID is
    treated as *not* Perun.
    """
    if not _pid_alive(pid):
        return False
    cmd = _cmdline(pid).lower()
    if not cmd:
        return False
    return "perun" in cmd and "serve" in cmd


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

    # Confirm identity at most once per PID: in degraded mode (no psutil) that
    # check can cost a PowerShell/CIM or ``ps`` start, and the PID file and the
    # port owner are usually the same process.
    confirmed: dict[int, bool] = {}

    def _confirmed(candidate: int) -> bool:
        if candidate not in confirmed:
            confirmed[candidate] = _is_perun_process(candidate)
        return confirmed[candidate]

    pid = read_pid()
    if pid is not None and pid != os.getpid():
        if _confirmed(pid):
            return pid
        # PID file is stale (dead PID or non-perun process).
        clear_pid(force=True)

    # Cross-check the listening port in case the PID file is missing or stale.
    port_owner = _port_owner(settings.port)
    if port_owner and port_owner != os.getpid() and _confirmed(port_owner):
        # A server is actually running on our port. Do not overwrite the
        # PID file here; leave it to the caller to decide.
        return port_owner
    return None


def stop(pid: int, timeout: float = 30.0) -> bool:
    """Gracefully stop *pid*, waiting up to *timeout* seconds.

    On POSIX this sends SIGTERM. On Windows we first try ``taskkill /T`` so
    the process tree receives a WM_CLOSE/CTRL+C friendly shutdown; if that
    fails or is unavailable we fall back to ``psutil.terminate()`` (or
    ``os.kill`` when psutil is not installed).

    Returns True if the process exited, False otherwise.
    """
    proc: Any = None
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        except Exception:
            proc = None
    if proc is None and not _pid_alive(pid):
        return True

    def _gone() -> bool:
        if proc is not None:
            try:
                return not proc.is_running()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return True
            except Exception:
                # Best-effort: if we can't inspect the process, assume it exited.
                return True
        return not _pid_alive(pid)

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

    if proc is not None:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            return True
        except Exception:
            pass
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gone():
            return True
        time.sleep(0.2)

    return _gone()


def kill(pid: int) -> bool:
    """Force-kill *pid*. Returns True if it is no longer running."""
    if psutil is not None:
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
        except Exception:
            pass
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return not _pid_alive(pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    return not _pid_alive(pid)


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
