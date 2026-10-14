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
import queue
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from urllib.parse import urlsplit

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import iterate_in_threadpool

from . import __version__, config, db, discovery, git, manifest, memory, registry, scaffold
from .orchestrator import Orchestrator

STARTED_AT = time.time()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

SETTINGS = config.Settings.load()
ORCH = Orchestrator(SETTINGS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    config.ensure_dirs()
    try:
        discovery.register_self()
    except Exception:  # pragma: no cover - self-registration is best-effort
        pass
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
    }


_MODELS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_MODELS_TTL = 300.0


@app.get("/api/models")
def models(refresh: bool = False) -> dict[str, Any]:
    """Best-effort list of models available to the OpenCode CLI (cached)."""
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
        return {"ok": False, "models": [], "error": str(exc)}
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    data = {"ok": proc.returncode == 0, "models": lines,
            "raw": (proc.stdout or "")[:4000]}
    _MODELS_CACHE.update(ts=now, data=data)
    return data


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
@app.get("/api/projects")
def api_list_projects() -> dict[str, Any]:
    return {"ok": True, "projects": registry.list_projects()}


def _name_from_repo(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail or "project"


def _register_project(payload: dict[str, Any], name: str, path: str
                      ) -> tuple[dict[str, Any], bool]:
    existing = registry.get_project_by_path(path)
    if existing:
        project = registry.update_project(
            existing["slug"],
            name=name,
            kind=payload.get("kind", existing.get("kind", "dir")),
            description=payload.get("description", existing.get("description", "")),
            model=payload.get("model", existing.get("model", "")),
            agent=payload.get("agent", existing.get("agent", "")),
            tags=payload.get("tags", existing.get("tags", "")),
            color=payload.get("color", existing.get("color", "")),
            instructions=payload.get("instructions", existing.get("instructions", "")),
        )
        return project or existing, False
    project = registry.add_project(
        name, path,
        root=payload.get("root", ""),
        kind=payload.get("kind", "dir"),
        description=payload.get("description", ""),
        tags=payload.get("tags", ""),
        model=payload.get("model", ""),
        agent=payload.get("agent", ""),
        color=payload.get("color", ""),
        instructions=payload.get("instructions", ""),
    )
    return project, True


@app.post("/api/projects")
def api_add_project(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Register a project.

    ``method`` selects the wizard path: ``existing`` (default; may point at a
    path that is not created yet), ``new`` (create + optional bootstrap), or
    ``clone`` (clone a remote into the target path). Optional git fields
    (``git_init``, ``git_remote``, ``git_branch``) configure the per-project
    repository, and ``index_docs`` indexes it immediately.
    """
    method = (payload.get("method") or "existing").strip().lower()
    path = (payload.get("path") or "").strip()
    name = (payload.get("name") or "").strip()

    if method == "clone":
        url = (payload.get("clone_url") or payload.get("git_remote") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="clone_url is required")
        if not name:
            name = _name_from_repo(url)
        if not path:
            root = (payload.get("root") or "").strip()
            if not root:
                raise HTTPException(status_code=400,
                                    detail="path is required (or set a root)")
            path = str(Path(root) / (registry.slugify(name) or "project"))
        try:
            git.clone(url, path)
        except git.GitError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    elif method == "new":
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        try:
            created_dir = scaffold.create_dir(path)[1]
            if created_dir and payload.get("bootstrap", True):
                scaffold.bootstrap(path, name or Path(path).name)
        except scaffold.ScaffoldError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    if not name:
        name = Path(path).name or "project"

    project, created = _register_project(payload, name, path)

    branch = (payload.get("git_branch") or "").strip()
    remote = (payload.get("git_remote") or "").strip()
    try:
        if payload.get("git_init") and not git.is_repo(path):
            git.init(path, branch or "main")
        if remote and not git.is_repo(path):
            git.init(path, branch or "main")
        if remote:
            git.set_remote(path, remote, branch)
        if remote or branch or payload.get("git_init"):
            registry.update_project(project["slug"], git_remote=remote,
                                    git_branch=branch)
            project = registry.get_project(project["slug"]) or project
    except git.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if payload.get("index_docs"):
        try:
            memory.index_project_docs(project["id"])
        except Exception:  # pragma: no cover - indexing is best-effort
            pass

    # A bootstrap (or an existing repo) may carry a perun.md manifest; merge it
    # into the registry now so the dashboard reflects it immediately.
    try:
        snap = _sync_manifest(project)
        if snap["found"]:
            project = registry.get_project(project["slug"]) or project
    except Exception:  # pragma: no cover - manifest merge is best-effort
        pass

    registry.log_activity(project["id"], "discover",
                          "Project created via wizard" if method in ("new", "clone")
                          else "Project registered manually")
    git_info = git.info(path)
    return {"ok": True, "project": project, "created": created, "git": git_info}


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
    deleted = registry.delete_project(slug)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"no such project: {slug}")
    return _ok(deleted=slug)


@app.post("/api/projects/{slug}/index")
def api_index_project(slug: str) -> dict[str, Any]:
    project = _project_or_404(slug)
    try:
        _sync_manifest(project)
    except Exception:  # pragma: no cover - best-effort
        pass
    return memory.index_project_docs(project["id"])


@app.get("/api/projects/{slug}/sessions")
def api_project_sessions(slug: str) -> dict[str, Any]:
    project = _project_or_404(slug)
    return _ok(sessions=registry.list_sessions(project["id"]))


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
def _sync_manifest(project: dict[str, Any]) -> dict[str, Any]:
    """Re-read the manifest from disk and merge declared fields into the registry."""
    snap = manifest.load(project["path"])
    if snap["found"]:
        declared = manifest.to_project_fields(snap["data"])
        if declared:
            registry.update_project(project["slug"], **declared)
        registry.set_project_manifest(project["slug"], manifest.snapshot(snap),
                                      snap["path"])
    return snap


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


@app.post("/api/runs")
def api_start_run(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    project_ref = payload.get("project") or payload.get("slug")
    prompt = (payload.get("prompt") or "").strip()
    if not project_ref:
        raise HTTPException(status_code=400, detail="project is required")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
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
    return _ok(cancelled=ORCH.cancel(run_uid))


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
    data["telegram_token"] = "***" if token else ""
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
    return _ok(
        configured=bool(token),
        allowed_chats=chats,
        bridge_running=bridge_running,
        bot=data.get("result") if data.get("ok") else None,
    )


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
