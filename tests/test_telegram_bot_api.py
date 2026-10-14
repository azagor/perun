"""Telegram bot configuration API — the dashboard's "switch to a new bot" flow.

Covered here:

* ``GET  /api/settings``         never echoes the token (only a ``*_set`` flag).
* ``GET  /api/telegram/status``  reports the bot label + whether a logon task exists.
* ``POST /api/telegram/bot``     verifies a new token with ``getMe`` *before*
  persisting it, edits the label/chat ids, and clears the token on ``token: ""``.
* ``POST /api/telegram/bridge/restart`` delegates to the installer.

Network calls are mocked — these tests must never contact Telegram.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    # Settings.telegram() falls back to the environment, and this machine has a
    # real bot in TELEGRAM_BOT_TOKEN — clear it so every test is deterministic.
    for var in ("TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TG_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    # Start from a known-clean bot config and restore it afterwards.
    saved = (server.SETTINGS.telegram_token,
             server.SETTINGS.telegram_bot_name,
             list(server.SETTINGS.telegram_allowed_chats))
    server.SETTINGS.telegram_token = ""
    server.SETTINGS.telegram_bot_name = ""
    server.SETTINGS.telegram_allowed_chats = []
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as c:
        yield c
    (server.SETTINGS.telegram_token,
     server.SETTINGS.telegram_bot_name,
     server.SETTINGS.telegram_allowed_chats) = saved


def _fake_getme(username: str = "perun_bot", name: str = "Perun"):
    def _call(token: str, *a, **k):
        if not token or not token.isalnum():
            return {"ok": False, "description": "Unauthorized"}
        return {"ok": True, "result": {"id": 1, "is_bot": True,
                                       "first_name": name, "username": username}}
    return _call


def test_settings_never_echoes_the_token(client, monkeypatch):
    from perun import server

    server.SETTINGS.telegram_token = "SECRET-TOKEN"
    r = client.get("/api/settings")
    assert r.status_code == 200
    s = r.json()["settings"]
    assert s["telegram_token"] == ""          # never the real value
    assert s["telegram_token_set"] is True
    assert "SECRET-TOKEN" not in r.text


def test_bot_saves_label_and_chats_without_a_token(client):
    r = client.post("/api/telegram/bot",
                    json={"name": "My bot", "chats": "111, 222"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bot_name"] == "My bot"
    assert body["allowed_chats"] == ["111", "222"]
    assert body["restart_required"] is False
    assert body["configured"] is False        # no token yet


def test_bot_rejects_an_invalid_token(client, monkeypatch):
    from perun import server

    monkeypatch.setattr(server, "_telegram_getme", _fake_getme())
    r = client.post("/api/telegram/bot", json={"token": "not a valid token!"})
    assert r.status_code == 400
    assert "Unauthorized" in r.json()["detail"]
    # A rejected token must not be stored.
    assert server.SETTINGS.telegram_token == ""


def test_bot_verifies_then_persists_a_new_token(client, monkeypatch):
    from perun import server

    monkeypatch.setattr(server, "_telegram_getme", _fake_getme(username="new_bot"))

    r = client.post("/api/telegram/bot", json={"token": "abc123", "chats": ["42"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["restart_required"] is True   # bridge must restart to apply
    assert server.SETTINGS.telegram_token == "abc123"

    # The identity comes from the (mocked) getMe using the *persisted* token.
    monkeypatch.setattr(server, "_telegram_api", lambda method, **kw: {
        "ok": True, "result": {"first_name": "Perun", "username": "new_bot"}})
    st = client.get("/api/telegram/status").json()
    assert st["configured"] is True
    assert st["bot"]["username"] == "new_bot"
    assert st["allowed_chats"] == ["42"]
    assert "bridge_running" in st and "bridge_task" in st


def test_bot_clear_token_disconnects(client, monkeypatch):
    from perun import server

    server.SETTINGS.telegram_token = "abc123"
    monkeypatch.setattr(server, "_telegram_api", lambda method, **kw: {"ok": False})
    r = client.post("/api/telegram/bot", json={"token": ""})
    assert r.status_code == 200
    assert server.SETTINGS.telegram_token == ""
    assert r.json()["configured"] is False
    assert r.json()["bot"] is None


def test_status_reports_bot_label_fallback(client, monkeypatch):
    from perun import server

    server.SETTINGS.telegram_token = "abc123"
    server.SETTINGS.telegram_bot_name = ""
    monkeypatch.setattr(server, "_telegram_api", lambda method, **kw: {
        "ok": True, "result": {"first_name": "Perun Overseer", "username": "perun_bot"}})
    st = client.get("/api/telegram/status").json()
    # No local label set -> the dashboard falls back to the Telegram name.
    assert st["bot_name"] == ""
    assert st["bot"]["first_name"] == "Perun Overseer"


def test_bridge_restart_delegates_to_installer(client, monkeypatch):
    from perun import installer

    calls = {}

    def _fake_restart():
        calls["hit"] = True
        return {"ok": True, "ended": True, "started": True}

    monkeypatch.setattr(installer, "restart_bridge_task", _fake_restart)
    r = client.post("/api/telegram/bridge/restart")
    assert r.status_code == 200
    assert calls.get("hit") is True
    assert r.json()["restart"]["ok"] is True


# --------------------------------------------------------------------------- #
# Environment fallback (this machine really does configure Telegram this way)
# --------------------------------------------------------------------------- #
def test_env_token_is_a_documented_fallback(client, monkeypatch):
    from perun import server

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "envtoken")
    monkeypatch.setattr(server, "_telegram_api", lambda method, **kw: {
        "ok": True, "result": {"first_name": "Env", "username": "env_bot"}})
    st = client.get("/api/telegram/status").json()
    assert st["configured"] is True
    assert st["token_source"] == "env"          # dashboard shows the warning
    assert st["bot"]["username"] == "env_bot"


def test_config_token_overrides_env(client, monkeypatch):
    from perun import server

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "envtoken")
    monkeypatch.setattr(server, "_telegram_getme", _fake_getme(username="cfg_bot"))
    monkeypatch.setattr(server, "_telegram_api", lambda method, **kw: {
        "ok": True, "result": {"username": "cfg_bot"}})
    r = client.post("/api/telegram/bot", json={"token": "cfgtoken"})
    assert r.status_code == 200, r.text
    assert server.SETTINGS.telegram_token == "cfgtoken"
    assert r.json()["token_source"] == "settings"   # settings win over env


def test_status_reports_settings_source_when_configured(client, monkeypatch):
    from perun import server

    server.SETTINGS.telegram_token = "abc123"
    monkeypatch.setattr(server, "_telegram_api", lambda method, **kw: {"ok": True, "result": {}})
    st = client.get("/api/telegram/status").json()
    assert st["token_source"] == "settings"
