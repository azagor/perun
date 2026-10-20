"""LLM provider discovery, status and configuration.

Perun does **not** store provider credentials long-term. It edits OpenCode's
own configuration (``opencode.json[c]``) so the OpenCode CLI/server remain the
source of truth for credentials and model availability.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from . import config, opencode_config

# Cache provider list the same way /api/models does — ``opencode auth list`` is
# fast but can still churn if the dashboard is reloaded repeatedly.
_PROVIDERS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_PROVIDERS_TTL = 30.0
_PROVIDERS_LOCK = threading.Lock()

# OpenCode provider IDs are lowercase alphanumeric with dashes/underscores.
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9_-]+$")

# Built-in and well-known providers. ``kind`` drives the dashboard form:
#   * api_key   — one or more API keys, optionally a custom base URL.
#   * local     — an OpenAI-compatible local endpoint (Ollama, LM Studio, ...).
#   * oauth     — must use ``opencode auth login`` in a terminal.
#   * custom    — free-form provider added by the user.
#
# ``package`` is only set for providers that are not built into OpenCode and
# must use the OpenAI-compatible runtime. ``default_base_url`` is used for
# local providers and for OpenAI-compatible presets where the endpoint is
# known. IDs are taken from the OpenCode/models.dev catalog.
_PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "opencode": {
        "name": "OpenCode Console",
        "kind": "api_key",
        "docs": "https://opencode.ai/v2/docs/console/",
    },
    "opencode-go": {
        "name": "OpenCode Go",
        "kind": "api_key",
        "docs": "https://opencode.ai/v2/docs/console/go/",
    },
    "ollama": {
        "name": "Ollama (local)",
        "kind": "local",
        "default_base_url": "http://127.0.0.1:11434/v1",
        "docs": "https://docs.ollama.com/integrations/opencode",
    },
    "ollama-cloud": {
        "name": "Ollama Cloud",
        "kind": "api_key",
        "default_base_url": "https://ollama.com/v1",
        "docs": "https://ollama.com/",
    },
    "openai": {
        "name": "OpenAI",
        "kind": "api_key",
        "docs": "https://platform.openai.com/",
    },
    "anthropic": {
        "name": "Anthropic",
        "kind": "api_key",
        "docs": "https://docs.anthropic.com/",
    },
    "deepseek": {
        "name": "DeepSeek",
        "kind": "api_key",
        "docs": "https://platform.deepseek.com/",
    },
    "moonshotai": {
        "name": "Moonshot AI",
        "kind": "api_key",
        "docs": "https://platform.moonshot.ai/",
    },
    "cohere": {
        "name": "Cohere",
        "kind": "api_key",
        "docs": "https://cohere.com/",
    },
    "mistral": {
        "name": "Mistral",
        "kind": "api_key",
        "docs": "https://docs.mistral.ai/",
    },
    "xai": {
        "name": "xAI",
        "kind": "api_key",
        "docs": "https://console.x.ai/",
    },
    "openrouter": {
        "name": "OpenRouter",
        "kind": "api_key",
        "docs": "https://openrouter.ai/",
    },
    "nvidia": {
        "name": "NVIDIA",
        "kind": "api_key",
        "docs": "https://build.nvidia.com/",
    },
    "perplexity": {
        "name": "Perplexity",
        "kind": "api_key",
        "docs": "https://docs.perplexity.ai/",
    },
    # OpenAI-compatible providers that are not built into OpenCode.
    "groq": {
        "name": "Groq (OpenAI-compatible)",
        "kind": "api_key",
        "package": "@opencode/ai/providers/openai-compatible",
        "default_base_url": "https://api.groq.com/openai/v1",
        "docs": "https://console.groq.com/",
    },
    "cerebras": {
        "name": "Cerebras (OpenAI-compatible)",
        "kind": "api_key",
        "package": "@opencode/ai/providers/openai-compatible",
        "default_base_url": "https://api.cerebras.ai/v1",
        "docs": "https://inference.cerebras.ai/",
    },
    "togetherai": {
        "name": "Together AI (OpenAI-compatible)",
        "kind": "api_key",
        "package": "@opencode/ai/providers/openai-compatible",
        "default_base_url": "https://api.together.xyz/v1",
        "docs": "https://www.together.ai/",
    },
    "fireworks-ai": {
        "name": "Fireworks AI (OpenAI-compatible)",
        "kind": "api_key",
        "package": "@opencode/ai/providers/openai-compatible",
        "default_base_url": "https://api.fireworks.ai/inference/v1",
        "docs": "https://fireworks.ai/",
    },
    "github-copilot": {
        "name": "GitHub Copilot",
        "kind": "oauth",
        "docs": "https://github.com/features/copilot/",
    },
    "gitlab": {
        "name": "GitLab Duo",
        "kind": "oauth",
        "docs": "https://docs.gitlab.com/user/duo_agent_platform/",
    },
    "lmstudio": {
        "name": "LM Studio (local)",
        "kind": "local",
        "default_base_url": "http://127.0.0.1:1234/v1",
        "docs": "https://lmstudio.ai/",
    },
    "vllm": {
        "name": "vLLM (local)",
        "kind": "local",
        "default_base_url": "http://127.0.0.1:8000/v1",
        "docs": "https://docs.vllm.ai/",
    },
}


def _opencode_argv() -> list[str]:
    """Return a safe ``opencode`` invocation list."""
    exe = shutil.which("opencode")
    return [exe] if exe else ["opencode"]


def _provider_status(*, auth_connected: bool, configured: bool, has_key: bool) -> str:
    if auth_connected and configured:
        return "connected & configured"
    if auth_connected:
        return "connected"
    if configured:
        return "configured" if has_key else "configured (no key)"
    return "not_connected"


def _auth_providers(timeout: float = 5.0) -> dict[str, Any]:
    """Return the raw output of ``opencode auth list`` as a status dict."""
    argv = _opencode_argv() + ["auth", "list", "--format", "json"]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
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
        return {
            "ok": False,
            "providers": [],
            "count": 0,
            "error": f"invalid JSON from opencode auth list: {exc}",
        }

    providers: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        conns = entry.get("connections") or []
        connected = bool(conns) and all(isinstance(c, dict) for c in conns)
        providers.append({
            "id": entry.get("id", ""),
            "name": entry.get("name", ""),
            "auth_connected": connected,
            "connected": connected,
            "status": "connected" if connected else "not_connected",
            "connections": conns,
        })
    return {"ok": True, "providers": providers, "count": len(providers), "error": None}


def list_providers(*, refresh: bool = False, timeout: float = 5.0) -> dict[str, Any]:
    """Return providers known to OpenCode, enriched with config/catalog state.

    The returned ``providers`` list contains every provider that is currently
    saved in OpenCode auth **or** configured in ``opencode.json[c]``. The
    separate ``catalog`` list contains well-known providers that are not yet
    present, so the dashboard can offer an "Add" flow.
    """
    now = time.time()
    with _PROVIDERS_LOCK:
        if (not refresh and _PROVIDERS_CACHE["data"] is not None
                and now - _PROVIDERS_CACHE["ts"] < _PROVIDERS_TTL):
            return _PROVIDERS_CACHE["data"]

    auth = _auth_providers(timeout=timeout)
    try:
        configured, config_had_comments = opencode_config.configured_providers()
    except Exception as exc:
        # A corrupt or unreadable opencode.json should not hide the auth list.
        auth["error"] = (auth.get("error") or "") + f"; cannot read opencode config: {exc}"
        configured = []
        config_had_comments = False

    active_by_id: dict[str, dict[str, Any]] = {}

    # Start with configured providers so user-written entries are visible even
    # when ``opencode auth list`` fails or the provider does not use auth.
    for cp in configured:
        pid = cp["id"]
        active_by_id[pid] = {
            "id": pid,
            "name": cp.get("name", pid),
            "auth_connected": False,
            "configured": True,
            "has_key": cp.get("has_key", False),
            "base_url": cp.get("base_url", ""),
            "source": cp.get("source", ""),
            "kind": _PROVIDER_CATALOG.get(pid, {}).get("kind", "custom"),
        }

    # Merge auth state on top.
    for ap in auth.get("providers") or []:
        pid = ap["id"]
        if pid in active_by_id:
            active_by_id[pid]["auth_connected"] = ap.get("auth_connected", False)
            active_by_id[pid]["connections"] = ap.get("connections", [])
        else:
            meta = _PROVIDER_CATALOG.get(pid, {})
            active_by_id[pid] = {
                "id": pid,
                "name": ap.get("name", meta.get("name", pid)),
                "auth_connected": ap.get("auth_connected", False),
                "configured": False,
                "has_key": False,
                "base_url": "",
                "source": "",
                "kind": meta.get("kind", "api_key"),
                "connections": ap.get("connections", []),
            }

    active: list[dict[str, Any]] = []
    for p in active_by_id.values():
        auth_conn = p.get("auth_connected", False)
        configured = p.get("configured", False)
        has_key = p.get("has_key", False)
        p["connected"] = auth_conn or configured
        p["status"] = _provider_status(
            auth_connected=auth_conn,
            configured=configured,
            has_key=has_key,
        )
        active.append(p)
    active.sort(key=lambda x: (x["name"] or x["id"]).lower())

    # Catalog = well-known providers that are not already active.
    active_ids = {p["id"] for p in active}
    catalog = sorted(
        [
            {
                "id": pid,
                "name": meta["name"],
                "kind": meta["kind"],
                "default_base_url": meta.get("default_base_url", ""),
                "docs": meta.get("docs", ""),
            }
            for pid, meta in _PROVIDER_CATALOG.items()
            if pid not in active_ids
        ],
        key=lambda x: x["name"].lower(),
    )

    data: dict[str, Any] = {
        "ok": auth.get("ok", True),
        "providers": active,
        "count": len(active),
        "catalog": catalog,
        "config_file": str(opencode_config._config_file()),
        "config_had_comments": config_had_comments,
        "error": auth.get("error"),
    }
    with _PROVIDERS_LOCK:
        _PROVIDERS_CACHE.update(ts=now, data=data)
    return data


def logout_provider(provider_id: str, timeout: float = 5.0) -> dict[str, Any]:
    """Disconnect a provider via ``opencode auth logout``."""
    if not provider_id or not _PROVIDER_ID_RE.match(provider_id):
        return {"ok": False, "error": f"invalid provider id: {provider_id!r}"}
    argv = _opencode_argv() + ["auth", "logout", provider_id]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
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


def connect_provider(provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Write a provider into the OpenCode config.

    ``payload`` is expected to contain the fields the dashboard form collected:
    ``api_key``, ``base_url``, ``models`` (list or comma-separated string) and,
    for custom providers, ``name`` and ``package``.
    """
    if not provider_id or not _PROVIDER_ID_RE.match(provider_id):
        return {"ok": False, "error": f"invalid provider id: {provider_id!r}"}

    meta = _PROVIDER_CATALOG.get(provider_id, {})
    fields: dict[str, Any] = {}

    if meta.get("name"):
        fields["name"] = meta["name"]

    name = str(payload.get("name") or "").strip()
    if name:
        fields["name"] = name

    package = str(payload.get("package") or "").strip()
    if package:
        fields["package"] = package
    elif meta.get("package"):
        # Catalog entry for a non-built-in, OpenAI-compatible provider.
        fields["package"] = meta["package"]
    elif not meta:
        # Unknown / custom provider without an explicit package: use the
        # OpenAI-compatible runtime, which is the right default for most
        # self-hosted and proxy endpoints.
        fields["package"] = "@opencode/ai/providers/openai-compatible"

    api_key = str(payload.get("api_key") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()

    if meta.get("kind") == "local" and not base_url:
        base_url = meta.get("default_base_url", "")
    elif meta.get("default_base_url") and not base_url:
        # OpenAI-compatible presets have a known endpoint.
        base_url = meta["default_base_url"]

    if api_key:
        fields["apiKey"] = api_key
    if base_url:
        fields["baseURL"] = base_url

    models = payload.get("models")
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    if isinstance(models, list):
        fields["models"] = models

    try:
        result = opencode_config.configure_provider(provider_id, fields)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    # Keep Perun's Ollama host setting in sync with the OpenCode provider URL.
    if provider_id == "ollama" and base_url:
        host = base_url.removesuffix("/v1").rstrip("/")
        if host:
            settings = config.Settings.load()
            if settings.ollama_host != host:
                settings.ollama_host = host
                settings.save()

    # Evict the cached list so the next view reflects the new entry.
    with _PROVIDERS_LOCK:
        _PROVIDERS_CACHE["data"] = None
    return {"ok": True, **result}


def disconnect_provider(provider_id: str) -> dict[str, Any]:
    """Remove a provider from config and, if it had an auth credential, logout."""
    if not provider_id or not _PROVIDER_ID_RE.match(provider_id):
        return {"ok": False, "error": f"invalid provider id: {provider_id!r}"}

    snapshot = list_providers(refresh=True)
    entry = next((p for p in snapshot.get("providers") or [] if p["id"] == provider_id), None)

    result: dict[str, Any] = {"ok": True, "logout": None, "config": None}

    if entry and entry.get("auth_connected"):
        result["logout"] = logout_provider(provider_id)

    # Always attempt to remove the config entry; if ``opencode auth list``
    # failed, ``entry`` may be None even though the provider is configured.
    if entry is None or entry.get("configured"):
        try:
            result["config"] = opencode_config.remove_provider(provider_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    with _PROVIDERS_LOCK:
        _PROVIDERS_CACHE["data"] = None
    return result
