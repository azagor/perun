"""Scaffold a brand-new project directory from the dashboard wizard.

Kept intentionally small: create the folder, then drop a minimal but *valid*
OpenCode project skeleton (``opencode.json``, ``AGENTS.md``, ``README.md``,
``docs/CHANGELOG.md``, ``docs/TECHNICAL_DOCUMENTATION.md``, ``docs/CODEMAP.md``,
``.opencode/``, ``.gitignore``, ``perun.md``). Existing files are never
overwritten.

The generated ``AGENTS.md`` points agents at ``perun.md`` as the primary source
of truth and anchors the most important docs and dependency files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import manifest


class ScaffoldError(Exception):
    """A user-facing failure while creating a project directory."""


# Files that should appear as anchors because they describe dependencies,
# build steps, deployment, or CI. Ordered roughly by importance.
DEPENDENCY_ANCHOR_FILES: tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "poetry.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "CMakeLists.txt",
    "Makefile",
    "justfile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    "Jenkinsfile",
)

# Directories whose presence is worth anchoring (usually CI / workflows).
ANCHOR_DIRS: tuple[str, ...] = (
    ".github/workflows",
    ".circleci",
)


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


def detect_anchors(path: str | Path) -> list[dict[str, str]]:
    """Return anchor dicts for dependency/build/CI files that exist under *path*."""
    p = Path(path)
    anchors: list[dict[str, str]] = []
    seen: set[str] = set()

    for rel in DEPENDENCY_ANCHOR_FILES:
        if (p / rel).is_file() and rel not in seen:
            seen.add(rel)
            anchors.append({"label": rel, "kind": "file", "path": rel})

    for rel in ANCHOR_DIRS:
        target = p / rel
        if target.is_dir() and rel not in seen:
            seen.add(rel)
            anchors.append({"label": rel, "kind": "file", "path": rel})

    return anchors


def _capitalize_words(text: str) -> str:
    """Capitalize each word in a snake/kebab/camel token."""
    return " ".join(w.capitalize() for w in text.replace("-", " ").replace("_", " ").split())


def _agents_md(name: str, anchors: list[dict[str, str]], manifest_path: str = "perun.md") -> str:
    anchor_lines = "\n".join(
        f"- [{a['label']}]({a['path']})" for a in anchors
    )
    return f"""# {name}

## Project source of truth

This project's **primary source of truth** is [`{manifest_path}`]({manifest_path}).
Read it first when starting work here. It contains the project identity, docs
map, commands, and anchors to important files and dependencies.

## Project anchors

- [{manifest_path}]({manifest_path}) — primary source of truth
- [README](README.md)
- [Agent guide](AGENTS.md) — this file
- [Changelog](docs/CHANGELOG.md)
- [Technical documentation](docs/TECHNICAL_DOCUMENTATION.md)
- [CODEMAP](docs/CODEMAP.md)
{anchor_lines}

## Working rules

- Keep changes consistent with the structure and style of the surrounding code.
- Prefer small, focused edits; explain non-obvious decisions in the PR/commit.
- Do not commit secrets (tokens, keys, credentials).
- When in doubt, check `perun.md` for the canonical docs map and commands.

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


def _changelog_md(name: str) -> str:
    return f"""# {name} — Changelog

All notable changes are recorded here.

## Unreleased

"""


def _technical_documentation_md(name: str) -> str:
    return f"""# {name} — Technical Documentation

## Overview

Brief description of the project, its purpose, and the problem it solves.

## Stack & dependencies

List the main languages, frameworks, and runtime dependencies.

## Project structure

Describe the important directories and files. Keep this up to date as the
project evolves.

## Getting started

How to build, run, and test the project locally.

## Notes

Additional architectural decisions, conventions, and operational notes.
"""


def _codemap_md(name: str) -> str:
    return f"""# {name} — CODEMAP

A concise map of the codebase. List modules/files with their responsibilities
and key identifiers.

## Entry points

## Core modules

## Tests

## Configuration
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


def _suggest_commands(path: str | Path) -> dict[str, str]:
    """Infer a few common commands from files present in the project."""
    p = Path(path)
    commands: dict[str, str] = {}
    if (p / "package.json").is_file():
        commands["install"] = "npm install"
        commands["dev"] = "npm run dev"
        commands["build"] = "npm run build"
        commands["test"] = "npm test"
    if any((p / f).is_file() for f in ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile")):
        if (p / "requirements.txt").is_file():
            commands["install"] = "python -m pip install -r requirements.txt"
        elif (p / "pyproject.toml").is_file():
            commands["install"] = "python -m pip install -e ."
        commands["test"] = commands.get("test") or "python -m pytest"
    if (p / "Cargo.toml").is_file():
        commands["build"] = commands.get("build") or "cargo build"
        commands["test"] = commands.get("test") or "cargo test"
    if (p / "go.mod").is_file():
        commands["build"] = commands.get("build") or "go build ./..."
        commands["test"] = commands.get("test") or "go test ./..."
    if (p / "Makefile").is_file():
        commands["make"] = "make"
    commands["status"] = "git status"
    return commands


def _manifest_md(name: str, path: str | Path) -> str:
    p = Path(path)
    anchors = [
        {"label": "README", "kind": "doc", "path": "README.md"},
        {"label": "Agent guide", "kind": "doc", "path": "AGENTS.md"},
        {"label": "Changelog", "kind": "doc", "path": "docs/CHANGELOG.md"},
        {"label": "Technical documentation", "kind": "doc", "path": "docs/TECHNICAL_DOCUMENTATION.md"},
        {"label": "CODEMAP", "kind": "doc", "path": "docs/CODEMAP.md"},
    ]
    anchors.extend(detect_anchors(p))

    data: dict[str, Any] = {
        "version": manifest.SCHEMA_VERSION,
        "name": name,
        "kind": "opencode",
        "description": f"{name} — managed by Perun.",
        "docs": {
            "readme": "README.md",
            "agents": "AGENTS.md",
            "changelog": "docs/CHANGELOG.md",
            "technical": "docs/TECHNICAL_DOCUMENTATION.md",
            "codemap": "docs/CODEMAP.md",
        },
        "commands": _suggest_commands(p),
        "anchors": anchors,
    }
    body = (f"# {name}\n\n"
            "This is the Perun project manifest. The YAML front matter drives the "
            "Perun dashboard (links, docs, commands, and anchors into project "
            "content); the Markdown below is shown as the project overview.\n")
    return manifest.dump(data, body)


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
    write("README.md", _readme_md(name or p.name))
    write("AGENTS.md", _agents_md(name or p.name, detect_anchors(p)))
    write(manifest.MANIFEST_MD, _manifest_md(name or p.name, p))
    write("docs/CHANGELOG.md", _changelog_md(name or p.name))
    write("docs/TECHNICAL_DOCUMENTATION.md", _technical_documentation_md(name or p.name))
    write("docs/CODEMAP.md", _codemap_md(name or p.name))
    write(".gitignore", _GITIGNORE)
    opencode_dir = p / ".opencode"
    if not opencode_dir.exists():
        opencode_dir.mkdir(parents=True, exist_ok=True)
    return created


def backfill(path: str | Path, name: str = "") -> list[str]:
    """Create missing scaffold files for an existing project. Returns created names.

    Unlike :func:`bootstrap`, this only writes files that are missing, so it is
    safe to run on existing projects. It also refreshes the generated anchors in
    ``AGENTS.md`` and ``perun.md`` only when those files do not already exist,
    preserving any hand-written content.
    """
    p = Path(path)
    if not p.is_dir():
        raise ScaffoldError(f"path does not exist: {p}")
    project_name = (name or p.name)
    created: list[str] = []

    def write(rel: str, content: str) -> None:
        f = p / rel
        if f.exists():
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        created.append(rel)

    write("opencode.json", json.dumps(_OPENCODE_JSON, indent=2) + "\n")
    write("README.md", _readme_md(project_name))
    write("AGENTS.md", _agents_md(project_name, detect_anchors(p)))
    write(manifest.MANIFEST_MD, _manifest_md(project_name, p))
    write("docs/CHANGELOG.md", _changelog_md(project_name))
    write("docs/TECHNICAL_DOCUMENTATION.md", _technical_documentation_md(project_name))
    write("docs/CODEMAP.md", _codemap_md(project_name))
    write(".gitignore", _GITIGNORE)
    opencode_dir = p / ".opencode"
    if not opencode_dir.exists():
        opencode_dir.mkdir(parents=True, exist_ok=True)
    return created


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
        "manifest": (p / manifest.MANIFEST_MD).is_file(),
        "changelog": (p / "docs" / "CHANGELOG.md").is_file(),
        "technical": (p / "docs" / "TECHNICAL_DOCUMENTATION.md").is_file(),
        "codemap": (p / "docs" / "CODEMAP.md").is_file(),
    }
