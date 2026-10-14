"""Project lifecycle — creation, registration, and manifest sync.

The HTTP API (:mod:`perun.server`) and the MCP tools (``mcp/perun_mcp.py``)
share this logic so a project created from the dashboard wizard and one created
by the orchestrator agent are identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import git, manifest, memory, registry, scaffold


class ProjectError(Exception):
    """A user-facing failure while creating or registering a project."""


def name_from_repo(url: str) -> str:
    """Derive a project name from a repository URL."""
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail or "project"


def sync_manifest(project: dict[str, Any]) -> dict[str, Any]:
    """Re-read a project's manifest from disk and merge declared fields."""
    snap = manifest.load(project["path"])
    if snap["found"]:
        declared = manifest.to_project_fields(snap["data"])
        if declared:
            registry.update_project(project["slug"], **declared)
        registry.set_project_manifest(project["slug"], manifest.snapshot(snap),
                                      snap["path"])
    return snap


def register_or_update(name: str, path: str, *, root: str = "", kind: str = "",
                       description: str = "", tags: str = "", model: str = "",
                       agent: str = "", color: str = "",
                       instructions: str = "") -> tuple[dict[str, Any], bool]:
    """Insert a new project or update the existing one at *path*.

    Returns ``(project, created)``. Only non-empty fields overwrite an existing
    record, so a partial update never clears configured values.
    """
    existing = registry.get_project_by_path(path)
    if existing:
        project = registry.update_project(
            existing["slug"],
            name=name or existing.get("name", ""),
            kind=kind or existing.get("kind", "dir"),
            description=description or existing.get("description", ""),
            model=model or existing.get("model", ""),
            agent=agent or existing.get("agent", ""),
            tags=tags or existing.get("tags", ""),
            color=color or existing.get("color", ""),
            instructions=instructions or existing.get("instructions", ""),
        )
        return project or existing, False
    project = registry.add_project(
        name, path, root=root, kind=kind or "dir", description=description,
        tags=tags, model=model, agent=agent, color=color,
        instructions=instructions,
    )
    return project, True


def create(*, method: str = "existing", name: str = "", path: str = "",
           clone_url: str = "", root: str = "", bootstrap: bool = True,
           description: str = "", kind: str = "", tags: str = "", model: str = "",
           agent: str = "", color: str = "", instructions: str = "",
           git_init: bool = False, git_remote: str = "", git_branch: str = "",
           index_docs: bool = False) -> dict[str, Any]:
    """Create/register a project and return ``{project, created, git}``.

    ``method`` is ``existing`` (default; the path may not exist yet), ``new``
    (create the directory and optionally bootstrap a starter project), or
    ``clone`` (clone a remote into the target path). Raises
    :class:`ProjectError` for bad input or a git failure.
    """
    method = (method or "existing").strip().lower()
    path = (path or "").strip()
    name = (name or "").strip()
    root = (root or "").strip()

    if method == "clone":
        url = (clone_url or git_remote or "").strip()
        if not url:
            raise ProjectError("clone_url is required")
        if not name:
            name = name_from_repo(url)
        if not path:
            if not root:
                raise ProjectError("path is required (or set a root)")
            path = str(Path(root) / (registry.slugify(name) or "project"))
        try:
            git.clone(url, path)
        except git.GitError as exc:
            raise ProjectError(str(exc))
    elif method == "new":
        if not path:
            raise ProjectError("path is required")
        try:
            created_dir = scaffold.create_dir(path)[1]
            if created_dir and bootstrap:
                scaffold.bootstrap(path, name or Path(path).name)
        except scaffold.ScaffoldError as exc:
            raise ProjectError(str(exc))

    if not path:
        raise ProjectError("path is required")
    if not name:
        name = Path(path).name or "project"

    project, created = register_or_update(
        name, path, root=root, kind=kind, description=description, tags=tags,
        model=model, agent=agent, color=color, instructions=instructions,
    )

    branch = (git_branch or "").strip()
    remote = (git_remote or "").strip()
    try:
        if git_init and not git.is_repo(path):
            git.init(path, branch or "main")
        if remote and not git.is_repo(path):
            git.init(path, branch or "main")
        if remote:
            git.set_remote(path, remote, branch)
        if remote or branch or git_init:
            registry.update_project(project["slug"], git_remote=remote,
                                    git_branch=branch)
            project = registry.get_project(project["slug"]) or project
    except git.GitError as exc:
        raise ProjectError(str(exc))

    if index_docs:
        try:
            memory.index_project_docs(project["id"])
        except Exception:  # pragma: no cover - indexing is best-effort
            pass

    # A bootstrap (or an existing repo) may carry a perun.md manifest; merge it
    # into the registry now so the dashboard reflects it immediately.
    try:
        snap = sync_manifest(project)
        if snap["found"]:
            project = registry.get_project(project["slug"]) or project
    except Exception:  # pragma: no cover - manifest merge is best-effort
        pass

    registry.log_activity(
        project["id"], "discover",
        "Project created via wizard" if method in ("new", "clone")
        else "Project registered manually",
    )
    return {"project": project, "created": created, "git": git.info(path)}
