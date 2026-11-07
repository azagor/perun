"""Live run drafts: partial output streaming and the draft Stop button."""

from __future__ import annotations

import threading

from perun.telegram import PerunTelegramBridge


class _StreamingOrch:
    def __init__(self):
        self.cancelled = []

    def stream(self, uid):  # noqa: ARG002
        yield {"type": "text", "text": "hello from the agent"}
        yield {"type": "tool", "tool": "bash"}
        yield {"type": "text", "text": "second paragraph"}
        yield {"type": "end"}

    def cancel(self, uid):
        self.cancelled.append(uid)
        return True


def test_a_private_chat_streams_a_draft_then_stops():
    orch = _StreamingOrch()
    bridge = PerunTelegramBridge(orch, token="test", allowed=["1"])
    calls = []

    def fake_api(method, **params):
        calls.append((method, dict(params)))
        return {"ok": True, "result": True}

    bridge.api = fake_api  # type: ignore[assignment]
    bridge._draft_loop("1", "run_1", 77, stop=threading.Event())

    assert calls[0][0] == "sendMessageDraft"
    assert calls[0][1]["text"] == ""           # native "Thinking…" placeholder
    method, last = calls[-1]
    assert method == "sendMessageDraft"
    assert "Working" in last["text"]
    assert "hello from the agent" in last["text"]
    assert "bash" in last["text"]
    assert last["can_stop"] is True


def test_draft_stop_cancels_the_matching_run():
    orch = _StreamingOrch()
    bridge = PerunTelegramBridge(orch, token="test", allowed=["1"])
    bridge._drafts[77] = "run_1"

    bridge._handle_generation_stopped({"draft_id": 77, "chat": {"id": "1"}})

    assert orch.cancelled == ["run_1"]


def test_group_chats_never_get_drafts():
    bridge = PerunTelegramBridge(None, token="test", allowed=[])
    assert bridge._is_private_chat("123456") is True
    assert bridge._is_private_chat("-100123456") is False
