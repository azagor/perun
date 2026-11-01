"""Telegram side of the operator inbox: cards, buttons, replies, commands."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from perun import attention, config, opencode_api, registry, telegram
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


def _last_card_text(bridge) -> str:
    """Last text the operator would see: an edit wins over an earlier send."""
    for method, kw in reversed(bridge.api_calls):
        if method in ("editMessageText", "sendMessage"):
            return str(kw.get("text") or "")
    return bridge.sent[-1]["text"] if bridge.sent else ""


def _last_edit(bridge) -> dict:
    for method, kw in reversed(bridge.api_calls):
        if method == "editMessageText":
            return kw
    return {}


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


def test_reply_to_a_card_rejects_a_permission_with_feedback(bridge, project, api):
    """The card promises "reply with feedback to reject"; deliver exactly that."""
    item = _permission(project)
    bridge.attention_tick()
    registry.record_attention_delivery(item["id"], "1", 42)

    handled = bridge._handle_attention_reply(
        "1", {"text": "not in this repo", "message_id": 99,
              "reply_to_message": {"message_id": 42}})

    assert handled is True
    assert api.calls == [("permission", "ses_tg", "per_1", "reject")]
    assert registry.get_attention(item["id"])["state"] == "rejected"
    assert registry.get_attention(item["id"])["answer"] == "not in this repo"


def test_a_tap_on_the_card_approves_it(bridge, project, api):
    item = _permission(project)
    bridge.attention_tick()
    registry.record_attention_delivery(item["id"], "1", 42)

    choice = {"id": "c1", "data": f"att:{item['id']}:once",
              "message": {"message_id": 42, "chat": {"id": "1"}}}
    bridge._handle_attention_callback(choice, choice["data"])

    assert api.calls == [("permission", "ses_tg", "per_1", "once")]
    assert registry.get_attention(item["id"])["state"] == "approved"
    last = _last_edit(bridge)
    assert last.get("message_id") == 42
    assert "approved" in last.get("text", "").lower()


def test_always_requires_a_second_tap(bridge, project, api):
    item = _permission(project)
    registry.record_attention_delivery(item["id"], "1", 42)

    first = {"id": "c1", "data": f"att:{item['id']}:always",
             "message": {"message_id": 42, "chat": {"id": "1"}}}
    bridge._handle_attention_callback(first, first["data"])

    assert api.calls == []
    confirm = _last_card_text(bridge).lower()
    assert "always allow" in confirm
    assert "<code>*</code>" in confirm

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


def test_a_multiselect_form_is_ticked_and_sent_with_buttons(bridge, project, api):
    item = attention.request(
        project, "form", "ses_form", "form", "Pick targets",
        ref={"form_id": "frm_1", "session_id": "ses_form",
             "fields": [{"key": "envs", "type": "multiselect",
                         "options": ["dev", "prod"]}]})

    def cq(action):  # noqa: ANN001
        return {"id": "c", "data": f"att:{item['id']}:{action}",
                "message": {"message_id": 42, "chat": {"id": "1"}}}

    bridge._handle_attention_callback(cq("t1"), cq("t1")["data"])

    assert api.calls == []                       # a tick never sends anything
    assert registry.get_attention(item["id"])["state"] == "open"
    assert registry.get_attention(item["id"])["ref"]["toggles"] == [1]
    labels = [row[0]["text"] for row in
              _last_edit(bridge)["reply_markup"]["inline_keyboard"]]
    assert labels[0].startswith("⬜") and labels[1].startswith("✅ prod")
    assert "Selected: prod" in _last_card_text(bridge)

    bridge._handle_attention_callback(cq("d"), cq("d")["data"])

    assert api.calls[-1] == ("form", "frm_1", {"envs": ["prod"]})
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


def test_a_button_tap_through_the_real_dispatch_answers_the_item(bridge, project, api):
    """Regression: ``att:`` callbacks must reach the handler via _handle_update.

    The unit tests called ``_handle_attention_callback`` directly, so a missing
    dispatch in ``_handle_callback`` went unnoticed and every Telegram button
    tap was silently dropped.
    """
    item = _permission(project)
    registry.record_attention_delivery(item["id"], "1", 42)

    bridge._handle_update({"update_id": 1, "callback_query": {
        "id": "cb-1", "data": f"att:{item['id']}:once",
        "message": {"message_id": 42, "chat": {"id": "1"}},
    }})

    assert api.calls == [("permission", "ses_tg", "per_1", "once")]
    assert registry.get_attention(item["id"])["state"] == "approved"
    # Acknowledged immediately, then the card is edited with the outcome.
    assert any(method == "answerCallbackQuery" for method, _ in bridge.api_calls)
    assert any(method == "editMessageText" for method, _ in bridge.api_calls)


def test_a_tap_on_a_stale_card_reports_the_state_and_does_nothing(bridge, project, api):
    item = _permission(project)
    registry.close_attention(item["id"], "rejected", answered_by="dashboard")
    registry.record_attention_delivery(item["id"], "1", 42)

    bridge._handle_update({"update_id": 2, "callback_query": {
        "id": "cb-2", "data": f"att:{item['id']}:once",
        "message": {"message_id": 42, "chat": {"id": "1"}},
    }})

    assert api.calls == []
    assert "already rejected" in bridge.api_calls[-1][1]["text"].lower()


def test_project_buttons_still_work_after_the_attention_dispatch(bridge, project):
    bridge.api_calls.clear()

    bridge._handle_callback({"callback_query": {
        "id": "cb-3", "data": f"project:{project['slug']}",
        "message": {"message_id": 5, "chat": {"id": "1"}},
    }})

    assert any(method == "answerCallbackQuery" for method, _ in bridge.api_calls)
    assert registry.get_bridge_link("1")["project_id"] == project["id"]


def test_a_malformed_button_payload_is_acknowledged(bridge):
    bridge._handle_update({"update_id": 9, "callback_query": {
        "id": "cb-bad", "data": "att:not-an-id:once",
        "message": {"message_id": 1, "chat": {"id": "1"}},
    }})

    assert bridge.api_calls[-1][0] == "answerCallbackQuery"
    assert "not valid" in bridge.api_calls[-1][1]["text"].lower()


def test_a_stale_tap_drops_the_cached_draft(bridge, project):
    item = attention.request(project, "session", "ses_tg", "ask", "Ship it?")
    bridge.attention_tick()
    bridge._maybe_attention_text("1", "yes please", 20)
    assert bridge._pending_answers

    registry.close_attention(item["id"], "answered", answered_by="dashboard")
    bridge._handle_update({"update_id": 10, "callback_query": {
        "id": "cb-stale", "data": f"att:{item['id']}:send_text",
        "message": {"message_id": 21, "chat": {"id": "1"}},
    }})

    assert bridge._pending_answers == {}
    assert bridge.work.empty()
    assert "already answered" in bridge.api_calls[-1][1]["text"].lower()


def test_the_reply_drainer_delivers_a_finished_run_once(bridge, project):
    from perun import registry

    session = registry.create_session(project["id"], "ses_reply", title="reply test")
    registry.set_bridge_link("1", project["id"], session["id"])
    run = registry.create_run("run_reply", project["id"], "hi", session_pk=session["id"])
    registry.update_run("run_reply", status="done", ended_at=registry.now_iso(),
                        output="the delivered reply", opencode_session_id="ses_reply")
    bridge.sent.clear()

    assert bridge._deliver_pending_replies() == 1
    assert [m["text"] for m in bridge.sent] == ["the delivered reply"]
    assert registry.get_run("run_reply")["reply_sent_at"]

    assert bridge._deliver_pending_replies() == 0
    assert len(bridge.sent) == 1


def test_the_reply_drainer_reports_an_empty_run_before_marking_it(bridge, project):
    from perun import registry

    session = registry.create_session(project["id"], "ses_empty", title="empty")
    registry.set_bridge_link("1", project["id"], session["id"])
    registry.create_run("run_empty", project["id"], "hi", session_pk=session["id"])
    registry.update_run("run_empty", status="failed", ended_at=registry.now_iso(),
                        output="", error="boom", opencode_session_id="ses_empty")
    bridge.sent.clear()

    assert bridge._deliver_pending_replies() == 1
    assert "failed" in bridge.sent[-1]["text"].lower()
    assert "boom" in bridge.sent[-1]["text"]

def test_the_poll_becomes_the_reconciliation_net_when_events_are_on():
    """A stored 5 s value (the old default) must not keep polling every 5 s."""
    from types import SimpleNamespace

    from perun.telegram import PerunTelegramBridge

    five = SimpleNamespace(attention_poll_seconds=5)
    assert PerunTelegramBridge.attention_interval(five, True) == 60
    # Without events, detection is the poll: keep the old fast cadence.
    assert PerunTelegramBridge.attention_interval(five, False) == 5
    # A slower explicit value is honoured in event mode.
    slow = SimpleNamespace(attention_poll_seconds=300)
    assert PerunTelegramBridge.attention_interval(slow, True) == 300
    assert PerunTelegramBridge.attention_interval(slow, False) == 5
    # Nonsense values never disable the timer or spin it.
    assert PerunTelegramBridge.attention_interval(SimpleNamespace(), True) == 60
    assert PerunTelegramBridge.attention_interval(
        SimpleNamespace(attention_poll_seconds=0), False) == 5

# --- Digest: informational notices are coalesced --------------------------- #

def _room_item(project, message_id, text="can someone explain the totals?"):
    from perun import attention as _attention

    return _attention.request(
        project, "room", message_id, "room", text,
        ref={"room_id": "grp:perun:tg-proj:chat", "message_id": message_id,
             "session_title": "tg-proj", "location": str(project["path"])},
        dedupe_key=f"room|grp:perun:tg-proj:chat|{message_id}",
    )


def test_a_burst_of_room_messages_waits_and_then_grows_one_card(bridge, project, monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(telegram.time, "time", lambda: clock["now"])
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=30)))
    first = _room_item(project, "msg_1", "what is the status of the deploy?")
    second = _room_item(project, "msg_2", "and the totals?")

    # Inside the window nothing goes out, but the items are held, not lost.
    assert bridge.attention_tick() == 0
    assert bridge.sent == []
    assert first["id"] in bridge._digest_pending
    assert second["id"] in bridge._digest_pending

    clock["now"] += 31
    assert bridge.attention_tick() == 1
    assert len(bridge.sent) == 1
    card = bridge.sent[0]
    assert "2 waiting" in card["text"]
    assert f"att_{first['id']}" in card["text"] and f"att_{second['id']}" in card["text"]
    buttons = [row[0]["callback_data"] for row in card["markup"]["inline_keyboard"]]
    assert buttons == [f"att:{first['id']}:show", f"att:{second['id']}:show"]

    # Neither item is re-sent as its own card afterwards.
    bridge.sent.clear()
    clock["now"] += 60
    assert bridge.attention_tick() == 0
    assert bridge.sent == []


def test_a_lone_room_message_still_arrives_after_the_window(bridge, project, monkeypatch):
    clock = {"now": 5_000.0}
    monkeypatch.setattr(telegram.time, "time", lambda: clock["now"])
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=30)))
    _room_item(project, "msg_lone")

    assert bridge.attention_tick() == 0
    clock["now"] += 30
    assert bridge.attention_tick() == 1
    assert len(bridge.sent) == 1
    assert "1 waiting" in bridge.sent[0]["text"]


def test_a_permission_is_never_digested(bridge, project, monkeypatch):
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=30)))
    _permission(project)

    assert bridge.attention_tick() == 1
    assert len(bridge.sent) == 1
    assert bridge.sent[0]["markup"]["inline_keyboard"][0][0]["text"] == "Allow once"


def test_a_digest_keeps_the_items_past_the_fifth(bridge, project, monkeypatch):
    """Items six and beyond were marked shown but never listed anywhere."""
    clock = {"now": 50_000.0}
    monkeypatch.setattr(telegram.time, "time", lambda: clock["now"])
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=30)))
    items = [_room_item(project, f"msg_{i}", f"question {i}") for i in range(6)]

    assert bridge.attention_tick() == 0
    clock["now"] += 31
    assert bridge.attention_tick() == 1

    card = bridge.sent[0]
    assert "6 waiting" in card["text"]
    listed = [it for it in items if it["id"] in bridge._digested]
    rest = [it for it in items if it["id"] not in bridge._digested]
    assert len(listed) == 5 and len(rest) == 1
    assert rest[0]["id"] in bridge._digest_pending

    # The second window delivers the remainder instead of losing it.
    bridge.sent.clear()
    clock["now"] += 31
    assert bridge.attention_tick() == 1
    assert "1 waiting" in bridge.sent[0]["text"]
    assert f"att_{rest[0]['id']}" in bridge.sent[0]["text"]


def test_a_partial_digest_retries_only_the_chat_that_missed_it(bridge, project, monkeypatch):
    clock = {"now": 70_000.0}
    monkeypatch.setattr(telegram.time, "time", lambda: clock["now"])
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=30)))
    item = _room_item(project, "msg_partial")
    bridge.allowed = ["1", "2"]
    real_send = bridge.send
    failing = {"2"}

    def send(chat_id, text, reply_to=None, reply_markup=None):  # noqa: ANN001
        if chat_id in failing:
            return None
        return real_send(chat_id, text, reply_to=reply_to, reply_markup=reply_markup)

    bridge.send = send

    assert bridge.attention_tick() == 0
    clock["now"] += 31
    assert bridge.attention_tick() == 1                 # only chat 1
    assert registry.attention_delivered_to(item["id"], "1") is True
    assert registry.attention_delivered_to(item["id"], "2") is False
    # Still queued, so the chat that missed it can be served next window.
    assert item["id"] not in bridge._digested

    failing.clear()
    clock["now"] += 31
    assert bridge.attention_tick() == 1
    assert registry.attention_delivered_to(item["id"], "2") is True
    assert item["id"] in bridge._digested


def test_digest_zero_sends_room_messages_immediately(bridge, project, monkeypatch):
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=0)))
    _room_item(project, "msg_now")

    assert bridge.attention_tick() == 1
    assert len(bridge.sent) == 1
    assert "att_" in bridge.sent[0]["text"]


def test_opening_an_item_from_a_digest_posts_a_replyable_card(bridge, project, monkeypatch):
    clock = {"now": 9_000.0}
    monkeypatch.setattr(telegram.time, "time", lambda: clock["now"])
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=30)))
    item = _room_item(project, "msg_open")
    bridge.attention_tick()
    clock["now"] += 31
    bridge.attention_tick()
    digest_message_id = bridge._next_id
    bridge.sent.clear()

    bridge._handle_update({"update_id": 11, "callback_query": {
        "id": "cb-show", "data": f"att:{item['id']}:show",
        "message": {"message_id": digest_message_id, "chat": {"id": "1"}},
    }})

    # A new card (not an edit of the list), recorded so a reply-to maps to it.
    assert len(bridge.sent) == 1
    new_id = bridge.sent[0]
    assert "att_" in new_id["text"]
    assert registry.attention_delivered_message(item["id"], "1") == bridge._next_id
    assert registry.attention_by_message("1", bridge._next_id)["id"] == item["id"]


def test_shadow_mode_logs_the_digest_without_sending(bridge, project, monkeypatch):
    clock = {"now": 12_000.0}
    monkeypatch.setattr(telegram.time, "time", lambda: clock["now"])
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=30,
                                                       attention_shadow=True)))
    _room_item(project, "msg_shadow")

    bridge.attention_tick()
    clock["now"] += 31
    assert bridge.attention_tick() == 0
    assert bridge.sent == []

def test_a_digest_redacts_credentials_like_a_single_card(bridge, project, monkeypatch):
    clock = {"now": 20_000.0}
    monkeypatch.setattr(telegram.time, "time", lambda: clock["now"])
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=30)))
    _room_item(project, "msg_secret",
               "please check api_key=sk-live-abcdef123456 in the log")

    bridge.attention_tick()
    clock["now"] += 31
    bridge.attention_tick()

    text = bridge.sent[0]["text"]
    assert "sk-live-abcdef123456" not in text
    assert "[redacted]" in text

def test_a_digested_item_is_not_replayed_after_a_restart(bridge, project, monkeypatch):
    """The in-memory set dies with the process; the activity marker must not."""
    clock = {"now": 30_000.0}
    monkeypatch.setattr(telegram.time, "time", lambda: clock["now"])
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=30)))
    item = _room_item(project, "msg_restart")
    bridge.attention_tick()
    clock["now"] += 31
    assert bridge.attention_tick() == 1
    assert registry.activity_exists(
        "attention_digest", f"att_{item['id']} listed in a digest")

    # A fresh process: no memory of the digest, the item still has no delivery row.
    fresh = PerunTelegramBridge(FakeOrch(), token="test-token", allowed=["1"])
    fresh.sent = []
    fresh.send = lambda chat_id, text, reply_to=None, reply_markup=None: fresh.sent.append(
        {"chat_id": chat_id, "text": text}) or 900
    fresh.api = lambda method, **kw: {"ok": True, "result": {}}
    fresh.attention_tick(replay=True)

    assert fresh.sent == []


def test_the_poll_interval_survives_a_hand_edited_value():
    from types import SimpleNamespace

    from perun.telegram import PerunTelegramBridge

    assert PerunTelegramBridge.attention_interval(
        SimpleNamespace(attention_poll_seconds="not-a-number"), True) == 60
    assert PerunTelegramBridge.attention_interval(
        SimpleNamespace(attention_poll_seconds="not-a-number"), False) == 5
    assert PerunTelegramBridge.attention_interval(
        SimpleNamespace(attention_poll_seconds=None), True) == 60

def _two_chat_bridge(failing):
    """A bridge paired with two chats; ``failing`` is the chat id that errors."""
    from perun.telegram import PerunTelegramBridge

    bridge = PerunTelegramBridge(FakeOrch(), token="test-token", allowed=["1", "2"])
    bridge.sent = []

    def _send(chat_id, text, reply_to=None, reply_markup=None):
        if str(chat_id) == failing:
            return 0
        bridge.sent.append({"chat_id": str(chat_id), "text": text})
        return 700 + len(bridge.sent)

    bridge.send = _send
    bridge.api = lambda method, **kw: {"ok": True, "result": {}}
    return bridge


def test_a_partial_digest_failure_does_not_relist_the_items(project, monkeypatch):
    """One chat getting the card is enough; a failed chat must not cause a repeat."""
    clock = {"now": 40_000.0}
    monkeypatch.setattr(telegram.time, "time", lambda: clock["now"])
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=30)))
    bridge = _two_chat_bridge(failing="2")
    item = _room_item(project, "msg_partial")

    bridge.attention_tick()
    clock["now"] += 31
    assert bridge.attention_tick() == 1
    assert [m["chat_id"] for m in bridge.sent] == ["1"]
    assert registry.activity_exists(
        "attention_digest", f"att_{item['id']} listed in a digest")

    # The card is not listed again, on either chat.
    clock["now"] += 60
    assert bridge.attention_tick() == 0
    assert [m["chat_id"] for m in bridge.sent] == ["1"]


def test_a_digest_no_chat_received_is_retried(project, monkeypatch):
    """Total failure must keep the items and leave no "shown" marker."""
    clock = {"now": 50_000.0}
    monkeypatch.setattr(telegram.time, "time", lambda: clock["now"])
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_digest_seconds=30)))
    bridge = _two_chat_bridge(failing="1")
    bridge.send = lambda *a, **k: 0          # every chat refuses
    item = _room_item(project, "msg_none")

    bridge.attention_tick()
    clock["now"] += 31
    assert bridge.attention_tick() == 0
    assert not registry.activity_exists(
        "attention_digest", f"att_{item['id']} listed in a digest")
    assert item["id"] in bridge._digest_pending

    # A working send in the next window delivers it.
    def _send(chat_id, text, reply_to=None, reply_markup=None):
        bridge.sent.append({"chat_id": str(chat_id), "text": text})
        return 800

    bridge.send = _send
    clock["now"] += 31
    assert bridge.attention_tick() == 2     # both chats
    assert {m["chat_id"] for m in bridge.sent} == {"1", "2"}
