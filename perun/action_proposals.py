"""Shared-project action proposal lifecycle.

A proposal is a chat-originated intent to change something in the project
workspace. It is created by the PerunChat bridge, approved/rejected by the
project owner (or delegated approver), and executed by the Project Agent under
the Path Guard allowlist.

The approval token is a short-lived HMAC bound to the proposal id and execution
details, issued only when an owner approves.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import config, db, fs_audit, hitl, path_guard, registry, shared_projects

logger = logging.getLogger(__name__)

STATUSES = {"pending", "approved", "rejected", "expired", "executed", "failed"}
DEFAULT_PENDING_TTL_SECONDS = 24 * 3600


def _now() -> str:
    return registry.now_iso()


def _to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _from_json(text: str, default: Any = None) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return default if default is not None else []


def create(
    project: dict[str, Any],
    requester: str,
    envelope: dict[str, Any],
) -> dict[str, Any]:
    """Create a pending action proposal from a validated Action Envelope.

    The envelope must contain at least ``intent`` and ``targets``. All targets
    are checked against the project allowlist; if any fail, a ValueError is
    raised with the reasons.
    """
    pid = int(project["id"])
    path = project["path"]
    cfg = shared_projects.load(path)

    # Enforce manifest-level permissions: only rw participants may propose
    # mutations. This protects both the HTTP API and the PerunChat bridge path.
    if not shared_projects.can_write(cfg, requester):
        raise ValueError(f"participant {requester} has read-only access to this project")

    action_type = str(envelope.get("action_type") or envelope.get("type") or "unknown").strip()
    intent = str(envelope.get("intent") or "").strip()
    targets = [str(t).strip() for t in envelope.get("targets", []) if str(t).strip()]
    details = envelope.get("details") or envelope.get("proposed_steps") or {}
    room_id = str(envelope.get("room_id") or "").strip()
    manifest_rev = int(envelope.get("manifest_rev") or cfg.get("manifest_rev", 1))

    if not intent:
        raise ValueError("proposal intent is required")
    if not targets:
        raise ValueError("proposal targets are required")

    errors = path_guard.validate_targets(
        path, targets, cfg.get("external_deps", []), require_write=True
    )
    if errors:
        registry.log_activity(
            pid,
            "proposal_denied",
            f"Proposal from {requester} denied: target outside allowlist",
            {"requester": requester, "intent": intent, "targets": targets, "errors": errors},
        )
        raise ValueError("; ".join(errors))

    proposal_id = f"prop_{uuid.uuid4().hex[:12]}"
    ts = _now()
    expires_at = int(time.time()) + DEFAULT_PENDING_TTL_SECONDS
    with db.db() as conn:
        conn.execute(
            """INSERT INTO action_proposals
               (id, project_id, room_id, requester, action_type, intent, targets,
                details, manifest_rev, status, expires_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                proposal_id,
                pid,
                room_id,
                requester.lower(),
                action_type,
                intent,
                _to_json(targets),
                _to_json(details),
                manifest_rev,
                "pending",
                expires_at,
                ts,
                ts,
            ),
        )
    registry.log_activity(
        pid,
        "proposal_created",
        f"Proposal {proposal_id} created by {requester}: {intent}",
        {"proposal_id": proposal_id, "requester": requester, "intent": intent, "targets": targets},
    )
    return get(proposal_id)


def get(proposal_id: str) -> dict[str, Any] | None:
    expire_stale()
    with db.db() as conn:
        row = db.one(conn, "SELECT * FROM action_proposals WHERE id=?", (proposal_id,))
        return _row_to_dict(row) if row else None


def list_by_project(project_id: int, status: str | None = None) -> list[dict[str, Any]]:
    expire_stale()
    with db.db() as conn:
        if status:
            rows = db.rows(
                conn,
                "SELECT * FROM action_proposals WHERE project_id=? AND status=? ORDER BY created_at DESC",
                (project_id, status),
            )
        else:
            rows = db.rows(
                conn,
                "SELECT * FROM action_proposals WHERE project_id=? ORDER BY created_at DESC",
                (project_id,),
            )
    return [_row_to_dict(r) for r in rows]


def approve(proposal_id: str, approver: str) -> dict[str, Any]:
    """Approve a proposal and issue a signed HITL token."""
    proposal = get(proposal_id)
    if not proposal:
        raise ValueError("proposal not found")
    if proposal["status"] != "pending":
        raise ValueError(f"proposal is {proposal['status']}")

    engine = hitl.default_engine()
    execution_details = {
        "proposal_id": proposal_id,
        "project_id": proposal["project_id"],
        "action_type": proposal["action_type"],
        "targets": proposal["targets"],
        "details": proposal["details"],
    }
    token = engine.issue(proposal_id, execution_details)
    ts = _now()
    with db.db() as conn:
        conn.execute(
            """UPDATE action_proposals SET status=?, approval_token=?, approval_expiry=?,
               approved_by=?, approved_at=?, updated_at=? WHERE id=?""",
            (
                "approved",
                _to_json(token),
                token["expiry_epoch"],
                approver.lower(),
                ts,
                ts,
                proposal_id,
            ),
        )
    proposal = get(proposal_id)
    if proposal:
        registry.log_activity(
            proposal["project_id"],
            "proposal_approved",
            f"Proposal {proposal_id} approved by {approver}",
            {"proposal_id": proposal_id, "approver": approver, "intent": proposal["intent"]},
        )
    return {**proposal, "approval_token": token}


def reject(proposal_id: str, approver: str, reason: str = "") -> dict[str, Any]:
    proposal = get(proposal_id)
    if not proposal:
        raise ValueError("proposal not found")
    if proposal["status"] != "pending":
        raise ValueError(f"proposal is {proposal['status']}")
    ts = _now()
    with db.db() as conn:
        conn.execute(
            "UPDATE action_proposals SET status=?, approved_by=?, result=?, updated_at=? WHERE id=?",
            ("rejected", approver.lower(), reason, ts, proposal_id),
        )
    proposal = get(proposal_id)
    if proposal:
        registry.log_activity(
            proposal["project_id"],
            "proposal_rejected",
            f"Proposal {proposal_id} rejected by {approver}: {reason}",
            {"proposal_id": proposal_id, "approver": approver, "reason": reason},
        )
    return proposal


def expire_stale(max_age_seconds: int = DEFAULT_PENDING_TTL_SECONDS) -> list[str]:
    """Mark pending proposals whose *expires_at* has passed as expired."""
    now = int(time.time())
    expired_ids: list[str] = []
    with db.db() as conn:
        rows = db.rows(
            conn,
            "SELECT id, project_id FROM action_proposals WHERE status='pending' AND expires_at<?",
            (now,),
        )
        expired_ids = [r["id"] for r in rows]
        if expired_ids:
            placeholders = ",".join("?" * len(expired_ids))
            conn.execute(
                f"UPDATE action_proposals SET status='expired', updated_at=? WHERE id IN ({placeholders})",
                (_now(), *expired_ids),
            )
    for proposal_id in expired_ids:
        registry.log_activity(
            None,
            "proposal_expired",
            f"Proposal {proposal_id} expired after {max_age_seconds} seconds",
            {"proposal_id": proposal_id},
        )
    return expired_ids


def execute(proposal_id: str, orchestrator_instance: Any | None = None) -> dict[str, Any]:
    """Execute an approved proposal by dispatching it to the Project Agent.

    Returns the updated proposal with run result. The caller is responsible for
    posting the State Digest to the project chat.
    """
    from . import orchestrator as orchestrator_module

    proposal = get(proposal_id)
    if not proposal:
        raise ValueError("proposal not found")
    if proposal["status"] != "approved":
        raise ValueError(f"proposal is {proposal['status']}")

    project = registry.get_project(proposal["project_id"])
    if not project:
        raise ValueError("project not found")

    # Verify the approval token is still valid before execution.
    engine = hitl.default_engine()
    token_raw = proposal.get("approval_token")
    expiry = proposal.get("approval_expiry")
    if not token_raw or not expiry:
        _set_status(proposal_id, "failed", "missing approval token")
        raise ValueError("missing approval token")

    token = _from_json(token_raw) if isinstance(token_raw, str) else token_raw
    if not isinstance(token, dict):
        _set_status(proposal_id, "failed", "corrupt approval token")
        raise ValueError("corrupt approval token")

    # Reconstruct the details the token was issued against.
    execution_details = {
        "proposal_id": proposal_id,
        "project_id": proposal["project_id"],
        "action_type": proposal["action_type"],
        "targets": proposal["targets"],
        "details": proposal["details"],
    }
    ok, reason = engine.validate(
        token=token.get("token", ""),
        proposal_id=proposal_id,
        execution_details=execution_details,
        expiry_epoch=int(expiry),
        nonce=token.get("nonce", ""),
    )
    if not ok:
        _set_status(proposal_id, "expired" if reason == "expired" else "failed", reason)
        raise ValueError(f"approval token invalid: {reason}")

    # Build the Project Agent prompt and the filesystem allowlist env.
    prompt = _build_execution_prompt(proposal, project)
    allowlist_env = _build_allowlist_env(project)
    before_snapshot = fs_audit.snapshot(project)

    settings = config.Settings.load()
    orch = orchestrator_instance or orchestrator_module.Orchestrator(settings)
    run_uid = proposal_id
    run_error = None
    try:
        result = orch.run_and_wait(
            project["slug"],
            prompt,
            model=project.get("model", ""),
            agent=project.get("agent", ""),
            timeout=settings.run_timeout or 1800,
            extra_env={"PERUN_ALLOWLIST": allowlist_env},
        )
        run = result.get("run") or {}
        run_uid = run.get("run_uid") or proposal_id
        output = (result.get("reply") or run.get("output") or "").strip()
        status = "executed" if run.get("status") == "completed" else "failed"
    except Exception as exc:
        logger.exception("Failed to execute proposal %s", proposal_id)
        run_error = str(exc)
        status = "failed"
        output = run_error[:2000]

    # Post-run filesystem audit.
    try:
        audit_result = fs_audit.diff(project, before_snapshot)
        fs_audit.log_result(project["id"], run_uid, audit_result)
        if audit_result.get("violations"):
            output += (
                f"\n\n[WARNING: {len(audit_result['violations'])} file(s) touched outside the allowlist]"
            )
    except Exception as exc:
        logger.warning("Filesystem audit failed for proposal %s: %s", proposal_id, exc)
        audit_result = {"error": str(exc)}

    _set_status(proposal_id, status, output[:4000])

    proposal = get(proposal_id)
    if proposal:
        registry.log_activity(
            proposal["project_id"],
            "proposal_executed",
            f"Proposal {proposal_id} executed with status {proposal['status']}",
            {"proposal_id": proposal_id, "status": proposal["status"],
             "result": proposal.get("result", "")[:200]},
        )
    return proposal


def _build_allowlist_env(project: dict[str, Any]) -> str:
    """Serialize the project allowlist for the guard MCP server."""
    cfg = shared_projects.load(project["path"])
    root = Path(project["path"]).resolve()
    deps: list[dict[str, str]] = []
    for dep in cfg.get("external_deps", []):
        raw = dep.get("path", "")
        if not raw:
            continue
        expanded = os.path.expandvars(os.path.expanduser(str(raw)))
        dep_path = Path(expanded)
        if not dep_path.is_absolute():
            dep_path = (root / dep_path).resolve()
        else:
            dep_path = dep_path.resolve()
        deps.append({"path": str(dep_path), "access": dep.get("access", "rw")})
    return json.dumps({"root": str(root), "deps": deps}, ensure_ascii=False)


def _set_status(proposal_id: str, status: str, result: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    with db.db() as conn:
        conn.execute(
            "UPDATE action_proposals SET status=?, result=?, updated_at=? WHERE id=?",
            (status, result, _now(), proposal_id),
        )


def _build_execution_prompt(proposal: dict[str, Any], project: dict[str, Any]) -> str:
    cfg = shared_projects.load(project["path"])
    allowlist_summary = "project root (rw)"
    deps = cfg.get("external_deps", [])
    if deps:
        allowlist_summary += ", " + ", ".join(
            f"{d.get('name') or d.get('path')} ({d.get('access', 'rw')})" for d in deps
        )
    targets = proposal.get("targets", [])
    details = proposal.get("details", {})
    return (
        f"You are working on the shared project '{project['name']}' ({project['slug']}).\n"
        f"Request from {proposal['requester']}: {proposal['intent']}\n"
        f"Allowed paths: {allowlist_summary}\n"
        f"You may only read or write these target paths: {targets}\n"
        f"Additional details: {json.dumps(details, ensure_ascii=False)}\n"
        "Use the guard_* MCP tools (guard_read_file, guard_write_file, guard_edit_file, "
        "guard_list_directory, guard_search_files) for all filesystem operations. "
        "Do not use any other file tools. Do not access files outside the allowed paths. "
        "Execute the request, then summarise what changed."
    )


def _row_to_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["targets"] = _from_json(d.get("targets") or "[]", [])
    d["details"] = _from_json(d.get("details") or "{}", {})
    return d


def state_digest(proposal: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """Compose a compact State Digest for the project chat."""
    return {
        "type": "state_digest",
        "project": project["slug"],
        "proposal_id": proposal["id"],
        "action_type": proposal["action_type"],
        "outcome": proposal["status"],
        "targets": proposal["targets"],
        "intent": proposal["intent"],
        "notes": (proposal.get("result") or "")[:300],
    }
