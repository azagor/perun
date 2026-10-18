"""Discover OpenCode agents available to a project.

OpenCode agents can be built-in (`build`, `plan`, `general`, `explore`) or
user-defined as Markdown files under `~/.config/opencode/agents/` (global) or
`.opencode/agents/` inside a project.  This module scans both locations and
returns a merged, ordered list.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import config

# Visible primary/subagents documented for OpenCode v2.
BUILTIN_AGENTS: list[dict[str, Any]] = [
    {"id": "build", "name": "Build", "mode": "primary",
     "description": "Default coding agent with full tool access."},
    {"id": "plan", "name": "Plan", "mode": "primary",
     "description": "Planning agent; file edits denied except plan files."},
    {"id": "general", "name": "General", "mode": "subagent",
     "description": "General-purpose research and multi-step work."},
    {"id": "explore", "name": "Explore", "mode": "subagent",
     "description": "Read-only code and web exploration."},
]


# Regex for YAML-ish frontmatter used by OpenCode agent Markdown files.
# We intentionally keep it lightweight: only `description`, `mode`, and a
# top-level `name` are useful for the dropdown.
_NAME_RE = re.compile(r"^name\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_DESC_RE = re.compile(r"^description\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_MODE_RE = re.compile(r"^mode\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _agent_id_from_path(base: Path, agent_file: Path) -> str:
    rel = agent_file.relative_to(base).with_suffix("").as_posix()
    return rel


def _parse_frontmatter(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    in_frontmatter = False
    for line in text.splitlines()[:80]:
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            break
        if not in_frontmatter:
            continue
        for key, regex in (("name", _NAME_RE), ("description", _DESC_RE), ("mode", _MODE_RE)):
            m = regex.search(line)
            if m:
                out[key] = m.group(1).strip().strip('"').strip("'")
    return out


def _scan_agent_dir(base: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not base.exists():
        return out
    for path in base.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        aid = _agent_id_from_path(base, path)
        out.append({
            "id": aid,
            "name": fm.get("name") or aid,
            "mode": fm.get("mode") or "all",
            "description": fm.get("description") or "Custom agent",
        })
    return out


def list_agents(project_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return all agents available to the current installation / project.

    Order: built-ins first, then global user agents, then project-local agents.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def add(agents: list[dict[str, Any]]) -> None:
        for a in agents:
            aid = a["id"]
            if aid in seen:
                continue
            seen.add(aid)
            out.append(a)

    add(BUILTIN_AGENTS)
    # OpenCode historically used ``agent/`` (singular); newer versions also
    # accept ``agents/``. Scan both so custom agents show up either way.
    add(_scan_agent_dir(config.OPENCODE_CONFIG_DIR / "agents"))
    add(_scan_agent_dir(config.OPENCODE_CONFIG_DIR / "agent"))
    if project_path:
        add(_scan_agent_dir(Path(project_path) / ".opencode" / "agents"))
        add(_scan_agent_dir(Path(project_path) / ".opencode" / "agent"))
    return out
