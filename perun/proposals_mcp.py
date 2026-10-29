#!/usr/bin/env python3
"""PerunChat proposal coordination MCP server.

Exposes lifecycle tools for shared-project action proposals so the PerunChat
agent can propose, approve, reject and query proposals through structured MCP
calls instead of brittle text markers.

The server is stateless. The current chat identity is passed via environment:

* ``PERUN_PROJECT_PATH`` - project root (required).
* ``PERUN_CHAT_SENDER``  - agent id of the participant whose reply is running.
* ``PERUN_ROOM_ID``      - project chat room id (optional, for logging).

The server validates the sender against the project manifest/trust list exactly
like the bridge used to do for markers. The room owner is always authorized.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastmcp import FastMCP  # noqa: E402

from perun import (  # noqa: E402
    action_proposals,
    config,
    registry,
    shared_projects,
)

mcp = FastMCP("PerunProposals")


def _settings() -> Any:
    return config.settings()


def _project_path() -> Path:
    raw = os.environ.get("PERUN_PROJECT_PATH", "").strip()
    if not raw:
        # OpenCode launches project-local MCP servers with the project as cwd.
        # Fall back to it so the server is usable from the TUI even when the
        # generated opencode.json does not set PERUN_PROJECT_PATH explicitly.
        # A deleted cwd raises OSError; match guard_mcp and degrade to ".".
        try:
            raw = os.getcwd()
        except OSError:
            raw = "."
    p = Path(raw).resolve()
    if not p.is_dir():
        raise ValueError(f"project path does not exist: {p}")
    return p


ACTOR_TTL_SECONDS = 3600


def _actor_value(path: Path, key: str) -> str:
    """Return a fresh value from an actor file, or "" when unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = str(data.get(key) or "").strip()
        if not value:
            return ""
        raw_ts = str(data.get("updated_at") or "").strip()
        if not raw_ts:
            return ""
        try:
            stamp = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except ValueError:
            return ""
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if abs(age) > ACTOR_TTL_SECONDS:
            return ""
        return value
    except Exception:
        return ""


def _sender(room_id: str = "") -> str:
    """Return the human/agent who requested the change."""
    raw = os.environ.get("PERUN_CHAT_SENDER", "").strip()
    if raw:
        return raw
    room = (room_id or _room_id()).strip()
    if room:
        path = config.perunchat_actor_path(room)
        if path.is_file():
            sender = _actor_value(path, "sender")
            if sender:
                return sender
    raise ValueError(
        "PERUN_CHAT_SENDER is not set and no fresh room actor is available")


def _approver(room_id: str = "") -> str:
    """Return the agent identity that is actually calling approve/reject.

    The bridge records both the incoming sender (the requester) and its own
    agent id. The bot must approve as itself, not as the human who asked.
    """
    raw = os.environ.get("PERUN_CHAT_AGENT", "").strip()
    if raw:
        return raw
    room = (room_id or _room_id()).strip()
    if room:
        path = config.perunchat_actor_path(room)
        if path.is_file():
            agent = _actor_value(path, "agent")
            if agent:
                return agent
    raise ValueError(
        "PERUN_CHAT_AGENT is not set and no fresh room agent is available")


def _room_id() -> str:
    return os.environ.get("PERUN_ROOM_ID", "").strip()


def _find_project(project_path: Path) -> dict[str, Any]:
    """Look up the registered project by absolute path."""
    project = registry.get_project_by_path(str(project_path))
    if project is None:
        raise ValueError("project is not registered")
    return project


def _room_owner(project: dict[str, Any]) -> str:
    """Return the managed_by owner for a bound project chat, if any."""
    try:
        chat = registry.get_project_chat(int(project["id"])) or {}
    except Exception:
        return ""
    return str(chat.get("managed_by") or "").strip()


def _is_trusted(agent_id: str) -> bool:
    trusted = set(
        (a or "").strip().lower()
        for a in getattr(_settings(), "perunchat_trusted", []) or []
    )
    return agent_id.lower() in trusted


def _validate_participant(
    agent_id: str,
    project: dict[str, Any],
    cfg: dict[str, Any],
    *,
    require_owner: bool = False,
) -> tuple[bool, str]:
    """Return (is_owner, error_message)."""
    owner = _room_owner(project)
    is_owner = bool(owner) and agent_id.lower() == owner.lower()
    if require_owner and not is_owner:
        return False, f"{agent_id} is not the room owner"
    if not shared_projects.can_write(cfg, agent_id) and not is_owner:
        return False, f"{agent_id} has read-only access to this project"
    if not is_owner and not _is_trusted(agent_id):
        return False, f"{agent_id} is not on the PerunChat trust list"
    return is_owner, ""


def _proposal_summary(proposal: dict[str, Any]) -> str:
    summary = " ".join(str(proposal.get("intent") or "").split())[:120]
    tg = ", ".join(str(t) for t in (proposal.get("targets") or []))[:80]
    if tg:
        summary += f" -> {tg}"
    return summary


@mcp.tool()
def propose(
    intent: str,
    action_type: str,
    targets: list[str],
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a pending action proposal for the current project and sender."""
    project_path = _project_path()
    project = _find_project(project_path)
    cfg = shared_projects.load(project["path"])
    chat = registry.get_project_chat(int(project["id"])) or {}
    room_id = _room_id() or str(chat.get("room_id") or "")
    sender = _sender(room_id)

    _, error = _validate_participant(sender, project, cfg)
    if error:
        raise ValueError(error)

    envelope: dict[str, Any] = {
        "intent": intent,
        "action_type": action_type,
        "targets": targets,
        "details": details or {},
        "room_id": room_id,
        "manifest_rev": shared_projects.get_manifest_rev(cfg),
    }
    proposal = action_proposals.create(
        project,
        sender,
        envelope,
        owner=_room_owner(project),
        trusted=list(getattr(_settings(), "perunchat_trusted", []) or []),
    )
    if shared_projects.is_policy_auto_approve(project["path"]):
        executed = action_proposals.auto_approve_and_execute(project, proposal)
        if executed:
            proposal = executed
    return {
        "proposal_id": proposal["id"],
        "status": proposal["status"],
        "requester": proposal["requester"],
        "summary": _proposal_summary(proposal),
    }


@mcp.tool()
def approve(proposal_id: str, decision: str, reason: str = "",
            execute: bool = False) -> dict[str, Any]:
    """Approve or reject a pending proposal.

    Approval defaults to ``execute=False`` so the proposal stays in
    ``approved`` state for external claim or explicit local execution. Pass
    ``execute=True`` only when Perun itself should start the headless run.
    """
    project_path = _project_path()
    project = _find_project(project_path)
    proposal = action_proposals.get(proposal_id)
    if not proposal:
        raise ValueError("proposal not found")
    if proposal["project_id"] != project["id"]:
        raise ValueError("proposal does not belong to this project")
    chat = registry.get_project_chat(int(project["id"])) or {}
    room_id = (str(proposal.get("room_id") or "") or _room_id()
               or str(chat.get("room_id") or ""))
    approver = _approver(room_id)
    cfg = shared_projects.load(project["path"])

    _, error = _validate_participant(approver, project, cfg)
    if error:
        raise ValueError(error)

    if proposal["status"] != "pending":
        raise ValueError(f"proposal is {proposal['status']}")

    decision = str(decision or "").strip().lower()
    if decision == "approve":
        action_proposals.approve(proposal_id, approver)
        if execute:
            action_proposals.start_execution(proposal_id)
    elif decision == "reject":
        action_proposals.reject(proposal_id, approver, reason=reason)
    else:
        raise ValueError("decision must be 'approve' or 'reject'")

    fresh = action_proposals.get(proposal_id)
    return {
        "proposal_id": fresh["id"],
        "status": fresh["status"],
        "approved_by": fresh.get("approved_by"),
        "summary": _proposal_summary(fresh),
    }


@mcp.tool()
def list_proposals(status: str | None = None) -> list[dict[str, Any]]:
    """List proposals for the current project."""
    project_path = _project_path()
    project = _find_project(project_path)
    # Models sometimes serialize an omitted optional string as the literal
    # "null"/"none"; treat those as "no filter" instead of querying for a
    # status that can never exist.
    normalized = str(status).strip().lower() if status is not None else ""
    if normalized in ("", "null", "none", "any", "all"):
        status = None
    else:
        status = normalized
    proposals = action_proposals.list_by_project(project["id"], status=status)
    return [
        {
            "proposal_id": p["id"],
            "status": p["status"],
            "requester": p["requester"],
            "approved_by": p.get("approved_by"),
            "intent": p["intent"],
            "targets": p["targets"],
        }
        for p in proposals
    ]


@mcp.tool()
def get_proposal(proposal_id: str) -> dict[str, Any]:
    """Return the full state of a single proposal."""
    project_path = _project_path()
    project = _find_project(project_path)
    proposal = action_proposals.get(proposal_id)
    if not proposal:
        raise ValueError("proposal not found")
    if proposal["project_id"] != project["id"]:
        raise ValueError("proposal does not belong to this project")
    return {
        "proposal_id": proposal["id"],
        "status": proposal["status"],
        "requester": proposal["requester"],
        "approved_by": proposal.get("approved_by"),
        "room_id": proposal.get("room_id", ""),
        "executor_id": proposal.get("executor_id", ""),
        "executed_by": proposal.get("executed_by", ""),
        "execution_mode": proposal.get("execution_mode", "perun"),
        "intent": proposal["intent"],
        "targets": proposal["targets"],
        "result": proposal.get("result", ""),
        "details": proposal.get("details", {}),
    }


if __name__ == "__main__":
    mcp.run()
