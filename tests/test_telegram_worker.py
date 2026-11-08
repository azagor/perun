"""Per-chat scheduling: one run per chat, different chats in parallel."""

from __future__ import annotations

import threading

from perun.telegram import PerunTelegramBridge


def _bridge():
    return PerunTelegramBridge(None, token="test", allowed=[])


def test_different_chats_run_in_parallel(monkeypatch):
    bridge = _bridge()
    started = {"1": threading.Event(), "2": threading.Event()}
    release = threading.Event()

    def fake_run(chat_id, text, message_id):  # noqa: ARG001
        started[chat_id].set()
        release.wait(5)

    monkeypatch.setattr(bridge, "run_message", fake_run)
    worker = threading.Thread(target=bridge.worker, daemon=True)
    worker.start()
    try:
        bridge.work.put({"chat_id": "1", "text": "one", "message_id": 1})
        bridge.work.put({"chat_id": "2", "text": "two", "message_id": 2})

        assert started["1"].wait(2)
        assert started["2"].wait(2)
    finally:
        release.set()
        bridge.stop_event.set()
        worker.join(3)


def test_one_chat_never_runs_two_messages_at_once(monkeypatch):
    bridge = _bridge()
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def fake_run(chat_id, text, message_id):  # noqa: ARG001
        if text == "first":
            first_started.set()
            release_first.wait(5)
        else:
            second_started.set()

    monkeypatch.setattr(bridge, "run_message", fake_run)
    worker = threading.Thread(target=bridge.worker, daemon=True)
    worker.start()
    try:
        bridge.work.put({"chat_id": "1", "text": "first", "message_id": 1})
        bridge.work.put({"chat_id": "1", "text": "second", "message_id": 2})

        assert first_started.wait(2)
        assert not second_started.is_set()   # queued behind the running one
        release_first.set()
        assert second_started.wait(2)
    finally:
        release_first.set()
        bridge.stop_event.set()
        worker.join(3)
