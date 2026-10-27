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


_WALK_SKIP_DIRS = {".git", ".perun", "node_modules", ".venv", "venv", "__pycache__"}


def snapshot(
    project: dict[str, Any], *, targets: list[str] | None = None
) -> dict[str, Any]:
    """Capture file metadata for the project's allowlisted paths.

    ``targets`` narrows the write audit to the proposal's declared paths. It is
    stored in the snapshot so :func:`diff` uses the same scope even though the
    after-snapshot does not receive the argument.
    """
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
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d.lower() not in _WALK_SKIP_DIRS]
                for name in filenames:
                    p = Path(dirpath) / name
                    key = _stat_key(p)
                    if key != (0, 0.0, 0):
                        files[str(p)] = key

    # Snapshot the parent's immediate entries with metadata so a modified or
    # deleted sibling is detected, not only a newly created one. The project
    # directory itself is excluded: its mtime changes on every legitimate write.
    root = Path(project["path"]).resolve()
    parent = root.parent
    parent_entries: dict[str, tuple[int, float, int]] = {}
    if parent.exists():
        for entry in parent.iterdir():
            if entry.resolve() == root:
                continue
            key = _stat_key(entry)
            if key != (0, 0.0, 0):
                parent_entries[str(entry)] = key

    resolved_targets = [
        str(path_guard.resolve_target(project["path"], target))
        for target in (targets or [])
    ]
    return {
        "project_slug": project["slug"],
        "files": files,
        "parent_entries": parent_entries,
        "targets": resolved_targets,
    }


def _is_under(target: Path, parent: Path) -> bool:
    try:
        target.relative_to(parent)
        return True
    except ValueError:
        return False


def diff(project: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """Compare current filesystem state with the *before* snapshot."""
    def _key(value):
        # Snapshots may be round-tripped through JSON, turning tuples into lists.
        return tuple(value) if isinstance(value, (list, tuple)) else value

    after = snapshot(project)
    before_files = before.get("files", {})
    after_files = after.get("files", {})

    created = [p for p in after_files if p not in before_files]
    deleted = [p for p in before_files if p not in after_files]
    modified = []
    for p, before_key in before_files.items():
        after_key = after_files.get(p)
        if after_key and _key(after_key) != _key(before_key):
            modified.append(p)

    # Detect any created, modified, or deleted entry next to the project root.
    before_parent = before.get("parent_entries", {})
    after_parent = after.get("parent_entries", {})
    if isinstance(before_parent, (set, list, tuple)):
        before_parent = {str(p): None for p in before_parent}
    if isinstance(after_parent, (set, list, tuple)):
        after_parent = {str(p): None for p in after_parent}
    escaped = sorted(
        str(p)
        for p in set(before_parent) | set(after_parent)
        if _key(before_parent.get(p)) != _key(after_parent.get(p))
    )

    violations: list[str] = []
    cfg = shared_projects.load(project["path"])
    if cfg.get("shared"):
        allowlist_entries = path_guard.from_project(project)
        targets = [Path(t).resolve() for t in before.get("targets", [])]
        for p in created + modified + deleted:
            target = Path(p).resolve()
            matches = [
                (Path(root).resolve(), access)
                for root, access in allowlist_entries
                if _is_under(target, Path(root).resolve())
            ]
            allowed = False
            if matches:
                # The most specific (longest) allowlist entry wins, so a
                # read-only dependency nested under another entry stays RO.
                _root, access = max(matches, key=lambda item: len(str(item[0])))
                allowed = access == "rw"
            if allowed and targets:
                # The guard MCP server enforces this at runtime; the audit is
                # the backstop if a different tool path was used.
                allowed = any(_is_under(target, t) for t in targets)
            if not allowed:
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
