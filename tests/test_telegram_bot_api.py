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

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    # Settings.telegram() falls back to the environment, and this machine has a
    # real bot in TELEGRAM_BOT_TOKEN — clear it so every test is deterministic.
    for var in ("TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TG_CHAT_ID", "PERUN_PASSWORD"):
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


def test_telegram_prefers_orchestrator_env_var(client, monkeypatch):
    """PERUN_TELEGRAM_TOKEN (injected by the orchestrator) beats raw env/config."""
    from perun import config

    monkeypatch.setenv("PERUN_TELEGRAM_TOKEN", "orchestrator-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "raw-token")
    s = config.Settings()
    s.telegram_token = "settings-token"
    token, _ = s.telegram()
    assert token == "orchestrator-token"


def test_telegram_sources_reports_orchestrator_env(client, monkeypatch):
    from perun import config

    monkeypatch.setenv("PERUN_TELEGRAM_TOKEN", "orchestrator-token")
    s = config.Settings()
    s.telegram_token = "settings-token"
    src = s.telegram_sources()
    assert src["token"] == "orchestrator"


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


def test_bot_encrypts_token_when_password_supplied(client, monkeypatch):
    from perun import secrets, server

    monkeypatch.setattr(server, "_telegram_getme", _fake_getme(username="enc_bot"))
    monkeypatch.setattr(secrets, "_password", lambda: None)

    r = client.post("/api/telegram/bot", json={"token": "abc123", "password": "s3cret"})
    assert r.status_code == 200, r.text
    assert r.json()["token_source"] == "settings-encrypted"
    assert server.SETTINGS.telegram_token.startswith("enc:")
    # Decrypt round-trip.
    assert secrets.decrypt_value(server.SETTINGS.telegram_token, "s3cret") == "abc123"


def test_bot_encrypts_existing_plain_token_when_only_password_supplied(client, monkeypatch):
    from perun import secrets, server

    server.SETTINGS.telegram_token = "plain123"
    monkeypatch.setattr(server, "_telegram_getme", _fake_getme())
    monkeypatch.setattr(secrets, "_password", lambda: None)

    r = client.post("/api/telegram/bot", json={"password": "s3cret"})
    assert r.status_code == 200, r.text
    assert r.json()["token_source"] == "settings-encrypted"
    assert secrets.decrypt_value(server.SETTINGS.telegram_token, "s3cret") == "plain123"


def test_bot_does_not_auto_encrypt_from_env_password(client, monkeypatch):
    from perun import secrets, server

    monkeypatch.setenv("PERUN_PASSWORD", "envpw")
    monkeypatch.setattr(server, "_telegram_getme", _fake_getme())
    server.SETTINGS.telegram_token = ""

    r = client.post("/api/telegram/bot", json={"token": "abc123"})
    assert r.status_code == 200, r.text
    assert r.json()["token_source"] == "settings"
    assert server.SETTINGS.telegram_token == "abc123"


def test_env_token_is_never_treated_as_encrypted(client, monkeypatch):
    from perun import secrets, server

    # A literal env value starting with "enc:" should be used as-is, not decrypted.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "enc:NOT-A-SECRET")
    server.SETTINGS.telegram_token = ""
    monkeypatch.setattr(server, "_telegram_api", lambda method, **kw: {"ok": True, "result": {}})
    st = client.get("/api/telegram/status").json()
    assert st["configured"] is True
    assert st["token_source"] == "env"
    assert secrets.decrypt_value("enc:NOT-A-SECRET") is None


def test_change_password_re_encrypts_stored_token(client, monkeypatch):
    from perun import secrets, server

    monkeypatch.setattr(secrets, "_password", lambda: None)
    server.SETTINGS.telegram_token = secrets.encrypt_value("abc123", "oldpw")

    r = client.post("/api/telegram/bot/change-password",
                    json={"old_password": "oldpw", "new_password": "newpw"})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True
    assert secrets.decrypt_value(server.SETTINGS.telegram_token, "newpw") == "abc123"
    assert secrets.decrypt_value(server.SETTINGS.telegram_token, "oldpw") is None


def test_change_password_rejects_wrong_old_password(client, monkeypatch):
    from perun import secrets, server

    monkeypatch.setattr(secrets, "_password", lambda: None)
    server.SETTINGS.telegram_token = secrets.encrypt_value("abc123", "oldpw")

    r = client.post("/api/telegram/bot/change-password",
                    json={"old_password": "wrong", "new_password": "newpw"})
    assert r.status_code == 400
    assert "current password is incorrect" in r.json()["detail"]


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


def test_bridge_restart_reloads_in_process_bridge(client, monkeypatch):
    from perun import server, __main__ as main_module

    monkeypatch.setattr(main_module, "reload_in_process_bridge",
                        lambda: {"reloaded": True})
    r = client.post("/api/telegram/bridge/restart")
    assert r.status_code == 200
    assert r.json()["restart"]["reloaded"] is True


def test_unlock_rejects_missing_password(client):
    r = client.post("/api/telegram/unlock", json={})
    assert r.status_code == 400
    assert "password is required" in r.json()["detail"]


def test_unlock_rejects_unencrypted_token(client):
    from perun import server

    server.SETTINGS.telegram_token = "plain-token"
    r = client.post("/api/telegram/unlock", json={"password": "pw"})
    assert r.status_code == 400
    assert "token is not encrypted" in r.json()["detail"]


def test_unlock_rejects_wrong_password(client, monkeypatch):
    from perun import secrets, server

    monkeypatch.setattr(secrets, "_password", lambda: None)
    server.SETTINGS.telegram_token = secrets.encrypt_value("abc123", "rightpw")
    r = client.post("/api/telegram/unlock", json={"password": "wrongpw"})
    assert r.status_code == 400
    assert "incorrect password" in r.json()["detail"]


def test_unlock_decrypts_and_sets_env(client, monkeypatch):
    from perun import secrets, server, __main__ as main_module

    monkeypatch.setattr(secrets, "_password", lambda: None)
    server.SETTINGS.telegram_token = secrets.encrypt_value("abc123", "rightpw")
    monkeypatch.setattr(main_module, "start_or_reload_in_process_bridge",
                        lambda: {"started": True})
    monkeypatch.setattr(secrets, "persist_password", lambda pw: False)

    r = client.post("/api/telegram/unlock",
                    json={"password": "rightpw", "remember": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unlocked"] is True
    assert body["persisted"] is False
    assert body["bridge"]["started"] is True
    assert os.environ.get("PERUN_PASSWORD") == "rightpw"


def test_unlock_remembers_password_when_requested(client, monkeypatch):
    from perun import secrets, server, __main__ as main_module

    monkeypatch.setattr(secrets, "_password", lambda: None)
    server.SETTINGS.telegram_token = secrets.encrypt_value("abc123", "rightpw")
    monkeypatch.setattr(main_module, "start_or_reload_in_process_bridge",
                        lambda: {"started": True})
    persist_calls = []
    monkeypatch.setattr(secrets, "persist_password", lambda pw: persist_calls.append(pw) or True)

    r = client.post("/api/telegram/unlock",
                    json={"password": "rightpw", "remember": True})
    assert r.status_code == 200, r.text
    assert r.json()["persisted"] is True
    assert persist_calls == ["rightpw"]


def test_unlock_reports_reason_when_bridge_lock_held_without_persist(client, monkeypatch):
    from perun import secrets, server, __main__ as main_module, telegram

    monkeypatch.setattr(secrets, "_password", lambda: None)
    server.SETTINGS.telegram_token = secrets.encrypt_value("abc123", "rightpw")
    monkeypatch.setattr(main_module, "start_or_reload_in_process_bridge",
                        lambda: {"started": False})
    monkeypatch.setattr(telegram.PidLock, "is_held", lambda path, name=None: True)
    monkeypatch.setattr(secrets, "persist_password", lambda pw: False)

    r = client.post("/api/telegram/unlock",
                    json={"password": "rightpw", "remember": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["persisted"] is False
    assert "another bridge process owns the lock" in body["bridge"]["reason"]


def test_status_includes_platform(client):
    import sys

    st = client.get("/api/telegram/status").json()
    assert st["platform"] == sys.platform


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



