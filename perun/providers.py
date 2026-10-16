"""LLM provider discovery and status.

Perun does **not** store provider credentials. OpenCode owns the auth layer;
this module only shells out to ``opencode auth list`` and (optionally) probes
whether a provider can serve models, so the dashboard can show connection
status without duplicating secrets.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from typing import Any

# Cache provider list the same way /api/models does — ``opencode auth list`` is
# fast but can still churn if the dashboard is reloaded repeatedly.
_PROVIDERS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_PROVIDERS_TTL = 30.0
_PROVIDERS_LOCK = threading.Lock()

# OpenCode provider IDs are lowercase alphanumeric with dashes/underscores.
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9_-]+$")


def _opencode_argv() -> list[str]:
    """Return a safe ``opencode`` invocation list."""
    exe = shutil.which("opencode")
    return [exe] if exe else ["opencode"]


def list_providers(*, refresh: bool = False, timeout: float = 5.0) -> dict[str, Any]:
    """Return the providers known to OpenCode auth.

    Results are cached for ``_PROVIDERS_TTL`` seconds unless *refresh* is
    ``True``. Returns ``{ok, providers, count, error}`` where each provider is
    enriched with ``connected`` (has at least one credential) and ``status``
    (``connected`` / ``not_connected``).
    """
    now = time.time()
    with _PROVIDERS_LOCK:
        if (not refresh and _PROVIDERS_CACHE["data"] is not None
                and now - _PROVIDERS_CACHE["ts"] < _PROVIDERS_TTL):
            return _PROVIDERS_CACHE["data"]

    argv = _opencode_argv() + ["auth", "list", "--format", "json"]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"ok": False, "providers": [], "count": 0, "error": str(exc)}

    if proc.returncode != 0:
        return {
            "ok": False,
            "providers": [],
            "count": 0,
            "error": (proc.stderr or "").strip()[:500] or "opencode auth list failed",
        }

    try:
        raw = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        return {"ok": False, "providers": [], "count": 0,
                "error": f"invalid JSON from opencode auth list: {exc}"}

    providers: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        conns = entry.get("connections") or []
        connected = bool(conns) and all(isinstance(c, dict) for c in conns)
        providers.append({
            "id": entry.get("id", ""),
            "name": entry.get("name", ""),
            "connected": connected,
            "status": "connected" if connected else "not_connected",
            "connections": conns,
        })

    data = {"ok": True, "providers": providers, "count": len(providers), "error": None}
    with _PROVIDERS_LOCK:
        _PROVIDERS_CACHE.update(ts=now, data=data)
    return data


def logout_provider(provider_id: str, timeout: float = 5.0) -> dict[str, Any]:
    """Disconnect a provider via ``opencode auth logout``.

    The provider ID is validated against a safe character set before being
    passed to the CLI.
    """
    if not provider_id or not _PROVIDER_ID_RE.match(provider_id):
        return {"ok": False, "error": f"invalid provider id: {provider_id!r}"}
    argv = _opencode_argv() + ["auth", "logout", provider_id]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or "").strip()[:500] or "opencode auth logout failed",
        }
    # Evict the cached list so the next dashboard/providers view is fresh.
    with _PROVIDERS_LOCK:
        _PROVIDERS_CACHE["data"] = None
    return {"ok": True, "error": None}
