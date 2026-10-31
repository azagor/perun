"""Operator inbox: typed targets, delivery adapters, policy, and the reply side."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from perun import attention, opencode_api, registry


class FakeAPI:
    """Records instead of calling OpenCode; enough surface for every adapter."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.permissions: list[dict] = []
        self.forms: list[dict] = []
        self.active: dict[str, dict] = {}
        self.locations_list: list[str] = []
        self.sessions: dict[str, dict] = {}
        self.fail: Exception | None = None
        self.permission_error: Exception | None = None

    # transport surface used by the watcher
    def session(self, session_id: str) -> dict:
        return self.sessions.get(session_id, {})

    def active_sessions(self) -> dict:
        return dict(self.active)

    def locations(self) -> list[str]:
        return list(self.locations_list)

    def pending_permissions(self, location: str) -> list[dict]:
        return [p for p in self.permissions if p.get("_location", location) == location]

    def pending_forms(self, location: str) -> list[dict]:
        return [f for f in self.forms if f.get("_location", location) == location]

    # write surface used by the adapters
    def reply_permission(self, session_id, request_id, decision, message=""):
        if self.permission_error:
            raise self.permission_error
        self.calls.append(("permission", session_id, request_id, decision, message))

    def reply_form(self, session_id, form_id, answer):
        self.calls.append(("form", session_id, form_id, answer))

    def prompt(self, session_id, text, *, delivery="queue", message_id="", resume=True):
        if self.fail:
            raise self.fail
        self.calls.append(("prompt", session_id, text, delivery, message_id))
        return {"id": f"inbox_{len(self.calls)}", "delivery": delivery}

    def set_inbox_delivery(self, session_id, inbox_id, delivery):
        self.calls.append(("steer", session_id, inbox_id, delivery))

    def cancel_inbox(self, session_id, inbox_id):
        self.calls.append(("cancel", session_id, inbox_id))

    def close(self) -> None:
        pass


def _settings(**overrides):
    base = dict(
        attention_enabled=True,
        attention_shadow=False,
        attention_projects=[],
        attention_operator_names=[],
        attention_ttl_minutes=240,
        attention_poll_seconds=5,
        attention_replay_open_minutes=15,
        attention_max_per_hour=6,
        attention_quiet_hours="",
        attention_show_paths=False,
        attention_plain_text_confirm=True,
        attention_default_delivery="queue",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def api():
    fake = FakeAPI()
    opencode_api.set_default_api(fake)  # type: ignore[arg-type]
    yield fake
    opencode_api.set_default_api(None)


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "inbox-proj").mkdir()
    return registry.add_project("inbox-proj", str(tmp_path / "inbox-proj"), kind="dir")


def _permission(project, request_id="per_1", session_id="ses_a"):
    return attention.request(
        project, "permission", request_id, "permission", "read .env",
        options=attention.PERMISSION_OPTIONS,
        ref={
            "request_id": request_id,
            "session_id": session_id,
            "session_title": "Fix totals",
            "action": "read",
            "resources": [r"C:\my_apps\inbox-proj\.env"],
            "save": ["*"],
        },
        dedupe_key=f"permission|{request_id}",
    )


def test_request_dedupes_open_items(project):
    first = _permission(project, "per_dup")
    second = _permission(project, "per_dup")

    assert first and second
    assert first["id"] == second["id"]
    assert [i["id"] for i in registry.open_attention()] == [first["id"]]


def test_permission_card_shows_the_saved_pattern_and_hides_absolute_paths(project):
    item = _permission(project)

    text = attention.render(item)

    assert "read" in text
    assert ".env" in text
    assert "C:\\my_apps" not in text          # path redacted by default
    assert "would save" in text and "`*`" in text

    shown = attention.render(item, show_paths=True)
    assert "C:\\my_apps\\inbox-proj\\.env" in shown


def test_permission_keyboard_carries_all_three_choices(project):
    item = _permission(project)
    rows = attention.keyboard(item)["inline_keyboard"]

    ids = [row[0]["callback_data"].rsplit(":", 1)[-1] for row in rows]
    assert ids == ["once", "always", "reject"]
    assert all(len(row[0]["callback_data"]) <= 64 for row in rows)


def test_choose_permission_replies_and_closes(project, api):
    item = _permission(project)

    result = attention.choose(item["id"], "once", by="telegram:1")

    assert result["ok"], result
    assert api.calls == [("permission", "ses_a", "per_1", "once", "")]
    assert result["item"]["state"] == "approved"
    assert result["item"]["answered_by"] == "telegram:1"


def test_reject_closes_other_requests_in_the_same_session(project, api):
    first = _permission(project, "per_1")
    second = _permission(project, "per_2")

    result = attention.choose(first["id"], "reject", by="telegram:1")

    assert result["ok"]
    assert registry.get_attention(second["id"])["state"] == "cancelled"


def test_answer_to_session_prompts_with_an_idempotent_message_id(project, api):
    item = attention.request(
        project, "session", "ses_live", "ask", "Ship it?",
        ref={"session_id": "ses_live", "session_title": "Release"},
    )

    result = attention.answer(item["id"], "yes, tag it", by="telegram:1")

    assert result["ok"], result
    kind, session_id, text, delivery, message_id = api.calls[0]
    assert (kind, session_id, text, delivery) == ("prompt", "ses_live", "yes, tag it", "queue")
    assert message_id == f"msg_att_{item['id']}"
    assert result["delivery"]["mode"] == "prompt"
    assert registry.get_attention(item["id"])["ref"]["inbox_id"].startswith("inbox_")


def test_answer_to_a_live_waiter_does_not_inject_a_prompt(project, api):
    item = attention.request(project, "session", "ses_live", "ask", "Ship it?")
    registry.touch_attention_waiter(item["id"])
    api.calls.clear()

    result = attention.answer(item["id"], "yes", by="telegram:1")

    assert result["ok"]
    assert result["delivery"]["mode"] == "tool"
    assert api.calls == []
    assert registry.get_attention(item["id"])["state"] == "answered"


def test_waiter_returns_the_answer_written_by_another_thread(project, api):
    item = attention.request(project, "session", "ses_live", "ask", "Ship it?")

    def reply() -> None:
        time.sleep(0.3)
        attention.answer(item["id"], "go", by="telegram:1")

    thread = threading.Thread(target=reply)
    thread.start()
    answer = attention.wait(item["id"], 3.0, poll=0.1)
    thread.join(timeout=2)

    assert answer == "go"


def test_answer_failure_keeps_the_item_open(project, api):
    item = attention.request(project, "session", "ses_live", "ask", "Ship it?")
    api.fail = opencode_api.OpenCodeUnavailable("service down")

    result = attention.answer(item["id"], "yes", by="telegram:1")

    assert result["ok"] is False
    assert "opencode unavailable" in result["error"]
    assert registry.get_attention(item["id"])["state"] == "open"


def test_set_delivery_promotes_a_queued_answer(project, api):
    item = attention.request(project, "session", "ses_live", "ask", "Ship it?")
    attention.answer(item["id"], "yes", by="telegram:1")
    api.calls.clear()

    result = attention.set_delivery(item["id"], "steer", by="telegram:1")

    assert result["ok"], result
    assert api.calls == [("steer", "ses_live", result["item"]["ref"]["inbox_id"], "steer")]
    assert result["item"]["delivery"] == "steer"


def test_undo_cancels_the_inbox_item_and_reopens(project, api):
    item = attention.request(project, "session", "ses_live", "ask", "Ship it?")
    answered = attention.answer(item["id"], "yes", by="telegram:1")
    api.calls.clear()

    result = attention.undo(item["id"], by="telegram:1")

    assert result["ok"], result
    assert api.calls == [("cancel", "ses_live", answered["item"]["ref"]["inbox_id"])]
    assert registry.get_attention(item["id"])["state"] == "open"


def test_form_answer_accepts_json_and_validates_fields(project, api):
    item = attention.request(
        project, "form", "ses_form", "form", "Deploy?",
        ref={"form_id": "frm_1", "session_id": "ses_form",
             "fields": [{"key": "env", "type": "string"},
                        {"key": "confirm", "type": "boolean"}]},
    )

    bad = attention.answer(item["id"], "prod", by="telegram:1")
    assert bad["ok"] is False
    assert "JSON" in bad["error"]

    good = attention.answer(item["id"], '{"env": "prod", "confirm": "yes"}', by="telegram:1")
    assert good["ok"], good
    assert api.calls[-1] == ("form", "ses_form", "frm_1", {"env": "prod", "confirm": True})


def test_room_answer_is_queued_when_the_bridge_is_offline(project, monkeypatch):
    from perun import perunchat_bridge

    monkeypatch.setattr(perunchat_bridge, "get_bridge", lambda: None)
    item = attention.request(project, "room", "grp:perun:inbox-proj:chat", "mention",
                             "perun, look at this")

    result = attention.answer(item["id"], "on it", by="telegram:1")

    assert result["ok"], result
    assert result["delivery"]["mode"] == "queued"
    pending = registry.attention_outbox_pending()
    assert len(pending) == 1
    assert pending[0]["payload"]["text"].startswith("operator (via Telegram):")


def test_room_answer_posts_threaded_when_connected(project, monkeypatch):
    from perun import perunchat_bridge

    posted: list[tuple] = []

    class Bridge:
        def is_connected(self):
            return True

        def post_to_room(self, room_id, text, **kw):
            posted.append((room_id, text, kw.get("in_reply_to")))
            return True

    monkeypatch.setattr(perunchat_bridge, "get_bridge", lambda: Bridge())
    item = attention.request(project, "room", "grp:perun:inbox-proj:chat", "mention",
                             "perun, look at this", ref={"frame_id": "msg_parent"})

    result = attention.answer(item["id"], "on it", by="telegram:1")

    assert result["ok"]
    assert posted == [("grp:perun:inbox-proj:chat",
                       "operator (via Telegram): on it", "msg_parent")]


def test_eligible_respects_the_flag_project_allowlist_and_quiet_hours(project):
    item = _permission(project)

    assert attention.eligible(item, _settings())[0]
    assert attention.eligible(item, _settings(attention_enabled=False)) == (
        False, "attention disabled")
    assert attention.eligible(item, _settings(attention_projects=["other"]))[0] is False
    late = _settings(attention_quiet_hours="00:00-23:59")
    assert attention.eligible(item, late)[0] is False

    allowed = _settings(attention_quiet_hours="")
    assert attention.eligible(item, allowed)[0]


def test_recipients_are_every_paired_chat_minus_mutes(project):
    item = _permission(project)
    muted = {"1": {project["slug"]}, "2": set()}

    assert attention.recipients(item, allowed=["1", "2"], muted=muted.get) == ["2"]
    assert attention.recipients(item, allowed=[], muted=muted.get) == []


def test_expire_stale_closes_overdue_items(project):
    item = attention.request(project, "session", "ses_x", "ask", "hello",
                             ttl_minutes=1)
    with registry.db.db() as conn:
        conn.execute("UPDATE attention_requests SET ttl_epoch=? WHERE id=?",
                     (int(time.time()) - 5, item["id"]))

    assert attention.expire_stale() == 1
    assert registry.get_attention(item["id"])["state"] == "expired"


def test_request_rejects_buttons_for_a_text_only_target(project):
    item = attention.request(project, "session", "ses_x", "ask", "hello")

    result = attention.choose(item["id"], "once", by="telegram:1")

    assert result["ok"] is False
    assert "text reply" in result["error"]


def test_drain_outbox_posts_queued_answers_once_the_bridge_is_back(project, monkeypatch):
    from perun import perunchat_bridge

    item = attention.request(project, "room", "grp:perun:inbox-proj:chat", "mention",
                             "perun, look at this", ref={"frame_id": "msg_parent"})
    monkeypatch.setattr(perunchat_bridge, "get_bridge", lambda: None)
    assert attention.answer(item["id"], "on it", by="telegram:1")["delivery"]["mode"] == "queued"
    assert len(registry.attention_outbox_pending()) == 1

    posted: list[tuple] = []

    class Bridge:
        def is_connected(self):
            return True

        def post_to_room(self, room_id, text, **kw):
            posted.append((room_id, text, kw.get("in_reply_to")))
            return True

    monkeypatch.setattr(perunchat_bridge, "get_bridge", lambda: Bridge())

    assert attention.drain_outbox() == 1
    assert posted == [("grp:perun:inbox-proj:chat",
                       "operator (via Telegram): on it", "msg_parent")]
    assert registry.attention_outbox_pending() == []


def test_drain_outbox_keeps_a_rejected_reply_for_a_retry(project, monkeypatch):
    from perun import perunchat_bridge

    item = attention.request(project, "room", "grp:perun:inbox-proj:chat", "mention",
                             "perun, look at this")
    monkeypatch.setattr(perunchat_bridge, "get_bridge", lambda: None)
    attention.answer(item["id"], "on it", by="telegram:1")

    class Bridge:
        def is_connected(self):
            return True

        def post_to_room(self, room_id, text, **kw):
            return False

    monkeypatch.setattr(perunchat_bridge, "get_bridge", lambda: Bridge())

    assert attention.drain_outbox() == 0
    pending = registry.attention_outbox_pending()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1


def test_cards_never_show_credentials_from_a_shell_command(project):
    item = attention.request(
        project, "permission", "per_secret", "permission", "shell",
        options=attention.PERMISSION_OPTIONS,
        ref={
            "request_id": "per_secret", "session_id": "ses_x", "action": "shell",
            "resources": [
                'curl -H "Authorization: Bearer sk-live_abcdef1234567890" https://api.example.com',
                'mysql --password hunter2 -e "select 1"',
                "export API_KEY=abc123456",
            ],
            "save": ["curl *"],
        },
    )

    text = attention.render(item)

    for secret in ("sk-live_abcdef1234567890", "hunter2", "abc123456"):
        assert secret not in text, text
    assert "Authorization: [redacted]" in text
    # Over-redaction is acceptable, losing the URL is not: the operator needs to
    # see what the command will touch.
    assert "https://api.example.com" in text


def test_the_operator_answer_is_delivered_verbatim(project, api):
    item = attention.request(project, "session", "ses_live", "ask", "which key?")

    result = attention.answer(item["id"], "use sk-test-123456", by="telegram:1")

    assert result["ok"], result
    assert api.calls[0][2] == "use sk-test-123456"


def test_pruning_removes_children_even_without_fk_cascades(project):
    item = _permission(project)
    registry.record_attention_delivery(item["id"], "1", 42)
    registry.attention_outbox_add(item["id"], "room", "grp:x", {"text": "x"})
    registry.close_attention(item["id"], "cancelled")
    with registry.db.db() as conn:
        conn.execute(
            "UPDATE attention_requests SET created_at='2020-01-01T00:00:00+00:00' "
            "WHERE id=?", (item["id"],))

    assert registry.prune_attention() == 1

    with registry.db.db() as conn:
        assert registry.db.rows(conn, "SELECT 1 FROM attention_deliveries") == []
        assert registry.db.rows(conn, "SELECT 1 FROM attention_outbox") == []


def test_deleting_a_project_removes_its_attention_rows(project):
    item = _permission(project)
    registry.record_attention_delivery(item["id"], "1", 7)

    assert registry.delete_project(project["slug"]) is True

    assert registry.get_attention(item["id"]) is None
    with registry.db.db() as conn:
        assert registry.db.rows(conn, "SELECT 1 FROM attention_deliveries") == []


def test_a_deleted_session_marks_the_answer_undeliverable(project, api):
    item = attention.request(project, "session", "ses_gone", "ask", "Ship it?")
    api.fail = opencode_api.OpenCodeHTTPError(404, "SessionNotFoundError", "/prompt")

    result = attention.answer(item["id"], "yes", by="telegram:1")

    assert result["ok"] is False
    assert "target is gone" in result["error"]
    assert registry.get_attention(item["id"])["state"] == "undeliverable"


def test_a_request_resolved_elsewhere_closes_the_card(project, api):
    item = _permission(project)
    api.permission_error = opencode_api.OpenCodeHTTPError(404, "PermissionNotFoundError", "/reply")

    result = attention.choose(item["id"], "once", by="telegram:1")

    assert result["ok"] is False
    assert "already resolved" in result["error"]
    assert registry.get_attention(item["id"])["state"] == "cancelled"


def test_an_undeliverable_outbox_row_is_abandoned_visibly(project, monkeypatch):
    from perun import perunchat_bridge

    item = attention.request(project, "room", "grp:perun:inbox-proj:chat", "mention", "look")
    monkeypatch.setattr(perunchat_bridge, "get_bridge", lambda: None)
    attention.answer(item["id"], "on it", by="telegram:1")
    with registry.db.db() as conn:
        conn.execute("UPDATE attention_outbox SET attempts=10")

    class Bridge:
        def is_connected(self):
            return True

        def post_to_room(self, room_id, text, **kw):
            raise AssertionError("a row that hit the attempt cap must not be posted")

    monkeypatch.setattr(perunchat_bridge, "get_bridge", lambda: Bridge())

    assert attention.drain_outbox() == 0
    assert registry.attention_outbox_pending() == []
    kinds = [(row["kind"], row["message"]) for row in registry.list_activity(limit=10)]
    assert any(k == "attention_failed" and "abandoned" in m for k, m in kinds)


def test_multiselect_form_answers_split_and_validate_choices(project, api):
    item = attention.request(
        project, "form", "ses_form", "form", "Pick targets",
        ref={"form_id": "frm_1", "session_id": "ses_form",
             "fields": [{"key": "envs", "type": "multiselect",
                         "options": ["dev", "prod", "staging"]}]},
    )

    bad = attention.answer(item["id"], '{"envs": "dev, nope"}', by="telegram:1")
    assert bad["ok"] is False
    assert "nope" in bad["error"]

    good = attention.answer(item["id"], '{"envs": "dev, staging"}', by="telegram:1")
    assert good["ok"], good
    assert api.calls[-1] == ("form", "ses_form", "frm_1", {"envs": ["dev", "staging"]})


def test_quiet_hours_wrap_across_midnight():
    from datetime import datetime

    window = "22:00-07:00"
    assert attention._quiet_now(window, datetime(2026, 10, 31, 23, 30)) is True
    assert attention._quiet_now(window, datetime(2026, 10, 31, 6, 30)) is True
    assert attention._quiet_now(window, datetime(2026, 10, 31, 12, 0)) is False
    assert attention._quiet_now("", datetime(2026, 10, 31, 12, 0)) is False
    assert attention._quiet_now("garbage", datetime(2026, 10, 31, 12, 0)) is False


def test_two_devices_answering_at_once_only_one_wins(project, api):
    import threading

    item = attention.request(project, "session", "ses_live", "ask", "Ship it?")
    results: list[dict] = []

    def reply(text: str) -> None:
        results.append(attention.answer(item["id"], text, by="telegram:1"))

    threads = [threading.Thread(target=reply, args=(text,)) for text in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sum(1 for r in results if r.get("ok")) == 1
    assert registry.get_attention(item["id"])["state"] == "answered"
