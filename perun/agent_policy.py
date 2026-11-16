"""Which agents a surface may offer.

Default-deny: a request with no ``surface`` gets the **project** rules, so a
picker that forgets to name its surface fails closed instead of leaking Perun's
own agents into a project.

    project    built-ins + agents assigned to that project (or globally)
    control    perun-orchestrator + built-ins
    perunchat  perunchat only
    docs       perun-docs only
    all        no filtering (explicit, operator/debug use only)

Pure filtering: no file, manifest or OpenCode behaviour is touched.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from . import agents as agents_module


def builtin_ids() -> set[str]:
    return {str(a["id"]) for a in agents_module.BUILTIN_AGENTS}


def managed_ids() -> set[str]:
    """Agent ids Perun owns (templates plus the chat persona)."""
    from . import managed_agent

    try:
        ids = managed_agent.managed_ids()
    except Exception:  # pragma: no cover - state is best effort
        ids = set()
    return {str(i) for i in ids if i} | {"perunchat"}


def assigned_ids(project_slug: str = "") -> set[str]:
    """Roster agents placed globally or in *project_slug*."""
    from . import agent_catalog

    target = f"project:{project_slug}" if project_slug else ""
    placed: set[str] = set()
    for slug, assignments in agent_catalog.assignments_map().items():
        if "global" in assignments or (target and target in assignments):
            placed.add(slug)
    return placed


def allowed_ids(surface: str = "", project_slug: str = "") -> Optional[set[str]]:
    """The ids a surface may show; ``None`` means "no filtering"."""
    name = (surface or "project").strip().lower()
    if name == "all":
        return None
    if name == "control":
        return builtin_ids() | {"perun-orchestrator"}
    if name == "perunchat":
        return {"perunchat"}
    if name == "docs":
        return {"perun-docs"}
    # project (the default) and anything unrecognised
    return builtin_ids() | assigned_ids(project_slug)


def filter_agents(agents: Iterable[Dict[str, Any]], surface: str = "",
                  project_slug: str = "") -> List[Dict[str, Any]]:
    allowed = allowed_ids(surface, project_slug)
    rows = list(agents)
    if allowed is None:
        return rows
    return [row for row in rows if str(row.get("id") or "") in allowed]
