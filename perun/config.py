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


def _resolve_opencode_config_dir() -> Path:
    """The OpenCode config directory, honouring ``XDG_CONFIG_HOME``.

    The global installer already respects ``XDG_CONFIG_HOME``; the runtime must
    agree with it or installation and discovery would look at different trees.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg and xdg.strip() else (Path.home() / ".config")
    return (base / "opencode").resolve()


HOME: Path = _resolve_home()
DB_PATH: Path = HOME / "perun.db"
CONFIG_PATH: Path = HOME / "config.json"
API_TOKEN_PATH: Path = HOME / "api-token"
LOGS_DIR: Path = HOME / "logs"
RUNS_DIR: Path = HOME / "runs"
WEB_DIR: Path = REPO_ROOT / "web"
MCP_SERVER: Path = REPO_ROOT / "mcp" / "perun_mcp.py"
TEMPLATES_DIR: Path = PACKAGE_DIR / "integrations" / "templates"
OPENCODE_CONFIG_DIR: Path = _resolve_opencode_config_dir()
GLOBAL_SKILLS_DIR: Path = OPENCODE_CONFIG_DIR / "skills"
PROJECT_SKILLS_DIR: Path = Path(".opencode") / "skills"
# Global instruction Markdown files ride alongside the OpenCode config so they
# are visible to OpenCode V2's ambient instruction loading and easy to edit by hand.
INSTRUCTIONS_DIR: Path = OPENCODE_CONFIG_DIR / "perun" / "instructions"

# POSIX terminal emulators, in preference order, with the flags that precede the
# program to run. Append the shell and ``-c <command>`` and every emulator below
# launches it in a real TTY (a bare ``bash -c`` has no terminal at all).
POSIX_TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("kitty", ("--",)),
    ("wezterm", ("start", "--")),
    ("ghostty", ("-e",)),
    ("alacritty", ("-e",)),
    ("foot", ()),
    ("konsole", ("-e",)),
    ("gnome-terminal", ("--",)),
    ("xfce4-terminal", ("-e",)),
    ("xterm", ("-e",)),
)


def posix_terminal_flags(name: str) -> tuple[str, ...] | None:
    """Flags for a known POSIX terminal emulator, else ``None``."""
    base = os.path.basename((name or "").strip()).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    for emu, flags in POSIX_TERMINALS:
        if base == emu:
            return flags
    return None


def posix_shell() -> str:
    """The user's login shell (``$SHELL``) or the first one that exists."""
    for candidate in (os.environ.get("SHELL", ""), "bash", "zsh", "sh"):
        if candidate and shutil.which(candidate):
            return candidate
    return "sh"


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
    max_concurrent_runs: int = 3
    run_timeout: int = 1800
    ollama_host: str = "http://127.0.0.1:11434"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    # Deterministic router (`perun/routing.py`): resolve a project and an action
    # from free text before spending an agent run. Off by default so an upgrade
    # never changes how existing chats behave until the operator opts in.
    routing_enabled: bool = False
    # Log-only mode: record the decision as if routing were live, then take the
    # previous path anyway. Lets an operator (or the product) collect real
    # decisions for a week before trusting them.
    routing_shadow: bool = False
    telegram_token: str = ""
    telegram_bot_name: str = ""
    telegram_allowed_chats: list[str] = field(default_factory=list)
    # One-time pairing code used when ``telegram_allowed_chats`` is empty. The
    # bridge generates it at startup, shows it in the dashboard, and consumes it
    # (clears it) as soon as a chat completes ``/pair <code>``. An empty
    # allow-list therefore grants *nobody* access by default.
    telegram_pair_code: str = ""
    # How far back the bridge looks for runs a restart interrupted, when it
    # notifies the bound chat on startup (minutes).
    telegram_interrupt_notice_minutes: int = 60

    # --- Operator inbox --------------------------------------------------- #
    # When enabled, anything that needs a human decision (a permission request,
    # a form, an explicit perun_ask_operator, a proposal, a handoff) is pushed to
    # every statically paired Telegram chat, and the reply is delivered back to
    # that exact context. Off by default; see docs/TECHNICAL_DOCUMENTATION.md.
    attention_enabled: bool = False
    # Log-only mode: record what would be sent, send nothing.
    attention_shadow: bool = True
    # Project slugs allowed to raise items. Empty means every non-control
    # project.
    attention_projects: list[str] = field(default_factory=list)
    # Names that count as "addressed to the operator" in a project room.
    attention_operator_names: list[str] = field(default_factory=list)
    # How long an unanswered item stays open before it expires (minutes).
    attention_ttl_minutes: int = 240
    # How often the OpenCode watcher polls for permission/form requests (seconds).
    attention_poll_seconds: int = 5
    # On startup, re-send only open items that were never delivered and are
    # younger than this (minutes). Older ones stay in /pending and the dashboard.
    attention_replay_open_minutes: int = 15
    # Per-project cap on notices per hour; excess is skipped, not queued.
    attention_max_per_hour: int = 6
    # Defer notices inside this local-time window, e.g. "22:00-07:00".
    attention_quiet_hours: str = ""
    # Show absolute paths and full commands in notices (off = project-relative).
    attention_show_paths: bool = False
    # When exactly one item is open, offer a one-tap "send to att_N" instead of
    # silently treating plain text as the answer.
    attention_plain_text_confirm: bool = True
    # Default delivery for session answers: "queue" (after the current turn) or
    # "steer" (interrupt now).
    attention_default_delivery: str = "queue"

    # --- Durable runs ------------------------------------------------------ #
    # A run is an idempotent prompt admitted to an OpenCode session, observed via
    # the service's event stream, so it survives a Perun restart. Set false to
    # fall back to the legacy `opencode run` child process.
    session_runner_enabled: bool = True
    # On startup, re-admit a run that never reached the session if it is younger
    # than this (minutes); older ones are marked interrupted.
    run_resume_window_minutes: int = 30
    # A standalone TUI run (an external terminal) is kept "working" across a
    # restart; only one older than this is assumed closed and swept (hours).
    tui_run_stale_hours: int = 24
    # PerunChat federation credentials. The API key is obtained from the company
    # website at peruninthemachine.com; the endpoint is the WebSocket ingress URL.
    perunchat_api_key: str = ""
    perunchat_api_endpoint: str = "wss://peruninthemachine.com/v1/chat/ws"
    # Autonomous agent-chat bridge. When enabled and a key is configured, the
    # local Orchestrator joins the listed rooms and replies to incoming messages.
    perunchat_auto_reply: bool = False
    perunchat_auto_join: list[str] = field(default_factory=lambda: ["pub:global:general"])
    perunchat_chat_model: str = ""
    # OpenCode agent used for room replies. Defaults to the dedicated
    # "perunchat" agent (a chat participant persona with no tools) instead of
    # the coding-orchestrator persona.
    perunchat_chat_agent: str = "perunchat"
    # Trusted PerunChat participants. Perun may reveal internal/private context
    # (project names, file paths, tools) only in private/group rooms with agents
    # on this list. Public rooms never receive internal context regardless of trust.
    perunchat_trusted: list[str] = field(default_factory=list)
    # Display name used when the local operator approves or rejects shared-project
    # action proposals. Falls back to the OS username, then "owner".
    perun_owner_name: str = ""
    # Signing secret for the local HITL approval-token engine. On a single
    # machine it defaults to a hash of PERUN_HOME; for multi-node deployments
    # set the same value everywhere via PERUN_HITL_SECRET or this setting.
    perun_hitl_secret: str = ""
    # Autonomous shared-project mode. When true, action proposals created by
    # chat agents are approved and executed automatically without waiting for a
    # human click. Humans can still approve or execute proposals manually from
    # the dashboard. The per-project manifest flag ``shared.auto_approve`` overrides
    # this global default.
    shared_auto_approve: bool = False
    auto_discover_on_start: bool = True
    index_docs_on_discover: bool = False
    memory_prefetch: str = "rules"
    # Dedicated terminal / command shell. On Windows the value can be a path to
    # powershell.exe, pwsh.exe, wt.exe, cmd.exe, or any other terminal; on other
    # platforms it can be a shell binary. A blank value lets Perun use the OS
    # default (cmd.exe / powershell on Windows, the user's $SHELL elsewhere).
    terminal: str = ""
    # External execution handoff: a rw participant may claim an approved
    # proposal and upload a ZIP package of file changes over PerunChat.
    external_execution_claim_ttl: int = 3600
    # A partial package upload slot is reclaimed after this much inactivity
    # (accepted chunks refresh the deadline). The bridge enforces a 60 s floor.
    external_execution_package_ttl: int = 30 * 60
    external_execution_max_package_bytes: int = 50 * 1024 * 1024
    external_execution_max_entry_bytes: int = 10 * 1024 * 1024
    external_execution_max_entries: int = 500
    external_execution_chunk_bytes: int = 360 * 1024

    @classmethod
    def load(cls) -> "Settings":
        data: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                # Never let a later save() silently destroy a hand-edited or
                # corrupted config: move it aside first, then carry on with
                # defaults. The leftover file is the user's data.
                _quarantine_config()
                data = {}
        if not isinstance(data, dict):
            _quarantine_config()
            data = {}
        known = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in data.items() if k in known}
        return cls(**clean)

    def save(self) -> None:
        ensure_dirs()
        payload = json.dumps(asdict(self), indent=2)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        # Create the temporary file owner-only *before* the token is written, so
        # there is never a window where the secret is world-readable, then swap
        # it in atomically (a crash mid-write cannot truncate the real file).
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        except OSError:  # pragma: no cover - unusual filesystem, best effort
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, CONFIG_PATH)
            return
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, CONFIG_PATH)
        # Restrict read access to the owner on POSIX systems.
        if os.name != "nt":
            try:
                import stat
                os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:  # pragma: no cover
                pass

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # -- Telegram resolution (settings first, environment as fallback) -------

    def telegram(self) -> tuple[str, list[str]]:
        from . import secrets
        # The orchestrator injects the already-resolved token into agent
        # subprocesses so the MCP server can send messages without inheriting
        # raw secrets (TELEGRAM_BOT_TOKEN / TG_BOT_TOKEN / PERUN_PASSWORD).
        env_token = (os.environ.get("PERUN_TELEGRAM_TOKEN") or "").strip()
        if env_token:
            token = env_token
        else:
            config_token = (self.telegram_token or "").strip()
            # Decrypt only a config-stored token, never an environment fallback.
            if config_token and secrets.is_encrypted(config_token):
                token = secrets.decrypt_value(config_token) or ""
            else:
                token = config_token or os.environ.get("TELEGRAM_BOT_TOKEN") \
                        or os.environ.get("TG_BOT_TOKEN") or ""
        chats = list(self.telegram_allowed_chats)
        if not chats:
            env_chats = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID") or ""
            chats = [c.strip() for c in env_chats.split(",") if c.strip()]
        return token.strip(), chats

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
        if configured and not shell_hint and os.name == "nt":
            base = os.path.basename(configured).lower()
            if base == "wt.exe":
                return [configured, "-d", "."]
            if base in ("cmd", "cmd.exe"):
                return [configured]
            if base in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
                return [configured, "-NoExit", "-Command"]
            return [configured]
        if os.name == "nt":
            hint_base = os.path.basename(hint)
            if hint_base in ("cmd", "cmd.exe"):
                path = shutil.which("cmd") or shutil.which("cmd.exe")
                return [path] if path else ["cmd.exe"]
            if hint_base in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
                for candidate in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
                    path = shutil.which(candidate)
                    if path:
                        return [path, "-NoExit", "-Command"]
            if hint_base in ("wt", "wt.exe"):
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
        # POSIX (Linux/macOS). A bare shell has no TTY, so prefer a real
        # terminal emulator; the returned argv ends with the shell, ready for
        # callers that append ``["-c", command]``.
        shell = posix_shell()
        requested = (configured or hint).strip()
        if requested:
            flags = posix_terminal_flags(requested)
            if flags is not None:
                return [requested, *flags, shell]
            if shutil.which(requested) or os.path.sep in requested:
                # An explicit shell/shell-like program.
                return [requested]
        if os.name != "nt":
            for emu, flags in POSIX_TERMINALS:
                path = shutil.which(emu)
                if path:
                    return [path, *flags, shell]
        return [shell]

    def terminal_sources(self) -> dict[str, str]:
        """Where the effective terminal comes from: ``settings`` | ``os`` | ``""``."""
        if (self.terminal or "").strip():
            return {"source": "settings", "path": self.terminal.strip()}
        if os.name == "nt":
            return {"source": "os", "path": shutil.which("pwsh") or shutil.which("pwsh.exe") or shutil.which("powershell") or shutil.which("powershell.exe") or shutil.which("cmd") or shutil.which("cmd.exe") or ""}
        for emu, _flags in POSIX_TERMINALS:
            path = shutil.which(emu)
            if path:
                return {"source": "os", "path": path}
        return {"source": "os", "path": os.environ.get("SHELL", shutil.which("bash") or shutil.which("sh") or "")}

    def telegram_sources(self) -> dict[str, str]:
        """Where the *effective* token/chats come from: ``settings`` | ``settings-encrypted`` | ``env`` | ``""``.

        ``Settings.telegram()`` prefers ``config.json`` and falls back to
        environment variables, so the dashboard has to be able to say which one
        is actually in force — otherwise "Disconnect" looks broken when a token
        is shadowing it from the environment.
        """
        from . import secrets
        if (os.environ.get("PERUN_TELEGRAM_TOKEN") or "").strip():
            token_src = "orchestrator"
        elif (self.telegram_token or "").strip():
            if secrets.is_encrypted(self.telegram_token):
                token_src = "settings-encrypted"
            else:
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

    # -- PerunChat resolution (settings first, environment as fallback) ------

    def perunchat(self) -> tuple[str, str]:
        """Return the effective PerunChat API key and WebSocket endpoint."""
        from . import secrets
        env_key = (os.environ.get("PERUNCHAT_API_KEY") or "").strip()
        key = env_key
        if not key:
            config_key = (self.perunchat_api_key or "").strip()
            if config_key and secrets.is_encrypted(config_key):
                key = secrets.decrypt_value(config_key) or ""
            else:
                key = config_key
        endpoint = (os.environ.get("PERUNCHAT_API_ENDPOINT") or self.perunchat_api_endpoint or "wss://peruninthemachine.com/v1/chat/ws").strip()
        return key, endpoint

    def perunchat_sources(self) -> dict[str, str]:
        """Where the effective PerunChat key/endpoint come from."""
        from . import secrets
        if (os.environ.get("PERUNCHAT_API_KEY") or "").strip():
            key_src = "env"
        elif (self.perunchat_api_key or "").strip():
            if secrets.is_encrypted(self.perunchat_api_key):
                key_src = "settings-encrypted"
            else:
                key_src = "settings"
        else:
            key_src = ""
        endpoint_src = "env" if (os.environ.get("PERUNCHAT_API_ENDPOINT") or "").strip() else "settings"
        return {"key": key_src, "endpoint": endpoint_src}


PUBLIC_GATEWAY_HOSTS = ("peruninthemachine.com", "www.peruninthemachine.com")


def perunchat_rest_base(endpoint: str) -> str:
    """Derive the PerunChat REST base URL from a WebSocket endpoint.

    The public deployment exposes the REST API under ``/chat/api/v1`` and the
    WebSocket under ``/v1/chat/ws``. Local/dev gateways expose both under the
    same origin with the REST API at ``/api/v1``. Returns ``""`` when the
    endpoint has no usable scheme or host.

    This lives here, next to the endpoint resolution, because the bridge, the
    HTTP proxy and the shared-project sync must all derive the *same* base.
    Diverging copies were what made shared-project chat creation fail against
    the public gateway.
    """
    from urllib.parse import urlparse

    raw = (endpoint or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        scheme = parsed.scheme.replace("wss", "https").replace("ws", "http")
        if not scheme or not parsed.hostname:
            return ""
        base = f"{scheme}://{parsed.netloc}"
        if parsed.hostname.lower() in PUBLIC_GATEWAY_HOSTS:
            return f"{base}/chat"
        return base
    except Exception:
        return ""


def perunchat_actor_path(room_id: str) -> Path:
    """Per-room file holding the current PerunChat actor for proposal tools.

    Project-local MCP servers get a static environment, so the bridge cannot
    pass the per-message sender through ``opencode.json``. It writes this file
    before a run instead; ``proposals_mcp`` reads it when
    ``PERUN_CHAT_SENDER`` is absent.
    """
    import hashlib

    digest = hashlib.sha256(str(room_id).encode("utf-8")).hexdigest()[:32]
    return HOME / "perunchat_actors" / f"{digest}.json"


def ensure_dirs() -> None:
    for d in (HOME, LOGS_DIR, RUNS_DIR, INSTRUCTIONS_DIR,
              HOME / "perunchat_actors"):
        d.mkdir(parents=True, exist_ok=True)


def _quarantine_config() -> Path | None:
    """Rename an unreadable ``config.json`` aside so it is never overwritten.

    ``Settings.load()`` falls back to defaults on a parse error; without this the
    next ``save()`` would silently replace the user's file (and its Telegram
    token) with a minimal one.
    """
    try:
        import time
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = CONFIG_PATH.with_suffix(f".json.corrupt-{stamp}")
        os.replace(CONFIG_PATH, dest)
        return dest
    except OSError:  # pragma: no cover - defensive
        return None


def api_token() -> str:
    """Return the local API token, creating it on first use.

    The dashboard has no user accounts, so the token is what stands between
    "any local process / any script" and *running arbitrary agent commands*.
    It is created atomically with owner-only permissions (``0600``) and lives in
    ``PERUN_HOME``; the dashboard receives it injected into ``index.html`` and
    CLI clients read the file (the same pattern Jupyter uses). Setting
    ``PERUN_API_TOKEN`` in the environment overrides the file, which keeps
    headless clients (CI, scripts) stable without touching disk.
    """
    env = (os.environ.get("PERUN_API_TOKEN") or "").strip()
    if env:
        return env
    path = API_TOKEN_PATH
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:  # pragma: no cover - defensive
        pass
    token = os.urandom(32).hex()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL makes creation atomic: a second process (MCP server, Telegram
        # bridge) that loses the race reads the winner's token instead.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover - defensive
            return token
    except OSError:  # pragma: no cover - defensive
        return token
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    return token


def settings() -> Settings:
    """Load settings (fresh) — convenience for short-lived callers."""
    return Settings.load()
