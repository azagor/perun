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
    """Infer common commands from files present in the project.

    Supports multi-language projects: commands are collected from every detected
    stack instead of stopping at the first match.
    """
    p = Path(path)
    commands: dict[str, str] = {}
    has_python = any((p / f).is_file() for f in ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile"))
    has_node = (p / "package.json").is_file()
    has_rust = (p / "Cargo.toml").is_file()
    has_go = (p / "go.mod").is_file()
    has_java_gradle = any((p / f).is_file() for f in ("build.gradle", "build.gradle.kts"))
    has_java_maven = (p / "pom.xml").is_file()

    if has_node:
        commands["npm-install"] = "npm install"
        commands["npm-dev"] = "npm run dev"
        commands["npm-build"] = "npm run build"
        commands["npm-test"] = "npm test"
    if has_python:
        if (p / "requirements.txt").is_file():
            commands["pip-install"] = "python -m pip install -r requirements.txt"
        elif (p / "pyproject.toml").is_file():
            commands["pip-install"] = "python -m pip install -e ."
        commands["pytest"] = "python -m pytest"
    if has_rust:
        commands["cargo-build"] = "cargo build"
        commands["cargo-test"] = "cargo test"
    if has_go:
        commands["go-build"] = "go build ./..."
        commands["go-test"] = "go test ./..."
    if has_java_gradle:
        commands["gradle-build"] = "./gradlew build"
        commands["gradle-test"] = "./gradlew test"
    if has_java_maven:
        commands["maven-build"] = "mvn package"
        commands["maven-test"] = "mvn test"
    if (p / "Makefile").is_file():
        commands["make"] = "make"
    commands["git-status"] = "git status"
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
    # A pre-existing minimal manifest (shared-project bootstrap, older wizard)
    # has no docs/commands/anchors, so the Manifest tab shows nothing to open.
    enriched = enrich_manifest(p, project_name)
    if enriched.get("updated"):
        created.append(f"{manifest.MANIFEST_MD} (enriched)")
    return created


def _doc_links() -> dict[str, str]:
    """The standard documentation pointers a scaffolded project provides."""
    return {
        "readme": "README.md",
        "agents": "AGENTS.md",
        "changelog": "docs/CHANGELOG.md",
        "technical": "docs/TECHNICAL_DOCUMENTATION.md",
        "codemap": "docs/CODEMAP.md",
    }


def _standard_anchors() -> list[dict[str, str]]:
    """Doc anchors that mirror :func:`_doc_links`."""
    return [
        {"label": "README", "kind": "doc", "path": "README.md"},
        {"label": "Agent guide", "kind": "doc", "path": "AGENTS.md"},
        {"label": "Changelog", "kind": "doc", "path": "docs/CHANGELOG.md"},
        {
            "label": "Technical documentation",
            "kind": "doc",
            "path": "docs/TECHNICAL_DOCUMENTATION.md",
        },
        {"label": "CODEMAP", "kind": "doc", "path": "docs/CODEMAP.md"},
    ]


def enrich_manifest(path: str | Path, name: str = "") -> dict[str, Any]:
    """Fill missing docs/commands/anchors in an existing manifest.

    A minimal manifest (for example one written by the shared-project bootstrap
    or by an older dashboard) carries empty ``docs`` / ``commands`` / ``anchors``
    maps, which leaves the dashboard's Manifest tab with nothing to link to.
    Only what is missing is added: existing keys, values, body and shared
    project fields are preserved.

    Returns ``{"updated": bool, "changed": [...], "reason": str}``.
    """
    p = Path(path)
    if not p.is_dir():
        raise ScaffoldError(f"path does not exist: {p}")
    target = manifest.find(p)
    if target is None:
        return {"updated": False, "changed": [], "reason": "no manifest"}

    snap = manifest.load(p)
    if not snap["found"]:
        return {"updated": False, "changed": [], "reason": "could not parse manifest"}

    data: dict[str, Any] = dict(snap["data"])
    changed: list[str] = []

    if not str(data.get("name") or "").strip():
        data["name"] = name or p.name
        changed.append("name")
    if not str(data.get("kind") or "").strip():
        data["kind"] = "opencode"
        changed.append("kind")
    if "version" not in data or data.get("version") in (None, ""):
        data["version"] = manifest.SCHEMA_VERSION
        changed.append("version")

    docs = data.get("docs")
    if not isinstance(docs, dict):
        docs = {}
    added_docs = 0
    for key, rel in _doc_links().items():
        if docs.get(key):
            continue
        # Never point at a file that does not exist: a dead link is worse than
        # an absent one, and backfill() creates these before enriching.
        if not (p / rel).is_file():
            continue
        docs[key] = rel
        added_docs += 1
    if added_docs:
        data["docs"] = docs
        changed.append(f"docs ({added_docs})")

    commands = data.get("commands")
    if not isinstance(commands, dict):
        commands = {}
    added_commands = 0
    for key, cmd in _suggest_commands(p).items():
        if commands.get(key):
            continue
        commands[key] = cmd
        added_commands += 1
    if added_commands:
        data["commands"] = commands
        changed.append(f"commands ({added_commands})")

    anchors = data.get("anchors")
    if not isinstance(anchors, list):
        anchors = []
    known = {
        str(a.get("path") or "").strip()
        for a in anchors
        if isinstance(a, dict)
    }
    added_anchors = 0
    for anchor in _standard_anchors() + detect_anchors(p):
        rel = str(anchor.get("path") or "").strip()
        if not rel or rel in known:
            continue
        known.add(rel)
        anchors.append(anchor)
        added_anchors += 1
    if added_anchors:
        data["anchors"] = anchors
        changed.append(f"anchors ({added_anchors})")

    if not changed:
        return {"updated": False, "changed": [], "reason": "already complete"}

    try:
        manifest.save(p, data, body=snap.get("body") or "")
    except manifest.ManifestError as exc:  # pragma: no cover - disk failure
        raise ScaffoldError(str(exc))
    return {"updated": True, "changed": changed, "reason": ""}


def _read_text_file(path: Path) -> str:
    """Best-effort read of a text file; returns empty string on failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_package_json(path: Path) -> list[tuple[str, str | None]]:
    """Return (name, version_or_None) pairs from package.json."""
    try:
        data = json.loads(_read_text_file(path))
    except (json.JSONDecodeError, OSError):
        return []
    deps: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        if isinstance(data.get(key), dict):
            deps.update(data[key])
    return sorted((name, str(v) if v else None) for name, v in deps.items())


def _parse_requirements_txt(path: Path) -> list[tuple[str, str | None]]:
    """Return (name, specifier_or_None) pairs from requirements.txt."""
    out: list[tuple[str, str | None]] = []
    for line in _read_text_file(path).splitlines():
        line_stripped = line.strip()
        if not line_stripped or line_stripped.startswith("#") or line_stripped.startswith("-"):
            continue
        # Handle extras and version specifiers: name[extra]>=1.0
        name_part = line_stripped.split(";", 1)[0].strip()
        name_part = name_part.split("[", 1)[0].strip()
        spec = ""
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            if sep in name_part:
                spec = name_part.split(sep, 1)[1].strip()
                name_part = name_part.split(sep, 1)[0].strip()
                break
        if name_part:
            out.append((name_part, spec or None))
    return out


def _parse_toml_section(text: str, section_header: str) -> dict[str, Any]:
    """Tiny TOML parser for a single [section] table. Returns key/value dict."""
    result: dict[str, Any] = {}
    in_section = False
    bracket_depth = 0
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        stripped = line.strip()
        if stripped.startswith("["):
            if stripped == section_header or stripped.startswith(section_header + "."):
                in_section = True
                bracket_depth = stripped.count("[") - stripped.count("]")
            elif in_section:
                # Entered a different section at the same or lower depth.
                if bracket_depth <= 1:
                    in_section = False
            continue
        if not in_section:
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and not key.startswith("["):
            result[key] = value
    return result


def _parse_pyproject_toml(path: Path) -> list[tuple[str, str | None]]:
    """Return dependency names from pyproject.toml (PEP 621 + Poetry style)."""
    text = _read_text_file(path)
    deps: list[tuple[str, str | None]] = []
    # PEP 621 project.dependencies
    for name, spec in _parse_toml_section(text, "[project.dependencies]").items():
        deps.append((name, str(spec) if spec else None))
    for name, spec in _parse_toml_section(text, "[project.optional-dependencies]").items():
        deps.append((name, str(spec) if spec else None))
    # Poetry
    for name, spec in _parse_toml_section(text, "[tool.poetry.dependencies").items():
        if name == "python":
            continue
        deps.append((name, str(spec) if spec else None))
    return deps


def _parse_cargo_toml(path: Path) -> list[tuple[str, str | None]]:
    """Return dependency names from Cargo.toml."""
    text = _read_text_file(path)
    deps: list[tuple[str, str | None]] = []
    for section in ("[dependencies]", "[dev-dependencies]", "[build-dependencies]"):
        for name, spec in _parse_toml_section(text, section).items():
            deps.append((name, str(spec) if spec else None))
    return deps


def _parse_go_mod(path: Path) -> list[tuple[str, str | None]]:
    """Return (module, version) pairs from go.mod require blocks."""
    text = _read_text_file(path)
    deps: list[tuple[str, str | None]] = []
    in_require = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("require ("):
            in_require = True
            continue
        if in_require and s == ")":
            in_require = False
            continue
        if in_require or s.startswith("require "):
            parts = s.strip("()").split()
            if len(parts) >= 2 and not parts[0].startswith("//"):
                deps.append((parts[0], parts[1]))
    return deps


# Mapping of anchor file name → parser function.
DEPENDENCY_PARSERS: dict[str, Any] = {
    "package.json": _parse_package_json,
    "requirements.txt": _parse_requirements_txt,
    "pyproject.toml": _parse_pyproject_toml,
    "Cargo.toml": _parse_cargo_toml,
    "go.mod": _parse_go_mod,
}


def parse_dependencies(path: str | Path) -> dict[str, list[tuple[str, str | None]]]:
    """Parse known dependency files under *path* and return {file: [(name, version), ...]}."""
    p = Path(path)
    result: dict[str, list[tuple[str, str | None]]] = {}
    for rel, parser in DEPENDENCY_PARSERS.items():
        target = p / rel
        if target.is_file():
            try:
                result[rel] = parser(target)
            except Exception:  # pragma: no cover - defensive
                result[rel] = []
    return result


def update_manifest_anchors(path: str | Path) -> dict[str, Any]:
    """Append newly-detected dependency anchors to an existing ``perun.md``.

    Returns a status dict. The manifest is only rewritten when at least one new
    anchor is added; hand-written content and existing anchors are preserved.
    """
    p = Path(path)
    target = p / manifest.MANIFEST_MD
    if not target.is_file():
        return {"updated": False, "reason": "no perun.md manifest"}

    snap = manifest.load(p)
    if not snap["found"]:
        return {"updated": False, "reason": "could not parse manifest"}

    existing_paths = {str(a.get("path", "")).replace("\\", "/") for a in snap["data"].get("anchors", [])}
    new_anchors = detect_anchors(p)
    to_add = [a for a in new_anchors if a["path"] not in existing_paths]

    if not to_add:
        return {"updated": False, "added": 0, "reason": "no new anchors"}

    anchors = list(snap["data"].get("anchors", []))
    anchors.extend(to_add)
    snap["data"]["anchors"] = anchors

    try:
        target.write_text(manifest.dump(snap["data"], snap["body"]), encoding="utf-8")
    except OSError as exc:
        return {"updated": False, "reason": f"could not write manifest: {exc}"}

    return {"updated": True, "added": len(to_add), "anchors": to_add}


def validate_manifest(path: str | Path) -> dict[str, Any]:
    """Check a ``perun.md`` manifest for stale doc paths and anchors.

    Returns a dict with `ok`, `missing_docs`, and `missing_anchors` lists.
    """
    p = Path(path)
    target = p / manifest.MANIFEST_MD
    if not target.is_file():
        return {"ok": False, "missing_docs": [], "missing_anchors": [], "reason": "no perun.md"}

    snap = manifest.load(p)
    if not snap["found"]:
        return {"ok": False, "missing_docs": [], "missing_anchors": [], "reason": "could not parse manifest"}

    missing_docs: list[str] = []
    for label, rel in (snap["data"].get("docs") or {}).items():
        if not (p / rel).exists():
            missing_docs.append(f"docs.{label} -> {rel}")

    missing_anchors: list[str] = []
    for a in snap["data"].get("anchors", []):
        if a.get("kind") in ("file", "doc", "symbol"):
            rel = a.get("path", "")
            if rel and not (p / rel).exists():
                missing_anchors.append(f"{a.get('label')} -> {rel}")

    return {
        "ok": not missing_docs and not missing_anchors,
        "missing_docs": missing_docs,
        "missing_anchors": missing_anchors,
    }


def find_local_project_references(path: str | Path, projects: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Find references in *path* to other registered projects.

    Currently detects:
    - ``file:../<local-path>`` entries in ``package.json`` dependencies.
    - Local path references in ``requirements.txt`` (``-e ../path`` or ``./path``).
    """
    p = Path(path)
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    import os

    project_paths = {
        os.path.normcase(os.path.abspath(pr.get("path", ""))): pr
        for pr in projects
        if pr.get("path")
    }

    package_json = p / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(_read_text_file(package_json))
        except json.JSONDecodeError:
            data = {}
        for dep_type in ("dependencies", "devDependencies", "peerDependencies"):
            for name, spec in (data.get(dep_type) or {}).items():
                if not isinstance(spec, str) or not spec.startswith("file:"):
                    continue
                local = spec[5:].lstrip("/")
                try:
                    resolved = (p / local).resolve()
                except Exception:
                    continue
                resolved_norm = os.path.normcase(str(resolved))
                key = (resolved_norm, name)
                if key in seen:
                    continue
                seen.add(key)
                for proj_path_norm, proj in project_paths.items():
                    if resolved_norm == proj_path_norm:
                        refs.append({
                            "source": str(p),
                            "target_slug": proj["slug"],
                            "target_name": proj.get("name", proj["slug"]),
                            "via": f"package.json:{dep_type}:{name}",
                        })

    req = p / "requirements.txt"
    if req.is_file():
        for line in _read_text_file(req).splitlines():
            line = line.strip()
            if not line.startswith("-e ") and not line.startswith("./") and not line.startswith("../"):
                continue
            local = line[3:].strip() if line.startswith("-e ") else line
            try:
                resolved = (p / local).resolve()
            except Exception:
                continue
            resolved_norm = os.path.normcase(str(resolved))
            for proj_path_norm, proj in project_paths.items():
                if resolved_norm == proj_path_norm:
                    key = (resolved_norm, proj["slug"])
                    if key in seen:
                        continue
                    seen.add(key)
                    refs.append({
                        "source": str(p),
                        "target_slug": proj["slug"],
                        "target_name": proj.get("name", proj["slug"]),
                        "via": f"requirements.txt:{line}",
                    })

    return refs


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
