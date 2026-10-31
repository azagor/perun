"""Project lifecycle — creation, registration, and manifest sync.

The HTTP API (:mod:`perun.server`) and the MCP tools (``mcp/perun_mcp.py``)
share this logic so a project created from the dashboard wizard and one created
by the orchestrator agent are identical.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import config, git, manifest, memory, orchestrator, registry, scaffold, shared_project_sync, shared_projects

# Config file names OpenCode looks for in a project, in precedence order.
OPENCODE_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")


class ProjectError(Exception):
    """A user-facing failure while creating or registering a project."""


def sync_opencode_config(project: dict[str, Any],
                         previous_agent_models: dict[str, str] | None = None) -> dict[str, Any]:
    """Mirror the project's ``model``/``agent``/``agent_models`` overrides into its OpenCode config.

    ``model`` and ``default_agent`` in ``<project>/opencode.json`` are what the
    OpenCode **TUI** reads, so a dashboard override only reaches TUI sessions
    when it is also written there (headless ``opencode run`` already gets the
    override via CLI flags). Per-agent defaults are written under ``agent.<id>.model``
    so the TUI can pick a model by agent; unknown keys are preserved.

    Existing keys (``mcp``, ``instructions``, …) are preserved; clearing an
    override removes the key Perun owns.

    Returns a status dict with ``action`` (``created`` / ``updated`` /
    ``unchanged`` / ``skipped`` / ``error``), optional ``path``, and optional
    ``warning``. An unreadable/invalid file or a ``.jsonc`` file that contains
    comments is never clobbered; a write failure is reported as ``error`` rather
    than raised.
    """
    path = Path((project.get("path") or "").strip())
    if not path.is_dir():
        return {"action": "skipped", "warning": "project path is missing or not a directory"}
    target = next((path / name for name in OPENCODE_CONFIG_NAMES
                   if (path / name).is_file()), path / "opencode.json")

    data: dict[str, Any] = {}
    if target.is_file():
        from .mcp_servers import read_opencode_config

        data, error, lossy = read_opencode_config(target)
        if error:
            return {"action": "error", "path": str(target),
                    "warning": f"could not read {target.name}: {error}"}
        if lossy:
            return {"action": "skipped", "path": str(target),
                    "warning": f"{target.name} contains comments/trailing commas; "
                               "leaving it untouched — edit model/default_agent by hand"}

    changed = False
    model = (project.get("model") or "").strip()
    agent = (project.get("agent") or "").strip()
    for key, value in (("model", model), ("default_agent", agent)):
        if value:
            if data.get(key) != value:
                data[key] = value
                changed = True
        elif key in data:
            del data[key]
            changed = True

    # Per-agent model defaults. We write them under ``agent.<id>.model`` so the
    # TUI can honor them; if it does not support the key, it is ignored.
    agent_models = project.get("agent_models") or {}
    if not isinstance(agent_models, dict):
        agent_models = {}
    previous_models = previous_agent_models or {}
    if not isinstance(previous_models, dict):
        previous_models = {}
    managed_ids = set(agent_models) | set(previous_models)
    current_agent_block: dict[str, Any] = data.get("agent") or {}
    if not isinstance(current_agent_block, dict):
        current_agent_block = {}
    # Build the desired agent block from the current one, updating/removing only
    # agent ids Perun manages. User-added agent configuration (e.g.
    # ``agent.general.model``) is preserved.
    desired_agent_block = dict(current_agent_block)
    for agent_id in managed_ids:
        agent_model = (str(agent_models.get(agent_id, "")) or "").strip()
        if not agent_id:
            continue
        if not agent_model:
            # Previously managed, now removed.
            agent_entry = desired_agent_block.get(agent_id)
            if isinstance(agent_entry, dict) and "model" in agent_entry:
                del agent_entry["model"]
                changed = True
                if not agent_entry:
                    del desired_agent_block[agent_id]
                    changed = True
            continue
        agent_entry = desired_agent_block.setdefault(agent_id, {})
        if not isinstance(agent_entry, dict):
            agent_entry = {}
            desired_agent_block[agent_id] = agent_entry
        if agent_entry.get("model") != agent_model:
            agent_entry["model"] = agent_model
            changed = True
    if desired_agent_block:
        if data.get("agent") != desired_agent_block:
            data["agent"] = desired_agent_block
            changed = True
    elif "agent" in data:
        del data["agent"]
        changed = True

    # Register the shared-project filesystem guard and proposal-coordination
    # MCP servers for shared projects.
    # Which shape the installed CLI understands: ``mcp.servers.<name>`` (V2) or
    # ``mcp.<name>`` (V1). Writing the wrong one silently hides every server, so
    # the shape is detected, never assumed, and a config written by the other
    # version is migrated once (with a backup) rather than left half-converted.
    from . import mcp_servers as _mcp

    style = _mcp.mcp_style()
    servers = dict(_mcp.server_entries(data))
    shared_cfg = shared_projects.load(project["path"])
    guard_key = "perun-guard"
    proposals_key = "perun-proposals"
    mcp_changed = False

    # Project ``opencode.json`` files are often committed. Never embed the
    # user's absolute home path for the default home; ``~/.perun`` expands in the
    # MCP child process and keeps the config portable. A custom PERUN_HOME is
    # still written verbatim because it cannot be derived from ``~``.
    _default_home = (Path.home() / ".perun").resolve()
    if Path(config.HOME).resolve() == _default_home:
        _perun_home = "~/.perun"
    else:
        _perun_home = str(config.HOME)
    guard_entry = _mcp.entry_for_style({
        "type": "local",
        "command": [sys.executable, "-m", "perun.guard_mcp"],
        "enabled": True,
        "environment": {"PERUN_HOME": _perun_home, "PERUN_PROJECT_PATH": "."},
    }, style)
    proposals_entry = _mcp.entry_for_style({
        "type": "local",
        "command": [sys.executable, "-m", "perun.proposals_mcp"],
        "enabled": True,
        "environment": {"PERUN_HOME": _perun_home, "PERUN_PROJECT_PATH": "."},
    }, style)
    if shared_cfg.get("shared"):
        for key, entry in ((guard_key, guard_entry), (proposals_key, proposals_entry)):
            if servers.get(key) != entry:
                servers[key] = entry
                mcp_changed = True
    else:
        for key in (guard_key, proposals_key):
            if key in servers:
                del servers[key]
                mcp_changed = True
    if mcp_changed:
        _mcp.set_server_entries(data, servers, style)
        changed = True

    if not changed:
        return {"action": "unchanged", "path": str(target)} if target.is_file() else {"action": "unchanged"}

    # Drop empty top-level containers that would otherwise be written only
    # because a different key changed. An empty mcp block is not useful.
    for key in ("mcp",):
        if key in data and not data[key]:
            del data[key]

    existed_before = target.is_file()
    backup_path = None
    if mcp_changed and existed_before:
        # The MCP block is the part a hand edit is most likely to have customised
        # (extra servers, environment tweaks). The copy goes under
        # PERUN_HOME/backups/<slug>/ so a project repo never gains a stray .bak.
        from . import backups as _backups
        copy = _backups.save_copy(target, label=str(project.get("slug") or "project"))
        backup_path = str(copy) if copy else None
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            suffix=".tmp", prefix=target.name + ".", dir=str(path))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, target)
    except OSError as exc:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return {"action": "error", "path": str(target),
                "warning": f"could not write {target.name}: {exc}"}
    result = {"action": "updated" if existed_before else "created", "path": str(target)}
    if backup_path:
        result["backup"] = backup_path
    return result


def shared_mcp_config_status(project: dict[str, Any]) -> dict[str, Any]:
    """Report whether a shared project's ``opencode.json`` has the required MCP entries.

    The dashboard surfaces this as a "stale config" hint: a shared project whose
    config was written by an older Perun build lacks ``PERUN_PROJECT_PATH`` and
    its ``perun-proposals`` server cannot resolve the project from the TUI.
    """
    path = Path((project.get("path") or "").strip())
    if not path.is_dir():
        return {"stale": False, "path": "", "warning": "", "missing": []}
    cfg = shared_projects.load(str(path))
    if not cfg.get("shared"):
        return {"stale": False, "path": "", "warning": "", "missing": []}

    target = next((path / name for name in OPENCODE_CONFIG_NAMES
                   if (path / name).is_file()), path / "opencode.json")
    if not target.is_file():
        return {
            "stale": True,
            "path": str(target),
            "warning": "opencode.json is missing; perun-guard and perun-proposals are not registered",
            "missing": ["opencode.json"],
        }

    from .mcp_servers import read_opencode_config

    data, error, _lossy = read_opencode_config(target)
    if error:
        return {
            "stale": True,
            "path": str(target),
            "warning": f"could not read {target.name}: {error}",
            "missing": [target.name],
        }

    from . import mcp_servers as _mcp

    mcp = _mcp.server_entries(data)
    missing: list[str] = []
    for key in ("perun-guard", "perun-proposals"):
        entry = mcp.get(key)
        if not isinstance(entry, dict):
            missing.append(key)
            continue
        env = entry.get("environment")
        if not isinstance(env, dict):
            env = entry.get("env")
        if not isinstance(env, dict) or not str(env.get("PERUN_PROJECT_PATH") or "").strip():
            missing.append(f"{key}.PERUN_PROJECT_PATH")
    if not missing and _mcp.mcp_style() == _mcp.STYLE_V2 and not isinstance(
            (data.get("mcp") or {}).get("servers"), dict):
        # Readable by V2 only through ``mcp.servers``; a flat file is the shape
        # an older Perun wrote and should be migrated.
        missing.append("mcp.servers")

    if not missing:
        return {"stale": False, "path": str(target), "warning": "", "missing": []}
    return {
        "stale": True,
        "path": str(target),
        "warning": "stale shared MCP config (missing " + ", ".join(missing) + ") — click Sync now",
        "missing": missing,
    }


def name_from_repo(url: str) -> str:
    """Derive a project name from a repository URL."""
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail or "project"


def sync_manifest(project: dict[str, Any], *, sync_chat: bool = True) -> dict[str, Any]:
    """Re-read a project's manifest from disk and merge declared fields.

    A manifest's ``instructions`` becomes ``.perun/instructions/default.md``
    (seed-once, so later dashboard edits are never clobbered); every other
    declared field is merged into the registry. Instruction text never touches
    the database.

    If the project is shared, the dedicated PerunChat room is reconciled so
    manual manifest edits are reflected in the chat ACL.
    """
    from . import shared_projects

    snap = manifest.load(project["path"])
    if snap["found"]:
        declared = manifest.to_project_fields(snap["data"])
        instructions = declared.pop("instructions", "")
        previous_agent_models = project.get("agent_models")
        if not isinstance(previous_agent_models, dict):
            previous_agent_models = {}
        if declared:
            registry.update_project(project["slug"], **declared)
            # Refresh the project dict so the opencode.json mirror below uses the
            # new model/agent/agent_models values.
            project = registry.get_project(project["slug"]) or project
        orchestrator.write_project_instruction_file(
            project["path"], instructions, overwrite=False)
        registry.set_project_manifest(project["slug"], manifest.snapshot(snap),
                                      snap["path"])
        registry.sync_dependencies(project["slug"], snap["data"].get("dependencies"))
        cfg = shared_projects.load(project["path"])
        # Mirror declared model/agent/per-agent defaults into opencode.json so the
        # TUI honors them too. For shared projects also ensure the guard/proposals MCP
        # servers are present, even when the manifest carries no new overrides.
        if ("model" in declared or "agent" in declared or "agent_models" in declared
                or cfg.get("shared")):
            try:
                sync_opencode_config(project, previous_agent_models=previous_agent_models)
            except Exception:  # pragma: no cover - best-effort config sync
                pass
        if cfg.get("shared"):
            if sync_chat:
                try:
                    shared_project_sync.sync_project_chat(project, cfg)
                except Exception:  # pragma: no cover - best-effort config sync
                    pass
    return snap


def _write_default_instructions(path: str, instructions: str) -> None:
    """Write ``instructions`` to ``.perun/instructions/default.md``."""
    orchestrator.write_project_instruction_file(path, instructions)


def register_or_update(name: str, path: str, *, root: str = "", kind: str = "",
                       description: str = "", tags: str = "", model: str = "",
                       agent: str = "", agent_models: dict[str, str] | None = None,
                       color: str = "", instructions: str = "") -> tuple[dict[str, Any], bool]:
    """Insert a new project or update the existing one at *path*.

    Returns ``(project, created)``. Only non-empty fields overwrite an existing
    record, so a partial update never clears configured values.
    """
    # ``~/projects/app`` is what users type; ``Popen(cwd=...)`` will not expand
    # it, so resolve it once, here, for every caller.
    path = os.path.expanduser((path or "").strip())
    existing = registry.get_project_by_path(path)
    if existing:
        previous_agent_models = existing.get("agent_models")
        if not isinstance(previous_agent_models, dict):
            previous_agent_models = {}
        update_fields: dict[str, Any] = {
            "name": name or existing.get("name", ""),
            "kind": kind or existing.get("kind", "dir"),
            "description": description or existing.get("description", ""),
            "model": model or existing.get("model", ""),
            "agent": agent or existing.get("agent", ""),
            "tags": tags or existing.get("tags", ""),
            "color": color or existing.get("color", ""),
        }
        if agent_models is not None:
            update_fields["agent_models"] = agent_models
        project = registry.update_project(existing["slug"], **update_fields)
        # Write into the *project's* path, not the possibly-nested path the
        # caller passed (registering ``<project>/src`` must not drop
        # instructions into a subdirectory of the project).
        _write_default_instructions(existing["path"], instructions)
        project = project or existing
        if model or agent or agent_models is not None:
            sync_opencode_config(project, previous_agent_models=previous_agent_models)
        return project, False
    try:
        project = registry.add_project(
            name, path, root=root, kind=kind or "dir", description=description,
            tags=tags, model=model, agent=agent, agent_models=agent_models,
            color=color,
        )
    except ValueError as exc:
        # e.g. all slug candidates are taken — surface as a clean client error.
        raise ProjectError(str(exc)) from exc
    _write_default_instructions(path, instructions)
    if model or agent or agent_models:
        sync_opencode_config(project)
    return project, True


def create(*, method: str = "existing", name: str = "", path: str = "",
           clone_url: str = "", root: str = "", bootstrap: bool = True,
           description: str = "", kind: str = "", tags: str = "", model: str = "",
           agent: str = "", agent_models: dict[str, str] | None = None,
           color: str = "", instructions: str = "",
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
            target_dir, created_dir = scaffold.create_dir(path)
            # The wizard (or an API caller) may point "new" at a directory the
            # user already created in Explorer or an editor. Bootstrapping must
            # happen for that existing-but-empty target too, otherwise the
            # project ends up with no README/AGENTS/docs/manifest anchors and the
            # dashboard shows an empty Manifest tab. Non-empty directories are
            # never touched.
            empty_dir = not created_dir and not any(target_dir.iterdir())
            if bootstrap and (created_dir or empty_dir):
                scaffold.bootstrap(str(target_dir), name or Path(path).name)
        except scaffold.ScaffoldError as exc:
            raise ProjectError(str(exc))

    if not path:
        raise ProjectError("path is required")
    if not name:
        name = Path(path).name or "project"

    project, created = register_or_update(
        name, path, root=root, kind=kind, description=description, tags=tags,
        model=model, agent=agent, agent_models=agent_models, color=color,
        instructions=instructions,
    )
    # Always mirror declared overrides into opencode.json, even when the path was
    # already registered and register_or_update skipped the sync.
    if created is False and (model or agent or agent_models):
        previous = project.get("agent_models")
        if not isinstance(previous, dict):
            previous = {}
        sync_opencode_config(project, previous_agent_models=previous)

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
