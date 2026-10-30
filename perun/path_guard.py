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


# Paths that must never be exposed to a shared-project run, even when they sit
# inside the project root. The guard MCP server cannot distinguish "the agent
# asked for .env" from "the agent asked for a legitimate target", so denial is
# by name and is deliberately conservative.
_SENSITIVE_DIR_NAMES = {
    ".git", ".perun", ".ssh", ".aws", ".gnupg", ".opencode", "node_modules",
    ".venv", "venv", "__pycache__",
}
_SENSITIVE_FILE_NAMES = {
    ".env", ".npmrc", ".pypirc", ".netrc", "credentials.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "perun.md", "perun.json", "opencode.json", "opencode.jsonc",
}
_SENSITIVE_SUFFIXES = (".pem", ".key", ".pfx", ".p12")


def is_sensitive_path(path: str | Path) -> bool:
    """Return True for credentials, VCS metadata, or host configuration."""
    try:
        parts = Path(path).parts
    except (TypeError, ValueError):
        return True
    for part in parts:
        lowered = part.lower().rstrip(" .")
        if lowered in _SENSITIVE_DIR_NAMES:
            return True
        if lowered in _SENSITIVE_FILE_NAMES:
            return True
        if lowered.endswith(_SENSITIVE_SUFFIXES):
            return True
        if lowered.startswith("secrets.") or lowered.startswith("secret."):
            return True
    return False


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
    Relative dependency paths are resolved against the project root, never
    against the process working directory.
    """
    base = _normalize(project_path)
    entries: list[tuple[Path, str]] = [(base, "rw")]
    for dep in deps or []:
        raw = dep.get("path", "")
        if not raw:
            continue
        expanded = os.path.expandvars(os.path.expanduser(str(raw)))
        dep_path = Path(expanded)
        if not dep_path.is_absolute():
            dep_path = (base / dep_path).resolve()
        else:
            dep_path = dep_path.resolve()
        access = str(dep.get("access") or "rw").strip().lower()
        if access not in ("rw", "ro"):
            access = "rw"
        entries.append((dep_path, access))
    return entries


def resolve_target(project_path: str | Path, target: str | Path) -> Path:
    """Resolve *target* relative to the project root, never the process CWD."""
    raw = str(target)
    if "$" in raw or "%" in raw or raw.startswith("~"):
        raise ValueError("proposal targets must not contain shell expansions")
    base = _normalize(project_path)
    target_path = Path(raw)
    if target_path.is_absolute():
        return target_path.resolve()
    return (base / target_path).resolve()


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
    A relative target is interpreted relative to the project root -- resolving
    it with ``Path.resolve()`` directly would silently use the process working
    directory and reject every legitimate chat-proposed change.

    Credentials, VCS metadata, and host configuration are denied even inside the
    root: sharing a project must not share the operator's secrets.
    """
    base = _normalize(project_path)
    target_path = resolve_target(project_path, target)

    entries = allowlist(project_path, deps or [])
    for parent, access in entries:
        if _is_under(target_path, parent):
            rel = target_path.relative_to(parent)
            if is_sensitive_path(rel):
                return False, f"{target_path} is a protected or sensitive path"
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
