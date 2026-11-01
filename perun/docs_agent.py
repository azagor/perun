"""The restricted agent that runs ``/update-docs`` after a proposal executes.

Documentation is deliberately outside an approved proposal's targets, so Perun
writes it itself: a dedicated session in the project runs the installed
``/update-docs`` command. With the project's *default* agent that session could
edit any file with full tools — an approval of two source files quietly reached
everything in the repository.

``perun-docs`` is that session's identity: read the project, write documentation
(``docs/**``, ``AGENTS.md``, ``perun.md``) and nothing else. The file is managed
exactly like ``perun-orchestrator`` (write-once with sha256, refresh only while
untouched, reset on request).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from . import managed_agent

DOCS_AGENT_ID = "perun-docs"
# Bump when DOCS_AGENT_MD changes in a way users should receive.
AGENT_TEMPLATE_VERSION = 1

DOCS_AGENT_MD = """---
description: Perun docs writer - updates documentation only, never code
mode: primary
permissions:
  # Deny everything, then allow the documentation surface. OpenCode's last-match
  # rule means the specific allows must come after the blanket deny.
  - action: "*"
    resource: "*"
    effect: deny
  - action: "read"
    resource: "*"
    effect: allow
  - action: "glob"
    resource: "*"
    effect: allow
  - action: "grep"
    resource: "*"
    effect: allow
  - action: "list"
    resource: "*"
    effect: allow
  - action: "edit"
    resource: "docs/**"
    effect: allow
  - action: "write"
    resource: "docs/**"
    effect: allow
  - action: "edit"
    resource: "AGENTS.md"
    effect: allow
  - action: "write"
    resource: "AGENTS.md"
    effect: allow
  - action: "edit"
    resource: "perun.md"
    effect: allow
  - action: "write"
    resource: "perun.md"
    effect: allow
---

You are Perun's documentation writer for this project. A change was just
executed, and your only job is to bring the project's documentation in line with
the code that now exists.

Rules
- You may **read** anything in the project, but you may only **write**
  documentation: `docs/**`, `AGENTS.md`, and `perun.md`. Attempting anything else
  will be refused, and that is intended - do not try to work around it.
- Never edit source code, configuration, tests, or scripts. If the code and the
  docs disagree because the code is wrong, say so in the changelog entry instead
  of changing the code.
- Keep `docs/CHANGELOG.md` current: one bullet under `## Unreleased`, in the
  user's language, saying what changed and why - not a file list.
- Update `docs/CODEMAP.md` and `docs/TECHNICAL_DOCUMENTATION.md` only where the
  change actually made them wrong. Do not rewrite sections you did not verify.
- Never paste credentials, tokens or `.env` contents into a document.
- Answer in the user's language. Report which files you changed and stop.
"""


def agent_path() -> Path:
    return managed_agent.agent_path(DOCS_AGENT_ID)


def ensure(*, force: bool = False) -> str:
    """Install or refresh the docs agent; never raises, never overwrites an edit."""
    return managed_agent.ensure(
        DOCS_AGENT_ID, DOCS_AGENT_MD, AGENT_TEMPLATE_VERSION, force=force)


def reset() -> Dict[str, Any]:
    return managed_agent.reset(DOCS_AGENT_ID, DOCS_AGENT_MD, AGENT_TEMPLATE_VERSION)


def status() -> Dict[str, Any]:
    return managed_agent.status(DOCS_AGENT_ID, AGENT_TEMPLATE_VERSION)


def is_customized() -> bool:
    return managed_agent.is_customized(DOCS_AGENT_ID)


def available(agent_id: Optional[str] = None) -> bool:
    return managed_agent.available(agent_id or DOCS_AGENT_ID)
