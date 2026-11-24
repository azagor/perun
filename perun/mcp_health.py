"""Health, diagnosis and repair for the Perun MCP server.

OpenCode talks to Perun over a local stdio server (``python mcp/perun_mcp.py``).
The client gives that handshake a hard deadline (observed at 60 s) and importing
``fastmcp`` alone costs several seconds on a cold, busy machine, so a slow start
turns into a dead connection with no error the operator can act on. Worse: once
OpenCode has cached ``failed``, a later connect can wedge in ``pending`` until
the server is explicitly disconnected (OpenCode 2.2.5).

The countermeasures live here, off every hot path:

* ``record_startup``/``read_startup`` keep the *measured* startup cost on disk,
  so "it was slow" is a number instead of a guess;
* ``server_status``/``revive`` read and repair the client-side state through
  ``/api/mcp`` and ``/api/experimental/mcp/{server}/{connect,disconnect}``;
* ``selftest`` performs the same stdio handshake the client does, timed, so the
  server can be proven healthy before the editor is blamed;
* ``diagnose`` folds the three into one verdict plus a remedy.

Only ``record_startup`` runs inside the MCP process, and it never raises.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import config, opencode_api

#: Written by the MCP server on every start, read by ``perun mcp-health``.
STARTUP_FILE: Path = config.HOME / "mcp-startup.json"

#: OpenCode's connect deadline is 60 s; anything above this is a warning sign.
SLOW_STARTUP_SECONDS = 30.0

#: One MCP "connect" call blocks for the whole handshake, so give it room.
CONNECT_TIMEOUT_SECONDS = 180.0

#: Closing a live connection can also block; it must never hold up the repair.
DISCONNECT_TIMEOUT_SECONDS = 60.0

#: How long to wait for the client to report the final state.
SETTLE_SECONDS = 120.0


def record_startup(payload: dict[str, Any]) -> None:
    """Merge *payload* into the startup record. Never raises."""
    try:
        record = read_startup()
        record.update(payload)
        record["recorded_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime())
        STARTUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STARTUP_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.replace(tmp, STARTUP_FILE)
    except Exception:  # pragma: no cover - diagnostics must never break a start
        pass


def read_startup() -> dict[str, Any]:
    """Last recorded MCP startup, or ``{}`` when nothing was recorded yet."""
    try:
        data = json.loads(STARTUP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _api(api: Any = None) -> Any:
    return api if api is not None else opencode_api.default_api()


def server_status(server: str = "perun", directory: str = "",
                  api: Any = None) -> dict[str, Any]:
    """State the OpenCode service reports for one MCP server.

    ``state`` is the service's own word (``connected``, ``failed``,
    ``pending``, ``disabled``) or ``""`` when the server is not configured for
    that directory.
    """
    directory = directory or os.getcwd()
    try:
        rows = _api(api).mcp_servers(directory)
    except Exception as exc:
        return {"server": server, "directory": directory, "state": "",
                "error": str(exc)[:300], "configured": False}
    for row in rows:
        if str(row.get("name") or "") != server:
            continue
        status = row.get("status") if isinstance(row.get("status"), dict) else {}
        return {
            "server": server,
            "directory": directory,
            "state": str(status.get("status") or ""),
            "error": str(status.get("error") or "")[:300],
            "configured": True,
        }
    return {"server": server, "directory": directory, "state": "",
            "error": "", "configured": False}


def project_directories(api: Any = None) -> list[str]:
    """Every directory that can hold a session: service locations + registry.

    OpenCode keeps one MCP connection **per location**, and caches a failure for
    the whole directory, so a repair has to visit each one — not just the
    directory the command happens to run in.
    """
    directories: list[str] = []
    seen: set[str] = set()

    def add(path: Any) -> None:
        text = str(path or "").strip()
        if not text:
            return
        key = os.path.normcase(os.path.normpath(text))
        if key in seen:
            return
        seen.add(key)
        directories.append(text)

    try:
        for location in _api(api).locations():
            add(location)
    except Exception:
        pass
    try:
        from . import registry

        for project in registry.list_projects():
            add(project.get("path"))
    except Exception:  # pragma: no cover - a read-only listing is best effort
        pass
    return directories


def revive(server: str = "perun", directory: str = "", api: Any = None,
           connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
           wait: float = SETTLE_SECONDS) -> dict[str, Any]:
    """Disconnect then reconnect one MCP server and report the final state.

    The disconnect is what breaks a connection stuck in ``pending`` — without it
    the client keeps answering the connect call with the old, half-open attempt.
    """
    directory = directory or os.getcwd()
    client = _api(api)
    before = server_status(server, directory, client)
    if before["state"] == "connected":
        return {"ok": True, "state": "connected", "already": True,
                "directory": directory, "server": server}
    try:
        client.mcp_disconnect(server, directory,
                              timeout=DISCONNECT_TIMEOUT_SECONDS)
    except Exception:
        # Disconnect is best-effort: it fails on a server that is already gone,
        # and a wedged service can leave the HTTP call hanging.
        pass
    try:
        client.mcp_connect(server, directory, timeout=connect_timeout)
    except Exception as exc:
        # The client may drop the HTTP call after the handshake still succeeds,
        # so the state check below decides, not the exception.
        failure = str(exc)[:300]
    else:
        failure = ""
    deadline = time.time() + wait
    status = server_status(server, directory, client)
    while status["state"] in ("pending", "") and time.time() < deadline:
        time.sleep(2.0)
        status = server_status(server, directory, client)
    return {
        "ok": status["state"] == "connected",
        "state": status["state"],
        "error": status["error"] or failure,
        "directory": directory,
        "server": server,
        "already": False,
    }


def _selftest_async(command: list[str], environment: dict[str, str],
                    timeout: float) -> dict[str, Any]:
    import asyncio

    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    async def run() -> dict[str, Any]:
        transport = StdioTransport(command=command[0], args=command[1:],
                                   env=environment, cwd=str(config.REPO_ROOT))
        began = time.monotonic()
        async with Client(transport) as client:
            handshake = time.monotonic() - began
            tools = await client.list_tools()
            return {
                "ok": True,
                "initialize_seconds": round(handshake, 3),
                "tools": len(tools),
                "total_seconds": round(time.monotonic() - began, 3),
            }

    return asyncio.run(asyncio.wait_for(run(), timeout=timeout))


def selftest(timeout: float = 120.0) -> dict[str, Any]:
    """Start the MCP server exactly as the client does, and time the handshake.

    This is the ground truth for "the server is fine, the connection is not".
    """
    command = [sys.executable, str(config.REPO_ROOT / "mcp" / "perun_mcp.py")]
    environment = {**os.environ, "PERUN_HOME": str(config.HOME)}
    began = time.monotonic()
    try:
        result = _selftest_async(command, environment, timeout)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300] or type(exc).__name__,
                "total_seconds": round(time.monotonic() - began, 3)}
    return result


_REMEDY = {
    "connected": "",
    "failed": "run `perun mcp-health --fix`; if it still fails, restart the "
              "editor (the client caches the failed connection per session)",
    "pending": "run `perun mcp-health --fix` — the connect wedged in pending "
               "and only an explicit disconnect clears it",
    "disabled": "run `perun mcp-health --fix` to connect it",
    "": "the server is not configured for this directory: add the `perun` "
        "entry to the OpenCode MCP config",
}


def diagnose(server: str = "perun", directory: str = "", api: Any = None) -> dict[str, Any]:
    """One verdict for the operator: state, measured startup, likely remedy."""
    directory = directory or os.getcwd()
    status = server_status(server, directory, api)
    startup = read_startup()
    total = float(startup.get("total_seconds") or 0.0)
    slow = bool(total and total >= SLOW_STARTUP_SECONDS)
    state = status["state"]
    verdict = {
        "connected": "ok",
        "failed": "connect_failed",
        "pending": "stuck_pending",
        "disabled": "not_connected",
    }.get(state, "not_configured")
    remedy = _REMEDY.get(state, "")
    if slow:
        verdict = f"{verdict} (slow_startup)" if verdict != "ok" else "ok (slow_startup)"
        remedy = (remedy + "; " if remedy else "") + (
            f"last start took {total:.0f}s of the client's 60s deadline — the "
            "import cost is the risk, see `perun mcp-health --selftest`")
    return {
        "server": server,
        "directory": directory,
        "state": state,
        "error": status["error"],
        "configured": status["configured"],
        "verdict": verdict,
        "remedy": remedy,
        "last_startup": startup,
        "slow_startup": slow,
    }
