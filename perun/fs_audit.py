"""Filesystem audit helpers for shared-project agent runs.

The audit snapshots the allowed paths before a run and compares them after,
reporting any created/modified files. It also scans the project root's parent
for unexpected new top-level entries as a lightweight escape detector.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import path_guard, registry, shared_projects


def _stat_key(path: Path) -> tuple[int, float, int]:
    try:
        st = path.stat()
        return int(st.st_size), float(st.st_mtime), int(st.st_mode)
    except OSError:
        return (0, 0.0, 0)


def snapshot(project: dict[str, Any]) -> dict[str, Any]:
    """Capture file metadata for the project's allowlisted paths."""
    cfg = shared_projects.load(project["path"])
    entries = path_guard.from_project(project)
    files: dict[str, tuple[int, float, int]] = {}
    for root, _access in entries:
        if not root.exists():
            continue
        if root.is_file():
            key = _stat_key(root)
            if key != (0, 0.0, 0):
                files[str(root)] = key
        elif root.is_dir():
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    p = Path(dirpath) / name
                    key = _stat_key(p)
                    if key != (0, 0.0, 0):
                        files[str(p)] = key

    # Also snapshot parent directory entries to detect escapes.
    parent = Path(project["path"]).resolve().parent
    parent_entries = set(str(p) for p in parent.iterdir()) if parent.exists() else set()

    return {
        "project_slug": project["slug"],
        "files": files,
        "parent_entries": parent_entries,
    }


def _is_under(target: Path, parent: Path) -> bool:
    try:
        target.relative_to(parent)
        return True
    except ValueError:
        return False


def diff(project: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """Compare current filesystem state with the *before* snapshot."""
    after = snapshot(project)
    before_files = before.get("files", {})
    after_files = after.get("files", {})

    created = [p for p in after_files if p not in before_files]
    deleted = [p for p in before_files if p not in after_files]
    modified = []
    for p, before_key in before_files.items():
        after_key = after_files.get(p)
        if after_key and after_key != before_key:
            modified.append(p)

    # Detect new entries next to the project root.
    before_parent = before.get("parent_entries", set())
    after_parent = after.get("parent_entries", set())
    escaped = sorted(after_parent - before_parent)

    violations: list[str] = []
    cfg = shared_projects.load(project["path"])
    if cfg.get("shared"):
        allowlist_entries = path_guard.from_project(project)
        allowlist_paths = [Path(root).resolve() for root, _ in allowlist_entries]
        for p in created + modified:
            target = Path(p).resolve()
            ok = any(_is_under(target, root) for root in allowlist_paths)
            if not ok:
                violations.append(p)

    return {
        "created": sorted(created),
        "deleted": sorted(deleted),
        "modified": sorted(modified),
        "escaped": escaped,
        "violations": sorted(violations),
    }


def log_result(project_id: int, run_uid: str, result: dict[str, Any]) -> None:
    violation_count = len(result.get("violations", []))
    escape_count = len(result.get("escaped", []))
    if violation_count or escape_count:
        registry.log_activity(
            project_id,
            "fs_audit_violation",
            f"Run {run_uid}: {violation_count} allowlist violation(s), {escape_count} escape(s)",
            {"run_uid": run_uid, "violations": result.get("violations", []),
             "escaped": result.get("escaped", [])},
        )
    else:
        registry.log_activity(
            project_id,
            "fs_audit_ok",
            f"Run {run_uid}: filesystem audit clean",
            {"run_uid": run_uid, "created": len(result.get("created", [])),
             "modified": len(result.get("modified", [])),
             "deleted": len(result.get("deleted", []))},
        )
