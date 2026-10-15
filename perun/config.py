"""Perun configuration: filesystem layout, defaults, and persisted settings.

Runtime state lives outside the repository (default ``~/.perun``) so it is
shared across every project and survives the repo being moved. The code lives
in the repository (``C:\\my_apps\\perun``).

Layout (all under ``PERUN_HOME``):

    perun.db          registry + unified memory + vector chunks (SQLite, WAL)
    config.json       user settings (roots, host/port, telegram, ...)
    logs/             service + bridge logs
    runs/             raw stdout/stderr of each agent run
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent


def _resolve_home() -> Path:
    env = os.environ.get("PERUN_HOME")
    if env and env.strip():
        return Path(env).expanduser().resolve()
    return (Path.home() / ".perun").resolve()


HOME: Path = _resolve_home()
DB_PATH: Path = HOME / "perun.db"
CONFIG_PATH: Path = HOME / "config.json"
LOGS_DIR: Path = HOME / "logs"
RUNS_DIR: Path = HOME / "runs"
WEB_DIR: Path = REPO_ROOT / "web"
MCP_SERVER: Path = REPO_ROOT / "mcp" / "perun_mcp.py"
TEMPLATES_DIR: Path = PACKAGE_DIR / "integrations" / "templates"
OPENCODE_CONFIG_DIR: Path = Path.home() / ".config" / "opencode"
GLOBAL_SKILLS_DIR: Path = OPENCODE_CONFIG_DIR / "skills"
PROJECT_SKILLS_DIR: Path = Path(".opencode") / "skills"


def is_control_path(path: "str | Path | None") -> bool:
    """True when *path* is the Perun repository itself.

    The control plane (see :mod:`perun.control`) is the agent session whose
    working directory is the Perun repo. It is identified by that path so it can
    never be confused with — or deleted as — a user project, and so it survives
    even when the registry is emptied.
    """
    if not path:
        return False
    try:
        return (os.path.normcase(os.path.abspath(str(path)))
                == os.path.normcase(os.path.abspath(str(REPO_ROOT))))
    except Exception:  # pragma: no cover - defensive
        return False


def _default_roots() -> list[str]:
    env = os.environ.get("PERUN_ROOTS")
    if env and env.strip():
        return [p.strip() for p in env.split(os.pathsep) if p.strip()]
    candidates: list[Path] = []
    if os.name == "nt":
        candidates.append(Path("C:/my_apps"))
    candidates.extend([Path.home() / "projects", Path.home() / "src", Path.home() / "code"])
    roots = [str(p) for p in candidates if p.exists()]
    return roots or [str(Path.home())]


@dataclass
class Settings:
    """User-tunable Perun settings (persisted to ``config.json``)."""

    roots: list[str] = field(default_factory=_default_roots)
    host: str = "127.0.0.1"
    port: int = 8760
    default_model: str = ""
    default_agent: str = ""
    orchestrator_model: str = ""
    orchestrator_agent: str = ""
    orchestrator_instructions: str = ""
    max_concurrent_runs: int = 3
    run_timeout: int = 1800
    ollama_host: str = "http://127.0.0.1:11434"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    telegram_token: str = ""
    telegram_bot_name: str = ""
    telegram_allowed_chats: list[str] = field(default_factory=list)
    auto_discover_on_start: bool = True
    index_docs_on_discover: bool = False
    # Dedicated terminal / command shell. On Windows the value can be a path to
    # powershell.exe, pwsh.exe, wt.exe, cmd.exe, or any other terminal; on other
    # platforms it can be a shell binary. A blank value lets Perun use the OS
    # default (cmd.exe / powershell on Windows, the user's $SHELL elsewhere).
    terminal: str = ""

    @classmethod
    def load(cls) -> "Settings":
        data: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        known = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in data.items() if k in known}
        return cls(**clean)

    def save(self) -> None:
        ensure_dirs()
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # -- Telegram resolution (settings first, environment as fallback) -------

    def telegram(self) -> tuple[str, list[str]]:
        token = (self.telegram_token or os.environ.get("TELEGRAM_BOT_TOKEN")
                 or os.environ.get("TG_BOT_TOKEN") or "").strip()
        chats = list(self.telegram_allowed_chats)
        if not chats:
            env_chats = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID") or ""
            chats = [c.strip() for c in env_chats.split(",") if c.strip()]
        return token, chats

    def terminal_cmd(self, shell_hint: str = "") -> list[str]:
        """Return the argv to start the user's preferred terminal emulator.

        The setting is a path or alias. On Windows, common choices are
        ``pwsh.exe``, ``powershell.exe``, ``wt.exe``, ``cmd.exe``. We try the
        configured value first, then fall back to a sensible default. The result
        is an argv list ready for ``subprocess.Popen``.

        ``shell_hint`` lets callers request a specific shell class; otherwise
        the configured/default shell is used.
        """
        configured = (self.terminal or "").strip()
        hint = (shell_hint or configured).lower()
        if configured and not shell_hint:
            low = configured.lower()
            base = os.path.basename(configured).lower()
            if low.endswith("wt.exe") or base == "wt.exe":
                return [configured, "-d", "."]
            if base in ("cmd", "cmd.exe"):
                return [configured]
            if base in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
                return [configured, "-NoExit", "-Command"]
            return [configured]
        if os.name == "nt":
            if hint in ("cmd", "cmd.exe"):
                path = shutil.which("cmd") or shutil.which("cmd.exe")
                return [path] if path else ["cmd.exe"]
            if hint in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
                for candidate in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
                    path = shutil.which(candidate)
                    if path:
                        return [path, "-NoExit", "-Command"]
            if hint in ("wt", "wt.exe"):
                path = shutil.which("wt") or shutil.which("wt.exe")
                if path:
                    return [path, "-d", "."]
            # Default: PowerShell Preview first, then Windows PowerShell.
            for candidate in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
                path = shutil.which(candidate)
                if path:
                    return [path, "-NoExit", "-Command"]
            path = shutil.which("cmd") or shutil.which("cmd.exe")
            if path:
                return [path]
            return ["cmd.exe"]
        for candidate in (os.environ.get("SHELL", ""), "bash", "zsh", "sh"):
            if candidate and shutil.which(candidate):
                return [candidate]
        return ["sh"]

    def terminal_sources(self) -> dict[str, str]:
        """Where the effective terminal comes from: ``settings`` | ``os`` | ``""``."""
        if (self.terminal or "").strip():
            return {"source": "settings", "path": self.terminal.strip()}
        if os.name == "nt":
            return {"source": "os", "path": shutil.which("pwsh") or shutil.which("pwsh.exe") or shutil.which("powershell") or shutil.which("powershell.exe") or shutil.which("cmd") or shutil.which("cmd.exe") or ""}
        return {"source": "os", "path": os.environ.get("SHELL", shutil.which("bash") or shutil.which("sh") or "")}

    def telegram_sources(self) -> dict[str, str]:
        """Where the *effective* token/chats come from: ``settings`` | ``env`` | ``""``.

        ``Settings.telegram()`` prefers ``config.json`` and falls back to
        environment variables, so the dashboard has to be able to say which one
        is actually in force — otherwise "Disconnect" looks broken when a token
        is shadowing it from the environment.
        """
        if (self.telegram_token or "").strip():
            token_src = "settings"
        elif (os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or "").strip():
            token_src = "env"
        else:
            token_src = ""

        if self.telegram_allowed_chats:
            chat_src = "settings"
        elif (os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID") or "").strip():
            chat_src = "env"
        else:
            chat_src = ""
        return {"token": token_src, "chats": chat_src}


def ensure_dirs() -> None:
    for d in (HOME, LOGS_DIR, RUNS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def settings() -> Settings:
    """Load settings (fresh) — convenience for short-lived callers."""
    return Settings.load()
