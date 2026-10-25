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

from perun import path_guard, shared_projects  # noqa: E402

mcp = FastMCP("PerunGuard")


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
        return Path(project_path).resolve(), path_guard.allowlist(
            project_path, cfg.get("external_deps", [])
        ), []
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
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {target}"


@mcp.tool()
def guard_edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace *old_string* with *new_string* in *path*."""
    target = _check(path, require_write=True)
    if not target.exists():
        raise FileNotFoundError(f"not found: {target}")
    text = target.read_text(encoding="utf-8", errors="replace")
    if old_string not in text:
        raise ValueError("old_string not found in file")
    text = text.replace(old_string, new_string, 1)
    target.write_text(text, encoding="utf-8")
    return f"edited {target}"


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
