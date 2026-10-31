"""Durable Telegram update queue: per-chat ordering, retries, dead-letter."""

from __future__ import annotations

import time

import pytest

from perun import registry
from perun.telegram import PerunTelegramBridge


class FakeOrch:
    def start_run(self, *a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("no run expected")

    def cancel(self, *_a, **_k):  # noqa: ANN002, ANN003
        return False


@pytest.fixture()
def bridge(control_repo):  # noqa: ARG001 - patches config.REPO_ROOT
    b = PerunTelegramBridge(FakeOrch(), token="test-token", allowed=["1"])
    b.sent = []
    b.send = lambda chat_id, text, **kw: b.sent.append(text) or 1  # type: ignore[assignment]
    b.api = lambda method, **kw: {"ok": True, "result": {}}  # type: ignore[assignment]
    yield b
    b.client.close()


def _job(chat_id="1", key="upd:1", kind="message"):
    return registry.enqueue_telegram_job(chat_id, kind, {"update": {"update_id": 1}},
                                         key)


def test_enqueue_is_idempotent_by_update_id():
    assert _job(key="upd:10") is True
    assert _job(key="upd:10") is False
    assert len(registry.list_telegram_jobs()) == 1


def test_claim_runs_one_job_per_chat_and_keeps_order():
    _job(key="upd:1")
    _job(key="upd:2")
    _job(chat_id="2", key="upd:3")

    first = registry.claim_telegram_jobs()
    assert sorted(job["dedupe_key"] for job in first) == ["upd:1", "upd:3"]

    # The second job for chat 1 waits until the first finishes.
    assert registry.claim_telegram_jobs() == []
    registry.finish_telegram_job(next(j for j in first if j["dedupe_key"] == "upd:1")["id"])

    second = registry.claim_telegram_jobs()
    assert [job["dedupe_key"] for job in second] == ["upd:2"]


def test_a_failing_job_backs_off_then_dead_letters():
    assert _job(key="upd:fail")
    job = registry.claim_telegram_jobs()[0]

    assert registry.fail_telegram_job(job["id"], "boom") == "queued"
    row = registry.list_telegram_jobs()[0]
    assert row["attempts"] == 1 and row["not_before"] > time.time()
    assert row["last_error"] == "boom"

    # The backoff keeps it out of the next claim until not_before passes.
    assert registry.claim_telegram_jobs() == []
    for _ in range(4):
        registry.fail_telegram_job(job["id"], "boom")
    assert registry.list_telegram_jobs()[0]["state"] == "failed"

    assert registry.retry_telegram_job(job["id"]) is True
    retried = registry.list_telegram_jobs()[0]
    assert retried["state"] == "queued" and retried["attempts"] == 0


def test_a_restart_requeues_running_jobs():
    _job(key="upd:run")
    claimed = registry.claim_telegram_jobs()
    assert claimed and claimed[0]["state"] == "running"

    assert registry.reset_telegram_jobs() == 1
    row = registry.list_telegram_jobs()[0]
    assert row["state"] == "queued" and row["attempts"] == 1


def test_pruning_drops_old_finished_jobs_only():
    _job(key="upd:old")
    job = registry.claim_telegram_jobs()[0]
    registry.finish_telegram_job(job["id"])
    _job(chat_id="2", key="upd:open")
    with registry.db.db() as conn:
        conn.execute(
            "UPDATE telegram_jobs SET created_at='2020-01-01T00:00:00+00:00' "
            "WHERE dedupe_key='upd:old'")

    assert registry.prune_telegram_jobs() == 1
    assert [j["dedupe_key"] for j in registry.list_telegram_jobs()] == ["upd:open"]


def test_the_reader_skips_bots_and_chatless_updates(bridge):
    bot = {"update_id": 1, "message": {"message_id": 1, "from": {"is_bot": True},
                                       "chat": {"id": "1"}, "text": "hi"}}
    assert bridge._enqueue_update(bot) is True
    assert registry.list_telegram_jobs() == []

    assert bridge._enqueue_update({"update_id": 2, "inline_query": {}}) is True
    assert registry.list_telegram_jobs() == []

    human = {"update_id": 3, "message": {"message_id": 7, "from": {"id": 9},
                                         "chat": {"id": "1"}, "text": "hi"}}
    assert bridge._enqueue_update(human) is True
    jobs = registry.list_telegram_jobs()
    assert [j["dedupe_key"] for j in jobs] == ["upd:3"]
    assert jobs[0]["chat_id"] == "1"

    # A redelivered update is accepted but not queued twice.
    assert bridge._enqueue_update(human) is True
    assert len(registry.list_telegram_jobs()) == 1


def test_run_job_finishes_on_success_and_requeues_on_failure(bridge, monkeypatch):
    _job(key="upd:ok")
    job = registry.claim_telegram_jobs()[0]
    handled = []
    monkeypatch.setattr(bridge, "_handle_update", lambda update: handled.append(update))
    bridge._run_job(job)
    assert handled == [{"update_id": 1}]
    assert registry.list_telegram_jobs()[0]["state"] == "done"

    _job(chat_id="2", key="upd:bad")
    bad = registry.claim_telegram_jobs()[0]

    def boom(update):  # noqa: ANN001
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(bridge, "_handle_update", boom)
    bridge._run_job(bad)
    row = next(j for j in registry.list_telegram_jobs() if j["dedupe_key"] == "upd:bad")
    assert row["state"] == "queued" and row["attempts"] == 1
