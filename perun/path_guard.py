"""Path allowlist enforcement for shared projects.

The allowlist is defined by the project manifest:
  * the project root directory (read/write)
  * every path listed in ``shared.external_deps`` (read or write per its access)

This module provides proposal-time validation. Runtime enforcement inside the
Project Agent's tool loop is a later layer; for now the orchestrator prompt and
these checks bound the declared intent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import shared_projects


class PathGuardError(Exception):
    """A target path violates the project allowlist."""


def _normalize(path: str | Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(path)))
    return Path(expanded).resolve()


def _is_under(target: Path, parent: Path) -> bool:
    try:
        target.relative_to(parent)
        return True
    except ValueError:
        return False


def allowlist(project_path: str | Path, deps: list[dict[str, Any]]) -> list[tuple[Path, str]]:
    """Return the resolved allowlist as (path, access) tuples.

    Access is either ``rw`` or ``ro``. The project root is always ``rw``.
    """
    base = _normalize(project_path)
    entries: list[tuple[Path, str]] = [(base, "rw")]
    for dep in deps or []:
        raw = dep.get("path", "")
        if not raw:
            continue
        dep_path = _normalize(raw)
        if not dep_path.is_absolute():
            dep_path = (base / dep_path).resolve()
        access = str(dep.get("access") or "rw").strip().lower()
        if access not in ("rw", "ro"):
            access = "rw"
        entries.append((dep_path, access))
    return entries


def check(
    project_path: str | Path,
    target: str | Path,
    deps: list[dict[str, Any]] | None = None,
    *,
    require_write: bool = False,
) -> tuple[bool, str]:
    """Return (ok, reason) for *target* against the allowlist.

    Symlinks are resolved. Targets inside the project root are always allowed.
    Targets outside the root are allowed only if listed in ``external_deps``.
    """
    base = _normalize(project_path)
    target_path = _normalize(target)

    # If target is relative and doesn't exist yet, resolve it relative to base.
    if not target_path.is_absolute() and not (_normalize(project_path) / target).exists():
        target_path = (base / target).resolve()

    entries = allowlist(project_path, deps or [])
    for parent, access in entries:
        if _is_under(target_path, parent):
            if require_write and access == "ro":
                return False, f"{target_path} is read-only in the project manifest"
            return True, ""
    return False, f"{target_path} is outside the project allowlist"


def validate_targets(
    project_path: str | Path,
    targets: list[str],
    deps: list[dict[str, Any]] | None = None,
    *,
    require_write: bool = False,
) -> list[str]:
    """Return a list of error messages for any target outside the allowlist."""
    errors: list[str] = []
    for target in targets:
        ok, reason = check(project_path, target, deps, require_write=require_write)
        if not ok:
            errors.append(reason)
    return errors


def from_project(project: dict[str, Any]) -> list[tuple[Path, str]]:
    """Build an allowlist from a project registry dict and its manifest."""
    path = project.get("path") or ""
    cfg = shared_projects.load(path)
    return allowlist(path, cfg.get("external_deps", []))
