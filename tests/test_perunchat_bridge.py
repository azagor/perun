"""Tests for the PerunChat autonomous bridge integration."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("PERUN_HOME", os.path.join(os.getcwd(), "tests", "tmp_perun_home"))

from perun import config, package_handoff
from perun.perunchat_bridge import PerunChatBridge


@pytest.fixture
def settings(tmp_path):
    os.environ["PERUN_HOME"] = str(tmp_path)
    config.CONFIG_PATH = tmp_path / "config.json"
    s = config.Settings.load()
    s.perunchat_api_key = "prn_test_secret"
    s.perunchat_api_endpoint = "ws://127.0.0.1:8765/v1/chat/ws"
    s.perunchat_auto_reply = True
    s.perunchat_auto_join = ["pub:global:test"]
    s.perunchat_trusted = ["frambo", "peruntheoverseer", "perun-local"]
    return s


def test_bridge_disabled_without_key(settings):
    settings.perunchat_api_key = ""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    assert not bridge.enabled


def test_bridge_disabled_without_auto_reply(settings):
    settings.perunchat_auto_reply = False
    bridge = PerunChatBridge(MagicMock(settings=settings))
    assert not bridge.enabled


def test_bridge_enabled(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    assert bridge.enabled


def test_bridge_reads_current_settings_from_orchestrator(settings):
    """After server.py replaces ORCH.settings, the bridge must read the new object.

    Regression: the bridge used to cache self.settings at __init__ time, so a
    dashboard settings save (which creates a new Settings instance) left the
    bridge reading the old auto_reply value and reporting 'bridge: stopped'.
    """
    settings.perunchat_auto_reply = False
    new_settings = config.Settings.load()
    new_settings.perunchat_api_key = "prn_test_secret"
    new_settings.perunchat_auto_reply = True

    orch = MagicMock(settings=settings)
    bridge = PerunChatBridge(orch)
    assert not bridge.enabled  # old settings say disabled

    orch.settings = new_settings
    assert bridge.enabled      # property reads the new settings object


def test_default_rooms_when_empty(settings):
    settings.perunchat_auto_join = []
    bridge = PerunChatBridge(MagicMock(settings=settings))
    assert bridge._rooms() == ["pub:global:general", "pub:global:tech", "pub:global:random"]


def test_default_rooms_merged_with_configured(settings):
    settings.perunchat_auto_join = ["pub:global:general", "grp:my-org:alerts"]
    bridge = PerunChatBridge(MagicMock(settings=settings))
    assert bridge._rooms() == [
        "pub:global:general", "pub:global:tech", "pub:global:random",
        "grp:my-org:alerts",
    ]


def test_room_type_classification():
    from perun.perunchat_bridge import _room_type
    assert _room_type("pub:global:test") == "PUBLIC"
    assert _room_type("priv:abc123") == "PRIVATE"
    assert _room_type("grp:tenant:room") == "GROUP"


def test_bridge_ignores_own_messages(settings):
    mock_orchestrator = MagicMock(settings=settings)
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    frame = {
        "type": "CHAT",
        "id": "msg_1",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "perun-local", "runtime": "perun"},
        "payload": "hello",
    }
    with patch.object(bridge, "_reply") as mock_reply:
        bridge._on_frame(frame, None)
        bridge._drain_room_queues()
        mock_reply.assert_not_called()


def test_bridge_operator_messages_are_not_swallowed_as_own(settings):
    """A frame from the dashboard human operator shares our agent_id but uses
    runtime 'perun-operator'. It must be kept in room history and not added to
    the own-message set, so replies to it are processed normally."""
    mock_orchestrator = MagicMock(settings=settings)
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    frame = {
        "type": "CHAT",
        "id": "msg_operator_1",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "perun-local", "runtime": "perun-operator"},
        "payload": "hello everyone",
    }
    with patch.object(bridge, "_reply") as mock_reply:
        bridge._on_frame(frame, None)
        bridge._drain_room_queues()
    hist_ids = [f.get("id") for f in bridge._room_history.get("pub:global:test", [])]
    assert "msg_operator_1" in hist_ids
    assert "msg_operator_1" not in bridge._own_message_ids


def test_bridge_dedups_duplicate_frame_ids(settings):
    """Exact replays of the same frame id must not be processed twice or
    create duplicate history entries."""
    mock_orchestrator = MagicMock(settings=settings)
    mock_orchestrator.run_and_wait.return_value = {"reply": "ok"}
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    frame = {
        "type": "CHAT",
        "id": "msg_dup_1",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "payload": "hello @perun",
    }
    with patch.object(bridge, "_reply") as mock_reply:
        bridge._on_frame(frame, None)
        bridge._drain_room_queues()
        bridge._on_frame(frame, None)
        bridge._drain_room_queues()
    mock_reply.assert_called_once()
    assert len(bridge._room_history.get("pub:global:test", [])) == 1


def test_bridge_duplicate_detection_handles_malformed_timestamp(settings):
    """A non-numeric timestamp must not crash the duplicate-detection path."""
    mock_orchestrator = MagicMock(settings=settings)
    mock_orchestrator.run_and_wait.return_value = {"reply": "ok"}
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    frame = {
        "type": "CHAT",
        "id": "msg_bad_ts",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "timestamp": "not-a-number",
        "payload": "hello @perun",
    }
    with patch.object(bridge, "_reply") as mock_reply:
        bridge._on_frame(frame, None)
        bridge._drain_room_queues()
    mock_reply.assert_called_once()
    assert "msg_bad_ts" in [f.get("id") for f in bridge._room_history.get("pub:global:test", [])]


def test_bridge_dedups_duplicate_content_with_new_id(settings):
    """The same content from the same sender within the same second, even with
    a fresh id, must be treated as a duplicate."""
    mock_orchestrator = MagicMock(settings=settings)
    mock_orchestrator.run_and_wait.return_value = {"reply": "ok"}
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    ts = 1712345678000
    frame1 = {
        "type": "CHAT",
        "id": "msg_dup_a",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "timestamp": ts,
        "payload": "hello @perun",
    }
    frame2 = {
        "type": "CHAT",
        "id": "msg_dup_b",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "timestamp": ts,
        "payload": "hello @perun",
    }
    with patch.object(bridge, "_reply") as mock_reply:
        bridge._on_frame(frame1, None)
        bridge._drain_room_queues()
        bridge._on_frame(frame2, None)
        bridge._drain_room_queues()
    mock_reply.assert_called_once()
    hist = bridge._room_history.get("pub:global:test", [])
    assert len(hist) == 1
    assert hist[0].get("id") == "msg_dup_a"


def test_build_prompt_includes_latest_message(settings):
    """The prompt must contain the latest message text and identify its sender."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    frame = {
        "id": "msg_q",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "payload": "how much is 2 + 6?",
    }
    prompt = bridge._build_prompt(frame)
    assert "Latest message:" in prompt
    assert "how much is 2 + 6?" in prompt
    assert "From: other-agent" in prompt


def test_build_prompt_excludes_current_frame_from_history(settings):
    """The frame being replied to must not appear again in the recent history."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    prev = {
        "id": "msg_prev",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "payload": "previous",
    }
    cur = {
        "id": "msg_cur",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "payload": "current",
    }
    bridge._room_history["pub:global:test"] = deque([prev, cur], maxlen=40)
    prompt = bridge._build_prompt(cur)
    # The current frame appears once in the Latest message block. It must not
    # be duplicated as a history line (format "other-agent: current").
    assert prompt.count("current") == 1
    assert "other-agent: previous" in prompt
    assert "other-agent: current" not in prompt


def test_build_prompt_drops_oldest_history_when_too_long(settings, monkeypatch):
    """If the prompt exceeds the size cap, the oldest history lines must be
    dropped first; the latest message is always preserved."""
    from perun import perunchat_bridge as bridge_mod
    from perun import memory

    # Derive the cap from the system prompt so this test keeps exercising the
    # trimming loop (rather than failing whenever the prompt grows). Leave room
    # for a handful of history lines and the small metadata footer.
    cap = len(bridge_mod.CHAT_SYSTEM_PROMPT) + 600
    monkeypatch.setattr(bridge_mod, "_MAX_PROMPT_CHARS", cap)
    monkeypatch.setattr(memory, "search_facts", lambda *a, **kw: [])
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    room_id = "pub:global:test"
    history = []
    for i in range(20):
        history.append({
            "id": f"msg_{i}",
            "room_id": room_id,
            "sender": {"agent_id": "other-agent"},
            "payload": f"history-item-{i:02d} with some padding text",
        })
    cur = {
        "id": "msg_cur",
        "room_id": room_id,
        "sender": {"agent_id": "other-agent"},
        "payload": "this is the latest question",
    }
    bridge._room_history[room_id] = deque(history, maxlen=40)
    prompt = bridge._build_prompt(cur)
    # Latest message must be present.
    assert "this is the latest question" in prompt
    # Oldest messages should have been trimmed away.
    assert "history-item-00" not in prompt
    assert "history-item-01" not in prompt
    # Newer history should still be present.
    assert "history-item-18" in prompt
    assert "history-item-19" in prompt


def test_own_replies_are_kept_in_room_history(settings):
    """Own outbound replies must stay in the transcript so the model sees a
    normal chat conversation, but they must not trigger further replies."""
    mock_orchestrator = MagicMock(settings=settings)
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    peer = {
        "type": "CHAT",
        "id": "msg_peer",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "payload": "hello @perun",
    }
    own = {
        "type": "CHAT",
        "id": "msg_own",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "perun-local", "runtime": "perun"},
        "payload": "hi there",
    }
    with patch.object(bridge, "_reply") as mock_reply:
        bridge._on_frame(peer, None)
        bridge._drain_room_queues()
        bridge._on_frame(own, None)
        bridge._drain_room_queues()
    mock_reply.assert_called_once()
    hist = bridge._room_history.get("pub:global:test", [])
    assert len(hist) == 2
    assert "msg_peer" in [f.get("id") for f in hist]
    assert "msg_own" in [f.get("id") for f in hist]


def test_participant_roster_is_capped(settings):
    """The prompt roster must not grow unbounded in busy rooms."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    now = time.time()
    participants = bridge._room_participants.setdefault("pub:global:test", {})
    for i in range(25):
        participants[f"agent-{i}"] = now
    roster = bridge._participant_roster("pub:global:test")
    assert len(roster) == 10
    assert "perun-local" not in roster


def test_bridge_replies_to_other_messages(settings):
    mock_orchestrator = MagicMock(settings=settings)
    mock_orchestrator.run_and_wait.return_value = {"reply": "Hi there"}
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    frame = {
        "type": "CHAT",
        "id": "msg_2",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "payload": "hello perun",
    }
    with patch.object(bridge, "_reply") as mock_reply:
        bridge._on_frame(frame, None)
        bridge._drain_room_queues()
        mock_reply.assert_called_once()


def test_on_frame_ignores_non_object_frames(settings):
    """A JSON array/scalar frame must be dropped, not crash the receive loop."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    for bad in ([1, 2, 3], "just a string", 42, None, True):
        bridge._on_frame(bad, None)  # must not raise
    assert bridge._room_history == {}


def test_on_frame_normalizes_null_sender(settings):
    """`sender: null` must not AttributeError on `.get('agent_id')` downstream."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    frame = {
        "type": "CHAT",
        "id": "msg_null_sender",
        "room_id": "pub:global:test",
        "sender": None,
        "payload": "hello",
    }
    with patch.object(bridge, "_reply") as mock_reply:
        bridge._on_frame(frame, None)  # must not raise
    assert frame["sender"] == {}
    # Frames with a missing sender key are normalized too.
    frame2 = {
        "type": "CHAT",
        "id": "msg_no_sender",
        "room_id": "pub:global:test",
        "payload": "hello",
    }
    with patch.object(bridge, "_reply"):
        bridge._on_frame(frame2, None)  # must not raise
    assert frame2["sender"] == {}


def test_status_exposes_agent_id(settings):
    mock_orchestrator = MagicMock(settings=settings)
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    status = bridge.status
    assert status["enabled"] is True
    assert status["agent_id"] == "perun-local"
    # Configured pub:global:test is merged with the default public rooms.
    assert "pub:global:test" in status["rooms"]
    assert "pub:global:general" in status["rooms"]


def test_rest_base_public_host():
    from perun.perunchat_bridge import _rest_base
    assert _rest_base("wss://peruninthemachine.com/v1/chat/ws") == "https://peruninthemachine.com/chat"
    assert _rest_base("ws://www.peruninthemachine.com/v1/chat/ws") == "http://www.peruninthemachine.com/chat"


def test_rest_base_local_dev():
    from perun.perunchat_bridge import _rest_base
    assert _rest_base("ws://127.0.0.1:8765/v1/chat/ws") == "http://127.0.0.1:8765"
    assert _rest_base("wss://localhost/v1/chat/ws") == "https://localhost"


def test_rest_base_helper_is_shared_with_config():
    """Bridge, HTTP proxy and shared-project sync must agree on the base URL."""
    from perun import config

    endpoint = "wss://peruninthemachine.com/v1/chat/ws"
    assert config.perunchat_rest_base(endpoint) == "https://peruninthemachine.com/chat"
    assert config.perunchat_rest_base("") == ""
    assert config.perunchat_rest_base("not a url") == ""


def test_shared_project_sync_rest_base_public_host():
    from perun.shared_project_sync import _rest_base
    # Regression: this used to return https://peruninthemachine.com (SPA HTML)
    # and every shared-project chat sync failed with a JSON decode error.
    assert _rest_base("wss://peruninthemachine.com/v1/chat/ws") == "https://peruninthemachine.com/chat"


def test_bridge_joins_project_chat_rooms(settings, tmp_path):
    """Shared-project chats must be joined even when not in perunchat_auto_join."""
    from perun import registry

    (tmp_path / "proj-chat").mkdir()
    project = registry.add_project("proj-chat", str(tmp_path / "proj-chat"))
    registry.set_project_chat(project["id"], "grp:perun:proj-chat:chat", "perun", "active", 1)
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._discovered_private_rooms = []
    rooms = bridge._rooms()
    assert "grp:perun:proj-chat:chat" in rooms
    assert rooms.count("grp:perun:proj-chat:chat") == 1


def test_bridge_discovers_private_rooms(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    response = MagicMock(status_code=200)
    response.json.return_value = {"rooms": ["priv:aaa", "priv:bbb"]}
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get.return_value = response
    with patch("perun.perunchat_bridge.httpx.Client", return_value=client):
        bridge._refresh_discovered_rooms()
    assert bridge._discovered_private_rooms == ["priv:aaa", "priv:bbb"]
    assert "priv:aaa" in bridge._rooms()
    assert "priv:bbb" in bridge._rooms()
    # Discovery joins Perun to private rooms it already belongs to; it must not
    # invent participants or rooms.
    assert "priv:ccc" not in bridge._rooms()


def test_join_room_sends_room_join_frame(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    ws = MagicMock()
    assert bridge._join_room(ws, "grp:perun:demo:chat") is True
    payload = json.loads(ws.send.call_args[0][0])
    assert payload["type"] == "ROOM_JOIN"
    assert payload["room_id"] == "grp:perun:demo:chat"
    assert payload["room_type"] == "GROUP"


def test_room_join_acknowledgement_is_tracked(settings):
    """A join is only complete when the gateway says ROOM_JOINED.

    A ``send`` that "succeeds" against a room that does not exist yet must not
    mark it joined forever; the ERROR frame clears the ack so the next refresh
    retries the room after it is created.
    """
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._on_frame({"type": "ROOM_JOINED", "room_id": "grp:perun:demo:chat"}, None)
    assert "grp:perun:demo:chat" in bridge._acked_rooms
    bridge._on_frame(
        {"type": "ERROR", "room_id": "grp:perun:demo:chat", "message": "room_access_denied"},
        None,
    )
    assert "grp:perun:demo:chat" not in bridge._acked_rooms


def test_bridge_replies_by_content_type_without_type_field(settings):
    """Gateway CHAT frames may omit 'type' and use content_type='text/plain'."""
    mock_orchestrator = MagicMock(settings=settings)
    mock_orchestrator.run_and_wait.return_value = {"reply": "Hi there"}
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    frame = {
        "id": "msg_3",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "content_type": "text/plain",
        "payload": "hello perun",
    }
    with patch.object(bridge, "_reply") as mock_reply:
        bridge._on_frame(frame, None)
        bridge._drain_room_queues()
        mock_reply.assert_called_once()


def test_bridge_skips_non_text_frames(settings):
    mock_orchestrator = MagicMock(settings=settings)
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    frame = {
        "id": "msg_4",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "content_type": "application/json",
        "payload": '{"action":"proposal"}',
    }
    with patch.object(bridge, "_reply") as mock_reply:
        bridge._on_frame(frame, None)
        bridge._drain_room_queues()
        mock_reply.assert_not_called()


def test_strip_instruction_wrappers():
    from perun.perunchat_bridge import _strip_instruction_wrappers
    text = "<perun_instructions scope='project'>hello</perun_instructions> world"
    assert _strip_instruction_wrappers(text) == "hello world"


def test_reply_uses_model_fallback_chain(settings):
    """chat_model -> orchestrator_model -> default_model is resolved and skip_instructions is used."""
    settings.perunchat_chat_model = ""
    settings.orchestrator_model = "opencode/orchestrator"
    settings.default_model = "opencode/default"
    mock_orchestrator = MagicMock(settings=settings)
    mock_orchestrator.run_and_wait.return_value = {"reply": "ok"}
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    frame = {
        "id": "msg_5",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "content_type": "text/plain",
        "payload": "hi",
    }
    bridge._reply(frame, None)
    call = mock_orchestrator.run_and_wait.call_args
    assert call.kwargs.get("model") == "opencode/orchestrator"
    assert call.kwargs.get("skip_instructions") is True


def test_reply_defaults_to_perunchat_agent(settings):
    """With no explicit chat agent the dedicated perunchat agent is used."""
    settings.perunchat_chat_agent = ""
    settings.perunchat_chat_model = "opencode/chat"
    mock_orchestrator = MagicMock(settings=settings)
    mock_orchestrator.run_and_wait.return_value = {"reply": "ok"}
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    frame = {
        "id": "msg_6",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "content_type": "text/plain",
        "payload": "hi",
    }
    bridge._reply(frame, None)
    call = mock_orchestrator.run_and_wait.call_args
    assert call.kwargs.get("agent") == "perunchat"


def test_reply_explicit_agent_overrides_default(settings):
    """An explicit perunchat_chat_agent setting wins over the default."""
    settings.perunchat_chat_agent = "my-custom-agent"
    settings.perunchat_chat_model = "opencode/chat"
    mock_orchestrator = MagicMock(settings=settings)
    mock_orchestrator.run_and_wait.return_value = {"reply": "ok"}
    bridge = PerunChatBridge(mock_orchestrator)
    bridge._agent_id = "perun-local"
    frame = {
        "id": "msg_7",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "content_type": "text/plain",
        "payload": "hi",
    }
    bridge._reply(frame, None)
    call = mock_orchestrator.run_and_wait.call_args
    assert call.kwargs.get("agent") == "my-custom-agent"


def test_public_reply_redacts_internal_details(settings):
    """Public replies must not leak local paths or internal references."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    text = "I live in C:\\my_apps\\perun and use MCP tools there."
    out = bridge._redact_internal_info(text, "pub:global:general")
    assert "C:\\my_apps\\perun" not in out
    assert "MCP tools" not in out
    assert "[redacted]" in out


def test_public_reply_redacts_forward_slash_and_posix_paths(settings):
    """Forward-slash Windows paths and POSIX paths are also internal."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    out = bridge._redact_internal_info(
        "code at C:/my_apps/perun/x.py and /home/user/project/main.py",
        "pub:global:general",
    )
    assert "my_apps" not in out
    assert "/home/user/project" not in out
    assert "[redacted]" in out


def test_public_reply_keeps_public_urls(settings):
    """A legitimate scheme URL must NOT be mangled by the path guard."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    text = "more at https://peruninthemachine.com/perunchat/public_chat.html"
    out = bridge._redact_internal_info(text, "pub:global:general")
    assert out == text


def test_public_reply_keeps_plain_ratios(settings):
    """Plain numeric ratios are not paths and must survive untouched."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    text = "3/4 of 1/2 is not a path"
    out = bridge._redact_internal_info(text, "pub:global:general")
    assert out == text


def test_public_redaction_preserves_json_structure(settings):
    """Redacting paths and internal terms inside a JSON payload must not
    consume quotes or break parsing."""
    import json

    bridge = PerunChatBridge(MagicMock(settings=settings))
    payload = {
        "path": "C:\\my_apps\\perun",
        "unix": "/home/user/project",
        "url": "https://peruninthemachine.com/perunchat/public_chat.html",
        "note": "use MCP tools and .env files carefully",
    }
    text = json.dumps(payload)
    out = bridge._redact_internal_info(text, "pub:global:general")
    data = json.loads(out)

    assert "[redacted]" in data["path"]
    assert "[redacted]" in data["unix"]
    assert data["url"] == payload["url"]
    assert "[redacted]" in data["note"]
    assert ".env" not in data["note"]


def test_trusted_private_reply_keeps_context(settings):
    """Trusted private replies may keep non-path context."""
    settings.perunchat_trusted = ["frambo"]
    bridge = PerunChatBridge(MagicMock(settings=settings))
    assert bridge._is_trusted("frambo") is True
    assert bridge._is_trusted("stranger") is False


def test_chat_system_prompt_loads_from_file():
    from perun.perunchat_bridge import _load_chat_instructions
    from perun import orchestrator as _orchestrator

    path = MagicMock()
    path.is_file.return_value = True
    with patch("perun.perunchat_bridge.Path", return_value=path):
        with patch.object(
            _orchestrator, "_load_instruction_file", return_value=("scope", "source", True, "Strict chat prompt from file.")
        ):
            body = _load_chat_instructions()
    assert body == "Strict chat prompt from file."


def test_chat_instructions_include_shared_project_guidance():
    from perun import config
    body = (config.REPO_ROOT / "perun" / "instructions" / "perunchat_chat.md").read_text(encoding="utf-8")
    assert "[shared-project-context]" in body
    assert "perun-proposals" in body.lower()
    assert "propose" in body.lower()
    assert "approve" in body.lower()
    assert "action_type" in body
    assert "targets" in body
    assert "must approve" in body.lower()
    assert "room owner" in body.lower() or "another participant" in body.lower()
    # A project chat must answer about the bound project, not about Perun.
    assert "never Perun" in body
    assert "as the project's purpose" in body


def _make_shared_project(
    tmp_path,
    slug="ai-playground",
    *,
    description="AI playground for experiments",
    participants=None,
    room=None,
):
    from perun import manifest, registry

    path = tmp_path / slug
    path.mkdir(parents=True)
    project = registry.add_project(slug, str(path), description=description)
    data = manifest.from_project(project)
    data["shared"] = True
    data["participants"] = participants or [{"agent_id": "frambo", "roles": ["rw"]}]
    manifest.save(str(path), data, body=f"# {project['name']}")
    room_id = room or f"grp:perun:{slug}:chat"
    registry.set_project_chat(project["id"], room_id, "peruntheoverseer", "active", 2)
    return project, room_id


def test_project_binding_resolves_only_bound_shared_chats(settings, tmp_path):
    project, room = _make_shared_project(tmp_path)
    bridge = PerunChatBridge(MagicMock(settings=settings))

    binding = bridge._project_binding(room)
    assert binding is not None
    bound_project, cfg = binding
    assert bound_project["slug"] == project["slug"]
    assert cfg["shared"] is True

    assert bridge._project_binding("grp:perun:other:chat") is None
    assert bridge._project_binding("pub:global:test") is None

    # Turning sharing off removes the binding even if the old row lingers.
    (tmp_path / "ai-playground" / "perun.md").unlink()
    from perun import manifest

    data = manifest.from_project(project)
    data["shared"] = False
    manifest.save(str(tmp_path / "ai-playground"), data)
    assert bridge._project_binding(room) is None


def test_project_context_names_bound_project_not_perun(settings, tmp_path):
    project, room = _make_shared_project(tmp_path)
    (tmp_path / "ai-playground" / "README.md").write_text(
        "# AI Playground\n\nA sandbox for testing image generation and local models.\n",
        encoding="utf-8",
    )
    bridge = PerunChatBridge(MagicMock(settings=settings))

    ctx = bridge._shared_project_context(room)

    assert "[shared-project-context]" in ctx
    assert "ai-playground" in ctx
    assert "AI playground for experiments" in ctx
    assert "A sandbox for testing image generation" in ctx
    assert "never Perun" in ctx
    assert "not the project's purpose" in ctx
    assert "[end-shared-project-context]" in ctx
    # The prompt may leave this machine for another Perun/agent; local host
    # paths and remotes are not part of the project identity.
    assert "Location on this machine" not in ctx
    assert str(tmp_path).lower() not in ctx.lower()


def test_project_context_admits_missing_description_and_docs(settings, tmp_path):
    project, room = _make_shared_project(
        tmp_path, slug="empty-project", description=""
    )
    bridge = PerunChatBridge(MagicMock(settings=settings))

    ctx = bridge._shared_project_context(room)

    assert "Description: not set yet" in ctx
    assert "Documentation: none found yet" in ctx
    # Never invent a purpose for a project that documents nothing.
    assert "say so plainly" in ctx


def test_project_member_authorization_covers_participants_owner_and_perun(settings, tmp_path):
    project, room = _make_shared_project(tmp_path)
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "peruntheoverseer"

    assert bridge._is_project_member(room, "frambo") is True       # participant
    assert bridge._is_project_member(room, "peruntheoverseer") is True  # owner + self
    assert bridge._is_project_member(room, "stranger") is False
    assert bridge._is_project_member(room, "") is False
    assert bridge._is_project_member("grp:other:room", "frambo") is False


def test_project_member_requires_current_trust(settings, tmp_path):
    project, room = _make_shared_project(tmp_path)
    settings.perunchat_trusted = ["perun-local"]
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"

    assert bridge._is_project_member(room, "frambo") is False
    # The local room owner is still authorized; this is not a global trust bypass.
    assert bridge._is_project_member(room, "peruntheoverseer") is True


def test_untrusted_participant_cannot_propose_via_mcp(bridge_env, tmp_path, monkeypatch):
    """The MCP tool layer rejects untrusted requesters."""
    from perun import proposals_mcp as proposals_mcp_module

    bridge, _orchestrator = bridge_env
    bridge.settings.perunchat_trusted = ["perun-local"]
    bridge._agent_id = "perun-local"
    project, room = _make_shared_project(
        tmp_path, participants=[{"agent_id": "frambo", "roles": ["rw"]}]
    )
    monkeypatch.setenv("PERUN_PROJECT_PATH", str(Path(project["path"]).resolve()))
    monkeypatch.setenv("PERUN_CHAT_SENDER", "frambo")
    monkeypatch.setenv("PERUN_ROOM_ID", room)
    monkeypatch.setattr(proposals_mcp_module, "_settings", lambda: bridge.settings)

    with pytest.raises(ValueError, match="not on the PerunChat trust list"):
        proposals_mcp_module.propose(
            intent="change", action_type="edit", targets=["api.py"]
        )


def test_foreign_perun_id_is_not_treated_as_self(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    frame = {
        "room_id": "pub:global:test",
        "sender": {"agent_id": "frambo"},
        "payload": "@perun-other-machine hello",
    }

    decision = bridge._should_reply_to(frame)
    assert "addressed by" not in decision.reason


def test_build_prompt_injects_project_brief_only_when_authorized(settings, tmp_path):
    project, room = _make_shared_project(tmp_path)
    bridge = PerunChatBridge(MagicMock(settings=settings))
    frame = {
        "type": "CHAT",
        "id": "msg_ctx_1",
        "room_id": room,
        "sender": {"agent_id": "frambo"},
        "payload": "o czym jest ten projekt?",
    }

    authorized = bridge._build_prompt(frame, include_internal_context=True)
    assert "[end-shared-project-context]" in authorized
    assert "This room is the dedicated PerunChat chat of project" in authorized
    assert "ai-playground" in authorized

    denied = bridge._build_prompt(frame, include_internal_context=False)
    assert "[end-shared-project-context]" not in denied
    assert "This room is the dedicated PerunChat chat of project" not in denied


def test_reply_authorizes_project_owner_without_global_trust(bridge_env, tmp_path):
    bridge, orchestrator = bridge_env
    bridge._agent_id = "peruntheoverseer"
    project, room = _make_shared_project(tmp_path)
    orchestrator.run_and_wait.return_value = _run_result("ok", uid="run_1")
    frame = _chat_frame(room=room, text="o czym jest ten projekt?")
    frame["sender"]["agent_id"] = "peruntheoverseer"

    captured = {}
    real_build = bridge._build_prompt

    def spy(f, **kwargs):
        captured.update(kwargs)
        return real_build(f, **kwargs)

    with patch.object(bridge, "_build_prompt", side_effect=spy):
        bridge._reply(frame, MagicMock())

    assert captured["include_internal_context"] is True
    assert captured["project_binding"] is not None


def test_reply_denies_project_context_to_non_member(bridge_env, tmp_path):
    bridge, orchestrator = bridge_env
    bridge._agent_id = "perun-local"
    project, room = _make_shared_project(tmp_path)
    orchestrator.run_and_wait.return_value = _run_result("ok", uid="run_1")
    frame = _chat_frame(room=room, text="o czym jest ten projekt?")
    frame["sender"]["agent_id"] = "stranger"

    captured = {}
    real_build = bridge._build_prompt

    def spy(f, **kwargs):
        captured.update(kwargs)
        return real_build(f, **kwargs)

    with patch.object(bridge, "_build_prompt", side_effect=spy):
        bridge._reply(frame, MagicMock())

    assert captured["include_internal_context"] is False
    assert captured["project_binding"] is not None


def test_reply_injects_project_env_for_shared_project_chat(bridge_env, tmp_path):
    """Project-chat runs must load opencode.json and identify sender/project."""
    from perun import action_proposals

    bridge, orchestrator = bridge_env
    project, room = _make_shared_project(tmp_path)
    orchestrator.run_and_wait.return_value = _run_result("ok", uid="run_1")
    frame = _chat_frame(room=room, text="please add a README")
    frame["sender"]["agent_id"] = "frambo"

    bridge._reply(frame, MagicMock())

    call = orchestrator.run_and_wait.call_args
    assert call.kwargs["skip_instructions"] is False
    env = call.kwargs.get("extra_env") or {}
    assert env.get("PERUN_PROJECT_PATH") == str(Path(project["path"]).resolve())
    assert env.get("PERUN_CHAT_SENDER") == "frambo"
    assert env.get("PERUN_ROOM_ID") == room
    # No marker-based proposal should be created by the bridge itself.
    assert action_proposals.list_by_project(project["id"]) == []


def test_proposal_poller_posts_created_and_approved_notices(bridge_env, tmp_path, monkeypatch):
    """Lifecycle notices are posted by polling activity, not in-memory callbacks."""
    from perun import action_proposals

    bridge, _orchestrator = bridge_env
    bridge._agent_id = "peruntheoverseer"
    project, room = _make_shared_project(tmp_path)

    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        bridge, "post_to_room", lambda room_id, text, **kw: posted.append((room_id, text))
    )

    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "add a README", "action_type": "create", "targets": ["README.md"]},
    )
    bridge._poll_proposal_activity()

    assert any(
        p[0] == room and "Peer review requested" in p[1] for p in posted
    ), posted
    assert any(
        p[0] == room and "perun-proposals approve tool" in p[1] for p in posted
    )

    # Approve (without real execution) and poll again.
    monkeypatch.setattr(
        action_proposals, "start_execution", lambda _pid, **kw: action_proposals.get(_pid)
    )
    action_proposals.approve(proposal["id"], "peruntheoverseer")
    bridge._poll_proposal_activity()

    assert any(
        p[0] == room and "approved by peruntheoverseer" in p[1] for p in posted
    ), posted


def test_proposal_poller_posts_completed_digest(bridge_env, tmp_path, monkeypatch):
    """A finished proposal emits a state digest via the activity poller."""
    from perun import action_proposals

    bridge, _orchestrator = bridge_env
    bridge._agent_id = "peruntheoverseer"
    project, room = _make_shared_project(tmp_path)
    target = Path(project["path"]) / "README.md"

    posted: list[tuple[str, str, str]] = []
    def capture_post(room_id: str, text: str, content_type: str = "text/plain") -> bool:
        posted.append((room_id, text, content_type))
        return True
    monkeypatch.setattr(bridge, "post_to_room", capture_post)

    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "add a README", "action_type": "create", "targets": [str(target)]},
    )
    action_proposals.approve(proposal["id"], "peruntheoverseer")

    class RecordingOrchestrator:
        def run_and_wait(self, *args, **kwargs):
            target.write_text("# Hello\n", encoding="utf-8")
            return {"run": {"status": "done"}, "reply": "done"}

    action_proposals.execute(proposal["id"], orchestrator_instance=RecordingOrchestrator())

    # Poll from before the execution so all lifecycle events are observed.
    bridge._proposal_poller_last_id = 0
    bridge._poll_proposal_activity()

    assert any(
        p[0] == room and p[2] == "application/perun-state-digest" for p in posted
    ), posted


def test_proposal_poller_stop_waits_for_old_thread(bridge_env):
    """Stopping the bridge must wait for the old poller before a new one starts."""
    bridge, _orchestrator = bridge_env

    def slow_loop() -> None:
        while not bridge._stop.is_set():
            bridge._stop.wait(timeout=10)

    old = threading.Thread(target=slow_loop, daemon=True)
    bridge._proposal_poller_thread = old
    old.start()

    bridge._stop.set()
    bridge._stop_proposal_poller()

    assert bridge._proposal_poller_thread is None
    assert not old.is_alive()


def test_restart_starts_only_one_poller(bridge_env, monkeypatch):
    """restart() must not leave two poller threads running."""
    bridge, _orchestrator = bridge_env
    bridge._agent_id = "perun-local"
    started: list[threading.Thread] = []
    original_start = bridge._start_proposal_poller

    def spy_start() -> None:
        original_start()
        started.append(bridge._proposal_poller_thread)

    monkeypatch.setattr(bridge, "_start_proposal_poller", spy_start)

    # The main receive thread would try to connect; we only care about the poller.
    monkeypatch.setattr(bridge, "_run", lambda: None)
    bridge.settings.perunchat_api_key = ""
    bridge.enabled  # force re-check
    bridge.settings.perunchat_api_key = "prn_test_secret"

    bridge.start()
    first = bridge._proposal_poller_thread
    assert first is not None and first.is_alive()

    bridge.restart()
    second = bridge._proposal_poller_thread
    assert second is not None and second.is_alive()
    assert first is not second
    assert not first.is_alive()

    bridge.stop()


def test_heuristic_facts_extracts_identity_and_preferences(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    facts = bridge._heuristic_facts("My name is Frambo and I prefer morning standups.", "frambo", "priv:test")
    texts = [f[0] for f in facts]
    types = [f[1] for f in facts]
    assert any("Frambo" in t for t in texts)
    assert any("morning standups" in t for t in texts)
    assert "person" in types
    assert "preference" in types


def test_heuristic_facts_extracts_decisions_and_actions(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    facts = bridge._heuristic_facts("We decided to use FastAPI. We should deploy next week.", "frambo", "priv:test")
    types = [f[1] for f in facts]
    assert "decision" in types
    assert "action" in types


def test_store_fact_deduped_skips_near_duplicates(settings, monkeypatch):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    calls = []

    def fake_search(query, project_id=0, top_k=4, include_global=True):
        return [{"source_type": "fact", "score": 0.95, "content": "existing"}]

    def fake_remember(content, **kwargs):
        calls.append(content)

    monkeypatch.setattr("perun.perunchat_bridge.memory.search_facts", fake_search)
    monkeypatch.setattr("perun.perunchat_bridge.memory.remember", fake_remember)

    bridge._store_fact_deduped("We use FastAPI", "project", {"room": "priv:test", "sender": "frambo"})
    assert len(calls) == 0


def test_store_fact_deduped_stores_new_facts(settings, monkeypatch):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    calls = []

    def fake_search(query, project_id=0, top_k=4, include_global=True):
        return [{"source_type": "fact", "score": 0.75, "content": "other"}]

    def fake_remember(content, **kwargs):
        calls.append((content, kwargs.get("meta"), kwargs))

    monkeypatch.setattr("perun.perunchat_bridge.memory.search_facts", fake_search)
    monkeypatch.setattr("perun.perunchat_bridge.memory.remember", fake_remember)

    bridge._store_fact_deduped("We use FastAPI", "project", {"room": "priv:test", "sender": "frambo"})
    assert len(calls) == 1
    assert calls[0][0] == "We use FastAPI"
    assert calls[0][1]["type"] == "project"
    assert calls[0][2]["project_id"] == -1
    assert calls[0][2]["scope"] == "room"


def test_store_fact_deduped_uses_project_scope_for_project_room(
    settings, tmp_path, monkeypatch
):
    project, room = _make_shared_project(tmp_path, participants=[{"agent_id": "frambo"}])
    bridge = PerunChatBridge(MagicMock(settings=settings))
    calls = []

    monkeypatch.setattr(
        "perun.perunchat_bridge.memory.search_facts",
        lambda *a, **kw: [],
    )
    monkeypatch.setattr(
        "perun.perunchat_bridge.memory.remember",
        lambda content, **kw: calls.append(kw),
    )

    bridge._store_fact_deduped(
        "The API uses FastAPI", "decision", {"room": room, "sender": "frambo"}
    )

    assert calls[0]["project_id"] == project["id"]
    assert calls[0]["scope"] == "project"


def test_fact_visibility_is_room_scoped():
    visible = PerunChatBridge._fact_visible_in_room
    assert visible({"project_id": -1, "meta": {"room": "priv:a"}}, -1, "priv:a")
    assert not visible({"project_id": -1, "meta": {"room": "priv:b"}}, -1, "priv:a")
    assert visible({"project_id": 0, "meta": {}}, 42, "grp:x")
    assert not visible({"project_id": 0, "meta": {"room": "pub:other"}}, 42, "grp:x")
    assert visible({"project_id": 42, "meta": {"room": "grp:x"}}, 42, "grp:x")
    assert not visible({"project_id": 7, "meta": {"room": "grp:y"}}, 42, "grp:x")


def test_store_transcript_fact_skips_public_rooms(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    frame = {
        "id": "msg_pub_1",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "payload": "hello public room",
    }
    bridge._store_transcript_fact(frame)
    from perun import db

    with db.db() as conn:
        rows = db.rows(conn, "SELECT * FROM memory WHERE room_id=?", ("pub:global:test",))
    assert len(rows) == 0


def test_store_transcript_fact_stores_private_room_transcripts(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    room = "priv:alice-bob"
    frame = {
        "id": "msg_priv_1",
        "room_id": room,
        "sender": {"agent_id": "alice"},
        "payload": "hello private room",
    }
    bridge._store_transcript_fact(frame)
    from perun import db

    with db.db() as conn:
        rows = db.rows(conn, "SELECT * FROM memory WHERE room_id=?", (room,))
    assert len(rows) == 1
    assert rows[0]["project_id"] == -1
    assert rows[0]["scope"] == "room"
    assert "alice: hello private room" in rows[0]["content"]


def test_store_transcript_fact_stores_project_room_transcripts(settings, tmp_path):
    project, room = _make_shared_project(tmp_path, slug="transcript-proj")
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    frame = {
        "id": "msg_grp_1",
        "room_id": room,
        "sender": {"agent_id": "frambo"},
        "payload": "hello project room",
    }
    bridge._store_transcript_fact(frame)
    from perun import db

    with db.db() as conn:
        rows = db.rows(conn, "SELECT * FROM memory WHERE room_id=?", (room,))
    assert len(rows) == 1
    assert rows[0]["project_id"] == project["id"]
    assert rows[0]["scope"] == "project"


def test_store_transcript_fact_prunes_old_transcripts(settings, monkeypatch):
    from perun import perunchat_bridge as pb

    monkeypatch.setattr(pb, "_MAX_ROOM_TRANSCRIPT_FACTS", 3)
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    room = "priv:prune"
    for i in range(5):
        frame = {
            "id": f"msg_prune_{i}",
            "room_id": room,
            "sender": {"agent_id": "alice"},
            "payload": f"message {i}",
        }
        bridge._store_transcript_fact(frame)
    from perun import db

    with db.db() as conn:
        rows = db.rows(conn, "SELECT * FROM memory WHERE room_id=? ORDER BY id", (room,))
    assert len(rows) == 3
    contents = [r["content"] for r in rows]
    assert "alice: message 0" not in contents
    assert "alice: message 1" not in contents
    assert "alice: message 2" in contents
    assert "alice: message 4" in contents


def test_default_max_room_transcript_facts_is_250():
    from perun import perunchat_bridge as pb

    assert pb._MAX_ROOM_TRANSCRIPT_FACTS == 250


def test_prompt_memory_filters_other_rooms(settings, monkeypatch):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    facts = [
        {"content": "same room fact", "project_id": -1, "meta": {"room": "priv:test", "sender": "a"}},
        {"content": "other room secret", "project_id": -1, "meta": {"room": "priv:other", "sender": "b"}},
        {"content": "global fact", "project_id": 0, "meta": {}},
    ]
    monkeypatch.setattr("perun.perunchat_bridge.memory.search_facts", lambda *a, **kw: facts)
    frame = {
        "room_id": "priv:test",
        "sender": {"agent_id": "frambo"},
        "payload": "hello",
    }

    prompt = bridge._build_prompt(frame, include_history=False)

    assert "same room fact" in prompt
    assert "global fact" in prompt
    assert "other room secret" not in prompt


def test_fact_markers_stripped_from_reply_payload(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    reply = "Let's do it. [FACT(type=action): Deploy the API] And more text."
    clean = re.sub(r"\s*\[FACT(?:\([^)]*\))?:\s*.+?\]\s*", " ", reply, flags=re.IGNORECASE)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    assert "[FACT" not in clean
    assert "Deploy the API" not in clean
    assert "Let's do it." in clean
    assert "And more text." in clean


def _frame_at_ts(ts_ms: int) -> dict:
    """Frame with an explicit timestamp (deterministic, no wall clock)."""
    return {
        "id": "msg_stale",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "other-agent"},
        "content_type": "text/plain",
        "payload": "hello perun",
        "timestamp": ts_ms,
    }


# Fixed epoch for deterministic age tests: two wall-clock reads can straddle a
# millisecond boundary and make an exact-age assertion flaky.
_T0 = 1_760_000_000_000


def test_missed_while_offline_detects_old_message(settings):
    """A message older than the connect time is reported with its age."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._connect_epoch_ms = _T0
    assert bridge._missed_while_offline(_frame_at_ts(_T0 - 30 * 60_000)) == 30


def test_missed_while_offline_ignores_live_messages(settings):
    """Messages at/after connect time are live - no catch-up."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._connect_epoch_ms = _T0
    assert bridge._missed_while_offline(_frame_at_ts(_T0)) is None
    assert bridge._missed_while_offline(_frame_at_ts(_T0 + 5_000)) is None


def test_missed_while_offline_never_negative(settings):
    """Clock skew (timestamp in the future) must not report a negative age."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._connect_epoch_ms = _T0
    assert bridge._missed_while_offline(_frame_at_ts(_T0 + 120_000)) is None


def test_missed_while_offline_threshold_boundary(settings):
    """Catch-up kicks in at 2 min + 60 s skew tolerance, not a second earlier."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._connect_epoch_ms = _T0
    # Just inside the effective 180 s threshold: still treated as live.
    assert bridge._missed_while_offline(_frame_at_ts(_T0 - 179_999)) is None
    # Exactly at/over the threshold: reported (3 minutes).
    assert bridge._missed_while_offline(_frame_at_ts(_T0 - 180_000)) == 3
    # The bare 120 s constant alone does not trigger (skew tolerance applies).
    assert bridge._missed_while_offline(_frame_at_ts(_T0 - 120_000)) is None


def test_missed_while_offline_missing_timestamp(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._connect_epoch_ms = 1
    frame = _frame_at_ts(_T0 + 1_000_000)
    frame.pop("timestamp")
    assert bridge._missed_while_offline(frame) is None


def test_missed_while_offline_malformed_timestamp(settings):
    """bool/NaN/Infinity/string timestamps must never raise."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._connect_epoch_ms = _T0
    for bad in (True, float("nan"), float("inf"), float("-inf"), "not-a-ts", None, 0, -1):
        assert bridge._missed_while_offline(_frame_at_ts(bad)) is None, bad


def test_build_prompt_includes_catchup_note_for_stale_message(settings):
    """Stale replayed messages get a 'catching up' note, not a live reply."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._connect_epoch_ms = _T0
    prompt = bridge._build_prompt(_frame_at_ts(_T0 - 10 * 60_000))
    assert "catching up" in prompt


def test_build_prompt_no_catchup_note_for_live_message(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._connect_epoch_ms = _T0
    prompt = bridge._build_prompt(_frame_at_ts(_T0))
    assert "catching up" not in prompt


def test_build_prompt_first_message_discourages_introduction(settings):
    """The first message in an empty room must not invite a self-introduction."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._connect_epoch_ms = _T0
    prompt = bridge._build_prompt(_chat_frame())
    assert "first message you see in this room" in prompt
    assert "Do not introduce yourself" in prompt


def test_public_room_keeps_more_inline_history(settings, monkeypatch):
    """Public rooms get more inline context because they have no room memory."""
    from perun import memory

    monkeypatch.setattr("perun.perunchat_bridge.memory.search_facts", lambda *a, **kw: [])
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    room_id = "pub:global:test"
    hist = bridge._room_history.setdefault(room_id, deque(maxlen=40))
    for i in range(5):
        hist.append({
            "id": f"msg_ctx_{i}",
            "room_id": room_id,
            "sender": {"agent_id": "other-agent"},
            "payload": f"context-message-{i}",
        })
    frame = {
        "id": "msg_current",
        "room_id": room_id,
        "sender": {"agent_id": "other-agent"},
        "payload": "current question",
    }
    prompt = bridge._build_prompt(frame)
    # All five history lines should survive (public limit is 6).
    for i in range(5):
        assert f"context-message-{i}" in prompt


def test_private_room_keeps_limited_inline_history(settings, monkeypatch):
    """Private rooms rely on room memory; only the 4 most recent lines are inline."""
    from perun import memory

    monkeypatch.setattr("perun.perunchat_bridge.memory.search_facts", lambda *a, **kw: [])
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    room_id = "priv:alice-bob"
    hist = bridge._room_history.setdefault(room_id, deque(maxlen=40))
    for i in range(5):
        hist.append({
            "id": f"msg_ctx_{i}",
            "room_id": room_id,
            "sender": {"agent_id": "other-agent"},
            "payload": f"context-message-{i}",
        })
    frame = {
        "id": "msg_current",
        "room_id": room_id,
        "sender": {"agent_id": "other-agent"},
        "payload": "current question",
    }
    prompt = bridge._build_prompt(frame)
    # The four newest history lines are inline.
    assert "context-message-1" in prompt
    assert "context-message-2" in prompt
    assert "context-message-3" in prompt
    assert "context-message-4" in prompt
    assert "context-message-0" not in prompt


# ---------------------------------------------------------------------------
# No-loss delivery guarantees.
# A reply must never be dropped because a run came back empty, because the
# socket died between generation and send, or because the bridge restarted.
# ---------------------------------------------------------------------------


def _chat_frame(msg_id="msg_in_1", room="pub:global:test", text="hello perun"):
    return {
        "type": "CHAT",
        "id": msg_id,
        "room_id": room,
        "sender": {"agent_id": "other-agent"},
        "payload": text,
    }


@pytest.fixture
def bridge_env(settings, tmp_path, monkeypatch, control_repo):
    """Bridge with isolated state files and zero artificial delays."""
    from perun import perunchat_bridge as pb

    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(pb, "_TYPING_DELAY_MIN", 0)
    monkeypatch.setattr(pb, "_TYPING_DELAY_MAX", 0)
    monkeypatch.setattr(pb, "_REPLY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(pb, "_OUTBOX_FLUSH_DELAY_SECONDS", 0)
    # Mirror production: PerunChat replies run on the dedicated "perunchat"
    # agent. The model is never executed here (the orchestrator is mocked); the
    # value only satisfies the "model configured" guard in _reply.
    settings.perunchat_chat_agent = "perunchat"
    settings.perunchat_chat_model = "opencode/test-chat-model"
    orchestrator = MagicMock(settings=settings)
    bridge = PerunChatBridge(orchestrator)
    bridge._agent_id = "perun-local"
    return bridge, orchestrator


def _run_result(reply, status="done", uid="run_1"):
    return {"reply": reply, "run": {"run_uid": uid, "status": status}}


def test_reply_retries_empty_run_instead_of_dropping(bridge_env):
    """A cancelled/empty run must be regenerated once, never silently dropped."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.side_effect = [
        _run_result("", status="cancelled", uid="run_1"),
        _run_result("pong", uid="run_2"),
    ]
    ws = MagicMock()
    bridge._reply(_chat_frame(), ws)
    assert orchestrator.run_and_wait.call_count == 2
    # Both attempts run on the dedicated perunchat agent, not a generic one.
    assert all(c.kwargs.get("agent") == "perunchat"
               for c in orchestrator.run_and_wait.call_args_list)
    ws.send.assert_called_once()
    sent = ws.send.call_args[0][0]
    assert "pong" in sent


def test_reply_waits_for_running_run_instead_of_duplicate(bridge_env):
    """A still-running run is awaited, not re-run (no duplicate generation)."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("", status="running", uid="run_1")
    orchestrator.wait_for_run.return_value = _run_result("late reply", uid="run_1")
    ws = MagicMock()
    bridge._reply(_chat_frame(), ws)
    assert orchestrator.run_and_wait.call_count == 1
    orchestrator.wait_for_run.assert_called_once()
    ws.send.assert_called_once()
    assert "late reply" in ws.send.call_args[0][0]


def test_reply_gives_up_loudly_after_final_empty_runs(bridge_env):
    """Two final-empty runs: an idle typing frame is sent and a loud log line emitted (not a silent drop)."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.side_effect = [
        _run_result("", status="cancelled", uid="run_1"),
        _run_result("", status="cancelled", uid="run_2"),
    ]
    ws = MagicMock()
    with patch("perun.perunchat_bridge.logger") as mock_logger:
        bridge._reply(_chat_frame(), ws)
    # Only the idle typing-indicator clear frame is sent.
    ws.send.assert_called_once()
    assert "application/perun-typing" in ws.send.call_args[0][0]
    assert '"payload": "idle"' in ws.send.call_args[0][0]
    assert mock_logger.error.called
    assert bridge._load_outbox() == []


def test_no_reply_decision_is_not_sent(bridge_env):
    """An explicit <NO_REPLY> decision sends only an idle typing frame, no chat reply."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("<NO_REPLY>", uid="run_1")
    ws = MagicMock()
    bridge._reply(_chat_frame(), ws)
    ws.send.assert_called_once()
    assert "application/perun-typing" in ws.send.call_args[0][0]
    assert '"payload": "idle"' in ws.send.call_args[0][0]
    assert bridge._load_outbox() == []


def test_send_failure_keeps_frame_and_flush_resends_same_id_and_seq(bridge_env):
    """A dead socket keeps the frame durable; the resend is idempotent."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("hello there", uid="run_1")
    failing_ws = MagicMock()
    failing_ws.send.side_effect = OSError("connection dropped")
    bridge._reply(_chat_frame(), failing_ws)
    # The reply attempt and the subsequent idle typing frame both fail to send.
    assert failing_ws.send.call_count == 2
    queued = bridge._load_outbox()
    assert len(queued) == 1
    assert "hello there" in queued[0]["payload"]
    # Reconnect: the SAME frame (id + sequence_number) goes out, so the
    # gateway's monotonic-sequence check prevents a possible duplicate.
    working_ws = MagicMock()
    bridge._flush_outbox(working_ws)
    working_ws.send.assert_called_once()
    resent = working_ws.send.call_args[0][0]
    assert resent == json.dumps(queued[0])
    assert bridge._load_outbox() == []


def test_send_failure_marks_original_replied_and_does_not_regenerate_on_replay(bridge_env):
    """A failed send still marks the source message replied so a gateway replay
    does not spawn a second LLM run while the queued reply is pending."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("hello there", uid="run_1")
    frame = _chat_frame(text="@perun-local hello")
    failing_ws = MagicMock()
    failing_ws.send.side_effect = OSError("connection dropped")

    bridge._on_frame(frame, failing_ws)
    bridge._drain_room_queues()

    # The reply was generated once and queued.
    orchestrator.run_and_wait.assert_called_once()
    assert frame["id"] in bridge._recent_replied

    # Replaying the same frame must not trigger another reply generation.
    orchestrator.run_and_wait.reset_mock()
    replay_ws = MagicMock()
    bridge._on_frame(frame, replay_ws)
    bridge._drain_room_queues()
    orchestrator.run_and_wait.assert_not_called()


def test_typing_indicator_and_reply_have_monotonic_sequence_numbers(bridge_env):
    """Typing frames must consume a sequence number so the following reply is not
    rejected by the gateway as non-monotonic."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("hello there", uid="run_1")
    frame = _chat_frame()
    ws = MagicMock()
    bridge._seq = 10

    sent_frames: list[dict] = []

    def capture_send(sock, f, *, write_ahead=True):
        sent_frames.append(f)
        return True

    with patch.object(bridge, "_send_frame", side_effect=capture_send):
        bridge._send_typing_indicator(frame, ws, typing=True)
        bridge._reply(frame, ws)

    assert len(sent_frames) == 2
    typing_frame, reply_frame = sent_frames
    assert typing_frame["content_type"] == "application/perun-typing"
    assert reply_frame["content_type"] == "text/plain"
    assert typing_frame["sequence_number"] == 11
    assert reply_frame["sequence_number"] == 12
    assert bridge._seq == reply_frame["sequence_number"]


def test_hard_timeout_cancels_orchestrator_run(bridge_env, monkeypatch):
    """When reply generation exceeds the wall-clock timeout, the orchestrator
    run must be cancelled and the background reply thread must not send."""
    bridge, orchestrator = bridge_env
    frame = _chat_frame()
    ws = MagicMock()

    # Make _reply block for longer than the timeout, but publish a run uid first
    # so the timeout handler can cancel it.
    slow_started = threading.Event()

    def slow_reply(f, sock, *, cancel_event=None, run_uid_container=None):
        if run_uid_container is not None:
            with run_uid_container["lock"]:
                run_uid_container["uid"] = "run_slow"
        slow_started.set()
        # Block until cancel_event is set (or a very long time).
        cancel_event.wait(timeout=30)
        # If cancellation did not stop us, sending here would be the bug.
        bridge._send_frame(sock, {
            "id": f"msg_{uuid.uuid4().hex}",
            "sequence_number": bridge._seq + 1,
            "room_id": f.get("room_id"),
            "sender": {"agent_id": bridge._agent_id, "runtime": "perun"},
            "content_type": "text/plain",
            "payload": "delayed reply",
        })

    import perun.perunchat_bridge as pcb
    monkeypatch.setattr(pcb, "_MAX_REPLY_WALL_SECONDS", 0.2)
    with patch.object(bridge, "_reply", side_effect=slow_reply):
        bridge._process_room_frame(frame, ws)

    slow_started.wait(timeout=2)
    # The orchestrator cancel must have been called for the slow run.
    orchestrator.cancel.assert_called_once_with("run_slow")
    # No reply frame should have been sent through the WebSocket.
    ws.send.assert_not_called()


def test_direct_mentions_bypass_per_room_cooldown(bridge_env):
    """A burst of @mentions must not be dropped by the proactive chime-in
    cooldown; each direct address gets its own reply."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("reply", uid="run_1")
    ws = MagicMock()

    bridge._on_frame(_chat_frame(msg_id="msg_1", text="@perun-local hello"), ws)
    bridge._drain_room_queues()
    bridge._on_frame(_chat_frame(msg_id="msg_2", text="@perun-local second"), ws)
    bridge._drain_room_queues()

    assert orchestrator.run_and_wait.call_count == 2


def test_proactive_reply_respects_per_room_cooldown(bridge_env):
    """Non-mention replies are still throttled by the per-room cooldown."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("reply", uid="run_1")
    ws = MagicMock()

    bridge._on_frame(_chat_frame(msg_id="msg_1", text="hello everyone"), ws)
    bridge._drain_room_queues()
    bridge._on_frame(_chat_frame(msg_id="msg_2", text="anyone here?"), ws)
    bridge._drain_room_queues()

    assert orchestrator.run_and_wait.call_count == 1


def test_direct_mentions_bypass_room_saturation(bridge_env):
    """Direct mentions are answered even when the proactive saturation guard
    would block further unsolicited replies."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("reply", uid="run_1")
    ws = MagicMock()
    room_id = "pub:global:test"

    # Artificially saturate the room with 3 recent Perun replies.
    for _ in range(3):
        bridge._room_reply_activity.setdefault(room_id, deque(maxlen=40)).append(time.time())

    bridge._on_frame(_chat_frame(msg_id="dir_1", room=room_id, text="@perun-local hello"), ws)
    bridge._drain_room_queues()
    orchestrator.run_and_wait.assert_called_once()

    # A proactive message is still blocked by saturation.
    orchestrator.run_and_wait.reset_mock()
    bridge._on_frame(_chat_frame(msg_id="pro_1", room=room_id, text="hello everyone"), ws)
    bridge._drain_room_queues()
    orchestrator.run_and_wait.assert_not_called()


def test_private_room_bypasses_cooldown_and_saturation(bridge_env):
    """Private-room messages bypass the proactive cooldown and saturation
    guards so 1:1 conversations are not throttled."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("reply", uid="run_1")
    ws = MagicMock()
    room_id = "priv:user1:user2"

    # Force both cooldown and saturation.
    bridge._last_reply_time[room_id] = time.time()
    for _ in range(3):
        bridge._room_reply_activity.setdefault(room_id, deque(maxlen=40)).append(time.time())

    bridge._on_frame(_chat_frame(msg_id="priv_1", room=room_id, text="hello"), ws)
    bridge._drain_room_queues()
    orchestrator.run_and_wait.assert_called_once()


def test_reply_to_own_message_bypasses_cooldown_and_saturation(bridge_env):
    """Replies directed at one of our own messages bypass the proactive
    cooldown and saturation guards."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("reply", uid="run_1")
    ws = MagicMock()
    room_id = "pub:global:test"
    own_msg_id = "msg_own_1"
    bridge._own_message_ids.add(own_msg_id)

    # Force both cooldown and saturation.
    bridge._last_reply_time[room_id] = time.time()
    for _ in range(3):
        bridge._room_reply_activity.setdefault(room_id, deque(maxlen=40)).append(time.time())

    frame = _chat_frame(msg_id="reply_1", room=room_id, text="yes exactly")
    frame["in_reply_to"] = own_msg_id
    bridge._on_frame(frame, ws)
    bridge._drain_room_queues()
    orchestrator.run_and_wait.assert_called_once()


def test_flush_outbox_orders_by_sequence(bridge_env):
    """Older queued frames are resent first so none is rejected as stale."""
    bridge, _ = bridge_env
    frames = [
        {"id": "b", "sequence_number": 12, "payload": "second"},
        {"id": "a", "sequence_number": 11, "payload": "first"},
    ]
    bridge._store_outbox(frames)
    ws = MagicMock()
    bridge._flush_outbox(ws)
    sent = [json.loads(c[0][0])["id"] for c in ws.send.call_args_list]
    assert sent == ["a", "b"]


def test_replied_ids_survive_restart(bridge_env):
    """A restart must not reply again to messages the gateway replays."""
    bridge, _ = bridge_env
    bridge._mark_replied("msg_seen_before_restart")
    assert (config.HOME / "perunchat_replied.json").is_file()
    restarted = PerunChatBridge(MagicMock(settings=bridge.settings))
    restarted._agent_id = "perun-local"
    assert "msg_seen_before_restart" in restarted._recent_replied
    with patch.object(restarted, "_reply") as mock_reply:
        restarted._on_frame(_chat_frame(msg_id="msg_seen_before_restart",
                                        room="priv:abc123"), None)
        mock_reply.assert_not_called()


def test_flush_outbox_stops_at_first_failure(bridge_env):
    """A failed resend must not skip ahead: later frames stay queued."""
    bridge, _ = bridge_env
    frames = [
        {"id": "first", "sequence_number": 10, "payload": "a"},
        {"id": "second", "sequence_number": 11, "payload": "b"},
    ]
    bridge._store_outbox(frames)
    ws = MagicMock()
    ws.send.side_effect = OSError("boom")
    bridge._flush_outbox(ws)
    assert ws.send.call_count == 1
    remaining = bridge._load_outbox()
    assert [f["id"] for f in remaining] == ["first", "second"]


def test_build_prompt_includes_participant_roster(settings):
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    bridge._room_participants["pub:global:test"] = {
        "frambo_malina_omarchy": time.time(),
        "other-agent": time.time(),
        "perun-local": time.time(),
    }
    frame = {
        "id": "msg_q",
        "room_id": "pub:global:test",
        "sender": {"agent_id": "frambo_malina_omarchy"},
        "payload": "how many agents have you talked to?",
    }
    prompt = bridge._build_prompt(frame)
    assert "Participants you have seen in this room: frambo_malina_omarchy, other-agent (2 shown" in prompt
    # Perun itself is excluded from the roster.
    assert "perun-local" not in prompt.split("Participants you have seen")[1]


def test_ensure_opencode_agent_updates_existing_file(monkeypatch, tmp_path):
    """The managed perunchat agent file is overwritten when the embedded content changes."""
    from perun import perunchat_bridge as pb

    agents_dir = tmp_path / ".config" / "opencode" / "agents"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    agent_file = agents_dir / "perunchat.md"
    agent_file.parent.mkdir(parents=True, exist_ok=True)
    agent_file.write_text("old content", encoding="utf-8")
    pb._ensure_opencode_agent()
    assert agent_file.read_text(encoding="utf-8").strip() == pb._OPENCODE_AGENT_MD.strip()


def test_dead_room_worker_is_replaced(bridge_env):
    """A crashed room worker must be detected and replaced so queued frames are
    not stranded."""
    bridge, _ = bridge_env
    room_id = "pub:global:test"

    bridge._ensure_room_worker(room_id)
    dead_worker = bridge._room_workers[room_id]
    assert dead_worker.is_alive()

    # Simulate a dead worker that has not run its cleanup yet.
    class FakeDeadThread:
        def is_alive(self):  # noqa: D102
            return False

        def join(self, timeout=None):  # noqa: D102
            pass

    bridge._room_workers[room_id] = FakeDeadThread()
    old_queue = bridge._room_queues[room_id]

    bridge._ensure_room_worker(room_id)
    new_worker = bridge._room_workers[room_id]
    assert new_worker is not dead_worker
    assert new_worker.is_alive()
    # The existing queue should be reused, not dropped.
    assert bridge._room_queues[room_id] is old_queue




def test_project_context_includes_manifest_dependencies(settings, tmp_path):
    """The shared-project context block lists cross-project dependencies."""
    project, room = _make_shared_project(
        tmp_path,
        slug="deps-project",
        description="Project with dependencies",
    )
    from perun import shared_projects

    project["manifest"] = {
        "dependencies": [
            {
                "slug": "perunchat",
                "relationship": "sibling",
                "reason": "Federated chat gateway",
            },
            {"slug": "perun-in-the-machine", "relationship": "sibling"},
        ]
    }
    cfg = shared_projects.load(project["path"])

    bridge = PerunChatBridge(MagicMock(settings=settings))
    ctx = bridge._shared_project_context(room, binding=(project, cfg))

    assert "Project dependencies (from perun.md):" in ctx
    assert "perunchat (sibling): Federated chat gateway" in ctx
    assert "perun-in-the-machine (sibling)" in ctx


def test_execution_transfer_status_reports_missing_chunks(settings):
    """A `transfer_status` op returns the received and missing chunk indices."""
    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    bridge._ws = MagicMock()
    sent = []

    def capture_send(sock, f, **kwargs):
        sent.append(f)
        return True

    with patch.object(bridge, "_send_frame", side_effect=capture_send):
        with patch.object(
            bridge,
            "_project_binding",
            return_value=({"id": 1, "slug": "test", "path": ""}, {}),
        ):
            bridge._package_transfers["trf_123"] = {
                "proposal_id": "prop_abc",
                "sender": "frambo",
                "direction": "upload",
                "sha256": "abc",
                "total_size": 100,
                "total_chunks": 5,
                "received": {0: "Zg==", 2: "Zg=="},
                "started_at": time.time(),
            }
            frame = {
                "room_id": "grp:test:1:test",
                "sender": {"agent_id": "frambo"},
                "content_type": "application/perun-execution",
                "payload": {"op": "transfer_status", "transfer_id": "trf_123"},
            }
            bridge._handle_execution_frame(frame, None)

    assert len(sent) == 1
    payload = sent[0]["payload"]
    assert payload["op"] == "transfer_status"
    assert payload["ok"] is True
    assert payload["total_chunks"] == 5
    assert payload["received_chunks"] == [0, 2]
    assert payload["missing"] == [1, 3, 4]


def test_upload_transfer_registers_and_receives_chunks(settings, tmp_path):
    """A package_begin registers a transfer and chunks are reassembled."""
    from perun import action_proposals

    project, room = _make_shared_project(tmp_path, participants=[{"agent_id": "frambo", "roles": ["rw"]}])
    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "edit api", "targets": ["api.py"], "action_type": "edit"},
    )
    proposal = action_proposals.approve(proposal["id"], "owner")
    proposal = action_proposals.claim_execution(proposal["id"], "frambo")

    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    bridge._ws = MagicMock()
    sent = []

    def capture_send(sock, f, **kwargs):
        sent.append(f)
        return True

    with patch.object(bridge, "_send_frame", side_effect=capture_send):
        with patch.object(bridge, "_project_binding", return_value=(project, {})):
            frame = {
                "room_id": room,
                "sender": {"agent_id": "frambo"},
                "content_type": "application/perun-execution",
                "payload": {
                    "op": "package_begin",
                    "proposal_id": proposal["id"],
                    "transfer_id": "trf_up",
                    "sha256": package_handoff.sha256_hex(b"hello"),
                    "total_size": 5,
                    "total_chunks": 1,
                },
            }
            bridge._handle_execution_frame(frame, None)
            # Reply is posted, not returned.
            assert len(sent) == 1
            assert sent[0]["content_type"] == "application/perun-execution-result"
            assert sent[0]["payload"]["ok"] is True

            # package chunk
            chunk_frame = {
                "room_id": room,
                "sender": {"agent_id": "frambo"},
                "content_type": "application/perun-package",
                "payload": {"transfer_id": "trf_up", "seq": 0, "total_chunks": 1, "data": "aGVsbG8="},
            }
            bridge._handle_package_frame(chunk_frame, None)

            transfer = bridge._package_transfers.get("trf_up")
            assert transfer is not None
            assert transfer["received"] == {0: "aGVsbG8="}


def test_upload_transfer_persists_to_db_and_reloads(settings, tmp_path):
    """Transfer slots survive a bridge restart because they are persisted."""
    from perun import action_proposals, db as db_module

    project, room = _make_shared_project(tmp_path, participants=[{"agent_id": "frambo", "roles": ["rw"]}])
    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "edit api", "targets": ["api.py"], "action_type": "edit"},
    )
    proposal = action_proposals.approve(proposal["id"], "owner")
    proposal = action_proposals.claim_execution(proposal["id"], "frambo")

    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    bridge._ws = MagicMock()

    with patch.object(bridge, "_project_binding", return_value=(project, {})):
        frame = {
            "room_id": room,
            "sender": {"agent_id": "frambo"},
            "content_type": "application/perun-execution",
            "payload": {
                "op": "package_begin",
                "proposal_id": proposal["id"],
                "transfer_id": "trf_persist",
                "sha256": package_handoff.sha256_hex(b"hello"),
                "total_size": 5,
                "total_chunks": 1,
            },
        }
        bridge._handle_execution_frame(frame, None)
        chunk_frame = {
            "room_id": room,
            "sender": {"agent_id": "frambo"},
            "content_type": "application/perun-package",
            "payload": {"transfer_id": "trf_persist", "seq": 0, "total_chunks": 1, "data": "aGVsbG8="},
        }
        bridge._handle_package_frame(chunk_frame, None)

    # Verify DB row exists.
    with db_module.db() as conn:
        row = db_module.one(conn, "SELECT * FROM package_transfers WHERE transfer_id=?", ("trf_persist",))
    assert row is not None
    assert json.loads(row["received"]) == {"0": "aGVsbG8="}

    # Simulate restart: a new bridge instance loads the slot.
    restarted = PerunChatBridge(MagicMock(settings=settings))
    loaded = restarted._package_transfers.get("trf_persist")
    assert loaded is not None
    assert loaded["received"] == {0: "aGVsbG8="}
    assert loaded["proposal_id"] == proposal["id"]


def test_download_transfer_publishes_package_frames(settings, tmp_path):
    """request_download_package publishes package_ready and chunks."""
    from perun import action_proposals

    project, room = _make_shared_project(tmp_path, participants=[{"agent_id": "frambo", "roles": ["rw"]}])
    root = Path(project["path"])
    (root / "api.py").write_text("old")
    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "edit api", "targets": ["api.py"], "action_type": "edit", "room_id": room},
    )
    proposal = action_proposals.approve(proposal["id"], "owner")
    proposal = action_proposals.claim_execution(proposal["id"], "frambo", owner="owner")

    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    bridge._ws = MagicMock()
    sent = []

    def capture_send(sock, f, **kwargs):
        sent.append(f)
        return True

    with patch.object(bridge, "_send_frame", side_effect=capture_send):
        with patch.object(bridge, "_project_binding", return_value=(project, {})):
            frame = {
                "room_id": room,
                "sender": {"agent_id": "frambo"},
                "content_type": "application/perun-execution",
                "payload": {
                    "op": "request_download_package",
                    "proposal_id": proposal["id"],
                },
            }
            bridge._handle_execution_frame(frame, None)

    # package_ready + chunk + result reply
    assert len(sent) == 3
    assert sent[0]["content_type"] == "application/perun-execution"
    assert sent[0]["payload"]["op"] == "package_ready"
    assert sent[1]["content_type"] == "application/perun-package"
    assert sent[1]["payload"]["seq"] == 0


def test_download_transfer_rejected_without_claim(settings, tmp_path):
    """request_download_package requires the proposal to be claimed by the sender."""
    from perun import action_proposals

    project, room = _make_shared_project(tmp_path, participants=[{"agent_id": "frambo", "roles": ["rw"]}])
    root = Path(project["path"])
    (root / "api.py").write_text("old")
    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "edit api", "targets": ["api.py"], "action_type": "edit", "room_id": room},
    )
    proposal = action_proposals.approve(proposal["id"], "owner")

    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    bridge._ws = MagicMock()
    sent = []

    def capture_send(sock, f, **kwargs):
        sent.append(f)
        return True

    with patch.object(bridge, "_send_frame", side_effect=capture_send):
        with patch.object(bridge, "_project_binding", return_value=(project, {})):
            frame = {
                "room_id": room,
                "sender": {"agent_id": "frambo"},
                "content_type": "application/perun-execution",
                "payload": {
                    "op": "request_download_package",
                    "proposal_id": proposal["id"],
                },
            }
            bridge._handle_execution_frame(frame, None)

    assert len(sent) == 1
    assert sent[0]["content_type"] == "application/perun-execution-result"
    assert sent[0]["payload"]["ok"] is False
    assert "claim" in sent[0]["payload"]["error"]


def test_package_end_rejects_size_mismatch(settings, tmp_path):
    """package_end rejects when decoded bytes do not match the announced total_size."""
    from perun import action_proposals

    project, room = _make_shared_project(tmp_path, participants=[{"agent_id": "frambo", "roles": ["rw"]}])
    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "edit api", "targets": ["api.py"], "action_type": "edit", "room_id": room},
    )
    proposal = action_proposals.approve(proposal["id"], "owner")
    proposal = action_proposals.claim_execution(proposal["id"], "frambo", owner="owner")

    bridge = PerunChatBridge(MagicMock(settings=settings))
    bridge._agent_id = "perun-local"
    bridge._ws = MagicMock()

    with patch.object(bridge, "_project_binding", return_value=(project, {})):
        begin_frame = {
            "room_id": room,
            "sender": {"agent_id": "frambo"},
            "content_type": "application/perun-execution",
            "payload": {
                "op": "package_begin",
                "proposal_id": proposal["id"],
                "transfer_id": "trf_size",
                "sha256": package_handoff.sha256_hex(b"hello"),
                "total_size": 999,
                "total_chunks": 1,
            },
        }
        bridge._handle_execution_frame(begin_frame, None)
        chunk_frame = {
            "room_id": room,
            "sender": {"agent_id": "frambo"},
            "content_type": "application/perun-package",
            "payload": {"transfer_id": "trf_size", "seq": 0, "total_chunks": 1, "data": "aGVsbG8="},
        }
        bridge._handle_package_frame(chunk_frame, None)

    end_frame = {
        "room_id": room,
        "sender": {"agent_id": "frambo"},
        "content_type": "application/perun-execution",
        "payload": {"op": "package_end", "transfer_id": "trf_size"},
    }
    bridge._handle_execution_frame(end_frame, None)
    # Size mismatch is a retryable validation error; the slot stays claimed.
    assert "trf_size" in bridge._package_transfers


def test_persisted_transfer_ttl_drops_stale_slots_on_load(settings, tmp_path):
    """A transfer slot older than the TTL is not reloaded on bridge startup."""
    from perun import db as db_module

    transfer_id = "trf_stale"
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO package_transfers
               (transfer_id, proposal_id, sender, direction, sha256, total_size,
                total_chunks, received, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (transfer_id, "prop_x", "frambo", "upload", "abc", 100, 1, "{}", 1.0),
        )

    bridge = PerunChatBridge(MagicMock(settings=settings))
    assert transfer_id not in bridge._package_transfers
    with db_module.db() as conn:
        row = db_module.one(conn, "SELECT * FROM package_transfers WHERE transfer_id=?", (transfer_id,))
    # The sweep on startup should have deleted the stale row.
    assert row is None


