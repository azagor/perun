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
import threading
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

#: How long to wait for a repaired connection to report its final state.
SETTLE_SECONDS = 120.0

#: How long a *diagnosis* waits for a ``pending`` connection to resolve. The
#: client itself gives up after 60 s, so a pending state has to become terminal
#: within that window; a diagnostic must not outlast it by much.
PENDING_SETTLE_SECONDS = 60.0

#: Reading ``/api/mcp`` for a directory makes the service materialize that
#: location, and the connect then happens off the request: measured as no row at
#: t+0, ``pending`` at t+2 s, ``connected`` at t+4 s. A location that reports no
#: server is therefore provisional, never an instant "not configured".
MATERIALIZE_SETTLE_SECONDS = 6.0

#: Poll interval while waiting for a materialized location to list its server.
MATERIALIZE_POLL_SECONDS = 1.0

#: Poll interval while waiting for ``pending`` to settle.
SETTLE_POLL_SECONDS = 2.0

#: ``os.replace`` fails on Windows while another process holds the destination
#: open for reading (a read handle has no ``FILE_SHARE_DELETE``), and a
#: diagnostics file is read by exactly such processes. A short retry keeps a
#: record that is otherwise fine instead of silently dropping it.
REPLACE_ATTEMPTS = 5
REPLACE_RETRY_SECONDS = 0.05

#: Two writers merge into one file from different threads of the same process
#: (the import record, then the schema record), so the read-merge-write is
#: serialised. It does **not** cover other processes: every OpenCode location
#: starts its own MCP server and they all write this one file, so a write whose
#: ``pid`` differs from the record's replaces it instead of merging into it.
_RECORD_LOCK = threading.Lock()


def record_startup(payload: dict[str, Any]) -> None:
    """Merge *payload* into the startup record. Never raises.

    A payload that names a different ``pid`` than the record is a concurrent
    start from another location: the record describes one run, so it is replaced
    rather than merged, and the payload must carry that run's identity fields.
    """
    tmp: Path | None = None
    try:
        with _RECORD_LOCK:
            record = read_startup()
            pid = payload.get("pid")
            if pid is not None and record.get("pid") != pid:
                record = {}
            record.update(payload)
            record["recorded_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime())
            STARTUP_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STARTUP_FILE.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
            for attempt in range(1, REPLACE_ATTEMPTS + 1):
                try:
                    os.replace(tmp, STARTUP_FILE)
                except OSError:
                    # Another process is reading it right now; retry briefly.
                    if attempt == REPLACE_ATTEMPTS:
                        raise
                    time.sleep(REPLACE_RETRY_SECONDS)
                else:
                    tmp = None
                    break
    except Exception:  # diagnostics must never break a start
        pass
    finally:
        # A failed write must not leave temp files in the Perun home.
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
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
    ``pending``, ``disabled``) or ``""`` when it lists no such server.
    ``reachable`` says whether the connection state could be read at all: an
    unreadable state must never be reported as "not configured", which would
    excuse a dead connection instead of naming it. ``answered`` separates the
    two ways that can fail — the service answered with an error status
    (``http_status``; 404 means it refused the location) or it did not answer at
    all — because the remedy differs.
    """
    directory = directory or os.getcwd()
    try:
        rows = _api(api).mcp_servers(directory)
    except opencode_api.OpenCodeHTTPError as exc:
        # The service answered, with an error. "Start the service" would be the
        # wrong advice, but the connection state is still unknown.
        return {"server": server, "directory": directory, "state": "",
                "error": str(exc)[:300], "configured": False,
                "reachable": False, "answered": True,
                "http_status": exc.status}
    except Exception as exc:
        return {"server": server, "directory": directory, "state": "",
                "error": str(exc)[:300], "configured": False,
                "reachable": False, "answered": False, "http_status": None}
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
            "reachable": True,
            "answered": True,
            "http_status": None,
        }
    return {"server": server, "directory": directory, "state": "",
            "error": "", "configured": False, "reachable": True,
            "answered": True, "http_status": None}


def directory_key(path: Any) -> str:
    """Identity of a directory: case-, separator- and 8.3-insensitive.

    OpenCode can list one directory under its long name and its 8.3 short form
    (``C:\\Users\\LSZCZE~1\\...``); both are the same location, and a repair
    must visit it once. ``realpath`` is what expands the short form.
    """
    text = str(path or "").strip()
    return os.path.normcase(os.path.realpath(text)) if text else ""


def service_locations(api: Any = None) -> list[str]:
    """Every location the OpenCode service knows right now."""
    return [str(path) for path in _api(api).locations() if str(path or "").strip()]


def location_known(directory: str = "", api: Any = None) -> bool:
    """Whether the service has a location for *directory*.

    An unreadable location list answers ``False``: the caller then waits out its
    window and reports ``not_confirmed``, which is what it actually knows.
    """
    key = directory_key(directory)
    if not key:
        return False
    try:
        return any(directory_key(path) == key for path in service_locations(api))
    except Exception:
        return False


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
        # A directory that no longer exists cannot hold a session, and asking
        # the service about it would materialize a stale location there.
        if not os.path.isdir(text):
            return
        key = directory_key(text)
        if key in seen:
            return
        seen.add(key)
        directories.append(text)

    try:
        for location in service_locations(api):
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


def settle(server: str = "perun", directory: str = "", api: Any = None,
           wait: float = SETTLE_SECONDS) -> dict[str, Any]:
    """Poll while the client reports ``pending``, then return the last status.

    Only ``pending`` is transient. An empty state means the service lists no
    such server, or could not be asked at all — waiting cannot change either,
    so an unreachable service is returned immediately instead of burning the
    whole window on a state that will never settle.
    """
    client = _api(api)
    status = server_status(server, directory, client)
    # Monotonic: the machine may sleep mid-wait, and the window is a duration.
    deadline = time.monotonic() + wait
    while status["state"] == "pending" and time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(SETTLE_POLL_SECONDS, remaining))
        status = server_status(server, directory, client)
    return status


def revive(server: str = "perun", directory: str = "", api: Any = None,
           connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
           wait: float = SETTLE_SECONDS) -> dict[str, Any]:
    """Disconnect then reconnect one MCP server and report the final state.

    The disconnect is what breaks a connection stuck in ``pending`` — without it
    the client keeps answering the connect call with the old, half-open attempt.
    Nothing is attempted through a service whose state cannot be read.
    """
    directory = directory or os.getcwd()
    client = _api(api)
    before = server_status(server, directory, client)
    if before["state"] == "connected":
        return {"ok": True, "state": "connected", "already": True,
                "directory": directory, "server": server}
    if not before["reachable"]:
        return {"ok": False, "state": before["state"], "error": before["error"],
                "directory": directory, "server": server, "already": False}
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
    status = settle(server, directory, client, wait)
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

UNREACHABLE_REMEDY = (
    "the OpenCode service did not answer, so the connection state is unknown — "
    "start or repair the service (`opencode service status`, `opencode serve`) "
    "and run this again")

HTTP_ERROR_REMEDY = (
    "the OpenCode service answered HTTP {status} instead of the MCP state, so "
    "the connection is unknown — its own log has the cause; repair the service "
    "and run this again")

LOCATION_REFUSED_REMEDY = (
    "the OpenCode service answered HTTP 404 for this directory — it cannot open "
    "the path, or this OpenCode build has no MCP endpoint; check that the "
    "directory exists and that OpenCode is up to date")

UNKNOWN_STATE_REMEDY = (
    "the client lists the server but reports no state for it — run "
    "`perun mcp-health --fix` to reconnect it")

NOT_CONFIRMED_REMEDY = (
    "the service never took this directory as a location, so whether the server "
    "is configured there is unknown — check that the path exists, then run this "
    "again")


def diagnose(server: str = "perun", directory: str = "", api: Any = None) -> dict[str, Any]:
    """One verdict for the operator: state, measured startup, likely remedy."""
    directory = directory or os.getcwd()
    status = server_status(server, directory, api)
    startup = read_startup()
    total = float(startup.get("total_seconds") or 0.0)
    slow = bool(total and total >= SLOW_STARTUP_SECONDS)
    state = status["state"]
    if not status["reachable"]:
        # "I could not ask" is not "there is nothing to ask about", and "the
        # service answered with an error" is not "the service did not answer".
        if status.get("answered"):
            if status.get("http_status") == 404:
                # It refused this location; "start the service" is wrong advice.
                verdict = "location_refused"
                remedy = LOCATION_REFUSED_REMEDY
            else:
                verdict = "service_error"
                remedy = HTTP_ERROR_REMEDY.format(
                    status=status.get("http_status") or "an error")
        else:
            verdict = "service_unreachable"
            remedy = UNREACHABLE_REMEDY
    else:
        verdict = {
            "connected": "ok",
            "failed": "connect_failed",
            "pending": "stuck_pending",
            "disabled": "not_connected",
        }.get(state)
        if verdict is not None:
            remedy = _REMEDY.get(state, "")
        elif status["configured"]:
            # The entry exists; "add the entry" would contradict what we just read.
            verdict = "unknown_state"
            remedy = UNKNOWN_STATE_REMEDY
        else:
            verdict = "not_configured"
            remedy = _REMEDY[""]
    if slow:
        verdict = f"{verdict} (slow_startup)" if verdict != "ok" else "ok (slow_startup)"
        remedy = (remedy + "; " if remedy else "") + (
            f"last start took {total:.0f}s of the client's 60s deadline — the "
            "import cost is the risk, see `perun mcp-health --selftest`")
    db_error = str(startup.get("db_init_error") or "")
    if db_error:
        remedy = (remedy + "; " if remedy else "") + (
            "the last start could not reconcile the database schema "
            f"({db_error}); a restart retries it")
    return {
        "server": server,
        "directory": directory,
        "state": state,
        "error": status["error"],
        "configured": status["configured"],
        "reachable": status["reachable"],
        "answered": status.get("answered", False),
        "verdict": verdict,
        "remedy": remedy,
        "last_startup": startup,
        "slow_startup": slow,
    }
