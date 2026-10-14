"""Shared Telegram ⇄ Perun bridge with per-project routing.

A single long-polling process serves *every* project. Incoming messages are
routed to the chat's currently-selected project and answered by an OpenCode
session started through the Perun orchestrator.

Commands
--------
``/start`` ``/help``     Help.
``/projects``            List registered projects (numbered).
``/project <name|#>``    Select the project to talk to.
``/new`` ``/reset``      Start a fresh OpenCode session for this chat.
``/status``              Bridge + current project status.
``/stop``                Cancel the in-flight agent run.
``/remember <fact>``     Store a fact (project scope).
``/recall <query>``      Search the unified memory store.
``/index``               Re-index the current project's docs.

Environment / settings
----------------------
Token + allow-list come from ``Settings.telegram()`` (config.json first, then
``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``). State (chat→project→session) is
persisted in Perun's database, not a side file.
"""

from __future__ import annotations

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

from . import config, memory, registry
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
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip())
            except Exception:
                pid = 0
            if pid and _pid_alive(pid):
                return False
        try:
            self.path.write_text(str(os.getpid()), "utf-8")
        except OSError:
            return False
        self.acquired = True
        return True

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
        self.current_run: Optional[str] = None
        self.current_chat: Optional[str] = None
        self.started_at = time.time()
        self._allowed_runtime: List[str] = []
        self.offset: Optional[int] = None
        self._state_lock = threading.Lock()

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

    def send(self, chat_id: str, text: str, *, reply_to: Optional[int] = None) -> None:
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
            res = self.api("sendMessage", **payload)
            if not res.get("ok"):
                payload.pop("parse_mode", None)
                payload["text"] = piece
                self.api("sendMessage", **payload)
            reply_to = None

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
        projects = registry.list_projects()
        if len(projects) == 1:
            registry.set_bridge_link(chat_id, projects[0]["id"], link.get("session_pk"))
            return projects[0]
        return None

    # ---- commands ---- #
    def handle_command(self, chat_id: str, text: str, message_id: int) -> bool:
        parts = text.strip().split()
        if not parts:
            return True
        root = parts[0].lower().split("@")[0]
        if root in ("/start", "/help"):
            self.send(chat_id,
                      "🤖 *Perun bridge*\n\n"
                      "Talk to any of your OpenCode projects from here.\n\n"
                      "*Commands*\n"
                      "`/projects` — list projects\n"
                      "`/project <name|#>` — switch project\n"
                      "`/status` — current project + session\n"
                      "`/new` — fresh session\n"
                      "`/stop` — cancel the running reply\n"
                      "`/remember <fact>` — store to memory\n"
                      "`/recall <query>` — search memory\n"
                      "`/index` — re-index project docs",
                      reply_to=message_id)
            return True
        if root == "/projects":
            projects = registry.list_projects()
            if not projects:
                self.send(chat_id, "No projects registered yet.", reply_to=message_id)
                return True
            lines = ["*Projects*"]
            for i, p in enumerate(projects, 1):
                mark = "▸" if self._is_current(chat_id, p) else " "
                lines.append(f"{mark} {i}. `{p['slug']}` — {p['name']} ({p['kind']})")
            self.send(chat_id, "\n".join(lines), reply_to=message_id)
            return True
        if root == "/project":
            projects = registry.list_projects()
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
            proj = self._current_project(chat_id)
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
            uid = self.current_run
            if uid and self.orch.cancel(uid):
                self.send(chat_id, "🛑 Cancelling…", reply_to=message_id)
            else:
                self.send(chat_id, "Nothing is running.", reply_to=message_id)
            return True
        if root == "/remember":
            proj = self._current_project(chat_id)
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
            proj = self._current_project(chat_id)
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
        link = self._chat_state(chat_id)
        if link.get("project_id") == project["id"]:
            return True
        projs = registry.list_projects()
        return len(projs) == 1 and projs[0]["id"] == project["id"]

    # ---- run a message ---- #
    def run_message(self, chat_id: str, text: str, message_id: int) -> None:
        proj = self._current_project(chat_id)
        if not proj:
            if not registry.list_projects():
                self.send(chat_id, "No projects registered yet. Run "
                                   "`python -m perun discover` (or open the "
                                   "dashboard) to add one, then try again.",
                          reply_to=message_id)
            else:
                self.send(chat_id, "Multiple projects registered — pick one with "
                                   "`/project <name>` (`/projects` to list).",
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
        self.current_run = uid
        self.current_chat = chat_id
        try:
            while not self.orch.is_final(uid):
                if self.stop_event.is_set():
                    break
                time.sleep(1.0)
        finally:
            typing_done.set()
            self.current_run = None
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
        if self.allowed:
            return chat_id in self.allowed
        if chat_id in self._allowed_runtime:
            return True
        if not self._allowed_runtime:
            self._allowed_runtime.append(chat_id)
            LOG.info("paired new chat %s", chat_id)
            return True
        return False

    def _handle_update(self, update: Dict[str, Any]) -> None:
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
        if not self.authorised(chat_id):
            self.send(chat_id, "⛔ Not authorised.")
            return
        message_id = int(msg.get("message_id") or 0)
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
    bridge = PerunTelegramBridge(orch, token=token, allowed=allowed)

    def _shutdown(signum, _frame):
        LOG.info("shutting down (signal %s)", signum)
        bridge.stop_event.set()
        uid = bridge.current_run
        if uid:
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
        lock.release()
        LOG.info("bridge stopped")
    return 0
