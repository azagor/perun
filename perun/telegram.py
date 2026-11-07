"""Shared Telegram ⇄ Perun bridge with per-project routing.

A single long-polling process serves *every* project. Incoming messages are
routed to the chat's currently-selected project and answered by an OpenCode
session started through the Perun orchestrator.

Commands
--------
``/start`` ``/help``     Help.
``/pair <code>``         Authorise this chat with the dashboard's one-time code.
``/orchestrator``        Talk to Perun itself (the machine-wide orchestrator).
``/projects``            List registered projects (numbered).
``/project <name|#>``    Select the project to talk to.
``/new`` ``/reset``      Start a fresh OpenCode session for this chat.
``/status``              Bridge + current project status.
``/stop``                Cancel the in-flight agent run.
``/remember <fact>``     Store a fact (project scope).
``/recall <query>``      Search the unified memory store.
``/index``               Re-index the current project's docs.

With no project selected, plain messages go to the **orchestrator**
(:mod:`perun.control`) — the agent that manages every project — so the bridge is
usable on a brand-new machine with nothing registered.

Environment / settings
----------------------
Token + allow-list come from ``Settings.telegram()`` (config.json first, then
``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``). State (chat→project→session) is
persisted in Perun's database, not a side file.
"""

from __future__ import annotations

import hmac
import logging
import logging.handlers
import os
import queue
import re
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from . import config, control, memory, registry, routing
from .orchestrator import Orchestrator
from .telegram_format import (
    MAX_CHARS as TELEGRAM_MAX,
    badge,
    chunk_markdown,
    format_html,
    heading,
    plain_text,
    time_marker,
)

LOG = logging.getLogger("perun-telegram")
TYPING_INTERVAL = 4.0


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def chunk(text: str, size: int) -> List[str]:
    """Backwards-compatible wrapper around the fence-aware chunker."""
    return chunk_markdown(text, size)


def _downgrade_markup(markup: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Strip fields an older Bot API server rejects (``style``, ``copy_text``).

    Keeping the buttons matters more than keeping their colour: a local Bot API
    server from before 9.4 answers 400 for the whole message, so degrading the
    markup keeps the decision tappable.
    """
    if not isinstance(markup, dict):
        return None
    rows: List[List[Dict[str, Any]]] = []
    for row in markup.get("inline_keyboard") or []:
        buttons = [
            {k: v for k, v in button.items()
             if k not in ("style", "copy_text", "icon_custom_emoji_id")}
            for button in row
            if isinstance(button, dict)
        ]
        if buttons:
            rows.append(buttons)
    return {"inline_keyboard": rows} if rows else None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        k32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _default_mutex_name() -> str:
    """Machine-global mutex name, namespaced by ``PERUN_HOME``.

    The name *must not* collide with unrelated bridges. The pre-existing
    ``trading_bot/scripts/telegram_bridge.py`` uses the bare name
    ``PerunTelegramBridge`` on the same machine, so a bare name here made
    ``perun status`` report ``bridge_running: true`` whenever the trading_bot
    bridge was up. Hashing the home path also keeps two different
    ``PERUN_HOME`` instances from fighting over one mutex.
    """

    try:
        import hashlib
        digest = hashlib.sha1(str(config.HOME).encode("utf-8")).hexdigest()[:12]
    except Exception:  # pragma: no cover - defensive
        digest = "default"
    return f"PerunManagedBridge-{digest}"


class PidLock:
    """Single-instance lock (named Windows mutex; POSIX pid-file fallback)."""

    def __init__(self, path, name: Optional[str] = None):
        self.path = path
        self.name = name or _default_mutex_name()
        self.acquired = False
        self._mutex: Optional[int] = None

    @classmethod
    def is_held(cls, path, name: Optional[str] = None) -> bool:
        """Return whether some process currently holds this lock.

        Side-effect free: unlike :meth:`acquire` it never writes or deletes the
        pid file, so it is safe to call from read-only commands such as
        ``perun status``.
        """

        name = name or _default_mutex_name()
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            handle = k32.CreateMutexW(None, False, name)
            if not handle:
                return False
            held = k32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(handle)
            return bool(held)
        try:
            pid = int(path.read_text().strip())
        except Exception:
            return False
        return bool(pid) and _pid_alive(pid)

    def acquire(self) -> bool:
        if os.name == "nt":
            import ctypes
            ERROR_ALREADY_EXISTS = 183
            k32 = ctypes.windll.kernel32
            handle = k32.CreateMutexW(None, False, self.name)
            if not handle:
                return False
            if k32.GetLastError() == ERROR_ALREADY_EXISTS:
                k32.CloseHandle(handle)
                return False
            self._mutex = handle
            self.acquired = True
            try:
                self.path.write_text(str(os.getpid()), "utf-8")
            except OSError:
                pass
            return True
        # POSIX: create the pid file *atomically*. A check-then-write race let
        # two bridges both believe they had acquired the lock; O_EXCL means
        # exactly one creator wins.
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip())
                except Exception:
                    pid = 0
                if pid and _pid_alive(pid):
                    return False
                # Stale lock left by a dead process: drop it and retry once.
                try:
                    os.unlink(self.path)
                except OSError:
                    return False
                continue
            except OSError:
                return False
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            self.acquired = True
            return True
        return False

    def release(self) -> None:
        if os.name == "nt" and self._mutex:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._mutex)
            self._mutex = None
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.acquired = False


# --------------------------------------------------------------------------- #
# Bridge
# --------------------------------------------------------------------------- #
class PerunTelegramBridge:
    def __init__(self, orch: Orchestrator, *, token: str, allowed: List[str],
                 max_chars: int = 3900):
        self.orch = orch
        self.token = token
        self.allowed = allowed
        self.max_chars = max_chars
        self.client = httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0))
        self.stop_event = threading.Event()
        self.work: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.cancel_event = threading.Event()
        # chat_id -> run_uid of that chat's in-flight run, so ``/stop`` cancels
        # only the caller's run (the old single ``current_run`` was shared by the
        # whole bridge, so one chat could kill another's run).
        self._runs: Dict[str, str] = {}
        self._runs_lock = threading.Lock()
        self.started_at = time.time()
        self._allowed_runtime: List[str] = []
        self._pair_lock = threading.Lock()
        self.offset: Optional[int] = None
        self._state_lock = threading.Lock()
        # Bounded deduplication set for Telegram message IDs. Telegram can
        # redeliver updates when long polling overlaps with a reconnect.
        self._seen_ids: set[tuple[str, int]] = set()
        self._seen_ids_lock = threading.Lock()
        # Operator inbox: answers typed as plain text wait here until the
        # operator taps "Send" (so a normal chat message is never silently
        # treated as an answer).
        self._pending_answers: Dict[str, str] = {}
        self._attention_logged: set[int] = set()
        # Streaming drafts (private chats): draft_id -> run_uid, so the Stop
        # button on the draft can cancel the exact run.
        self._drafts: Dict[int, str] = {}
        self._drafts_lock = threading.Lock()
        # Informational notices wait for a short window so a burst becomes one
        # card: ``{attention_id: first_seen}``. Items already listed on a digest
        # card are remembered so the next tick does not resend them as cards.
        self._digest_pending: Dict[int, float] = {}
        self._digested: set[int] = set()
        # Hourly, not once per process: a bridge that stays up for weeks must
        # still expire open items and prune closed ones.
        self._last_attention_prune: float = 0.0

    def update_token(self, token: str) -> None:
        """Update the bot token used by this bridge instance."""
        self.token = token

    def active_runs(self) -> List[str]:
        """Run uids currently tracked for any chat (used on shutdown)."""
        with self._runs_lock:
            return list(self._runs.values())

    def run_for(self, chat_id: str) -> Optional[str]:
        with self._runs_lock:
            return self._runs.get(chat_id)

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    # ---- Telegram API ---- #
    def api(self, method: str, *, retries: int = 4, **params: Any) -> Dict[str, Any]:
        url = f"{self.api_base}/{method}"
        delay = 2.0
        # Total time a single call may spend sleeping on rate limits, so a 429
        # storm cannot stall the notifier for minutes.
        budget = 30.0
        for attempt in range(1, retries + 1):
            try:
                data = self.client.post(url, json=params).json()
                if data.get("ok"):
                    return data
                if data.get("error_code") == 409:
                    # Handled (and de-spammed) by the polling loop.
                    return data
                if data.get("error_code") == 429:
                    # Respect Telegram's own backoff instead of doubling blindly:
                    # a burst of attention cards is exactly when this happens.
                    retry_after = (data.get("parameters") or {}).get("retry_after")
                    try:
                        wait = float(retry_after or delay)
                    except (TypeError, ValueError):
                        wait = delay
                    wait = min(max(wait, 1.0), 30.0, budget)
                    LOG.warning("telegram %s rate limited; retrying in %.0fs",
                                method, wait)
                    if attempt < retries and wait >= 1.0:
                        time.sleep(wait)
                        budget -= wait
                    continue
                LOG.warning("telegram %s error (%d/%d): %s", method, attempt, retries,
                            data.get("description"))
            except Exception as exc:
                LOG.warning("telegram %s failed (%d/%d): %s", method, attempt, retries, exc)
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        return {}

    @classmethod
    def is_running(cls) -> bool:
        """Best-effort check for an active bridge poller on this machine."""
        try:
            return PidLock.is_held(config.HOME / "telegram.pid")
        except Exception:  # pragma: no cover
            return False

    def send(self, chat_id: str, text: str, *, reply_to: Optional[int] = None,
             reply_markup: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """Send *text*, chunked; returns the first message id (or ``None``).

        The id is what lets an operator reply-to a card and have the answer
        matched to exactly one attention item, so the first chunk's id is the
        one that matters (the keyboard rides on that chunk too).
        """
        first_id: Optional[int] = None
        for piece in chunk(text or "(empty reply)", self.max_chars):
            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": format_html(piece),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_to is not None:
                payload["reply_to_message_id"] = reply_to
                payload["allow_sending_without_reply"] = True
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            res = self.api("sendMessage", **payload)
            if not res.get("ok"):
                # Telegram can reject the HTML (a bad entity) or the keyboard (a
                # stale callback payload). Retry as plain text but KEEP the
                # keyboard: dropping every button because bold failed is worse
                # than sending unformatted text.
                payload.pop("parse_mode", None)
                payload["text"] = plain_text(piece)
                res = self.api("sendMessage", **payload)
            if not res.get("ok") and "reply_markup" in payload:
                # A coloured/copy button may be the problem on an older Bot API
                # server. Degrade the markup before giving up the keyboard.
                degraded = _downgrade_markup(payload["reply_markup"])
                if degraded:
                    payload["reply_markup"] = degraded
                    res = self.api("sendMessage", **payload)
            if not res.get("ok") and "reply_markup" in payload:
                payload.pop("reply_markup", None)
                res = self.api("sendMessage", **payload)
            if first_id is None:
                try:
                    first_id = int((res.get("result") or {}).get("message_id"))
                except (TypeError, ValueError, AttributeError):
                    first_id = None
            reply_to = None
            reply_markup = None
        return first_id

    def _typing_loop(self, chat_id: str, done: threading.Event) -> None:
        while not done.wait(TYPING_INTERVAL):
            self.api("sendChatAction", chat_id=chat_id, action="typing")

    # ---- live run drafts (private chats) ---------------------------------- #
    DRAFT_MIN_INTERVAL = 1.6

    @staticmethod
    def _is_private_chat(chat_id: str) -> bool:
        """Telegram user ids are positive; groups/supergroups are negative."""
        raw = str(chat_id or "")
        return raw.lstrip("-").isdigit() and not raw.startswith("-")

    @staticmethod
    def _draft_id(run_uid: str) -> int:
        import hashlib

        value = int(hashlib.sha1(str(run_uid).encode("utf-8")).hexdigest()[:8], 16)
        return value or 1

    def _start_draft(self, chat_id: str, uid: str) -> threading.Event:
        stop = threading.Event()
        draft_id = self._draft_id(uid)
        with self._drafts_lock:
            self._drafts[draft_id] = uid
        threading.Thread(
            target=self._draft_loop,
            args=(chat_id, uid, draft_id, stop),
            daemon=True,
            name="perun-tg-draft",
        ).start()
        return stop

    def _draft_loop(self, chat_id: str, uid: str, draft_id: int,
                    stop: threading.Event) -> None:
        """Stream a partial reply while the run generates (private chats only).

        The draft is a temporary 30-second preview: updates keep it alive, and
        the final ``sendMessage`` in the caller persists the answer.  Any
        failure (old local Bot API server, revoked message, network) degrades to
        the existing typing indicator instead of failing the run.
        """
        stream = getattr(self.orch, "stream", None)
        if not callable(stream):
            with self._drafts_lock:
                self._drafts.pop(draft_id, None)
            return
        parts: List[str] = []
        tool = ""
        last = 0.0
        try:
            # An empty draft renders Telegram's native "Thinking…" placeholder.
            first = self.api("sendMessageDraft", retries=1, chat_id=chat_id,
                             draft_id=draft_id, text="", can_stop=True,
                             keep_on_stop=False)
            if not first.get("ok"):
                # Old local Bot API server or drafts disabled for this chat:
                # keep the typing indicator and send the final answer normally.
                return
            for event in stream(uid):
                if stop.is_set():
                    break
                kind = event.get("type")
                if kind == "text" and event.get("text"):
                    parts.append(str(event["text"]))
                    if sum(len(p) for p in parts) > 4000:
                        parts = [("".join(parts))[-2000:]]
                elif kind == "tool":
                    tool = str(event.get("tool") or event.get("name") or "").strip()
                elif kind == "error" and event.get("text"):
                    parts.append(str(event["text"]))
                if kind in ("end", "__eof__"):
                    break
                now = time.time()
                if now - last < self.DRAFT_MIN_INTERVAL and kind not in ("tool",):
                    continue
                last = now
                body = "\n\n".join(p for p in parts if p).strip()
                text = f"⏳ **Working…**"
                if tool:
                    text += f"\n🔧 `{tool}`"
                if body:
                    text += "\n\n" + body[-1600:]
                res = self.api(
                    "sendMessageDraft", retries=1, chat_id=chat_id,
                    draft_id=draft_id, text=format_html(text),
                    parse_mode="HTML", can_stop=True, keep_on_stop=False,
                )
                if not res.get("ok"):
                    break
        except Exception:  # pragma: no cover - a draft must never break a run
            LOG.debug("draft stream for %s failed", uid, exc_info=True)
        finally:
            with self._drafts_lock:
                self._drafts.pop(draft_id, None)

    def _handle_generation_stopped(self, payload: Dict[str, Any]) -> None:
        """The operator tapped Stop on a draft: cancel that exact run."""
        try:
            draft_id = int(payload.get("draft_id") or 0)
        except (TypeError, ValueError):
            return
        with self._drafts_lock:
            uid = self._drafts.get(draft_id)
        if not uid:
            return
        try:
            self.orch.cancel(uid)
        except Exception:  # pragma: no cover - the run may already be gone
            LOG.debug("could not cancel %s from a draft stop", uid, exc_info=True)

    # ---- project/session resolution ---- #
    def _chat_state(self, chat_id: str) -> Dict[str, Any]:
        link = registry.get_bridge_link(chat_id) or {}
        return link

    def _current_project(self, chat_id: str) -> Optional[Dict[str, Any]]:
        link = self._chat_state(chat_id)
        pid = link.get("project_id")
        proj = registry.get_project(pid) if pid else None
        if proj:
            return proj
        # No explicit selection (or it points at a deleted project). Preserve the
        # long-standing convenience of auto-selecting a lone user project…
        users = [p for p in registry.list_projects() if not control.is_control(p)]
        if len(users) == 1:
            registry.set_bridge_link(chat_id, users[0]["id"], link.get("session_pk"))
            return users[0]
        # …otherwise default to the orchestrator. It always exists, so "no
        # project" is never a dead end — the user can still ask Perun to create
        # or launch one.
        try:
            return control.ensure()
        except Exception:  # pragma: no cover - defensive
            return None

    # ---- commands ---- #
    def _project_keyboard(self, projects: list[Dict[str, Any]], chat_id: str) -> Dict[str, Any]:
        """Build an inline keyboard that maps each project to a callback payload.

        Telegram limits ``callback_data`` to 64 bytes, so we use the numeric
        project id instead of the slug when the slug would be too long. The
        ``_handle_callback`` method resolves either form back to a project.
        """
        buttons: list[list[Dict[str, Any]]] = []
        current_id = (self._chat_state(chat_id).get("project_id"))
        for p in projects:
            label = ("▸ " if p.get("id") == current_id else "") + p["name"]
            if control.is_control(p):
                label += " 🛰"
            # Telegram callback_data has a hard 64-byte limit.
            slug = p["slug"]
            payload = f"project:{slug}"
            if len(payload.encode("utf-8")) > 64:
                payload = f"project:id:{p['id']}"
            button: Dict[str, Any] = {"text": label, "callback_data": payload}
            if p.get("id") == current_id:
                button["style"] = "primary"
            buttons.append([button])
        return {"inline_keyboard": buttons}

    def _handle_callback(self, update: Dict[str, Any]) -> None:
        """Handle inline-keyboard button taps (operator inbox + project picker)."""
        cq = update.get("callback_query") or {}
        data = (cq.get("data") or "").strip()
        if data.startswith("att:"):
            self._handle_attention_callback(cq, data)
            return
        if data.startswith("muteproj:"):
            self._handle_mute_callback(cq, data)
            return
        if not data or not data.startswith("project:"):
            return
        payload = data.split(":", 1)[1]
        chat_id = str((cq.get("message", {}).get("chat") or {}).get("id"))
        message_id = int(cq.get("message", {}).get("message_id") or 0)
        if not chat_id:
            return
        if not self.authorised(chat_id):
            self.api("answerCallbackQuery", callback_query_id=cq.get("id"), text="Not authorised.")
            return
        # Long slugs are encoded as "id:<project_id>" to fit Telegram's 64-byte limit.
        if payload.startswith("id:"):
            project = registry.get_project(payload.split(":", 1)[1])
        else:
            project = registry.get_project(payload)
        if not project:
            self.api("answerCallbackQuery", callback_query_id=cq.get("id"), text="Project not found.")
            return
        registry.set_bridge_link(chat_id, project["id"], None)
        self.api("answerCallbackQuery", callback_query_id=cq.get("id"),
                  text=f"Now talking to {project['name']}")
        self.send(chat_id, f"✅ Now talking to *{project['name']}* (`{project['slug']}`).",
                  reply_to=message_id if message_id else None)

    def _handle_mute_callback(self, cq: Dict[str, Any], data: str) -> None:
        """Mute one project from a digest card, then drop that button."""
        chat_id = str((cq.get("message", {}).get("chat") or {}).get("id"))
        callback_id = cq.get("id")

        def ack(text: str) -> None:
            self.api("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])

        if not chat_id:
            return
        if not self.authorised(chat_id):
            ack("Not authorised.")
            return
        try:
            project = registry.get_project(int(data.split(":", 1)[1]))
        except (TypeError, ValueError, IndexError):
            project = None
        if not project:
            ack("Project not found.")
            return
        registry.mute_attention_project(chat_id, project["slug"])
        registry.log_activity(
            project["id"], "attention_muted",
            f"{project['slug']} muted for a Telegram chat",
            {"chat_id": chat_id},
        )
        ack(f"Muted {project['slug']} — /unmute {project['slug']} to undo")

        # Keep the Open buttons, remove only the mute row from this card.
        message = cq.get("message") or {}
        message_id = int(message.get("message_id") or 0)
        rows = [
            row for row in ((message.get("reply_markup") or {}).get("inline_keyboard") or [])
            if not any(str(b.get("callback_data") or "").startswith("muteproj:")
                       for b in row)
        ]
        if message_id and rows:
            self.api("editMessageReplyMarkup", chat_id=chat_id,
                     message_id=message_id,
                     reply_markup={"inline_keyboard": rows}, retries=2)

    def _health_report(self) -> str:
        """One-screen machine health, answered from this process (no agent run).

        A chat that cannot say whether the agent service is up is useless when
        something breaks; every probe is short and degrades to ``✗``/``?``
        instead of raising.
        """
        from . import opencode_api

        lines = ["🩺 **Health**"]
        try:
            info = opencode_api.default_api().info() or {}
        except Exception as exc:
            lines.append(f"❌ opencode service — {type(exc).__name__}")
        else:
            version = str(info.get("version") or info.get("app") or "").strip()
            lines.append("✅ opencode service" + (f" — {version}" if version else ""))
        host = ""
        try:
            host = str(config.Settings.load().ollama_host or "").rstrip("/")
        except Exception:  # pragma: no cover - settings unreadable
            host = ""
        if host:
            try:
                httpx.get(f"{host}/api/tags", timeout=2.0)
            except Exception:
                lines.append(f"❌ ollama — {host} not reachable")
            else:
                lines.append("✅ ollama")
        up = int(time.time() - self.started_at)
        lines.append(
            f"✅ bridge — {up // 3600}h {(up % 3600) // 60}m · "
            f"{self.work.qsize()} queued · {len(self.allowed)} paired chat(s)"
        )
        try:
            lines.append(f"⏳ {len(registry.list_active_runs())} run(s) active")
        except Exception:  # pragma: no cover - a DB hiccup must not hide the rest
            pass
        try:
            open_items = len(registry.open_attention(limit=100))
            if open_items:
                lines.append(f"❓ {open_items} item(s) waiting for you — /pending")
        except Exception:  # pragma: no cover - defensive
            pass
        return "\n".join(lines)

    def _handle_text_command(self, chat_id: str, text: str, message_id: int,
                             *, project_override: str | None = None) -> bool:
        parts = text.strip().split()
        if not parts:
            return True
        root = parts[0].lower().split("@")[0]
        if root in ("/start", "/help"):
            self.send(chat_id,
                      "🤖 **Perun bridge**\n\n"
                      "Talk to Perun itself or to any of your OpenCode projects.\n\n"
                      "**🛰 Switch**\n"
                      "`/orchestrator` — talk to Perun (manages all projects)\n"
                      "`/projects` — list projects with buttons\n"
                      "`/project <name|#>` — switch project\n\n"
                      "**📊 Status**\n"
                      "`/status` — current project + session\n"
                      "`/now` — everything running right now\n"
                      "`/health` — opencode, ollama and bridge state\n"
                      "`/runs` — the last few runs and how they ended\n"
                      "`/usage` — tokens and cost in the last 24h\n"
                      "`/routing` — how much of the traffic the router understands\n\n"
                      "**🧠 Memory & docs**\n"
                      "`/remember <fact>` — store to memory\n"
                      "`/recall <query>` — search memory\n"
                      "`/index` — re-index project docs\n\n"
                      "**▶ Work**\n"
                      "`/new` — fresh session\n"
                      "`/stop` — cancel *your* running reply\n"
                      "`/resume` — continue one of the recent runs\n"
                      "`/diff` — what changed in the working tree\n\n"
                      "**🔑 Access**\n"
                      "`/pair <code>` — authorise this chat (code from the dashboard)\n\n"
                      "With no project selected, messages go to the orchestrator.\n"
                      "You can also just ask: _what's waiting?_ · _list projects_ · _help_",
                      reply_to=message_id)
            return True
        if root in ("/orchestrator", "/perun", "/overseer"):
            proj = control.ensure()
            registry.set_bridge_link(chat_id, proj["id"], None)
            self.send(chat_id, "🛰 Now talking to *Perun* — the orchestrator. Ask about "
                               "any project, or tell me to create, launch, or remove one.",
                      reply_to=message_id)
            return True
        if root == "/projects":
            try:
                control.ensure()
            except Exception:  # pragma: no cover - defensive
                pass
            # Only the static allow-list (owner) sees the hidden orchestrator
            # project. Runtime-paired chats must not see it even though they are
            # authorised to talk to the bot.
            include_hidden = chat_id in self.allowed
            projects = registry.list_projects(include_hidden=include_hidden)
            if not projects:
                self.send(chat_id, "No projects registered yet.", reply_to=message_id)
                return True
            lines = ["*Projects*"]
            for i, p in enumerate(projects, 1):
                mark = "▸" if self._is_current(chat_id, p) else " "
                tag = "  🛰 orchestrator" if control.is_control(p) else ""
                lines.append(f"{mark} {i}. `{p['slug']}` — {p['name']} ({p['kind']}){tag}")
            keyboard = self._project_keyboard(projects, chat_id)
            self.send(chat_id, "\n".join(lines), reply_to=message_id, reply_markup=keyboard)
            return True
        if root == "/project":
            include_hidden = chat_id in self.allowed
            projects = registry.list_projects(include_hidden=include_hidden)
            if len(parts) < 2:
                self.send(chat_id, "Usage: `/project <name|#>`", reply_to=message_id)
                return True
            sel = parts[1].lstrip("#")
            target = None
            if sel.isdigit() and 1 <= int(sel) <= len(projects):
                target = projects[int(sel) - 1]
            else:
                for p in projects:
                    if p["slug"] == sel or p["name"].lower() == " ".join(parts[1:]).lower():
                        target = p
                        break
                if not target:
                    for p in projects:
                        if sel.lower() in p["slug"]:
                            target = p
                            break
            if not target:
                self.send(chat_id, f"No project matches `{sel}`. Try `/projects`.", reply_to=message_id)
                return True
            registry.set_bridge_link(chat_id, target["id"], None)
            self.send(chat_id, f"✅ Now talking to *{target['name']}* (`{target['slug']}`).",
                      reply_to=message_id)
            return True
        if root in ("/new", "/reset"):
            link = self._chat_state(chat_id)
            registry.set_bridge_link(chat_id, link.get("project_id"), None)
            self.send(chat_id, "🆕 New session. Context cleared.", reply_to=message_id)
            return True
        if root == "/status":
            proj = (registry.get_project(project_override) if project_override else None) \
                or self._current_project(chat_id)
            link = self._chat_state(chat_id)
            up = int(time.time() - self.started_at)
            self.send(chat_id,
                      f"{heading('orchestrator', 'Perun bridge')}\n"
                      f"📁 project: `{proj['slug'] if proj else '(none)'}`\n"
                      f"🧵 session: `{link.get('session_pk') or '(new)'}`\n"
                      f"📥 queue: {self.work.qsize()}\n"
                      f"⏱ uptime: {up // 3600}h {(up % 3600) // 60}m",
                      reply_to=message_id)
            return True
        if root in ("/now", "/running"):
            active = registry.list_active_runs()
            if not active:
                self.send(chat_id, "Nothing is running right now.", reply_to=message_id)
                return True
            lines = [f"⏳ **Running now — {len(active)}**"]
            for run in active[:8]:
                started = str(run.get("started_at") or run.get("admitted_at") or "")
                age = ""
                tg_time = ""
                try:
                    from datetime import datetime as _dt
                    delta = _dt.now() - _dt.fromisoformat(started)
                    minutes = int(delta.total_seconds() // 60)
                    age = f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}"
                    tg_time = time_marker(started)
                except Exception:  # pragma: no cover - a malformed timestamp
                    age = ""
                tag = "📥 queued" if run.get("status") == "queued" else "⏳ running"
                model = str(run.get("model") or "default model")
                shown_age = f"🕒 {tg_time}" if tg_time else age
                lines.append(f"• `{run.get('project_slug') or '?'}` · {tag} · "
                             f"{shown_age} · `{model}`")
            if len(active) > 8:
                lines.append(f"＋{len(active) - 8} more — see the dashboard")
            self.send(chat_id, "\n".join(lines), reply_to=message_id)
            return True
        if root in ("/health", "/doctor"):
            self.send(chat_id, self._health_report(), reply_to=message_id)
            return True
        if root in ("/routing", "/coverage"):
            rows = registry.routing_stats(days=7)
            if not rows:
                self.send(chat_id, "No routing traffic yet — the counter starts with the "
                                   "first message after a restart.", reply_to=message_id)
                return True
            by_source: dict[str, dict[str, int]] = {}
            for row in rows:
                src = str(row.get("source") or "?")
                bucket = by_source.setdefault(src, {"total": 0, "unknown": 0})
                count = int(row.get("count") or 0)
                bucket["total"] += count
                if str(row.get("action")) == "unknown":
                    bucket["unknown"] += count
            lines = ["*Routing coverage — 7 days*"]
            for src, b in sorted(by_source.items(), key=lambda kv: -kv[1]["total"]):
                pct = round((1 - b["unknown"] / b["total"]) * 100) if b["total"] else 0
                lines.append(f"• `{src}` — {pct}% of {b['total']} message(s)")
            lines.append("Coverage = share the router could classify (not the share it acted on).")
            self.send(chat_id, "\n".join(lines), reply_to=message_id)
            return True
        if root in ("/usage", "/cost"):
            hours = 24
            totals = registry.usage_totals(hours=hours)
            if not int(totals.get("sessions") or 0):
                self.send(chat_id,
                          "No usage recorded in the last 24h. Usage is captured when a "
                          "run finishes (a run started before this feature was added "
                          "has no numbers).",
                          reply_to=message_id)
                return True
            lines = [
                f"*Usage — captured in the last {hours}h*",
                f"${float(totals.get('cost_usd') or 0):.4f} · "
                f"{int(totals.get('tokens_in') or 0):,} in · "
                f"{int(totals.get('tokens_out') or 0):,} out · "
                f"{int(totals.get('cache_read') or 0):,} cached",
                f"{int(totals.get('sessions') or 0)} session(s) with usage",
            ]
            for row in registry.usage_by_project(hours=hours, limit=4):
                lines.append(
                    f"• `{row.get('slug')}` — ${float(row.get('cost') or 0):.4f} · "
                    f"{int(row.get('tokens') or 0):,} tok"
                )
            self.send(chat_id, "\n".join(lines), reply_to=message_id)
            return True
        if root == "/resume":
            proj = (registry.get_project(project_override) if project_override else None) \
                or self._current_project(chat_id)
            if not proj:
                self.send(chat_id, "Pick a project first with `/project`.", reply_to=message_id)
                return True
            runs = registry.list_runs(project_id=proj["id"], limit=6)
            if not runs:
                self.send(chat_id, f"No runs yet in `{proj['slug']}`.", reply_to=message_id)
                return True
            selector = parts[1].lstrip("#") if len(parts) > 1 else ""
            if not selector:
                lines = [f"*Resume* — `{proj['slug']}`"]
                for index, run in enumerate(runs, 1):
                    when = str(run.get("started_at") or "")[:16].replace("T", " ")
                    preview = " ".join(str(run.get("prompt") or "").split())[:60]
                    lines.append(f"{index}. `{run.get('status')}` · {when} · {preview}")
                lines.append("Send `/resume <n>` to continue that session (`/new` starts fresh).")
                self.send(chat_id, "\n".join(lines), reply_to=message_id)
                return True
            target = None
            if selector.isdigit() and 1 <= int(selector) <= len(runs):
                target = runs[int(selector) - 1]
            else:
                target = next((r for r in runs if r.get("run_uid") == selector), None)
            if not target:
                self.send(chat_id, f"No run matches `{selector}`. Try `/resume` for the list.",
                          reply_to=message_id)
                return True
            session_pk = target.get("session_pk")
            if not session_pk:
                self.send(chat_id, "That run has no session to resume (it ran in legacy "
                                   "process mode).", reply_to=message_id)
                return True
            registry.set_bridge_link(chat_id, proj["id"], session_pk)
            self.send(chat_id,
                      f"↩ Resumed `{target.get('run_uid')}` — your next message continues "
                      "that session. `/new` starts fresh.",
                      reply_to=message_id)
            return True
        if root in ("/diff", "/changes"):
            from . import git as _git

            proj = (registry.get_project(project_override) if project_override else None) \
                or self._current_project(chat_id)
            if not proj:
                self.send(chat_id, "Pick a project first with `/project`.", reply_to=message_id)
                return True
            try:
                state = _git.info(proj["path"])
            except Exception as exc:  # pragma: no cover - git failures are messy
                self.send(chat_id, f"Could not read git state: {type(exc).__name__}",
                          reply_to=message_id)
                return True
            if not state.get("repo"):
                self.send(chat_id, f"`{proj['slug']}` is not a git repository.",
                          reply_to=message_id)
                return True
            branch = state.get("branch") or "?"
            if not state.get("dirty"):
                self.send(chat_id, f"`{proj['slug']}` — working tree clean on `{branch}`.",
                          reply_to=message_id)
                return True
            lines = [f"*Changes — `{proj['slug']}`*",
                     f"{state.get('change_count')} file(s) on `{branch}`"]
            for line in (state.get("changes") or [])[:8]:
                lines.append(f"`{str(line).strip()}`")
            if int(state.get("change_count") or 0) > 8:
                lines.append("＋more — see the dashboard")
            self.send(chat_id, "\n".join(lines), reply_to=message_id)
            return True
        if root in ("/runs", "/last"):
            proj = (registry.get_project(project_override) if project_override else None) \
                or self._current_project(chat_id)
            if not proj:
                self.send(chat_id, "Pick a project first with `/project`.", reply_to=message_id)
                return True
            runs = registry.list_runs(project_id=proj["id"], limit=5)
            if not runs:
                self.send(chat_id, f"No runs yet in `{proj['slug']}`.", reply_to=message_id)
                return True
            lines = [f"*Recent runs* — `{proj['slug']}`"]
            for run in runs:
                status = str(run.get("status") or "?")
                when = str(run.get("started_at") or "")[11:16]
                detail = " ".join(
                    str(run.get("output") or run.get("error") or "").split()
                )[:80]
                lines.append(f"• {when} `{status}` {detail}".rstrip())
            self.send(chat_id, "\n".join(lines), reply_to=message_id)
            return True
        if root in ("/stop", "/cancel"):
            uid = self.run_for(chat_id)
            if uid and self.orch.cancel(uid):
                # Same path as the dashboard's Stop: cancel the orchestrator run
                # *and* close the terminal Perun opened for it. The tracker falls
                # back to `runs.pid` + the PERUN_RUN_UID marker after a restart.
                from . import process_tracker

                try:
                    process_tracker.TRACKER.terminate(uid)
                except Exception:  # pragma: no cover - a stop must never fail the reply
                    logging.getLogger("perun.telegram").exception(
                        "could not close the terminal for run %s", uid)
                self.send(chat_id, "🛑 Cancelling…", reply_to=message_id)
            else:
                self.send(chat_id, "Nothing is running.", reply_to=message_id)
            return True
        if root == "/remember":
            proj = (registry.get_project(project_override) if project_override else None) \
                or self._current_project(chat_id)
            fact = text.split(" ", 1)[1].strip() if len(parts) > 1 else ""
            if not fact:
                self.send(chat_id, "Usage: `/remember <fact>`", reply_to=message_id)
                return True
            res = memory.remember(fact, project_id=proj["id"] if proj else 0,
                                  scope="project" if proj else "global",
                                  topic="general", source="telegram")
            self.send(chat_id, "Stored ✅" if res.get("ok") else f"Refused: {res.get('reason')}",
                      reply_to=message_id)
            return True
        if root == "/recall":
            proj = (registry.get_project(project_override) if project_override else None) \
                or self._current_project(chat_id)
            query = text.split(" ", 1)[1].strip() if len(parts) > 1 else ""
            if not query:
                self.send(chat_id, "Usage: `/recall <query>`", reply_to=message_id)
                return True
            results = memory.unified_search(query, proj["id"] if proj else 0, k=5)
            if not results:
                self.send(chat_id, "No results.", reply_to=message_id)
                return True
            lines = []
            for r in results:
                tag = "DOC" if r.get("source_type") == "doc" else "FACT"
                where = r.get("source_file") or r.get("topic") or ""
                lines.append(f"[{tag}] `{where}` {(' '.join(r['content'].split()))[:160]}")
            self.send(chat_id, "\n\n".join(lines), reply_to=message_id)
            return True
        if root == "/index":
            proj = self._current_project(chat_id)
            if not proj:
                self.send(chat_id, "Select a project first with `/project`.", reply_to=message_id)
                return True
            self.send(chat_id, f"Indexing `{proj['slug']}` docs…", reply_to=message_id)
            res = memory.index_project_docs(proj["id"])
            self.send(chat_id, f"Indexed {res.get('chunks', 0)} chunk(s) from "
                               f"{len(res.get('files', []))} file(s).", reply_to=message_id)
            return True
        if root.startswith("/"):
            self.send(chat_id, f"Unknown command `{root}`. Try `/help`.", reply_to=message_id)
            return True
        return False

    def _is_current(self, chat_id: str, project: Dict[str, Any]) -> bool:
        cur = self._current_project(chat_id)
        return bool(cur and cur["id"] == project["id"])

    def handle_command(self, chat_id: str, text: str, message_id: int) -> bool:
        return self._handle_text_command(chat_id, text, message_id)

    # ---- deterministic routing ------------------------------------------- #
    _ROUTE_LEADS: Dict[str, tuple[str, ...]] = {
        "remember": ("zapamietaj", "zapamiętaj", "remember", "zapisz"),
        "recall": ("co pamietasz o", "co pamiętasz o", "co wiesz o", "przypomnij", "recall"),
    }

    @classmethod
    def _command_argument(cls, text: str, action: str) -> str:
        """Strip the trigger word/phrase so ``/remember`` gets just the payload."""
        lowered = text.casefold()
        best = -1
        length = 0
        for lead in cls._ROUTE_LEADS.get(action, ()):
            index = lowered.find(lead)
            if index < 0:
                continue
            if best < 0 or index < best or (index == best and len(lead) > length):
                best, length = index, len(lead)
        if best < 0:
            return text.strip(" \t,.:;-")
        return text[best + length :].strip(" \t,.:;-")

    def _route_message(self, chat_id: str, text: str, message_id: int) -> bool:
        """Apply the deterministic router. True when the message is fully handled.

        Disabled by default (``Settings.routing_enabled``): with routing off this
        is a no-op and every message follows the previous path.
        """
        try:
            settings = config.Settings.load()
        except Exception:  # pragma: no cover - defensive
            return False
        if not getattr(settings, "routing_enabled", False):
            return False
        shadow = bool(getattr(settings, "routing_shadow", False))
        try:
            projects = registry.list_projects(include_hidden=(chat_id in self.allowed))
            current = self._current_project(chat_id)
            decision = routing.route(
                text,
                projects=projects,
                current=(current or {}).get("slug"),
            )
        except Exception:  # pragma: no cover - routing must never break the chat
            LOG.exception("routing failed; falling back to the agent path")
            return False

        self._log_routing_decision(decision, chat_id, current, shadow=shadow)

        if shadow:
            # Log what routing *would* have done, then take the previous path.
            return False

        if decision.action == "clarify":
            names = ", ".join(f"`{slug}`" for slug in decision.candidates)
            self.send(
                chat_id,
                f"❓ Which project? {names}\n\nPick one with `/project <name>` "
                "or send the message again naming a single project.",
                reply_to=message_id,
            )
            return True

        if decision.action in ("status", "stop"):
            return self._handle_text_command(
                chat_id, f"/{decision.action}", message_id,
                project_override=decision.project,
            )

        if decision.action in ("pending", "projects", "help", "runs", "now", "health",
                              "usage", "resume", "diff", "routing"):
            # Machine-wide, read-only: the existing slash commands answer these
            # exactly, so no agent run is spent. `/pending` is dispatched by the
            # inbox handler, the rest by the command handler.
            command = f"/{decision.action}"
            if self._handle_attention_command(chat_id, command, message_id):
                return True
            return self._handle_text_command(
                chat_id, command, message_id, project_override=decision.project,
            )

        if decision.action in ("remember", "recall"):
            argument = self._command_argument(text, decision.action)
            if not argument:
                self.send(
                    chat_id,
                    "What should I remember?" if decision.action == "remember" else "What should I look up?",
                    reply_to=message_id,
                )
                return True
            return self._handle_text_command(
                chat_id, f"/{decision.action} {argument}", message_id,
                project_override=decision.project,
            )

        if (
            decision.action in ("run", "ask")
            and decision.project
            and (not current or decision.project != current.get("slug"))
        ):
            target = registry.get_project(decision.project)
            if target:
                # Switching the bound project is the same effect as /project,
                # without the operator having to type it.
                registry.set_bridge_link(chat_id, target["id"], None)
                self.send(chat_id, f"→ `{target['slug']}`", reply_to=message_id)
        # ``ask``/``unknown`` and same-project runs continue to the agent path.
        return False

    def _log_routing_decision(self, decision: routing.RouteDecision, chat_id: str,
                              current: Optional[Dict[str, Any]],
                              *, shadow: bool = False) -> None:
        """Record a routing decision. Never stores the message text."""
        try:
            project_id = None
            if decision.project:
                project = registry.get_project(decision.project)
                project_id = project["id"] if project else None
            elif current:
                project_id = current.get("id")
            meta = decision.as_dict()
            meta["source"] = "telegram"
            meta["shadow"] = shadow
            # Coverage counter: every routed message, including ``unknown``.
            try:
                registry.bump_routing_stat("telegram", decision.action)
            except Exception:  # pragma: no cover - telemetry is best-effort
                pass
            registry.log_activity(
                project_id,
                "routing_decision",
                ("shadow: " if shadow else "routing: ")
                + decision.action
                + (f" -> {decision.project}" if decision.project else ""),
                meta,
            )
        except Exception:  # pragma: no cover - telemetry is best-effort
            LOG.debug("could not record routing decision", exc_info=True)

    # ---- run a message ---- #
    def _notify_interrupted_runs(self) -> None:
        """Tell the chat when a restart killed its run.

        A restart marks in-flight runs ``interrupted``; without this the operator
        sees silence and no reply. Only runs from the last hour are
        announced, only once per run, and only to chats whose ``session_pk``
        matches, so a dashboard/API run does not message a random chat.
        """
        try:
            minutes = int(config.Settings.load().telegram_interrupt_notice_minutes)
        except Exception:
            minutes = 60
        try:
            runs = registry.recent_interrupted_runs(minutes=max(1, minutes))
        except Exception:  # pragma: no cover - startup must not depend on this
            LOG.debug("could not list interrupted runs", exc_info=True)
            return
        for run in runs:
            run_uid = str(run.get("run_uid") or "")
            if not run_uid:
                continue
            try:
                if registry.interrupt_notice_sent(run_uid):
                    continue
                chats = registry.chats_for_session(run.get("session_pk"))
                if not chats:
                    continue
                for chat_id in chats:
                    self.send(
                        chat_id,
                        "⚠️ Your previous run was interrupted by a Perun restart, "
                        "so no reply was produced. Please resend the message.",
                    )
                registry.log_activity(
                    run.get("project_id"),
                    "run_interrupted_notice",
                    f"interrupted run notice for {run_uid}",
                    {"run_uid": run_uid, "chats": chats},
                )
            except Exception:  # pragma: no cover - best effort
                LOG.debug("could not notify about interrupted run %s", run_uid, exc_info=True)

    # ---- operator inbox ------------------------------------------------ #
    def _attention_settings(self) -> Optional["config.Settings"]:
        try:
            return config.Settings.load()
        except Exception:  # pragma: no cover - defensive
            return None

    # Only these target kinds are informational enough to coalesce. A decision
    # (permission, form, proposal, handoff, perun_ask_operator) must never wait.
    DIGEST_KINDS = ("room",)

    def _attention_digest_seconds(self, settings: "config.Settings") -> int:
        try:
            return max(0, int(getattr(settings, "attention_digest_seconds", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _attention_deliver(self, items: List[Dict[str, Any]],
                           settings: "config.Settings") -> int:
        """Send cards for never-delivered items; returns how many went out."""
        from . import attention

        digest = self._attention_digest_seconds(settings)
        sent = 0
        for item in items:
            if item["id"] in self._digested:
                # Already listed on a digest card; its buttons open the real one.
                continue
            ok, reason = attention.eligible(item, settings)
            if not ok:
                # Log once per item per process; do not mark it delivered, so a
                # quiet-hours or hourly-cap item still goes out later.
                if item["id"] not in self._attention_logged:
                    self._attention_logged.add(item["id"])
                    registry.log_activity(
                        item.get("project_id"), "attention_skipped",
                        f"att_{item['id']} not sent: {reason}",
                        {"attention_id": item["id"], "reason": reason},
                    )
                continue
            chats = [
                chat_id for chat_id in attention.recipients(
                    item, allowed=list(self.allowed),
                    muted=registry.attention_muted_projects,
                )
                # A chat that already has this item (a digest that reached it, or
                # a replay skip) must not get a second card.
                if not registry.attention_delivered_to(item["id"], chat_id)
            ]
            if not chats:
                if item["id"] not in self._attention_logged:
                    self._attention_logged.add(item["id"])
                    registry.log_activity(
                        item.get("project_id"), "attention_skipped",
                        f"att_{item['id']} not sent: no paired chat",
                        {"attention_id": item["id"], "reason": "no chat"},
                    )
                continue
            if digest and str(item.get("target_kind") or "") in self.DIGEST_KINDS:
                # Hold informational notices for the window; the flush sends one
                # card per project instead of one message per item.
                self._digest_pending.setdefault(item["id"], time.time())
                continue
            text = attention.render(
                item, show_paths=bool(getattr(settings, "attention_show_paths", False))
            )
            # If other things are already waiting, say so on the card: a burst of
            # separate messages is confusing, and /pending is the list.
            try:
                waiting = [other for other in registry.open_attention(
                    project_id=item.get("project_id"), limit=50)
                    if other["id"] != item["id"]]
            except Exception:  # pragma: no cover - cosmetic only
                waiting = []
            if waiting:
                text += f"\n\n＋{len(waiting)} more waiting — /pending"
            markup = attention.keyboard(item)
            if getattr(settings, "attention_shadow", True):
                if item["id"] not in self._attention_logged:
                    self._attention_logged.add(item["id"])
                    LOG.info("[shadow] att_%s would go to %s: %s",
                             item["id"], chats, text.splitlines()[0])
                continue
            for chat_id in chats:
                message_id = self.send(chat_id, text, reply_markup=markup)
                if message_id:
                    registry.record_attention_delivery(item["id"], chat_id, message_id)
                    sent += 1
                else:
                    # Telegram refused/failed: leave the item undelivered so the
                    # next tick retries instead of silently losing the card.
                    if item["id"] not in self._attention_logged:
                        self._attention_logged.add(item["id"])
                        LOG.warning("could not send att_%s to %s; will retry",
                                    item["id"], chat_id)
        return sent

    def _flush_digest(self, settings: "config.Settings") -> int:
        """Send one card per project for the informational items whose window elapsed."""
        from . import attention

        seconds = self._attention_digest_seconds(settings)
        if not seconds:
            self._digest_pending.clear()
            return 0
        now = time.time()
        due = [aid for aid, seen in list(self._digest_pending.items())
               if now - seen >= seconds]
        if not due:
            return 0
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for attention_id in due:
            self._digest_pending.pop(attention_id, None)
            item = registry.get_attention(attention_id)
            if not item or item.get("state") != "open":
                self._digested.discard(attention_id)
                continue
            grouped.setdefault(str(item.get("project_id") or 0), []).append(item)

        # A card carries five items. Everything else is *requeued*, never marked
        # as shown: marking it here (as this used to) made items six and beyond
        # invisible forever — no card, no delivery row, and `/pending` inner-joins
        # deliveries, so they were not listed there either.
        sent = 0
        for items in grouped.values():
            items.sort(key=lambda it: it["id"])
            chats = attention.recipients(
                items[0], allowed=list(self.allowed),
                muted=registry.attention_muted_projects,
            )
            eligible_ids = {it["id"] for it in items if attention.eligible(it, settings)[0]}
            eligible = [it for it in items if it["id"] in eligible_ids]
            if not chats or not eligible:
                # No recipient, quiet hours, or the hourly cap: hold for later.
                for item in items:
                    self._digested.discard(item["id"])
                    self._digest_pending.setdefault(item["id"], now)
                continue
            listed, rest = eligible[:5], eligible[5:]
            label = attention.project_label(listed[0])
            # Markdown, like every other card: ``send()`` runs ``format_html``.
            lines = [f"🗂 **{len(eligible)} waiting in {label}**"]
            for item in listed:
                # Same redaction a single card would get: a room message can quote
                # a token or an Authorization header, and a digest must not be the
                # one path that lets it through.
                question = " ".join(attention.redact(item.get("question") or "").split())
                if len(question) > 90:
                    question = question[:87] + "…"
                lines.append(f"{badge(item.get('target_kind'))} "
                             f"`att_{item['id']}` — {question}")
            if rest:
                lines.append(f"…and {len(rest)} more, in the next card.")
            text = "\n".join(lines)
            rows = [
                [{"text": f"📖 Open att_{item['id']}",
                  "callback_data": f"att:{item['id']}:show",
                  "style": "primary"}]
                for item in listed
            ]
            project_id = listed[0].get("project_id")
            if project_id:
                rows.append([{
                    "text": f"🔕 Mute {label}",
                    "callback_data": f"muteproj:{int(project_id)}",
                    "style": "primary",
                }])
            markup = {"inline_keyboard": rows}
            if getattr(settings, "attention_shadow", True):
                # Log once per item, send nothing, and keep them queued so
                # turning shadow off does not silently lose the backlog.
                for item in listed:
                    if item["id"] not in self._attention_logged:
                        self._attention_logged.add(item["id"])
                        LOG.info("[shadow] digest of %d item(s) would go to %s",
                                 len(eligible), chats)
                for item in eligible:
                    self._digest_pending.setdefault(item["id"], now)
                continue
            # A previous attempt may already have reached some chats; only the
            # ones still missing the listed items get the reminder.
            chats = [chat_id for chat_id in chats
                     if not all(registry.attention_delivered_to(item["id"], chat_id)
                                for item in listed)]
            if not chats:
                for item in listed:
                    self._digested.add(item["id"])
                for item in rest:
                    self._digest_pending.setdefault(item["id"], now)
                continue
            delivered: List[str] = []
            failed: List[str] = []
            for chat_id in chats:
                message_id = self.send(chat_id, text, reply_markup=markup)
                if message_id:
                    delivered.append(chat_id)
                else:
                    failed.append(str(chat_id))
            if not delivered:
                LOG.warning("could not send a digest to %s; will retry", ", ".join(failed))
                for item in eligible:
                    self._digest_pending.setdefault(item["id"], now)
                continue
            if failed:
                LOG.warning("digest reached %d/%d chat(s); %s will retry next window",
                            len(delivered), len(chats), ", ".join(failed))
            complete = not failed
            # Per-chat records, not one global "shown": a reached chat keeps its
            # copy even when another chat failed, and the next attempt skips it.
            for item in listed:
                for chat_id in delivered:
                    # A digest is a list, not a replyable card: record the
                    # chat as served but leave the message id empty, so "Open"
                    # still posts a fresh card instead of editing the list.
                    registry.record_attention_delivery(item["id"], chat_id, None)
                if complete:
                    self._digested.add(item["id"])
                else:
                    self._digest_pending.setdefault(item["id"], now)
                registry.log_activity(
                    item.get("project_id"), "attention_digest",
                    f"att_{item['id']} listed in a digest",
                    {"attention_id": item["id"], "chats": delivered},
                )
            for item in rest:
                self._digest_pending.setdefault(item["id"], now)
            sent += len(delivered)
        if len(self._digested) > 200:
            # Ids only grow; a closed item never comes back through the delivery
            # path, so the oldest half can be forgotten.
            self._digested = set(sorted(self._digested)[-100:])
        if len(self._attention_logged) > 200:
            self._attention_logged = set(sorted(self._attention_logged)[-100:])
        return sent

    def _attention_startup_replay(self, settings: "config.Settings") -> None:
        """Send only what a restart could have lost: open, undelivered, recent."""
        from . import attention

        try:
            window = int(getattr(settings, "attention_replay_open_minutes", 15))
        except (TypeError, ValueError):
            window = 15
        recent = [item for item in registry.undelivered_attention(
            within_minutes=max(1, window))]
        recent_ids = {item["id"] for item in recent}
        # A restart loses the in-memory digest set, but those items were already
        # shown in a coalesced card: the activity marker is what keeps them from
        # being listed again on every boot.
        fresh = [item for item in recent
                 if not registry.activity_exists(
                     "attention_digest", f"att_{item['id']} listed in a digest")]
        for item in registry.undelivered_attention():
            if item["id"] in recent_ids:
                continue
            registry.log_activity(
                item.get("project_id"), "attention_replay_skipped",
                f"att_{item['id']} left unsent after restart (older than {window} min)",
                {"attention_id": item["id"]},
            )
            for chat_id in list(self.allowed):
                registry.record_attention_delivery(item["id"], chat_id, None)
        if fresh:
            self._attention_deliver(fresh, settings)
        attention.expire_stale()

    def attention_tick(self, *, replay: bool = False) -> int:
        """One notifier pass (also used by tests)."""
        settings = self._attention_settings()
        if settings is None or not settings.attention_enabled:
            return 0
        if replay:
            self._attention_startup_replay(settings)
            return 0
        try:
            registry.expire_attention()
            if time.time() - self._last_attention_prune > 3600:
                self._last_attention_prune = time.time()
                registry.prune_attention()
        except Exception:  # pragma: no cover - housekeeping is best effort
            LOG.debug("attention housekeeping failed", exc_info=True)
        try:
            from . import attention_watcher
            attention_watcher.poll_once()
        except Exception:  # pragma: no cover - watcher failures must not stop sending
            LOG.debug("attention watcher tick failed", exc_info=True)
        try:
            from . import attention as _attention
            if _attention.drain_outbox():
                LOG.info("attention: posted queued operator answers")
        except Exception:  # pragma: no cover - delivery retries on the next tick
            LOG.debug("attention outbox drain failed", exc_info=True)
        sent = self._attention_deliver(registry.undelivered_attention(), settings)
        try:
            sent += self._flush_digest(settings)
        except Exception:  # pragma: no cover - a digest must never stop delivery
            LOG.debug("attention digest flush failed", exc_info=True)
        return sent

    # A stream outage falls back to this cadence, which matches the old poller.
    FAST_POLL_SECONDS = 5
    # With events driving detection, the timer is only the safety net — a stored
    # 5 s value (the old default) must not keep polling five times a minute.
    RECONCILE_SECONDS = 60

    @classmethod
    def attention_interval(cls, settings: Any, ingest_active: bool) -> int:
        """Seconds between detection passes for the current mode."""
        try:
            configured = int(getattr(settings, "attention_poll_seconds",
                                     cls.RECONCILE_SECONDS) or cls.RECONCILE_SECONDS)
        except (TypeError, ValueError):
            # A hand-edited config must never take the poller down with it.
            configured = cls.RECONCILE_SECONDS
        configured = max(2, configured)
        if ingest_active:
            return max(configured, cls.RECONCILE_SECONDS)
        return min(configured, cls.FAST_POLL_SECONDS)

    def _attention_loop(self) -> None:
        settings = self._attention_settings()
        if settings is None or not settings.attention_enabled:
            return
        try:
            registry.prune_attention()
            attention = __import__("perun.attention", fromlist=["attention"])
            attention.expire_stale()
        except Exception:  # pragma: no cover - startup best effort
            LOG.debug("attention startup sweep failed", exc_info=True)
        try:
            self.attention_tick(replay=True)
        except Exception:  # pragma: no cover - never kill the poller
            LOG.exception("attention replay failed")
        # Prefer the service's event stream; polling stays as the safety net.
        attention_watcher = None
        ingest = False
        if getattr(settings, "attention_ingest_events", True):
            try:
                from . import attention_watcher as _watcher
                attention_watcher = _watcher
                ingest = attention_watcher.start_event_ingest()
            except Exception:  # pragma: no cover - never kill the poller
                LOG.debug("attention: event ingestion unavailable", exc_info=True)
        LOG.info("operator inbox online (chats=%s, poll=%ss, events=%s)",
                 self.allowed or "<no paired chat>",
                 self.attention_interval(settings, ingest),
                 "on" if ingest else "off")
        try:
            while not self.stop_event.is_set():
                current = self._attention_settings() or settings
                live = bool(attention_watcher
                            and attention_watcher.event_ingest_active())
                interval = self.attention_interval(current, live)
                if self._digest_pending:
                    # Something is waiting out its digest window: wake often enough
                    # that the window closes on time, not on the next 60 s tick.
                    interval = min(interval, self.FAST_POLL_SECONDS)
                if self.stop_event.wait(interval):
                    break
                try:
                    self.attention_tick()
                except Exception:  # pragma: no cover - keep the loop alive
                    LOG.exception("attention tick failed")
        finally:
            if attention_watcher is not None:
                try:
                    attention_watcher.stop_event_ingest()
                except Exception:  # pragma: no cover - shutdown must not raise
                    LOG.debug("attention: could not stop event ingestion",
                              exc_info=True)

    def _chat_open_items(self, chat_id: str) -> List[Dict[str, Any]]:
        return registry.open_attention_for_chat(chat_id)

    def _handle_attention_reply(self, chat_id: str, msg: Dict[str, Any]) -> bool:
        """A Telegram reply-to on a card answers that item."""
        from . import attention

        text = (msg.get("text") or "").strip()
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if not text or not reply_to or text.startswith("/"):
            return False
        try:
            item = registry.attention_by_message(chat_id, int(reply_to))
        except (TypeError, ValueError):
            return False
        if not item:
            return False
        message_id = int(msg.get("message_id") or 0)
        if item["state"] != "open":
            self.send(chat_id, f"att_{item['id']} is already {item['state']}.",
                      reply_to=message_id or None)
            return True
        result = attention.answer(item["id"], text, by=f"telegram:{chat_id}")
        if not result.get("ok"):
            self.send(chat_id, f"⚠️ {result.get('error') or 'could not deliver'}",
                      reply_to=message_id or None,
                      reply_markup=attention.keyboard(item) if item["target_kind"] == "permission" else None)
            return True
        self.send(chat_id, attention.confirmation(item, result),
                  reply_to=message_id or None,
                  reply_markup=attention.followup_keyboard(item, result))
        return True

    def _maybe_attention_text(self, chat_id: str, text: str, message_id: int) -> bool:
        """Offer a one-tap confirmation instead of guessing a plain text answer."""
        settings = self._attention_settings()
        if settings is None or not getattr(settings, "attention_plain_text_confirm", True):
            return False
        if not text or text.startswith("/"):
            return False
        items = [item for item in self._chat_open_items(chat_id)
                 if item["target_kind"] not in ("permission", "proposal")]
        if len(items) != 1:
            return False
        item = items[0]
        key = f"{chat_id}:{item['id']}"
        if len(self._pending_answers) > 50:
            self._pending_answers.clear()
        self._pending_answers[key] = text
        label = f"att_{item['id']}"
        self.send(
            chat_id,
            f"Send this to *{label}* ({item['target_kind']})?\n\n> {text[:300]}",
            reply_to=message_id or None,
            reply_markup={"inline_keyboard": [[
                {"text": "Send", "callback_data": f"att:{item['id']}:send_text"},
                {"text": "No", "callback_data": f"att:{item['id']}:discard_text"},
            ]]},
        )
        return True

    # ---- card edits (in-place, so the chat stays one card per decision) ---- #
    def _clear_buttons(self, chat_id: str, message_id: int) -> None:
        if not chat_id or not message_id:
            return
        self.api("editMessageReplyMarkup", chat_id=chat_id,
                 message_id=message_id,
                 reply_markup={"inline_keyboard": []}, retries=2)

    def _edit_card(self, chat_id: str, message_id: int, text: str, *,
                   reply_markup: Optional[Dict[str, Any]] = None,
                   retries: int = 2) -> bool:
        """Edit a card in place; falls back to a plain edit, then to a send."""
        if not chat_id or not message_id:
            return False
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": format_html(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        res = self.api("editMessageText", retries=retries, **payload)
        if not res.get("ok"):
            # HTML rejected, or the message is too old / not modified: one plain
            # retry covers "too old"; keep the keyboard so a formatting problem
            # never strips the operator's buttons.
            payload.pop("parse_mode", None)
            payload["text"] = plain_text(text)
            res = self.api("editMessageText", retries=1, **payload)
        if not res.get("ok") and "reply_markup" in payload:
            degraded = _downgrade_markup(payload.get("reply_markup"))
            if degraded:
                payload["reply_markup"] = degraded
                res = self.api("editMessageText", retries=1, **payload)
        if not res.get("ok") and "reply_markup" in payload:
            payload.pop("reply_markup", None)
            res = self.api("editMessageText", retries=1, **payload)
        return bool(res.get("ok"))

    def _show_item_state(self, chat_id: str, message_id: int,
                         item: Dict[str, Any], note: str) -> None:
        from . import attention

        if not self._edit_card(chat_id, message_id,
                               attention.render(item) + f"\n\n{note}",
                               reply_markup=attention.keyboard(item)):
            self.send(chat_id, note, reply_to=message_id or None)

    def _finish_card(self, chat_id: str, message_id: int,
                     item: Dict[str, Any], result: Dict[str, Any]) -> None:
        from . import attention

        item = result.get("item") or item
        if result.get("ok"):
            text = attention.render(item) + "\n\n" + attention.confirmation(item, result)
            keyboard = attention.followup_keyboard(item, result)
        else:
            text = attention.render(item) + f"\n\n⚠️ {result.get('error')}"
            keyboard = attention.keyboard(item)
        if not self._edit_card(chat_id, message_id, text, reply_markup=keyboard):
            self.send(chat_id, text, reply_to=message_id or None,
                      reply_markup=keyboard)

    def _handle_attention_callback(self, cq: Dict[str, Any], data: str) -> None:
        from . import attention

        callback_id = cq.get("id")

        def ack(text: str) -> None:
            self.api("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])

        parts = data.split(":", 2)
        try:
            attention_id = int(parts[1]) if len(parts) >= 3 else 0
        except ValueError:
            attention_id = 0
        if not attention_id:
            # Malformed payload (old card, hand-edited data): acknowledge so the
            # client does not spin forever.
            ack("That button is not valid.")
            return
        action = parts[2]
        chat_id = str((cq.get("message", {}).get("chat") or {}).get("id"))
        message_id = int(cq.get("message", {}).get("message_id") or 0)
        if not chat_id:
            return
        if not self.authorised(chat_id):
            ack("Not authorised.")
            return
        item = registry.get_attention(attention_id)
        if not item:
            ack("That item is gone.")
            return
        if item["state"] != "open" and action not in ("show", "steer", "undo"):
            # A tap on an old card must say so, not try to act again. ``steer``
            # and ``undo`` act on an *answered* item by design.
            self._pending_answers.pop(f"{chat_id}:{attention_id}", None)
            ack(f"Already {item['state']}")
            self._show_item_state(chat_id, message_id, item,
                                  f"Already {item['state']}.")
            return
        if action in ("always_yes", "approve", "reject") and chat_id not in self.allowed:
            ack("Only the paired owner can do that.")
            return

        if action == "show":
            ack("Here it is")
            # A digest card lists several items behind one message: editing it
            # would replace the list, so open a fresh, replyable card and record
            # that delivery (a reply-to must resolve to this item, not the list).
            own_card = registry.attention_delivered_message(item["id"], chat_id)
            if own_card and message_id and int(own_card) == int(message_id):
                self._edit_card(chat_id, message_id, attention.render(item),
                                reply_markup=attention.keyboard(item))
            else:
                text = attention.render(item)
                sent_id = self.send(chat_id, text,
                                    reply_markup=attention.keyboard(item))
                if sent_id:
                    registry.record_attention_delivery(item["id"], chat_id, sent_id)
            return

        # Acknowledge immediately, then edit the card in place: the spinner is
        # gone before any work and the chat keeps one card per decision.
        ack("Working…")

        if action[:1] == "t" and action[1:].isdigit():
            # Multi-select form: a tick re-renders the card, it does not answer,
            # so the keyboard is swapped in one edit instead of cleared first.
            result = attention.toggle(attention_id, int(action[1:]),
                                      by=f"telegram:{chat_id}")
            fresh = result.get("item") or item
            if result.get("ok"):
                self._edit_card(chat_id, message_id, attention.render(fresh),
                                reply_markup=attention.keyboard(fresh))
            else:
                self._finish_card(chat_id, message_id, fresh, result)
            return

        self._clear_buttons(chat_id, message_id)

        if action == "always":
            # A persistent grant needs a second, explicit tap: show the patterns.
            # Redacted like every other composed string: a save pattern can quote
            # the command, including the credential the card already masks.
            patterns = ", ".join(
                attention.redact(str(p))
                for p in ((item.get("ref") or {}).get("save") or [])[:3]
            ) or "the tool's proposed patterns"
            self._edit_card(
                chat_id, message_id,
                f"⚠️ *Always allow* saves `{patterns}` for "
                f"*{attention.project_label(item)}* and applies to future requests.\n"
                f"Confirm?  att_{attention_id}",
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ Confirm always allow",
                     "callback_data": f"att:{attention_id}:always_yes",
                     "style": "danger"},
                    {"text": "↩ Cancel", "callback_data": f"att:{attention_id}:cancel",
                     "style": "primary"},
                ]]},
            )
            return

        if action == "cancel":
            self._edit_card(chat_id, message_id,
                            attention.render(item) + "\n\nNo change made.",
                            reply_markup=attention.keyboard(item))
            return

        if action == "discard_text":
            text = self._pending_answers.pop(f"{chat_id}:{attention_id}", "")
            if text:
                # "No" means "this was for Perun, not for the card": keep the
                # user's message instead of dropping it.
                self.work.put({"chat_id": chat_id, "text": text,
                               "message_id": message_id})
            self._edit_card(chat_id, message_id,
                            attention.render(item)
                            + "\n\nNot an answer — passed it to Perun instead.",
                            reply_markup=attention.keyboard(item))
            return

        if action == "send_text":
            text = self._pending_answers.pop(f"{chat_id}:{attention_id}", "")
            if not text:
                self._edit_card(chat_id, message_id,
                                attention.render(item)
                                + "\n\nThat draft expired; resend your answer.",
                                reply_markup=attention.keyboard(item))
                return
            self._finish_card(chat_id, message_id, item,
                              attention.answer(attention_id, text,
                                               by=f"telegram:{chat_id}"))
            return

        if action == "steer":
            self._finish_card(chat_id, message_id, item,
                              attention.set_delivery(attention_id, "steer",
                                                     by=f"telegram:{chat_id}"))
            return

        if action == "undo":
            self._finish_card(chat_id, message_id, item,
                              attention.undo(attention_id, by=f"telegram:{chat_id}"))
            return

        choice = "always" if action == "always_yes" else action
        self._finish_card(chat_id, message_id, item,
                          attention.choose(attention_id, choice,
                                           by=f"telegram:{chat_id}"))

    def _handle_attention_command(self, chat_id: str, text: str,
                                  message_id: int) -> bool:
        """``/pending``, ``/answer``, ``/cancel``, ``/mute``, ``/unmute``."""
        from . import attention

        parts = text.strip().split()
        if not parts:
            return False
        root = parts[0].lower().split("@")[0]
        if root not in ("/pending", "/answer", "/cancel", "/mute", "/unmute"):
            return False

        if root == "/pending":
            items = self._chat_open_items(chat_id)
            if not items:
                self.send(chat_id, "Nothing is waiting on you.", reply_to=message_id or None)
                return True
            lines = [f"🗂 **Waiting on you — {len(items)}**"]
            keyboard: List[List[Dict[str, Any]]] = []
            for item in items:
                when = time_marker(str(item.get("created_at") or "")) or \
                    str(item.get("created_at") or "")[:16].replace("T", " ")
                lines.append(
                    f"{badge(item.get('target_kind'))} `att_{item['id']}` · "
                    f"{attention.project_label(item)} · "
                    f"{item['target_kind']} · 🕒 {when}"
                )
                keyboard.append([{
                    "text": f"📖 att_{item['id']}",
                    "callback_data": f"att:{item['id']}:show",
                    "style": "primary",
                }])
            self.send(chat_id, "\n".join(lines), reply_to=message_id or None,
                      reply_markup={"inline_keyboard": keyboard[:8]})
            return True

        if root == "/answer":
            body = text.strip().split(" ", 1)
            payload = body[1].strip() if len(body) > 1 else ""
            items = [item for item in self._chat_open_items(chat_id)
                     if item["target_kind"] not in ("permission", "proposal")]
            if not payload:
                self.send(chat_id, "Usage: `/answer <text>`", reply_to=message_id or None)
                return True
            if not items:
                self.send(chat_id, "Nothing accepts a text answer right now.",
                          reply_to=message_id or None)
                return True
            if len(items) > 1:
                listing = "\n".join(
                    f"• att_{i['id']} — {attention.redact(i['question'])[:60]}"
                    for i in items[:8]
                )
                self.send(chat_id, "More than one is open — reply to the card you mean:\n"
                                   + listing, reply_to=message_id or None)
                return True
            item = items[0]
            result = attention.answer(item["id"], payload, by=f"telegram:{chat_id}")
            self.send(chat_id, attention.confirmation(item, result)
                      if result.get("ok") else f"⚠️ {result.get('error')}",
                      reply_to=message_id or None,
                      reply_markup=attention.followup_keyboard(item, result)
                      if result.get("ok") else None)
            return True

        if root == "/cancel":
            if len(parts) < 2:
                self.send(chat_id, "Usage: `/cancel att_<id>`", reply_to=message_id or None)
                return True
            try:
                attention_id = int(parts[1].replace("att_", ""))
            except ValueError:
                self.send(chat_id, "That is not an attention id.", reply_to=message_id or None)
                return True
            if registry.close_attention(attention_id, "cancelled",
                                        answered_by=f"telegram:{chat_id}"):
                self.send(chat_id, f"att_{attention_id} cancelled.",
                          reply_to=message_id or None)
            else:
                self.send(chat_id, "That item is already closed or does not exist.",
                          reply_to=message_id or None)
            return True

        if root in ("/mute", "/unmute"):
            if len(parts) < 2:
                self.send(chat_id, f"Usage: `{root} <project>`", reply_to=message_id or None)
                return True
            slug = parts[1].lower()
            if root == "/mute":
                registry.mute_attention_project(chat_id, slug)
                self.send(chat_id, f"🔇 {slug}: notices muted for this chat. "
                                   "They still appear in `/pending`.",
                          reply_to=message_id or None)
            else:
                registry.unmute_attention_project(chat_id, slug)
                self.send(chat_id, f"🔔 {slug}: notices re-enabled.",
                          reply_to=message_id or None)
            return True
        return False

    def run_message(self, chat_id: str, text: str, message_id: int) -> None:
        if self._route_message(chat_id, text, message_id):
            return
        proj = self._current_project(chat_id)
        if not proj:
            if not registry.list_projects():
                self.send(chat_id, "No projects registered yet. Run "
                                   "`python -m perun discover` (or open the "
                                   "dashboard) to add one, then try again.",
                          reply_to=message_id)
            else:
                self.send(chat_id, "Multiple projects registered — pick one with "
                                   "`/projects` for buttons or `/project <name>`.",
                          reply_to=message_id)
            return
        link = self._chat_state(chat_id)
        session_pk = link.get("session_pk")
        oc_sid = ""
        if session_pk:
            s = registry.get_session(session_pk)
            if s:
                oc_sid = s.get("opencode_session_id") or ""

        typing_done = threading.Event()
        threading.Thread(target=self._typing_loop, args=(chat_id, typing_done), daemon=True).start()
        draft_stop = threading.Event()
        try:
            run = self.orch.start_run(proj["slug"], text, session_pk=session_pk,
                                      opencode_session_id=oc_sid)
        except Exception as exc:
            typing_done.set()
            self.send(chat_id, f"❌ Could not start run: {exc}", reply_to=message_id)
            return
        uid = run["run_uid"]
        if self._is_private_chat(chat_id):
            draft_stop = self._start_draft(chat_id, uid)
        with self._runs_lock:
            self._runs[chat_id] = uid
        try:
            while not self.orch.is_final(uid):
                if self.stop_event.is_set():
                    break
                time.sleep(1.0)
        finally:
            typing_done.set()
            draft_stop.set()
            with self._runs_lock:
                self._runs.pop(chat_id, None)
        final = registry.get_run(uid) or run
        reply = (final.get("output") or "").strip()
        new_session_pk = final.get("session_pk")
        if new_session_pk:
            registry.set_bridge_link(chat_id, proj["id"], new_session_pk)
        # Claim the delivery: the reply drainer may race us after a restart, and
        # exactly one of the two is allowed to send.
        if not registry.claim_reply_delivery(uid):
            return
        if final.get("status") == "cancelled":
            self.send(chat_id, "🛑 Run cancelled.", reply_to=message_id)
            return
        if not reply:
            err = (final.get("error") or "no reply").strip()[-1500:]
            self.send(chat_id, f"❌ Agent {final.get('status')}.\n\n{err}", reply_to=message_id)
            return
        self.send(chat_id, reply, reply_to=message_id)

    # ---- reply delivery ------------------------------------------------ #
    def _deliver_pending_replies(self, limit: int = 10) -> int:
        """One drain pass. Returns how many runs were delivered."""
        sent = 0
        for run in registry.runs_awaiting_reply(limit=limit):
            uid = run["run_uid"]
            chats = registry.chats_for_session(run.get("session_pk"))
            if not chats:
                registry.claim_reply_delivery(uid)
                continue
            if not registry.claim_reply_delivery(uid):
                continue
            text = (run.get("output") or "").strip()
            if not text:
                text = (f"⚠️ Run {run.get('status')}: "
                        f"{(run.get('error') or 'no output')[:1500]}")
            # ``send()`` returns None instead of raising, so the old try/except
            # never fired: a Telegram outage burned the claim and the reply was
            # never retried. Release it when nothing reached anyone.
            delivered = 0
            for chat_id in chats:
                if self.send(chat_id, text):
                    delivered += 1
            if not delivered:
                registry.clear_reply_sent(uid)
                LOG.warning("could not deliver run %s to any chat; will retry", uid)
                continue
            if delivered < len(chats):
                LOG.warning("run %s reply reached %d/%d chat(s)", uid,
                            delivered, len(chats))
            registry.log_activity(
                run.get("project_id"), "run_reply_delivered",
                f"Run {uid} reply delivered to {len(chats)} chat(s)",
                {"run_uid": uid, "chats": chats},
            )
            sent += 1
        return sent

    def _reply_drainer(self) -> None:
        """Deliver replies of runs that finished without a waiting caller.

        A run adopted after a restart (or dispatched by the reconciler) has no
        Telegram worker waiting on it, so its output would otherwise be stored and
        never sent. The conditional claim makes delivery exactly-once relative to
        ``run_message``.
        """
        while not self.stop_event.wait(5.0):
            try:
                self._deliver_pending_replies()
            except Exception:  # pragma: no cover - keep the loop alive
                LOG.exception("reply drainer failed")

    # ---- worker + poll ---- #
    def worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.work.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.run_message(job["chat_id"], job["text"], job["message_id"])
            except Exception:
                LOG.exception("worker error")
                self.send(job["chat_id"], "❌ Internal bridge error (see perun.log).")
            finally:
                self.work.task_done()

    def authorised(self, chat_id: str) -> bool:
        """True only for chats that were explicitly granted access.

        Access comes from the allow-list (``config.json``
        ``telegram_allowed_chats``, ``TELEGRAM_CHAT_ID``) or from a completed
        ``/pair <code>`` handshake. An **empty** allow-list grants nobody: the
        old behaviour ("the first chat that writes wins") handed full ``--auto``
        agent access to whoever found the bot's public name first, and was
        forgotten on every restart.
        """
        if chat_id in self.allowed:
            return True
        if not self.allowed:
            with self._pair_lock:
                return chat_id in self._allowed_runtime
        return False

    def pair_code(self) -> str:
        """The persisted one-time pairing code, or ``""`` when not needed."""
        if self.allowed:
            return ""
        try:
            return (config.Settings.load().telegram_pair_code or "").strip()
        except Exception:  # pragma: no cover - defensive
            return ""

    def _handle_pair(self, chat_id: str, text: str, message_id: int) -> None:
        """``/pair <code>``: the only way an unknown chat gains access."""
        parts = text.split(" ", 1)
        provided = parts[1].strip() if len(parts) > 1 else ""
        if chat_id in self.allowed:
            self.send(chat_id, "✅ This chat is already paired.", reply_to=message_id)
            return
        expected = self.pair_code()
        if not expected:
            self.send(chat_id, "⛔ Pairing is off: this bridge has a configured "
                               "chat allow-list.", reply_to=message_id)
            return
        # Compare in constant time; the code is short and remote-guessable.
        if not provided or not hmac.compare_digest(provided, expected):
            LOG.warning("telegram pairing attempt from chat %s with a wrong code", chat_id)
            self.send(chat_id, "⛔ Wrong pairing code. Read it from the dashboard "
                               "(Telegram → Bot settings).", reply_to=message_id)
            return
        if chat_id not in self.allowed:
            self.allowed.append(chat_id)
        with self._pair_lock:
            if chat_id not in self._allowed_runtime:
                self._allowed_runtime.append(chat_id)
        # Persist + consume the code so it is truly one-time, even across a
        # bridge restart.
        try:
            st = config.Settings.load()
            chats = list(st.telegram_allowed_chats)
            if chat_id not in chats:
                chats.append(chat_id)
            st.telegram_allowed_chats = chats
            st.telegram_pair_code = ""
            st.save()
        except Exception:  # pragma: no cover - defensive
            LOG.exception("could not persist the paired Telegram chat")
        LOG.info("telegram chat %s paired via one-time code", chat_id)
        self.send(chat_id, "✅ Paired. You now have access to Perun — send "
                           "`/help` to see the commands.", reply_to=message_id)

    def _handle_update(self, update: Dict[str, Any]) -> None:
        if update.get("stopped_message_generation"):
            self._handle_generation_stopped(update["stopped_message_generation"])
            return
        if update.get("callback_query"):
            self._handle_callback(update)
            return
        msg = (update.get("message") or update.get("edited_message")
               or update.get("channel_post") or {})
        if (msg.get("from") or {}).get("is_bot"):
            return
        text = msg.get("text")
        if not text or not text.strip():
            return
        chat_id = str((msg.get("chat") or {}).get("id"))
        if not chat_id:
            return
        message_id = int(msg.get("message_id") or 0)
        # ``/pair`` must run *before* the authorisation gate: its whole purpose
        # is to authorise a chat that is not on the list yet.
        root = text.strip().split()[0].split("@", 1)[0].lower()
        if root == "/pair":
            self._handle_pair(chat_id, text.strip(), message_id)
            return
        if not self.authorised(chat_id):
            self.send(chat_id, "⛔ Not authorised. Send `/pair <code>` with the code "
                               "shown in the Perun dashboard to request access.")
            return
        dedup_key = (chat_id, message_id)
        with self._seen_ids_lock:
            if dedup_key in self._seen_ids:
                LOG.debug("skipping duplicate Telegram update %s/%s", chat_id, message_id)
                return
            self._seen_ids.add(dedup_key)
            # Keep the set bounded.
            if len(self._seen_ids) > 1000:
                self._seen_ids = set(list(self._seen_ids)[-500:])
        if self._handle_attention_command(chat_id, text, message_id):
            return
        if self._handle_attention_reply(chat_id, msg):
            return
        if self.handle_command(chat_id, text, message_id):
            return
        if self._maybe_attention_text(chat_id, text, message_id):
            return
        self.work.put({"chat_id": chat_id, "text": text, "message_id": message_id})

    # ---- durable update queue -------------------------------------------- #
    def _enqueue_update(self, update: Dict[str, Any]) -> bool:
        """Queue one Telegram update. Returns False only on an enqueue error.

        A duplicate (same ``update_id``) is *not* an error: it means the update
        was already accepted before a reconnect, which is exactly what the queue
        is for.
        """
        try:
            if update.get("callback_query"):
                cq = update["callback_query"]
                chat_id = str((cq.get("message", {}).get("chat") or {}).get("id") or "")
                kind = "callback"
            else:
                msg = (update.get("message") or update.get("edited_message")
                       or update.get("channel_post") or {})
                if (msg.get("from") or {}).get("is_bot"):
                    return True
                chat_id = str((msg.get("chat") or {}).get("id") or "")
                kind = "message"
            if not chat_id:
                return True
            registry.enqueue_telegram_job(
                chat_id, kind, {"update": update},
                f"upd:{update.get('update_id')}",
            )
            return True
        except Exception:
            LOG.exception("could not enqueue update %s", update.get("update_id"))
            return False

    def _job_dispatcher(self) -> None:
        """Claim queued updates and run them off the polling thread.

        One job per chat at a time (enforced by ``claim_telegram_jobs``), several
        chats in parallel. A crash leaves the row ``running``; the next start
        re-queues it (``reset_telegram_jobs``) instead of dropping the update.
        """
        from concurrent.futures import ThreadPoolExecutor

        executor = ThreadPoolExecutor(max_workers=4,
                                      thread_name_prefix="perun-tg-job")
        try:
            while not self.stop_event.is_set():
                try:
                    jobs = registry.claim_telegram_jobs()
                except Exception:  # pragma: no cover - DB hiccup
                    LOG.exception("could not claim telegram jobs")
                    jobs = []
                for job in jobs:
                    executor.submit(self._run_job, job)
                self.stop_event.wait(0.5)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _run_job(self, job: Dict[str, Any]) -> None:
        update = (job.get("payload") or {}).get("update") or {}
        try:
            self._handle_update(update)
        except Exception as exc:
            LOG.exception("telegram job %s failed", job.get("id"))
            state = registry.fail_telegram_job(job["id"], str(exc))
            if state == "failed":
                registry.log_activity(
                    None, "telegram_failed",
                    f"telegram update {job.get('dedupe_key')} gave up after retries",
                    {"job_id": job["id"], "chat_id": job.get("chat_id"),
                     "error": str(exc)[:300]},
                )
            return
        registry.finish_telegram_job(job["id"])

    #: Telegram's native command menu (the ☰ button in the chat).  Kept short:
    #: the menu is for discovery, `/help` carries the full list.
    COMMANDS = [
        ("status", "Current project, session and queue"),
        ("pending", "What is waiting for you"),
        ("projects", "List projects and switch"),
        ("now", "Runs active right now"),
        ("new", "Start a fresh session"),
        ("stop", "Stop your running reply"),
        ("help", "All commands"),
    ]

    def _register_commands(self) -> None:
        """Publish the command menu; a failure is cosmetic, never fatal."""
        try:
            self.api("setMyCommands", commands=[
                {"command": name, "description": text}
                for name, text in self.COMMANDS
            ])
        except Exception:  # pragma: no cover - old server / offline
            LOG.debug("could not publish the Telegram command menu", exc_info=True)

    def run(self) -> None:
        threading.Thread(target=self.worker, daemon=True).start()
        try:
            recovered = registry.reset_telegram_jobs()
            if recovered:
                LOG.info("re-queued %d interrupted telegram update(s)", recovered)
            registry.prune_telegram_jobs()
        except Exception:  # pragma: no cover - startup must not depend on this
            LOG.exception("could not recover the telegram job queue")
        threading.Thread(target=self._job_dispatcher, daemon=True,
                         name="perun-tg-dispatcher").start()
        threading.Thread(target=self._reply_drainer, daemon=True,
                         name="perun-run-replies").start()
        boot = self.api("getUpdates", timeout=0, offset=-1)
        if boot.get("error_code") == 409:
            self._log_conflict(first=True)
        self._register_commands()
        results = boot.get("result") or []
        self.offset = (results[-1]["update_id"] + 1) if results else 0
        LOG.info("perun telegram bridge online (allow=%s)",
                 self.allowed or "<pair-on-first-message>")
        # Off the polling thread: a slow sendMessage must not delay updates.
        threading.Thread(
            target=self._notify_interrupted_runs, daemon=True,
            name="perun-interrupt-notice",
        ).start()
        # Operator inbox: notifier + OpenCode watcher. Started here because this
        # process owns the Telegram poller (one consumer per bot token), which is
        # also what makes the notifier a singleton.
        try:
            if config.Settings.load().attention_enabled:
                threading.Thread(
                    target=self._attention_loop, daemon=True,
                    name="perun-attention",
                ).start()
        except Exception:  # pragma: no cover - never block polling
            LOG.exception("could not start the attention loop")
        conflict_logged = 0.0
        while not self.stop_event.is_set():
            data = self.api("getUpdates", timeout=30, offset=self.offset)
            if not data.get("ok"):
                if data.get("error_code") == 409:
                    now = time.time()
                    if now - conflict_logged > 60.0:
                        conflict_logged = now
                        self._log_conflict()
                    if self.stop_event.wait(30.0):
                        break
                    continue
                if self.stop_event.wait(3.0):
                    break
                continue
            for update in data.get("result") or []:
                # Enqueue before advancing: a crash between the two re-delivers
                # the same update_id, which the unique dedupe key absorbs.
                if not self._enqueue_update(update):
                    break
                self.offset = update["update_id"] + 1

    def _log_conflict(self, *, first: bool = False) -> None:
        """Emit one actionable 409 message (then at most once per minute)."""
        LOG.error(
            "Telegram 409 Conflict: another process is polling this bot. Only ONE "
            "getUpdates consumer is allowed per bot token. Stop the other poller "
            "(e.g. trading_bot/scripts/telegram_bridge.py or the 'PerunTelegramBridge' "
            "scheduled task), or run the dashboard without --bridge. "
            "Backing off 30s.%s",
            " (occurred at startup)" if first else "",
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _setup_logging(log_path) -> None:
    config.ensure_dirs()
    LOG.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(str(log_path), maxBytes=2 * 1024 * 1024,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    LOG.handlers[:] = [fh, sh]


def run_bridge(orch: Orchestrator) -> int:
    settings = config.Settings.load()
    token, allowed = settings.telegram()
    _setup_logging(config.LOGS_DIR / "telegram.log")
    if not token:
        LOG.error("no Telegram token configured (set config.json telegram_token or "
                  "TELEGRAM_BOT_TOKEN)")
        return 2
    lock = PidLock(config.HOME / "telegram.pid")
    if not lock.acquire():
        LOG.error("another Perun bridge instance is running")
        return 2
    # Make sure the orchestrator exists so plain messages always have a target.
    try:
        control.ensure()
    except Exception:  # pragma: no cover - best-effort
        LOG.exception("could not ensure the orchestrator project")
    # Without an allow-list the bridge pairs *nobody* automatically: it mints a
    # one-time code, shows it in the dashboard, and only ``/pair <code>`` from
    # Telegram grants access (persisted to config.json).
    if allowed:
        if settings.telegram_pair_code:
            settings.telegram_pair_code = ""
            settings.save()
    elif not settings.telegram_pair_code:
        settings.telegram_pair_code = os.urandom(4).hex()
        settings.save()
        LOG.warning("no allowed chats configured — pair with /pair %s "
                    "(also shown in the dashboard under Telegram)",
                    settings.telegram_pair_code)
    bridge = PerunTelegramBridge(orch, token=token, allowed=allowed)

    def _shutdown(signum, _frame):
        LOG.info("shutting down (signal %s)", signum)
        bridge.stop_event.set()
        for uid in bridge.active_runs():
            orch.cancel(uid)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass
    try:
        bridge.run()
    except KeyboardInterrupt:
        bridge.stop_event.set()
    finally:
        try:
            bridge.client.close()
        except Exception:  # pragma: no cover - best-effort
            pass
        lock.release()
        LOG.info("bridge stopped")
    return 0
