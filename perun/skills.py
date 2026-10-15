"""Discover OpenCode agent skills and tools available to a project.

Skills live in ``.opencode/skills/<name>/SKILL.md`` (project-local) and
``~/.config/opencode/skills/<name>/SKILL.md`` (global). Tools are the
built-in OpenCode tool set plus custom TypeScript/JavaScript definitions under
``.opencode/tools/`` and ``~/.config/opencode/tools/``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None  # type: ignore

from . import config


# Built-in OpenCode skills provided by the runtime. These IDs are reserved and
# must not be shadowed by user SKILL.md files, but they are always available for
# selection in tasks and the TUI.
BUILTIN_SKILLS: list[dict[str, Any]] = [
    {
        "name": "code-audit",
        "description": "Perform a production-readiness code audit on changes made in the current session. Checks thread safety, race conditions, security risks, error handling, resource leaks, test coverage, and documentation/traceability.",
        "builtin": True,
    },
    {
        "name": "android-design-guidelines",
        "description": "Material Design 3 and Android platform guidelines. Use when building Android apps with Jetpack Compose or XML layouts, implementing Material You, navigation, or accessibility.",
        "builtin": True,
    },
    {
        "name": "find-skills",
        "description": "Helps users discover and install agent skills when they ask questions like 'how do I do X', 'find a skill for X', or express interest in extending capabilities.",
        "builtin": True,
    },
]

# Built-in OpenCode tools. These are always available.
BUILTIN_TOOLS: list[dict[str, Any]] = [
    {"name": "bash", "description": "Run shell commands."},
    {"name": "edit", "description": "Edit existing files with exact replacements."},
    {"name": "write", "description": "Create or overwrite files."},
    {"name": "read", "description": "Read file contents."},
    {"name": "grep", "description": "Search file contents with regex."},
    {"name": "glob", "description": "Find files by glob pattern."},
    {"name": "lsp", "description": "Query LSP servers (experimental)."},
    {"name": "apply_patch", "description": "Apply patch files to the codebase."},
    {"name": "skill", "description": "Load an agent skill."},
    {"name": "webfetch", "description": "Fetch a web page by URL."},
    {"name": "websearch", "description": "Search the web."},
    {"name": "question", "description": "Ask the user a question."},
]


def _read_skill_manifest(md: Path) -> dict[str, Any] | None:
    """Parse a SKILL.md front matter; return None if it is not a valid skill."""
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return None
        front: list[str] = []
        for line in lines[1:]:
            if line.strip() == "---":
                break
            front.append(line)
        data = yaml.safe_load("\n".join(front)) or {} if yaml else {}
    except Exception:
        return None
    name = str(data.get("name") or "").strip()
    if not name:
        return None
    return {
        "name": name,
        "description": str(data.get("description") or "").strip(),
        "path": str(md),
    }


def discover_skills(project_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return all available OpenCode skills for a project.

    Skills are discovered from the global OpenCode config directory and from the
    project-local ``.opencode/skills`` directory. Each skill is represented by a
    ``SKILL.md`` file with YAML front matter containing ``name`` and ``description``.
    Project-local skills take precedence over global skills with the same name.
    Runtime built-in skills are always appended last and cannot be shadowed by
    disk-based skills.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _collect(root: Path, *, project_local: bool) -> None:
        if not root.is_dir():
            return
        for skill_dir in root.iterdir():
            if not skill_dir.is_dir():
                continue
            md = skill_dir / "SKILL.md"
            if not md.is_file():
                continue
            data = _read_skill_manifest(md)
            if not data or data["name"] in seen:
                continue
            seen.add(data["name"])
            data["project_local"] = project_local
            out.append(data)

    if project_path:
        _collect(Path(project_path) / config.PROJECT_SKILLS_DIR, project_local=True)
    _collect(config.GLOBAL_SKILLS_DIR, project_local=False)

    # Append runtime built-ins that are not already provided on disk.
    for s in BUILTIN_SKILLS:
        if s["name"] not in seen:
            seen.add(s["name"])
            out.append({**s, "project_local": False})

    return out


def _tool_name_from_path(path: Path) -> str:
    return path.stem


def _export_names(text: str) -> list[str]:
    """Cheap scan for ``export default`` and ``export const name = tool(...)`` exports."""
    names: list[str] = []
    for line in text.splitlines():
        if re.search(r"^\s*export\s+default\b", line):
            names.append("default")
            continue
        m = re.match(r"^\s*export\s+(?:const\s+)?(\w+)\s*=", line)
        if m:
            names.append(m.group(1))
    return names


def discover_tools(project_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return all tools available to OpenCode for a project.

    Combines the documented built-in tool set with custom TypeScript/JavaScript
    tool definitions found under ``~/.config/opencode/tools/`` and the project-local
    ``.opencode/tools/`` directory. Multiple named exports in one file become
    ``filename_exportname`` tools.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for t in BUILTIN_TOOLS:
        seen.add(t["name"])
        out.append({**t, "project_local": False, "builtin": True})

    def _collect(root: Path, *, project_local: bool) -> None:
        if not root.is_dir():
            return
        for path in root.iterdir():
            if path.is_file() and path.suffix in (".ts", ".js", ".mjs"):
                base = _tool_name_from_path(path)
                names: list[str] = []
                has_default = False
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    extra = _export_names(text)
                    # Detect a default export so the file itself becomes a tool.
                    if "default" in extra or base in extra:
                        has_default = True
                    named = [e for e in extra if e not in ("default", "tool", base)]
                    if has_default:
                        names.append(base)
                    for n in named:
                        names.append(f"{base}_{n}")
                    # If no recognizable exports, fall back to the file name.
                    if not names:
                        names.append(base)
                except OSError:
                    names = [base]
                for name in names:
                    if name in seen:
                        continue
                    seen.add(name)
                    out.append({
                        "name": name,
                        "description": f"Custom tool from {path.name}.",
                        "path": str(path),
                        "project_local": project_local,
                        "builtin": False,
                    })

    if project_path:
        _collect(Path(project_path) / ".opencode" / "tools", project_local=True)
    _collect(config.OPENCODE_CONFIG_DIR / "tools", project_local=False)
    return out
