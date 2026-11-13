"""Local AI: client, model manager, settings, docs fallback, governor.

Every unit here must degrade — a missing runtime, a rejected JSON mode, a failed
download or an active session may never raise into a run.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from perun import config, context_governor, local_llm, model_manager, opencode_config, registry

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Response:
    def __init__(self, body: bytes = b"", status: int = 200, lines: list[bytes] | None = None):
        self._body = body
        self.status = status
        self._lines = list(lines or [])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body

    def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class FakeAPI:
    def __init__(self, *, active: bool = False, tokens: int = 0):
        self.active = active
        self.tokens = tokens
        self.compactions: list[str] = []

    def session_active(self, session_id: str) -> bool:
        return self.active

    def message_page(self, session_id: str, *, limit: int = 50, cursor: str = ""):
        if not self.tokens:
            return [], {}
        return ([{"id": "m1", "tokens": {"input": self.tokens - 200,
                                         "output": 10,
                                         "cache": {"read": 200}}}], {})

    def compact(self, session_id: str) -> dict:
        self.compactions.append(session_id)
        return {}


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def _settings(**overrides) -> config.Settings:
    s = config.Settings()
    s.ollama_host = "http://127.0.0.1:11434"
    s.local_ai_model = "qwen2.5:1.5b"
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def test_base_url_derives_from_ollama_host():
    assert local_llm.base_url(_settings()) == "http://127.0.0.1:11434/v1"
    assert local_llm.base_url(_settings(ollama_host="http://127.0.0.1:11434/v1")) == \
        "http://127.0.0.1:11434/v1"


def test_base_url_custom_endpoint_wins():
    s = _settings(local_ai_base_url="http://127.0.0.1:1234/v1/")
    assert local_llm.base_url(s) == "http://127.0.0.1:1234/v1"


def test_complete_parses_a_chat_completion(monkeypatch):
    payload = json.dumps({"choices": [{"message": {"content": "  ready \n"}}]}).encode()
    seen: list[dict] = []

    def fake_post(body, *, timeout, base):
        seen.append(body)
        return 200, payload

    monkeypatch.setattr(local_llm, "_post", fake_post)
    text = local_llm.complete("hello", system="be terse", settings=_settings())
    assert text == "ready"
    assert seen and seen[0]["model"] == "qwen2.5:1.5b"
    assert seen[0]["messages"][0]["role"] == "system"


def test_complete_returns_empty_on_transport_failure(monkeypatch):
    def boom(body, *, timeout, base):
        raise RuntimeError("offline")

    monkeypatch.setattr(local_llm, "_post", boom)
    assert local_llm.complete("hello", settings=_settings()) == ""


def test_complete_retries_without_json_mode(monkeypatch):
    calls: list[dict] = []
    payload = json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()

    def fake_post(body, *, timeout, base):
        calls.append(body)
        if "response_format" in body:
            return 400, b""
        return 200, payload

    monkeypatch.setattr(local_llm, "_post", fake_post)
    data = local_llm.complete_json("give json", settings=_settings())
    assert data == {"ok": True}
    assert len(calls) == 2 and "response_format" not in calls[-1]


def test_complete_json_extracts_a_fenced_object(monkeypatch):
    payload = json.dumps({"choices": [{"message": {"content": '```json\n{"a": 1}\n```'}}]}).encode()
    monkeypatch.setattr(local_llm, "_post", lambda body, **kw: (200, payload))
    assert local_llm.complete_json("x", settings=_settings()) == {"a": 1}


def test_complete_json_is_empty_when_the_model_is_unusable(monkeypatch):
    monkeypatch.setattr(local_llm, "_post", lambda body, **kw: (503, b""))
    assert local_llm.complete_json("x", settings=_settings()) == {}


def test_probe_reports_latency_and_failure(monkeypatch):
    monkeypatch.setattr(local_llm, "complete", lambda *a, **k: "ready")
    ok = local_llm.probe(_settings())
    assert ok["ok"] is True and ok["reply"] == "ready"
    monkeypatch.setattr(local_llm, "complete", lambda *a, **k: "")
    assert local_llm.probe(_settings())["ok"] is False


# --------------------------------------------------------------------------- #
# Model manager
# --------------------------------------------------------------------------- #
def _tags(models: dict[str, int]) -> bytes:
    return json.dumps({"models": [{"name": n, "size": s} for n, s in models.items()]}).encode()


def test_status_reports_installed_model_and_disk(monkeypatch):
    monkeypatch.setattr(model_manager.urllib.request, "urlopen",
                        lambda *a, **k: _Response(_tags({"qwen2.5:1.5b": 1024**3})))
    st = model_manager.status(_settings())
    assert st["available"] is True
    assert st["installed"] is True
    assert st["size_bytes"] == 1024**3
    assert st["disk_free_bytes"] > 0
    assert st["commands"] == ["ollama pull qwen2.5:1.5b"]


def test_status_when_ollama_is_down(monkeypatch):
    def offline(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(model_manager.urllib.request, "urlopen", offline)
    st = model_manager.status(_settings())
    assert st["available"] is False
    assert st["installed"] is False


def test_start_pull_refuses_without_a_runtime(monkeypatch):
    monkeypatch.setattr(model_manager.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    res = model_manager.start_pull("qwen2.5:1.5b", _settings())
    assert res["ok"] is False
    assert res["reason"] == "ollama_not_running"


def test_start_pull_is_idempotent_for_an_installed_model(monkeypatch):
    monkeypatch.setattr(model_manager.urllib.request, "urlopen",
                        lambda *a, **k: _Response(_tags({"qwen2.5:1.5b": 1})))
    res = model_manager.start_pull("qwen2.5:1.5b", _settings())
    assert res["ok"] is True and res["installed"] is True


def test_start_pull_rejects_a_bad_model_name(monkeypatch):
    res = model_manager.start_pull("bad name!", _settings())
    assert res["ok"] is False
    assert "invalid" in res["reason"]


def test_start_pull_refuses_without_room_on_disk(monkeypatch):
    monkeypatch.setattr(model_manager.urllib.request, "urlopen",
                        lambda *a, **k: _Response(_tags({})))
    monkeypatch.setattr(model_manager, "disk_free_bytes", lambda: 1024**3)
    res = model_manager.start_pull("qwen2.5:1.5b", _settings())
    assert res["ok"] is False
    assert res["reason"] == "not enough disk space"
    assert res["required_bytes"] == model_manager.MIN_FREE_BYTES


def test_cancel_ignores_a_finished_job():
    job = registry.create_local_ai_job("qwen2.5:1.5b", "done")
    res = model_manager.cancel_job(int(job["id"]))
    assert res["cancelled"] is False
    assert int(job["id"]) not in model_manager._CANCELLED


def test_remove_rejects_a_bad_model_name():
    assert model_manager.remove("../etc/passwd")["ok"] is False


def test_pull_progress_is_recorded(monkeypatch):
    lines = [
        json.dumps({"status": "pulling manifest"}).encode() + b"\n",
        json.dumps({"status": "downloading", "completed": 5, "total": 10}).encode() + b"\n",
        json.dumps({"status": "success"}).encode() + b"\n",
    ]

    def fake_open(req, timeout=0):
        url = getattr(req, "full_url", str(req))
        if url.endswith("/api/pull"):
            return _Response(lines=lines)
        return _Response(_tags({"qwen2.5:1.5b": 10}))

    monkeypatch.setattr(model_manager.urllib.request, "urlopen", fake_open)
    job = registry.create_local_ai_job("qwen2.5:1.5b")
    model_manager._run_pull(int(job["id"]), "qwen2.5:1.5b", "http://127.0.0.1:11434")
    row = registry.get_local_ai_job(int(job["id"]))
    assert row["state"] == "done"
    assert row["total"] == 10


# --------------------------------------------------------------------------- #
# Settings validation
# --------------------------------------------------------------------------- #
def test_settings_accept_the_local_ai_fields():
    from perun.server import _coerce_setting

    assert _coerce_setting("local_ai_download", "AUTO") == "auto"
    assert _coerce_setting("local_ai_base_url", " http://127.0.0.1:1234/v1/ ") == \
        "http://127.0.0.1:1234/v1"
    assert _coerce_setting("local_ai_base_url", "") == ""
    assert _coerce_setting("context_governor_tokens", "120000") == 120000
    assert _coerce_setting("local_ai_enabled", "true") is True


def test_settings_reject_invalid_local_ai_values():
    from perun.server import _coerce_setting

    with pytest.raises(HTTPException):
        _coerce_setting("local_ai_download", "sometimes")
    with pytest.raises(HTTPException):
        _coerce_setting("local_ai_base_url", "ftp://example.com")
    with pytest.raises(HTTPException):
        _coerce_setting("context_governor_tokens", 4000)
    with pytest.raises(HTTPException):
        _coerce_setting("context_tool_budget_chars", 100)


# --------------------------------------------------------------------------- #
# Small model (OpenCode config)
# --------------------------------------------------------------------------- #
def test_small_model_is_set_and_removed(monkeypatch, tmp_path):
    target = tmp_path / "opencode.json"
    target.write_text(json.dumps({"model": "opencode/big-pickle"}), encoding="utf-8")
    monkeypatch.setattr(opencode_config, "_config_file", lambda: target)
    monkeypatch.setattr(opencode_config, "reload_opencode",
                        lambda timeout=10.0: {"ok": True})

    res = opencode_config.set_small_model("ollama/qwen2.5:1.5b")
    assert res["ok"] is True
    assert res["changed"] is True
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["small_model"] == "ollama/qwen2.5:1.5b"
    assert data["model"] == "opencode/big-pickle"      # unrelated keys survive
    backups = list(tmp_path.glob("opencode.json.perun-backup-*"))
    assert len(backups) == 1

    again = opencode_config.set_small_model("ollama/qwen2.5:1.5b")
    assert again["ok"] is True and again["changed"] is False
    assert len(list(tmp_path.glob("opencode.json.perun-backup-*"))) == 1

    cleared = opencode_config.set_small_model("")
    assert cleared["changed"] is True
    assert "small_model" not in json.loads(target.read_text(encoding="utf-8"))
    assert len(list(tmp_path.glob("opencode.json.perun-backup-*"))) == 2


# --------------------------------------------------------------------------- #
# Docs fallback
# --------------------------------------------------------------------------- #
def test_docs_fallback_is_used_when_local_ai_is_off():
    from perun import docs_sync

    proposal = {"id": "abc", "intent": "Add a health endpoint", "targets": ["app.py"]}
    line = docs_sync._entry(proposal)
    assert "Add a health endpoint" in line
    assert "`abc`" in line


def test_docs_shadow_keeps_the_heuristic_line(monkeypatch):
    from perun import docs_sync

    monkeypatch.setattr(config.Settings, "load", classmethod(
        lambda cls: _settings(local_ai_enabled=True, local_ai_docs=True,
                              local_ai_shadow=True)))
    monkeypatch.setattr(local_llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(local_llm, "complete", lambda *a, **k: "A generated line")

    proposal = {"id": "abc", "intent": "Add a health endpoint", "targets": ["app.py"]}
    assert "Add a health endpoint" in docs_sync._entry(proposal)


def test_docs_live_uses_the_local_line(monkeypatch):
    from perun import docs_sync

    monkeypatch.setattr(config.Settings, "load", classmethod(
        lambda cls: _settings(local_ai_enabled=True, local_ai_docs=True,
                              local_ai_shadow=False)))
    monkeypatch.setattr(local_llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(local_llm, "complete", lambda *a, **k: "A generated line")

    proposal = {"id": "abc", "intent": "Add a health endpoint", "targets": ["app.py"]}
    line = docs_sync._entry(proposal)
    assert "A generated line" in line
    assert "`abc`" in line


# --------------------------------------------------------------------------- #
# Governor
# --------------------------------------------------------------------------- #
def _session(**overrides):
    row = {"id": 1, "opencode_session_id": "ses_x", "usage_messages": 100,
           "cost_usd": 1.5}
    row.update(overrides)
    return row


def test_live_context_tokens_uses_the_newest_message():
    api = FakeAPI(tokens=1200)
    assert context_governor.live_context_tokens(api, "ses_x") == 1200


def test_governor_skips_below_the_threshold():
    api = FakeAPI(tokens=500)
    s = _settings(context_governor_enabled=True, context_governor_tokens=8000)
    assert context_governor.maybe_compact(api, _session(), s)["skipped"] == "below_threshold"
    assert api.compactions == []


def test_governor_skips_an_active_session():
    api = FakeAPI(active=True, tokens=9000)
    s = _settings(context_governor_enabled=True, context_governor_tokens=8000)
    assert context_governor.maybe_compact(api, _session(), s)["skipped"] == "active"
    assert api.compactions == []


def test_governor_compacts_and_records_the_event():
    api = FakeAPI(tokens=9000)
    s = _settings(context_governor_enabled=True, context_governor_tokens=8000)
    res = context_governor.maybe_compact(api, _session(), s)
    assert res["compacted"] is True
    assert api.compactions == ["ses_x"]
    event = registry.last_context_event(1)
    assert event["tokens_before"] == 9000
    assert event["messages_before"] == 100


def test_governor_respects_the_cooldown():
    api = FakeAPI(tokens=9000)
    s = _settings(context_governor_enabled=True, context_governor_tokens=8000,
                  context_governor_turns=20)
    context_governor.maybe_compact(api, _session(), s)
    res = context_governor.maybe_compact(api, _session(usage_messages=105), s)
    assert res["skipped"] == "cooldown"
    assert api.compactions == ["ses_x"]


def test_governor_disables_itself_when_compaction_does_not_help():
    api = FakeAPI(tokens=9000)
    s = _settings(context_governor_enabled=True, context_governor_tokens=8000,
                  context_governor_turns=0)
    for index in range(3):
        registry.record_context_event(1, "compact", tokens_before=10000,
                                      messages_before=index)
        registry.update_context_event(
            registry.last_context_event(1)["id"], tokens_after=9800)
    res = context_governor.maybe_compact(api, _session(), s)
    assert res.get("disabled") is True
    assert s.context_governor_enabled is False


def test_governor_tick_is_a_no_op_when_disabled(monkeypatch):
    monkeypatch.setattr(context_governor, "_last_check", 0.0)
    s = _settings(context_governor_enabled=False)
    assert context_governor.tick(s, api=FakeAPI())["skipped"] == "disabled"


# --------------------------------------------------------------------------- #
# MCP budget
# --------------------------------------------------------------------------- #
def _mcp_module():
    path = REPO_ROOT / "mcp" / "perun_mcp.py"
    spec = importlib.util.spec_from_file_location("perun_mcp_local_ai", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_truncate_marks_omitted_output():
    mod = _mcp_module()
    text = mod._truncate("x" * 500, 100)
    assert len(text) < 500
    assert "truncated at 100 chars" in text
    assert "read the file for exact content" in text


def test_truncate_leaves_short_output_alone():
    mod = _mcp_module()
    assert mod._truncate("short", 100) == "short"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def test_local_ai_endpoint_reports_status_and_governor(client, monkeypatch):
    monkeypatch.setattr(model_manager, "status", lambda settings=None: {
        "available": True, "endpoint": "http://127.0.0.1:11434",
        "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:1.5b",
        "installed": True, "size_bytes": 10, "disk_free_bytes": 20,
        "policy": "auto", "enabled": True, "shadow": True,
        "small_model": False, "commands": [], "job": None, "busy": False,
    })
    r = client.get("/api/local-ai")
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "qwen2.5:1.5b"
    assert "governor" in body and "budgets" in body


def test_prepare_returns_manual_commands(client, monkeypatch):
    from perun import server

    monkeypatch.setattr(server.SETTINGS, "local_ai_download", "manual")
    monkeypatch.setattr(server.SETTINGS, "local_ai_model", "qwen2.5:1.5b")
    r = client.post("/api/local-ai/prepare", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["manual"] is True
    assert body["commands"] == ["ollama pull qwen2.5:1.5b"]


def test_test_endpoint_returns_the_probe(client, monkeypatch):
    monkeypatch.setattr(local_llm, "probe", lambda settings=None: {
        "ok": True, "latency_ms": 5, "reply": "ready", "model": "m",
        "base_url": "http://127.0.0.1:11434/v1"})
    r = client.post("/api/local-ai/test", json={})
    assert r.status_code == 200 and r.json()["reply"] == "ready"


def test_patch_small_model_updates_opencode(client, monkeypatch):
    from perun import server

    seen: list[str] = []
    monkeypatch.setattr(opencode_config, "set_small_model",
                        lambda model, reload=True: (seen.append(model), {"ok": True})[1])
    for key, value in (("local_ai_base_url", ""),
                       ("local_ai_model", "qwen2.5:1.5b"),
                       ("local_ai_small_model", False)):
        monkeypatch.setattr(server.SETTINGS, key, value)
    r = client.patch("/api/settings", json={"local_ai_small_model": True})
    assert r.status_code == 200
    assert seen == ["ollama/qwen2.5:1.5b"]


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as c:
        yield c
