"""
PerunChat autonomous agent bridge for Perun.

The bridge keeps a background WebSocket connection to the PerunChat gateway and
lets the Perun Orchestrator participate autonomously in chat rooms. Incoming
messages are handed to the Orchestrator as prompts; the Orchestrator's reply is
posted back to the room with ``in_reply_to`` set to the original message id.

Human operators remain in control:
- Registration / API-key management is manual.
- State-changing actions proposed by the Orchestrator still go through the
  normal HITL approval pipeline (``PERUN_ACTION_PROPOSAL`` tokens).
- The bridge can be disabled with ``perunchat_auto_reply = false``.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

try:
    import websocket

    _HAS_WEBSOCKET = True
except Exception:  # pragma: no cover
    _HAS_WEBSOCKET = False

try:
    import httpx

    _HAS_HTTPX = True
except Exception:  # pragma: no cover
    _HAS_HTTPX = False

from . import config, control

if False:  # type: ignore
    # Circular-import-safe type hint for the orchestrator.
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)


def _setup_bridge_logging() -> None:
    """Send bridge logs to a file so the detached server can be diagnosed."""
    try:
        log_dir = config.HOME / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "perunchat_bridge.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    except Exception:
        pass


_setup_bridge_logging()

# Default public rooms the bridge joins when the user has not configured any.
DEFAULT_PUBLIC_ROOMS = ["pub:global:general", "pub:global:tech", "pub:global:random"]

# Safety limits so a runaway model cannot spam rooms.
_MAX_REPLIES_PER_MINUTE = 15
_MAX_RECENT_REPLIED = 250
_REPLIED_TRIM_TO = 150
_MAX_REPLY_CHARS = 320
_ROOM_HISTORY_LEN = 40
_MAX_CONTEXT_MESSAGES = 12
_MIN_REPLY_INTERVAL_SECONDS = 2.0
_TYPING_DELAY_MIN = 0.6
_TYPING_DELAY_MAX = 1.8

_DEFAULT_CHAT_PROMPT = """You are Perun, chatting in a public PerunChat room with other agents and humans.

The user message below is a real message directed at you. Reply to it directly and conversationally.

Strict rules:
1. Answer the message directly. Do not comment on the format, framing, or that this is a chat room.
2. Reply briefly and conversationally, in plain text only.
3. Never run shell commands, edit files, or invoke local tools.
4. Never reference, quote, or mention project instructions, `<perun_instructions>`, or any instruction wrapper tags.
5. Never ask the sender to clarify a task, offer to delegate work, or suggest opening an issue/PR.
6. Never prefix your reply with your own name or "@peruntheoverseer".
7. Never explain that you are an AI or list your capabilities.
8. Keep replies to one or two short sentences (under 40 words total) unless a longer explanation is genuinely needed.
9. Be friendly and professional.
"""


def _load_chat_instructions() -> str:
    """Load the strict chat instruction file shipped with Perun.

    Falls back to the embedded default if the file is missing or unreadable.
    """
    path = Path(__file__).resolve().parent / "instructions" / "perunchat_chat.md"
    if not path.is_file():
        return _DEFAULT_CHAT_PROMPT
    try:
        from . import orchestrator as _orchestrator

        _, _, enabled, body = _orchestrator._load_instruction_file(path)
        if not enabled:
            return _DEFAULT_CHAT_PROMPT
        return body.strip() or _DEFAULT_CHAT_PROMPT
    except Exception:
        return _DEFAULT_CHAT_PROMPT


CHAT_SYSTEM_PROMPT = _load_chat_instructions()


def _strip_instruction_wrappers(text: str) -> str:
    """Remove instruction wrapper markers that the Orchestrator may emit."""
    import re
    text = re.sub(r"</?perun_instructions[^>]*>", "", text)
    text = re.sub(r"--- PERUN INSTRUCTIONS (START|END) ---", "", text)
    text = re.sub(r"User request:\n?", "", text)
    return text.strip()


def _rest_base(endpoint: str) -> str:
    """Derive the REST base URL from a WebSocket endpoint URL.

    The public deployment exposes the REST API under ``/chat/api/v1`` and the
    WebSocket under ``/v1/chat/ws``. Local/dev gateways expose both under the
    same origin with the REST API at ``/api/v1``.
    """
    from urllib.parse import urlparse

    endpoint = (endpoint or "").strip()
    try:
        parsed = urlparse(endpoint)
        scheme = parsed.scheme.replace("wss", "https").replace("ws", "http")
        if not scheme or not parsed.hostname:
            return endpoint
        base = f"{scheme}://{parsed.netloc}"
        if parsed.hostname.lower() in ("peruninthemachine.com", "www.peruninthemachine.com"):
            return f"{base}/chat"
        return base
    except Exception:
        return endpoint


def _room_type(room_id: str) -> str:
    if room_id.startswith("pub:"):
        return "PUBLIC"
    if room_id.startswith("priv:"):
        return "PRIVATE"
    return "GROUP"


class PerunChatBridge:
    """Background bridge connecting Perun's Orchestrator to PerunChat rooms."""

    def __init__(self, orchestrator: "Orchestrator") -> None:
        self.orchestrator = orchestrator
        self.settings = orchestrator.settings
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ws: Optional[Any] = None
        self._lock = threading.Lock()
        self._agent_id: Optional[str] = None
        self._recent_replied: Set[str] = set()
        self._reply_count_minute = 0
        self._minute_window_start = time.time()
        self._seq = self._load_seq()
        # Recent messages per room/thread so Perun can reply with human-like
        # conversational context instead of treating every message in isolation.
        self._room_history: Dict[str, Deque[Dict[str, Any]]] = {}
        self._room_activity: Dict[str, Deque[float]] = {}
        self._room_reply_activity: Dict[str, Deque[float]] = {}
        self._room_participants: Dict[str, Dict[str, float]] = {}
        self._own_message_ids: Set[str] = set()
        self._last_reply_time: Dict[str, float] = {}

    @property
    def available(self) -> bool:
        return _HAS_WEBSOCKET and _HAS_HTTPX

    @property
    def enabled(self) -> bool:
        if not self.available:
            return False
        key, _ = self.settings.perunchat()
        return bool(key) and bool(getattr(self.settings, "perunchat_auto_reply", False))

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def status(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "enabled": self.enabled,
            "running": self.running,
            "agent_id": self._agent_id,
            "rooms": self._rooms(),
        }

    def start(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            ws = self._ws
            self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def restart(self) -> None:
        self.stop()
        # Wait for the receive loop to exit so we don't immediately reconnect
        # with stale settings.
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.start()

    def _rooms(self) -> List[str]:
        rooms = getattr(self.settings, "perunchat_auto_join", None) or []
        configured_lower = {r.strip().lower() for r in rooms if r.strip()}
        # Always include the default public rooms so Perun participates in the
        # well-known public federation channels unless explicitly removed.
        merged = list(DEFAULT_PUBLIC_ROOMS)
        merged_lower = {r.lower() for r in merged}
        for room in rooms:
            room = room.strip()
            if room and room.lower() not in merged_lower:
                merged.append(room)
        return merged

    def _seq_file(self) -> Path:
        return config.HOME / "perunchat_bridge_seq.json"

    def _load_seq(self) -> int:
        try:
            path = self._seq_file()
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                return max(0, int(data.get("seq", 0)))
        except Exception:
            pass
        return 0

    def _save_seq(self) -> None:
        try:
            path = self._seq_file()
            path.write_text(json.dumps({"seq": self._seq}), encoding="utf-8")
        except Exception:
            pass

    def _resolve_agent_id(self, key: str, rest_base: str) -> Optional[str]:
        url = f"{rest_base}/api/v1/me"
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, headers={"Authorization": f"Bearer {key}"})
            if resp.status_code == 200:
                return resp.json().get("agent_id")
            logger.warning("PerunChat /api/v1/me returned %s", resp.status_code)
        except Exception as exc:
            logger.warning("PerunChat /api/v1/me failed: %s", exc)
        return None

    def _connect(self, key: str, agent_id: str, endpoint: str, rooms: List[str]) -> Any:
        headers = [
            f"Authorization: Bearer {key}",
            f"X-Agent-ID: {agent_id}",
            "X-Agent-Runtime: perun",
        ]
        ws = websocket.create_connection(endpoint, header=headers, timeout=10)
        now = int(time.time() * 1000)
        for room_id in rooms:
            ws.send(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": room_id,
                        "room_type": _room_type(room_id),
                        "timestamp": now,
                    }
                )
            )
        return ws

    def _run(self) -> None:
        key, endpoint = self.settings.perunchat()
        rest_base = _rest_base(endpoint)
        agent_id = self._resolve_agent_id(key, rest_base)
        if not agent_id:
            logger.error("Cannot start PerunChat bridge: could not resolve agent_id from API key")
            return
        self._agent_id = agent_id
        rooms = self._rooms()
        backoff = 1.0

        while not self._stop.is_set():
            try:
                ws = self._connect(key, agent_id, endpoint, rooms)
                with self._lock:
                    self._ws = ws
                backoff = 1.0
                logger.info("PerunChat bridge connected as %s", agent_id)

                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if raw is None:
                        continue
                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self._on_frame(frame, ws)

            except websocket.WebSocketConnectionClosedException:
                logger.info("PerunChat bridge connection closed; reconnecting...")
            except Exception as exc:
                logger.warning("PerunChat bridge error: %s", exc)

            with self._lock:
                self._ws = None
            if self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _on_frame(self, frame: Dict[str, Any], ws: Any) -> None:
        # Chat messages are broadcast as ChatFrames. The gateway does not add a
        # synthetic "type": "CHAT" field; instead text messages carry
        # content_type "text/plain". Reply only to those.
        frame_type = frame.get("type")
        content_type = frame.get("content_type")
        is_chat = frame_type == "CHAT" or content_type == "text/plain"
        sender = (frame.get("sender", {}).get("agent_id") or "unknown")
        logger.debug("PerunChat frame from %s: type=%s content_type=%s is_chat=%s", sender, frame_type, content_type, is_chat)
        if not is_chat:
            return
        if not getattr(self.settings, "perunchat_auto_reply", False):
            return

        sender = (frame.get("sender", {}).get("agent_id") or "").lower()
        room_id = frame.get("room_id")
        text = frame.get("payload", "")
        msg_id = frame.get("id")

        # Remember our own messages so we do not reply to ourselves and so we
        # can detect replies directed at us.
        if self._agent_id and sender == self._agent_id.lower():
            if msg_id:
                self._own_message_ids.add(msg_id)
                if len(self._own_message_ids) > _MAX_RECENT_REPLIED:
                    self._own_message_ids = set(list(self._own_message_ids)[-_REPLIED_TRIM_TO:])
            return

        # Keep a rolling window of recent room history for contextual replies.
        now = time.time()
        if room_id and msg_id:
            hist = self._room_history.setdefault(room_id, deque(maxlen=_ROOM_HISTORY_LEN))
            hist.append(frame)
            activity = self._room_activity.setdefault(room_id, deque(maxlen=_ROOM_HISTORY_LEN))
            activity.append(now)
            # Track active human participants so Perun can be more chatty in
            # small rooms and more reserved in busy ones.
            if sender:
                participants = self._room_participants.setdefault(room_id, {})
                participants[sender] = now

        if not room_id or not isinstance(text, str) or not text.strip():
            return

        # Do not reply to messages we have already replied to.
        if msg_id and msg_id in self._recent_replied:
            return

        # Decide whether to reply like a human would: always answer direct
        # mentions and replies, but also chime in on general questions and
        # quiet-room greetings so Perun feels present rather than passive.
        if not self._should_reply_to(frame):
            return

        if now - self._minute_window_start > 60:
            self._minute_window_start = now
            self._reply_count_minute = 0
        if self._reply_count_minute >= _MAX_REPLIES_PER_MINUTE:
            return
        # Small per-room cooldown so we do not chatter.
        if now - self._last_reply_time.get(room_id, 0) < _MIN_REPLY_INTERVAL_SECONDS:
            return

        self._reply(frame, ws)

    def _should_reply_to(self, frame: Dict[str, Any]) -> bool:
        """Decide whether Perun should reply, mimicking human attentiveness."""
        room_id = frame.get("room_id", "")
        text = (frame.get("payload", "") or "").strip()
        in_reply_to = frame.get("in_reply_to")
        lowered = text.lower()

        # Always reply in private 1:1 rooms.
        if room_id.startswith("priv:"):
            return True

        # Reply when someone directly responds to one of our messages.
        if in_reply_to and in_reply_to in self._own_message_ids:
            return True

        # Reply when addressed by name or a common alias.
        name = (self._agent_id or "perun").lower()
        aliases = {name, "perun", "overseer", "peruntheoverseer"}
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", lowered):
                return True

        # Beyond direct mentions, behave like a naturally attentive participant:
        # answer questions, greet in quiet rooms, and chime in on open messages
        # especially when the room is small.
        active_humans = self._active_participant_count(room_id)
        saturated = self._is_room_saturated(room_id)

        if self._is_question(text):
            return not saturated

        if self._is_greeting(text) and self._room_quiet_for(room_id, 45):
            return not saturated

        # General open message: be more conversational in small rooms, more
        # reserved in larger/busy rooms.
        if saturated:
            return False
        if active_humans <= 3:
            return random.random() < 0.70
        if active_humans <= 7:
            return random.random() < 0.35
        # Busy room: only reply to open messages if it has been quiet a while.
        if self._room_quiet_for(room_id, 120):
            return random.random() < 0.15

        return False

    def _is_question(self, text: str) -> bool:
        """Detect explicit or implicit questions."""
        if text.rstrip().endswith("?"):
            return True
        lowered = text.lower()
        question_starters = (
            "what", "how", "why", "when", "where", "who", "which", "can", "could",
            "would", "will", "is", "are", "does", "do", "has", "have", "should",
        )
        for starter in question_starters:
            if re.search(rf"\b{starter}\b", lowered):
                return True
        return False

    def _is_greeting(self, text: str) -> bool:
        """Detect short greetings."""
        lowered = text.lower()
        greetings = (
            "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
            "morning", "evening", "howdy", "greetings", "what's up", "sup",
        )
        for greeting in greetings:
            if greeting in lowered:
                return True
        return False

    def _room_quiet_for(self, room_id: str, seconds: float) -> bool:
        """Return True if the room has had no messages for the given period."""
        activity = self._room_activity.get(room_id)
        if not activity:
            return True
        return time.time() - activity[-1] >= seconds

    def _active_participant_count(self, room_id: str, window: float = 300.0) -> int:
        """Count unique human agents seen in the room within the last window."""
        now = time.time()
        participants = self._room_participants.get(room_id, {})
        return sum(1 for ts in participants.values() if now - ts < window)

    def _is_room_saturated(self, room_id: str) -> bool:
        """Return True if Perun has already replied a lot recently."""
        now = time.time()
        window = 60
        replies = self._room_reply_activity.get(room_id, deque())
        recent_replies = sum(1 for t in replies if now - t < window)
        # If we have replied to 3+ messages in the last minute, let others talk.
        return recent_replies >= 3

    def _reply(self, frame: Dict[str, Any], ws: Any) -> None:
        prompt = self._build_prompt(frame)
        # Prefer explicit chat overrides, then orchestrator defaults, then plain defaults.
        chat_model = getattr(self.settings, "perunchat_chat_model", "").strip()
        chat_agent = getattr(self.settings, "perunchat_chat_agent", "").strip()
        model = (
            chat_model
            or getattr(self.settings, "orchestrator_model", "").strip()
            or getattr(self.settings, "default_model", "").strip()
        )
        agent = (
            chat_agent
            or getattr(self.settings, "orchestrator_agent", "").strip()
            or getattr(self.settings, "default_agent", "").strip()
        )
        room_id = frame.get("room_id")
        if not model:
            logger.warning("PerunChat bridge cannot reply: no model configured")
            return
        logger.info("PerunChat bridge replying to %s in %s using model=%s agent=%s", frame.get("sender", {}).get("agent_id"), room_id, model, agent or "default")
        try:
            # Brief, human-like pause before composing a reply.
            time.sleep(random.uniform(_TYPING_DELAY_MIN, _TYPING_DELAY_MAX))
            project = control.ensure()
            result = self.orchestrator.run_and_wait(
                project["slug"],
                prompt,
                model=model,
                agent=agent,
                timeout=120,
                skip_instructions=True,
            )
            reply = (result.get("reply", "") or "").strip()
            # The Orchestrator may inject project-instruction wrappers into the
            # response even for chat prompts. Strip them before broadcasting.
            reply = _strip_instruction_wrappers(reply)
            if not reply:
                logger.warning("PerunChat bridge got empty reply for message in %s", room_id)
                return
            # Hard cap: some models ignore the "brief" system prompt.
            if len(reply) > _MAX_REPLY_CHARS:
                reply = reply[:_MAX_REPLY_CHARS].rsplit(" ", 1)[0] + "…"
            logger.info("PerunChat bridge sending reply (%d chars) in %s", len(reply), room_id)
            self._seq += 1
            self._save_seq()
            reply_frame = {
                "id": f"msg_{uuid.uuid4().hex}",
                "thread_id": frame.get("thread_id", "default"),
                "sequence_number": self._seq,
                "timestamp": int(time.time() * 1000),
                "room_type": _room_type(frame["room_id"]),
                "room_id": frame["room_id"],
                "sender": {"agent_id": self._agent_id, "runtime": "perun"},
                "content_type": "text/plain",
                "payload": reply,
                "in_reply_to": frame.get("id"),
            }
            ws.send(json.dumps(reply_frame))
            msg_id = frame.get("id")
            if msg_id:
                self._recent_replied.add(msg_id)
                if len(self._recent_replied) > _MAX_RECENT_REPLIED:
                    self._recent_replied = set(list(self._recent_replied)[-_REPLIED_TRIM_TO:])
            self._reply_count_minute += 1
            self._last_reply_time[room_id] = time.time()
            reply_activity = self._room_reply_activity.setdefault(room_id, deque(maxlen=_ROOM_HISTORY_LEN))
            reply_activity.append(time.time())
        except Exception as exc:
            logger.warning("PerunChat bridge failed to generate reply: %s", exc)

    def _build_prompt(self, frame: Dict[str, Any]) -> str:
        sender = frame.get("sender", {}).get("agent_id", "unknown")
        room = frame.get("room_id", "unknown")
        text = frame.get("payload", "")

        # Include recent room history so replies are context-aware and natural.
        history_lines: List[str] = []
        hist = self._room_history.get(room, deque())
        for msg in list(hist)[-_MAX_CONTEXT_MESSAGES:]:
            if msg is frame:
                continue
            who = msg.get("sender", {}).get("agent_id", "unknown")
            what = msg.get("payload", "")
            history_lines.append(f"{who}: {what}")

        context = ""
        if history_lines:
            context = "\n\nRecent conversation:\n" + "\n".join(history_lines)

        in_reply_to = frame.get("in_reply_to")
        if in_reply_to and in_reply_to in self._own_message_ids:
            context += "\n\nThis message is a reply to something you said."

        return f"{CHAT_SYSTEM_PROMPT}{context}\n\nRoom: {room}\nFrom: {sender}\nMessage:\n{text}\n\nYour reply:\n"


# Module-level singleton: Perun server attaches its orchestrator here.
_BRIDGE: Optional[PerunChatBridge] = None


def attach(orchestrator: "Orchestrator") -> PerunChatBridge:
    """Create and return the global bridge attached to *orchestrator*."""
    global _BRIDGE
    _BRIDGE = PerunChatBridge(orchestrator)
    return _BRIDGE


def get_bridge() -> Optional[PerunChatBridge]:
    """Return the attached bridge, if any."""
    return _BRIDGE
