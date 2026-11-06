#!/usr/bin/env python3
"""Shared-project filesystem guard MCP server.

Exposes guarded file tools that validate every target path against the project's
manifest allowlist before touching disk. The allowlist is read from the
``PERUN_ALLOWLIST`` environment variable (JSON) or derived from the project at
``PERUN_PROJECT_PATH``.

When no allowlist is configured the server falls back to pass-through, so it is
safe to register globally and only enforces for shared-project runs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastmcp import FastMCP  # noqa: E402

from perun import checks, path_guard, registry, shared_projects  # noqa: E402

mcp = FastMCP("PerunGuard")


def _project() -> dict[str, Any]:
    """The registered project this guard server runs inside."""
    raw = os.environ.get("PERUN_PROJECT_PATH", "").strip()
    if not raw:
        try:
            raw = os.getcwd()
        except OSError:
            raw = "."
    project = registry.get_project_by_path(str(Path(raw).resolve()))
    if project is None:
        raise ValueError(f"project is not registered: {raw}")
    return project


def _requester() -> str:
    return os.environ.get("PERUN_CHAT_SENDER", "").strip() or "perun"


def _active_local_proposal(
    project_path: Path,
) -> tuple[dict[str, Any] | None, bool]:
    """Return (executing proposal, ambiguous) for a local run."""
    try:
        from perun import action_proposals, registry

        project = registry.get_project_by_path(str(project_path))
        if not project:
            return None, False
        executing = [
            p for p in action_proposals.list_by_project(project["id"], status="executing")
            if p.get("execution_mode") != "external"
        ]
        if not executing:
            return None, False
        if len(executing) > 1:
            return None, True
        return executing[0], False
    except Exception:
        return None, False


def _active_targets(project_path: Path) -> tuple[list[Path], bool]:
    """Return (targets, ambiguous) for the project's active local execution.

    The project-local guard server is started by the OpenCode service, which
    does not inherit the per-run ``PERUN_ALLOWLIST``. When that env is absent,
    derive the target scope from the proposal currently in ``executing`` state.
    More than one executing proposal is ambiguous; the caller fails closed.
    """
    proposal, ambiguous = _active_local_proposal(project_path)
    if ambiguous:
        return [], True
    if proposal is None:
        return [], False
    operations = proposal.get("operations") or []
    if not operations:
        # An executing proposal without a declared changeset is ambiguous;
        # fail closed rather than granting an unrestricted write scope.
        return [], True
    try:
        return [
            path_guard.resolve_target(project_path, str(op.get("path") or ""))
            for op in operations
            if isinstance(op, dict) and str(op.get("path") or "").strip()
        ], False
    except Exception:
        return [], False


def _operation_for(target: Path) -> str:
    """Return the active changeset's operation for *target*, or "" if none."""
    try:
        project = _project()
        proposal, ambiguous = _active_local_proposal(Path(project["path"]))
        if ambiguous or proposal is None:
            return ""
        for op in proposal.get("operations") or []:
            if not isinstance(op, dict):
                continue
            try:
                candidate = path_guard.resolve_target(
                    project["path"], str(op.get("path") or ""))
            except Exception:
                continue
            if candidate == target:
                return str(op.get("op") or "").strip().lower()
    except Exception:
        return ""
    return ""


def _allowlist() -> tuple[Path, list[tuple[Path, str]], list[Path]]:
    """Return (project_root, [(path, access), ...], approved_targets)."""
    raw = os.environ.get("PERUN_ALLOWLIST", "").strip()
    if raw:
        try:
            cfg = json.loads(raw)
        except Exception:
            cfg = {}
        root = Path(cfg.get("root") or os.getcwd()).resolve()
        deps: list[tuple[Path, str]] = []
        for dep in cfg.get("deps", []):
            p = Path(dep.get("path") or "").resolve() if dep.get("path") else root
            access = str(dep.get("access") or "rw").strip().lower()
            if access not in ("rw", "ro"):
                access = "rw"
            deps.append((p, access))
        targets: list[Path] = []
        for target in cfg.get("targets", []) or []:
            p = Path(str(target))
            targets.append((p if p.is_absolute() else root / p).resolve())
        return root, [(root, "rw"), *deps], targets

    project_path = os.environ.get("PERUN_PROJECT_PATH", "").strip()
    if not project_path:
        try:
            project_path = os.getcwd()
        except OSError:
            project_path = "."
    cfg = shared_projects.load(project_path)
    if cfg.get("shared"):
        root = Path(project_path).resolve()
        targets, ambiguous = _active_targets(root)
        if ambiguous:
            # Two local executions in the same project: refuse writes rather
            # than falling back to an unscoped root allowlist.
            return root, [], []
        return root, path_guard.allowlist(
            project_path, cfg.get("external_deps", [])
        ), targets
    # Not a shared project: unrestricted pass-through.
    root = Path(project_path).resolve()
    return root, [(root, "rw")], []


def _is_under(target: Path, parent: Path) -> bool:
    try:
        target.relative_to(parent)
        return True
    except ValueError:
        return False


def _check(path: str, require_write: bool = False) -> Path:
    """Resolve *path* and validate it against the allowlist and targets.

    Raises ValueError with a clear message if the path is disallowed.
    """
    root, entries, targets = _allowlist()
    target = path_guard.resolve_target(root, path)

    if path_guard.is_sensitive_path(target):
        raise ValueError(f"{target} is a protected or sensitive path")

    allowed = False
    for parent, access in entries:
        if not _is_under(target, parent):
            continue
        if require_write and access == "ro":
            raise ValueError(f"{target} is read-only in the project manifest")
        allowed = True
        break
    if not allowed:
        raise ValueError(f"{target} is outside the project allowlist")
    if require_write and targets and not any(_is_under(target, t) for t in targets):
        raise ValueError(f"{target} is outside the approved target paths")
    return target


def _read_text(path: Path, limit: int, offset: int) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        if offset:
            fh.seek(offset)
        return fh.read(limit)


@mcp.tool()
def guard_read_file(path: str, limit: int = 2000, offset: int = 0) -> str:
    """Read up to *limit* characters from *path* starting at *offset*."""
    target = _check(path, require_write=False)
    if not target.exists():
        raise FileNotFoundError(f"not found: {target}")
    if not target.is_file():
        raise ValueError(f"not a file: {target}")
    return _read_text(target, max(0, limit), max(0, offset))


@mcp.tool()
def guard_list_directory(path: str = ".") -> list[dict[str, Any]]:
    """List entries in *path*."""
    target = _check(path, require_write=False)
    if not target.exists():
        raise FileNotFoundError(f"not found: {target}")
    if not target.is_dir():
        raise ValueError(f"not a directory: {target}")
    out: list[dict[str, Any]] = []
    for entry in sorted(target.iterdir(), key=lambda e: e.name):
        if path_guard.is_sensitive_path(entry):
            continue
        out.append({
            "name": entry.name,
            "type": "directory" if entry.is_dir() else "file",
            "path": str(entry),
        })
    return out


@mcp.tool()
def guard_write_file(path: str, content: str) -> str:
    """Write *content* to *path* (create or overwrite)."""
    target = _check(path, require_write=True)
    operation = _operation_for(target)
    if operation == "delete":
        raise ValueError("guarded writes are not valid for a delete operation")
    if operation and operation not in ("create", "edit"):
        raise ValueError(f"guarded writes are not valid for a {operation} operation")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {target}"


@mcp.tool()
def guard_edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace *old_string* with *new_string* in *path*."""
    target = _check(path, require_write=True)
    operation = _operation_for(target)
    if operation == "delete":
        raise ValueError("guarded edits are not valid for a delete operation")
    if operation and operation not in ("create", "edit"):
        raise ValueError(f"guarded edits are not valid for a {operation} operation")
    if not target.exists():
        raise FileNotFoundError(f"not found: {target}")
    text = target.read_text(encoding="utf-8", errors="replace")
    if old_string not in text:
        raise ValueError("old_string not found in file")
    text = text.replace(old_string, new_string, 1)
    target.write_text(text, encoding="utf-8")
    return f"edited {target}"


@mcp.tool()
def guard_delete_file(path: str) -> str:
    """Delete *path*, only while its declared operation is ``delete``.

    Deleting is a declared operation: the path must be inside the active
    changeset and that target's operation must be ``delete``. The chat persona
    cannot call it at all (its agent permissions deny delete), and this check is
    the backstop for any other tool path.
    """
    target = _check(path, require_write=True)
    if _operation_for(target) != "delete":
        raise ValueError(
            "guard_delete_file requires a delete operation on this target")
    if not target.exists():
        raise FileNotFoundError(f"not found: {target}")
    if not target.is_file():
        raise ValueError(f"not a file: {target}")
    target.unlink()
    return f"deleted {target}"


@mcp.tool()
def guard_list_checks() -> list[dict[str, Any]]:
    """List the operator-declared checks for this project.

    A check is the only command Perun may run without asking the operator
    again. The declaration is owned by the operator (``perun checks add``);
    this tool can list and run them, never create one.
    """
    project = _project()
    return [
        {
            "name": check["name"],
            "command": check["command"],
            "cwd": check["cwd"],
            "timeout_seconds": check["timeout_seconds"],
            "pinned": bool(check["pin_path"]),
            "last_status": check.get("last_status"),
            "last_exit_code": check.get("last_exit_code"),
        }
        for check in checks.list_checks(project["id"])
    ]


@mcp.tool()
def guard_run_check(name: str) -> str:
    """Run one operator-declared check and report its exit code and output.

    Use this to verify a change instead of saying you have no shell access.
    Only checks the operator declared with ``perun checks add`` exist; this
    tool cannot add one and never accepts a command as an argument.
    """
    project = _project()
    try:
        result = checks.run_declared(
            project, name, requester=_requester(), trigger="room-check",
            max_timeout=checks.MCP_MAX_TIMEOUT,
        )
    except checks.CheckBusy as exc:
        return f"[perun] {exc}; try again when it finishes."
    except checks.CheckError as exc:
        return f"[perun] {exc}"
    return checks.format_result(result)


@mcp.tool()
def guard_search_files(path: str, pattern: str) -> list[dict[str, Any]]:
    """Search *path* recursively for files whose names match *pattern*."""
    target = _check(path, require_write=False)
    if not target.exists():
        raise FileNotFoundError(f"not found: {target}")
    if not target.is_dir():
        raise ValueError(f"not a directory: {target}")
    matches: list[dict[str, Any]] = []
    skip_dirs = {".git", ".perun", "node_modules", ".venv", "venv", "__pycache__"}
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d.lower() not in skip_dirs]
        for name in files:
            p = Path(root) / name
            if path_guard.is_sensitive_path(p):
                continue
            if pattern.lower() in name.lower():
                matches.append({"path": str(p), "name": name})
    return matches[:100]


if __name__ == "__main__":
    mcp.run(transport="stdio")
