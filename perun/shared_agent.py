"""Restricted OpenCode agent used for approved shared-project execution.

The shared-project bridge is intentionally unable to edit files. Approved
proposals run headlessly under this agent, whose permissions allow only the
guard MCP server. Native shell/edit/read/search/web/subagent tools are denied,
so a prompt injection in a project file cannot turn into an arbitrary command.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SHARED_AGENT_ID = "perun-shared"

SHARED_AGENT_MD = """---
description: Restricted executor for approved shared-project changes (guard MCP only).
mode: primary
permissions:
  - action: "*"
    resource: "*"
    effect: deny
  - action: "perun_guard_*"
    resource: "*"
    effect: allow
  - action: "perun-guard_*"
    resource: "*"
    effect: allow
  - action: "guard_*"
    resource: "*"
    effect: allow
---
You are a restricted shared-project executor. Use only the guard_* MCP tools
(guard_read_file, guard_write_file, guard_edit_file, guard_list_directory,
guard_search_files) to inspect and change files. You cannot use shell, native
file tools, web access, subagents, or any other MCP server. Work only inside the
allowlisted paths and the explicit target paths supplied by the user. Never read
credentials, .env files, VCS metadata, or host configuration. When finished,
summarise exactly what changed.
"""


def _agent_path() -> Path:
    return Path.home() / ".config" / "opencode" / "agents" / f"{SHARED_AGENT_ID}.md"


# Public accessor so callers can check the file without importing the private name.
def agent_path() -> Path:
    return _agent_path()


def ensure_shared_agent() -> Path:
    """Install or refresh the restricted agent file.

    Best effort by design: a read-only home directory must not crash proposal
    execution, but the execution will fail safely because the agent denies all
    tools if the file cannot be installed.
    """
    try:
        target = _agent_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_text(encoding="utf-8").strip() != SHARED_AGENT_MD.strip():
            target.write_text(SHARED_AGENT_MD, encoding="utf-8")
            logger.info("Installed restricted shared-project agent at %s", target)
        return target
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("Could not install shared-project agent: %s", exc)
        return _agent_path()
