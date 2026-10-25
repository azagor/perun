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
import getpass
import hmac
import json
import logging
import os
import queue
import re
import shlex
import sys
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from urllib.parse import urlsplit

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import iterate_in_threadpool

from . import (__version__, action_proposals, config, context, control, db,
               discovery, git, manifest, memory, projects, providers, registry,
               scaffold, shared_projects, shared_project_sync)
from . import orchestrator
from .perunchat_bridge import attach as _attach_perunchat_bridge
from .perunchat_bridge import get_bridge as _get_perunchat_bridge

STARTED_AT = time.time()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

SETTINGS = config.Settings.load()
ORCH = orchestrator.Orchestrator(SETTINGS)
PERUNCHAT_BRIDGE = _attach_perunchat_bridge(ORCH)


def _approver_identity() -> str:
    """Return the human-readable identity of the local operator.

    Uses the configured ``perun_owner_name`` if set, otherwise the OS login
    name, with a final fallback to ``"owner"``.
    """
    name = getattr(SETTINGS, "perun_owner_name", "").strip()
    if name:
        return name
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - unusual environments
        return "owner"


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


_REEMBED_INITIAL_DELAY_SECONDS = 5.0
_REEMBED_INTERVAL_SECONDS = 60.0


def _start_reembed_thread() -> threading.Event | None:
    """Background repair of facts/doc chunks whose embeddings are missing/stale."""
    # Tests frequently recreate the DB mid-run; a background thread that touches
    # the same store would be a source of races, so stay idle under pytest.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    stop = threading.Event()
    def _loop() -> None:
        delay = _REEMBED_INITIAL_DELAY_SECONDS
        while not stop.wait(delay):
            try:
                memory.reembed_missing(limit=32)
            except Exception:  # pragma: no cover - best effort
                pass
            try:
                context.reembed_missing(limit=32)
            except Exception:  # pragma: no cover - best effort
                pass
            delay = _REEMBED_INTERVAL_SECONDS
    t = threading.Thread(target=_loop, daemon=True, name="perun-reembed")
    t.start()
    return stop


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    config.ensure_dirs()
    _control_project()
    # Seed the default global guidance file once on startup.
    orchestrator._seed_default_instructions()
    # After a restart we lose all in-memory process tracking; any run still
    # marked running/queued is effectively dead. Mark it ``interrupted`` — NOT
    # ``done``: OpenCode may have been killed mid-write, and a finished status
    # would make the history claim the run succeeded.
    for run in registry.list_active_runs():
        registry.update_run(run["run_uid"], status="interrupted",
                            ended_at=registry.now_iso(),
                            error=run.get("error") or "interrupted by a Perun restart")
    # A proposal whose run was just marked interrupted can never finish; leaving
    # it in "executing" would make it permanently un-actionable. Mark it failed
    # now and notify its project room once the bridge is up.
    try:
        recovered_proposals = action_proposals.reconcile_interrupted()
    except Exception:  # pragma: no cover - startup must not depend on this
        logging.getLogger("perun.server").exception(
            "Could not reconcile interrupted action proposals"
        )
        recovered_proposals = []
    if SETTINGS.auto_discover_on_start:
        try:
            discovery.discover_and_sync(SETTINGS.roots, max_depth=1)
        except Exception:  # pragma: no cover - discovery is best-effort
            pass
    # Retry shared-project chat deletions that failed while the gateway was offline.
    try:
        shared_project_sync.retry_pending_room_deletions()
    except Exception:  # pragma: no cover - best-effort retry
        pass
    # Start a background thread that repairs embeddings written while Ollama
    # was down. It is daemonic and signalled on shutdown.
    reembed_stop = _start_reembed_thread()
    # Start the autonomous PerunChat agent bridge when a key is configured.
    if PERUNCHAT_BRIDGE is not None:
        PERUNCHAT_BRIDGE.start()
        if recovered_proposals:
            _post_recovered_digests(recovered_proposals)
    try:
        yield
    finally:
        if reembed_stop is not None:
            reembed_stop.set()
        if PERUNCHAT_BRIDGE is not None:
            PERUNCHAT_BRIDGE.stop()
    # Shutdown: stop what we started so a restart does not orphan interactive
    # terminals or leave agent processes writing into a database nobody watches.
    # Run the blocking cleanup in a thread so the event loop is not stalled.
    def _shutdown_sync() -> None:
        try:
            for uid in ORCH.active():
                ORCH.cancel(uid)
        except Exception:  # pragma: no cover - best effort
            pass
        try:
            from . import process_tracker
            process_tracker.TRACKER.terminate_all()
        except Exception:  # pragma: no cover - best effort
            pass
    try:
        await asyncio.to_thread(_shutdown_sync)
    except Exception:  # pragma: no cover - best effort
        pass


def _post_proposal_digest(project: dict[str, Any], proposal: dict[str, Any]) -> None:
    """Post a compact State Digest for *proposal* to its bound project chat."""
    chat = registry.get_project_chat(project["id"])
    if not chat or not chat.get("room_id") or not PERUNCHAT_BRIDGE:
        return
    try:
        digest = action_proposals.state_digest(proposal, project)
        text = json.dumps(digest, ensure_ascii=False)
        # The digest leaves the process on the project chat. Redact local paths
        # and credential-shaped strings even though project participants are
        # trusted; a run's output may echo something it should not have seen.
        redact = getattr(PERUNCHAT_BRIDGE, "_redact_internal_info", None)
        if callable(redact):
            text = redact(text, chat["room_id"])
        PERUNCHAT_BRIDGE.post_to_room(
            chat["room_id"],
            text,
            content_type="application/perun-state-digest",
        )
    except Exception:  # pragma: no cover - best-effort digest post
        pass


def _post_recovered_digests(recovered: list[dict[str, Any]]) -> None:
    """Wait briefly for the bridge socket, then report interrupted proposals.

    The bridge connects asynchronously, so posting immediately after ``start()``
    would silently drop the digest. This is a daemon best-effort notifier, never
    a startup dependency.
    """
    def _work() -> None:
        for _ in range(60):  # up to 30 s
            if getattr(PERUNCHAT_BRIDGE, "_ws", None):
                break
            time.sleep(0.5)
        for proposal in recovered:
            project = registry.get_project(proposal["project_id"])
            if project:
                _post_proposal_digest(project, proposal)

    threading.Thread(target=_work, name="proposal-recovery-digest", daemon=True).start()


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
_API_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
# Longest accepted instruction filename (the OS would otherwise raise OSError).
MAX_INSTRUCTION_FILENAME = 120
_WILDCARD_BINDS = {"", "*", "0.0.0.0", "::", "[::]"}


def _bearer(header: str) -> str:
    value = (header or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _token_ok(request: Request) -> bool:
    """Validate the local API token for a state-changing request.

    Reads the token from disk on every call so the dashboard (which gets it
    injected into ``index.html``) and CLIs that read the file always agree, even
    when another Perun process created it first.
    """
    expected = config.api_token()
    if not expected:
        return True
    supplied = (request.headers.get("x-perun-token")
                or _bearer(request.headers.get("authorization", "")))
    return bool(supplied) and hmac.compare_digest(supplied, expected)


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


def _split_host_port(value: str) -> tuple[str, int]:
    """Return ``(host, port)`` from a ``Host`` header or an ``Origin`` URL.

    Handles IPv6 brackets and default/omitted ports. Used by the origin guard
    so loopback aliases are allowed only when the port matches exactly.
    """
    v = (value or "").strip()
    if not v:
        return "", 0
    if "//" in v:
        v = urlsplit(v).netloc
    if v.startswith("["):
        host = v[1:].split("]", 1)[0].lower()
        rest = v.split("]", 1)[1]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else 0
        return host, port
    if v.count(":") == 1:
        host, port_str = v.rsplit(":", 1)
        port = int(port_str) if port_str.isdigit() else 0
        return host.lower(), port
    return v.lower(), 0


def _origin_parts(origin: str) -> tuple[str, str, int]:
    """Return ``(scheme, host, effective_port)`` for an ``Origin`` value.

    A URL with no explicit port uses the scheme's default (80 for http, 443 for
    https) — comparing only "both sides have a port" let
    ``Origin: http://localhost`` (port 0) pass for ``Host: localhost:8760``.
    """
    scheme = (urlsplit(origin).scheme or "").lower()
    host, port = _split_host_port(origin)
    if not port:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


def _origin_ok(origin: str, host_header: str, request_scheme: str = "http") -> bool:
    """True when a browser ``Origin`` matches the request host + port + scheme."""
    o_scheme, o_host, o_port = _origin_parts(origin)
    h_host, h_port = _split_host_port(host_header)
    if not o_host:
        return False
    # The Origin's scheme must be the scheme this request arrived over: an
    # ``https`` origin can never be a page served by the plain-HTTP dashboard.
    if o_scheme and o_scheme != (request_scheme or "http").lower():
        return False
    # A Host header without an explicit port is the server's own configured port.
    if not h_port:
        h_port = int(SETTINGS.port or 80)
    if o_port != h_port:
        return False
    if o_host == h_host:
        return True
    # Allow loopback aliases (127.0.0.1 / localhost / ::1) only when ports match.
    if o_host in _LOOPBACK_NAMES and h_host in _LOOPBACK_NAMES:
        return True
    return False


def _perunchat_connect_origin() -> str:
    """Return the configured PerunChat gateway origin for connect-src CSP.

    The dashboard opens a WebSocket directly to the gateway, so the CSP must
    allow that origin. Derives the origin from the configured WS endpoint.
    """
    endpoint = SETTINGS.perunchat()[1] or "wss://peruninthemachine.com/v1/chat/ws"
    try:
        from urllib.parse import urlparse

        parsed = urlparse(endpoint)
        if parsed.scheme and parsed.hostname:
            if parsed.port:
                return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
            return f"{parsed.scheme}://{parsed.hostname}"
    except Exception:
        pass
    return "wss://peruninthemachine.com"


@app.middleware("http")
async def _origin_guard(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        host_header = request.headers.get("host", "")
        if not _host_allowed(host_header):
            return JSONResponse(
                {"detail": "blocked: unexpected Host header (possible DNS rebinding)"},
                status_code=403)
        origin = request.headers.get("origin")
        if origin and not _origin_ok(origin, host_header, request.url.scheme):
            return JSONResponse(
                {"detail": "blocked: cross-origin request"},
                status_code=403)
        # State-changing calls need the local API token. The Origin check above
        # only stops a *browser* on another site; any local process (curl, a
        # script, another agent) sends no Origin at all, so without a token it
        # could drive `POST /api/run-in-terminal` or start agent runs.
        if request.method not in _API_SAFE_METHODS and not _token_ok(request):
            return JSONResponse(
                {"detail": "invalid or missing API token "
                           "(send X-Perun-Token; see <PERUN_HOME>/api-token)"},
                status_code=401)
    response = await call_next(request)
    # Harden the dashboard with baseline security headers.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Allow the configured PerunChat gateway origin for WebSocket connections.
    pc_origin = _perunchat_connect_origin()
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        f"connect-src 'self' {pc_origin}; "
        "img-src 'self' data:; "
        "font-src 'self';"
    )
    return response


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _project_or_404(slug: str) -> dict[str, Any]:
    project = registry.get_project(slug)
    if not project:
        raise HTTPException(status_code=404, detail=f"no such project: {slug}")
    return project


def _resolve_project(ref: str | int | None) -> dict[str, Any] | None:
    """Resolve a project reference, treating blank/absent as "no project".

    A *non-empty* reference that matches nothing is a client error rather than
    "global scope": a typo'd slug used to write a project fact into every
    project's memory (or prune all of them).
    """
    if ref is None:
        return None
    if isinstance(ref, str) and not ref.strip():
        return None
    project = registry.get_project(ref)
    if not project:
        raise HTTPException(status_code=404, detail=f"no such project: {ref}")
    return project


def _str_field(payload: dict[str, Any], key: str, default: str = "") -> str:
    """Return a string body field, rejecting wrong JSON types with **400**.

    ``payload.get(key) or ""`` silently passes an int/bool/list through to code
    that calls ``.strip()``/``Path()`` on it, turning a bad request into a 500.
    """
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400, detail=f"{key} must be a string")
    return value


def _ok(**kw: Any) -> dict[str, Any]:
    return {"ok": True, **kw}


def _normalize_instruction_filename(filename: str) -> str:
    """Validate and normalize an instruction filename.

    Rejects path separators, parent-directory references, NUL bytes, over-long
    names, and empty names; ensures the file has a ``.md`` suffix. All of these
    would otherwise surface as a 500 from the filesystem layer.
    """
    filename = (filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    if ".." in filename or "/" in filename or "\\" in filename or "\x00" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not filename.lower().endswith(".md"):
        filename += ".md"
    if len(filename) > MAX_INSTRUCTION_FILENAME:
        raise HTTPException(
            status_code=400,
            detail=f"filename is too long (max {MAX_INSTRUCTION_FILENAME} characters)")
    return filename


def _project_instruction_path(project: dict[str, Any], filename: str) -> Path:
    """Return the validated path for a project-local instruction file."""
    filename = _normalize_instruction_filename(filename)
    root = Path(project["path"]).resolve()
    path = (root / orchestrator.PROJECT_INSTRUCTIONS_DIR / filename).resolve()
    try:
        if not path.is_relative_to(root):
            raise ValueError("path escapes project root")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="instruction file must be inside the project") from exc
    return path


def _clamp(value: Any, min_val: int, max_val: int, name: str) -> int:
    """Validate and clamp an integer query/body parameter."""
    try:
        v = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be an integer") from exc
    if v < min_val:
        raise HTTPException(status_code=400, detail=f"{name} must be at least {min_val}")
    if v > max_val:
        raise HTTPException(status_code=400, detail=f"{name} must be at most {max_val}")
    return v


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


# --------------------------------------------------------------------------- #
# Self-update
# --------------------------------------------------------------------------- #
@app.get("/api/update/check")
def api_update_check(refresh: bool = False) -> dict[str, Any]:
    from . import updater
    return updater.check(refresh=refresh)


@app.get("/api/update/status")
def api_update_status() -> dict[str, Any]:
    from . import updater
    return updater.status()


@app.post("/api/update/apply")
def api_update_apply(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    from . import updater
    try:
        return updater.apply(confirm=True, force=bool(payload.get("force")),
                             restart=bool(payload.get("restart", True)))
    except updater.UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/update/dismiss")
def api_update_dismiss() -> dict[str, Any]:
    from . import updater
    try:
        return updater.dismiss()
    except updater.UpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@app.get("/api/providers")
def api_list_providers(refresh: bool = False) -> dict[str, Any]:
    """LLM providers known to OpenCode, with auth/config/catalog status.

    The response merges providers saved in OpenCode auth with providers
    configured in ``opencode.json[c]``. The separate ``catalog`` list contains
    well-known providers that are not yet active, so the dashboard can offer an
    "Add provider" flow. Results are cached for 30 seconds unless
    ``refresh=true`` is passed.
    """
    return providers.list_providers(refresh=refresh)


@app.post("/api/providers/{provider_id}/logout")
def api_provider_logout(provider_id: str) -> dict[str, Any]:
    """Disconnect a provider from OpenCode auth.

    Perun does not touch credentials itself; it only invokes
    ``opencode auth logout <provider_id>``.
    """
    result = providers.logout_provider(provider_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "logout failed")
    return _ok(**result)


@app.post("/api/providers/{provider_id}/connect")
def api_provider_connect(provider_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Add or update a provider in the OpenCode config.

    The dashboard collects the fields appropriate for the provider kind
    (API key, base URL, models, etc.) and writes them into the user's
    ``opencode.json[c]``. OpenCode is then asked to reload its configuration.
    """
    result = providers.connect_provider(provider_id, payload)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "connect failed")
    return _ok(**result)


@app.post("/api/providers/{provider_id}/disconnect")
def api_provider_disconnect(provider_id: str) -> dict[str, Any]:
    """Disconnect a provider from both config and OpenCode auth.

    Removing the config entry clears credentials written by the dashboard;
    ``opencode auth logout`` clears credentials saved through the CLI.
    """
    result = providers.disconnect_provider(provider_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "disconnect failed")
    return _ok(**result)


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


_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _ps_quote(value: str) -> str:
    """Single-quoted PowerShell literal — ``$`` interpolation cannot happen."""
    return "'" + str(value).replace("'", "''") + "'"


def _posix_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def _cmd_quote(value: str) -> str:
    """Double-quoted ``cmd.exe`` argument (embedded quotes doubled)."""
    return '"' + str(value).replace('"', '""') + '"'


def _terminal_command(*, shell_cls: str, cwd: str, prompt: str = "", model: str = "",
                      agent: str = "", session: str = "", run_uid: str = "",
                      tui: bool = False) -> str:
    """Build the terminal command line, quoting every value for *shell_cls*.

    Values never reach the shell unquoted (PowerShell single quotes, POSIX
    single quotes, ``cmd`` double quotes), so a prompt is passed as data — not
    parsed as PowerShell/``cmd`` code — whichever terminal is configured.
    """
    if shell_cls in ("powershell", "pwsh"):
        q = _ps_quote
        head = f"$env:PERUN_RUN_UID={q(run_uid)}; " if run_uid else ""
        cmd = f"{head}Set-Location -LiteralPath {q(cwd)}; opencode"
        if tui:
            if prompt:
                cmd += f" --prompt {q(prompt)}"
            if session:
                cmd += f" --session {q(session)}"
            return cmd
        cmd += " run"
        if model:
            cmd += f" --model {q(model)}"
        if agent:
            cmd += f" --agent {q(agent)}"
        if session:
            cmd += f" --session {q(session)}"
        cmd += f" -- {q(prompt or 'Working session in progress.')}"
        return cmd

    if shell_cls in ("cmd", "wt"):
        q = _cmd_quote
        head = f'set "PERUN_RUN_UID={run_uid}" && ' if run_uid else ""
        cmd = f"{head}cd /d {q(cwd)} && opencode"
        if tui:
            if prompt:
                cmd += f" --prompt {q(prompt)}"
            if session:
                cmd += f" --session {q(session)}"
            return cmd
        cmd += " run"
        if model:
            cmd += f" --model {q(model)}"
        if agent:
            cmd += f" --agent {q(agent)}"
        if session:
            cmd += f" --session {q(session)}"
        cmd += f" -- {q(prompt or 'Working session in progress.')}"
        return cmd

    q = _posix_quote
    head = f"PERUN_RUN_UID={q(run_uid)} " if run_uid else ""
    cmd = f"cd {q(cwd)} && {head}opencode"
    if tui:
        if prompt:
            cmd += f" --prompt {q(prompt)}"
        if session:
            cmd += f" --session {q(session)}"
        return cmd
    cmd += " run"
    if model:
        cmd += f" --model {q(model)}"
    if agent:
        cmd += f" --agent {q(agent)}"
    if session:
        cmd += f" --session {q(session)}"
    cmd += f" -- {q(prompt or 'Working session in progress.')}"
    return cmd


@app.post("/api/run-in-terminal")
def api_run_in_terminal(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Open the user's preferred terminal for an ``opencode`` command.

    The command line is built **here** from structured fields, and every
    interpolated value is quoted for the target shell. It used to be assembled
    in the dashboard and POSTed as free text; a prompt containing PowerShell
    ``$(...)`` then executed as soon as the terminal opened.
    """
    project_ref = str(payload.get("project") or "").strip()
    cwd = str(payload.get("cwd") or "").strip()
    if project_ref and not cwd:
        project = registry.get_project(project_ref)
        if project:
            cwd = project["path"]

    action = str(payload.get("action") or "session").strip().lower()
    if action not in ("session", "auth-login"):
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")

    prompt = str(payload.get("prompt") or "")
    model = str(payload.get("model") or "").strip()
    agent = str(payload.get("agent") or "").strip()
    session = str(payload.get("session") or "").strip()
    run_uid = str(payload.get("run_uid") or "").strip()
    tui = bool(payload.get("tui"))
    # These are single-line values; newlines/NUL would break the command line.
    for label, value in (("cwd", cwd), ("model", model), ("agent", agent),
                         ("session", session), ("run_uid", run_uid)):
        if "\r" in value or "\n" in value or "\x00" in value:
            raise HTTPException(status_code=400, detail=f"{label} must be a single line")
    if cwd and not Path(cwd).is_dir():
        raise HTTPException(status_code=400, detail=f"cwd does not exist: {cwd}")
    if action == "session" and not cwd:
        logging.getLogger("perun.server").warning(
            "run-in-terminal rejected: no cwd or resolvable project (project=%r cwd=%r)",
            project_ref, cwd)
        raise HTTPException(status_code=400,
                            detail="cwd (or a project with a path) is required")

    shell = str(payload.get("shell") or SETTINGS.terminal.strip() or "")
    argv = SETTINGS.terminal_cmd(shell_hint=shell)
    # Derive the shell class from the resolved executable so an empty/default
    # shell still gets the right command-line syntax. Normalize to the basename
    # so a configured full path (e.g. C:\...\wt.exe) still matches cmd/pwsh/wt.
    exe = os.path.basename(argv[0]).lower().replace(".exe", "")
    shell_cls = os.path.basename(shell.lower().replace(".exe", "")) or exe
    # cmd.exe cannot carry a newline in its command line; flatten them.
    if shell_cls in ("cmd", "wt"):
        prompt = " ".join(prompt.splitlines())

    if action == "auth-login":
        provider = str(payload.get("provider") or "").strip()
        if not _PROVIDER_ID_RE.match(provider):
            raise HTTPException(status_code=400, detail="provider id is invalid")
        if shell_cls in ("cmd", "wt"):
            quote = _cmd_quote
        elif shell_cls in ("powershell", "pwsh"):
            quote = _ps_quote
        else:
            quote = _posix_quote
        cmd_text = f"opencode auth login {quote(provider)}"
    else:
        cmd_text = _terminal_command(
            shell_cls=shell_cls, cwd=cwd, prompt=prompt, model=model, agent=agent,
            session=session, run_uid=run_uid, tui=tui)

    use_cmdline = False
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
        # Build the trailing "cmd /k <text>" as raw text, never through
        # list2cmdline(... + cmd_text): cmd.exe does not understand C-runtime
        # \" quoting, so list2cmdline would emit \" and cmd would fail with
        # "The filename, directory name, or volume label syntax is incorrect."
        cmdline = subprocess.list2cmdline(wt_argv) + " cmd /k " + cmd_text
        use_cmdline = True
    elif shell_cls in ("cmd",):
        # /k keeps the console window alive after opencode exits. Same rule as
        # above: cmd_text is authored (and quoted) for cmd.exe itself, so hand it
        # over verbatim. Passing [argv[0], "/k", cmd_text] through list2cmdline
        # escapes the inner quotes as \" which cmd.exe cannot parse.
        cmdline = subprocess.list2cmdline([argv[0]]) + " /k " + cmd_text
        use_cmdline = True
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
        # Generic fallback: hand the command to the shell. On POSIX the argv
        # already ends with the shell (a terminal emulator was detected and
        # prepended), so also keep the window open afterwards — a terminal that
        # vanishes the instant opencode exits shows the user nothing.
        text = cmd_text
        if os.name != "nt" and argv:
            shell_exe = argv[-1] if len(argv) > 1 else argv[0]
            text = f"{cmd_text}; exec {shlex.quote(shell_exe)} -i"
        argv = argv + ["-c", text]

    try:
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        else:
            creationflags = 0
        if use_cmdline:
            proc = subprocess.Popen(
                cmdline,
                cwd=cwd or None,
                creationflags=creationflags,
            )
        else:
            proc = subprocess.Popen(
                argv,
                cwd=cwd or None,
                creationflags=creationflags,
            )
        if run_uid:
            from . import process_tracker
            process_tracker.TRACKER.register(run_uid, proc, shell=shell)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not open terminal: {exc}")
    return _ok(launched=True, shell=shell, command=cmd_text,
               pid=proc.pid if 'proc' in locals() else None)


# --------------------------------------------------------------------------- #
# MCP servers
# --------------------------------------------------------------------------- #
@app.get("/api/mcp/servers")
def api_mcp_servers() -> dict[str, Any]:
    """Every MCP server OpenCode is configured to use (global + per project).

    Config-only: no server is started. Each entry carries a cheap derived status
    (``ready``/``missing``/``disabled``/``remote``/``invalid``); use
    ``POST /api/mcp/servers/probe`` for a live handshake against one server.
    """
    from . import mcp_servers

    return _ok(**mcp_servers.snapshot(registry.list_projects()))


@app.post("/api/mcp/servers/probe")
def api_mcp_probe(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Live-probe one configured MCP server (explicit, on-demand action).

    The command/URL is read from the on-disk config for the named server, so the
    request cannot make Perun run an arbitrary command. Local servers are spawned
    and driven through the MCP ``initialize`` + ``tools/list`` handshake; remote
    servers get a best-effort HTTP handshake. Always returns a status object, even
    when the server is unreachable. Probes are gated (at most a handful run at
    once), so a burst of Test clicks yields ``status: "busy"`` instead of pinning
    worker threads, and the timeout is capped so no probe can hold one for long.
    """
    from . import mcp_servers

    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    scope = "project" if str(payload.get("scope") or "").lower() == "project" else "global"
    project = str(payload.get("project") or "").strip()
    if scope == "project" and not project:
        raise HTTPException(status_code=400, detail="project is required for project scope")
    try:
        timeout = float(payload.get("timeout") or mcp_servers.DEFAULT_PROBE_TIMEOUT)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="timeout must be a number") from None
    try:
        result = mcp_servers.probe(name, scope=scope, projects=registry.list_projects(),
                                   project=project, timeout=timeout)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _ok(**result)


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
            method=_str_field(payload, "method") or "existing",
            name=_str_field(payload, "name").strip(),
            path=_str_field(payload, "path").strip(),
            clone_url=_str_field(payload, "clone_url").strip(),
            root=_str_field(payload, "root").strip(),
            bootstrap=payload.get("bootstrap", True),
            description=_str_field(payload, "description"),
            kind=_str_field(payload, "kind"),
            tags=_str_field(payload, "tags"),
            model=_str_field(payload, "model"),
            agent=_str_field(payload, "agent"),
            color=_str_field(payload, "color"),
            instructions=_str_field(payload, "instructions"),
            git_init=bool(payload.get("git_init")),
            git_remote=_str_field(payload, "git_remote").strip(),
            git_branch=_str_field(payload, "git_branch").strip(),
            index_docs=bool(payload.get("index_docs")),
        )
    except projects.ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Persist shared-project manifest fields when supplied by the wizard.
    shared_payload = {
        "shared": payload.get("shared"),
        "participants": payload.get("participants", []),
        "external_deps": payload.get("external_deps", []),
    }
    if shared_payload["shared"] or shared_payload["participants"] or shared_payload["external_deps"]:
        project = result.get("project") or {}
        if project.get("path"):
            cfg, errors = shared_projects.apply_payload(
                project["path"], shared_payload, trusted=_trusted_agents()
            )
            result["shared"] = cfg
            if errors:
                result["shared_errors"] = errors
            else:
                try:
                    projects.sync_manifest(project, sync_chat=False)
                    refreshed = registry.get_project(project["slug"])
                    if refreshed:
                        result["project"] = refreshed
                except Exception:  # pragma: no cover - best-effort refresh
                    pass
                try:
                    # Keep the project's opencode.json in sync so the perun-guard
                    # MCP server is registered (or removed) for shared projects.
                    projects.sync_opencode_config(result.get("project") or project)
                except Exception:  # pragma: no cover - best-effort config sync
                    pass
                try:
                    chat_result = shared_project_sync.sync_project_chat(
                        result.get("project") or project, cfg
                    )
                    result["chat_sync"] = chat_result
                except Exception:  # pragma: no cover - best-effort sync
                    pass
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
    project = _project_or_404(slug)
    model_agent_changed = "model" in payload or "agent" in payload
    project = registry.update_project(slug, **payload)
    opencode_config: dict[str, Any] = {"action": "skipped"}
    if model_agent_changed and project:
        # The TUI reads model/default_agent from the project's opencode.json,
        # so mirror the override there and report the result to the dashboard.
        opencode_config = projects.sync_opencode_config(project)
    return _ok(project=project, opencode_config=opencode_config)


@app.get("/api/projects/{slug}/shared")
def api_get_shared_project(slug: str) -> dict[str, Any]:
    """Return the shared-project configuration, binding and live chat state."""
    project = _project_or_404(slug)
    cfg = shared_projects.load(project["path"])
    chat = registry.get_project_chat(project["id"])
    project_chat = shared_project_sync.describe_project_chat(project, cfg)
    return _ok(shared=cfg, chat=chat, project_chat=project_chat)


@app.post("/api/projects/{slug}/shared/sync")
def api_sync_shared_project_chat(slug: str) -> dict[str, Any]:
    """Force-reconcile the project's PerunChat room with its participant list.

    Idempotent: it creates the managed room when missing, adds declared
    participants, and removes anyone who is not on the manifest. Used by the
    "Sync now" control and safe to call repeatedly.
    """
    project = _project_or_404(slug)
    cfg = shared_projects.load(project["path"])
    result = shared_project_sync.sync_project_chat(project, cfg)
    project = registry.get_project(slug) or project
    chat = registry.get_project_chat(project["id"])
    project_chat = shared_project_sync.describe_project_chat(project, cfg)
    return _ok(result=result, chat=chat, project_chat=project_chat)


@app.put("/api/projects/{slug}/shared")
def api_update_shared_project(slug: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Update participants/external_deps for a shared project.

    All declared participants must be on the PerunChat trust list. On success
    the manifest revision is bumped and the registry snapshot is refreshed.
    """
    project = _project_or_404(slug)
    cfg, errors = shared_projects.apply_payload(
        project["path"], payload, trusted=_trusted_agents()
    )
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    try:
        projects.sync_manifest(project, sync_chat=False)
        project = registry.get_project(slug) or project
    except Exception:  # pragma: no cover - best-effort refresh
        pass
    try:
        # Mirror the shared flag into opencode.json so perun-guard is added/removed.
        projects.sync_opencode_config(project)
    except Exception:  # pragma: no cover - best-effort config sync
        pass
    try:
        chat_sync = shared_project_sync.sync_project_chat(project, cfg)
    except Exception as exc:  # pragma: no cover - best-effort sync
        chat_sync = {"ok": False, "error": str(exc)}
    chat = registry.get_project_chat(project["id"])
    project_chat = shared_project_sync.describe_project_chat(project, cfg)
    return _ok(shared=cfg, project=project, chat=chat,
               chat_sync=chat_sync, project_chat=project_chat)


@app.get("/api/projects/{slug}/proposals")
def api_list_proposals(slug: str, status: str | None = None) -> dict[str, Any]:
    """List action proposals for a shared project."""
    project = _project_or_404(slug)
    proposals = action_proposals.list_by_project(project["id"], status=status)
    return _ok(proposals=proposals)


@app.post("/api/projects/{slug}/proposals")
def api_create_proposal(slug: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Create an action proposal from an Action Envelope (used by the bridge)."""
    project = _project_or_404(slug)
    envelope = payload.get("envelope") or payload
    requester = str(payload.get("requester") or "unknown").strip()
    try:
        chat = registry.get_project_chat(project["id"]) or {}
        proposal = action_proposals.create(
            project, requester, envelope,
            owner=chat.get("managed_by"),
            trusted=_trusted_agents(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ok(proposal=proposal)


@app.post("/api/projects/{slug}/proposals/{proposal_id}/approve")
def api_approve_proposal(slug: str, proposal_id: str) -> dict[str, Any]:
    project = _project_or_404(slug)
    proposal = action_proposals.get(proposal_id)
    if not proposal or proposal["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="proposal not found")
    try:
        result = action_proposals.approve(proposal_id, _approver_identity())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ok(proposal=result)


@app.post("/api/projects/{slug}/proposals/{proposal_id}/reject")
def api_reject_proposal(
    slug: str, proposal_id: str, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    project = _project_or_404(slug)
    proposal = action_proposals.get(proposal_id)
    if not proposal or proposal["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="proposal not found")
    try:
        result = action_proposals.reject(
            proposal_id, _approver_identity(), reason=payload.get("reason", "")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ok(proposal=result)


@app.post(
    "/api/projects/{slug}/proposals/{proposal_id}/approve-and-execute",
    status_code=202,
)
def api_approve_and_execute_proposal(slug: str, proposal_id: str) -> dict[str, Any]:
    """Approve a proposal and start its headless run in one owner action."""
    project = _project_or_404(slug)
    proposal = action_proposals.get(proposal_id)
    if not proposal or proposal["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="proposal not found")
    try:
        action_proposals.approve(proposal_id, _approver_identity())
        result = action_proposals.start_execution(
            proposal_id,
            orchestrator_instance=ORCH,
            on_complete=lambda final: _post_proposal_digest(project, final),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ok(proposal=result, accepted=True)


@app.post(
    "/api/projects/{slug}/proposals/{proposal_id}/execute",
    status_code=202,
)
def api_execute_proposal(slug: str, proposal_id: str) -> dict[str, Any]:
    """Start an already-approved proposal without blocking the HTTP request."""
    project = _project_or_404(slug)
    proposal = action_proposals.get(proposal_id)
    if not proposal or proposal["project_id"] != project["id"]:
        raise HTTPException(status_code=404, detail="proposal not found")
    try:
        result = action_proposals.start_execution(
            proposal_id,
            orchestrator_instance=ORCH,
            on_complete=lambda final: _post_proposal_digest(project, final),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _ok(proposal=result, accepted=True)


@app.delete("/api/projects/{slug}")
def api_delete_project(slug: str) -> dict[str, Any]:
    project = _project_or_404(slug)
    if control.is_control(project):
        raise HTTPException(
            status_code=400,
            detail="the Perun orchestrator project cannot be removed")
    try:
        shared_project_sync.delete_project_chat(project)
    except Exception:  # pragma: no cover - lifecycle best-effort
        pass
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
    return _ok(activity=registry.list_activity(project["id"], limit=_clamp(limit, 1, 500, "limit")))


@app.get("/api/projects/{slug}/memory")
def api_project_memory(slug: str, limit: int = 100) -> dict[str, Any]:
    project = _project_or_404(slug)
    return _ok(facts=memory.recent_facts(project["id"], limit=_clamp(limit, 1, 500, "limit")),
               stats=memory.stats(project["id"]))


@app.get("/api/projects/{slug}/instructions")
def api_get_instructions(slug: str) -> dict[str, Any]:
    project = _project_or_404(slug)
    from . import orchestrator

    blocks = orchestrator._project_instruction_blocks(project)
    return _ok(instructions="\n\n".join(blocks))


@app.put("/api/projects/{slug}/instructions")
def api_set_instructions(slug: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Replace a project's custom instructions (project-scoped).

    Writes the provided ``instructions`` text to ``.perun/instructions/default.md``
    (or ``filename`` when supplied) with YAML front matter.
    """
    from . import orchestrator

    project = _project_or_404(slug)
    text = payload.get("instructions", "")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="instructions must be a string")
    filename = str(payload.get("filename") or "").strip() or "default.md"
    path = _project_instruction_path(project, filename)
    name = str(payload.get("name") or "").strip() or path.stem
    enabled = orchestrator._coerce_enabled(payload.get("enabled", True))
    body = text.strip()
    file_text = orchestrator._build_frontmatter({
        "name": name,
        "enabled": enabled,
    }) + body + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(file_text, encoding="utf-8")
    registry.log_activity(project["id"], "config", f"Instruction file {path.name} saved")
    blocks = orchestrator._project_instruction_blocks(project)
    return _ok(instructions="\n\n".join(blocks), filename=path.name)


@app.get("/api/projects/{slug}/instruction-files")
def api_list_project_instruction_files(slug: str) -> dict[str, Any]:
    """List project-local instruction Markdown files with parsed metadata."""
    project = _project_or_404(slug)
    files: list[dict[str, Any]] = []
    for path in orchestrator._project_instruction_files(project):
        name, enabled, body = orchestrator._load_project_instruction_file(path)
        files.append({
            "filename": path.name,
            "name": name,
            "enabled": enabled,
            "size": len(body),
        })
    return _ok(files=files, count=len(files))


@app.get("/api/projects/{slug}/instruction-files/{filename}")
def api_get_project_instruction_file(slug: str, filename: str) -> dict[str, Any]:
    """Read a single project-local instruction file."""
    project = _project_or_404(slug)
    path = _project_instruction_path(project, filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    name, enabled, body = orchestrator._load_project_instruction_file(path)
    return _ok(filename=path.name, name=name, enabled=enabled, body=body)


@app.post("/api/projects/{slug}/instruction-files/{filename}")
def api_save_project_instruction_file(slug: str, filename: str,
                                      payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Create or update a project-local instruction Markdown file."""
    project = _project_or_404(slug)
    path = _project_instruction_path(project, filename)
    name = str(payload.get("name") or "").strip() or path.stem
    enabled = orchestrator._coerce_enabled(payload.get("enabled", True))
    body = str(payload.get("body") or "").strip()
    text = orchestrator._build_frontmatter({
        "name": name,
        "enabled": enabled,
    }) + body + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    registry.log_activity(project["id"], "config", f"Instruction file {path.name} saved")
    return _ok(filename=path.name, name=name, enabled=enabled)


@app.patch("/api/projects/{slug}/instruction-files/{filename}/toggle")
def api_toggle_project_instruction_file(slug: str, filename: str,
                                          payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Toggle a project-local instruction file on or off."""
    project = _project_or_404(slug)
    path = _project_instruction_path(project, filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    name, _, body = orchestrator._load_project_instruction_file(path)
    enabled = orchestrator._coerce_enabled(payload.get("enabled", True))
    text = orchestrator._build_frontmatter({
        "name": name,
        "enabled": enabled,
    }) + body + "\n"
    path.write_text(text, encoding="utf-8")
    return _ok(filename=path.name, enabled=enabled)


@app.delete("/api/projects/{slug}/instruction-files/{filename}")
def api_delete_project_instruction_file(slug: str, filename: str) -> dict[str, Any]:
    """Delete a project-local instruction Markdown file."""
    project = _project_or_404(slug)
    path = _project_instruction_path(project, filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    path.unlink()
    return _ok(deleted=path.name)


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

    snap = projects.sync_manifest(project)
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
    path = _str_field(payload, "path").strip()
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
    max_depth = _clamp(payload.get("max_depth", 1), 0, 5, "max_depth")
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
    return _ok(found=discovery.scan(root_list, max_depth=_clamp(max_depth, 0, 5, "max_depth")))


# --------------------------------------------------------------------------- #
# Runs (agent orchestration)
# --------------------------------------------------------------------------- #
@app.get("/api/runs")
def api_list_runs(project: str | None = None, limit: int = 50) -> dict[str, Any]:
    pid = None
    if project:
        p = registry.get_project(project)
        pid = p["id"] if p else None
    return _ok(runs=registry.list_runs(pid, limit=_clamp(limit, 1, 500, "limit")))


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
    prompt = (payload.get("prompt") or "").strip() or "Started a working session for this project."
    # Prefer an explicit model/agent passed by the frontend; fall back to the
    # project's configured defaults so the run record matches what opencode
    # actually receives.
    model = (payload.get("model") or "").strip() or project.get("model", "")
    agent = (payload.get("agent") or "").strip() or project.get("agent", "")
    _registry.create_run(
        run_uid, project["id"], prompt,
        model=model, model_explicit=bool(model),
        agent=agent,
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
    if run.get("status") in ("queued", "running"):
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
    # A run row is created before the uid is ever handed to a client, so an
    # unknown uid is a bad request — refuse it instead of opening an SSE
    # stream that never ends (stream() would just emit pings forever).
    if registry.get_run(run_uid) is None:
        raise HTTPException(status_code=404, detail=f"no such run: {run_uid}")

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
    content = _str_field(payload, "content").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    project = _resolve_project(payload.get("project"))
    scope = payload.get("scope")
    pid = project["id"] if project else 0
    if scope not in (None, "global", "project"):
        scope = None
    result = memory.remember(content, project_id=pid, scope=scope,
                             topic=_str_field(payload, "topic") or "general",
                             source=_str_field(payload, "source") or "api")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason", "rejected"))
    return _ok(**result)


@app.post("/api/memory/search")
def api_memory_search(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    query = (payload.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    project = _resolve_project(payload.get("project"))
    k = _clamp(payload.get("k", 6), 1, 20, "k")
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
    return _ok(facts=memory.recent_facts(p["id"] if p else None, limit=_clamp(limit, 1, 500, "limit")))


@app.post("/api/memory/import-legacy")
def api_memory_import(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    return memory.import_legacy_memory(payload.get("path"))


@app.post("/api/memory/prune")
def api_memory_prune(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        keep = int(payload.get("keep", 200))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="keep must be an integer")
    # ``keep=0`` would delete every fact; make the caller be explicit about a
    # positive number of facts to preserve.
    keep = _clamp(keep, 1, 1_000_000, "keep")
    try:
        min_age_days = float(payload.get("min_age_days", 30.0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="min_age_days must be a number")
    removed = memory.prune(
        project_id=(_resolve_project(payload.get("project")) or {}).get("id"),
        keep=keep,
        min_age_days=min_age_days,
    )
    return _ok(removed=removed)


@app.post("/api/memory/reembed")
def api_memory_reembed(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """On-demand repair of facts/doc chunks whose embeddings are missing or stale."""
    try:
        limit = int(payload.get("limit", 64))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit must be an integer")
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be positive")
    result = memory.reembed_missing(limit=limit)
    try:
        result["context"] = context.reembed_missing(limit=limit)
    except Exception:  # pragma: no cover - best effort
        result["context"] = {"ok": False}
    return _ok(**result)


# --------------------------------------------------------------------------- #
# Activity
# --------------------------------------------------------------------------- #
@app.get("/api/activity")
def api_activity(project: str | None = None, limit: int = 100) -> dict[str, Any]:
    p = registry.get_project(project) if project else None
    return _ok(activity=registry.list_activity(p["id"] if p else None, limit=_clamp(limit, 1, 500, "limit")))


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
    # The pairing code is a short-lived secret; it is only revealed to loopback
    # clients via /api/telegram/status.
    data["telegram_pair_code"] = ""
    data["telegram_chat_count"] = len(chats)
    pc_key, pc_endpoint = SETTINGS.perunchat()
    data["perunchat_api_key"] = ""  # never echo
    data["perunchat_api_key_set"] = bool(pc_key)
    data["perunchat_api_endpoint"] = pc_endpoint
    # HITL signing secret is sensitive; never echo it.
    data["perun_hitl_secret"] = ""
    data["perun_hitl_secret_set"] = bool(SETTINGS.perun_hitl_secret.strip() or os.environ.get("PERUN_HITL_SECRET", "").strip())
    # Expose bridge controls so the dashboard can pre-fill them.
    data.setdefault("perunchat_auto_reply", False)
    data.setdefault("perunchat_auto_join", [])
    data.setdefault("perunchat_trusted", [])
    data.setdefault("perunchat_chat_model", "")
    data.setdefault("perunchat_chat_agent", "")
    return _ok(settings=data)


@app.get("/api/instructions")
def api_list_instructions() -> dict[str, Any]:
    """List global instruction Markdown files with parsed metadata."""
    from . import orchestrator

    files: list[dict[str, Any]] = []
    for path in orchestrator._instruction_files():
        name, scope, enabled, body = orchestrator._load_instruction_file(path)
        files.append({
            "filename": path.name,
            "name": name,
            "scope": scope,
            "enabled": enabled,
            "size": len(body),
        })
    return _ok(instructions=files)


@app.get("/api/instructions/dir")
def api_instructions_dir() -> dict[str, Any]:
    """Return the absolute path to the global instructions directory.

    Read-only: the directory is created on demand by the writers, not here.
    """
    return _ok(dir=str(config.INSTRUCTIONS_DIR))


@app.post("/api/instructions")
def api_save_instruction(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Create or update an instruction Markdown file."""
    from . import orchestrator

    filename = str(payload.get("filename") or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    if not filename.lower().endswith(".md"):
        filename += ".md"
    name = str(payload.get("name") or "").strip() or filename[:-3]
    scope = str(payload.get("scope") or "all_projects").strip()
    if scope not in ("orchestrator", "all_projects"):
        raise HTTPException(status_code=400, detail="scope must be orchestrator or all_projects")
    enabled = orchestrator._coerce_enabled(payload.get("enabled", True))
    body = str(payload.get("body") or "").strip()
    text = orchestrator._build_frontmatter({
        "name": name,
        "scope": scope,
        "enabled": enabled,
    }) + body + "\n"
    path = config.INSTRUCTIONS_DIR / filename
    config.ensure_dirs()
    path.write_text(text, encoding="utf-8")
    return _ok(filename=filename, name=name, scope=scope, enabled=enabled)


@app.delete("/api/instructions/{filename}")
def api_delete_instruction(filename: str) -> dict[str, Any]:
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    path = config.INSTRUCTIONS_DIR / filename
    if path.exists() and path.suffix.lower() == ".md":
        path.unlink()
    return _ok(deleted=filename)


@app.get("/api/instructions/{filename}")
def api_get_instruction(filename: str) -> dict[str, Any]:
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    path = config.INSTRUCTIONS_DIR / filename
    if not path.exists() or path.suffix.lower() != ".md":
        raise HTTPException(status_code=404, detail="not found")
    from . import orchestrator

    name, scope, enabled, body = orchestrator._load_instruction_file(path)
    return _ok(filename=filename, name=name, scope=scope, enabled=enabled, body=body)


@app.patch("/api/instructions/{filename}/toggle")
def api_toggle_instruction(filename: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    path = config.INSTRUCTIONS_DIR / filename
    if not path.exists() or path.suffix.lower() != ".md":
        raise HTTPException(status_code=404, detail="not found")
    from . import orchestrator

    name, scope, _, body = orchestrator._load_instruction_file(path)
    enabled = orchestrator._coerce_enabled(payload.get("enabled", True))
    text = orchestrator._build_frontmatter({
        "name": name,
        "scope": scope,
        "enabled": enabled,
    }) + body + "\n"
    path.write_text(text, encoding="utf-8")
    return _ok(filename=filename, enabled=enabled)


# --------------------------------------------------------------------------- #
# Integrated code search (ripgrep / ast-grep)
# --------------------------------------------------------------------------- #
@app.post("/api/search/grep")
def api_search_grep(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Fast regex content search in a project directory."""
    from . import grep

    project_ref = payload.get("project")
    pattern = str(payload.get("pattern") or "").strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern is required")
    project = _resolve_project(project_ref)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    include = payload.get("include")
    if include is not None and not isinstance(include, str):
        raise HTTPException(status_code=400, detail="include must be a string")
    result = grep.rg_search(
        pattern, project["path"],
        case_sensitive=bool(payload.get("case_sensitive", True)),
        whole_word=bool(payload.get("whole_word", False)),
        include=(include or "").strip() or None,
        max_results=_clamp(payload.get("max_results", 50), 1, 200, "max_results"),
    )
    if not result.get("ok"):
        detail = result.get("error", "search failed")
        status = 400 if result.get("error_kind") == "invalid_input" else 500
        raise HTTPException(status_code=status, detail=detail)
    return _ok(**result)


@app.post("/api/search/ast-grep")
def api_search_ast_grep(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Structural code search in a project directory."""
    from . import grep

    project_ref = payload.get("project")
    rule = str(payload.get("rule") or "").strip()
    if not rule:
        raise HTTPException(status_code=400, detail="rule is required")
    project = _resolve_project(project_ref)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    result = grep.ast_grep_search(
        rule, project["path"],
        max_results=_clamp(payload.get("max_results", 50), 1, 200, "max_results"),
    )
    if not result.get("ok"):
        detail = result.get("error", "search failed")
        status = 400 if result.get("error_kind") == "invalid_input" else 500
        raise HTTPException(status_code=status, detail=detail)
    return _ok(**result)


@app.post("/api/search/global-grep")
def api_search_global_grep(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Fast regex content search across all registered projects (or a subset)."""
    from . import grep, registry

    pattern = str(payload.get("pattern") or "").strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern is required")
    all_projects = registry.list_projects()
    project_refs = payload.get("projects")
    if project_refs is not None:
        if not isinstance(project_refs, list):
            raise HTTPException(status_code=400, detail="projects must be a list")
        if not all(isinstance(p, str) for p in project_refs):
            raise HTTPException(status_code=400, detail="projects must be a list of strings")
        allowed = {p.strip().lower() for p in project_refs if p.strip()}
        all_projects = [p for p in all_projects if p["slug"].lower() in allowed]
    if not all_projects:
        raise HTTPException(status_code=404, detail="no projects to search")
    include = payload.get("include")
    if include is not None and not isinstance(include, str):
        raise HTTPException(status_code=400, detail="include must be a string")
    result = grep.rg_search_global(
        pattern, all_projects,
        case_sensitive=bool(payload.get("case_sensitive", True)),
        whole_word=bool(payload.get("whole_word", False)),
        include=(include or "").strip() or None,
        max_results=_clamp(payload.get("max_results", 20), 1, 200, "max_results"),
    )
    if not result.get("ok"):
        detail = result.get("error", "search failed")
        status = 400 if result.get("error_kind") == "invalid_input" else 500
        raise HTTPException(status_code=status, detail=detail)
    return _ok(**result)


@app.post("/api/search/global/ast-grep")
def api_search_global_ast_grep(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Structural code search across all registered projects (or a subset)."""
    from . import grep, registry

    rule = str(payload.get("rule") or "").strip()
    if not rule:
        raise HTTPException(status_code=400, detail="rule is required")
    all_projects = registry.list_projects()
    project_refs = payload.get("projects")
    if project_refs is not None:
        if not isinstance(project_refs, list):
            raise HTTPException(status_code=400, detail="projects must be a list")
        if not all(isinstance(p, str) for p in project_refs):
            raise HTTPException(status_code=400, detail="projects must be a list of strings")
        allowed = {p.strip().lower() for p in project_refs if p.strip()}
        all_projects = [p for p in all_projects if p["slug"].lower() in allowed]
    if not all_projects:
        raise HTTPException(status_code=404, detail="no projects to search")
    result = grep.ast_grep_search_global(
        rule, all_projects,
        max_results=_clamp(payload.get("max_results", 20), 1, 200, "max_results"),
    )
    if not result.get("ok"):
        detail = result.get("error", "search failed")
        status = 400 if result.get("error_kind") == "invalid_input" else 500
        raise HTTPException(status_code=status, detail=detail)
    return _ok(**result)


@app.post("/api/search/unified")
def api_search_unified(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Unified cross-project search: memory + grep + ast-grep."""
    from . import search as search_mod

    project_refs = payload.get("projects")
    if project_refs is not None:
        if not isinstance(project_refs, list):
            raise HTTPException(status_code=400, detail="projects must be a list")
        if not all(isinstance(p, str) for p in project_refs):
            raise HTTPException(status_code=400, detail="projects must be a list of strings")
    result = search_mod.unified_search(
        query=str(payload.get("query") or ""),
        pattern=str(payload.get("pattern") or ""),
        rule=str(payload.get("rule") or ""),
        projects=project_refs,
        use_memory=bool(payload.get("memory", True)),
        use_grep=bool(payload.get("grep", True)),
        use_ast_grep=bool(payload.get("ast_grep", True)),
        max_code=_clamp(payload.get("max_results", 20), 1, 200, "max_results"),
        max_memory=_clamp(payload.get("max_memory", 6), 1, 50, "max_memory"),
    )
    if not result.get("ok"):
        status = 400 if result.get("error_kind") == "invalid_input" else 500
        raise HTTPException(status_code=status, detail=result.get("error"))
    return _ok(**result)


def _coerce_setting(key: str, value: Any) -> Any:
    """Validate and coerce a setting value to the expected type."""
    int_fields = {"port", "max_concurrent_runs", "run_timeout", "embed_dim"}
    bool_fields = {
        "auto_discover_on_start", "index_docs_on_discover", "perunchat_auto_reply",
    }
    list_fields = {
        "roots", "telegram_allowed_chats", "perunchat_auto_join",
        "perunchat_trusted",
    }
    str_fields = {
        "host", "default_model", "default_agent", "orchestrator_model",
        "orchestrator_agent", "ollama_host", "embed_model", "telegram_token",
        "telegram_bot_name", "terminal", "perunchat_api_key", "perunchat_api_endpoint",
        "perunchat_chat_model", "perunchat_chat_agent", "perun_owner_name",
        "perun_hitl_secret",
    }
    if key in int_fields:
        try:
            v = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{key} must be an integer") from exc
        if key == "port" and not (1 <= v <= 65535):
            raise HTTPException(status_code=400, detail=f"{key} must be between 1 and 65535")
        if key in ("max_concurrent_runs", "run_timeout") and v < 1:
            raise HTTPException(status_code=400, detail=f"{key} must be at least 1")
        if key == "embed_dim" and v < 1:
            raise HTTPException(status_code=400, detail=f"{key} must be positive")
        return v
    if key in bool_fields:
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if key in list_fields:
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        if isinstance(value, (list, tuple)):
            return [str(p).strip() for p in value if str(p).strip()]
        raise HTTPException(status_code=400, detail=f"{key} must be a list")
    if key in str_fields:
        return str(value).strip() if value is not None else ""
    # Unknown field — fall back to the raw value; the caller filters by allowed keys.
    return value


@app.patch("/api/settings")
def api_update_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    global SETTINGS, ORCH
    from . import secrets as _secrets

    # Encrypt perunchat_api_key when a password is available, exactly like the
    # Telegram token path, so the key is not stored plaintext in config.json.
    pc_password = (payload.get("perunchat_password") or os.environ.get("PERUN_PASSWORD") or "").strip()
    if "perunchat_api_key" in payload:
        pc_key = str(payload.get("perunchat_api_key") or "").strip()
        if pc_key and pc_password:
            try:
                payload = dict(payload)
                payload["perunchat_api_key"] = _secrets.encrypt_value(pc_key, pc_password)
            except _secrets.SecretError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif pc_password and SETTINGS.perunchat_api_key:
        # No new key supplied, but a password is now available: encrypt an
        # existing plaintext key in place.
        current = SETTINGS.perunchat_api_key
        if not _secrets.is_encrypted(current):
            try:
                SETTINGS.perunchat_api_key = _secrets.encrypt_value(current, pc_password)
            except _secrets.SecretError:
                pass

    # Encrypt perun_hitl_secret when a password is available.
    if "perun_hitl_secret" in payload:
        hitl_secret = str(payload.get("perun_hitl_secret") or "").strip()
        if hitl_secret and pc_password:
            try:
                payload = dict(payload)
                payload["perun_hitl_secret"] = _secrets.encrypt_value(hitl_secret, pc_password)
            except _secrets.SecretError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif pc_password and SETTINGS.perun_hitl_secret:
        current = SETTINGS.perun_hitl_secret
        if not _secrets.is_encrypted(current):
            try:
                SETTINGS.perun_hitl_secret = _secrets.encrypt_value(current, pc_password)
            except _secrets.SecretError:
                pass

    allowed = set(config.Settings.__dataclass_fields__)
    for key, value in payload.items():
        if key in allowed:
            setattr(SETTINGS, key, _coerce_setting(key, value))
    SETTINGS.save()
    # Keep the orchestrator in sync so run_timeout, default_model, etc. apply
    # immediately instead of requiring a server restart.
    ORCH.settings = SETTINGS
    ORCH.set_max_concurrent(SETTINGS.max_concurrent_runs)
    # Restart the autonomous PerunChat bridge so key/endpoint/auto_reply changes
    # take effect without a full restart.
    if PERUNCHAT_BRIDGE is not None:
        PERUNCHAT_BRIDGE.restart()
    data = SETTINGS.to_dict()
    data["telegram_token"] = ""
    data["telegram_token_set"] = bool(SETTINGS.telegram()[0])
    data["telegram_chat_count"] = len(SETTINGS.telegram()[1])
    pc_key, pc_endpoint = SETTINGS.perunchat()
    data["perunchat_api_key"] = ""
    data["perunchat_api_key_set"] = bool(pc_key)
    data["perunchat_api_endpoint"] = pc_endpoint
    return _ok(settings=data)


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
def api_telegram_status(request: Request) -> dict[str, Any]:
    token, chats = SETTINGS.telegram()
    data = _telegram_api("getMe") if token else {"ok": False}
    # The pairing code grants full agent access once, so reveal it only to a
    # loopback caller — not to another machine that can reach a wildcard bind.
    client_host = (request.client.host if request.client else "") or ""
    loopback = client_host in _LOOPBACK_NAMES
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
        # Only surfaced while nobody is allowed: the one-time code a chat must
        # send via ``/pair`` to gain access (loopback callers only).
        pair_code="" if (chats or not loopback) else SETTINGS.telegram_pair_code,
        token_source=SETTINGS.telegram_sources()["token"],
        chats_source=SETTINGS.telegram_sources()["chats"],
        bridge_running=bridge_running,
        bridge_task=task,
        bot=data.get("result") if data.get("ok") else None,
        platform=sys.platform,
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
        # A configured allow-list makes the pairing code unnecessary (and a
        # lingering code would be a second, overlooked door).
        if SETTINGS.telegram_allowed_chats:
            SETTINGS.telegram_pair_code = ""
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
            from . import secrets as _secrets
            password = (payload.get("password") or "").strip()
            if password:
                try:
                    SETTINGS.telegram_token = _secrets.encrypt_value(token, password)
                except _secrets.SecretError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            else:
                SETTINGS.telegram_token = token
            changed.append("token")
        else:
            SETTINGS.telegram_token = ""
            changed.append("token(cleared)")
    elif "password" in payload:
        # If a password is supplied without a new token, try to encrypt an
        # existing plain-text token in place. This lets the dashboard add
        # encryption to a token that was already saved.
        from . import secrets as _secrets
        password = (payload.get("password") or "").strip()
        current = SETTINGS.telegram_token
        if password and current and not _secrets.is_encrypted(current):
            try:
                SETTINGS.telegram_token = _secrets.encrypt_value(current, password)
                changed.append("token-encrypted")
            except _secrets.SecretError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

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


@app.post("/api/telegram/bot/change-password")
def api_telegram_change_password(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Change the encryption password for the stored Telegram token."""
    from . import secrets as _secrets

    old_pw = (payload.get("old_password") or "").strip()
    new_pw = (payload.get("new_password") or "").strip()
    if not old_pw or not new_pw:
        raise HTTPException(status_code=400, detail="old and new passwords are required")
    current = SETTINGS.telegram_token
    if not current:
        raise HTTPException(status_code=400, detail="no token stored")
    if not _secrets.is_encrypted(current):
        raise HTTPException(status_code=400, detail="token is not encrypted")
    decrypted = _secrets.decrypt_value(current, old_pw)
    if decrypted is None:
        raise HTTPException(status_code=400, detail="current password is incorrect")
    try:
        SETTINGS.telegram_token = _secrets.encrypt_value(decrypted, new_pw)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not re-encrypt token: {exc}")
    SETTINGS.save()
    return _ok(changed=True)


@app.post("/api/telegram/bridge/restart")
def api_telegram_bridge_restart() -> dict[str, Any]:
    """Reload the in-process bridge if the dashboard owns it, otherwise restart
    the standalone ``PerunBridge`` task so it re-reads the configured bot."""
    from . import __main__ as _main

    reloaded = _main.reload_in_process_bridge()
    if reloaded.get("reloaded"):
        return _ok(restart=reloaded)
    try:
        from . import installer
    except Exception as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=str(exc))
    return _ok(restart=installer.restart_bridge_task())


@app.post("/api/telegram/unlock")
def api_telegram_unlock(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Unlock an encrypted Telegram token with a password.

    Validates the password by decrypting the stored token, then makes it
    available to the running process. When ``remember`` is true, the password is
    persisted to the user's Windows environment so future launches do not need
    to prompt again. Finally the in-process bridge is started/reloaded, and the
    standalone ``PerunBridge`` task is restarted if the password was persisted.
    """
    from . import __main__ as _main, secrets as _secrets

    password = (payload.get("password") or "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="password is required")

    current = SETTINGS.telegram_token
    if not _secrets.is_encrypted(current):
        raise HTTPException(status_code=400, detail="token is not encrypted")

    if _secrets.decrypt_value(current, password) is None:
        raise HTTPException(status_code=400, detail="incorrect password")

    os.environ["PERUN_PASSWORD"] = password

    persisted = False
    if payload.get("remember"):
        persisted = _secrets.persist_password(password)

    bridge = _main.start_or_reload_in_process_bridge()

    if not bridge.get("started") and not bridge.get("reloaded"):
        # Determine whether another process owns the singleton bridge lock.
        try:
            from .telegram import PidLock
            lock_held_elsewhere = PidLock.is_held(config.HOME / "telegram.pid")
        except Exception:  # pragma: no cover - defensive
            lock_held_elsewhere = False

        if lock_held_elsewhere:
            if persisted:
                try:
                    from . import installer
                    if installer.bridge_task_exists():
                        bridge["standalone_restarted"] = installer.restart_bridge_task()
                except Exception as exc:  # pragma: no cover - defensive
                    bridge["standalone_error"] = str(exc)
            else:
                bridge["reason"] = (
                    "another bridge process owns the lock; enable 'Remember on this PC' "
                    "to restart it with the password"
                )
        elif persisted:
            # No in-process bridge started and no external lock; a registered
            # standalone task may pick up the persisted password.
            try:
                from . import installer
                if installer.bridge_task_exists():
                    bridge["standalone_restarted"] = installer.restart_bridge_task()
            except Exception as exc:  # pragma: no cover - defensive
                bridge["standalone_error"] = str(exc)

    return _ok(unlocked=True, persisted=persisted, bridge=bridge)


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
# PerunChat federation proxy (dashboard UI)
# --------------------------------------------------------------------------- #
def _perunchat_rest_base() -> str:
    """Derive the PerunChat REST base URL from the configured WS endpoint.

    The public deployment exposes the REST API under ``/chat/api/v1`` and the
    WebSocket under ``/v1/chat/ws``. Local/dev gateways expose both under the
    same origin with the REST API at ``/api/v1``.
    """
    endpoint = SETTINGS.perunchat()[1] or "wss://peruninthemachine.com/v1/chat/ws"
    base = config.perunchat_rest_base(endpoint)
    if not base:
        raise HTTPException(status_code=400, detail="invalid perunchat_api_endpoint")
    return base


def _perunchat_key() -> str:
    key, _ = SETTINGS.perunchat()
    if not key:
        raise HTTPException(status_code=400, detail="perunchat api key not configured")
    return key


def _perunchat_proxy(method: str, path: str, body: dict[str, Any] | None = None,
                     params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Forward a request to the PerunChat gateway using the configured key."""
    key = _perunchat_key()
    base = _perunchat_rest_base()
    url = f"{base}{path}"
    try:
        with httpx.Client(timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {key}"}
            if method == "GET":
                res = client.get(url, headers=headers, params=params)
            elif method == "POST":
                res = client.post(url, headers=headers, json=body)
            elif method == "DELETE":
                res = client.delete(url, headers=headers, params=params)
            else:
                raise HTTPException(status_code=405, detail="method not supported")
            res.raise_for_status()
            return res.json()
    except httpx.HTTPStatusError as exc:
        detail = "perunchat gateway error"
        try:
            detail = exc.response.json().get("detail", detail)
        except Exception:
            pass
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/perunchat/status")
def api_perunchat_status() -> dict[str, Any]:
    key, endpoint = SETTINGS.perunchat()
    rest_base = _perunchat_rest_base() if key else ""
    agent_id = ""
    if key:
        try:
            agent_id = _perunchat_proxy("GET", "/api/v1/me").get("agent_id", "")
        except Exception:
            pass
    return _ok(configured=bool(key), endpoint=endpoint, rest_base=rest_base, agent_id=agent_id)


@app.get("/api/perunchat/key")
def api_perunchat_key(request: Request) -> dict[str, Any]:
    """Reveal the PerunChat API key only to loopback clients so the dashboard
    can open a WebSocket connection directly to the chat gateway."""
    client_host = (request.client.host if request.client else "") or ""
    if client_host not in _LOOPBACK_NAMES:
        raise HTTPException(status_code=403, detail="key only available to loopback clients")
    key, _ = SETTINGS.perunchat()
    if not key:
        raise HTTPException(status_code=400, detail="perunchat api key not configured")
    return _ok(key=key)


@app.get("/api/perunchat/bridge/status")
def api_perunchat_bridge_status() -> dict[str, Any]:
    bridge = _get_perunchat_bridge()
    if bridge is None:
        return _ok(available=False, enabled=False, running=False)
    return _ok(**bridge.status)


@app.get("/api/perunchat/rooms")
def api_perunchat_rooms() -> dict[str, Any]:
    """Discoverable rooms (active, joined, and well-known public channels)."""
    return _perunchat_proxy("GET", "/api/v1/rooms")


@app.post("/api/perunchat/rooms/private")
def api_perunchat_create_private_room(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _perunchat_proxy("POST", "/api/v1/rooms/private", body=payload)


@app.get("/api/perunchat/rooms/private")
def api_perunchat_list_private_rooms() -> dict[str, Any]:
    return _perunchat_proxy("GET", "/api/v1/rooms/private")


@app.get("/api/perunchat/rooms/{room_id}/members")
def api_perunchat_room_members(room_id: str) -> dict[str, Any]:
    return _perunchat_proxy("GET", f"/api/v1/rooms/{room_id}/members")


@app.post("/api/perunchat/rooms/{room_id}/members")
def api_perunchat_room_add_member(room_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _perunchat_proxy("POST", f"/api/v1/rooms/{room_id}/members", body=payload)


@app.delete("/api/perunchat/rooms/{room_id}/members/{agent_id}")
def api_perunchat_room_remove_member(room_id: str, agent_id: str) -> dict[str, Any]:
    return _perunchat_proxy("DELETE", f"/api/v1/rooms/{room_id}/members/{agent_id}")


@app.get("/api/perunchat/social/friends")
def api_perunchat_social_friends() -> dict[str, Any]:
    return _perunchat_proxy("GET", "/api/v1/social/friends")


@app.get("/api/perunchat/social/friends/status")
def api_perunchat_social_friends_status() -> dict[str, Any]:
    return _perunchat_proxy("GET", "/api/v1/social/friends/status")


@app.get("/api/perunchat/social/friends/requests")
def api_perunchat_social_requests(direction: str = "all") -> dict[str, Any]:
    return _perunchat_proxy("GET", "/api/v1/social/friends/requests", params={"direction": direction})


@app.post("/api/perunchat/social/friends/requests")
def api_perunchat_social_send_request(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _perunchat_proxy("POST", "/api/v1/social/friends/requests", body=payload)


@app.post("/api/perunchat/social/friends/requests/{request_id}/accept")
def api_perunchat_social_accept(request_id: str) -> dict[str, Any]:
    return _perunchat_proxy("POST", f"/api/v1/social/friends/requests/{request_id}/accept")


@app.post("/api/perunchat/social/friends/requests/{request_id}/reject")
def api_perunchat_social_reject(request_id: str) -> dict[str, Any]:
    return _perunchat_proxy("POST", f"/api/v1/social/friends/requests/{request_id}/reject")


@app.delete("/api/perunchat/social/friends/{agent_id}")
def api_perunchat_social_remove_friend(agent_id: str) -> dict[str, Any]:
    return _perunchat_proxy("DELETE", f"/api/v1/social/friends/{agent_id}")


@app.get("/api/perunchat/social/blocked")
def api_perunchat_social_blocked() -> dict[str, Any]:
    return _perunchat_proxy("GET", "/api/v1/social/blocked")


@app.post("/api/perunchat/social/blocked")
def api_perunchat_social_block(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _perunchat_proxy("POST", "/api/v1/social/blocked", body=payload)


@app.delete("/api/perunchat/social/blocked/{agent_id}")
def api_perunchat_social_unblock(agent_id: str) -> dict[str, Any]:
    return _perunchat_proxy("DELETE", f"/api/v1/social/blocked/{agent_id}")


# --------------------------------------------------------------------------- #
# PerunChat trust list
#
# Internal/private context may only be shared with agents on this list, and only
# in private/group rooms. Public rooms never receive internal context. Entries
# are stored in config.json as ``perunchat_trusted``.
# --------------------------------------------------------------------------- #
def _trusted_agents() -> list[str]:
    return list(getattr(SETTINGS, "perunchat_trusted", []) or [])


def _handle_trust_revocations(removed: set[str]) -> None:
    """Re-sync shared-project chats when agents are removed from the trust list."""
    if not removed:
        return
    for project in registry.list_projects():
        cfg = shared_projects.load(project["path"])
        if not cfg.get("shared"):
            continue
        participants = {p["agent_id"].lower() for p in cfg.get("participants", [])}
        evicted = participants & removed
        if not evicted:
            continue
        updated, actually_removed = shared_projects.remove_participants(
            project["path"], evicted
        )
        if not actually_removed:
            continue
        # Refresh the registry snapshot before reconciling the gateway room so
        # the dashboard, bridge, and ACL all read the same participant list.
        try:
            projects.sync_manifest(project, sync_chat=False)
        except Exception:
            pass
        for agent_id in sorted(actually_removed):
            registry.log_activity(
                project["id"],
                "trust_revoked",
                f"Participant '{agent_id}' removed from PerunChat trust list and project manifest",
                {"agent_id": agent_id},
            )
        try:
            shared_project_sync.sync_project_chat(project, updated)
        except Exception:
            pass


def _save_trusted(agents: list[str]) -> list[str]:
    global SETTINGS
    old = {a.strip().lower() for a in getattr(SETTINGS, "perunchat_trusted", []) if str(a).strip()}
    # De-duplicate while preserving order, case-insensitively.
    seen: set[str] = set()
    clean: list[str] = []
    for raw in agents:
        name = str(raw).strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            clean.append(name)
    removed = old - {c.lower() for c in clean}
    SETTINGS.perunchat_trusted = clean
    SETTINGS.save()
    if removed:
        _handle_trust_revocations(removed)
        registry.log_activity(
            None,
            "trusted_list_changed",
            f"Removed from PerunChat trust list: {', '.join(sorted(removed))}",
            {"removed": sorted(removed)},
        )
    return clean


@app.get("/api/perunchat/trusted")
def api_perunchat_trusted_list() -> dict[str, Any]:
    return _ok(trusted=_trusted_agents())


@app.post("/api/perunchat/trusted")
def api_perunchat_trusted_add(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    agent_id = str(payload.get("agent_id") or payload.get("agent") or "").strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required")
    return _ok(trusted=_save_trusted(_trusted_agents() + [agent_id]))


@app.delete("/api/perunchat/trusted/{agent_id}")
def api_perunchat_trusted_remove(agent_id: str) -> dict[str, Any]:
    target = agent_id.strip().lower()
    remaining = [a for a in _trusted_agents() if a.lower() != target]
    return _ok(trusted=_save_trusted(remaining))


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
    @app.get("/", include_in_schema=False)
    def _dashboard_index() -> HTMLResponse:
        """Serve the SPA shell with the local API token injected.

        A state-changing API call needs ``X-Perun-Token`` (see the middleware);
        the dashboard is same-origin as the server, so handing it the token here
        is equivalent to Jupyter embedding its token in the page it serves.
        """
        page = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
        snippet = (
            "<script>window.PERUN_TOKEN = "
            f"{json.dumps(config.api_token())};</script>"
        )
        if "</head>" in page:
            page = page.replace("</head>", f"    {snippet}\n</head>", 1)
        else:  # pragma: no cover - defensive
            page = snippet + page
        return HTMLResponse(page)

    app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True),
              name="web")
else:  # pragma: no cover
    @app.get("/")
    def _no_web() -> JSONResponse:
        return JSONResponse({"ok": True, "detail": "web/ dashboard not found",
                             "home": str(config.HOME)})


def request_shutdown() -> bool:
    """Ask uvicorn to exit gracefully by setting ``should_exit`` on the running
    ``Server`` instance(s). Returns ``True`` when at least one server was told to
    stop; ``False`` when no handle was found and the caller should fall back to
    sending a signal."""
    try:
        from uvicorn.server import Server

        stopped = False
        for server in list(getattr(Server, "servers", [])):
            try:
                server.should_exit = True
                stopped = True
            except Exception:
                continue
        return stopped
    except Exception:
        return False


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
    try:
        from uvicorn.server import Server

        original_run = Server.run

        def _patched_run(self):
            try:
                if not hasattr(Server, "servers"):
                    Server.servers = set()
                Server.servers.add(self)
                return original_run(self)
            finally:
                try:
                    Server.servers.discard(self)
                except Exception:
                    pass

        Server.run = _patched_run
    except Exception:
        pass
    uvicorn.run("perun.server:app", host=host, port=port, reload=reload,
                log_level="info")

