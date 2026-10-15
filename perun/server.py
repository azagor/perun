"""Perun HTTP service — REST API + Server-Sent Events + the web dashboard.

One process exposes:

* ``/api/*``      — JSON API consumed by the dashboard (and any script).
* ``/api/*/stream`` — SSE streams of live agent output / activity.
* ``/``           — the static single-page dashboard in ``web/``.

Sync endpoints are declared as ``def`` so FastAPI runs them in its thread pool;
the agent orchestrator is blocking by design and this keeps the event loop
free. SSE endpoints are ``async`` and pump the blocking event bus through a
background thread, so slow clients never stall a run.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from urllib.parse import urlsplit

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import iterate_in_threadpool

from . import (__version__, config, control, db, discovery, git, manifest,
               memory, projects, registry, scaffold)
from .orchestrator import Orchestrator

STARTED_AT = time.time()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

SETTINGS = config.Settings.load()
ORCH = Orchestrator(SETTINGS)


def _control_project() -> dict[str, Any]:
    """Return the control-plane project, ensuring it exists (idempotent).

    The control plane is the self-registered Perun repo — the agent you talk to
    when no project is selected (see :mod:`perun.control`). Creating it lazily
    keeps the dashboard usable on a machine with zero user projects.
    """
    try:
        return control.ensure()
    except Exception:  # pragma: no cover - best-effort
        return {}


def _control_slug() -> str:
    return _control_project().get("slug", "")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    config.ensure_dirs()
    _control_project()
    # After a restart we lose all in-memory process tracking; any run still marked
    # running is effectively stale. Finalize them so cards don't stay lit forever.
    for run in registry.list_active_runs():
        registry.update_run(run["run_uid"], status="done", ended_at=registry.now_iso())
    if SETTINGS.auto_discover_on_start:
        try:
            discovery.discover_and_sync(SETTINGS.roots, max_depth=1)
        except Exception:  # pragma: no cover - discovery is best-effort
            pass
    yield


app = FastAPI(title="Perun", version=__version__, docs_url="/api/docs",
              openapi_url="/api/openapi.json", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Same-origin / anti-DNS-rebinding guard
#
# The dashboard is an intentionally local, unauthenticated API. The two realistic
# remote attack paths are:
#
#   * CSRF — a website the user visits silently POSTs to http://127.0.0.1:<port>.
#   * DNS rebinding — an attacker's domain resolves to 127.0.0.1, so the browser
#     treats their page as "same origin" with Perun and grants full API access
#     (including starting agent runs via ``POST /api/runs``).
#
# Both are defeated by requiring the ``Host`` header to address this machine and,
# when a browser sends ``Origin``, requiring it to match too. Loopback is always
# accepted; a deliberately non-loopback bind (a concrete address or a wildcard)
# is also accepted so the operator can expose it on a trusted network.
# --------------------------------------------------------------------------- #
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
_WILDCARD_BINDS = {"", "*", "0.0.0.0", "::", "[::]"}


def _hostname(value: str) -> str:
    """Return the bare hostname from a ``Host`` header or an ``Origin`` URL."""
    v = (value or "").strip()
    if not v:
        return ""
    if "//" in v:
        v = urlsplit(v).netloc
    if v.startswith("["):            # [::1]:8760
        return v[1:].split("]", 1)[0].lower()
    if v.count(":") == 1:            # host:port
        return v.rsplit(":", 1)[0].lower()
    return v.lower()


def _host_allowed(host_header: str) -> bool:
    host = _hostname(host_header)
    if not host:
        return False
    if host in _LOOPBACK_NAMES:
        return True
    configured = (SETTINGS.host or "").strip().lower()
    if configured in _WILDCARD_BINDS:
        return True                  # operator explicitly bound all interfaces
    return host == _hostname(SETTINGS.host)


def _origin_ok(origin: str, host_header: str) -> bool:
    """True when a browser ``Origin`` is same-host or loopback."""
    o = _hostname(origin)
    if not o:
        return False
    if o in _LOOPBACK_NAMES and _hostname(host_header) in _LOOPBACK_NAMES:
        return True                  # localhost <-> 127.0.0.1 aliasing
    return o == _hostname(host_header)


@app.middleware("http")
async def _origin_guard(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        host_header = request.headers.get("host", "")
        if not _host_allowed(host_header):
            return JSONResponse(
                {"detail": "blocked: unexpected Host header (possible DNS rebinding)"},
                status_code=403)
        origin = request.headers.get("origin")
        if origin and not _origin_ok(origin, host_header):
            return JSONResponse(
                {"detail": "blocked: cross-origin request"},
                status_code=403)
    return await call_next(request)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _project_or_404(slug: str) -> dict[str, Any]:
    project = registry.get_project(slug)
    if not project:
        raise HTTPException(status_code=404, detail=f"no such project: {slug}")
    return project


def _resolve_project(ref: str | int | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    return registry.get_project(ref)


def _ok(**kw: Any) -> dict[str, Any]:
    return {"ok": True, **kw}


# --------------------------------------------------------------------------- #
# Health & meta
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "uptime": int(time.time() - STARTED_AT),
        "projects": len(registry.list_projects()),
        "active_runs": ORCH.active(),
        "home": str(config.HOME),
        # Slug of the control-plane project, so the dashboard can open a chat
        # with the orchestrator even when nothing else is registered.
        "control": _control_slug(),
    }


_MODELS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_MODELS_TTL = 300.0
_AGENTS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_AGENTS_TTL = 60.0


@app.get("/api/models")
def models(refresh: bool = False) -> dict[str, Any]:
    """Every model offered by the connected OpenCode providers (cached).

    Returns structured entries ``{id, provider, name}`` ready for the
    dashboard's searchable model picker, plus the raw CLI output for
    diagnostics. The CLI is the source of truth for "connected providers".
    """
    import subprocess

    from .orchestrator import resolve_opencode

    now = time.time()
    if (not refresh and _MODELS_CACHE["data"] is not None
            and now - _MODELS_CACHE["ts"] < _MODELS_TTL):
        return _MODELS_CACHE["data"]

    argv = resolve_opencode("opencode") + ["models"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                              encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"ok": False, "models": [], "providers": [], "count": 0,
                "error": str(exc)}

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in (proc.stdout or "").splitlines():
        mid = raw.strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        provider, _, name = mid.partition("/")
        if not name:
            provider, name = "", mid
        entries.append({"id": mid, "provider": provider or "other",
                        "name": name, "label": mid})
    entries.sort(key=lambda m: (m["provider"].lower(), m["name"].lower()))
    providers = sorted({m["provider"] for m in entries}, key=str.lower)
    ok = proc.returncode == 0
    data = {
        "ok": ok,
        "models": entries,
        "providers": providers,
        "count": len(entries),
        "error": None if ok else (proc.stderr or "").strip()[:500] or "opencode models failed",
        "raw": (proc.stdout or "")[:4000],
    }
    _MODELS_CACHE.update(ts=now, data=data)
    return data


@app.get("/api/agents")
def api_list_agents(refresh: bool = False) -> dict[str, Any]:
    """OpenCode agents available to the current installation / project.

    Combines documented built-ins with user-defined agents found on disk under
    ``~/.config/opencode/agents/`` and, when a project path is provided, the
    project-local ``.opencode/agents/`` directory.
    """
    from . import agents

    now = time.time()
    if (not refresh and _AGENTS_CACHE["data"] is not None
            and now - _AGENTS_CACHE["ts"] < _AGENTS_TTL):
        return _AGENTS_CACHE["data"]
    entries = agents.list_agents()
    # Alphabetical, but keep primary agents before subagents for readability.
    def sort_key(a: dict[str, Any]) -> tuple[str, str]:
        mode = {"primary": "0", "all": "1"}.get(a.get("mode", ""), "2")
        return (mode, (a.get("name") or a.get("id") or "").lower())

    entries.sort(key=sort_key)
    data = {"ok": True, "agents": entries, "count": len(entries)}
    _AGENTS_CACHE.update(ts=now, data=data)
    return data


@app.get("/api/skills")
def api_list_skills(project: str = "") -> dict[str, Any]:
    """OpenCode agent skills available globally and (optionally) for a project."""
    from . import skills

    path = None
    if project:
        p = registry.get_project(project)
        if p:
            path = p["path"]
    skills_list = skills.discover_skills(path)
    return _ok(skills=skills_list, count=len(skills_list))


@app.get("/api/tools")
def api_list_tools(project: str = "") -> dict[str, Any]:
    """Tools available to OpenCode: built-ins plus custom project/global tools."""
    from . import skills

    path = None
    if project:
        p = registry.get_project(project)
        if p:
            path = p["path"]
    tools_list = skills.discover_tools(path)
    return _ok(tools=tools_list, count=len(tools_list))


@app.get("/api/terminals")
def api_terminals() -> dict[str, Any]:
    """Terminals/shells available on this machine.

    The dashboard uses this to let the user pick which command interpreter to
    open when copying or launching a command. The configured preference is
    returned alongside the detected list.
    """
    import shutil

    candidates = []
    if os.name == "nt":
        roots = os.environ.get("PATH", "").split(os.pathsep)
        for name, label in [
            ("pwsh.exe", "PowerShell 7+"),
            ("pwsh", "PowerShell 7+"),
            ("powershell.exe", "Windows PowerShell"),
            ("powershell", "Windows PowerShell"),
            ("cmd.exe", "Command Prompt"),
            ("cmd", "Command Prompt"),
            ("wt.exe", "Windows Terminal"),
            ("wt", "Windows Terminal"),
        ]:
            path = shutil.which(name)
            if path and not any(c["id"].lower() == name.lower().replace(".exe", "") for c in candidates):
                candidates.append({"id": name.lower().replace(".exe", ""), "name": label, "path": path})
    else:
        for name, label in [
            (os.environ.get("SHELL", "").split("/")[-1], "Current shell"),
            ("bash", "Bash"),
            ("zsh", "Zsh"),
            ("fish", "Fish"),
            ("sh", "sh"),
        ]:
            if not name:
                continue
            path = shutil.which(name)
            if path and not any(c["id"] == name for c in candidates):
                candidates.append({"id": name, "name": label, "path": path})
    source = SETTINGS.terminal_sources()
    return _ok(
        terminals=candidates,
        configured=SETTINGS.terminal.strip(),
        configured_source=source["source"],
        effective_path=source["path"],
    )


@app.post("/api/run-in-terminal")
def api_run_in_terminal(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Open the user's preferred terminal with the equivalent opencode command.

    The frontend computes the command text; Perun only starts the configured
    shell/terminal with that command. This keeps shell-quoting decisions next to
    the UI that produced them.
    """
    project_ref = payload.get("project") or ""
    cwd = (payload.get("cwd") or "").strip()
    cmd_text = (payload.get("command") or "").strip()
    if not cmd_text:
        raise HTTPException(status_code=400, detail="command is required")
    if project_ref and not cwd:
        project = registry.get_project(project_ref)
        if project:
            cwd = project["path"]
    if cwd and not Path(cwd).is_dir():
        raise HTTPException(status_code=400, detail=f"cwd does not exist: {cwd}")

    shell = payload.get("shell") or SETTINGS.terminal.strip() or ""
    argv = SETTINGS.terminal_cmd(shell_hint=shell)
    # Derive the shell class from the resolved executable so an empty/default
    # shell still gets the right command-line syntax.
    exe = os.path.basename(argv[0]).lower().replace(".exe", "")
    shell_cls = shell.lower().replace(".exe", "") or exe
    # Windows Terminal needs the starting directory; then it should run the
    # provided command. PowerShell/cmd are launched with a single command line.
    if shell_cls in ("wt",):
        wt_argv = list(argv)
        if cwd:
            try:
                d_idx = wt_argv.index("-d")
                wt_argv[d_idx + 1] = cwd
            except (ValueError, IndexError):
                wt_argv.extend(["-d", cwd])
        # Keep the tab open after opencode exits so the user can see results.
        wt_argv.extend(["cmd", "/k", cmd_text])
        argv = wt_argv
    elif shell_cls in ("cmd",):
        # /k keeps the console window alive after opencode exits.
        argv = [argv[0], "/k", cmd_text]
    elif shell_cls in ("powershell", "pwsh"):
        # Launch PowerShell directly with a new console. Using subprocess with
        # CREATE_NEW_CONSOLE avoids the fragile cmd.exe "start" quoting dance.
        # terminal_cmd() already returns [pwsh, -NoExit, -Command]; if it does,
        # just append the command text, otherwise build the argv from scratch.
        if len(argv) >= 3 and argv[-2] == "-NoExit" and argv[-1] == "-Command":
            argv = list(argv) + [cmd_text]
        else:
            argv = [argv[0], "-NoExit", "-Command", cmd_text]
    else:
        # Generic fallback: pass the command through the shell.
        argv = argv + ["-c", cmd_text]

    try:
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        else:
            creationflags = 0
        proc = subprocess.Popen(
            argv,
            cwd=cwd or None,
            creationflags=creationflags,
        )
        run_uid = (payload.get("run_uid") or "").strip()
        if run_uid:
            from . import process_tracker
            process_tracker.TRACKER.register(run_uid, proc, shell=shell)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not open terminal: {exc}")
    return _ok(launched=True, shell=shell, command=cmd_text, pid=proc.pid if 'proc' in locals() else None)


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
@app.get("/api/projects")
def api_list_projects() -> dict[str, Any]:
    return {"ok": True, "projects": registry.list_projects()}


@app.post("/api/projects")
def api_add_project(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Register a project (shared by the dashboard wizard and the API).

    ``method`` selects the path: ``existing`` (default; may point at a path that
    is not created yet), ``new`` (create + optional bootstrap), or ``clone``
    (clone a remote into the target path). Optional git fields (``git_init``,
    ``git_remote``, ``git_branch``) configure the per-project repository, and
    ``index_docs`` indexes it immediately. The work is delegated to
    :mod:`perun.projects` so the MCP tools create identical projects.
    """
    try:
        result = projects.create(
            method=payload.get("method") or "existing",
            name=(payload.get("name") or "").strip(),
            path=(payload.get("path") or "").strip(),
            clone_url=(payload.get("clone_url") or "").strip(),
            root=(payload.get("root") or "").strip(),
            bootstrap=payload.get("bootstrap", True),
            description=payload.get("description") or "",
            kind=payload.get("kind") or "",
            tags=payload.get("tags") or "",
            model=payload.get("model") or "",
            agent=payload.get("agent") or "",
            color=payload.get("color") or "",
            instructions=payload.get("instructions") or "",
            git_init=bool(payload.get("git_init")),
            git_remote=(payload.get("git_remote") or "").strip(),
            git_branch=(payload.get("git_branch") or "").strip(),
            index_docs=bool(payload.get("index_docs")),
        )
    except projects.ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **result}


@app.get("/api/projects/{slug}")
def api_get_project(slug: str) -> dict[str, Any]:
    project = _project_or_404(slug)
    snap = registry.get_project_manifest(slug)
    project = dict(project)
    project["manifest_body"] = snap.get("body", "")
    project["manifest_source"] = snap.get("source", "")
    return {
        "ok": True,
        "project": project,
        "sessions": registry.list_sessions(project["id"], limit=50),
        "runs": registry.list_runs(project["id"], limit=25),
        "activity": registry.list_activity(project["id"], limit=40),
        "memory": memory.recent_facts(project["id"], limit=25),
        "memory_stats": memory.stats(project["id"]),
    }


@app.patch("/api/projects/{slug}")
def api_update_project(slug: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _project_or_404(slug)
    project = registry.update_project(slug, **payload)
    return _ok(project=project)


@app.delete("/api/projects/{slug}")
def api_delete_project(slug: str) -> dict[str, Any]:
    project = _project_or_404(slug)
    if control.is_control(project):
        raise HTTPException(
            status_code=400,
            detail="the Perun orchestrator project cannot be removed")
    deleted = registry.delete_project(slug)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"no such project: {slug}")
    return _ok(deleted=slug)


@app.post("/api/projects/{slug}/index")
def api_index_project(slug: str) -> dict[str, Any]:
    project = _project_or_404(slug)
    try:
        projects.sync_manifest(project)
    except Exception:  # pragma: no cover - best-effort
        pass
    return memory.index_project_docs(project["id"])


@app.get("/api/projects/{slug}/sessions")
def api_project_sessions(slug: str) -> dict[str, Any]:
    project = _project_or_404(slug)
    return _ok(sessions=registry.list_sessions(project["id"]))


@app.post("/api/projects/{slug}/session")
def api_start_project_session(slug: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Start an OpenCode session for a specific project.

    The prompt is optional; when omitted Perun uses a default starter prompt so
    the session is created in the project's scope without requiring the user to
    type one first.
    """
    project = _project_or_404(slug)
    try:
        run = ORCH.start_run(
            slug, payload.get("prompt", ""),
            session_pk=payload.get("session_pk"),
            opencode_session_id=payload.get("opencode_session_id", ""),
            model=payload.get("model", ""),
            agent=payload.get("agent", ""),
            title=payload.get("title", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ok(run=run)


@app.delete("/api/sessions/{pk}")
def api_delete_session(pk: int) -> dict[str, Any]:
    registry.delete_session(pk)
    return _ok(deleted=pk)


@app.get("/api/projects/{slug}/activity")
def api_project_activity(slug: str, limit: int = 100) -> dict[str, Any]:
    project = _project_or_404(slug)
    return _ok(activity=registry.list_activity(project["id"], limit=limit))


@app.get("/api/projects/{slug}/memory")
def api_project_memory(slug: str, limit: int = 100) -> dict[str, Any]:
    project = _project_or_404(slug)
    return _ok(facts=memory.recent_facts(project["id"], limit=limit),
               stats=memory.stats(project["id"]))


@app.get("/api/projects/{slug}/instructions")
def api_get_instructions(slug: str) -> dict[str, Any]:
    project = _project_or_404(slug)
    return _ok(instructions=project.get("instructions", "") or "")


@app.put("/api/projects/{slug}/instructions")
def api_set_instructions(slug: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Replace a project's custom instructions (project-scoped)."""
    project = _project_or_404(slug)
    text = payload.get("instructions", "")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="instructions must be a string")
    registry.update_project(slug, instructions=text)
    registry.log_activity(project["id"], "config", "Project instructions updated")
    return _ok(instructions=(registry.get_project(slug) or project).get("instructions", ""))


# --------------------------------------------------------------------------- #
# Project manifest (perun.md / perun.json)
# --------------------------------------------------------------------------- #
# `projects.sync_manifest` is the single implementation shared with the MCP
# tools; the routes below call it directly.
_sync_manifest = projects.sync_manifest


@app.get("/api/projects/{slug}/manifest")
def api_get_manifest(slug: str, live: bool = True) -> dict[str, Any]:
    """Return the project manifest.

    With ``live=true`` (default) the file is read from disk; otherwise the
    stored snapshot is returned.
    """
    project = _project_or_404(slug)
    if live:
        snap = manifest.load(project["path"])
        if not snap["found"]:
            stored = registry.get_project_manifest(slug)
            if stored["data"] or stored["body"]:
                snap = {"found": False, "path": stored["source"],
                        "data": stored["data"], "body": stored["body"],
                        "warnings": ["manifest file not found on disk; showing last snapshot"]}
    else:
        stored = registry.get_project_manifest(slug)
        snap = {"found": bool(stored["data"] or stored["body"]),
                "path": stored["source"], "data": stored["data"],
                "body": stored["body"], "warnings": []}
    return _ok(manifest=snap)


@app.post("/api/projects/{slug}/manifest/refresh")
def api_refresh_manifest(slug: str) -> dict[str, Any]:
    """Re-read the manifest from disk and update the registry + snapshot."""
    project = _project_or_404(slug)
    snap = _sync_manifest(project)
    if not snap["found"]:
        raise HTTPException(status_code=404,
                            detail="no perun.md or perun.json found in the project")
    registry.log_activity(project["id"], "config", "Manifest reloaded from disk")
    return _ok(manifest=snap, project=registry.get_project(slug))


@app.post("/api/projects/{slug}/manifest/export")
def api_export_manifest(slug: str) -> dict[str, Any]:
    """Generate a perun.md from the current registry state and write it to disk."""
    project = _project_or_404(slug)
    base = Path(project["path"])
    if not base.is_dir():
        raise HTTPException(status_code=400, detail=f"project path does not exist: {base}")
    if manifest.find(base):
        raise HTTPException(status_code=409,
                            detail="a manifest already exists; edit it directly")
    data = manifest.from_project(project)
    body = manifest.default_body(project)
    try:
        target = manifest.save(base, data, body)
    except manifest.ManifestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    snap = manifest.load(base)
    registry.set_project_manifest(slug, manifest.snapshot(snap), str(target))
    registry.log_activity(project["id"], "config", "Manifest generated from registry")
    return _ok(path=str(target), manifest=snap)


@app.put("/api/projects/{slug}/manifest")
def api_put_manifest(slug: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Write a manifest to disk.

    Accepts either raw ``content`` (written verbatim, validated) or structured
    ``data`` (+ optional ``body``)."""
    project = _project_or_404(slug)
    base = Path(project["path"])
    if not base.is_dir():
        raise HTTPException(status_code=400, detail=f"project path does not exist: {base}")

    content = payload.get("content")
    if content is not None:
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="content must be a string")
        if content.lstrip().startswith("{"):
            data, body, warnings = manifest.parse_json(content)
        else:
            data, body, warnings = manifest.parse(content)
        if any(w.startswith("invalid") for w in warnings):
            raise HTTPException(status_code=400, detail="; ".join(warnings))
        try:
            target = manifest.save(base, data, body)
        except manifest.ManifestError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    else:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="data must be an object")
        body = payload.get("body") or ""
        try:
            target = manifest.save(base, data, body)
        except manifest.ManifestError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    snap = manifest.load(base)
    declared = manifest.to_project_fields(snap["data"])
    if declared:
        registry.update_project(slug, **declared)
    registry.set_project_manifest(slug, manifest.snapshot(snap), str(target))
    registry.log_activity(project["id"], "config", "Manifest saved")
    return _ok(path=str(target), manifest=snap,
               project=registry.get_project(slug))


# --------------------------------------------------------------------------- #
# Filesystem (folder picker for the project wizard)
# --------------------------------------------------------------------------- #
@app.get("/api/fs/list")
def api_fs_list(path: str = "", show_hidden: bool = False) -> dict[str, Any]:
    target = Path(path).expanduser() if path.strip() else Path.home()
    if not target.exists():
        raise HTTPException(status_code=400, detail=f"does not exist: {target}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {target}")
    try:
        entries = list(target.iterdir())
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"permission denied: {target}")

    def visible(e) -> bool:
        return show_hidden or not e.name.startswith(".")

    dirs = sorted((e.name for e in entries if e.is_dir() and visible(e)),
                  key=str.lower)
    files = sorted((e.name for e in entries if e.is_file() and visible(e)),
                   key=str.lower)
    parent = str(target.parent) if target.parent != target else ""
    return _ok(path=str(target), parent=parent, dirs=dirs, files=files[:300],
               roots=list(SETTINGS.roots), home=str(Path.home()))


@app.post("/api/fs/mkdir")
def api_fs_mkdir(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    path = (payload.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    try:
        p, created = scaffold.create_dir(path)
    except scaffold.ScaffoldError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ok(path=str(p), created=created)


# --------------------------------------------------------------------------- #
# Git (one repository per project)
# --------------------------------------------------------------------------- #
@app.get("/api/projects/{slug}/git")
def api_git_status(slug: str) -> dict[str, Any]:
    project = _project_or_404(slug)
    return _ok(git=git.info(project["path"]), project=project)


@app.get("/api/projects/{slug}/git/log")
def api_git_log(slug: str, limit: int = 30) -> dict[str, Any]:
    project = _project_or_404(slug)
    return _ok(commits=git.log(project["path"], limit=limit))


def _git_call(fn, project: dict[str, Any], *args: Any, **kw: Any) -> dict[str, Any]:
    try:
        return fn(project["path"], *args, **kw)
    except git.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/projects/{slug}/git/init")
def api_git_init(slug: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    project = _project_or_404(slug)
    branch = (payload.get("branch") or project.get("git_branch") or "main").strip()
    info = _git_call(git.init, project, branch)
    registry.update_project(slug, git_branch=branch)
    registry.log_activity(project["id"], "git", f"Initialised repository on {branch}")
    return _ok(git=info)


@app.post("/api/projects/{slug}/git/remote")
def api_git_remote(slug: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    project = _project_or_404(slug)
    url = (payload.get("url") or payload.get("remote") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    branch = (payload.get("branch") or project.get("git_branch") or "").strip()
    info = _git_call(git.set_remote, project, url, branch)
    registry.update_project(slug, git_remote=url,
                            git_branch=branch or project.get("git_branch", ""))
    registry.log_activity(project["id"], "git", "Remote configured")
    return _ok(git=info)


@app.post("/api/projects/{slug}/git/commit")
def api_git_commit(slug: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    project = _project_or_404(slug)
    message = (payload.get("message") or "").strip()
    res = _git_call(git.commit, project, message)
    if res.get("committed"):
        registry.log_activity(project["id"], "git", f"Committed {res.get('sha')}")
    return _ok(**res)


@app.post("/api/projects/{slug}/git/push")
def api_git_push(slug: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    project = _project_or_404(slug)
    branch = (payload.get("branch") or project.get("git_branch") or "").strip()
    res = _git_call(git.push, project, branch,
                    remote=(payload.get("remote") or "origin"),
                    set_upstream=payload.get("set_upstream", True))
    registry.log_activity(project["id"], "git",
                          f"Pushed {res.get('branch')} → {res.get('remote')}")
    return _ok(**res)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
@app.post("/api/discover")
def api_discover(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    roots = payload.get("roots") or SETTINGS.roots
    max_depth = int(payload.get("max_depth", 1))
    result = discovery.discover_and_sync(roots, max_depth=max_depth)
    if SETTINGS.index_docs_on_discover:
        for slug in result.get("created", []):
            proj = registry.get_project(slug)
            if proj:
                memory.index_project_docs(proj["id"])
    return _ok(**{k: v for k, v in result.items() if k != "found"})


@app.get("/api/discover/scan")
def api_discover_scan(roots: str = "", max_depth: int = 1) -> dict[str, Any]:
    root_list = [r for r in roots.split(",") if r.strip()] or SETTINGS.roots
    return _ok(found=discovery.scan(root_list, max_depth=max_depth))


# --------------------------------------------------------------------------- #
# Runs (agent orchestration)
# --------------------------------------------------------------------------- #
@app.get("/api/runs")
def api_list_runs(project: str | None = None, limit: int = 50) -> dict[str, Any]:
    pid = None
    if project:
        p = registry.get_project(project)
        pid = p["id"] if p else None
    return _ok(runs=registry.list_runs(pid, limit=limit))


@app.get("/api/runs/active")
def api_active_runs() -> dict[str, Any]:
    """Every run that is queued or running, with its owning project.

    Drives the dashboard's live "an agent is working" layer: each entry maps a
    run to a project slug so the matching card can come alive. ``live`` lists the
    run ids with a child process in this server right now, and ``projects`` is the
    set of project slugs that are currently working (handy for the cards).
    """
    runs = registry.list_active_runs()
    slugs = sorted({r["project_slug"] for r in runs if r.get("project_slug")})
    return _ok(runs=runs, live=ORCH.active(), projects=slugs, count=len(runs))


@app.post("/api/runs")
def api_start_run(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    # No project → the control plane (the orchestrator), so a fresh machine with
    # nothing registered can still be talked to.
    project_ref = payload.get("project") or payload.get("slug") or _control_slug()
    prompt = (payload.get("prompt") or "").strip()
    if not project_ref:
        raise HTTPException(status_code=400, detail="project is required")
    try:
        run = ORCH.start_run(
            project_ref, prompt,
            session_pk=payload.get("session_pk"),
            opencode_session_id=payload.get("opencode_session_id", ""),
            model=payload.get("model", ""),
            agent=payload.get("agent", ""),
            title=payload.get("title", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ok(run=run)


@app.get("/api/runs/{run_uid}")
def api_get_run(run_uid: str) -> dict[str, Any]:
    run = registry.get_run(run_uid)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    return _ok(run=run, final=ORCH.is_final(run_uid))


@app.post("/api/runs/{run_uid}/cancel")
def api_cancel_run(run_uid: str) -> dict[str, Any]:
    # Headless orchestrator runs are cancelled through ORCH; TUI/terminal sessions
    # launched by the dashboard are tracked separately. Cancel both so the Stop
    # button always ends whatever is active.
    from . import process_tracker

    orch_cancelled = ORCH.cancel(run_uid)
    tracker_killed = False
    if process_tracker.TRACKER.get(run_uid):
        tracker_killed = process_tracker.TRACKER.terminate(run_uid)
    # If this was a TUI-only run, ORCH won't have a record; finalize explicitly.
    if tracker_killed and not orch_cancelled:
        process_tracker.TRACKER.finalize(run_uid)
    return _ok(cancelled=orch_cancelled or tracker_killed)


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #
@app.get("/api/projects/{slug}/tasks")
def api_list_tasks(slug: str) -> dict[str, Any]:
    project = registry.get_project(slug)
    if not project:
        raise HTTPException(status_code=404, detail="no such project")
    return _ok(tasks=registry.list_tasks(project["id"]))


@app.post("/api/projects/{slug}/tasks")
def api_create_task(slug: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    project = registry.get_project(slug)
    if not project:
        raise HTTPException(status_code=404, detail="no such project")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    task = registry.create_task(
        project["id"],
        name=name,
        kind=str(payload.get("kind") or "prompt").strip(),
        prompt=str(payload.get("prompt") or "").strip(),
        tools=payload.get("tools") or [],
        skills=payload.get("skills") or [],
        enabled=bool(payload.get("enabled", True)),
        sort_order=int(payload.get("sort_order") or 0),
    )
    return _ok(task=task)


@app.patch("/api/projects/{slug}/tasks/{task_id}")
def api_update_task(slug: str, task_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    project = registry.get_project(slug)
    if not project:
        raise HTTPException(status_code=404, detail="no such project")
    task = registry.get_task(task_id)
    if not task or task["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="no such task")
    updates: dict[str, Any] = {}
    if "name" in payload:
        updates["name"] = str(payload["name"]).strip()
    if "kind" in payload:
        updates["kind"] = str(payload["kind"]).strip()
    if "prompt" in payload:
        updates["prompt"] = str(payload["prompt"]).strip()
    if "tools" in payload:
        updates["tools"] = payload["tools"]
    if "skills" in payload:
        updates["skills"] = payload["skills"]
    if "enabled" in payload:
        updates["enabled"] = bool(payload["enabled"])
    if "sort_order" in payload:
        updates["sort_order"] = int(payload["sort_order"])
    if "name" in updates and not updates["name"]:
        raise HTTPException(status_code=400, detail="name is required")
    updated = registry.update_task(task_id, **updates)
    return _ok(task=updated)


@app.delete("/api/projects/{slug}/tasks/{task_id}")
def api_delete_task(slug: str, task_id: int) -> dict[str, Any]:
    project = registry.get_project(slug)
    if not project:
        raise HTTPException(status_code=404, detail="no such project")
    task = registry.get_task(task_id)
    if not task or task["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="no such task")
    registry.delete_task(task_id)
    return _ok(deleted=True)


@app.post("/api/runs/tui")
def api_start_tui_run(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Create a lightweight run record for an externally-managed TUI session.

    The dashboard uses this so that opening OpenCode in a project directory
    lights up the project card and can later be explicitly finished.
    """
    from . import registry as _registry  # local import to avoid cycle concerns

    project_ref = payload.get("project") or payload.get("slug") or _control_slug()
    if not project_ref:
        raise HTTPException(status_code=400, detail="project is required")
    project = _registry.get_project(project_ref)
    if not project:
        raise HTTPException(status_code=404, detail="no such project")
    run_uid = uuid.uuid4().hex[:16]
    prompt = (payload.get("prompt") or "").strip() or "Working session in progress."
    _registry.create_run(
        run_uid, project["id"], prompt,
        model=project.get("model", ""), model_explicit=bool(project.get("model")),
        agent=project.get("agent", ""),
    )
    _registry.update_run(run_uid, status="running", started_at=_registry.now_iso())
    _registry.log_activity(project["id"], "run",
                           f"Started TUI session ({run_uid})",
                           {"prompt": prompt[:200]})
    run = _registry.get_run(run_uid)
    return _ok(run=run)


@app.post("/api/runs/{run_uid}/finish")
def api_finish_tui_run(run_uid: str) -> dict[str, Any]:
    """Finalize a TUI session run record."""
    from . import process_tracker, registry as _registry

    run = _registry.get_run(run_uid)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    _registry.update_run(run_uid, status="done", ended_at=_registry.now_iso())
    # Also stop tracking the external process; caller should use /kill to close the window.
    process_tracker.TRACKER.finalize(run_uid)
    return _ok(finished=True)


@app.post("/api/runs/{run_uid}/kill")
def api_kill_tui_run(run_uid: str) -> dict[str, Any]:
    """Terminate the tracked TUI/terminal process for a run and finalize it."""
    from . import process_tracker

    killed = process_tracker.TRACKER.terminate(run_uid)
    return _ok(killed=killed)


@app.get("/api/runs/{run_uid}/stream")
async def api_stream_run(run_uid: str, request: Request) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        yield _sse({"type": "open", "run": run_uid})
        iterator: Iterator[dict[str, Any]] = ORCH.stream(run_uid)
        async for event in iterate_in_threadpool(iterator):
            if await request.is_disconnected():
                break
            yield _sse(event)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


@app.get("/api/stream")
async def api_global_stream(request: Request) -> StreamingResponse:
    """Global multiplexed stream: every run event + the activity feed."""
    async def gen() -> AsyncIterator[str]:
        q = ORCH.bus.subscribe_all()
        last_activity_id = _max_activity_id()
        yield _sse({"type": "open"})
        try:
            while True:
                if await request.is_disconnected():
                    break
                drained = False
                try:
                    while True:
                        ev = q.get_nowait()
                        drained = True
                        yield _sse(ev)
                except queue.Empty:
                    pass
                for ev in _new_activity(last_activity_id):
                    last_activity_id = ev["id"]
                    yield _sse({"type": "activity", **ev})
                if not drained:
                    await asyncio.sleep(1.0)
        finally:
            ORCH.bus.unsubscribe_all(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


# --------------------------------------------------------------------------- #
# Memory
# --------------------------------------------------------------------------- #
@app.post("/api/memory/remember")
def api_remember(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    content = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    project = _resolve_project(payload.get("project"))
    scope = payload.get("scope")
    pid = project["id"] if project else 0
    if scope not in (None, "global", "project"):
        scope = None
    result = memory.remember(content, project_id=pid, scope=scope,
                             topic=payload.get("topic", "general"),
                             source=payload.get("source", "api"))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "rejected"))
    return _ok(**result)


@app.post("/api/memory/search")
def api_memory_search(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    query = (payload.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    project = _resolve_project(payload.get("project"))
    k = int(payload.get("k", 6))
    results = memory.unified_search(
        query, project["id"] if project else 0, k=k,
        include_facts=payload.get("include_facts", True),
        include_docs=payload.get("include_docs", True),
        include_global=payload.get("include_global", True),
        all_projects=payload.get("all_projects", False),
    )
    return _ok(results=results, query=query)


@app.get("/api/memory/stats")
def api_memory_stats(project: str | None = None) -> dict[str, Any]:
    p = registry.get_project(project) if project else None
    return _ok(stats=memory.stats(p["id"] if p else None))


@app.get("/api/memory/recent")
def api_memory_recent(project: str | None = None, limit: int = 50) -> dict[str, Any]:
    p = registry.get_project(project) if project else None
    return _ok(facts=memory.recent_facts(p["id"] if p else None, limit=limit))


@app.post("/api/memory/import-legacy")
def api_memory_import(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    return memory.import_legacy_memory(payload.get("path"))


@app.post("/api/memory/prune")
def api_memory_prune(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    removed = memory.prune(
        project_id=(_resolve_project(payload.get("project")) or {}).get("id"),
        keep=int(payload.get("keep", 200)),
        min_age_days=float(payload.get("min_age_days", 30.0)),
    )
    return _ok(removed=removed)


# --------------------------------------------------------------------------- #
# Activity
# --------------------------------------------------------------------------- #
@app.get("/api/activity")
def api_activity(project: str | None = None, limit: int = 100) -> dict[str, Any]:
    p = registry.get_project(project) if project else None
    return _ok(activity=registry.list_activity(p["id"] if p else None, limit=limit))


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@app.get("/api/settings")
def api_get_settings() -> dict[str, Any]:
    data = SETTINGS.to_dict()
    token, chats = SETTINGS.telegram()
    data["telegram_token_set"] = bool(token)
    # Never echo the token — the dashboard shows a "set" badge instead.
    data["telegram_token"] = ""
    data["telegram_chat_count"] = len(chats)
    return _ok(settings=data)


@app.patch("/api/settings")
def api_update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    global SETTINGS
    allowed = set(config.Settings.__dataclass_fields__)
    for key, value in payload.items():
        if key in allowed:
            setattr(SETTINGS, key, value)
    SETTINGS.save()
    return _ok(settings=SETTINGS.to_dict())


# --------------------------------------------------------------------------- #
# Telegram (lightweight passthrough for the dashboard)
# --------------------------------------------------------------------------- #
def _telegram_api(method: str, **params: Any) -> dict[str, Any]:
    token, _ = SETTINGS.telegram()
    if not token:
        raise HTTPException(status_code=400, detail="Telegram token not configured")
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with httpx.Client(timeout=30.0) as client:
            return client.post(url, json=params).json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


def _telegram_getme(token: str) -> dict[str, Any]:
    """Call ``getMe`` with an *explicit* token (before it is persisted)."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with httpx.Client(timeout=30.0) as client:
            return client.post(url, json={}).json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/telegram/status")
def api_telegram_status() -> dict[str, Any]:
    token, chats = SETTINGS.telegram()
    data = _telegram_api("getMe") if token else {"ok": False}
    bridge_running: bool | None
    try:
        from .telegram import PidLock
        bridge_running = PidLock.is_held(config.HOME / "telegram.pid")
    except Exception:  # pragma: no cover - defensive
        bridge_running = None
    try:
        from . import installer
        task: dict[str, Any] | None = installer.bridge_task_status()
    except Exception:  # pragma: no cover - defensive
        task = None
    return _ok(
        configured=bool(token),
        bot_name=SETTINGS.telegram_bot_name,
        allowed_chats=chats,
        token_source=SETTINGS.telegram_sources()["token"],
        chats_source=SETTINGS.telegram_sources()["chats"],
        bridge_running=bridge_running,
        bridge_task=task,
        bot=data.get("result") if data.get("ok") else None,
    )


@app.post("/api/telegram/bot")
def api_telegram_bot(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Configure the Telegram bot: label, token (api key) and chat id(s).

    A new token is verified with ``getMe`` **before** it is persisted, so a typo
    can never silently replace a working bot. Sending ``token: ""`` disconnects
    the bot; omitting ``token`` keeps the current one.
    """
    global SETTINGS
    changed: list[str] = []

    if "name" in payload:
        SETTINGS.telegram_bot_name = str(payload.get("name") or "").strip()
        changed.append("name")

    if "chats" in payload:
        raw = payload.get("chats") or []
        if isinstance(raw, str):
            raw = [c.strip() for c in raw.split(",")]
        SETTINGS.telegram_allowed_chats = [str(c).strip() for c in raw if str(c).strip()]
        changed.append("chats")

    if "token" in payload:
        token = str(payload.get("token") or "").strip()
        if token:
            me = _telegram_getme(token)
            if not me.get("ok"):
                raise HTTPException(
                    status_code=400,
                    detail=me.get("description") or "Telegram rejected that token",
                )
            SETTINGS.telegram_token = token
            changed.append("token")
        else:
            SETTINGS.telegram_token = ""
            changed.append("token(cleared)")

    SETTINGS.save()

    # Re-read through the resolver so an env-var fallback is reflected too.
    token_now, chats_now = SETTINGS.telegram()
    bot = None
    if token_now:
        try:
            me = _telegram_api("getMe")
            bot = me.get("result") if me.get("ok") else None
        except HTTPException:
            bot = None
    return _ok(
        changed=changed,
        configured=bool(token_now),
        bot_name=SETTINGS.telegram_bot_name,
        allowed_chats=chats_now,
        token_source=SETTINGS.telegram_sources()["token"],
        chats_source=SETTINGS.telegram_sources()["chats"],
        bot=bot,
        restart_required=("token" in payload),
    )


@app.post("/api/telegram/bridge/restart")
def api_telegram_bridge_restart() -> dict[str, Any]:
    """Restart the ``PerunBridge`` task so it re-reads the configured bot."""
    try:
        from . import installer
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=str(exc))
    return _ok(restart=installer.restart_bridge_task())


@app.post("/api/telegram/send")
def api_telegram_send(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    _, chats = SETTINGS.telegram()
    chat_id = str(payload.get("chat_id") or (chats[0] if chats else ""))
    if not chat_id:
        raise HTTPException(status_code=400, detail="no chat id configured")
    from .telegram import format_html
    res = _telegram_api("sendMessage", chat_id=chat_id, text=format_html(text),
                        parse_mode="HTML", disable_web_page_preview=True)
    return _ok(sent=bool(res.get("ok")), result=res.get("result"))


# --------------------------------------------------------------------------- #
# SSE plumbing
# --------------------------------------------------------------------------- #
def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def _max_activity_id() -> int:
    with db.db() as conn:
        row = db.one(conn, "SELECT MAX(id) m FROM activity")
        return int(row["m"] or 0) if row else 0


def _new_activity(after_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with db.db() as conn:
        rows = db.rows(
            conn, "SELECT * FROM activity WHERE id>? ORDER BY id ASC LIMIT ?",
            (after_id, limit),
        )
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except Exception:
            d["meta"] = {}
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# Static dashboard (mounted last so /api/* always wins)
# --------------------------------------------------------------------------- #
if config.WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True),
              name="web")
else:  # pragma: no cover
    @app.get("/")
    def _no_web() -> JSONResponse:
        return JSONResponse({"ok": True, "detail": "web/ dashboard not found",
                             "home": str(config.HOME)})


def serve(host: str | None = None, port: int | None = None, *,
          reload: bool = False) -> None:
    import uvicorn

    host = host or SETTINGS.host
    port = port or SETTINGS.port
    if _hostname(host) not in _LOOPBACK_NAMES:
        print(
            f"[perun] WARNING: binding to {host!r}. The dashboard has no "
            "authentication and can start agent runs — expose it only on a "
            "trusted network.",
            flush=True,
        )
    uvicorn.run("perun.server:app", host=host, port=port, reload=reload,
                log_level="info")
