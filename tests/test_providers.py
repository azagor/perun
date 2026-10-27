"""Tests for the LLM provider discovery and configuration module."""

from __future__ import annotations

import json
import time

import pytest

from perun import opencode_config, providers


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point the OpenCode config at an empty temp file for the duration of a test."""
    path = tmp_path / "opencode.json"
    monkeypatch.setattr(opencode_config, "_config_file", lambda: path)
    monkeypatch.setattr(opencode_config, "reload_opencode", lambda **kw: {"ok": True})
    providers._PROVIDERS_CACHE["data"] = None
    return path


def test_list_providers_parses_opencode_auth_output(monkeypatch, isolated_config):
    """list_providers parses the JSON emitted by ``opencode auth list``."""
    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = json.dumps([
                {"id": "p1", "name": "Provider One",
                 "connections": [{"type": "credential", "label": "API key"}]},
                {"id": "p2", "name": "Provider Two", "connections": []},
            ])
            stderr = ""
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    data = providers.list_providers()
    assert data["ok"] is True
    assert data["count"] == 2
    assert data["providers"][0]["connected"] is True
    assert data["providers"][0]["status"] == "connected"
    assert data["providers"][1]["connected"] is False
    assert data["providers"][1]["status"] == "not_connected"


def test_list_providers_uses_cache(monkeypatch, isolated_config):
    """Repeated calls reuse the cached result unless refresh=True."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(1)
        class R:
            returncode = 0
            stdout = json.dumps([{"id": "p1", "name": "P1", "connections": []}])
            stderr = ""
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    providers.list_providers()
    providers.list_providers()
    assert len(calls) == 1
    providers.list_providers(refresh=True)
    assert len(calls) == 2


def test_list_providers_reports_subprocess_errors(monkeypatch, isolated_config):
    """A failing ``opencode auth list`` command is returned gracefully."""
    def fake_run(argv, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "not logged in"
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    data = providers.list_providers()
    assert data["ok"] is False
    assert data["count"] == 0
    assert "not logged in" in data["error"]


def test_list_providers_parses_wrapped_auth_output(monkeypatch, isolated_config):
    """``opencode auth list`` may wrap providers in a top-level providers key."""
    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = json.dumps({
                "providers": [
                    {"integrationID": "openai", "name": "OpenAI",
                     "connections": [{"type": "credential"}]},
                    {"providerId": "ollama", "name": "Ollama", "connections": []},
                ]
            })
            stderr = ""
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    data = providers.list_providers()
    assert data["count"] == 2
    ids = {p["id"] for p in data["providers"]}
    assert ids == {"openai", "ollama"}


def test_list_providers_merges_configured_providers(monkeypatch, isolated_config):
    """Providers written to opencode.json appear as configured, even without auth."""
    opencode_config.configure_provider("ollama", {"baseURL": "http://localhost:11434/v1"})

    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = json.dumps([{"id": "p1", "name": "P1", "connections": []}])
            stderr = ""
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    data = providers.list_providers()
    assert data["count"] == 2
    ids = {p["id"] for p in data["providers"]}
    assert ids == {"p1", "ollama"}
    ollama = next(p for p in data["providers"] if p["id"] == "ollama")
    assert ollama["configured"] is True
    assert ollama["connected"] is True


def test_configured_providers_merges_windows_config_dirs(monkeypatch, tmp_path, isolated_config):
    """On Windows, providers from %APPDATA%\\opencode are merged with the primary config."""
    # Primary config gets one provider.
    opencode_config.configure_provider("primary", {"baseURL": "http://primary/v1"})

    # Simulate a TUI-written config in APPDATA\opencode on Windows.
    appdata = tmp_path / "appdata" / "opencode"
    appdata.mkdir(parents=True)
    appdata_config = appdata / "opencode.json"
    appdata_config.write_text(json.dumps({
        "providers": {"tui-provider": {"name": "TUI Provider", "apiKey": "sk-tui"}}
    }), encoding="utf-8")

    monkeypatch.setattr(opencode_config.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    # Clear LOCALAPPDATA so it does not interfere.
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    out, _ = opencode_config.configured_providers()
    ids = {p["id"] for p in out}
    assert "primary" in ids
    assert "tui-provider" in ids
    tui = next(p for p in out if p["id"] == "tui-provider")
    assert tui["has_key"] is True


def test_configured_providers_tolerates_list_form(monkeypatch, isolated_config):
    """OpenCode may store providers as a list; we still surface them."""
    path = isolated_config
    opencode_config.save_config(
        {
            "providers": [
                {"id": "openai", "name": "OpenAI", "apiKey": "sk-test"},
                {"id": "custom-provider", "name": "Custom", "baseURL": "http://example.com/v1"},
            ]
        },
        path,
    )

    out, _ = opencode_config.configured_providers()
    ids = {p["id"] for p in out}
    assert ids == {"openai", "custom-provider"}
    openai = next(p for p in out if p["id"] == "openai")
    assert openai["has_key"] is True
    custom = next(p for p in out if p["id"] == "custom-provider")
    assert custom["base_url"] == "http://example.com/v1"


def test_list_providers_falls_back_to_models_when_auth_and_config_empty(monkeypatch, isolated_config):
    """If auth/config yield nothing, derive providers from ``opencode models``."""
    calls = []

    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        if "auth" in argv:
            R.stdout = json.dumps([])
        elif "models" in argv:
            R.stdout = "openai/gpt-4\nollama/llama3\nopenai/gpt-3.5-turbo"
        calls.append(" ".join(argv))
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    data = providers.list_providers()
    assert data["count"] == 2
    ids = {p["id"] for p in data["providers"]}
    assert ids == {"openai", "ollama"}
    assert all(p["connected"] for p in data["providers"])


def test_auth_env_for_subprocess_exports_active_keys(monkeypatch):
    """auth_env_for_subprocess maps active credentials to provider env vars."""
    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = json.dumps([
                {"id": "cred_1", "integrationID": "openai", "active": True,
                 "value": {"type": "key", "key": "sk-openai"}},
                {"id": "cred_2", "integrationID": "opencode-go", "active": True,
                 "value": {"type": "key", "key": "sk-go"}},
                {"id": "cred_3", "integrationID": "opencode", "active": False,
                 "value": {"type": "key", "key": "sk-inactive"}},
            ])
            stderr = ""
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    env = providers.auth_env_for_subprocess()
    assert env == {"OPENAI_API_KEY": "sk-openai", "OPENCODE_GO_API_KEY": "sk-go"}


def test_resolve_run_model_keeps_authenticated_provider(monkeypatch):
    """A model whose provider is authenticated is passed through unchanged."""
    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = json.dumps([
                {"id": "c1", "integrationID": "openai", "active": True,
                 "value": {"type": "key", "key": "sk"}},
            ])
            stderr = ""
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    providers._MODELS_CACHE["data"] = frozenset(["openai/gpt-5"])
    providers._MODELS_CACHE["ts"] = time.time()
    resolved, env = providers.resolve_run_model("openai/gpt-5")
    assert resolved == "openai/gpt-5"
    assert env["OPENAI_API_KEY"] == "sk"


def test_resolve_run_model_opencode_alias_to_opencode_go(monkeypatch):
    """Third-party models advertised under ``opencode/`` are routed to ``opencode-go``."""
    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = json.dumps([
                {"id": "c1", "integrationID": "opencode", "active": True,
                 "value": {"type": "key", "key": "oc-sk"}},
                {"id": "c2", "integrationID": "opencode-go", "active": True,
                 "value": {"type": "key", "key": "go-sk"}},
            ])
            stderr = ""
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    providers._MODELS_CACHE["data"] = frozenset([
        "opencode/big-pickle", "opencode-go/kimi-k2.7-code",
    ])
    providers._MODELS_CACHE["ts"] = time.time()
    resolved, env = providers.resolve_run_model("opencode/kimi-k2.7-code")
    assert resolved == "opencode-go/kimi-k2.7-code"
    assert env["OPENCODE_GO_API_KEY"] == "go-sk"


def test_resolve_run_model_opencode_own_model_stays_put(monkeypatch):
    """OpenCode's own models (not on opencode-go) keep the ``opencode/`` prefix."""
    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = json.dumps([
                {"id": "c1", "integrationID": "opencode", "active": True,
                 "value": {"type": "key", "key": "oc-sk"}},
                {"id": "c2", "integrationID": "opencode-go", "active": True,
                 "value": {"type": "key", "key": "go-sk"}},
            ])
            stderr = ""
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    providers._MODELS_CACHE["data"] = frozenset(["opencode/big-pickle"])
    providers._MODELS_CACHE["ts"] = time.time()
    resolved, _ = providers.resolve_run_model("opencode/big-pickle")
    assert resolved == "opencode/big-pickle"


def test_resolve_run_model_falls_back_to_active_provider(monkeypatch):
    """If the requested provider is not authenticated, switch to one that is."""
    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = json.dumps([
                {"id": "c1", "integrationID": "openai", "active": True,
                 "value": {"type": "key", "key": "sk"}},
            ])
            stderr = ""
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    providers._MODELS_CACHE["data"] = frozenset(["openai/gpt-5"])
    providers._MODELS_CACHE["ts"] = time.time()
    resolved, env = providers.resolve_run_model("anthropic/gpt-5")
    assert resolved == "openai/gpt-5"
    assert env["OPENAI_API_KEY"] == "sk"


def test_resolve_run_model_prefers_cloud_over_local_fallback(monkeypatch):
    """When the requested provider is missing, cloud providers outrank local ones."""
    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = json.dumps([
                {"id": "c1", "integrationID": "openai", "active": True,
                 "value": {"type": "key", "key": "sk-openai"}},
                {"id": "c2", "integrationID": "ollama", "active": True,
                 "value": {"type": "key", "key": "sk-ollama"}},
            ])
            stderr = ""
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    providers._MODELS_CACHE["data"] = frozenset(["openai/gpt-5", "ollama/gpt-5"])
    providers._MODELS_CACHE["ts"] = time.time()
    resolved, env = providers.resolve_run_model("anthropic/gpt-5")
    assert resolved == "openai/gpt-5"
    assert env["OPENAI_API_KEY"] == "sk-openai"


def test_resolve_run_model_detects_custom_local_provider(monkeypatch, isolated_config):
    """Custom providers pointing at localhost are treated as local fallbacks."""
    opencode_config.configure_provider("my-local", {
        "baseURL": "http://localhost:1234/v1", "apiKey": "x",
    })

    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = json.dumps([
                {"id": "c1", "integrationID": "openai", "active": True,
                 "value": {"type": "key", "key": "sk-openai"}},
                {"id": "c2", "integrationID": "my-local", "active": True,
                 "value": {"type": "key", "key": "sk-local"}},
            ])
            stderr = ""
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    providers._MODELS_CACHE["data"] = frozenset(["openai/gpt-5", "my-local/gpt-5"])
    providers._MODELS_CACHE["ts"] = time.time()
    resolved, env = providers.resolve_run_model("anthropic/gpt-5")
    assert resolved == "openai/gpt-5"
    assert env["OPENAI_API_KEY"] == "sk-openai"


def test_logout_provider_reports_success(monkeypatch):
    def fake_run(argv, **kwargs):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    providers._PROVIDERS_CACHE["data"] = {"ok": True}
    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    result = providers.logout_provider("p1")
    assert result["ok"] is True
    assert result["error"] is None
    assert providers._PROVIDERS_CACHE["data"] is None


def test_logout_provider_rejects_invalid_id():
    result = providers.logout_provider("../../etc/passwd")
    assert result["ok"] is False
    assert "invalid provider id" in result["error"]


def test_logout_provider_reports_failure(monkeypatch):
    def fake_run(argv, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "no such provider"
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    result = providers.logout_provider("missing")
    assert result["ok"] is False
    assert "no such provider" in result["error"]


def test_connect_provider_writes_config(isolated_config):
    """connect_provider writes the API key and base URL to opencode.json."""
    result = providers.connect_provider("openai", {"api_key": "sk-test", "base_url": "https://proxy.example/v1"})
    assert result["ok"] is True

    data, _, _ = opencode_config.load_config()
    section = opencode_config.providers_section(data)
    assert "openai" in section
    assert section["openai"]["settings"]["apiKey"] == "sk-test"
    assert section["openai"]["settings"]["baseURL"] == "https://proxy.example/v1"


def test_disconnect_provider_removes_config(isolated_config):
    """disconnect_provider removes the provider entry from opencode.json."""
    providers.connect_provider("ollama", {"base_url": "http://localhost:11434/v1"})
    result = providers.disconnect_provider("ollama")
    assert result["ok"] is True
    assert result["config"]["removed"] is True

    data, _, _ = opencode_config.load_config()
    assert "ollama" not in opencode_config.providers_section(data)


def test_connect_provider_syncs_ollama_host(isolated_config):
    """Connecting Ollama also updates Perun's ollama_host setting."""
    from perun import config

    result = providers.connect_provider("ollama", {"base_url": "http://127.0.0.1:11434/v1"})
    assert result["ok"] is True
    settings = config.Settings.load()
    assert settings.ollama_host == "http://127.0.0.1:11434"


def test_connect_provider_uses_catalog_package_for_openai_compatible_presets(isolated_config):
    """Non-built-in presets are written with the OpenAI-compatible runtime package."""
    result = providers.connect_provider("groq", {"api_key": "gsk-test"})
    assert result["ok"] is True

    data, _, _ = opencode_config.load_config()
    section = opencode_config.providers_section(data)
    assert "groq" in section
    assert section["groq"]["package"] == "@opencode/ai/providers/openai-compatible"
    assert section["groq"]["settings"]["apiKey"] == "gsk-test"
    assert section["groq"]["settings"]["baseURL"] == "https://api.groq.com/openai/v1"


def test_connect_custom_provider_writes_package_and_base_url(isolated_config):
    """Custom providers get the canonical OpenAI-compatible package by default."""
    result = providers.connect_provider("my-gateway", {
        "name": "My Gateway",
        "api_key": "sk-test",
        "base_url": "https://gateway.example/v1",
    })
    assert result["ok"] is True

    data, _, _ = opencode_config.load_config()
    section = opencode_config.providers_section(data)
    assert "my-gateway" in section
    assert section["my-gateway"]["name"] == "My Gateway"
    assert section["my-gateway"]["package"] == "@opencode/ai/providers/openai-compatible"
    assert section["my-gateway"]["settings"]["baseURL"] == "https://gateway.example/v1"


def test_opencode_config_migrates_legacy_provider_key(isolated_config):
    """Writing providers migrates a legacy top-level ``provider`` key to ``providers``."""
    path = isolated_config
    path.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "provider": {"ollama": {"settings": {"baseURL": "http://localhost:11434/v1"}}},
    }), encoding="utf-8")

    opencode_config.configure_provider("openai", {"apiKey": "sk-test"}, reload=False)

    data, _, _ = opencode_config.load_config()
    assert "provider" not in data
    assert "providers" in data
    assert "ollama" in data["providers"]
    assert "openai" in data["providers"]


def test_opencode_config_creates_backup_on_write(isolated_config):
    """The previous config file is backed up before it is rewritten."""
    path = isolated_config
    path.write_text(json.dumps({"providers": {}}), encoding="utf-8")

    opencode_config.configure_provider("openai", {"apiKey": "sk-test"}, reload=False)

    backups = list(path.parent.glob("opencode.json.perun-backup-*"))
    assert len(backups) == 1
    backup = json.loads(backups[0].read_text(encoding="utf-8"))
    assert backup == {"providers": {}}


def test_disconnect_provider_removes_config_when_auth_list_fails(monkeypatch, isolated_config):
    """Disconnect still purges the config entry even if opencode auth list fails."""
    providers.connect_provider("ollama", {"base_url": "http://localhost:11434/v1"})

    def fake_run(argv, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "opencode not found"
        return R()

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    result = providers.disconnect_provider("ollama")
    assert result["ok"] is True
    assert result["config"]["removed"] is True

    data, _, _ = opencode_config.load_config()
    assert "ollama" not in opencode_config.providers_section(data)
