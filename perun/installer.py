"""Machine-level installation for Perun.

``perun install`` (see :mod:`perun.integrations.install_global`) wires Perun into
OpenCode for *this user*. This module covers the parts that are specific to a
*machine* rather than to a single OpenCode install:

* **Prerequisite checks** — Python version, ``pip``, and (as advisory notes)
  ``git``, the ``opencode`` CLI, and a reachable Ollama server.
* **Dependency install** — ``python -m pip install -r requirements.txt``, so a
  freshly cloned/copied repo becomes runnable in one step. Perun's import graph
  is stdlib-only at module load (``yaml``/``fastapi``/``httpx`` are imported
  lazily), so this can safely run even before any dependency exists.
* **The optional ``PerunBridge`` logon task** — on Windows this can register the
  single-instance Telegram bridge so it starts at logon. By default the bridge is
  *not* registered as a task; it starts with the dashboard (``serve --bridge``) or
  can be launched by hand with ``python -m perun bridge``. The bridge is a
  *singleton* (``getUpdates`` allows one consumer per token), so the task is
  created with ``MultipleInstancesPolicy=IgnoreNew`` and delegates
  single-instance enforcement to the machine-global :class:`perun.telegram.PidLock`.

Everything is idempotent: re-running repairs rather than duplicates.

The one-click entry point for a **new machine** is ``bootstrap.ps1`` at the repo
root, which locates Python and then delegates here::

    python -m perun install --machine
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape as _xml_escape

from . import config

#: Name of the scheduled task that keeps the Telegram bridge alive.
TASK_NAME = "PerunBridge"

#: Minimum supported Python (matches ``pyproject.toml`` ``requires-python``).
MIN_PYTHON = (3, 10)

#: Description embedded in the registered scheduled task.
TASK_DESCRIPTION = (
    "Perun Telegram bridge — single machine-wide getUpdates consumer. "
    "Managed by 'python -m perun install --machine'."
)


def is_windows() -> bool:
    """``True`` on Windows.

    Isolated behind a function (rather than a bare ``os.name == "nt"``) so tests
    can exercise the Windows-only scheduled-task paths without patching the
    global ``os`` module.
    """
    return os.name == "nt"


# --------------------------------------------------------------------------- #
# Prerequisites
# --------------------------------------------------------------------------- #
def python_version_ok(version: tuple[int, int] | None = None) -> bool:
    v = version if version is not None else (sys.version_info.major, sys.version_info.minor)
    return v >= MIN_PYTHON


def pip_available() -> bool:
    try:
        import pip  # noqa: F401

        return True
    except Exception:
        return False


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""


def which_version(executable: str, *args: str) -> str | None:
    """Return the first output line of ``executable args`` or ``None``."""
    exe = shutil.which(executable)
    if not exe:
        return None
    try:
        res = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return _first_line(res.stdout or res.stderr) or None
    except Exception:
        return None


def ollama_status(timeout: float = 1.5) -> tuple[bool, str]:
    """Best-effort probe of the configured Ollama server (semantic memory).

    Ollama is *optional* — the traceability/embedding paths are fail-open — so
    this never raises and never blocks the install for long.
    """
    host = config.Settings.load().ollama_host.rstrip("/")
    url = f"{host}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost
            if 200 <= getattr(resp, "status", 200) < 300:
                return True, f"reachable at {host}"
    except urllib.error.URLError as exc:
        return False, f"not reachable at {host} ({exc.reason})"
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"not reachable at {host} ({exc})"
    return False, f"unexpected response from {host}"


def check_prerequisites() -> list[dict[str, Any]]:
    """A list of ``{name, ok, required, detail}`` prerequisite checks.

    ``required`` marks the checks that abort the install (Python version, pip);
    everything else is surfaced as a warning so the install still completes on a
    machine that has not yet installed Ollama or the ``opencode`` CLI.
    """
    checks: list[dict[str, Any]] = []

    checks.append({
        "name": "python",
        "ok": python_version_ok(),
        "required": True,
        "detail": f"{platform.python_version()} at {sys.executable}",
    })

    has_pip = pip_available()
    checks.append({
        "name": "pip",
        "ok": has_pip,
        "required": True,
        "detail": "importable" if has_pip else "missing (run: python -m ensurepip)",
    })

    git_version = which_version("git", "--version")
    checks.append({
        "name": "git",
        "ok": bool(git_version),
        "required": False,
        "detail": git_version or "not found on PATH (optional)",
    })

    opencode_version = which_version("opencode", "--version")
    checks.append({
        "name": "opencode",
        "ok": bool(opencode_version),
        "required": False,
        "detail": opencode_version or "not found on PATH (needed to run agents)",
    })

    node_version = which_version("node", "--version")
    checks.append({
        "name": "node",
        "ok": bool(node_version),
        "required": False,
        "detail": node_version or "not found on PATH (Node.js is required by OpenCode CLI)",
    })

    ok, detail = ollama_status()
    checks.append({
        "name": "ollama",
        "ok": ok,
        "required": False,
        "detail": detail + " (optional: semantic memory is fail-open)",
    })

    return checks


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
def requirements_file() -> Path:
    return config.REPO_ROOT / "requirements.txt"


def pip_install(requirements: Path | None = None, timeout: int = 1800) -> dict[str, Any]:
    """``python -m pip install -r requirements.txt`` (or ``pyproject`` extras).

    Returns ``{ok, command, summary, output_tail}``; never raises.
    """
    req = requirements or requirements_file()
    if req.is_file():
        cmd = [sys.executable, "-m", "pip", "install", "-r", str(req)]
    else:
        # No requirements.txt (e.g. an installed package): install the project.
        cmd = [sys.executable, "-m", "pip", "install", str(config.REPO_ROOT)]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(config.REPO_ROOT),
            check=False,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "command": cmd, "summary": str(exc), "output_tail": ""}

    out = res.stdout or ""
    err = res.stderr or ""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    blob = out + err
    tail = "\n".join(blob.strip().splitlines()[-15:])
    summary = _first_line(err) or _first_line(out)
    return {
        "ok": res.returncode == 0,
        "command": cmd,
        "summary": summary,
        "output_tail": tail,
    }


# --------------------------------------------------------------------------- #
# PerunBridge scheduled task (Windows only)
# --------------------------------------------------------------------------- #
def _python_exe() -> str:
    """Absolute interpreter path for the task's ``Exec`` action.

    Honours ``PERUN_PYTHON`` (the same override ``Perun.cmd`` uses) so the
    launcher and the logon task are guaranteed to use the same interpreter.
    """
    env = (os.environ.get("PERUN_PYTHON") or "").strip()
    if env and Path(env).is_file():
        return str(Path(env).resolve())
    exe = sys.executable or shutil.which("python") or "python"
    try:
        if os.path.isabs(exe) and Path(exe).is_file():
            return str(Path(exe).resolve())
    except Exception:  # pragma: no cover - defensive
        pass
    return str(exe)


def _task_user() -> str:
    """``DOMAIN\\user`` for the logon trigger / principal (best effort)."""
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    if domain and user:
        return f"{domain}\\{user}"
    return user or os.environ.get("USER") or ""


def bridge_task_xml(
    *,
    python_exe: str | None = None,
    workdir: str | None = None,
    user: str | None = None,
    description: str = TASK_DESCRIPTION,
) -> str:
    """Deterministic XML for the ``PerunBridge`` task.

    Mirrors the settings that make the bridge a safe long-running singleton:
    ``IgnoreNew`` (a second trigger is refused), an unlimited
    ``ExecutionTimeLimit``, restart-on-failure, and a logon trigger.
    """
    exe = python_exe or _python_exe()
    cwd = workdir or str(config.REPO_ROOT)
    who = user if user is not None else _task_user()
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <URI>\\{_xml_escape(TASK_NAME)}</URI>\n"
        f"    <Description>{_xml_escape(description)}</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{_xml_escape(who)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <RestartOnFailure>\n"
        "      <Count>999</Count>\n"
        "      <Interval>PT1M</Interval>\n"
        "    </RestartOnFailure>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <IdleSettings>\n"
        "      <Duration>PT10M</Duration>\n"
        "      <WaitTimeout>PT1H</WaitTimeout>\n"
        "      <StopOnIdleEnd>true</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>\n"
        "  </Settings>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{_xml_escape(who)}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{_xml_escape(exe)}</Command>\n"
        "      <Arguments>-m perun bridge</Arguments>\n"
        f"      <WorkingDirectory>{_xml_escape(cwd)}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _run_schtasks(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Invoke ``schtasks.exe`` capturing raw bytes (its output is not UTF-8)."""
    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _decode_console(raw: bytes) -> str:
    """Decode ``schtasks`` output, which may be UTF-16 or the OEM codepage."""
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    for enc in ("utf-8", "cp1252", "cp850"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def bridge_task_exists() -> bool:
    if not is_windows():
        return False
    return _run_schtasks(["/Query", "/TN", TASK_NAME]).returncode == 0


def bridge_task_status() -> dict[str, Any]:
    """``{windows, exists, command, arguments, workdir, raw_ok}``."""
    status: dict[str, Any] = {"windows": is_windows(), "exists": False}
    if not is_windows():
        return status
    res = _run_schtasks(["/Query", "/TN", TASK_NAME, "/XML"])
    status["exists"] = res.returncode == 0
    if not status["exists"]:
        return status
    xml = _decode_console(res.stdout or b"")
    status["raw_ok"] = True
    # Strip the XML declaration before parsing a *str* (stdlib expat rejects a
    # str whose declaration names an encoding other than UTF-8).
    xml = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", xml, count=1)
    try:
        root = ElementTree.fromstring(xml)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        exec_el = root.find(".//t:Actions/t:Exec", namespaces=ns)
        if exec_el is not None:
            status["command"] = (exec_el.findtext("t:Command", default="", namespaces=ns) or "")
            status["arguments"] = (exec_el.findtext("t:Arguments", default="", namespaces=ns) or "")
            status["workdir"] = (exec_el.findtext("t:WorkingDirectory", default="", namespaces=ns) or "")
    except (ElementTree.ParseError, ValueError):
        status["parse_error"] = True
    return status


def register_bridge_task(*, start: bool = False) -> dict[str, Any]:
    """Create/replace the ``PerunBridge`` logon task. Idempotent (``/F``).

    Returns ``{ok, skipped?, reason, python, workdir, started?}``. Never raises.
    """
    if not is_windows():
        return {"ok": False, "skipped": True, "reason": "the bridge task is Windows-only"}

    exe = _python_exe()
    cwd = str(config.REPO_ROOT)
    xml = bridge_task_xml(python_exe=exe, workdir=cwd)

    fd, tmp_name = tempfile.mkstemp(prefix="perun_bridge_", suffix=".xml")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(xml, encoding="utf-16")
        res = _run_schtasks(["/Create", "/TN", TASK_NAME, "/XML", str(tmp), "/F"])
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "reason": str(exc), "python": exe, "workdir": cwd}
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    out: dict[str, Any] = {
        "ok": res.returncode == 0,
        "python": exe,
        "workdir": cwd,
        "reason": "" if res.returncode == 0 else _decode_console(res.stderr or res.stdout or b"").strip(),
    }

    if out["ok"] and start:
        run = _run_schtasks(["/Run", "/TN", TASK_NAME])
        out["started"] = run.returncode == 0
    return out


def remove_bridge_task() -> dict[str, Any]:
    """Delete the ``PerunBridge`` task if present. Never raises."""
    if not is_windows():
        return {"ok": True, "skipped": True, "reason": "the bridge task is Windows-only"}
    if not bridge_task_exists():
        return {"ok": True, "removed": False, "reason": "not registered"}
    res = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    return {
        "ok": res.returncode == 0,
        "removed": res.returncode == 0,
        "reason": "" if res.returncode == 0 else _decode_console(res.stderr or res.stdout or b"").strip(),
    }


def restart_bridge_task() -> dict[str, Any]:
    """Stop and re-start the ``PerunBridge`` task so it re-reads ``config.json``.

    This is how a **new Telegram bot** takes effect: the bridge resolves its
    token once at startup (:func:`perun.telegram.run_bridge`), so a changed token
    only applies after a restart.

    Safe by construction: ``schtasks /End`` terminates only the process *the task*
    started, never the dashboard. If the task is not the current lock holder (for
    example the dashboard itself runs the bridge via ``serve --bridge``), ``/End``
    is a no-op and the new instance exits on :class:`perun.telegram.PidLock` —
    so a second poller still never appears. Never raises.
    """
    if not is_windows():
        return {"ok": False, "skipped": True, "reason": "the bridge task is Windows-only"}
    if not bridge_task_exists():
        return {"ok": False, "reason": "the PerunBridge task is not registered"}

    end = _run_schtasks(["/End", "/TN", TASK_NAME])
    run = _run_schtasks(["/Run", "/TN", TASK_NAME])
    # /End reports "not running" as a soft failure; only /Run must succeed.
    return {
        "ok": run.returncode == 0,
        "ended": end.returncode == 0,
        "started": run.returncode == 0,
        "reason": "" if run.returncode == 0
        else _decode_console(run.stderr or run.stdout or b"").strip(),
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _print_checks(checks: list[dict[str, Any]]) -> bool:
    """Print the check table; return ``True`` when a required check failed."""
    failed_required = False
    for c in checks:
        if c["ok"]:
            mark = "ok  "
        elif c["required"]:
            mark = "FAIL"
            failed_required = True
        else:
            mark = "warn"
        print(f"[perun] {mark} {c['name']:<10} {c['detail']}")
    return failed_required


def machine_install(
    *,
    skip_deps: bool = False,
    skip_global: bool = False,
    skip_task: bool = False,
    task: bool = False,
    skip_index: bool = False,
    force: bool = False,
) -> int:
    """Full machine setup. Returns a process exit code.

    The Telegram bridge starts with the dashboard (``serve --bridge``) or can be
    launched by hand with ``python -m perun bridge``. The optional
    ``PerunBridge`` logon task is only registered when ``task=True``.
    """
    from . import db

    print("=" * 68)
    print("  Perun — machine install")
    print("=" * 68)
    print(f"[perun] repo    {config.REPO_ROOT}")
    print(f"[perun] home    {config.HOME}")
    print(f"[perun] python  {sys.executable} ({platform.python_version()})")
    print("-" * 68)

    if _print_checks(check_prerequisites()):
        print("[perun] aborting: a required prerequisite is missing.")
        return 1

    db.init_db()
    config.ensure_dirs()

    if not skip_deps:
        print("-" * 68)
        print("[perun] installing Python dependencies (requirements.txt) ...")
        res = pip_install()
        if res["ok"]:
            print("[perun] deps    ok (pip install -r requirements.txt)")
        else:
            print("[perun] deps    FAILED")
            if res.get("output_tail"):
                print(res["output_tail"])
            print("[perun] hint: install the dependencies manually, then re-run "
                  "`python -m perun install --machine --skip-deps`")
            return 1

    if not skip_global:
        print("-" * 68)
        from .integrations import install_global

        install_global.install(force=force, index=not skip_index)

    if task:
        print("-" * 68)
        token, _ = config.Settings.load().telegram()
        result = register_bridge_task(start=bool(token))
        if result.get("skipped"):
            print(f"[perun] bridge  skipped ({result.get('reason')})")
        elif result.get("ok"):
            state = "started" if result.get("started") else (
                "registered (starts at next logon)" if not token
                else "registered")
            print(f"[perun] bridge  {state}")
            print(f"[perun]         task '{TASK_NAME}' -> {result['python']} -m perun bridge")
            print(f"[perun]         workdir {result['workdir']}")
            if not token:
                print("[perun]         note: no Telegram token configured in "
                      f"{config.CONFIG_PATH} — the task will idle harmlessly")
        else:
            print(f"[perun] bridge  FAILED to register the '{TASK_NAME}' task")
            if result.get("reason"):
                print(f"[perun]         {result['reason']}")
            print("[perun]         fix: run this from an elevated shell, or register "
                  "manually with schtasks /Create /TN PerunBridge /XML <file> /F")
    elif skip_task:
        print("[perun] bridge  skipped (--skip-task)")
    else:
        print("-" * 68)
        result = remove_bridge_task()
        if result.get("removed"):
            print(f"[perun] bridge  removed the '{TASK_NAME}' logon task")
        elif result.get("skipped"):
            print(f"[perun] bridge  no logon task ({result.get('reason')})")
        else:
            print(f"[perun] bridge  logon task removal result: {result}")

    print("=" * 68)
    print("  Perun is installed.")
    print(f"  * dashboard : python -m perun serve   -> http://127.0.0.1:{config.Settings.load().port}")
    print(f"  * or just double-click: {config.REPO_ROOT / 'Perun.cmd'}")
    print("  * every OpenCode project now inherits Perun (MCP + AGENTS.md).")
    print("=" * 68)
    return 0


def uninstall_machine() -> int:
    """Machine-level teardown (currently: the bridge task)."""
    res = remove_bridge_task()
    if res.get("skipped"):
        print(f"[perun] bridge  skipped ({res.get('reason')})")
    elif res.get("removed"):
        print(f"[perun] bridge  removed the '{TASK_NAME}' task")
    else:
        print(f"[perun] bridge  no '{TASK_NAME}' task to remove")
    return 0
