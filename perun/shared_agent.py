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
  - action: "skill"
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
You are a restricted shared-project executor. Use only these prefixed guard MCP
tools: perun-guard_guard_read_file, perun-guard_guard_write_file,
perun-guard_guard_edit_file, perun-guard_guard_list_directory,
perun-guard_guard_search_files. The tool names are prefixed with the MCP server
name; use the exact names above. Do not call the `skill` tool — it is not a
filesystem tool. If a call accidentally goes to `skill` and returns
"Unable to load skill ...", immediately retry the same operation with the exact
`perun-guard_guard_*` tool name; never conclude the guard tools are missing from
that error. You cannot use shell, native file tools, web access, subagents, or
any other MCP server. Work only inside the allowlisted paths and the explicit
target paths supplied by the user. Never read credentials, .env files, VCS
metadata, or host configuration. When finished, summarise exactly what changed.
"""


def _agent_path() -> Path:
    from . import config

    try:
        return config.OPENCODE_CONFIG_DIR / "agents" / f"{SHARED_AGENT_ID}.md"
    except Exception:
        return Path.home() / ".config" / "opencode" / "agents" / f"{SHARED_AGENT_ID}.md"


# Public accessor so callers can check the file without importing the private name.
def agent_path() -> Path:
    return _agent_path()


def ensure_shared_agent() -> Path:
    """Install or refresh the restricted agent file.

    Best effort by design: a read-only home directory must not crash proposal
    execution, but the execution will fail safely because the agent denies all
    tools if the file cannot be installed.

    Writes the agent into both the modern ``agents/`` and the legacy ``agent/``
    subdirectories under the same OpenCode config base so either convention is
    satisfied. The canonical path from :func:`_agent_path` is returned.
    """
    try:
        canonical = _agent_path()
        # The canonical path is inside either ``agents/`` or ``agent/`` under a
        # single OpenCode config base. Mirror to the sibling subdirectory so
        # both conventions are covered without crossing into unrelated config
        # directories.
        base = canonical.parent.parent
        for name in ("agents", "agent"):
            target = base / name / f"{SHARED_AGENT_ID}.md"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.is_file() or target.read_text(encoding="utf-8").strip() != SHARED_AGENT_MD.strip():
                    target.write_text(SHARED_AGENT_MD, encoding="utf-8")
                    logger.info("Installed restricted shared-project agent at %s", target)
            except Exception:
                continue
        # Return the canonical path even if it could not be written; callers
        # check ``agent_path().is_file()`` and fail safely if installation did
        # not succeed. This keeps the public API stable for tests and for
        # execution preparation.
        return canonical
    except Exception as exc:  # pragma: no cover - environment dependent
        logger.warning("Could not install shared-project agent: %s", exc)
        return _agent_path()
