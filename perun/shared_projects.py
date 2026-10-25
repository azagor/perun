"""Shared Projects — collaborative workspaces bound to a PerunChat room.

A shared project is a normal Perun project whose manifest declares
``shared: true`` plus a participant list and optional external dependencies.
This module validates those declarations, resolves the filesystem allowlist,
and provides the helpers used by the HTTP API and the PerunChat bridge.

Design rules
------------
* The manifest (``perun.md``) is the source of truth for participants and
  external dependencies; the registry only caches a snapshot.
* All declared external agents must be on the local ``perunchat_trusted`` list
  before they can be saved.
* Paths are resolved relative to the project root and validated for existence
  at save time (best-effort; missing deps are rejected up front).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import manifest


DEFAULT_PARTICIPANT_ROLES = ["rw"]
DEFAULT_DEP_ACCESS = "rw"
VALID_ACCESS = {"rw", "ro"}


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "on")
    return bool(value)


def _normalize_participants(value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (participants, warnings)."""
    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    if value is None:
        return out, warnings
    if not isinstance(value, list):
        return out, ["participants must be a list"]
    for i, item in enumerate(value, 1):
        if isinstance(item, str):
            item = {"agent_id": item}
        if not isinstance(item, dict):
            warnings.append(f"participant #{i} is not an object")
            continue
        agent_id = str(item.get("agent_id") or "").strip().lower()
        if not agent_id:
            warnings.append(f"participant #{i} is missing agent_id")
            continue
        roles = _string_list(item.get("roles") or DEFAULT_PARTICIPANT_ROLES)
        roles = [r for r in roles if r]
        if not roles:
            roles = DEFAULT_PARTICIPANT_ROLES
        entry: dict[str, Any] = {"agent_id": agent_id, "roles": roles}
        display = str(item.get("display") or "").strip()
        if display:
            entry["display"] = display
        out.append(entry)
    return out, warnings


def _normalize_external_deps(value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (deps, warnings)."""
    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    if value is None:
        return out, warnings
    if not isinstance(value, list):
        return out, ["external_deps must be a list"]
    for i, item in enumerate(value, 1):
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            warnings.append(f"external_deps #{i} is not an object")
            continue
        name = str(item.get("name") or "").strip()
        path = str(item.get("path") or "").strip()
        if not path:
            warnings.append(f"external_deps #{i} is missing path")
            continue
        access = str(item.get("access") or DEFAULT_DEP_ACCESS).strip().lower()
        if access not in VALID_ACCESS:
            access = DEFAULT_DEP_ACCESS
            warnings.append(f"external_deps #{i} has invalid access; defaulting to rw")
        entry: dict[str, Any] = {"path": path, "access": access}
        if name:
            entry["name"] = name
        out.append(entry)
    return out, warnings


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def normalize(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate and normalize shared-project fields inside a manifest dict.

    Unknown keys are preserved (forward compatibility). Returns (data, warnings).
    """
    warnings: list[str] = []

    if "shared" in data:
        data["shared"] = _is_truthy(data["shared"])

    shared_enabled = data.get("shared", False)

    if "auto_approve" in data:
        data["auto_approve"] = _is_truthy(data["auto_approve"])

    participants, pw = _normalize_participants(data.get("participants"))
    deps, dw = _normalize_external_deps(data.get("external_deps"))
    warnings += pw + dw

    # Only materialise shared-project keys when the user already supplied them
    # or when shared mode is enabled, so non-shared manifests stay clean.
    if shared_enabled or "participants" in data:
        data["participants"] = participants
    if shared_enabled or "external_deps" in data:
        data["external_deps"] = deps

    has_shared_fields = shared_enabled or bool(participants) or bool(deps)
    rev = data.get("manifest_rev")
    if rev is not None or has_shared_fields:
        try:
            data["manifest_rev"] = int(rev) if rev is not None else 1
        except (TypeError, ValueError):
            data["manifest_rev"] = 1
            warnings.append("manifest_rev must be an integer; defaulting to 1")

    # If shared is disabled, keep the fields but also surface a warning if
    # participants remain. We do not silently discard user data.
    if not shared_enabled and (participants or deps):
        warnings.append("shared is false but participants/external_deps are present")

    return data, warnings


def is_shared(data: dict[str, Any] | None) -> bool:
    if not data:
        return False
    return _is_truthy(data.get("shared"))


def get_participants(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not data:
        return []
    value = data.get("participants")
    participants, _ = _normalize_participants(value)
    return participants


def get_participant_role(data: dict[str, Any] | None, agent_id: str) -> str | None:
    """Return the effective role for *agent_id*: ``'rw'``, ``'ro'``, or ``None``."""
    target = str(agent_id or "").strip().lower()
    for p in get_participants(data):
        if p["agent_id"] == target:
            roles = {str(r).strip().lower() for r in p.get("roles", [])}
            if "rw" in roles:
                return "rw"
            if "ro" in roles:
                return "ro"
    return None


def can_write(data: dict[str, Any] | None, agent_id: str) -> bool:
    """Whether *agent_id* has write access in this shared project."""
    return get_participant_role(data, agent_id) == "rw"


def is_auto_approve(project_path: str | Path) -> bool:
    """Effective auto-approve setting for a project.

    The per-project manifest flag ``shared.auto_approve`` overrides the global
    ``shared_auto_approve`` default. When neither is set, autonomous mode is
    disabled.
    """
    snap = manifest.load(project_path)
    data = snap.get("data") or {}
    if "auto_approve" in data:
        return _is_truthy(data["auto_approve"])
    from . import config

    return config.Settings.load().shared_auto_approve


def get_external_deps(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not data:
        return []
    value = data.get("external_deps")
    deps, _ = _normalize_external_deps(value)
    return deps


def get_manifest_rev(data: dict[str, Any] | None) -> int:
    if not data:
        return 1
    rev = data.get("manifest_rev")
    try:
        return int(rev) if rev is not None else 1
    except (TypeError, ValueError):
        return 1


def bump_manifest_rev(data: dict[str, Any]) -> int:
    """Bump and return the manifest revision. Mutates *data* in place."""
    rev = get_manifest_rev(data) + 1
    data["manifest_rev"] = rev
    return rev


def validate_participants(
    participants: list[dict[str, Any]], trusted: list[str]
) -> list[str]:
    """Return a list of human-readable errors for participants not on the trust list.

    *trusted* is the raw list (case-insensitive comparison).
    """
    if not participants:
        return []
    trusted_set = {str(t).strip().lower() for t in (trusted or []) if str(t).strip()}
    errors: list[str] = []
    for p in participants:
        agent_id = p.get("agent_id", "")
        if agent_id and agent_id not in trusted_set:
            errors.append(f"'{agent_id}' is not on the PerunChat trust list")
    return errors


def resolve_allowlist(project_path: str | Path, deps: list[dict[str, Any]]) -> list[Path]:
    """Return absolute, normalized Paths for the project root + external deps."""
    base = Path(project_path).resolve()
    paths: list[Path] = [base]
    for dep in deps:
        raw = dep.get("path", "")
        if not raw:
            continue
        expanded = os.path.expandvars(os.path.expanduser(str(raw)))
        dep_path = Path(expanded)
        if not dep_path.is_absolute():
            dep_path = (base / dep_path).resolve()
        else:
            dep_path = dep_path.resolve()
        paths.append(dep_path)
    return paths


def validate_external_deps(project_path: str | Path, deps: list[dict[str, Any]]) -> list[str]:
    """Return errors for deps that do not exist or fall inside the project root."""
    base = Path(project_path).resolve()
    errors: list[str] = []
    for dep in deps:
        raw = dep.get("path", "")
        if not raw:
            continue
        expanded = os.path.expandvars(os.path.expanduser(str(raw)))
        dep_path = Path(expanded)
        if not dep_path.is_absolute():
            dep_path = (base / dep_path).resolve()
        else:
            dep_path = dep_path.resolve()
        try:
            dep_path.relative_to(base)
            errors.append(
                f"external dep '{dep.get('name') or raw}' resolves inside the project root"
            )
            continue
        except ValueError:
            pass
        if not dep_path.exists():
            errors.append(f"external dep '{dep.get('name') or raw}' does not exist: {dep_path}")
    return errors


def load(project_path: str | Path) -> dict[str, Any]:
    """Load the shared-project section from a project's manifest."""
    snap = manifest.load(project_path)
    data = snap.get("data") or {}
    out, _ = normalize(data)
    return {
        "shared": out.get("shared", False),
        "auto_approve": out.get("auto_approve", False),
        "participants": out.get("participants", []),
        "external_deps": out.get("external_deps", []),
        "manifest_rev": out.get("manifest_rev", 1),
    }


def apply_payload(
    project_path: str | Path,
    payload: dict[str, Any],
    *,
    trusted: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Merge a shared-project payload into the on-disk manifest.

    Returns (updated shared config, errors). On validation failure the manifest
    is **not** written.
    """
    base = Path(project_path)
    snap = manifest.load(base)
    data = snap.get("data") or {}
    body = snap.get("body") or ""

    participants, _ = _normalize_participants(payload.get("participants"))
    deps, _ = _normalize_external_deps(payload.get("external_deps"))

    errors = validate_participants(participants, trusted or [])
    if not errors:
        errors = validate_external_deps(base, deps)

    if errors:
        return load(base), errors

    data["shared"] = _is_truthy(payload.get("shared", True))
    data["auto_approve"] = _is_truthy(payload.get("auto_approve", False))
    data["participants"] = participants
    data["external_deps"] = deps
    bump_manifest_rev(data)

    manifest.save(base, data, body)
    return load(base), []


def remove_participants(
    project_path: str | Path, agent_ids: set[str] | list[str]
) -> tuple[dict[str, Any], list[str]]:
    """Remove participants from the manifest and bump its revision.

    Returns ``(updated_config, removed_agent_ids)``. This is used when an agent
    is revoked from the PerunChat trust list: the manifest is the source of
    truth, so leaving a revoked agent listed would make the dashboard and the
    bridge disagree about who is still allowed in the project room.
    """
    base = Path(project_path)
    snap = manifest.load(base)
    if not snap.get("found"):
        return load(base), []
    data = snap.get("data") or {}
    body = snap.get("body") or ""
    participants, _ = _normalize_participants(data.get("participants"))
    revoked = {
        str(agent_id).strip().lower()
        for agent_id in (agent_ids or [])
        if str(agent_id).strip()
    }
    if not revoked:
        return load(base), []
    kept = [p for p in participants if p["agent_id"] not in revoked]
    removed = [p["agent_id"] for p in participants if p["agent_id"] in revoked]
    if not removed:
        return load(base), []
    data["participants"] = kept
    bump_manifest_rev(data)
    manifest.save(base, data, body)
    return load(base), removed


def project_chat_room_id(tenant_id: str, project_slug: str) -> str:
    """Deterministic PerunChat group room id for a shared project."""
    return f"grp:{tenant_id}:{project_slug}:chat"
