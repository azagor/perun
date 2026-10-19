"""Tests for the PerunChat autonomous bridge integration."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("PERUN_HOME", os.path.join(os.getcwd(), "tests", "tmp_perun_home"))

from perun import config
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
        mock_reply.assert_not_called()


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
        mock_reply.assert_called_once_with(frame, None)


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
        mock_reply.assert_called_once_with(frame, None)


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
