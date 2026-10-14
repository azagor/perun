"""The Perun control plane — a project-less orchestrator agent.

Historically Perun insisted on a *selected project* before an agent could be
reached, so a fresh install (no projects) or an ambiguous one (several, none
chosen) could not be talked to at all. The control plane fixes that: it is a
first-class agent session whose working directory is the **Perun repository**
itself and whose job is to *orchestrate* — list, describe, create, launch and
delete projects on the user's behalf.

The control project is the self-registered ``Perun`` entry
(:func:`perun.discovery.register_self`), identified by its path being
:data:`perun.config.REPO_ROOT`. Because it is keyed by path it always exists,
is never confused with a user project, and is protected from deletion.

This module is deliberately dependency-light (``config`` / ``discovery`` /
``registry``) so both the orchestrator and the Telegram bridge can import it.
"""

from __future__ import annotations

from typing import Any

from . import config, discovery, registry

# The identity injected into a control-plane session's first prompt. It is a
# *system-ish* preamble (not user instructions): it tells the agent which MCP
# tools actually work from here and how to behave. Kept short on purpose.
ORCHESTRATOR_INSTRUCTIONS = """\
You are **Perun**, the machine-wide overseer and orchestrator for every OpenCode
project. You are being talked to directly — no specific project is selected.

You can manage the whole machine from here through the `perun_*` MCP tools:

- `perun_list_projects` / `perun_project_info` — see what exists and its state.
- `perun_create_project` — register an existing folder, scaffold a brand-new
  project, or clone a git remote.
- `perun_update_project` — rename, retag, or set the model/agent/instructions.
- `perun_delete_project` — remove a project from the registry (ask first).
- `perun_discover` — rescan the configured roots for projects.
- `perun_start_session` / `perun_ask` — start work inside a project's own agent.
- `perun_run_status` — check on a run started there.
- `perun_memory_search` / `perun_memory_store` — cross-project memory.
- `perun_get_manifest` / `perun_set_manifest` — read/write a project's `perun.md`.
- `perun_telegram_send` — message the user out of band.

How to behave:

- Answer questions about the projects directly and concretely, using slugs the
  user will recognise. Never invent a project that is not in the registry.
- For anything destructive (create, rename, delete, overwrite), state exactly
  what you will do first and confirm when it is not obviously intended.
- A `perun_delete_project` removes the registry entry and Perun's memory for it;
  the files on disk are never touched — say so, and never imply otherwise.
- To do *work inside* a project, delegate with `perun_ask` /
  `perun_start_session` rather than editing that project's files from here.
- Keep replies short. Prefer a compact list over prose.
"""


def is_control(project: dict[str, Any] | None) -> bool:
    """True when *project* is the Perun repo (the control plane)."""
    return config.is_control_path((project or {}).get("path"))


def ensure() -> dict[str, Any]:
    """Return the control project, (re)registering it if needed.

    Idempotent and cheap: a registry lookup in the common case. It is safe to
    call on every service/bridge/MCP start so that "talk to Perun" always has a
    target even on a brand-new machine.
    """
    project, _created = discovery.register_self()
    return project


def resolve(ref: str | int | None = None) -> dict[str, Any]:
    """Return the project for *ref*, or the control project as the default."""
    if ref is not None:
        project = registry.get_project(ref)
        if project:
            return project
    return ensure()
