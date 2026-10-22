"""Tests for the PerunChat autonomous bridge integration."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
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
    """Two final-empty runs: no send, but a loud log line (not a silent drop)."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.side_effect = [
        _run_result("", status="cancelled", uid="run_1"),
        _run_result("", status="cancelled", uid="run_2"),
    ]
    ws = MagicMock()
    with patch("perun.perunchat_bridge.logger") as mock_logger:
        bridge._reply(_chat_frame(), ws)
    ws.send.assert_not_called()
    assert mock_logger.error.called
    assert bridge._load_outbox() == []


def test_no_reply_decision_is_not_sent(bridge_env):
    """An explicit <NO_REPLY> decision is silence, not a delivery failure."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("<NO_REPLY>", uid="run_1")
    ws = MagicMock()
    bridge._reply(_chat_frame(), ws)
    ws.send.assert_not_called()
    assert bridge._load_outbox() == []


def test_send_failure_keeps_frame_and_flush_resends_same_id_and_seq(bridge_env):
    """A dead socket keeps the frame durable; the resend is idempotent."""
    bridge, orchestrator = bridge_env
    orchestrator.run_and_wait.return_value = _run_result("hello there", uid="run_1")
    failing_ws = MagicMock()
    failing_ws.send.side_effect = OSError("connection dropped")
    bridge._reply(_chat_frame(), failing_ws)
    failing_ws.send.assert_called_once()
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
    assert "Participants you have seen in this room: frambo_malina_omarchy, other-agent (2 total" in prompt
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
