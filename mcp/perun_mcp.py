#!/usr/bin/env python3
"""Perun MCP server — expose the overseer to any OpenCode project.

Installed globally by ``python -m perun install`` so *every* project inherits
these tools without per-project configuration. The server is deliberately
thin: it just forwards to the Perun package (registry, memory, orchestrator).

Tools
-----
``perun_list_projects``  List every registered project.
``perun_project_info``   Details + recent activity for one project.
``perun_memory_search``  Search facts + indexed docs (this or all projects).
``perun_memory_store``   Store a durable fact (global or project scope).
``perun_docs_index``     (Re)index a project's docs into the vector store.
``perun_get_instructions`` Read a project's custom instructions.
``perun_set_instructions`` Set a project's custom instructions.
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
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastmcp import FastMCP  # noqa: E402

from perun import config, db, memory, registry  # noqa: E402

mcp = FastMCP("Perun")

db.init_db()


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
    return (f"{p['slug']}  —  {p['name']} ({p['kind']})"
            f"  [{p['path']}]")


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
    k = max(1, min(int(k), 8))
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
def perun_get_instructions(project: str | None = None) -> str:
    """Read the custom instructions stored for a project.

    Instructions are scoped to a single project and are injected into every new
    agent session started in that project.

    Args:
        project: Project slug/path. Omit for the current project.
    """
    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    detail = registry.get_project(p["slug"]) or p
    text = (detail.get("instructions") or "").strip()
    if not text:
        return f"[perun] No custom instructions set for {p['slug']}."
    return f"[perun] Instructions for {p['slug']}:\n{text}"


@mcp.tool
def perun_set_instructions(instructions: str, project: str | None = None) -> str:
    """Set the custom instructions for a project (replaces the current text).

    The text is scoped to this project only and is prepended to every new agent
    session started in it.

    Args:
        instructions: The full instruction text. Pass an empty string to clear.
        project: Project slug/path. Omit for the current project.
    """
    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    registry.update_project(p["slug"], instructions=instructions or "")
    registry.log_activity(p["id"], "config", "Project instructions updated")
    if not (instructions or "").strip():
        return f"[perun] Cleared instructions for {p['slug']}."
    return f"[perun] Updated instructions for {p['slug']} ({len(instructions)} chars)."


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
def perun_set_manifest(data: dict, body: str = "", project: str | None = None) -> str:
    """Write a project's Perun manifest (perun.md).

    Args:
        data: The front-matter mapping (name, tags, docs, commands, anchors, …).
        body: Optional Markdown body shown as the project overview.
        project: Project slug/path. Omit for the current project.
    """
    from perun import manifest, registry
    p = _resolve_project(project)
    if not p:
        return "[perun] No project resolved; pass `project`."
    try:
        target = manifest.save(p["path"], data or {}, body or "")
    except manifest.ManifestError as exc:
        return f"[perun] Could not write manifest: {exc}"
    snap = manifest.load(p["path"])
    declared = manifest.to_project_fields(snap["data"])
    if declared:
        registry.update_project(p["slug"], **declared)
    registry.set_project_manifest(p["slug"], manifest.snapshot(snap), str(target))
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
    from perun.orchestrator import Orchestrator
    orch = Orchestrator()
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
    from perun.orchestrator import Orchestrator
    orch = Orchestrator(config.Settings.load())
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
    """Send a Telegram message to the configured Perun chat.

    Args:
        text: Message body (Markdown allowed).
        chat_id: Optional override; defaults to the configured chat.
    """
    import httpx
    settings = config.Settings.load()
    token, chats = settings.telegram()
    if not token:
        return "[perun] No Telegram token configured (settings or TELEGRAM_BOT_TOKEN)."
    target = chat_id or (chats[0] if chats else "")
    if not target:
        return "[perun] No chat id configured (settings or TELEGRAM_CHAT_ID)."
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
