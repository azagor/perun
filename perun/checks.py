"""Operator-declared project checks and proposal verification runs.

A **check** is the only kind of command Perun runs on its own. Two sources
exist, and they have different trust anchors:

``declared``
    The operator declares a name -> command in Perun's own database (``perun
    checks add``). The declaration lives in ``PERUN_HOME``, never in the project
    tree, so no room message, agent tool or approved proposal can add or change
    it. An optional ``pin`` freezes the reviewed script's sha256: the run is
    refused if the file changed after the operator read it.

``proposal``
    Ad-hoc verification of one executed shared-project proposal. The command is
    *derived* from an approved script target (never free text from a proposal),
    the operator is shown the exact argv and the content hash, and the run is
    refused if the file changed between the confirmation and the execution.

Everything else - arbitrary shell, an unapproved target, a command typed into a
room - is deliberately impossible.

Concurrency: a check run inserts a ``running`` row before starting the process.
A partial unique index on ``(project_id) WHERE status='running'`` makes that row
a cross-process lease (the Perun and guard MCP servers are separate processes),
so two rooms cannot run two checks in one project at the same time. A crashed
run leaves an ``abandoned`` row for the next call to reap.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import db, path_guard, registry

#: Check names are stable identifiers, not prose.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
#: The MCP/room path is capped lower than the CLI: a room reply must not block
#: for ten minutes on a hung build.
MCP_MAX_TIMEOUT = 300
MAX_COMMAND = 2000
MAX_OUTPUT = 16_000
SCRIPT_SUFFIXES = (".py", ".ps1", ".sh", ".bat", ".cmd")

#: Environment kept for a check. Everything else is dropped, so a delivered
#: script cannot exfiltrate credentials that happen to be in Perun's process
#: environment. ``PYTHON*`` is included because the interpreter needs it.
_ENV_ALLOW = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "WINDIR",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
    "OS", "USERNAME", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL",
    "LC_CTYPE", "TZ", "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
}

_ANSI = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


class CheckError(ValueError):
    """A check cannot be run as requested (unknown name, drift, bad target)."""


class CheckBusy(RuntimeError):
    """Another check is already running in this project."""


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
#: How long an operator's "Run it" answer stays usable. Long enough to survive a
#: room's short wait window, short enough that a stale confirmation cannot be
#: replayed the next day.
CONFIRM_TTL_SECONDS = 3600


def get_confirmation(project_id: int, proposal_id: str, target: str,
                     sha256: str) -> dict[str, Any] | None:
    """The live confirmation for this exact proposal content, if any."""
    with db.db() as conn:
        row = db.one(
            conn,
            "SELECT * FROM check_confirmations WHERE project_id=? AND "
            "proposal_id=? AND target=? AND sha256=? AND used_at='' AND "
            "expires_at>? ORDER BY created_at DESC LIMIT 1",
            (int(project_id), proposal_id, target, sha256, time.time()),
        )
    return dict(row) if row else None


def record_confirmation(project_id: int, proposal_id: str, target: str,
                        sha256: str, attention_id: int) -> dict[str, Any]:
    """Remember the card so a later call does not ask the operator twice."""
    confirmation_id = f"chk_{uuid.uuid4().hex[:12]}"
    now = time.time()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO check_confirmations (id, project_id, proposal_id, target, "
            "sha256, attention_id, created_at, expires_at, used_at) "
            "VALUES (?,?,?,?,?,?,?,?, '') "
            "ON CONFLICT(proposal_id, target, sha256) DO UPDATE SET "
            "attention_id=excluded.attention_id, created_at=excluded.created_at, "
            "expires_at=excluded.expires_at, used_at=''",
            (confirmation_id, int(project_id), proposal_id, target, sha256,
             int(attention_id), registry.now_iso(), now + CONFIRM_TTL_SECONDS),
        )
        row = db.one(
            conn,
            "SELECT * FROM check_confirmations WHERE proposal_id=? AND target=? "
            "AND sha256=?",
            (proposal_id, target, sha256),
        )
    return dict(row)


def claim_confirmation(confirmation_id: str) -> bool:
    """Consume the confirmation atomically; only one caller may run it."""
    with db.db() as conn:
        cur = conn.execute(
            "UPDATE check_confirmations SET used_at=? WHERE id=? AND used_at=''",
            (registry.now_iso(), confirmation_id),
        )
        return cur.rowcount == 1


def release_confirmation(confirmation_id: str) -> None:
    """Hand a consumed confirmation back after a run that never started."""
    with db.db() as conn:
        conn.execute(
            "UPDATE check_confirmations SET used_at='' WHERE id=?",
            (confirmation_id,),
        )


def drop_confirmation(confirmation_id: str) -> None:
    """Forget a card that expired or vanished without an answer."""
    with db.db() as conn:
        conn.execute("DELETE FROM check_confirmations WHERE id=?", (confirmation_id,))


def _clamp_timeout(value: Any, *, maximum: int = MAX_TIMEOUT) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise CheckError("timeout must be an integer number of seconds")
    if seconds < 1:
        raise CheckError("timeout must be at least 1 second")
    return min(seconds, maximum)


def _project_root(project: dict[str, Any]) -> Path:
    root = Path(str(project.get("path") or "")).resolve()
    if not root.is_dir():
        raise CheckError(f"project directory does not exist: {root}")
    return root


def _resolve_cwd(root: Path, cwd: str) -> Path:
    """Resolve a project-relative working directory, refusing an escape."""
    raw = (cwd or "").strip()
    if not raw or raw == ".":
        return root
    candidate = (root / raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise CheckError(f"cwd '{raw}' escapes the project directory")
    if not candidate.is_dir():
        raise CheckError(f"cwd '{raw}' is not a directory")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _pin_digest(root: Path, pin: str) -> tuple[str, str]:
    """Return ``(relative_path, sha256)`` for a pinned file inside the project."""
    raw = (pin or "").strip()
    if not raw:
        return "", ""
    path = path_guard.resolve_target(root, raw)
    if path != root and root not in path.parents:
        raise CheckError(f"pin '{raw}' escapes the project directory")
    if not path.is_file():
        raise CheckError(f"pin '{raw}' is not a file")
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - guarded above
        raise CheckError(f"pin '{raw}' escapes the project directory")
    return relative, _sha256_file(path)


def _check_row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["timeout_seconds"] = int(out.get("timeout_seconds") or DEFAULT_TIMEOUT)
    return out


def declare(
    project: dict[str, Any],
    name: str,
    command: str,
    *,
    cwd: str = "",
    timeout_seconds: Any = DEFAULT_TIMEOUT,
    pin: str = "",
    created_by: str = "",
) -> dict[str, Any]:
    """Declare (or replace) an operator-owned check. CLI/dashboard only.

    No MCP tool calls this: declarations are a deliberate operator action, so a
    shared-project participant can never widen Perun's execution surface. The
    command and its runs are stored in Perun's database and activity log: never
    put a credential in it (use the secret store for that).
    """
    project_id = int(project["id"])
    slug = str(project.get("slug") or project_id)
    name = (name or "").strip().lower()
    if not NAME_RE.fullmatch(name):
        raise CheckError(
            "check name must be 1-40 lowercase letters, digits, '.', '_' or '-'")
    command = (command or "").strip()
    if not command:
        raise CheckError("command is required")
    if len(command) > MAX_COMMAND:
        raise CheckError(f"command is too long (max {MAX_COMMAND} characters)")
    timeout = _clamp_timeout(timeout_seconds)
    root = _project_root(project)
    cwd = (cwd or "").strip().replace("\\", "/")
    _resolve_cwd(root, cwd)
    pin_path, pin_sha256 = _pin_digest(root, pin)
    ts = registry.now_iso()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO project_checks (project_id, name, command, cwd, "
            "timeout_seconds, pin_path, pin_sha256, created_by, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id, name) DO UPDATE SET command=excluded.command, "
            "cwd=excluded.cwd, timeout_seconds=excluded.timeout_seconds, "
            "pin_path=excluded.pin_path, pin_sha256=excluded.pin_sha256, "
            "updated_at=excluded.updated_at",
            (project_id, name, command, cwd, timeout, pin_path, pin_sha256,
             created_by or "operator", ts, ts),
        )
        row = db.one(
            conn, "SELECT * FROM project_checks WHERE project_id=? AND name=?",
            (project_id, name),
        )
    registry.log_activity(
        project_id, "check_declared", f"Check '{name}' declared for {slug}",
        {"name": name, "command": command, "pin": pin_path,
         "timeout_seconds": timeout},
    )
    return _check_row(row)


def list_checks(project_id: int) -> list[dict[str, Any]]:
    with db.db() as conn:
        rows = db.rows(
            conn,
            "SELECT c.*, r.status AS last_status, r.exit_code AS last_exit_code, "
            "r.created_at AS last_run_at FROM project_checks c "
            "LEFT JOIN check_runs r ON r.id = (SELECT id FROM check_runs "
            "  WHERE project_id=c.project_id AND check_name=c.name "
            "  ORDER BY id DESC LIMIT 1) "
            "WHERE c.project_id=? ORDER BY c.name",
            (int(project_id),),
        )
    return [_check_row(r) for r in rows]


def get_check(project_id: int, name: str) -> dict[str, Any] | None:
    with db.db() as conn:
        row = db.one(
            conn, "SELECT * FROM project_checks WHERE project_id=? AND name=?",
            (int(project_id), (name or "").strip().lower()),
        )
    return _check_row(row) if row else None


def remove(project_id: int, name: str) -> bool:
    with db.db() as conn:
        cur = conn.execute(
            "DELETE FROM project_checks WHERE project_id=? AND name=?",
            (int(project_id), (name or "").strip().lower()),
        )
        removed = cur.rowcount == 1
    if removed:
        registry.log_activity(
            int(project_id), "check_removed", f"Check '{name}' removed",
            {"name": name},
        )
    return removed


def history(project_id: int, name: str = "", limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 20), 200))
    with db.db() as conn:
        if name:
            rows = db.rows(
                conn,
                "SELECT * FROM check_runs WHERE project_id=? AND check_name=? "
                "ORDER BY id DESC LIMIT ?",
                (int(project_id), name.strip().lower(), limit),
            )
        else:
            rows = db.rows(
                conn,
                "SELECT * FROM check_runs WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (int(project_id), limit),
            )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def _env() -> dict[str, str]:
    """A scrubbed environment: PATH and OS basics, never Perun's credentials."""
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _ENV_ALLOW or upper.startswith("PYTHON"):
            out[key] = value
    out.setdefault("PYTHONIOENCODING", "utf-8")
    out.setdefault("PYTHONUTF8", "1")
    return out


def _clean_output(text: str) -> str:
    text = _ANSI.sub("", text or "")
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip()


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the process and everything it started.

    ``subprocess`` has no cross-platform tree kill, and a timed-out build that
    leaves children behind would keep the port/file lock it held.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except Exception:  # pragma: no cover - taskkill should exist
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except Exception:  # pragma: no cover - the child may already be gone
            pass
    try:
        proc.kill()
    except Exception:  # pragma: no cover
        pass


def _execute(command: str | list[str], *, cwd: Path,
             timeout_seconds: int) -> dict[str, Any]:
    """Run one command, capture merged stdout/stderr, never raise for exit!=0."""
    started = time.monotonic()
    shell = isinstance(command, str)
    argv: str | list[str] = command if shell else list(command)
    extra: dict[str, Any] = {}
    if os.name == "nt":
        extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        extra["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv, shell=shell, cwd=str(cwd), env=_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", **extra,
        )
    except OSError as exc:
        return {
            "status": "error", "exit_code": None, "timed_out": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output": f"could not start the command: {exc}", "truncated": False,
        }
    timed_out = False
    try:
        raw, _ = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            raw, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            raw = ""
    duration_ms = int((time.monotonic() - started) * 1000)
    text = _clean_output(raw or "")
    truncated = len(text) > MAX_OUTPUT
    if truncated:
        text = text[:MAX_OUTPUT] + f"\n... [truncated at {MAX_OUTPUT} characters]"
    exit_code = proc.returncode
    if timed_out:
        status = "timeout"
    elif exit_code == 0:
        status = "ok"
    else:
        status = "failed"
    return {
        "status": status, "exit_code": exit_code, "timed_out": timed_out,
        "duration_ms": duration_ms, "output": text, "truncated": truncated,
    }


def reap_stale(project_id: int | None = None) -> int:
    """Mark runs whose process died without finishing as ``abandoned``."""
    cutoff = time.time()
    sql = ("UPDATE check_runs SET status='abandoned' WHERE status='running' "
           "AND started_epoch + timeout_seconds + 120 < ?")
    with db.db() as conn:
        if project_id is None:
            cur = conn.execute(sql, (cutoff,))
        else:
            cur = conn.execute(sql + " AND project_id=?", (cutoff, int(project_id)))
        return cur.rowcount


def _begin_run(project: dict[str, Any], *, check_name: str, source: str,
               command: str, requester: str, trigger: str, proposal_id: str,
               timeout_seconds: int, content_sha256: str) -> int:
    reap_stale(int(project["id"]))
    ts = registry.now_iso()
    try:
        with db.db() as conn:
            cur = conn.execute(
                "INSERT INTO check_runs (project_id, check_name, source, command, "
                "requester, trigger, proposal_id, status, timeout_seconds, "
                "started_epoch, content_sha256, created_at) "
                "VALUES (?,?,?,?,?,?,?, 'running', ?,?,?,?)",
                (int(project["id"]), check_name, source, command, requester,
                 trigger, proposal_id, int(timeout_seconds), time.time(),
                 content_sha256, ts),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise CheckBusy(
            f"another check is already running in {project.get('slug')}") from exc


def _finish_run(run_id: int, result: dict[str, Any]) -> None:
    with db.db() as conn:
        conn.execute(
            "UPDATE check_runs SET status=?, exit_code=?, duration_ms=?, "
            "output=?, output_truncated=? WHERE id=?",
            (result["status"], result.get("exit_code"), int(result["duration_ms"]),
             result.get("output") or "", 1 if result.get("truncated") else 0,
             int(run_id)),
        )


def _run(project: dict[str, Any], *, name: str, source: str,
         command: str | list[str], cwd: str, timeout_seconds: int,
         requester: str, trigger: str, proposal_id: str = "",
         content_sha256: str = "") -> dict[str, Any]:
    root = _project_root(project)
    workdir = _resolve_cwd(root, cwd)
    command_text = command if isinstance(command, str) else shlex.join(command)
    run_id = _begin_run(
        project, check_name=name, source=source, command=command_text,
        requester=requester, trigger=trigger, proposal_id=proposal_id,
        timeout_seconds=timeout_seconds, content_sha256=content_sha256,
    )
    try:
        result = _execute(command, cwd=workdir, timeout_seconds=timeout_seconds)
    except Exception as exc:  # pragma: no cover - defensive
        result = {
            "status": "error", "exit_code": None, "timed_out": False,
            "duration_ms": 0, "output": f"check runner failed: {exc}",
            "truncated": False,
        }
    _finish_run(run_id, result)
    result.update({
        "run_id": run_id, "check": name, "source": source,
        "command": command_text, "timeout_seconds": timeout_seconds,
        "requester": requester, "trigger": trigger, "proposal_id": proposal_id,
        "content_sha256": content_sha256,
    })
    kind = {"ok": "check_ok", "failed": "check_failed",
            "timeout": "check_timeout"}.get(result["status"], "check_error")
    registry.log_activity(
        int(project["id"]), kind,
        f"Check '{name}' {result['status']} "
        f"({result.get('exit_code')}, {result['duration_ms']} ms)",
        {"run_id": run_id, "name": name, "source": source,
         "status": result["status"], "exit_code": result.get("exit_code"),
         "proposal_id": proposal_id},
    )
    return result


def _verify_pin(project: dict[str, Any], check: dict[str, Any]) -> None:
    pin_path = str(check.get("pin_path") or "")
    if not pin_path:
        return
    root = _project_root(project)
    path = path_guard.resolve_target(root, pin_path)
    if not path.is_file():
        raise CheckError(
            f"check '{check['name']}' is pinned to {pin_path}, which no longer exists")
    actual = _sha256_file(path)
    if actual != str(check.get("pin_sha256") or ""):
        raise CheckError(
            f"check '{check['name']}' is pinned to {pin_path}, which changed "
            f"(expected {str(check.get('pin_sha256'))[:12]}, found {actual[:12]}); "
            "review it and re-declare the check")


def run_declared(project: dict[str, Any], name: str, *, requester: str = "",
                 trigger: str = "mcp", max_timeout: int = MAX_TIMEOUT) -> dict[str, Any]:
    """Run an operator-declared check. Raises ``CheckError``/``CheckBusy``."""
    check = get_check(int(project["id"]), name)
    if not check:
        declared = ", ".join(c["name"] for c in list_checks(int(project["id"])))
        raise CheckError(
            f"no check named '{name}' in {project.get('slug')}"
            + (f"; declared: {declared}" if declared else "; none declared"))
    _verify_pin(project, check)
    return _run(
        project, name=check["name"], source="declared", command=check["command"],
        cwd=check["cwd"], timeout_seconds=min(check["timeout_seconds"], max_timeout),
        requester=requester, trigger=trigger, content_sha256=check["pin_sha256"],
    )


# --------------------------------------------------------------------------- #
# Proposal verification
# --------------------------------------------------------------------------- #
def _proposal_argv(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(path)]
    if suffix == ".ps1":
        return ["powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(path)]
    if suffix == ".sh":
        shell = shutil.which("bash") or shutil.which("sh")
        if not shell:
            raise CheckError("no bash/sh interpreter is available")
        return [shell, str(path)]
    if suffix in (".bat", ".cmd"):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", str(path)]
    raise CheckError(
        "only a .py, .ps1, .sh, .bat or .cmd target can be verified, "
        f"not '{path.suffix or 'a directory'}'")


def _display_argv(argv: list[str], root: Path) -> str:
    """A card the operator can reproduce: ``python tests/verify.py``.

    The absolute interpreter stays in the executed argv (never resolved through
    PATH), but the confirmation shows the short form a human recognises.
    """
    interpreters = {
        os.path.normcase(sys.executable): "python",
        os.path.normcase(os.environ.get("COMSPEC") or "cmd.exe"): "cmd",
    }
    shown: list[str] = []
    for part in argv:
        short = interpreters.get(os.path.normcase(part))
        if short:
            shown.append(short)
            continue
        try:
            shown.append(Path(part).relative_to(root).as_posix())
        except (ValueError, OSError):
            shown.append(part)
    return shlex.join(shown)


def _same_target(left: str, right: str) -> bool:
    return (os.path.normcase(str(left).replace("\\", "/"))
            == os.path.normcase(str(right).replace("\\", "/")))


def derive_proposal_verification(
    project: dict[str, Any], proposal: dict[str, Any], target: str = ""
) -> dict[str, Any]:
    """Derive the one command a proposal may be verified with.

    Nothing here comes from free text: the command is the project's interpreter
    plus an approved script target, so a proposal can never smuggle a shell
    command into the confirmation card.
    """
    if not isinstance(proposal, dict) or not proposal.get("id"):
        raise CheckError("proposal not found")
    status = str(proposal.get("status") or "")
    if status != "executed":
        raise CheckError(
            f"proposal {proposal.get('id')} is {status or 'unknown'}; only an "
            "executed proposal can be verified")
    targets = [str(t) for t in (proposal.get("targets") or [])]
    candidates = [t for t in targets
                  if Path(t).suffix.lower() in SCRIPT_SUFFIXES]
    wanted = (target or "").strip()
    if wanted:
        chosen = next((t for t in targets if _same_target(t, wanted)), "")
        if not chosen:
            raise CheckError(f"{wanted} is not a target of this proposal")
        if Path(chosen).suffix.lower() not in SCRIPT_SUFFIXES:
            raise CheckError(f"{chosen} is not a runnable script target")
    elif len(candidates) == 1:
        chosen = candidates[0]
    elif not candidates:
        raise CheckError(
            "this proposal has no runnable script target "
            f"({', '.join(SCRIPT_SUFFIXES)}); declare a check instead")
    else:
        raise CheckError(
            "this proposal has several script targets; pass target=<path>: "
            + ", ".join(candidates))
    root = _project_root(project)
    path = path_guard.resolve_target(root, chosen)
    if path != root and root not in path.parents:
        raise CheckError(f"{chosen} resolves outside the project directory")
    if path_guard.is_sensitive_path(path):
        raise CheckError(f"{chosen} is a protected or sensitive path")
    if not path.is_file():
        raise CheckError(f"{chosen} does not exist on disk")
    size = path.stat().st_size
    if size > 256 * 1024 * 1024:
        raise CheckError(f"{chosen} is too large to hash safely ({size} bytes)")
    argv = _proposal_argv(path)
    return {
        "target": chosen,
        "path": str(path),
        "argv": argv,
        "display": _display_argv(argv, root),
        "sha256": _sha256_file(path),
        "size": size,
    }


def run_proposal_verification(
    project: dict[str, Any], proposal: dict[str, Any], *,
    target: str = "", expected_sha256: str = "", requester: str = "",
    trigger: str = "mcp", max_timeout: int = MCP_MAX_TIMEOUT,
) -> dict[str, Any]:
    """Run a proposal's approved script, only at the hash the operator saw."""
    derived = derive_proposal_verification(project, proposal, target)
    if expected_sha256 and derived["sha256"] != expected_sha256:
        raise CheckError(
            "the target changed after the operator confirmed it; ask again "
            f"(confirmed {expected_sha256[:12]}, found {derived['sha256'][:12]})")
    result = _run(
        project, name=derived["target"], source="proposal",
        command=derived["argv"], cwd="", timeout_seconds=max_timeout,
        requester=requester, trigger=trigger,
        proposal_id=str(proposal.get("id") or ""),
        content_sha256=derived["sha256"],
    )
    result["target"] = derived["target"]
    result["display"] = derived["display"]
    return result


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #
def format_result(result: dict[str, Any], *, max_output: int = 4000) -> str:
    """One compact, honest report: status, exit code, duration, output."""
    parts = [f"exit {result.get('exit_code')}" if result.get("exit_code") is not None
             else "no exit code",
             f"{int(result.get('duration_ms') or 0) / 1000:.1f}s"]
    if result.get("timed_out"):
        parts.append(f"timed out after {result.get('timeout_seconds')}s")
    if result.get("source") == "proposal":
        parts.append(f"proposal {result.get('proposal_id')}")
        parts.append(f"sha256 {str(result.get('content_sha256') or '')[:12]}")
    head = (f"[perun] check '{result.get('check')}' {result.get('status')} "
            f"({', '.join(parts)})")
    output = (result.get("output") or "").strip()
    if not output:
        return head + "\n(no output)"
    if len(output) > max_output:
        output = output[:max_output] + "\n... [more output in the check history]"
    return head + "\n" + output
