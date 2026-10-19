"""Project discovery — find OpenCode-related projects under configured roots.

A directory is considered an *OpenCode project* when it carries any of the
framework markers (``.opencode/``, ``opencode.json``, ``AGENTS.md``,
``docs/CODEMAP.md``) or is simply a git working tree. Discovery is cheap: it
stats a handful of well-known paths per candidate directory and never recurses
into dependency/build directories.
"""

from __future__ import annotations

from . import project_roots

import os
from pathlib import Path
from typing import Any

from . import manifest, memory, orchestrator, registry, scaffold


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
        try:
            if not f.is_file():
                continue
        except OSError:
            # Unreadable parent directory (PermissionError) — not a candidate.
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
    """Probe *path* for framework markers.

    Every probe is guarded: ``Path.exists``/``is_file``/``is_dir`` propagate
    ``PermissionError`` for an unreadable directory, which would otherwise
    abort the whole scan (``detect`` → ``classify`` → ``capabilities``).
    """

    def _safe(probe: Any, default: bool = False) -> bool:
        try:
            return bool(probe())
        except OSError:
            return default

    def has(*rels: str) -> bool:
        return any(_safe(lambda r=r: (path / r).exists()) for r in rels)

    return {
        "git": _safe(lambda: (path / ".git").exists()),
        "opencode": has(*OPENCODE_MARKERS),
        "agents_md": _safe(lambda: (path / "AGENTS.md").is_file()),
        "manifest": has(*manifest.MANIFEST_NAMES),
        "docs": has(*DOC_MARKERS),
        "codemap": _safe(lambda: (path / "docs" / "CODEMAP.md").is_file()),
        "memory_scripts": _safe(lambda: (path / "scripts" / "memory.py").is_file()),
        "telegram_bridge": _safe(lambda: (path / "scripts" / "telegram_bridge.py").is_file()),
        "mcp_memory": _safe(lambda: (path / "scripts" / "mcp_memory_server.py").is_file()),
        "build_ps1": _safe(lambda: (path / "scripts" / "build.ps1").is_file()),
        "package_json": _safe(lambda: (path / "package.json").is_file()),
        "tests": _safe(lambda: (path / "tests").is_dir()),
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
    # Track visited (dev, ino) pairs to avoid symlink cycles.
    visited: set[tuple[int, int]] = set()

    def walk(d: Path, depth: int) -> None:
        try:
            entries = list(d.iterdir())
        except (OSError, PermissionError):
            return
        for entry in entries:
            if not entry.is_dir() or entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            try:
                st = entry.stat()
                key = (st.st_dev, st.st_ino)
            except OSError:
                key = None
            if key is not None:
                if key in visited:
                    continue
                visited.add(key)
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
            # Use absolute() rather than resolve() so symlinks under a root do
            # not escape the configured root.
            key = os.path.normcase(str(cand.absolute()))
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


def record_dependencies(project_id: int, path: str) -> None:
    """Parse dependency files and store each dependency as a project-scoped fact."""
    try:
        deps = scaffold.parse_dependencies(path)
    except Exception:  # pragma: no cover - defensive
        return
    for source_file, items in deps.items():
        for name, version in items:
            content = f"Dependency {name}" + (f" ({version})" if version else "")
            content += f" from {source_file}"
            try:
                memory.remember(
                    content, project_id=project_id, scope="project",
                    topic="dependencies", source="discovery",
                )
            except Exception:  # pragma: no cover - memory is fail-open
                pass


_record_dependencies = record_dependencies


def record_local_references(project: dict[str, Any],
                            existing_by_path: dict[str, dict[str, Any]]) -> None:
    """Store cross-project references as project-scoped facts."""
    projects = list(existing_by_path.values())
    try:
        refs = scaffold.find_local_project_references(project["path"], projects)
    except Exception:  # pragma: no cover - defensive
        return
    for ref in refs:
        try:
            memory.remember(
                f"References local project {ref['target_name']} ({ref['target_slug']}) via {ref['via']}",
                project_id=project["id"], scope="project",
                topic="relationships", source="discovery",
            )
        except Exception:  # pragma: no cover - memory is fail-open
            pass


_record_local_references = record_local_references


def register(info: dict[str, Any],
             existing_by_path: dict[str, dict[str, Any]] | None = None) -> tuple[dict[str, Any], bool]:
    """Insert or update a discovered project. Returns (project, created).

    A ``perun.md``/``perun.json`` manifest, when present, is authoritative for
    the fields it declares (name, kind, description, tags, model, agent,
    instructions, git) and its snapshot is stored for the dashboard.

    After registration, dependency files are parsed into project-scoped memory
    facts, local project references are recorded, and missing dependency anchors
    are appended to an existing ``perun.md``.
    """
    norm = os.path.normcase(os.path.realpath(os.path.abspath(info["path"])))
    existing = existing_by_path.get(norm) if existing_by_path else registry.get_project_by_path(info["path"])
    caps = info.get("capabilities", {})
    cap_tags = ",".join(sorted(k for k, v in caps.items() if v))

    man = manifest.load(info["path"])
    declared = manifest.to_project_fields(man["data"]) if man["found"] else {}

    name = declared.get("name") or info.get("name", Path(info["path"]).name)
    kind = declared.get("kind") or info.get("kind", "dir")
    description = declared.get("description") or info.get("description", "")
    tags = declared.get("tags") or cap_tags

    instructions = declared.get("instructions", "")
    if existing:
        updated = registry.update_project(
            existing["slug"],
            name=name,
            root=info.get("root", existing.get("root", "")),
            kind=kind,
            description=description,
            tags=tags,
            **{k: v for k, v in declared.items()
               if k in ("model", "agent", "git_remote", "git_branch")},
        )
        orchestrator.write_project_instruction_file(
            info["path"], instructions, overwrite=False)
        if man["found"]:
            registry.set_project_manifest(existing["slug"], manifest.snapshot(man),
                                          man["path"])
        project = updated or existing
        registry.touch_project(existing["id"], seen=True)
        try:
            project_roots.auto_register(project["slug"], project["path"])
        except Exception:
            pass
    else:
        # Store the resolved path, not the spelling reached through a symlink:
        # the link can be recreated/renamed, and the same project would otherwise
        # be registered twice (once per spelling).
        try:
            real_path = os.path.realpath(os.path.abspath(info["path"]))
        except (OSError, ValueError):
            real_path = info["path"]
        created = registry.add_project(
            name,
            real_path,
            root=info.get("root", ""),
            kind=kind,
            description=description,
            tags=tags,
            model=declared.get("model", ""),
            agent=declared.get("agent", ""),
        )
        orchestrator.write_project_instruction_file(
            info["path"], instructions, overwrite=False)
        if declared.get("git_remote") or declared.get("git_branch"):
            registry.update_project(created["slug"],
                                    git_remote=declared.get("git_remote", ""),
                                    git_branch=declared.get("git_branch", ""))
        if man["found"]:
            registry.set_project_manifest(created["slug"], manifest.snapshot(man),
                                          man["path"])
        registry.log_activity(created["id"], "discover",
                              f"Registered project ({created['kind']})")
        project = registry.get_project(created["slug"]) or created
        try:
            project_roots.auto_register(project["slug"], project["path"])
        except Exception:
            pass

    # Refresh manifest anchors for new dependency files without clobbering edits.
    try:
        scaffold.update_manifest_anchors(info["path"])
    except Exception:  # pragma: no cover - best effort
        pass

    # Re-load manifest snapshot if it changed.
    refreshed = manifest.load(info["path"])
    if refreshed["found"]:
        registry.set_project_manifest(project["slug"], manifest.snapshot(refreshed),
                                      refreshed["path"])

    # Parse and remember dependencies / relationships.
    record_dependencies(project["id"], info["path"])
    if existing_by_path:
        record_local_references(project, existing_by_path)

    return project, existing is None


def discover_and_sync(roots: list[str], *, max_depth: int = 1) -> dict[str, Any]:
    found = scan(roots, max_depth=max_depth)
    created, updated = [], []
    # Build a path lookup once so registration is O(1) per candidate.
    # Keys use realpath so a path reached through a symlink maps onto the
    # project stored under its resolved location.
    existing_by_path: dict[str, dict[str, Any]] = {}
    for p in registry.list_projects():
        try:
            key = os.path.normcase(os.path.realpath(os.path.abspath(p["path"])))
        except (OSError, ValueError):
            key = os.path.normcase(os.path.abspath(p["path"]))
        existing_by_path[key] = p
    for info in found:
        if info["kind"] == "dir":
            # Do not auto-register anonymous plain directories.
            continue
        proj, is_new = register(info, existing_by_path=existing_by_path)
        (created if is_new else updated).append(proj["slug"])
    # Registered projects under the scanned roots whose directory vanished —
    # surfaced so callers can mark/prune them instead of offering dead targets.
    resolved_roots: list[str] = []
    for r in roots:
        try:
            resolved_roots.append(
                os.path.normcase(os.path.realpath(os.path.abspath(str(r)))))
        except (OSError, ValueError):
            continue
    missing: list[str] = []
    for p in registry.list_projects():
        try:
            pnorm = os.path.normcase(os.path.realpath(os.path.abspath(p["path"])))
        except (OSError, ValueError):
            continue
        under = any(pnorm == root or pnorm.startswith(root + os.sep)
                    for root in resolved_roots)
        if under and not os.path.isdir(p["path"]):
            missing.append(p["slug"])
    return {
        "scanned": len(found),
        "created": created,
        "updated": updated,
        "missing": missing,
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
