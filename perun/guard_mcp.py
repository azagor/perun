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


def _allowlist() -> tuple[Path, list[tuple[Path, str]]]:
    """Return (project_root, [(path, access), ...])."""
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
        return root, [(root, "rw"), *deps]

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
        )
    # Not a shared project: unrestricted pass-through.
    root = Path(project_path).resolve()
    return root, [(root, "rw")]


def _check(path: str, require_write: bool = False) -> Path:
    """Resolve *path* and validate it against the allowlist.

    Raises ValueError with a clear message if the path is disallowed.
    """
    root, entries = _allowlist()
    target = Path(path)
    if not target.is_absolute():
        target = (Path(os.getcwd()) / target).resolve()
    else:
        target = target.resolve()

    allowed = False
    for parent, access in entries:
        try:
            target.relative_to(parent)
        except ValueError:
            continue
        if require_write and access == "ro":
            raise ValueError(f"{target} is read-only in the project manifest")
        allowed = True
        break
    if not allowed:
        raise ValueError(f"{target} is outside the project allowlist")
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
    for root, _dirs, files in os.walk(target):
        for name in files:
            if pattern.lower() in name.lower():
                p = Path(root) / name
                matches.append({"path": str(p), "name": name})
    return matches[:100]


if __name__ == "__main__":
    mcp.run(transport="stdio")
