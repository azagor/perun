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


def test_default_room_when_empty(settings):
    settings.perunchat_auto_join = []
    bridge = PerunChatBridge(MagicMock(settings=settings))
    assert bridge._rooms() == ["pub:global:general"]


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
        "sender": {"agent_id": "perun-local"},
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
    assert status["rooms"] == ["pub:global:test"]
