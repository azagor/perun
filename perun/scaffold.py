"""Scaffold a brand-new project directory from the dashboard wizard.

Kept intentionally small: create the folder, then drop a minimal but *valid*
OpenCode project skeleton (``opencode.json``, ``AGENTS.md``, ``README.md``,
``.opencode/``, ``.gitignore``). Existing files are never overwritten.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import manifest


class ScaffoldError(Exception):
    """A user-facing failure while creating a project directory."""


def create_dir(path: str | Path) -> tuple[Path, bool]:
    """Create the directory (and parents). Returns ``(path, created)``."""
    p = Path(path).expanduser()
    if p.exists():
        if not p.is_dir():
            raise ScaffoldError(f"{p} exists and is not a directory")
        return p, False
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScaffoldError(f"could not create {p}: {exc}")
    return p, True


def _agents_md(name: str) -> str:
    return f"""# {name}

## Overview

{name} is an OpenCode project managed by **Perun**.

## Working rules

- Keep changes consistent with the structure and style of the surrounding code.
- Prefer small, focused edits; explain non-obvious decisions in the PR/commit.
- Do not commit secrets (tokens, keys, credentials).

## Perun

This project is registered with Perun, which provides a shared memory store,
semantic documentation search, and an agent console. Use the `perun_*` MCP tools
(or the Perun dashboard) to search across projects and to record durable facts.
"""


def _readme_md(name: str) -> str:
    return f"""# {name}

Getting started.

## Development

Add your build/run/test instructions here.
"""


_OPENCODE_JSON = {
    "$schema": "https://opencode.ai/config.json",
}

_GITIGNORE = """\
# OS / editor
.DS_Store
Thumbs.db
.idea/
.vscode/

# Python
__pycache__/
*.py[cod]
.venv/
venv/

# Node
node_modules/
dist/
build/

# Env
.env
.env.*
"""


def bootstrap(path: str | Path, name: str) -> list[str]:
    """Write a minimal OpenCode project skeleton. Returns created file names."""
    p = Path(path)
    if not p.is_dir():
        raise ScaffoldError(f"path does not exist: {p}")
    created: list[str] = []

    def write(rel: str, content: str) -> None:
        f = p / rel
        if f.exists():
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        created.append(rel)

    write("opencode.json", json.dumps(_OPENCODE_JSON, indent=2) + "\n")
    write("AGENTS.md", _agents_md(name or p.name))
    write("README.md", _readme_md(name or p.name))
    write(".gitignore", _GITIGNORE)
    write(manifest.MANIFEST_MD, _manifest_md(name or p.name))
    opencode_dir = p / ".opencode"
    if not opencode_dir.exists():
        opencode_dir.mkdir(parents=True, exist_ok=True)
    return created


def _manifest_md(name: str) -> str:
    data = {
        "version": manifest.SCHEMA_VERSION,
        "name": name,
        "kind": "opencode",
        "description": f"{name} — managed by Perun.",
        "docs": {"readme": "README.md", "agents": "AGENTS.md"},
        "anchors": [
            {"label": "README", "kind": "doc", "path": "README.md"},
            {"label": "Agent guide", "kind": "doc", "path": "AGENTS.md"},
        ],
    }
    body = (f"# {name}\n\n"
            "This is the Perun project manifest. The YAML front matter drives the "
            "Perun dashboard (links, docs, commands, and anchors into project "
            "content); the Markdown below is shown as the project overview.\n")
    return manifest.dump(data, body)


def summary(path: str | Path) -> dict[str, Any]:
    """Cheap capabilities snapshot for a folder (used by the wizard)."""
    p = Path(path)
    if not p.exists():
        return {"exists": False, "path": str(p)}
    return {
        "exists": True,
        "is_dir": p.is_dir(),
        "path": str(p),
        "is_empty": p.is_dir() and not any(p.iterdir()),
        "git": (p / ".git").exists(),
        "opencode": (p / "opencode.json").exists() or (p / ".opencode").exists(),
        "agents_md": (p / "AGENTS.md").is_file(),
        "readme": (p / "README.md").is_file(),
    }
