"""Minimal client for the local OpenCode HTTP API (v2).

Perun already drives ``opencode run`` as a child process.  OpenCode v2 also
exposes an HTTP API on the background service, which is what lets Perun answer
a permission prompt, fill a form, or deliver a message into a *running* TUI
session.  This module is the only place that knows how to talk to it.

Two facts shape the implementation, both verified against opencode 2.2.0:

* The service requires HTTP Basic auth ``opencode:<password>``; the password
  lives in ``~/.config/opencode/service.json``.  It is read on demand and kept
  in memory only — never logged, persisted, or copied into Perun's database.
* Pending permission/form lists are *location-scoped*; the location is passed
  as ``?location[directory]=<path>``.  Session routes (prompt/inbox/permission
  reply) take no location and resolve from the session record, so a session in
  any project is addressable from one process.

The API is marked experimental upstream.  Every failure therefore degrades to
:class:`OpenCodeUnavailable` so the caller can stay quiet instead of crashing.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

LOG = logging.getLogger("perun.opencode")

SERVICE_FILE = Path.home() / ".config" / "opencode" / "service.json"
DISCOVERY_TIMEOUT = 10.0


class OpenCodeError(RuntimeError):
    """Base class for OpenCode transport failures."""


class OpenCodeUnavailable(OpenCodeError):
    """The service or its credential is not reachable on this machine."""


class OpenCodeHTTPError(OpenCodeError):
    """The service answered with an error status."""

    def __init__(self, status: int, detail: str, path: str):
        super().__init__(f"HTTP {status} for {path}: {detail}")
        self.status = status
        self.detail = detail
        self.path = path


def _service_password() -> str:
    """Read the local API password, or return ``""``.

    ``OPENCODE_SERVICE_PASSWORD`` overrides the file, which keeps tests and
    unusual installs possible without touching the user's config.
    """
    env = (os.environ.get("OPENCODE_SERVICE_PASSWORD") or "").strip()
    if env:
        return env
    try:
        data = json.loads(SERVICE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("password") or "").strip()


def _opencode_argv(*args: str) -> list[str]:
    """Resolve ``opencode`` the same way the orchestrator does."""
    path = shutil.which("opencode")
    if path is None:
        return []
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", path, *args]
    return [path, *args]


def discover_base_url(timeout: float = DISCOVERY_TIMEOUT) -> str:
    """Ask the ``opencode`` CLI where its background service listens.

    Discovery is deliberately delegated to the CLI: it already knows how the
    service is registered on every platform, and re-implementing that lookup
    (port files, named pipes, per-OS paths) would be fragile.  This runs once
    per process (and again after a failed request), not on every poll.
    """
    argv = _opencode_argv("api", "get", "/api/info")
    if not argv:
        return ""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # pragma: no cover - depends on the host
        LOG.debug("opencode service discovery failed: %s", exc)
        return ""
    if proc.returncode != 0:
        LOG.debug("opencode api exited %s: %s", proc.returncode, (proc.stderr or "")[:300])
        return ""
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError:
        return ""
    urls = payload.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    for url in urls:
        url = str(url).strip().rstrip("/")
        if url.startswith("http://") or url.startswith("https://"):
            return url
    return ""


def _auth_header(password: str) -> str:
    token = base64.b64encode(f"opencode:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


class OpenCodeAPI:
    """Thin, typed wrapper over the endpoints Perun needs."""

    def __init__(
        self,
        *,
        base_url: str = "",
        password: str = "",
        timeout: float = 5.0,
        discovery=discover_base_url,
    ):
        self._base = (base_url or "").rstrip("/")
        self._password = password or ""
        self._timeout = timeout
        self._discovery = discovery
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=3.0))

    # ---- plumbing ---------------------------------------------------- #
    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def _ensure_ready(self) -> None:
        if not self._base:
            self._base = (self._discovery() or "").rstrip("/")
        if not self._password:
            self._password = _service_password()
        if not self._base:
            raise OpenCodeUnavailable("opencode service URL not found")
        if not self._password:
            raise OpenCodeUnavailable("opencode service credential not found")

    def _url(self, path: str, location: str = "") -> str:
        url = self._base + path
        if location:
            sep = "&" if "?" in url else "?"
            quoted = urllib.parse.quote(str(location), safe="")
            url = f"{url}{sep}location%5Bdirectory%5D={quoted}"
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        location: str = "",
        allow_404: bool = False,
    ) -> Any:
        self._ensure_ready()
        headers = {"Authorization": _auth_header(self._password)}
        try:
            response = self._client.request(
                method, self._url(path, location), json=body, headers=headers
            )
        except Exception as exc:
            # The service may have restarted on a new port: rediscover once.
            self._base = ""
            try:
                self._ensure_ready()
                response = self._client.request(
                    method, self._url(path, location), json=body, headers=headers
                )
            except Exception:
                raise OpenCodeUnavailable(f"opencode service unreachable: {exc}") from exc
        if response.status_code == 401:
            # A rotated credential: reload it once, then give up.
            self._password = _service_password()
            if self._password:
                headers = {"Authorization": _auth_header(self._password)}
                response = self._client.request(
                    method, self._url(path, location), json=body, headers=headers
                )
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 400:
            detail = (response.text or "")[:300]
            raise OpenCodeHTTPError(response.status_code, detail, path)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # ---- read --------------------------------------------------------- #
    def info(self) -> Dict[str, Any]:
        return self._request("GET", "/api/info") or {}

    def active_sessions(self) -> Dict[str, Any]:
        payload = self._request("GET", "/api/session/active") or {}
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}

    def locations(self) -> List[str]:
        payload = self._request("GET", "/api/debug/location") or []
        entries = payload.get("data") if isinstance(payload, dict) else payload
        out: List[str] = []
        for entry in entries or []:
            if isinstance(entry, dict):
                directory = str(entry.get("directory") or "").strip()
                if directory:
                    out.append(directory)
            elif isinstance(entry, str) and entry.strip():
                out.append(entry.strip())
        return out

    def session(self, session_id: str) -> Optional[Dict[str, Any]]:
        payload = self._request(
            "GET", f"/api/session/{session_id}", allow_404=True
        )
        return (payload or {}).get("data") if isinstance(payload, dict) else None

    def pending_permissions(self, location: str) -> List[Dict[str, Any]]:
        payload = self._request(
            "GET", "/api/permission/request", location=location, allow_404=True
        )
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        return [r for r in (data or []) if isinstance(r, dict)]

    def pending_forms(self, location: str) -> List[Dict[str, Any]]:
        payload = self._request("GET", "/api/form", location=location, allow_404=True)
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        return [r for r in (data or []) if isinstance(r, dict)]

    # ---- write -------------------------------------------------------- #
    def reply_permission(
        self, session_id: str, request_id: str, decision: str, message: str = ""
    ) -> None:
        body: Dict[str, Any] = {"decision": decision}
        if message:
            body["message"] = message
        self._request(
            "POST",
            f"/api/session/{session_id}/permission/{request_id}/reply",
            body=body,
        )

    def reply_form(self, session_id: str, form_id: str, answer: Dict[str, Any]) -> None:
        self._request(
            "POST",
            f"/api/session/{session_id}/form/{form_id}/reply",
            body={"answer": answer},
        )

    def prompt(
        self,
        session_id: str,
        text: str,
        *,
        delivery: str = "queue",
        message_id: str = "",
        resume: bool = True,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "text": text,
            "delivery": delivery if delivery in ("queue", "steer") else "queue",
            "resume": bool(resume),
        }
        if message_id:
            body["id"] = message_id
        payload = self._request(
            "POST", f"/api/session/{session_id}/prompt", body=body
        )
        data = (payload or {}).get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}

    def set_inbox_delivery(
        self, session_id: str, inbox_id: str, delivery: str
    ) -> None:
        self._request(
            "PATCH",
            f"/api/session/{session_id}/inbox/{inbox_id}",
            body={"delivery": delivery},
        )

    def cancel_inbox(self, session_id: str, inbox_id: str) -> None:
        self._request(
            "DELETE", f"/api/session/{session_id}/inbox/{inbox_id}", allow_404=True
        )

    def interrupt(self, session_id: str) -> None:
        self._request("POST", f"/api/session/{session_id}/interrupt", allow_404=True)


_DEFAULT_API: Optional[OpenCodeAPI] = None
_DEFAULT_LOCK = threading.Lock()


def default_api() -> OpenCodeAPI:
    """Return the process-wide client (lazily created)."""
    global _DEFAULT_API
    with _DEFAULT_LOCK:
        if _DEFAULT_API is None:
            _DEFAULT_API = OpenCodeAPI()
        return _DEFAULT_API


def set_default_api(api: Optional[OpenCodeAPI]) -> None:
    """Swap the process-wide client (tests, or a configured instance)."""
    global _DEFAULT_API
    with _DEFAULT_LOCK:
        if _DEFAULT_API is not None and _DEFAULT_API is not api:
            closer = getattr(_DEFAULT_API, "close", None)
            if callable(closer):
                closer()
        _DEFAULT_API = api


def ping(api: Optional[OpenCodeAPI] = None, *, timeout: float = 3.0) -> bool:
    """Cheap health probe used by the watcher before polling."""
    client = api or default_api()
    try:
        started = time.perf_counter()
        client.info()
        return (time.perf_counter() - started) < max(timeout, 1.0)
    except OpenCodeError:
        return False
