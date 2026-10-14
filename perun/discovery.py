"""Project discovery — find OpenCode-related projects under configured roots.

A directory is considered an *OpenCode project* when it carries any of the
framework markers (``.opencode/``, ``opencode.json``, ``AGENTS.md``,
``docs/CODEMAP.md``) or is simply a git working tree. Discovery is cheap: it
stats a handful of well-known paths per candidate directory and never recurses
into dependency/build directories.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import manifest, registry

SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", ".buildozer",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".idea", ".vscode",
    "site-packages", ".tox", "env", ".next", "target",
}

OPENCODE_MARKERS = [".opencode", "opencode.json", "opencode.jsonc"]
DOC_MARKERS = [
    "docs/CODEMAP.md",
    "docs/CHANGELOG.md",
    "docs/TECHNICAL_DOCUMENTATION.md",
]


def _read_description(path: Path) -> str:
    for rel in ("AGENTS.md", "README.md", "docs/README.md"):
        f = path / rel
        if not f.is_file():
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("<!--") or s.startswith("|"):
                continue
            return s[:240]
    return ""


def capabilities(path: Path) -> dict[str, Any]:
    def has(*rels: str) -> bool:
        return any((path / r).exists() for r in rels)

    return {
        "git": (path / ".git").exists(),
        "opencode": has(*OPENCODE_MARKERS),
        "agents_md": (path / "AGENTS.md").is_file(),
        "manifest": has(*manifest.MANIFEST_NAMES),
        "docs": has(*DOC_MARKERS),
        "codemap": (path / "docs" / "CODEMAP.md").is_file(),
        "memory_scripts": (path / "scripts" / "memory.py").is_file(),
        "telegram_bridge": (path / "scripts" / "telegram_bridge.py").is_file(),
        "mcp_memory": (path / "scripts" / "mcp_memory_server.py").is_file(),
        "build_ps1": (path / "scripts" / "build.ps1").is_file(),
        "package_json": (path / "package.json").is_file(),
        "tests": (path / "tests").is_dir(),
    }


def classify(path: Path) -> str:
    caps = capabilities(path)
    if caps["opencode"] or caps["agents_md"] or caps["codemap"] or caps["manifest"]:
        return "opencode"
    if caps["git"]:
        return "git"
    return "dir"


def detect(path: Path, root: str = "") -> dict[str, Any]:
    return {
        "name": path.name,
        "path": str(path),
        "root": root,
        "kind": classify(path),
        "description": _read_description(path),
        "capabilities": capabilities(path),
    }


def _candidate_dirs(root: Path, max_depth: int) -> list[Path]:
    out: list[Path] = []
    if not root.is_dir():
        return out

    def walk(d: Path, depth: int) -> None:
        try:
            entries = list(d.iterdir())
        except (OSError, PermissionError):
            return
        for entry in entries:
            if not entry.is_dir() or entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            out.append(entry)
            if depth < max_depth:
                walk(entry, depth + 1)

    walk(root, 1)
    return out


def scan(roots: list[str], *, max_depth: int = 1) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        rp = Path(root).expanduser()
        if not rp.is_dir():
            continue
        for cand in _candidate_dirs(rp, max_depth):
            key = os.path.normcase(str(cand.resolve()))
            if key in seen:
                continue
            seen.add(key)
            info = detect(cand, root=str(rp))
            if info["kind"] == "dir" and not info["description"]:
                # plain directory with nothing of interest — still list it so
                # the user can register it explicitly.
                pass
            found.append(info)
    found.sort(key=lambda i: (i["kind"] != "opencode", i["name"].lower()))
    return found


def register(info: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Insert or update a discovered project. Returns (project, created).

    A ``perun.md``/``perun.json`` manifest, when present, is authoritative for
    the fields it declares (name, kind, description, tags, model, agent,
    instructions, git) and its snapshot is stored for the dashboard.
    """
    existing = registry.get_project_by_path(info["path"])
    caps = info.get("capabilities", {})
    cap_tags = ",".join(sorted(k for k, v in caps.items() if v))

    man = manifest.load(info["path"])
    declared = manifest.to_project_fields(man["data"]) if man["found"] else {}

    name = declared.get("name") or info.get("name", Path(info["path"]).name)
    kind = declared.get("kind") or info.get("kind", "dir")
    description = declared.get("description") or info.get("description", "")
    tags = declared.get("tags") or cap_tags

    if existing:
        updated = registry.update_project(
            existing["slug"],
            name=name,
            root=info.get("root", existing.get("root", "")),
            kind=kind,
            description=description,
            tags=tags,
            **{k: v for k, v in declared.items()
               if k in ("model", "agent", "instructions", "git_remote", "git_branch")},
        )
        if man["found"]:
            registry.set_project_manifest(existing["slug"], manifest.snapshot(man),
                                          man["path"])
        registry.touch_project(existing["id"], seen=True)
        return updated or existing, False

    created = registry.add_project(
        name,
        info["path"],
        root=info.get("root", ""),
        kind=kind,
        description=description,
        tags=tags,
        model=declared.get("model", ""),
        agent=declared.get("agent", ""),
        instructions=declared.get("instructions", ""),
    )
    if declared.get("git_remote") or declared.get("git_branch"):
        registry.update_project(created["slug"],
                                git_remote=declared.get("git_remote", ""),
                                git_branch=declared.get("git_branch", ""))
    if man["found"]:
        registry.set_project_manifest(created["slug"], manifest.snapshot(man),
                                      man["path"])
    registry.log_activity(created["id"], "discover",
                          f"Registered project ({created['kind']})")
    return (registry.get_project(created["slug"]) or created), True


def discover_and_sync(roots: list[str], *, max_depth: int = 1) -> dict[str, Any]:
    found = scan(roots, max_depth=max_depth)
    created, updated = [], []
    for info in found:
        if info["kind"] == "dir":
            # Do not auto-register anonymous plain directories.
            continue
        proj, is_new = register(info)
        (created if is_new else updated).append(proj["slug"])
    return {
        "scanned": len(found),
        "created": created,
        "updated": updated,
        "found": found,
    }


def register_self() -> tuple[dict[str, Any], bool]:
    """Ensure the Perun repository itself is a registered project.

    Perun is the natural "home" project — it should always be manageable from
    the dashboard, even when it lives outside the configured scan roots.
    """
    from . import config

    repo = config.REPO_ROOT
    name = "Perun"
    existing = registry.get_project_by_path(str(repo))
    if existing:
        return existing, False
    return register({
        "name": name,
        "path": str(repo),
        "root": str(repo.parent),
        "kind": "opencode",
        "description": "Perun — overseer and orchestrator for all OpenCode projects.",
        "capabilities": capabilities(repo),
    })
