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
from typing import Any, AsyncIterator, Iterator

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import iterate_in_threadpool

from . import config, db, discovery, memory, registry
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
    if SETTINGS.auto_discover_on_start:
        try:
            discovery.discover_and_sync(SETTINGS.roots, max_depth=1)
        except Exception:  # pragma: no cover - discovery is best-effort
            pass
    yield


app = FastAPI(title="Perun", version="0.1.0", docs_url="/api/docs",
              openapi_url="/api/openapi.json", lifespan=lifespan)


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
        "version": "0.1.0",
        "uptime": int(time.time() - STARTED_AT),
        "projects": len(registry.list_projects()),
        "active_runs": ORCH.active(),
        "home": str(config.HOME),
    }


@app.get("/api/models")
def models() -> dict[str, Any]:
    """Best-effort list of models available to the OpenCode CLI."""
    from .orchestrator import resolve_opencode
    import subprocess

    argv = resolve_opencode("opencode") + ["models"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                              encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"ok": False, "models": [], "error": str(exc)}
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return {"ok": proc.returncode == 0, "models": lines,
            "raw": (proc.stdout or "")[:4000]}


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
@app.get("/api/projects")
def api_list_projects() -> dict[str, Any]:
    return {"ok": True, "projects": registry.list_projects()}


@app.post("/api/projects")
def api_add_project(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    path = (payload.get("path") or "").strip()
    name = (payload.get("name") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    if not name:
        from pathlib import Path
        name = Path(path).name or "project"
    existing = registry.get_project_by_path(path)
    if existing:
        project = registry.update_project(
            existing["slug"],
            name=name,
            description=payload.get("description", existing.get("description", "")),
            model=payload.get("model", existing.get("model", "")),
            agent=payload.get("agent", existing.get("agent", "")),
            tags=payload.get("tags", existing.get("tags", "")),
            color=payload.get("color", existing.get("color", "")),
        )
        return {"ok": True, "project": project, "created": False}
    project = registry.add_project(
        name, path,
        root=payload.get("root", ""),
        kind=payload.get("kind", "dir"),
        description=payload.get("description", ""),
        tags=payload.get("tags", ""),
        model=payload.get("model", ""),
        agent=payload.get("agent", ""),
        color=payload.get("color", ""),
    )
    registry.log_activity(project["id"], "discover", "Project registered manually")
    return {"ok": True, "project": project, "created": True}


@app.get("/api/projects/{slug}")
def api_get_project(slug: str) -> dict[str, Any]:
    project = _project_or_404(slug)
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
    return _ok(
        configured=bool(token),
        allowed_chats=chats,
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
    uvicorn.run("perun.server:app", host=host, port=port, reload=reload,
                log_level="info")
