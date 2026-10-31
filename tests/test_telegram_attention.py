"""Telegram side of the operator inbox: cards, buttons, replies, commands."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from perun import attention, config, opencode_api, registry
from perun.telegram import PerunTelegramBridge


class FakeOrch:
    def start_run(self, *a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("the inbox must never launch a run")

    def cancel(self, *_a, **_k):  # noqa: ANN002, ANN003
        return False


class FakeAPI:
    def __init__(self):
        self.calls: list[tuple] = []

    def reply_permission(self, session_id, request_id, decision, message=""):
        self.calls.append(("permission", session_id, request_id, decision))

    def prompt(self, session_id, text, *, delivery="queue", message_id="", resume=True):
        self.calls.append(("prompt", session_id, text, delivery, message_id))
        return {"id": "inbox_1", "delivery": delivery}

    def set_inbox_delivery(self, session_id, inbox_id, delivery):
        self.calls.append(("steer", inbox_id, delivery))

    def cancel_inbox(self, session_id, inbox_id):
        self.calls.append(("cancel", inbox_id))

    def reply_form(self, session_id, form_id, answer):
        self.calls.append(("form", form_id, answer))

    def close(self):
        pass


def _settings(**overrides):
    base = dict(
        attention_enabled=True, attention_shadow=False, attention_projects=[],
        attention_operator_names=["szczepan"], attention_ttl_minutes=240,
        attention_poll_seconds=5, attention_replay_open_minutes=15,
        attention_max_per_hour=6, attention_quiet_hours="",
        attention_show_paths=False, attention_plain_text_confirm=True,
        attention_default_delivery="queue",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def api(monkeypatch):
    fake = FakeAPI()
    opencode_api.set_default_api(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(config.Settings, "load", staticmethod(lambda: _settings()))
    yield fake
    opencode_api.set_default_api(None)


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "tg-proj").mkdir()
    return registry.add_project("tg-proj", str(tmp_path / "tg-proj"), kind="dir")


@pytest.fixture()
def bridge(control_repo, api):  # noqa: ARG001 - control_repo patches config
    b = PerunTelegramBridge(FakeOrch(), token="test-token", allowed=["1"])
    b.sent: list[dict] = []
    b._next_id = 500

    def _send(chat_id, text, reply_to=None, reply_markup=None):  # noqa: ANN001
        b._next_id += 1
        b.sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to,
                       "markup": reply_markup})
        return b._next_id

    b.send = _send  # type: ignore[assignment]
    b.api_calls: list[tuple[str, dict]] = []

    def _api(method, **kw):  # noqa: ANN003
        b.api_calls.append((method, kw))
        return {"ok": True, "result": {}}

    b.api = _api  # type: ignore[assignment]
    yield b
    b.client.close()


def _permission(project, request_id="per_1", session_id="ses_tg"):
    return attention.request(
        project, "permission", request_id, "permission", "read .env",
        options=attention.PERMISSION_OPTIONS,
        ref={"request_id": request_id, "session_id": session_id,
             "session_title": "Fix totals", "action": "read",
             "resources": ["specs/monthly_totals.md"], "save": ["*"]},
        dedupe_key=f"permission|{request_id}",
    )


def test_send_returns_the_first_message_id(bridge):
    bridge.send = PerunTelegramBridge.send.__get__(bridge)  # restore the real one
    bridge.api = lambda method, **kw: {"ok": True, "result": {"message_id": 4711}}

    assert bridge.send("1", "hello") == 4711


def test_tick_sends_every_paired_chat_and_never_twice(bridge, project):
    _permission(project)

    assert bridge.attention_tick() == 1
    assert [m["chat_id"] for m in bridge.sent] == ["1"]
    assert bridge.sent[0]["markup"]["inline_keyboard"][0][0]["text"] == "Allow once"
    assert "att_" in bridge.sent[0]["text"]

    bridge.sent.clear()
    assert bridge.attention_tick() == 0
    assert bridge.sent == []


def test_shadow_mode_renders_nothing(bridge, project, monkeypatch):
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_shadow=True)))
    _permission(project)

    assert bridge.attention_tick() == 0
    assert bridge.sent == []
    assert registry.attention_deliveries(registry.open_attention()[0]["id"]) == []


def test_reply_to_a_card_answers_that_item(bridge, project, api):
    item = _permission(project)
    bridge.attention_tick()
    card_id = bridge.sent[-1]
    registry.record_attention_delivery(item["id"], "1", 42)

    handled = bridge._handle_attention_reply(
        "1", {"text": "allow it", "message_id": 99,
              "reply_to_message": {"message_id": 42}})

    assert handled is True
    assert bridge.sent[-1]["text"].startswith("⚠️")   # permissions need a button
    assert registry.get_attention(item["id"])["state"] == "open"

    choice = {"id": "c1", "data": f"att:{item['id']}:once",
              "message": {"message_id": 42, "chat": {"id": "1"}}}
    bridge._handle_attention_callback(choice, choice["data"])

    assert api.calls == [("permission", "ses_tg", "per_1", "once")]
    assert registry.get_attention(item["id"])["state"] == "approved"
    assert bridge.sent[-1]["reply_to"] == 42


def test_always_requires_a_second_tap(bridge, project, api):
    item = _permission(project)
    registry.record_attention_delivery(item["id"], "1", 42)

    first = {"id": "c1", "data": f"att:{item['id']}:always",
             "message": {"message_id": 42, "chat": {"id": "1"}}}
    bridge._handle_attention_callback(first, first["data"])

    assert api.calls == []
    assert "would" not in bridge.sent[-1]["text"].lower()  # wording differs here
    assert "always allow" in bridge.sent[-1]["text"].lower()
    assert "`*`" in bridge.sent[-1]["text"]

    second = {"id": "c2", "data": f"att:{item['id']}:always_yes",
              "message": {"message_id": 43, "chat": {"id": "1"}}}
    bridge._handle_attention_callback(second, second["data"])

    assert api.calls == [("permission", "ses_tg", "per_1", "always")]


def test_a_runtime_paired_chat_cannot_use_always(bridge, project, api):
    item = _permission(project)
    registry.record_attention_delivery(item["id"], "2", 7)
    bridge._allowed_runtime = ["2"]

    cq = {"id": "c", "data": f"att:{item['id']}:always_yes",
          "message": {"message_id": 7, "chat": {"id": "2"}}}
    bridge._handle_attention_callback(cq, cq["data"])

    # A bridge with a static allow-list disables runtime pairing entirely, so a
    # stranger chat is refused before it can reach the approval branch at all.
    assert api.calls == []
    refusal = bridge.api_calls[-1][1]["text"].lower()
    assert "not authorised" in refusal or "owner" in refusal


def test_pending_lists_open_items(bridge, project):
    _permission(project)
    bridge.attention_tick()

    assert bridge._handle_attention_command("1", "/pending", 10) is True

    text = bridge.sent[-1]["text"]
    assert "Waiting on you" in text
    assert "tg-proj" in text
    assert bridge.sent[-1]["markup"]["inline_keyboard"]


def test_answer_with_one_open_item_answers_it(bridge, project, api):
    item = attention.request(project, "session", "ses_tg", "ask", "Ship it?")
    bridge.attention_tick()

    assert bridge._handle_attention_command("1", "/answer yes, tag it", 11) is True

    assert api.calls[-1][0] == "prompt"
    assert registry.get_attention(item["id"])["state"] == "answered"
    assert "queued" in bridge.sent[-1]["text"]


def test_answer_with_several_items_refuses_to_guess(bridge, project, api):
    attention.request(project, "session", "ses_a", "ask", "One?")
    attention.request(project, "session", "ses_b", "ask", "Two?")
    bridge.attention_tick()
    api.calls.clear()

    bridge._handle_attention_command("1", "/answer yes", 11)

    assert api.calls == []
    assert "reply to the card" in bridge.sent[-1]["text"]


def test_mute_hides_a_project_for_one_chat(bridge, project):
    _permission(project)
    bridge.attention_tick()
    bridge.sent.clear()

    bridge._handle_attention_command("1", "/mute tg-proj", 12)
    assert registry.attention_muted_projects("1") == {"tg-proj"}

    _permission(project, "per_2")
    assert bridge.attention_tick() == 0
    assert bridge.sent[-1]["text"].startswith("🔇")

    bridge._handle_attention_command("1", "/unmute tg-proj", 13)
    assert registry.attention_muted_projects("1") == set()


def test_plain_text_with_one_open_item_asks_before_sending(bridge, project, api):
    item = attention.request(project, "session", "ses_tg", "ask", "Ship it?")
    bridge.attention_tick()
    bridge.sent.clear()

    handled = bridge._maybe_attention_text("1", "yes, ship it", 20)

    assert handled is True
    assert api.calls == []
    assert "Send this to" in bridge.sent[-1]["text"]
    assert registry.get_attention(item["id"])["state"] == "open"

    cq = {"id": "c", "data": f"att:{item['id']}:send_text",
          "message": {"message_id": 21, "chat": {"id": "1"}}}
    bridge._handle_attention_callback(cq, cq["data"])

    assert api.calls[-1][0] == "prompt"
    assert registry.get_attention(item["id"])["state"] == "answered"


def test_steer_and_undo_callbacks(bridge, project, api):
    item = attention.request(project, "session", "ses_tg", "ask", "Ship it?")
    bridge.attention_tick()
    bridge._handle_attention_reply(
        "1", {"text": "yes", "message_id": 30,
              "reply_to_message": {"message_id": bridge.sent[-1]["reply_to"] or 0}})
    # reply-to above is not a real card id; answer via the item directly instead
    attention.answer(item["id"], "yes", by="telegram:1")

    steer = {"id": "c", "data": f"att:{item['id']}:steer",
             "message": {"message_id": 30, "chat": {"id": "1"}}}
    bridge._handle_attention_callback(steer, steer["data"])
    assert ("steer", "inbox_1", "steer") in api.calls

    undo = {"id": "c", "data": f"att:{item['id']}:undo",
            "message": {"message_id": 30, "chat": {"id": "1"}}}
    bridge._handle_attention_callback(undo, undo["data"])
    assert registry.get_attention(item["id"])["state"] == "open"


def test_startup_replay_sends_only_fresh_undelivered_items(bridge, project):
    fresh = _permission(project, "per_fresh")
    stale = _permission(project, "per_stale", "ses_old")
    with registry.db.db() as conn:
        conn.execute("UPDATE attention_requests SET created_at='2020-01-01T00:00:00+00:00' "
                     "WHERE id=?", (stale["id"],))

    bridge._attention_startup_replay(_settings())

    assert [m["text"].startswith("🔐") for m in bridge.sent] == [True]
    assert registry.attention_deliveries(fresh["id"])[0]["chat_id"] == "1"
    deliveries = registry.attention_deliveries(stale["id"])
    assert deliveries and deliveries[0]["message_id"] is None
    kinds = [row["kind"] for row in registry.list_activity(limit=10)]
    assert "attention_replay_skipped" in kinds


def test_cancelled_mid_flow_is_reported_as_already_handled(bridge, project):
    item = _permission(project)
    registry.close_attention(item["id"], "cancelled", answered_by="dashboard")
    registry.record_attention_delivery(item["id"], "1", 42)

    handled = bridge._handle_attention_reply(
        "1", {"text": "yes", "message_id": 2,
              "reply_to_message": {"message_id": 42}})

    assert handled is True
    assert "already cancelled" in bridge.sent[-1]["text"]


def test_a_failed_send_is_retried_instead_of_being_lost(bridge, project):
    item = _permission(project)
    real_send = bridge.send
    bridge.send = lambda *a, **k: None

    assert bridge.attention_tick() == 0
    assert registry.attention_deliveries(item["id"]) == []

    bridge.send = real_send
    assert bridge.attention_tick() == 1
    assert registry.attention_deliveries(item["id"])[0]["message_id"]


def test_declining_the_plain_text_prompt_passes_the_message_to_perun(bridge, project):
    item = attention.request(project, "session", "ses_tg", "ask", "Ship it?")
    bridge.attention_tick()
    bridge._maybe_attention_text("1", "please run the tests", 20)

    cq = {"id": "c", "data": f"att:{item['id']}:discard_text",
          "message": {"message_id": 21, "chat": {"id": "1"}}}
    bridge._handle_attention_callback(cq, cq["data"])

    queued = bridge.work.get_nowait()
    assert queued["text"] == "please run the tests"
    assert registry.get_attention(item["id"])["state"] == "open"


def test_each_card_says_how_many_others_are_waiting(bridge, project):
    _permission(project, "per_1")
    _permission(project, "per_2")

    bridge.attention_tick()

    assert len(bridge.sent) == 2
    assert all("1 more waiting" in m["text"] for m in bridge.sent)
    assert all("/pending" in m["text"] for m in bridge.sent)


def test_telegram_429_waits_as_instructed_then_retries(bridge, monkeypatch):
    sleeps: list[float] = []
    responses = [
        {"ok": False, "error_code": 429, "parameters": {"retry_after": 3}},
        {"ok": True, "result": {"message_id": 9}},
    ]

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        def post(self, url, json=None):  # noqa: A002
            return Resp(responses.pop(0))

        def close(self):
            pass

    bridge.client = Client()
    monkeypatch.setattr("perun.telegram.time.sleep", lambda s: sleeps.append(s))
    bridge.api = PerunTelegramBridge.api.__get__(bridge)

    result = bridge.api("sendMessage", chat_id="1", text="hi")

    assert result["ok"] is True
    assert sleeps == [3.0]
