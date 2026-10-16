"""Discover and probe the MCP servers OpenCode is configured to use.

OpenCode reads MCP server definitions from the ``mcp`` map of its JSON config:

* machine-wide: ``~/.config/opencode/opencode.json`` (and ``.jsonc``), and
* per project:  ``<project>/opencode.json`` / ``opencode.jsonc``.

Local servers look like::

    {"type": "local", "command": ["python", "server.py"], "enabled": true}

Remote servers look like::

    {"type": "remote", "url": "https://host/mcp", "enabled": true}

Perun is a *reader* here: it never writes those files (the global installer owns
the ``perun`` entry). :func:`snapshot` lists every server with a cheap,
config-derived status; :func:`probe` is the on-demand, opt-in step that actually
spawns a local server (or calls a remote URL) to perform the MCP ``initialize``
+ ``tools/list`` handshake, so the dashboard can show real health instead of a
guess. Nothing here runs automatically — probing is always triggered by an
explicit user action and bounded by a concurrency gate so a burst of probes
cannot pin the server's worker threads or spawn unbounded processes.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__, config

# MCP protocol version we advertise during the handshake. Servers may negotiate
# anything; we only need a version both sides understand to exchange tool lists.
MCP_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_PROBE_TIMEOUT = 20.0
# Hard ceiling for a single probe, regardless of what the caller asks for.
MAX_PROBE_TIMEOUT = 60.0
MAX_TOOLS = 500
# Budget for captured stderr, used only to explain a failed handshake.
STDERR_TAIL = 2000

# Config file names OpenCode looks for, in precedence order.
_CONFIG_NAMES = ("opencode.json", "opencode.jsonc")

# At most this many MCP servers are spawned/handshaked at once. The endpoint is
# synchronous and runs in FastAPI's shared thread pool, so an unbounded number of
# concurrent probes could starve every other sync endpoint; callers that cannot
# get a slot fail fast with a ``busy`` status instead of blocking a worker.
MAX_CONCURRENT_PROBES = 2
_PROBE_GATE = threading.BoundedSemaphore(MAX_CONCURRENT_PROBES)
_GATE_WAIT = 5.0


# --------------------------------------------------------------------------- #
# Tolerant JSON / JSONC loading
# --------------------------------------------------------------------------- #
def _strip_jsonc(text: str) -> str:
    """Drop ``//`` / ``/* */`` comments *and* trailing commas from JSONC.

    Both passes track string literals so ``","`` inside a value is never touched.
    OpenCode configs are ``.jsonc``-style (VS Code allows trailing commas), so
    tolerating them avoids silently dropping an otherwise valid config.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    escaped = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch == ",":
            # Drop the comma when the next non-whitespace char closes a block.
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _read_config(path: Path) -> tuple[dict[str, Any], str | None]:
    """Parse a JSON/JSONC file.

    Returns ``(data, error)``; ``error`` is a human-readable string when the file
    exists but could not be read or parsed, so a broken config surfaces in the
    dashboard instead of silently looking like "no servers".
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"cannot read: {exc}"
    last: json.JSONDecodeError | None = None
    for candidate in (raw, _strip_jsonc(raw)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last = exc
            continue
        if isinstance(data, dict):
            return data, None
        return {}, "top-level value is not a JSON object"
    return {}, f"invalid JSON: {last}"


def _config_files(directory: Path) -> list[Path]:
    return [directory / name for name in _CONFIG_NAMES if (directory / name).is_file()]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def global_config_files() -> list[Path]:
    """Config files that define machine-wide MCP servers."""
    return _config_files(config.OPENCODE_CONFIG_DIR)


def project_config_files(project_path: str | Path | None) -> list[Path]:
    """Config files that define MCP servers for one project."""
    if not project_path:
        return []
    try:
        directory = Path(project_path)
    except (TypeError, ValueError):
        return []
    return _config_files(directory)


def _server_entries(data: dict[str, Any]) -> dict[str, Any]:
    mcp = data.get("mcp")
    return mcp if isinstance(mcp, dict) else {}


def _resolve_command(command: Any) -> tuple[list[str], str | None]:
    """Normalize a local server ``command`` and resolve its executable on PATH.

    Returns ``(argv, resolved_exe)``; ``resolved_exe`` is ``None`` when the first
    token is not found. Command values may be a list (OpenCode's canonical form)
    or a shell-ish string, which we split on whitespace.
    """
    if isinstance(command, str):
        argv = command.split()
    elif isinstance(command, list):
        argv = [str(c) for c in command]
    else:
        argv = []
    if not argv:
        return [], None
    first = argv[0]
    resolved = first if os.path.isabs(first) and Path(first).is_file() else shutil.which(first)
    return argv, resolved


def windows_launch_argv(argv: list[str], resolved: str | None, *, os_name: str | None = None) -> list[str]:
    """Wrap a resolved command so Windows can launch non-``.exe`` shims.

    ``CreateProcess`` cannot run a ``.cmd``/``.bat``/``.ps1`` directly, so those
    are routed through ``cmd.exe`` / PowerShell. Pure function (no I/O) so it can
    be unit-tested on any platform; ``os_name`` exists purely for that. No-op off
    Windows.
    """
    plat = os_name or os.name
    if plat != "nt" or not resolved:
        return list(argv)
    low = str(resolved).lower()
    if low.endswith((".cmd", ".bat")):
        return ["cmd", "/c", *argv]
    if low.endswith(".ps1"):
        return ["powershell", "-NoProfile", "-File", resolved, *argv[1:]]
    return list(argv)


def _normalize(name: str, raw: Any, *, scope: str, source: Path,
               project: dict[str, Any] | None) -> dict[str, Any] | None:
    """Turn one raw ``mcp`` entry into the normalized shape the UI consumes."""
    if not isinstance(raw, dict):
        return None
    server_type = str(raw.get("type") or ("remote" if raw.get("url") else "local")).lower()
    entry: dict[str, Any] = {
        "name": name,
        "scope": scope,
        "source": str(source),
        "enabled": raw.get("enabled", True) is not False,
        "type": server_type,
        "project": (project or {}).get("slug", ""),
        "project_name": (project or {}).get("name", ""),
    }
    if server_type == "remote":
        entry["transport"] = "remote"
        entry["url"] = str(raw.get("url") or "")
        entry["command"] = []
        entry["resolved"] = None
    else:
        argv, resolved = _resolve_command(raw.get("command"))
        entry["transport"] = "local"
        entry["command"] = argv
        entry["resolved"] = resolved
        entry["url"] = ""
    entry["status"] = _derived_status(entry)
    return entry


def _derived_status(entry: dict[str, Any]) -> str:
    """Cheap, config-only status (no process is spawned)."""
    if not entry["enabled"]:
        return "disabled"
    if entry["transport"] == "remote":
        return "remote" if entry.get("url") else "invalid"
    if not entry.get("command"):
        return "invalid"
    return "ready" if entry.get("resolved") else "missing"


def _collect(files: list[Path], *, scope: str, project: dict[str, Any] | None
             ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return ``(entries, errors)`` for a set of config files."""
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in files:
        data, error = _read_config(path)
        if error:
            errors.append({"source": str(path), "error": error})
            continue
        for name, raw in _server_entries(data).items():
            entry = _normalize(str(name), raw, scope=scope, source=path, project=project)
            if entry:
                entries.append(entry)
    return entries, errors


def snapshot(projects: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """List every configured MCP server (global first, then per project).

    Project entries shadow global ones of the same name; the shadowed global
    entry is still listed but flagged with ``shadowed_by``. Duplicate
    ``(scope, project, name)`` entries (e.g. a name defined in both
    ``opencode.json`` and ``opencode.jsonc``) are collapsed to the first.
    """
    servers: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            key = (entry["scope"], entry["project"], entry["name"])
            if key in seen:
                continue
            seen.add(key)
            servers.append(entry)

    glob, glob_errors = _collect(global_config_files(), scope="global", project=None)
    _add(glob)
    errors.extend(glob_errors)
    for project in (projects or []):
        entries, errs = _collect(project_config_files(project.get("path")),
                                 scope="project", project=project)
        _add(entries)
        errors.extend(errs)

    # Mark global servers that one or more projects override with the same name.
    project_names: dict[str, list[str]] = {}
    for s in servers:
        if s["scope"] == "project":
            project_names.setdefault(s["name"], []).append(s["project"])
    for s in servers:
        if s["scope"] == "global" and s["name"] in project_names:
            s["shadowed_by"] = sorted(set(project_names[s["name"]]))

    counts: dict[str, int] = {}
    for s in servers:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {
        "servers": servers,
        "count": len(servers),
        "counts": counts,
        "parse_errors": errors,
        "config_files": {
            "global": [str(p) for p in global_config_files()],
        },
    }


def find(name: str, *, scope: str = "global",
         projects: list[dict[str, Any]] | None = None,
         project: str = "") -> dict[str, Any] | None:
    """Return the normalized entry for *name* in the given scope, or ``None``.

    Project scope is keyed by slug, so the caller passes registry output.
    """
    name = (name or "").strip()
    if not name:
        return None
    if scope == "project":
        target = next((p for p in (projects or []) if p.get("slug") == project), None)
        if target is None:
            return None
        entries, _ = _collect(project_config_files(target.get("path")),
                              scope="project", project=target)
    else:
        entries, _ = _collect(global_config_files(), scope="global", project=None)
    return next((e for e in entries if e["name"] == name), None)


# --------------------------------------------------------------------------- #
# Live probe
# --------------------------------------------------------------------------- #
def _drain_stderr(proc: subprocess.Popen, sink: list[str], cap: int = STDERR_TAIL) -> None:
    """Read stderr to EOF, keeping only the last *cap* characters (bounded memory)."""
    try:
        assert proc.stderr is not None
        buf = ""
        for line in proc.stderr:
            buf = (buf + line)[-cap:]
    except Exception:  # pragma: no cover - best effort
        return
    if buf:
        sink.append(buf)


def _reader(proc: subprocess.Popen, lines: queue.Queue) -> None:
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
    except Exception:  # pragma: no cover - pipe closed
        pass
    finally:
        lines.put(None)


def _probe_stdio(entry: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Spawn a local MCP server and perform initialize + tools/list."""
    argv = list(entry.get("command") or [])
    resolved = entry.get("resolved")
    if not argv:
        return {"status": "invalid", "ok": False, "error": "no command configured", "tools": []}
    if not resolved:
        return {"status": "missing", "ok": False,
                "error": f"executable not found on PATH: {argv[0]}", "tools": []}
    argv = windows_launch_argv(argv, resolved)
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")  # so stdout flushes promptly
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        return {"status": "error", "ok": False, "error": f"cannot start: {exc}", "tools": []}

    lines: queue.Queue = queue.Queue()
    stderr_buf: list[str] = []
    threading.Thread(target=_reader, args=(proc, lines), daemon=True).start()
    threading.Thread(target=_drain_stderr, args=(proc, stderr_buf), daemon=True).start()

    def _send(obj: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def _await(msg_id: int, deadline: float) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no reply to id={msg_id} within {timeout:.0f}s")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"no reply to id={msg_id} within {timeout:.0f}s") from None
            if line is None:
                raise EOFError("server closed stdout during handshake")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate log noise / banners on stdout
            if isinstance(msg, dict) and msg.get("id") == msg_id:
                return msg

    deadline = started + timeout
    result: dict[str, Any] = {"status": "ok", "ok": True, "tools": [], "error": ""}
    try:
        _send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "perun", "version": __version__},
            },
        })
        init = _await(1, deadline)
        if "error" in init:
            raise RuntimeError(f"initialize failed: {init.get('error')}")
        info = (init.get("result") or {}).get("serverInfo") or {}
        result["server_info"] = {
            "name": str(info.get("name") or ""),
            "version": str(info.get("version") or ""),
        }
        result["protocol_version"] = str((init.get("result") or {}).get("protocolVersion") or "")
        _send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listing = _await(2, deadline)
        if "error" in listing:
            raise RuntimeError(f"tools/list failed: {listing.get('error')}")
        tools = (listing.get("result") or {}).get("tools") or []
        names = [str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")]
        result["tools"] = names[:MAX_TOOLS]
        result["tool_count"] = len(names)
    except (TimeoutError, EOFError, RuntimeError) as exc:
        result.update(status="unreachable" if isinstance(exc, (TimeoutError, EOFError)) else "error",
                      ok=False, error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        result.update(status="error", ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:  # pragma: no cover
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # pragma: no cover - force kill, then reap
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        stderr_text = "".join(stderr_buf).strip()
        if stderr_text and not result.get("ok"):
            result["stderr"] = stderr_text[-STDERR_TAIL:]
    return result


def _probe_http(entry: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Best-effort reachability + tools/list for a remote (HTTP) MCP server.

    Speaks the streamable-HTTP MCP handshake far enough to report health: a
    JSON-RPC ``initialize`` POST followed by ``tools/list`` (echoing any
    ``Mcp-Session-Id`` the server returns). The two calls share one deadline, so
    the whole probe honours *timeout* rather than taking twice as long. Servers
    that only expose SSE or need auth surface as ``unreachable``/``error`` with
    the HTTP status rather than hanging.
    """
    url = entry.get("url") or ""
    if not url:
        return {"status": "invalid", "ok": False, "error": "no url configured", "tools": []}
    import httpx

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    started = time.monotonic()
    deadline = started + timeout
    result: dict[str, Any] = {"status": "ok", "ok": True, "tools": [], "error": ""}

    def _rpc(method: str, msg_id: int, params: dict[str, Any],
             session: str | None) -> tuple[dict[str, Any], str | None]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise httpx.TimeoutException(f"probe deadline exceeded before {method}")
        body = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        hdrs = dict(headers)
        if session:
            hdrs["Mcp-Session-Id"] = session
        resp = httpx.post(url, json=body, headers=hdrs, timeout=remaining)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} for {method}")
        new_session = resp.headers.get("mcp-session-id") or session
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            payload = None
            for raw in resp.text.splitlines():
                if raw.startswith("data:"):
                    try:
                        payload = json.loads(raw[5:].strip())
                    except json.JSONDecodeError:
                        continue
            if payload is None:
                raise RuntimeError(f"empty SSE payload for {method}")
            return payload, new_session
        try:
            return resp.json(), new_session
        except Exception as exc:
            raise RuntimeError(f"non-JSON reply for {method}: {exc}") from exc

    try:
        init, session = _rpc("initialize", 1, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "perun", "version": __version__},
        }, None)
        if "error" in init:
            raise RuntimeError(f"initialize failed: {init.get('error')}")
        info = (init.get("result") or {}).get("serverInfo") or {}
        result["server_info"] = {
            "name": str(info.get("name") or ""),
            "version": str(info.get("version") or ""),
        }
        listing, _ = _rpc("tools/list", 2, {}, session)
        tools = (listing.get("result") or {}).get("tools") or []
        names = [str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")]
        result["tools"] = names[:MAX_TOOLS]
        result["tool_count"] = len(names)
    except Exception as exc:
        status = "unreachable" if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)) else "error"
        result.update(status=status, ok=False, error=str(exc))
    finally:
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


def probe(name: str, *, scope: str = "global", projects: list[dict[str, Any]] | None = None,
          project: str = "", timeout: float = DEFAULT_PROBE_TIMEOUT) -> dict[str, Any]:
    """Probe one configured server. Raises :class:`LookupError` if not configured.

    The command/URL is always read from the on-disk config for the named server —
    callers cannot inject an arbitrary command to run. Concurrent probes are
    capped; a caller that cannot get a slot within a few seconds receives a
    ``busy`` result instead of blocking a worker thread.
    """
    scope = "project" if scope == "project" else "global"
    entry = find(name, scope=scope, projects=projects, project=project)
    if entry is None:
        raise LookupError(f"no MCP server '{name}' in {scope} scope")
    timeout = max(1.0, min(float(timeout or DEFAULT_PROBE_TIMEOUT), MAX_PROBE_TIMEOUT))
    if not _PROBE_GATE.acquire(timeout=_GATE_WAIT):
        return {"server": entry, "status": "busy", "ok": False, "tools": [],
                "error": "another MCP probe is already running; try again in a moment"}
    try:
        outcome = _probe_http(entry, timeout) if entry["transport"] == "remote" \
            else _probe_stdio(entry, timeout)
    finally:
        _PROBE_GATE.release()
    return {"server": entry, **outcome}
