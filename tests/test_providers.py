"""Tests for the LLM provider discovery module."""

from __future__ import annotations

import json

from perun import providers


def test_list_providers_parses_opencode_auth_output(monkeypatch):
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

    providers._PROVIDERS_CACHE["data"] = None
    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    data = providers.list_providers()
    assert data["ok"] is True
    assert data["count"] == 2
    assert data["providers"][0]["connected"] is True
    assert data["providers"][0]["status"] == "connected"
    assert data["providers"][1]["connected"] is False
    assert data["providers"][1]["status"] == "not_connected"


def test_list_providers_uses_cache(monkeypatch):
    """Repeated calls reuse the cached result unless refresh=True."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(1)
        class R:
            returncode = 0
            stdout = json.dumps([{"id": "p1", "name": "P1", "connections": []}])
            stderr = ""
        return R()

    providers._PROVIDERS_CACHE["data"] = None
    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    providers.list_providers()
    providers.list_providers()
    assert len(calls) == 1
    providers.list_providers(refresh=True)
    assert len(calls) == 2


def test_list_providers_reports_subprocess_errors(monkeypatch):
    """A failing ``opencode auth list`` command is returned gracefully."""
    def fake_run(argv, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = "not logged in"
        return R()

    providers._PROVIDERS_CACHE["data"] = None
    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    data = providers.list_providers()
    assert data["ok"] is False
    assert data["count"] == 0
    assert "not logged in" in data["error"]


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
