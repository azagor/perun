"""Tests for the PerunChat proxy endpoints in Perun's dashboard integration."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from perun import config, server


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Use a fresh PERUN_HOME and the expected base URL for the origin guard."""
    home = tmp_path / "perun_home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "CONFIG_PATH", home / "config.json")
    monkeypatch.setattr(config, "DB_PATH", home / "perun.db")
    monkeypatch.setattr(config, "API_TOKEN_PATH", home / "api-token")
    monkeypatch.setenv("PERUN_HOME", str(home))
    monkeypatch.setenv("PERUN_ENV", "test")

    fresh = config.Settings.load()
    fresh.auto_discover_on_start = False
    server.SETTINGS = fresh
    server.ORCH.settings = fresh
    fresh.save()

    with TestClient(server.app, base_url="http://127.0.0.1:8760") as c:
        yield c


def _token_header() -> dict[str, str]:
    return {"X-Perun-Token": config.api_token()}


def test_perunchat_status_unconfigured(client) -> None:
    r = client.get("/api/perunchat/status", headers=_token_header())
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["configured"] is False


def test_perunchat_key_rejected_for_non_loopback(client) -> None:
    # TestClient reports a non-loopback client host, so the key endpoint must
    # refuse to reveal the secret before checking whether one exists.
    r = client.get("/api/perunchat/key", headers=_token_header())
    assert r.status_code == 403


def test_perunchat_status_configured(client) -> None:
    server.SETTINGS.perunchat_api_key = "prn_test_abc123"
    server.SETTINGS.perunchat_api_endpoint = "wss://peruninthemachine.com/v1/chat/ws"
    r = client.get("/api/perunchat/status", headers=_token_header())
    assert r.status_code == 200
    data = r.json()
    assert data["configured"] is True
    assert data["rest_base"] == "https://peruninthemachine.com/chat"


def test_perunchat_status_includes_agent_id(client, monkeypatch) -> None:
    server.SETTINGS.perunchat_api_key = "prn_test_abc123"
    server.SETTINGS.perunchat_api_endpoint = "wss://peruninthemachine.com/v1/chat/ws"

    def fake_proxy(method, path, body=None, params=None):
        if path == "/api/v1/me":
            return {"agent_id": "PerunTheOverseer"}
        return {}

    monkeypatch.setattr(server, "_perunchat_proxy", fake_proxy)
    r = client.get("/api/perunchat/status", headers=_token_header())
    data = r.json()
    assert data["configured"] is True
    assert data["agent_id"] == "PerunTheOverseer"


def test_perunchat_status_local_endpoint(client) -> None:
    server.SETTINGS.perunchat_api_key = "prn_test_abc123"
    server.SETTINGS.perunchat_api_endpoint = "ws://127.0.0.1:8765/v1/chat/ws"
    r = client.get("/api/perunchat/status", headers=_token_header())
    data = r.json()
    assert data["rest_base"] == "http://127.0.0.1:8765"


def test_settings_never_echoes_perunchat_key(client) -> None:
    server.SETTINGS.perunchat_api_key = "prn_test_secret"
    server.SETTINGS.save()
    r = client.get("/api/settings", headers=_token_header())
    data = r.json()
    assert data["settings"]["perunchat_api_key"] == ""
    assert data["settings"]["perunchat_api_key_set"] is True


def test_settings_can_update_perunchat_credentials(client) -> None:
    r = client.patch(
        "/api/settings",
        headers=_token_header(),
        json={
            "perunchat_api_key": "prn_test_new",
            "perunchat_api_endpoint": "wss://chat.example.com/v1/chat/ws",
        },
    )
    assert r.status_code == 200
    assert server.SETTINGS.perunchat() == ("prn_test_new", "wss://chat.example.com/v1/chat/ws")


def test_room_aliases_round_trip_and_never_reach_the_gateway(client, monkeypatch) -> None:
    """Aliases are local display state; the gateway must never hear about them."""
    calls: list[tuple[str, str]] = []

    def fake_proxy(method, path, body=None, params=None):
        calls.append((method, path))
        return {"ok": True, "rooms": []}

    monkeypatch.setattr(server, "_perunchat_proxy", fake_proxy)
    r = client.patch(
        "/api/settings",
        headers=_token_header(),
        json={"perunchat_room_aliases": {"priv:abc123": "Ada"}},
    )
    assert r.status_code == 200
    assert r.json()["settings"]["perunchat_room_aliases"] == {"priv:abc123": "Ada"}
    assert server.SETTINGS.perunchat_room_aliases == {"priv:abc123": "Ada"}
    # Clearing is an empty map, and the gateway is never called.
    cleared = client.patch(
        "/api/settings",
        headers=_token_header(),
        json={"perunchat_room_aliases": {}},
    )
    assert cleared.status_code == 200
    assert server.SETTINGS.perunchat_room_aliases == {}
    assert calls == []


def test_room_aliases_reject_junk_and_oversized_labels(client) -> None:
    headers = _token_header()
    assert client.patch(
        "/api/settings", headers=headers,
        json={"perunchat_room_aliases": {"not-a-room": "x"}},
    ).status_code == 400
    assert client.patch(
        "/api/settings", headers=headers,
        json={"perunchat_room_aliases": {"priv:abc": "x" * 61}},
    ).status_code == 400
    assert client.patch(
        "/api/settings", headers=headers,
        json={"perunchat_room_aliases": ["priv:abc"]},
    ).status_code == 400


def test_alias_save_does_not_bounce_the_perunchat_bridge(client, monkeypatch) -> None:
    """Display-only settings must not restart the bridge.

    Alias saves are frequent, and ``restart()`` drains room workers and
    reconnects the gateway socket, which can interrupt in-flight replies.
    """

    class _Bridge:
        def __init__(self) -> None:
            self.calls = 0

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def restart(self) -> None:
            self.calls += 1

    bridge = _Bridge()
    monkeypatch.setattr(server, "PERUNCHAT_BRIDGE", bridge)
    headers = _token_header()

    client.patch("/api/settings", headers=headers,
                 json={"perunchat_room_aliases": {"priv:abc": "Ada"}})
    assert bridge.calls == 0

    client.patch("/api/settings", headers=headers,
                 json={"perunchat_auto_join": ["pub:global:general"]})
    assert bridge.calls == 1


def test_perunchat_rooms_forwards_to_gateway(client, monkeypatch) -> None:
    """The dashboard room picker calls /api/perunchat/rooms, which must proxy
    the gateway's /api/v1/rooms discovery endpoint with the stored key."""
    calls: list[tuple[str, str]] = []

    def fake_proxy(method, path, body=None, params=None):
        calls.append((method, path))
        return {
            "ok": True,
            "rooms": [
                {"room_id": "pub:global:general", "type": "public",
                 "joined": False, "active": True, "members": 0, "connections": 2},
            ],
        }

    monkeypatch.setattr(server, "_perunchat_proxy", fake_proxy)
    r = client.get("/api/perunchat/rooms", headers=_token_header())
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["rooms"][0]["room_id"] == "pub:global:general"
    assert calls == [("GET", "/api/v1/rooms")]


def test_trusted_list_add_remove(client) -> None:
    """The trust list round-trips through settings and persists."""
    r = client.get("/api/perunchat/trusted", headers=_token_header())
    assert r.status_code == 200
    assert r.json()["trusted"] == []

    r = client.post("/api/perunchat/trusted",
                    json={"agent_id": "frambo_malina_omarchy"},
                    headers=_token_header())
    assert r.status_code == 200
    assert r.json()["trusted"] == ["frambo_malina_omarchy"]

    # Duplicates (case-insensitive) and blanks are ignored.
    r = client.post("/api/perunchat/trusted",
                    json={"agent_id": "FRAMBO_malina_omarchy"},
                    headers=_token_header())
    assert r.json()["trusted"] == ["frambo_malina_omarchy"]

    r = client.get("/api/perunchat/trusted", headers=_token_header())
    assert r.json()["trusted"] == ["frambo_malina_omarchy"]

    r = client.delete("/api/perunchat/trusted/frambo_malina_omarchy",
                      headers=_token_header())
    assert r.status_code == 200
    assert r.json()["trusted"] == []


def test_trusted_add_requires_agent_id(client) -> None:
    r = client.post("/api/perunchat/trusted", json={}, headers=_token_header())
    assert r.status_code == 400
