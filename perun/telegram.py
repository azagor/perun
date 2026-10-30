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

LOG = logging.getLogger("perun-telegram")
TELEGRAM_MAX = 4096
TYPING_INTERVAL = 4.0


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_html(md: str) -> str:
    md = md.replace("\r\n", "\n")
    blocks: List[tuple] = []

    def _stash_block(m: "re.Match[str]") -> str:
        blocks.append(((m.group(1) or "").strip(), m.group(2).rstrip("\n")))
        return f"\x00CB{len(blocks) - 1}\x00"

    md = re.sub(r"```([a-zA-Z0-9_+\-]*)\n?(.*?)```", _stash_block, md, flags=re.S)
    inline: List[str] = []

    def _stash_inline(m: "re.Match[str]") -> str:
        inline.append(m.group(1))
        return f"\x00IC{len(inline) - 1}\x00"

    md = re.sub(r"`([^`\n]+)`", _stash_inline, md)
    md = _esc(md)
    md = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", md, flags=re.S)
    md = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", md, flags=re.S)
    md = re.sub(r"^#{1,6}\s*(.+?)\s*$", r"<b>\1</b>", md, flags=re.M)
    md = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', md)
    for i, c in enumerate(inline):
        md = md.replace(f"\x00IC{i}\x00", f"<code>{_esc(c)}</code>")
    for i, (lang, code) in enumerate(blocks):
        cls = f' class="language-{lang}"' if lang else ""
        md = md.replace(f"\x00CB{i}\x00", f"<pre><code{cls}>{_esc(code)}</code></pre>")
    return md


def chunk(text: str, size: int) -> List[str]:
    if len(text) <= size:
        return [text]
    out: List[str] = []
    rest = text
    while len(rest) > size:
        window = rest[:size]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut < size // 2:
            cut = size
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        out.append(rest)
    return out


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
        for attempt in range(1, retries + 1):
            try:
                data = self.client.post(url, json=params).json()
                if data.get("ok"):
                    return data
                if data.get("error_code") == 409:
                    # Handled (and de-spammed) by the polling loop.
                    return data
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
             reply_markup: Optional[Dict[str, Any]] = None) -> None:
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
                payload.pop("parse_mode", None)
                payload.pop("reply_markup", None)
                payload["text"] = piece
                self.api("sendMessage", **payload)
            reply_to = None
            reply_markup = None

    def _typing_loop(self, chat_id: str, done: threading.Event) -> None:
        while not done.wait(TYPING_INTERVAL):
            self.api("sendChatAction", chat_id=chat_id, action="typing")

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
            buttons.append([{"text": label, "callback_data": payload}])
        return {"inline_keyboard": buttons}

    def _handle_callback(self, update: Dict[str, Any]) -> None:
        """Handle inline-keyboard button taps for project selection."""
        cq = update.get("callback_query") or {}
        data = (cq.get("data") or "").strip()
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

    def _handle_text_command(self, chat_id: str, text: str, message_id: int,
                             *, project_override: str | None = None) -> bool:
        parts = text.strip().split()
        if not parts:
            return True
        root = parts[0].lower().split("@")[0]
        if root in ("/start", "/help"):
            self.send(chat_id,
                      "🤖 *Perun bridge*\n\n"
                      "Talk to Perun itself or to any of your OpenCode projects.\n\n"
                      "*Commands*\n"
                      "`/orchestrator` — talk to Perun (manages all projects)\n"
                      "`/projects` — list projects\n"
                      "`/project <name|#>` — switch project\n"
                      "`/status` — current project + session\n"
                      "`/new` — fresh session\n"
                      "`/stop` — cancel *your* running reply\n"
                      "`/remember <fact>` — store to memory\n"
                      "`/recall <query>` — search memory\n"
                      "`/index` — re-index project docs\n"
                      "`/pair <code>` — authorise this chat (code from the dashboard)\n\n"
                      "With no project selected, messages go to the orchestrator.",
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
                      f"*Perun bridge*\n"
                      f"project: `{proj['slug'] if proj else '(none)'}`\n"
                      f"session: `{link.get('session_pk') or '(new)'}`\n"
                      f"queue: {self.work.qsize()}\n"
                      f"uptime: {up // 3600}h {(up % 3600) // 60}m",
                      reply_to=message_id)
            return True
        if root in ("/stop", "/cancel"):
            uid = self.run_for(chat_id)
            if uid and self.orch.cancel(uid):
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
        try:
            run = self.orch.start_run(proj["slug"], text, session_pk=session_pk,
                                      opencode_session_id=oc_sid)
        except Exception as exc:
            typing_done.set()
            self.send(chat_id, f"❌ Could not start run: {exc}", reply_to=message_id)
            return
        uid = run["run_uid"]
        with self._runs_lock:
            self._runs[chat_id] = uid
        try:
            while not self.orch.is_final(uid):
                if self.stop_event.is_set():
                    break
                time.sleep(1.0)
        finally:
            typing_done.set()
            with self._runs_lock:
                self._runs.pop(chat_id, None)
        final = registry.get_run(uid) or run
        reply = (final.get("output") or "").strip()
        new_session_pk = final.get("session_pk")
        if new_session_pk:
            registry.set_bridge_link(chat_id, proj["id"], new_session_pk)
        if final.get("status") == "cancelled":
            self.send(chat_id, "🛑 Run cancelled.", reply_to=message_id)
            return
        if not reply:
            err = (final.get("error") or "no reply").strip()[-1500:]
            self.send(chat_id, f"❌ Agent {final.get('status')}.\n\n{err}", reply_to=message_id)
            return
        self.send(chat_id, reply, reply_to=message_id)

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
        if self.handle_command(chat_id, text, message_id):
            return
        self.work.put({"chat_id": chat_id, "text": text, "message_id": message_id})

    def run(self) -> None:
        threading.Thread(target=self.worker, daemon=True).start()
        boot = self.api("getUpdates", timeout=0, offset=-1)
        if boot.get("error_code") == 409:
            self._log_conflict(first=True)
        results = boot.get("result") or []
        self.offset = (results[-1]["update_id"] + 1) if results else 0
        LOG.info("perun telegram bridge online (allow=%s)",
                 self.allowed or "<pair-on-first-message>")
        # Off the polling thread: a slow sendMessage must not delay updates.
        threading.Thread(
            target=self._notify_interrupted_runs, daemon=True,
            name="perun-interrupt-notice",
        ).start()
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
                self.offset = update["update_id"] + 1
                try:
                    self._handle_update(update)
                except Exception:
                    LOG.exception("failed to handle update %s", update.get("update_id"))

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
