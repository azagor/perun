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

# Cache provider list the same way /api/models does - ``opencode auth list`` is
# fast but can still churn if the dashboard is reloaded repeatedly.
_PROVIDERS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_PROVIDERS_TTL = 30.0
_PROVIDERS_LOCK = threading.Lock()

# Map OpenCode integration IDs to the environment variable name the OpenCode
# CLI expects for API-key authentication. This lets headless ``opencode run``
# use the same cloud credentials the TUI/desktop app already authenticated.
_AUTH_ENV_NAMES: dict[str, str] = {
    "opencode": "OPENCODE_API_KEY",
    "opencode-go": "OPENCODE_GO_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "moonshotai": "MOONSHOT_API_KEY",
    "cohere": "COHERE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "togetherai": "TOGETHER_API_KEY",
    "fireworks-ai": "FIREWORKS_API_KEY",
    "ollama-cloud": "OLLAMA_CLOUD_API_KEY",
}

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


def _models_providers(timeout: float = 5.0) -> dict[str, Any]:
    """Derive connected providers from ``opencode models``.

    This is a fallback for when ``opencode auth list`` does not include
    providers that were added through the OpenCode TUI but are already serving
    models. The model list entries look like ``provider_id/model_id``.
    """
    from .orchestrator import resolve_opencode

    argv = resolve_opencode("opencode") + ["models"]
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
            "error": (proc.stderr or "").strip()[:500] or "opencode models failed",
        }

    provider_names: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        mid = line.strip()
        if not mid:
            continue
        provider, _, name = mid.partition("/")
        if not name:
            continue
        provider_id = provider.lower()
        meta = _PROVIDER_CATALOG.get(provider_id)
        provider_names[provider_id] = meta.get("name", provider) if meta else provider

    providers: list[dict[str, Any]] = []
    for pid, name in provider_names.items():
        meta = _PROVIDER_CATALOG.get(pid, {})
        providers.append({
            "id": pid,
            "name": name,
            "auth_connected": True,
            "connected": True,
            "configured": False,
            "has_key": False,
            "base_url": "",
            "source": "",
            "kind": meta.get("kind", "custom"),
            "status": "connected",
        })
    return {"ok": True, "providers": providers, "count": len(providers), "error": None}


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
    # OpenCode may wrap the list in {"providers": [...]}; accept both forms.
    if isinstance(raw, dict):
        entries = raw.get("providers") or []
        if not isinstance(entries, list):
            entries = [raw] if entries is None else []
    elif isinstance(raw, list):
        entries = raw
    else:
        entries = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # OpenCode may emit keys with different capitalisation; normalise.
        entry_norm = {k.lower(): v for k, v in entry.items()}
        conns = entry_norm.get("connections") or []
        connected = bool(conns) and all(isinstance(c, dict) for c in conns)
        providers.append({
            "id": entry_norm.get("id", entry_norm.get("integrationid", entry_norm.get("providerid", ""))),
            "name": entry_norm.get("name", ""),
            "auth_connected": connected,
            "connected": connected,
            "status": "connected" if connected else "not_connected",
            "connections": conns,
        })
    return {"ok": True, "providers": providers, "count": len(providers), "error": None}


def _integration_env_var(integration_id: str) -> str:
    """Canonical env-var name for an OpenCode integration ID."""
    return _AUTH_ENV_NAMES.get(integration_id) or (
        re.sub(r"[^A-Za-z0-9_]+", "_", integration_id).upper() + "_API_KEY"
    )


def active_api_key_credentials(*, timeout: float = 10.0) -> list[dict[str, str]]:
    """Return active API-key credentials from ``opencode auth export``.

    Each entry contains ``integration_id``, ``env_var`` and ``key``. The
    credentials are read fresh on every call and are never cached, logged, or
    stored by Perun; they are only returned so they can be injected directly
    into a child ``opencode run`` process.
    """
    creds: list[dict[str, str]] = []
    argv = _opencode_argv() + ["auth", "export"]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0:
            raw = json.loads(proc.stdout or "[]")
            for entry in raw if isinstance(raw, list) else []:
                if not isinstance(entry, dict):
                    continue
                if not entry.get("active"):
                    continue
                value = entry.get("value") or {}
                if value.get("type") != "key":
                    continue
                key = str(value.get("key") or "").strip()
                if not key:
                    continue
                integration = str(entry.get("integrationID") or "").strip()
                if not integration:
                    continue
                creds.append({
                    "integration_id": integration,
                    "env_var": _integration_env_var(integration),
                    "key": key,
                })
    except Exception:
        # Graceful degradation: if auth export fails (opencode missing, user
        # denies export, etc.), fall back to the previous behavior. The run may
        # still work if the provider does not need a key or if the key is in
        # opencode.json.
        pass
    return [dict(c) for c in creds]


def auth_env_for_subprocess(*, timeout: float = 10.0) -> dict[str, str]:
    """Return API-key env vars for every active OpenCode credential.

    Headless ``opencode run`` does not share the TUI/desktop app's interactive
    authentication session. Exporting active credentials and injecting them as
    environment variables lets the same cloud providers work in daemon runs.
    The keys are only passed to the child process and are never cached or stored
    by Perun.
    """
    return {c["env_var"]: c["key"]
            for c in active_api_key_credentials(timeout=timeout)}


# OpenCode sometimes lists the same third-party model under the ``opencode``
# provider namespace, but that namespace is only reachable for OpenCode's own
# models (e.g. ``opencode/big-pickle``). For headless runs we prefer the
# API-key gateway that actually hosts the model.
_PROVIDER_ALIASES: dict[str, list[str]] = {
    "opencode": ["opencode-go"],
}

# Cache for ``opencode models`` output.
_MODELS_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_MODELS_TTL = 60.0
_MODELS_LOCK = threading.Lock()


def _available_models(*, refresh: bool = False, timeout: float = 10.0) -> frozenset[str]:
    """Return the set of ``provider/model`` IDs OpenCode currently exposes."""
    now = time.time()
    with _MODELS_LOCK:
        if (not refresh and _MODELS_CACHE["data"] is not None
                and now - _MODELS_CACHE["ts"] < _MODELS_TTL):
            return _MODELS_CACHE["data"]

    models: set[str] = set()
    argv = _opencode_argv() + ["models"]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if line and "/" in line and not line.startswith("["):
                    models.add(line.split()[0])
    except Exception:
        proc = None

    if proc is not None and proc.returncode == 0:
        data = frozenset(models)
        with _MODELS_LOCK:
            _MODELS_CACHE.update(ts=now, data=data)
        return data

    # Return the previous cache (or an empty set) without overwriting it.
    with _MODELS_LOCK:
        cached = _MODELS_CACHE["data"]
    return cached if cached is not None else frozenset()


def _local_provider_ids() -> set[str]:
    """Return provider IDs that are known to run on the local machine.

    Built-in local providers are taken from the catalog; custom providers are
    detected from the configured ``baseURL`` when it points to localhost.
    """
    local = {pid for pid, meta in _PROVIDER_CATALOG.items()
             if meta.get("kind") == "local"}
    try:
        configured, _ = opencode_config.configured_providers()
        for cp in configured:
            base = (cp.get("base_url") or "").lower()
            if "localhost" in base or "127.0.0.1" in base or "::1" in base:
                local.add(cp["id"])
    except Exception:
        pass
    return local


def resolve_run_model(model: str, *, refresh: bool = False,
                      timeout: float = 10.0) -> tuple[str, dict[str, str]]:
    """Resolve a model string to one that works with headless ``opencode run``.

    The requested provider is honored when it has an active API-key credential.
    If the requested provider is authenticated but known to mis-list models from
    another gateway, the gateway is preferred when it also offers the model. If
    the requested provider is not authenticated, we look for an active cloud
    provider that exposes the same model base; local providers are only used as
    a last resort so a slow local model is never silently substituted for a
    cloud model.

    Returns ``(resolved_model, auth_env)``. ``resolved_model`` is the original
    string when no better match is found; ``auth_env`` contains the API-key
    environment variables for all active credentials, even when resolution does
    not change, so a child process can authenticate in standalone mode.
    """
    creds = active_api_key_credentials(timeout=timeout)
    env = {c["env_var"]: c["key"] for c in creds}
    active = {c["integration_id"] for c in creds}
    requested = (model or "").strip()
    if not requested:
        return "", env
    if "/" not in requested:
        return requested, env

    prefix, _, base = requested.partition("/")
    models = _available_models(refresh=refresh, timeout=timeout)

    # If the requested provider is active, prefer it unless a known alias is
    # active and lists the same model (e.g. ``opencode/kimi-k2.7-code`` is
    # actually hosted on ``opencode-go``).
    if prefix in active:
        for alias in _PROVIDER_ALIASES.get(prefix, []):
            if alias in active and f"{alias}/{base}" in models:
                return f"{alias}/{base}", env
        if requested in models:
            return requested, env
        # The requested provider is authenticated but the exact model id is not
        # advertised; keep the user's choice and let the run fail clearly.
        return requested, env

    def _candidate(prefixes: list[str]) -> str | None:
        for candidate_prefix in prefixes:
            if candidate_prefix == prefix:
                continue
            candidate = f"{candidate_prefix}/{base}"
            if candidate in models:
                return candidate
        return None

    # Requested provider is not authenticated via API key. Try aliases first,
    # then active cloud providers, and only fall back to local providers when
    # no cloud option exists.
    aliases = _PROVIDER_ALIASES.get(prefix, [])
    local_ids = _local_provider_ids()
    cloud = [p for p in active if p not in local_ids]
    local = [p for p in active if p in local_ids]

    for prefixes in (aliases, cloud, local):
        candidate = _candidate(prefixes)
        if candidate:
            return candidate, env

    return requested, env


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

    # Fallback: if auth/config produced nothing, derive providers from the
    # model list so the dashboard still shows providers that are already usable.
    if not active:
        models_auth = _models_providers(timeout=timeout)
        for mp in models_auth.get("providers") or []:
            pid = mp["id"]
            if pid not in active_by_id:
                active_by_id[pid] = mp
                active.append(mp)
        if models_auth.get("error") and auth.get("error") is None:
            auth["error"] = models_auth["error"]

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
