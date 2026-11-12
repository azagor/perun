"""VS Code companion bridge: tickets, WebSocket clients, edit and terminal events.

This is the Perun side of the VS Code integration. It deliberately reuses the
existing FastAPI server instead of running a second daemon:

* a short-lived, single-use ticket authenticates the WebSocket (same pattern as
  PerunChat; a long-lived token never appears in a URL);
* the extension registers the workspace it has open, so a manually opened
  workspace is discovered too;
* OpenCode's global event stream is the source for "the agent edited this file";
* terminal open/focus/close requests are routed to the VS Code window that owns
  the workspace.

The bridge never executes editor-supplied commands. It only sends a fixed set of
message types and validates every path against the registered project root.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from . import config, opencode_api, process_tracker, registry, vscode_events, vscode_workspace

LOG = logging.getLogger("perun.vscode.bridge")

PROTOCOL_VERSION = 1
TICKET_TTL_SECONDS = 30.0
RECENT_EDITS = 20
DEBOUNCE_SECONDS = 0.3


class VSCodeBridge:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tickets: Dict[str, Dict[str, Any]] = {}
        self._embed_tickets: Dict[str, float] = {}
        self._clients: Dict[str, Dict[str, Any]] = {}
        self._recent: Dict[int, Deque[Dict[str, Any]]] = {}
        self._context: Dict[int, Dict[str, Any]] = {}
        self._terminals: Dict[str, Dict[str, Any]] = {}
        self._debounce: Dict[tuple[int, str], float] = {}
        self._hub: Optional[opencode_api.EventHub] = None
        self._hub_token: Optional[str] = None
        self._started = False

    # ---- lifecycle ------------------------------------------------------- #
    def start(self) -> None:
        """Subscribe to OpenCode's global event stream once per process."""
        with self._lock:
            if self._started:
                return
            self._started = True
        try:
            hub = opencode_api.default_hub()
            if self._hub_token is None:
                self._hub_token = hub.subscribe(hub.GLOBAL_SESSION, self._on_event)
            hub.start()
            self._hub = hub
            LOG.info("vscode bridge listening for agent edits")
        except Exception:  # pragma: no cover - the service may be down
            LOG.debug("could not subscribe the vscode bridge", exc_info=True)

    def stop(self) -> None:
        hub, token = self._hub, self._hub_token
        self._hub_token = None
        if hub and token:
            try:
                hub.unsubscribe(token)
            except Exception:  # pragma: no cover - shutdown is best effort
                pass
        with self._lock:
            self._started = False

    # ---- tickets --------------------------------------------------------- #
    def issue_ticket(self, project_id: int, workspace: str,
                     *, ttl: float = TICKET_TTL_SECONDS) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            for key, value in list(self._tickets.items()):
                if float(value.get("expires") or 0) <= now:
                    self._tickets.pop(key, None)
            ticket = uuid.uuid4().hex
            self._tickets[ticket] = {
                "project_id": int(project_id),
                "workspace": str(workspace or ""),
                "expires": now + max(5.0, float(ttl)),
            }
        return {"ticket": ticket, "expires_in": int(max(5.0, float(ttl)))}

    def redeem_ticket(self, ticket: str) -> Optional[Dict[str, Any]]:
        value = str(ticket or "").strip()
        if not value:
            return None
        with self._lock:
            info = self._tickets.pop(value, None)
        if not info:
            return None
        if float(info.get("expires") or 0) <= time.time():
            return None
        return info

    def issue_embed_ticket(self, *, ttl: float = TICKET_TTL_SECONDS) -> Dict[str, Any]:
        """A one-shot ticket that lets the VS Code webview frame the dashboard.

        The dashboard sends ``X-Frame-Options: DENY`` for every normal page. The
        embed ticket is the only way to relax framing, and it is single-use and
        short-lived; the iframe's own API calls are same-origin and still use the
        loopback token.
        """
        now = time.time()
        with self._lock:
            for key, expires in list(self._embed_tickets.items()):
                if expires <= now:
                    self._embed_tickets.pop(key, None)
            ticket = uuid.uuid4().hex
            self._embed_tickets[ticket] = now + max(5.0, float(ttl))
        return {"ticket": ticket, "expires_in": int(max(5.0, float(ttl)))}

    def redeem_embed_ticket(self, ticket: str) -> bool:
        """Whether *ticket* is a live embed permission (reusable until it expires).

        The webview may request the dashboard more than once (a reload or a
        prefetch); a strictly single-use ticket produced an empty panel. The
        ticket is still short-lived and only issued with the loopback API token.
        """
        value = str(ticket or "").strip()
        if not value:
            return False
        with self._lock:
            expires = float(self._embed_tickets.get(value) or 0.0)
            if expires <= time.time():
                self._embed_tickets.pop(value, None)
                return False
            return True

    # ---- connections ----------------------------------------------------- #
    def register(self, conn_id: str, websocket: Any, loop: Any, outbox: Any,
                 ticket: Dict[str, Any]) -> None:
        with self._lock:
            self._clients[conn_id] = {
                "websocket": websocket,
                "loop": loop,
                "outbox": outbox,
                "project_id": int(ticket["project_id"]),
                "workspace": str(ticket.get("workspace") or ""),
                "window_id": "",
                "protocol": 0,
                "extension_version": "",
                "connected_at": time.time(),
            }

    def unregister(self, conn_id: str) -> None:
        with self._lock:
            self._clients.pop(conn_id, None)

    def client_count(self, project_id: Optional[int] = None) -> int:
        with self._lock:
            if project_id is None:
                return len(self._clients)
            return sum(1 for c in self._clients.values()
                       if int(c.get("project_id") or 0) == int(project_id))

    def _send(self, client: Dict[str, Any], payload: Dict[str, Any]) -> None:
        loop = client.get("loop")
        outbox = client.get("outbox")
        if not loop or not outbox:
            return
        try:
            if loop.is_closed():
                return
            loop.call_soon_threadsafe(outbox.put_nowait, payload)
        except Exception:  # pragma: no cover - a closing loop is normal
            LOG.debug("could not queue a VS Code message", exc_info=True)

    def send_to_project(self, project_id: int, payload: Dict[str, Any]) -> int:
        with self._lock:
            clients = [c for c in self._clients.values()
                       if int(c.get("project_id") or 0) == int(project_id)]
        for client in clients:
            self._send(client, payload)
        return len(clients)

    def _first_client(self, project_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            clients = [c for c in self._clients.values()
                       if int(c.get("project_id") or 0) == int(project_id)]
            if not clients:
                return None
            return max(clients, key=lambda c: float(c.get("connected_at") or 0))

    def broadcast(self, project_id: int, payload: Dict[str, Any]) -> int:
        return self.send_to_project(project_id, payload)

    # ---- inbound messages ------------------------------------------------ #
    def handle_message(self, conn_id: str, message: Dict[str, Any]) -> None:
        with self._lock:
            client = self._clients.get(conn_id)
        if not client or not isinstance(message, dict):
            return
        kind = str(message.get("type") or "")
        if kind in ("hello", "workspace.register"):
            self._hello(client, message)
        elif kind == "selection.shared":
            self._selection(client, message)
        elif kind == "terminal.opened":
            self._terminal_opened(client, message)
        elif kind == "terminal.closed":
            self._terminal_closed(client, message)
        elif kind == "ping":
            self._send(client, {"type": "pong", "ts": int(time.time())})
        else:
            self._send(client, {"type": "error", "message": "unknown message type"})

    def _hello(self, client: Dict[str, Any], message: Dict[str, Any]) -> None:
        try:
            protocol = int(message.get("protocol") or 0)
        except (TypeError, ValueError):
            protocol = 0
        if protocol != PROTOCOL_VERSION:
            self._send(client, {
                "type": "error",
                "message": (f"protocol mismatch: companion speaks {protocol or 'unknown'}, "
                            f"Perun expects {PROTOCOL_VERSION}; update Perun or the extension"),
            })
            return
        workspace = str(message.get("workspace_path") or client.get("workspace") or "")
        try:
            expected = Path(str(client.get("workspace") or "")).resolve()
            actual = Path(workspace).resolve()
        except (OSError, RuntimeError):
            self._send(client, {"type": "error", "message": "workspace path is invalid"})
            return
        if expected != actual:
            # The ticket is bound to one workspace; never let a connection
            # register a different project.
            self._send(client, {"type": "error", "message": "workspace does not match the ticket"})
            return
        project = registry.get_project(int(client.get("project_id") or 0))
        if not project:
            self._send(client, {"type": "error", "message": "project is no longer registered"})
            return
        with self._lock:
            client["workspace"] = workspace or str(project.get("path") or "")
            client["window_id"] = str(message.get("window_id") or "")
            client["protocol"] = int(message.get("protocol") or 0)
            client["extension_version"] = str(message.get("extension_version") or "")
            recent = list(self._recent.get(int(project["id"]), ()))
            terminals = [dict(t) for t in self._terminals.values()
                         if int(t.get("project_id") or 0) == int(project["id"])]
        self._write_heartbeat(client)
        active = self._active_tui_run(project["id"])
        self._send(client, {
            "type": "hello.ok",
            "protocol": PROTOCOL_VERSION,
            "project": {
                "slug": project.get("slug"),
                "name": project.get("name"),
                "path": project.get("path"),
                "ide": project.get("ide") or {},
            },
            "linked": bool((project.get("ide") or {}).get("vscode_linked")),
            "active_run": active,
            "recent_edits": recent[-RECENT_EDITS:],
            "terminals": terminals,
        })

    def _selection(self, client: Dict[str, Any], message: Dict[str, Any]) -> None:
        project = registry.get_project(int(client.get("project_id") or 0))
        if not project:
            self._send(client, {"type": "error", "message": "project is no longer registered"})
            return
        selection = vscode_events.validate_selection(message, str(project.get("path") or ""))
        if not selection:
            self._send(client, {"type": "error", "message": "selection path is outside the project"})
            return
        with self._lock:
            self._context[int(project["id"])] = selection
            terminal = self._terminal_for_project(int(project["id"]))
        mode = str((project.get("ide") or {}).get("launch_mode") or "external-terminal")
        action = "insert" if mode == "vscode-terminal" and terminal else "copy"
        if action == "insert" and self._session_blocked(project):
            # A permission/form owns the TUI input; typing into it would land in
            # the wrong place. Fall back to the clipboard instead.
            action = "copy"
        try:
            registry.log_activity(
                int(project["id"]), "vscode_selection",
                f"Selection shared from VS Code ({selection['path']}:{selection['start_line']})",
                {"path": selection["path"], "start": selection["start_line"],
                 "end": selection["end_line"], "action": action},
            )
        except Exception:  # pragma: no cover - activity is best effort
            pass
        self._send(client, {
            "type": "selection.ok",
            "request_id": str(message.get("request_id") or ""),
            "action": action,
            "ref": selection["ref"],
            "text": selection["text"] if (selection["dirty"] or action == "copy") else "",
            "dirty": selection["dirty"],
        })

    @staticmethod
    def _session_blocked(project: Dict[str, Any]) -> bool:
        try:
            api = opencode_api.default_api()
            location = str(project.get("path") or "")
            if api.pending_permissions(location):
                return True
            if api.pending_forms(location):
                return True
        except Exception:
            # If the service cannot answer, prefer the safe clipboard path.
            return True
        return False

    def _terminal_opened(self, client: Dict[str, Any], message: Dict[str, Any]) -> None:
        run_uid = str(message.get("run_uid") or "").strip()
        terminal_id = str(message.get("terminal_id") or "").strip()
        run = registry.get_run(run_uid) if run_uid else None
        if not run or int(run.get("project_id") or 0) != int(client.get("project_id") or 0):
            self._send(client, {"type": "error", "message": "unknown run"})
            return
        try:
            pid = int(message.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        with self._lock:
            self._terminals[run_uid] = {
                "run_uid": run_uid,
                "project_id": int(run["project_id"]),
                "terminal_id": terminal_id,
                "window_id": str(client.get("window_id") or ""),
                "pid": pid,
            }
        registry.update_run(run_uid, terminal_id=terminal_id, terminal_owner="vscode")
        if pid:
            process_tracker.TRACKER.register_pid(run_uid, pid)
        try:
            registry.log_activity(
                int(run["project_id"]), "vscode_terminal",
                f"VS Code terminal opened for run {run_uid}",
                {"run_uid": run_uid, "terminal_id": terminal_id, "pid": pid},
            )
        except Exception:  # pragma: no cover
            pass

    def _terminal_closed(self, client: Dict[str, Any], message: Dict[str, Any]) -> None:
        terminal_id = str(message.get("terminal_id") or "")
        run_uid = str(message.get("run_uid") or "")
        with self._lock:
            if not run_uid:
                if terminal_id:
                    for uid, terminal in list(self._terminals.items()):
                        if terminal.get("terminal_id") == terminal_id:
                            run_uid = uid
                            break
            terminal = self._terminals.pop(run_uid, None)
        if not terminal or int(terminal.get("project_id") or 0) != int(client.get("project_id") or 0):
            return
        run = registry.get_run(run_uid)
        if run and run.get("status") in ("queued", "running"):
            registry.update_run(run_uid, status="done", ended_at=registry.now_iso())
        try:
            process_tracker.TRACKER.finalize(run_uid)
        except Exception:  # pragma: no cover
            pass

    def _write_heartbeat(self, client: Dict[str, Any]) -> None:
        path = vscode_workspace.heartbeat_path()
        payload = {
            "version": str(client.get("extension_version") or "unknown"),
            "protocol": int(client.get("protocol") or 0),
            "last_seen": registry.now_iso(),
            "window_id": str(client.get("window_id") or ""),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    # ---- outbound terminal commands -------------------------------------- #
    def request_terminal(self, run: Dict[str, Any], project: Dict[str, Any]) -> bool:
        payload = {
            "type": "terminal.open",
            "run_uid": str(run.get("run_uid") or ""),
            "project_slug": project.get("slug"),
            "cwd": project.get("path"),
            "session_id": str(run.get("opencode_session_id") or ""),
            "model": str(run.get("model") or ""),
            "agent": str(run.get("agent") or ""),
        }
        client = self._first_client(int(project["id"]))
        if not client:
            return False
        self._send(client, payload)
        return True

    def focus_terminal(self, run_uid: str) -> bool:
        with self._lock:
            terminal = dict(self._terminals.get(str(run_uid)) or {})
        if not terminal:
            return False
        return self.send_to_project(
            int(terminal["project_id"]),
            {"type": "terminal.focus", "terminal_id": terminal.get("terminal_id"),
             "run_uid": str(run_uid)},
        ) > 0

    def close_terminal(self, run_uid: str) -> bool:
        with self._lock:
            terminal = dict(self._terminals.get(str(run_uid)) or {})
        if not terminal:
            return False
        return self.send_to_project(
            int(terminal["project_id"]),
            {"type": "terminal.close", "terminal_id": terminal.get("terminal_id"),
             "run_uid": str(run_uid)},
        ) > 0

    # ---- edit stream ----------------------------------------------------- #
    def _on_event(self, event: Dict[str, Any]) -> None:
        try:
            if not vscode_events.is_edit_event(event):
                return
            project = None
            sid = vscode_events.session_id(event)
            if sid:
                project = registry.project_for_opencode_session(sid)
            if not project:
                location = vscode_events.location(event)
                if location:
                    from . import attention  # local import: avoid a module cycle

                    project = attention.project_for_location(location)
            if not project:
                return
            project_id = int(project["id"])
            ide = project.get("ide") or {}
            if ide.get("auto_follow_edits") is False:
                return
            edit = vscode_events.extract_edit(event, str(project.get("path") or ""))
            if not edit:
                return
            key = (project_id, edit["path"])
            now = time.time()
            with self._lock:
                if now - self._debounce.get(key, 0.0) < DEBOUNCE_SECONDS:
                    return
                self._debounce[key] = now
                recent = self._recent.setdefault(project_id, deque(maxlen=RECENT_EDITS))
                recent.append(edit)
            if not self.client_count(project_id):
                if ide.get("vscode_linked"):
                    # No companion installed/connected: the fixed `code --goto`
                    # path still gives live reveal without any extension.
                    target = Path(str(project.get("path") or "")) / edit["path"]
                    vscode_workspace.reveal(str(target), int(edit.get("start_line") or 1))
                return
            self.broadcast(project_id, {"type": "edit.reveal", **edit})
        except Exception:  # pragma: no cover - one bad event must not kill the hub
            LOG.debug("could not process a vscode edit event", exc_info=True)

    def _active_tui_run(self, project_id: int) -> Optional[Dict[str, Any]]:
        try:
            for run in registry.list_active_runs():
                if (int(run.get("project_id") or 0) == int(project_id)
                        and run.get("standalone")):
                    return {k: run.get(k) for k in
                            ("run_uid", "project_slug", "opencode_session_id",
                             "model", "agent", "started_at")}
        except Exception:  # pragma: no cover
            pass
        return None

    def _terminal_for_project(self, project_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            for terminal in self._terminals.values():
                if int(terminal.get("project_id") or 0) == int(project_id):
                    return dict(terminal)
        return None

    def context_for(self, project_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            value = self._context.get(int(project_id))
            return dict(value) if value else None


#: Process-wide singleton, imported by ``server.py``.
BRIDGE = VSCodeBridge()


__all__ = ["BRIDGE", "PROTOCOL_VERSION", "VSCodeBridge"]
