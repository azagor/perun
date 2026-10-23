#!/usr/bin/env python3
"""Perun MCP server — expose the overseer to any OpenCode project.

Installed globally by ``python -m perun install`` so *every* project inherits
these tools without per-project configuration. The server is deliberately
thin: it just forwards to the Perun package (registry, memory, orchestrator).

Tools
-----
``perun_list_projects``  List every registered project.
``perun_project_info``   Details + recent activity for one project.
``perun_create_project`` Register/scaffold/clone a project.
``perun_update_project`` Rename, retag, or set model/agent, or write instructions.
``perun_delete_project`` Remove a project from the registry (files kept).
``perun_discover``       Rescan the configured roots for projects.
``perun_memory_search``  Search facts + indexed docs (this or all projects).
``perun_memory_store``   Store a durable fact (global or project scope).
``perun_context``        Retrieve context docs — keys, or the context for a task.
``perun_secret``         Read/write a named secret from the encrypted store.
``perun_docs_index``     (Re)index a project's docs into the vector store.
``perun_get_instructions`` Read a project's custom instructions (effective or a specific file).
``perun_set_instructions`` Set a project's custom instructions in ``.perun/instructions/*.md``.
``perun_list_instruction_files`` List project-local instruction Markdown files.
``perun_grep``           Fast regex content search across a project (ripgrep + fallback).
``perun_ast_grep``       Structural code search across a project (ast-grep + fallback).
``perun_global_grep``    Fast regex content search across *all* registered projects.
``perun_global_ast_grep`` Structural code search across *all* registered projects.
``perun_unified_search``  Unified cross-project search: vector memory + grep + ast-grep.
``perun_get_manifest``   Read a project's perun.md manifest (data + body).
``perun_set_manifest``   Write a project's perun.md manifest.
``perun_start_session``  Start an agent run in a project (returns run id).
``perun_ask``            Run a prompt in a project and wait for the reply.
``perun_run_status``     Status / output of a run.
``perun_telegram_send``  Send a message to the configured Telegram chat.

Most tools accept an optional ``project`` (slug or path). When omitted, the
project is resolved from the current working directory.
"""

from __future__ import annotations

import os

# FastMCP prints a startup banner to stdout by default, which corrupts the
# stdio JSON-RPC stream used by MCP clients. Suppress it before importing.
os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastmcp import FastMCP  # noqa: E402

from perun import config, context, control, db, discovery, grep, handbook, memory, orchestrator, projects, registry, secret_store  # noqa: E402

mcp = FastMCP("Perun")

db.init_db()

# The orchestrator (control plane) must exist for *every* tool call, so a brand
# new machine with no projects can still be talked to. Registration is
# idempotent and best-effort.
try:
    control.ensure()
except Exception:  # pragma: no cover - never block the MCP server from starting
    pass

# Make sure the Perun Context store holds the handbook, so `perun_context`
# works on a machine that never re-ran the installer. Indexing is
# content-hash based: after the first run this costs a few small SELECTs and
# no embedding calls.
try:
    handbook.ensure_indexed()
except Exception:  # pragma: no cover - never block the MCP server from starting
    pass

# Shared orchestrator for MCP-initiated runs so they live in the same in-memory
# event bus and can be observed/cancelled consistently.
_ORCH: orchestrator.Orchestrator | None = None


def _mcp_orchestrator() -> orchestrator.Orchestrator:
    global _ORCH
    if _ORCH is None:
        _ORCH = orchestrator.Orchestrator(config.Settings.load())
    return _ORCH


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _resolve_project(project: str | None) -> dict | None:
    if project:
        p = registry.get_project(project)
        if p:
            return p
        p = registry.get_project_by_path(project)
        if p:
            return p
        return None
    # Walk up from cwd looking for a registered path.
    try:
        cur = Path.cwd()
    except OSError:
        return None
    projects = registry.list_projects()
    by_path = {}
    for p in projects:
        try:
            by_path[os.path.normcase(os.path.abspath(p["path"]))] = p
        except Exception:
            continue
    for candidate in [cur, *cur.parents]:
        key = os.path.normcase(str(candidate))
        if key in by_path:
            return by_path[key]
    return None


def _project_line(p: dict) -> str:
    tag = "  [orchestrator]" if p.get("is_control") else ""
    return (f"{p['slug']}  —  {p['name']} ({p['kind']})"
            f"  [{p['path']}]{tag}")


def _normalize_project_instruction_filename(filename: str) -> tuple[str | None, str]:
    """Validate and normalize a project-local instruction filename.

    Rejects empty names, special directory entries (``.``, ``..``), and any
    path containing ``/`` or ``\\``. Ensures the filename ends with ``.md``.
    Returns ``(normalized_filename, "")`` on success or ``(None, error_msg)``.
    """
    filename = (filename or "").strip()
    if not filename:
        return None, "[perun] filename is required"
    # Segment-level traversal detection: after normalizing separators the name
    # must be exactly one non-special segment.
    parts = filename.replace("\\", "/").split("/")
    if len(parts) != 1 or parts[0] in ("", ".", ".."):
        return None, "[perun] filename must be a single safe name (no path separators or parent-directory references) ending in .md"
    if not filename.lower().endswith(".md"):
        return None, "[perun] filename must end in .md"
    return filename, ""


def _project_instruction_file_path(project: dict, filename: str) -> tuple[Path | None, str]:
    """Return the validated absolute path for a project-local instruction file."""
    normalized, error = _normalize_project_instruction_filename(filename)
    if error:
        return None, error
    root = Path(project["path"]).resolve()
    path = (root / orchestrator.PROJECT_INSTRUCTIONS_DIR / normalized).resolve()
    try:
        if not path.is_relative_to(root):
            raise ValueError("path escapes project root")
    except ValueError:
        return None, "[perun] filename must resolve inside the project directory"
    return path, ""


def _safe_int(value, name: str, min_val: int | None = None,
              max_val: int | None = None) -> tuple[int | None, str]:
    """Coerce ``value`` to int and optionally clamp between bounds.

    Returns ``(int_value, "")`` on success or ``(None, "[perun] ...")``.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None, f"[perun] {name} must be an integer"
    if min_val is not None and v < min_val:
        return None, f"[perun] {name} must be at least {min_val}"
    if max_val is not None and v > max_val:
        return None, f"[perun] {name} must be at most {max_val}"
    return v, ""


def _validate_projects_list(projects) -> tuple[list | None, str]:
    """Accept ``None`` or a list of slugs; reject other types.

    Returns ``(None, "")`` when the caller omitted the filter, and the list
    otherwise, so an explicit empty list stays distinguishable from "no
    filter" (an empty list means "no projects to search").
    """
    if projects is None:
        return None, ""
    if not isinstance(projects, list):
        return None, "[perun] projects must be a list of slugs or omitted"
    return projects, ""


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool
def perun_list_projects() -> str:
    """List every OpenCode project registered with Perun.

    Returns one line per project: slug, name, kind, and path. The project whose
    path contains the current working directory is marked with ``→``.
    """
    projects = registry.list_projects()
    if not projects:
        return ("[perun] No projects registered yet. Run "
                "`python -m perun discover` to scan your project roots.")
    here = _resolve_project(None)
    lines = [f"[perun] {len(projects)} project(s):"]
    for p in projects:
        mark = "→ " if here and p["id"] == here["id"] else "  "
        sessions = p.get("session_count", 0)
        facts = p.get("memory_count", 0)
        lines.append(f"{mark}{_project_line(p)}"
                     f"  (sessions={sessions}, facts={facts})")
    return "\n".join(lines)


@mcp.tool
def perun_project_info(project: str | None = None) -> str:
    """Details for one project: counts, model/agent, and recent activity.

    Args:
        project: Project slug or absolute path. Omit to use the current
            working directory's project.
    """
    p = _resolve_project(project)
    if not p:
        target = project or os.getcwd()
        return (f"[perun] No project found for '{target}'. Try "
                "`perun_list_projects`.")
    detail = registry.get_project(p["slug"]) or p
    stats = memory.stats(p["id"])
    lines = [
        f"[perun] {detail['name']} ({detail['slug']})",
        f"  path:        {detail['path']}",
        f"  kind:        {detail['kind']}",
        f"  description: {detail.get('description') or '(none)'}",
        f"  model:       {detail.get('model') or '(default)'}",
        f"  agent:       {detail.get('agent') or '(default)'}",
        f"  sessions:    {detail.get('session_count', 0)}",
        f"  memory:      facts={stats.get('facts_project', 0)}, "
        f"doc_chunks={stats.get('chunks', 0)}",
    ]
    recent = registry.list_activity(p["id"], limit=5)
    if recent:
        lines.append("  recent activity:")
        for a in recent:
            lines.append(f"    - [{a['kind']}] {a['message']}")
    return "\n".join(lines)


@mcp.tool
def perun_create_project(name: str = "", path: str = "", method: str = "existing",
                         clone_url: str = "", root: str = "", bootstrap: bool = True,
                         description: str = "", tags: str = "", model: str = "",
                         agent: str = "", instructions: str = "",
                         git_init: bool = False, git_remote: str = "",
                         git_branch: str = "", index_docs: bool = False,
                         confirm: bool = False) -> str:
    """Create, scaffold, or clone a project and register it with Perun.

    Args:
        name: Project name (defaults to the folder/repo name).
        path: Absolute target path. Required for ``new``; optional for ``clone``
            when ``root`` is given (the folder is named after the project).
        method: ``existing`` (register a folder that already exists — default),
            ``new`` (create the folder, optionally bootstrap a starter project),
            or ``clone`` (clone ``clone_url`` into ``path``).
        clone_url: Git URL for ``method="clone"``.
        root: Base directory to place the project under when ``path`` is empty.
        bootstrap: For ``new``, scaffold AGENTS.md + docs + .opencode.
        description/tags/model/agent/instructions: Optional project metadata.
            ``instructions`` is written to ``.perun/instructions/default.md``.
        git_init/git_remote/git_branch: Optional git configuration.
        index_docs: Index the project's docs into Perun's memory immediately.
        confirm: Required for ``method="clone"``: cloning fetches code from an
            arbitrary remote onto this machine.
    """
    if (method or "").strip().lower() == "clone" or (clone_url or "").strip():
        if not confirm:
            return ("[perun] Refused: pass confirm=True to clone a remote "
                    "repository onto this machine.")
    try:
        result = projects.create(
            method=method, name=name, path=path, clone_url=clone_url, root=root,
            bootstrap=bootstrap, description=description, tags=tags, model=model,
            agent=agent, instructions=instructions, git_init=git_init,
            git_remote=git_remote, git_branch=git_branch, index_docs=index_docs,
        )
    except projects.ProjectError as exc:
        return f"[perun] Could not create project: {exc}"
    p = result["project"]
    verb = "Created" if result.get("created") else "Updated"
    return (f"[perun] {verb} project `{p['slug']}` ({p['name']}) at {p['path']}.\n"
            f"  git: {'repo' if result.get('git', {}).get('is_repo') else 'no repo'}")


@mcp.tool
def perun_update_project(slug: str, name: str = "", description: str = "",
                         tags: str = "", model: str = "", agent: str = "",
                         color: str = "", instructions: str = "") -> str:
    """Update a project's registry metadata (empty fields are left unchanged).

    Args:
        slug: The project slug to update.
        name/description/tags/model/agent/color: New values (omit to keep).
        instructions: New custom instructions for the project's sessions, written
            to ``.perun/instructions/default.md``.
    """
    from perun import orchestrator

    p = registry.get_project(slug)
    if not p:
        return f"[perun] No such project: {slug}"
    fields: dict[str, str] = {}
    for key, value in (("name", name), ("description", description),
                       ("tags", tags), ("model", model), ("agent", agent),
                       ("color", color)):
        if value:
            fields[key] = value
    opencode_note = ""
    if fields:
        registry.update_project(slug, **fields)
        if "model" in fields or "agent" in fields:
            # Mirror into the project's opencode.json so the TUI honors it too.
            updated = registry.get_project(slug) or p
            cfg_result = projects.sync_opencode_config(updated)
            if cfg_result.get("warning"):
                opencode_note = f" (opencode.json: {cfg_result['warning']})"
            elif cfg_result.get("action") in ("created", "updated"):
                opencode_note = " (opencode.json updated)"
    wrote_instructions = bool(
        orchestrator.write_project_instruction_file(p["path"], instructions)
    )
    if not fields and not wrote_instructions:
        return f"[perun] Nothing to update for {slug}."
    registry.log_activity(p["id"], "config", "Project updated via MCP")
    changed = sorted(fields) + (["instructions"] if wrote_instructions else [])
    return f"[perun] Updated {slug}: {', '.join(changed)}.{opencode_note}"


@mcp.tool
def perun_delete_project(slug: str, confirm: bool = False) -> str:
    """Remove a project from the Perun registry. **Files on disk are kept.**

    Destructive: the registry entry, its sessions/runs, and its Perun memory are
    removed. The directory itself is never touched. Requires ``confirm=True``.

    Args:
        slug: The project slug to remove.
        confirm: Must be ``True`` — guards against accidental deletions.
    """
    p = registry.get_project(slug)
    if not p:
        return f"[perun] No such project: {slug}"
    if control.is_control(p):
        return "[perun] Refused: the Perun orchestrator project cannot be removed."
    if not confirm:
        return (f"[perun] Refused: pass confirm=True to delete `{slug}`. Files at "
                f"{p['path']} are NOT deleted — only the registry entry and memory.")
    registry.delete_project(slug)
    return (f"[perun] Removed `{slug}` from the registry. Files at {p['path']} "
            "were left untouched.")


@mcp.tool
def perun_discover(roots: str = "", max_depth: int = 1, index: bool = False,
                   confirm: bool = False) -> str:
    """Rescan the configured project roots and register anything found.

    Args:
        roots: Comma-separated roots to scan. Omit for the configured roots.
        max_depth: How deep to recurse (default 1).
        index: Also index each discovered project's docs.
        confirm: Required when ``roots`` is given, since scanning outside the
            configured roots registers arbitrary directories as projects.
    """
    root_list = [r.strip() for r in roots.split(",") if r.strip()] or None
    if root_list and not confirm:
        return ("[perun] Refused: pass confirm=True to scan roots outside the "
                "configured list (they would be registered as projects).")
    settings = config.Settings.load()
    result = discovery.discover_and_sync(root_list or list(settings.roots),
                                         max_depth=max_depth)
    created = result.get("created", [])
    updated = result.get("updated", [])
    if index:
        for slug in [*created, *updated]:
            p = registry.get_project(slug)
            if p:
                try:
                    memory.index_project_docs(p["id"])
                except Exception:  # pragma: no cover - indexing is best-effort
                    pass
    return (f"[perun] Scanned {result.get('scanned', 0)} candidate(s): "
            f"{len(created)} created ({', '.join(created) or 'none'}), "
            f"{len(updated)} updated ({', '.join(updated) or 'none'}).")


@mcp.tool
def perun_memory_search(query: str, project: str | None = None, k: int = 5,
                        all_projects: bool = False) -> str:
    """Search Perun's unified memory (facts + indexed docs) across projects.

    Use this for questions whose answer may live in *another* project's docs or
    in previously stored facts.

    Args:
        query: Natural-language question or keywords.
        project: Project slug/path to scope the search. Omit for the current
            project.
        k: Maximum number of results (1-8).
        all_projects: Search the document store across every project.
    """
    k, error = _safe_int(k, "k", min_val=1, max_val=8)
    if error:
        return error
    p = _resolve_project(project)
    results = memory.unified_search(query, p["id"] if p else 0, k=k,
                                    all_projects=all_projects)
    if not results:
        return "[perun] No matching memory."
    out = []
    for i, r in enumerate(results, 1):
        tag = "DOC" if r.get("source_type") == "doc" else "FACT"
        where = r.get("source_file") or r.get("topic") or ""
        preview = " ".join((r.get("content") or "").split())[:300]
        out.append(f"[{i}] {tag} score={r.get('score')} project={r.get('project_id')} {where}\n    {preview}")
        if r.get("paths"):
            out.append(f"    paths: {', '.join(r['paths'])}")
    return "\n".join(out)


@mcp.tool
def perun_memory_store(content: str, project: str | None = None,
                       global_scope: bool = False, topic: str = "general") -> str:
    """Store a durable fact in Perun's unified memory.

    Args:
        content: The fact to remember (one atomic statement per call).
        project: Project slug/path to scope the fact to.
        global_scope: Store as a global fact visible to every project.
        topic: Optional topic bucket (default "general").
    """
    p = _resolve_project(project)
    scope = "global" if global_scope else ("project" if p else "global")
    pid = 0 if scope == "global" else (p["id"] if p else 0)
    res = memory.remember(content, project_id=pid, scope=scope, topic=topic,
                          source="mcp")
    if not res.get("ok"):
        return f"[perun] Not stored: {res.get('reason')}"
    if res.get("duplicate"):
        return "[perun] Already remembered (duplicate)."
    return f"[perun] Stored ({scope} scope, id={res['id']})."


@mcp.tool
def perun_context(topic: str = "", project: str | None = None,
                  k: int = 3, mode: str = "auto") -> str:
    """Retrieve Perun context: reference docs, the key inventory, or the
    context for the task at hand.

    **Call this first when you start work in a Perun-managed project.** Pass a
    short description of *what you are about to do* and Perun resolves it into
    the reference material that task needs — build commands, server access,
    key files, project conventions, the MCP tool surface. You do not have to
    know which document holds the answer, and you should not guess.

    Args:
        topic: A context key ("handbook.build"), an anchor label, or — most
            usefully — a description of what you are about to do.
        project: Project slug/path to scope to. Omit for the current project.
        k: Maximum number of documents to return (1-8).
        mode: "auto" (exact key -> anchor label -> key substring -> semantic),
            "exact" (exact key or anchor label only), or "list" (the inventory
            of available keys).
    """
    k, error = _safe_int(k, "k", min_val=1, max_val=8)
    if error:
        return error
    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "exact", "list"):
        return "[perun] mode must be one of: auto, exact, list"
    p = _resolve_project(project)
    try:
        hits = context.search(topic, project_id=p["id"] if p else 0, k=k, mode=mode)
    except Exception as exc:  # pragma: no cover - defensive
        return f"[perun] context lookup failed: {exc}"
    if mode == "list":
        return context.render_inventory(hits)
    return context.render_hits(hits)


@mcp.tool
def perun_secret(name: str = "", action: str = "get", value: str = "",
                 hint: str = "", scope: str = "auto",
                 project: str | None = None) -> str:
    """Read or write a named secret from Perun's encrypted secret store.

    Secrets live *outside* every prompt. A project file tells you the **name**
    of a secret; you fetch the value here, only when a task actually needs it.
    The value is encrypted at rest (Fernet), and the master password is
    protected with Windows DPAPI, bound to the current user.

    Use ``action="list"`` to see the available secret names and their hints —
    the listing never contains values.

    Args:
        name: The secret name, e.g. "deploy-token" or "ssh/prod-key".
        action: "get" (default), "set", "list" or "delete".
        value: The new value — for action="set" only.
        hint: A short, non-sensitive description of the secret.
        scope: "auto" (project scope when a project is resolved, else global),
            "global" or "project".
        project: Project slug/path to scope to. Omit for the current project.
    """
    action = (action or "get").strip().lower()
    if action not in ("get", "set", "list", "delete"):
        return "[perun] action must be one of: get, set, list, delete"
    p = _resolve_project(project)
    pid = int(p["id"]) if p else 0

    if action == "list":
        rows = secret_store.list_secrets(project_id=pid)
        if not rows:
            return "[perun] no secrets stored."
        out = ["[perun] secrets (names only):"]
        for r in rows:
            scope_txt = f"{r['scope']}" + (f":{r['project_id']}" if r["scope"] == "project" else "")
            hint_txt = f" — {r['hint']}" if r["hint"] else ""
            out.append(f"  - {r['name']} [{scope_txt}]{hint_txt}")
        return "\n".join(out)

    if action == "set":
        if not name or not value:
            return "[perun] set needs both a name and a value"
        use_scope = (scope or "auto").strip().lower()
        if use_scope == "auto":
            use_scope = "project" if pid else "global"
        if use_scope not in ("global", "project"):
            return "[perun] scope must be one of: auto, global, project"
        res = secret_store.set_secret(name, value, scope=use_scope,
                                      project_id=pid, hint=hint)
        if not res.get("ok"):
            return f"[perun] could not store the secret: {res.get('reason')}" \
                   + (f" ({res['hint']})" if res.get("hint") else "")
        return f"[perun] stored '{res['name']}' in {res['scope']} scope."

    if action == "delete":
        removed = secret_store.delete_secret(name, project_id=pid)
        return (f"[perun] deleted '{secret_store.normalize_name(name)}'."
                if removed else f"[perun] no secret named '{name}'.")

    # action == "get"
    if not name:
        return "[perun] get needs a name (or use action=\"list\")"
    row = secret_store.get_secret(name, project_id=pid)
    if row is None:
        return f"[perun] no secret named '{name}'."
    if row.get("error"):
        return f"[perun] {row['error']}"
    return (f"[perun] secret '{row['name']}' ({row['scope']} scope)\n"
            f"{(row['hint'] + chr(10)) if row['hint'] else ''}"
            f"{row['value']}")


@mcp.tool
def perun_docs_index(project: str | None = None) -> str:
    """(Re)index a project's docs (CODEMAP/CHANGELOG/TECHNICAL/README) for search.

    Args:
        project: Project slug/path. Omit for the current project.
    """
    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    res = memory.index_project_docs(p["id"])
    if not res.get("ok"):
        return f"[perun] Index failed: {res.get('reason')}"
    return (f"[perun] Indexed {res.get('chunks', 0)} chunk(s) from "
            f"{len(res.get('files', []))} file(s): {', '.join(res.get('files', []))}")


@mcp.tool
def perun_get_instructions(project: str | None = None, filename: str = "") -> str:
    """Read the custom instructions stored for a project.

    Instructions are scoped to a single project and are injected into every new
    agent session started in that project. If ``filename`` is omitted, the
    effective combined instructions from ``.perun/instructions/*.md`` are
    returned. If ``filename`` is given, that specific file is returned.

    Args:
        project: Project slug/path. Omit for the current project.
        filename: Optional project-local instruction filename to read.
    """
    from perun import orchestrator

    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    filename = (filename or "").strip()
    if filename:
        path, error = _project_instruction_file_path(p, filename)
        if error:
            return error
        if not path.exists():
            return f"[perun] Instruction file not found: {path.name}"
        name, enabled, body = orchestrator._load_project_instruction_file(path)
        return (f"[perun] Instruction file {path.name} for {p['slug']} "
                f"(enabled={enabled}):\n---\nname: {name}\nenabled: {enabled}\n---\n\n{body}")
    blocks = orchestrator._project_instruction_blocks(p)
    if not blocks:
        return f"[perun] No custom instructions set for {p['slug']}."
    return f"[perun] Effective instructions for {p['slug']}:\n\n" + "\n\n".join(blocks)


@mcp.tool
def perun_set_instructions(instructions: str,
                           filename: str = "",
                           enabled: bool = True,
                           project: str | None = None,
                           confirm: bool = False) -> str:
    """Set the custom instructions for a project.

    Writes ``instructions`` to ``.perun/instructions/<filename>`` (default
    ``default.md``) with YAML front matter.  Pass an empty ``instructions`` string
    to clear the file.

    Args:
        instructions: The full instruction text. Pass an empty string to clear.
        filename: Optional ``.perun/instructions/<filename>`` file to write.
        enabled: Front-matter enabled flag when writing by filename (default True).
        project: Project slug/path. Omit for the current project.
        confirm: Must be ``True``. Instructions are durable and are injected into
            every *new* session of the project, so this deliberately cannot
            happen as a side effect of reading file content.
    """
    from perun import orchestrator

    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    filename = (filename or "").strip()
    if not filename:
        filename = "default.md"
    path, error = _project_instruction_file_path(p, filename)
    if error:
        return error
    if not confirm:
        return ("[perun] Refused: pass confirm=True to change "
                f"`.perun/instructions/{path.name}` — instructions apply to every "
                "new session of this project.")
    name = path.stem
    text = orchestrator._build_frontmatter({"name": name, "enabled": enabled}) + f"{instructions}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    registry.log_activity(p["id"], "config", f"Instruction file {path.name} saved")
    if not instructions.strip():
        return f"[perun] Cleared instruction file {path.name} for {p['slug']}."
    return f"[perun] Wrote instruction file {path.name} for {p['slug']} ({len(instructions)} chars)."


@mcp.tool
def perun_list_instruction_files(project: str | None = None) -> str:
    """List project-local instruction Markdown files for a project.

    Args:
        project: Project slug/path. Omit for the current project.
    """
    from perun import orchestrator

    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    files = orchestrator._project_instruction_files(p)
    if not files:
        return f"[perun] No instruction files for {p['slug']}."
    lines = [f"[perun] Instruction files for {p['slug']}:"]
    for path in files:
        name, enabled, body = orchestrator._load_project_instruction_file(path)
        lines.append(f"  - {path.name}: {name} (enabled={enabled}, {len(body)} chars)")
    return "\n".join(lines)


@mcp.tool
def perun_grep(pattern: str, project: str | None = None,
               case_sensitive: bool = True, whole_word: bool = False,
               include: str = "", max_results: int = 50) -> str:
    """Search file contents in a project with ripgrep (or a Python fallback).

    Args:
        pattern: POSIX/ripgrep regex.
        project: Project slug/path. Omit for the current project.
        case_sensitive: False to ignore case.
        whole_word: True to match whole words only.
        include: Optional glob filter (e.g. "*.py").
        max_results: Maximum matches to return (1-200).
    """
    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    if not (pattern or "").strip():
        return "[perun] pattern is required"
    max_results, error = _safe_int(max_results, "max_results", min_val=1, max_val=200)
    if error:
        return error
    result = grep.rg_search(
        pattern, p["path"],
        case_sensitive=case_sensitive, whole_word=whole_word,
        include=include or None, max_results=max_results,
    )
    if not result.get("ok"):
        return f"[perun] search failed: {result.get('error')}"
    matches = result.get("matches", [])
    if not matches:
        return "[perun] No matches."
    lines = [f"{m['file']}:{m['line']}:{m['column']}  {m['text']}" for m in matches]
    return "[perun] Matches:\n" + "\n".join(lines) + f"\n\n(tool={result.get('tool')}, total={len(matches)})"


@mcp.tool
def perun_ast_grep(rule: str, project: str | None = None,
                   max_results: int = 50) -> str:
    """Structural code search in a project with ast-grep (or a Python fallback).

    Args:
        rule: An ast-grep rule, e.g. "$F($$$ARGS)" or a YAML rule.
        project: Project slug/path. Omit for the current project.
        max_results: Maximum matches to return (1-200).
    """
    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    if not (rule or "").strip():
        return "[perun] rule is required"
    max_results, error = _safe_int(max_results, "max_results", min_val=1, max_val=200)
    if error:
        return error
    result = grep.ast_grep_search(rule, p["path"], max_results=max_results)
    if not result.get("ok"):
        return f"[perun] search failed: {result.get('error')}"
    matches = result.get("matches", [])
    if not matches:
        return "[perun] No matches."
    lines = [f"{m['file']}:{m['line']}:{m['column']}  {m['text']}" for m in matches]
    return "[perun] Matches:\n" + "\n".join(lines) + f"\n\n(tool={result.get('tool')}, total={len(matches)})"


@mcp.tool
def perun_global_grep(pattern: str, projects: list[str] | None = None,
                      case_sensitive: bool = True, whole_word: bool = False,
                      include: str = "", max_results: int = 20) -> str:
    """Regex content search across *all* registered projects (or a subset).

    Args:
        pattern: POSIX/ripgrep regex.
        projects: Optional list of project slugs to restrict the search.
        case_sensitive: False to ignore case.
        whole_word: True to match whole words only.
        include: Optional glob filter (e.g. "*.py").
        max_results: Maximum total matches to return (default 20, max 200).
    """
    if not (pattern or "").strip():
        return "[perun] pattern is required"
    max_results, error = _safe_int(max_results, "max_results", min_val=1, max_val=200)
    if error:
        return error
    projects, error = _validate_projects_list(projects)
    if error:
        return error
    all_projects = registry.list_projects()
    if projects is not None:
        allowed = {p.strip().lower() for p in projects if isinstance(p, str) and p.strip()}
        all_projects = [p for p in all_projects if p["slug"].lower() in allowed]
    if not all_projects:
        return "[perun] No projects to search."
    result = grep.rg_search_global(
        pattern, all_projects,
        case_sensitive=case_sensitive, whole_word=whole_word,
        include=include or None, max_results=max_results,
    )
    if not result.get("ok"):
        return f"[perun] search failed: {result.get('error')}"
    matches = result.get("matches", [])
    if not matches:
        return "[perun] No matches."
    lines = [f"{m.get('project','?')}/{m['file']}:{m['line']}:{m['column']}  {m['text']}" for m in matches]
    return "[perun] Matches:\n" + "\n".join(lines) + f"\n\n(tool={result.get('tool')}, projects={result.get('projects_searched')}, total={len(matches)})"


@mcp.tool
def perun_global_ast_grep(rule: str, projects: list[str] | None = None,
                          max_results: int = 20) -> str:
    """Structural code search across *all* registered projects (or a subset).

    Args:
        rule: An ast-grep rule, e.g. "$F($$$ARGS)" or a YAML rule.
        projects: Optional list of project slugs to restrict the search.
        max_results: Maximum total matches to return (default 20, max 200).
    """
    if not (rule or "").strip():
        return "[perun] rule is required"
    max_results, error = _safe_int(max_results, "max_results", min_val=1, max_val=200)
    if error:
        return error
    projects, error = _validate_projects_list(projects)
    if error:
        return error
    all_projects = registry.list_projects()
    if projects is not None:
        allowed = {p.strip().lower() for p in projects if isinstance(p, str) and p.strip()}
        all_projects = [p for p in all_projects if p["slug"].lower() in allowed]
    if not all_projects:
        return "[perun] No projects to search."
    result = grep.ast_grep_search_global(rule, all_projects, max_results=max_results)
    if not result.get("ok"):
        return f"[perun] search failed: {result.get('error')}"
    matches = result.get("matches", [])
    if not matches:
        return "[perun] No matches."
    lines = [f"{m.get('project','?')}/{m['file']}:{m['line']}:{m['column']}  {m['text']}" for m in matches]
    return "[perun] Matches:\n" + "\n".join(lines) + f"\n\n(tool={result.get('tool')}, projects={result.get('projects_searched')}, total={len(matches)})"


@mcp.tool
def perun_unified_search(
    query: str = "",
    pattern: str = "",
    rule: str = "",
    projects: list[str] | None = None,
    memory: bool = True,
    grep: bool = True,
    ast_grep: bool = True,
    max_results: int = 20,
    max_memory: int = 6,
) -> str:
    """Unified search across all projects: vector memory + global grep + ast-grep.

    This is the preferred Perun search tool. Use it after consulting CODEMAP.

    Args:
        query: Natural-language query for semantic/conceptual memory search.
        pattern: Optional POSIX regex for global grep.
        rule: Optional ast-grep rule for structural search.
        projects: Optional project slugs to restrict the search.
        memory: Enable vector memory search (default True).
        grep: Enable global regex search when pattern is given (default True).
        ast_grep: Enable global structural search when rule is given (default True).
        max_results: Maximum code-level results to return (default 20, max 200).
        max_memory: Maximum memory results to return (default 6, clamped 1-50).

    Returns:
        Compact, ranked results with source attribution.
    """
    from perun import search as search_mod
    max_results, error = _safe_int(max_results, "max_results", min_val=1, max_val=200)
    if error:
        return error
    max_memory, error = _safe_int(max_memory, "max_memory", min_val=1, max_val=50)
    if error:
        return error
    projects, error = _validate_projects_list(projects)
    if error:
        return error
    result = search_mod.unified_search(
        query=query,
        pattern=pattern,
        rule=rule,
        projects=projects,
        use_memory=memory,
        use_grep=grep,
        use_ast_grep=ast_grep,
        max_code=max_results,
        max_memory=max_memory,
    )
    if not result.get("ok"):
        return f"[perun] search failed: {result.get('error')}"
    lines = [f"Tools used: {', '.join(result.get('tools_used', []))}",
             f"Projects searched: {result.get('projects_searched', 0)}", ""]
    if result.get("memory"):
        lines.append("Memory matches:")
        for m in result["memory"]:
            lines.append(f"  {m['project']}/{m.get('file','')}#{m.get('section','')}  {m['text']}")
    if result.get("code"):
        lines.append("Code matches:")
        for m in result["code"]:
            lines.append(f"  {m['project']}/{m['file']}:{m['line']}:{m['column']}  {m['text']}")
    return "\n".join(lines)


@mcp.tool
def perun_get_manifest(project: str | None = None) -> str:
    """Read a project's Perun manifest (perun.md front matter + body).

    Args:
        project: Project slug/path. Omit for the current project.
    """
    import json as _json

    from perun import manifest
    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    snap = manifest.load(p["path"])
    if not snap["found"]:
        return (f"[perun] No manifest for {p['slug']}. Create one with "
                "`perun_set_manifest`, or generate one from the dashboard.")
    lines = [f"[perun] Manifest for {p['slug']} ({snap['path']}):"]
    lines.append(_json.dumps(snap["data"], indent=2, ensure_ascii=False))
    if snap["body"].strip():
        lines.append("--- body ---")
        lines.append(snap["body"].strip()[:4000])
    if snap["warnings"]:
        lines.append("warnings: " + "; ".join(snap["warnings"]))
    return "\n".join(lines)


@mcp.tool
def perun_set_manifest(data: dict, body: str = "", project: str | None = None,
                       confirm: bool = False) -> str:
    """Write a project's Perun manifest (perun.md).

    Args:
        data: The front-matter mapping (name, tags, docs, commands, anchors, …).
        body: Optional Markdown body shown as the project overview.
        project: Project slug/path. Omit for the current project.
        confirm: Must be ``True``. A manifest can seed durable instructions that
            are injected into every new session, so writing one cannot happen as
            a side effect.
    """
    from perun import manifest, projects, registry
    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    if not confirm:
        return ("[perun] Refused: pass confirm=True to write "
                f"`{p['path']}/perun.md` — a manifest can carry instructions that "
                "apply to every new session.")
    try:
        target = manifest.save(p["path"], data or {}, body or "")
    except manifest.ManifestError as exc:
        return f"[perun] Could not write manifest: {exc}"
    projects.sync_manifest(p)
    registry.log_activity(p["id"], "config", "Manifest updated via MCP")
    return f"[perun] Wrote manifest for {p['slug']} ({target})."


@mcp.tool
def perun_start_session(prompt: str, project: str | None = None,
                        model: str = "", agent: str = "",
                        session: str = "") -> str:
    """Start an OpenCode agent run in a project (non-blocking).

    Returns a run id; poll it with ``perun_run_status``.

    Args:
        prompt: The instruction for the target project's agent.
        project: Project slug/path. Omit for the current project.
        model: Optional model override.
        agent: Optional agent override.
        session: Optional OpenCode session id to continue.
    """
    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    orch = _mcp_orchestrator()
    try:
        run = orch.start_run(p["slug"], prompt, model=model, agent=agent,
                             opencode_session_id=session)
    except ValueError as exc:
        return f"[perun] Could not start run: {exc}"
    return (f"[perun] Started run {run['run_uid']} in {p['slug']}. "
            f"Poll with perun_run_status(run_id='{run['run_uid']}').")


@mcp.tool
def perun_ask(prompt: str, project: str | None = None, model: str = "",
              agent: str = "", session: str = "", timeout: int = 1800) -> str:
    """Run a prompt in a project's agent and wait for the reply.

    Args:
        prompt: Question/instruction for the agent.
        project: Project slug/path. Omit for the current project.
        model: Optional model override.
        agent: Optional agent override.
        session: Optional OpenCode session id to continue.
        timeout: Seconds to wait (default 1800).
    """
    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    orch = _mcp_orchestrator()
    result = orch.run_and_wait(p["slug"], prompt, timeout=timeout, model=model,
                               agent=agent, opencode_session_id=session)
    run = result.get("run") or {}
    reply = (result.get("reply") or run.get("output") or "").strip()
    status = run.get("status", "?")
    if status != "done" and not reply:
        return f"[perun] Run {status} in {p['slug']}: {(run.get('error') or '')[:800]}"
    return f"[{p['slug']}] {reply or '(no reply)'}"


@mcp.tool
def perun_run_status(run_id: str) -> str:
    """Status and (partial) output of a Perun agent run.

    Args:
        run_id: The run id returned by ``perun_start_session``.
    """
    run = registry.get_run(run_id)
    if not run:
        return f"[perun] No such run: {run_id}"
    proj = registry.get_project(run["project_id"])
    lines = [
        f"[perun] run {run_id}",
        f"  project: {proj['slug'] if proj else run['project_id']}",
        f"  status:  {run['status']}",
        f"  exit:    {run.get('exit_code')}",
    ]
    if run.get("error"):
        lines.append(f"  error:   {run['error'][:500]}")
    out = (run.get("output") or "").strip()
    if out:
        lines.append("  output:")
        lines.append(out[:2000])
    return "\n".join(lines)


@mcp.tool
def perun_telegram_send(text: str, chat_id: str = "") -> str:
    """Send a Telegram message to a chat allowed by Perun's configuration.

    The target must be one of the configured chats (``config.json``
    ``telegram_allowed_chats`` / ``TELEGRAM_CHAT_ID``). An arbitrary ``chat_id``
    is rejected: otherwise an agent tricked by file content could use this as an
    exfiltration channel to any Telegram user.

    Args:
        text: Message body (Markdown allowed).
        chat_id: Optional override; must be on the allowed list.
    """
    import httpx
    settings = config.Settings.load()
    token, chats = settings.telegram()
    if not token:
        return "[perun] No Telegram token configured (settings or TELEGRAM_BOT_TOKEN)."
    if not chats:
        return ("[perun] No chat id configured — pair a chat with the bridge "
                "(dashboard → Telegram) before sending messages.")
    if chat_id and chat_id not in chats:
        return ("[perun] Refused: chat_id is not in the configured allow-list "
                f"({', '.join(chats)}). Add it in the dashboard under Telegram.")
    target = chat_id or chats[0]
    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": target, "text": text,
                      "disable_web_page_preview": True},
            ).json()
    except Exception as exc:
        return f"[perun] Telegram send failed: {exc}"
    return "[perun] Sent." if res.get("ok") else f"[perun] Telegram error: {res.get('description')}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
