"""Idempotent global installation of Perun into OpenCode.

Perun's whole point is to be inherited by *every* project without per-project
edits. That is achieved by writing a handful of entries into the user-level
OpenCode config (``~/.config/opencode`` on every platform, honouring
``XDG_CONFIG_HOME``):

* ``mcp.perun``            — registers the Perun MCP server.
* ``AGENTS.md``            — a managed block with the behaviour guide. OpenCode
  V2 loads the global ``AGENTS.md`` as ambient instructions; the
  ``instructions[]`` array is accepted but **not resolved** by V2, so the
  ``AGENTS.md`` block is the mechanism that actually reaches the model.
* ``instructions[]``       — appended for V1 compatibility (inert under V2).
* ``command.{perun-*}``    — adds helper slash-commands.
* ``perun/INSTRUCTIONS.md`` — the human/agent-readable behaviour guide and the
  source for the ``AGENTS.md`` managed block.

Every step is additive and reversible: unrelated keys are preserved byte-for-
byte (the config is loaded as JSON and re-serialised), and the previous config
is backed up before the first modification. The ``AGENTS.md`` change is confined
to a marker-delimited block, so any surrounding user content is preserved.
``uninstall()`` removes exactly what Perun added and leaves everything else
untouched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from .. import config

MCP_NAME = "perun"
PERUN_SUBDIR = "perun"
INSTRUCTIONS_FILE = "INSTRUCTIONS.md"
AGENTS_FILE = "AGENTS.md"
AGENTS_BEGIN = "<!-- BEGIN perun (managed) -->"
AGENTS_END = "<!-- END perun (managed) -->"


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _env_config_file() -> Path | None:
    """``OPENCODE_CONFIG`` points OpenCode itself at a specific config file."""
    env = (os.environ.get("OPENCODE_CONFIG") or "").strip()
    return Path(env).expanduser() if env else None


def config_dir() -> Path:
    env_file = _env_config_file()
    if env_file is not None:
        return env_file.parent
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "opencode"


def opencode_json() -> Path:
    """The OpenCode config file Perun merges into.

    Honours ``OPENCODE_CONFIG`` (the same variable OpenCode itself reads) so the
    installer and the running instance can never disagree about which file is
    the real one — and so a per-instance config is possible without touching the
    user's global tree.
    """
    env_file = _env_config_file()
    if env_file is not None:
        return env_file
    return config_dir() / "opencode.json"


def perun_dir() -> Path:
    return config_dir() / PERUN_SUBDIR


def instructions_target() -> Path:
    return perun_dir() / INSTRUCTIONS_FILE


def agents_target() -> Path:
    return config_dir() / AGENTS_FILE


# --------------------------------------------------------------------------- #
# Global AGENTS.md managed block (the V2 instruction mechanism)
# --------------------------------------------------------------------------- #
def kernel_text() -> str:
    """The rendered global kernel: the template plus the generated key table.

    The template ships with the package; the key table is generated from the
    handbook manifest so the table an agent reads can never disagree with the
    context docs that actually exist.
    """
    src = config.TEMPLATES_DIR / INSTRUCTIONS_FILE
    raw = src.read_text(encoding="utf-8") if src.is_file() else ""
    try:
        from ..handbook import render_kernel

        return render_kernel(raw).strip()
    except Exception:  # pragma: no cover - fall back to the raw template
        return raw.strip()


# A minimal pointer file written to ``perun/INSTRUCTIONS.md``.  OpenCode V2
# loads ``instructions[]`` files into the prompt, so keeping a second copy of
# the kernel there duplicated the managed ``AGENTS.md`` block.  The previous
# full guide is retrievable as ``handbook.global``; the tool table lives in
# ``handbook.tools``.
_INSTRUCTIONS_POINTER = """\
# Perun — global instructions

Perun's machine-wide guidance lives in the managed block of your global
``AGENTS.md`` and in retrievable handbook docs. Call ``perun_context(\"list\")``
to see the available keys, ``perun_context(\"handbook.global\")`` for the
complete previous guide, and ``perun_context(\"handbook.tools\")`` for the
live MCP tool reference.
"""


def _managed_block() -> str:
    """The AGENTS.md block, sourced from the packaged kernel template."""
    body = kernel_text()
    return f"{AGENTS_BEGIN}\n{body}\n{AGENTS_END}\n"


def _sync_instructions_file() -> Path:
    """Write the thin ``perun/INSTRUCTIONS.md`` pointer, idempotently."""
    target = instructions_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = _INSTRUCTIONS_POINTER
    if target.is_file() and target.read_text(encoding="utf-8") == text:
        return target
    if target.is_file():
        _backup(target)
    target.write_text(text, encoding="utf-8")
    return target


def _strip_block(text: str) -> str:
    """Return *text* with the Perun managed block removed."""
    pattern = re.compile(
        r"[ \t]*" + re.escape(AGENTS_BEGIN) + r".*?" + re.escape(AGENTS_END) + r"[ \t]*\n?",
        re.DOTALL,
    )
    return pattern.sub("", text)


def _sync_agents_block() -> Path:
    """Write/refresh the Perun block in the global ``AGENTS.md``.

    Idempotent and non-destructive: user content outside the markers is kept.
    """
    target = agents_target()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    base = _strip_block(existing).strip()
    block = _managed_block()
    text = f"{base}\n\n{block}" if base else block
    target.parent.mkdir(parents=True, exist_ok=True)
    if existing != text:
        if target.is_file():
            _backup(target)
        target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# JSON merge helpers
# --------------------------------------------------------------------------- #
class ConfigParseError(RuntimeError):
    """Raised when the target OpenCode config cannot be parsed."""


def _read_json(path: Path) -> dict[str, Any]:
    """Parse the OpenCode config, tolerating JSONC.

    Raises :class:`ConfigParseError` when the file exists but cannot be parsed:
    rewriting it would silently drop the user's agents and providers, so the
    installer must stop instead.
    """
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigParseError(f"cannot read {path}: {exc}") from exc
    from ..mcp_servers import _strip_jsonc

    last: Exception | None = None
    for candidate in (text, _strip_jsonc(text)):
        try:
            data = json.loads(candidate)
        except Exception as exc:  # noqa: BLE001 - reported verbatim below
            last = exc
            continue
        return data if isinstance(data, dict) else {}
    raise ConfigParseError(
        f"{path} is not valid JSON/JSONC ({last}). Perun will not rewrite it — "
        "fix or move the file, or point OPENCODE_CONFIG at a Perun-managed one."
    ) from last


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _backup(path: Path) -> Path | None:
    """Copy the global config into ``PERUN_HOME/backups/opencode/`` before a rewrite."""
    from .. import backups as _backups

    return _backups.save_copy(path, label="opencode")


def _python_cmd() -> list[str]:
    exe = shutil.which(sys.executable) if sys.executable else None
    return [exe or sys.executable or "python", str(config.MCP_SERVER)]


def _relative_to_config(path: Path) -> str:
    """Instructions paths are relative to the config dir when possible."""
    try:
        return str(path.relative_to(config_dir())).replace("\\", "/")
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- #
# Install / uninstall
# --------------------------------------------------------------------------- #
def install(*, force: bool = False, index: bool = True) -> int:
    cfg_dir = config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    # 1. Write the Perun instructions pointer + command templates.
    perun_dir().mkdir(parents=True, exist_ok=True)
    templates = config.TEMPLATES_DIR
    ins_src = templates / INSTRUCTIONS_FILE
    if ins_src.is_file():
        # Write only a pointer: the managed AGENTS.md block already carries the
        # kernel, and duplicating it here cost ~1,500 tokens per session. The
        # previous full guide is retrievable as handbook.global.
        _sync_instructions_file()
    commands_dir = cfg_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    installed_commands: list[str] = []
    for tpl in sorted(templates.glob("command_*.md")):
        name = tpl.name[len("command_"):]
        shutil.copy2(tpl, commands_dir / name)
        installed_commands.append(name[:-3])

    # 2. Merge into opencode.json. The MCP block is written in the shape the
    # installed CLI understands (V2 nests servers under ``mcp.servers`` and uses
    # ``disabled``); reading is shape-agnostic, so a config written by an older
    # Perun is migrated once instead of being left with a dead flat entry.
    from .. import mcp_servers as _mcp

    path = opencode_json()
    try:
        data = _read_json(path)
    except ConfigParseError as exc:
        print(f"[perun] ERROR: {exc}")
        return 2
    original = path.read_text(encoding="utf-8") if path.is_file() else None
    backup = None

    style = _mcp.mcp_style()
    servers = dict(_mcp.server_entries(data))
    servers[MCP_NAME] = _mcp.entry_for_style({
        "type": "local",
        "command": _python_cmd(),
        "enabled": True,
        # Pin the state directory: a session with a non-default PERUN_HOME (or a
        # different one in its environment) would otherwise talk to whichever
        # database the MCP process happened to pick up, not the running server's.
        "environment": {"PERUN_HOME": str(config.HOME)},
    }, style)
    _mcp.set_server_entries(data, servers, style)

    instruction_ref = _relative_to_config(instructions_target())
    instructions = data.get("instructions")
    if not isinstance(instructions, list):
        instructions = []
    if instruction_ref not in instructions:
        instructions.append(instruction_ref)
    data["instructions"] = instructions

    commands = data.get("command")
    if not isinstance(commands, dict):
        commands = {}
    commands.setdefault(
        "perun-projects",
        {"template": "List every OpenCode project Perun knows about.",
         "description": "List Perun projects (via the perun MCP server)"},
    )
    commands.setdefault(
        "perun-ask",
        {"template": "Ask another Perun project: $ARGUMENTS",
         "description": "Ask another Perun-managed project a question"},
    )
    commands.setdefault(
        "logwork",
        {"template": "Save a short work summary to Perun memory: $ARGUMENTS",
         "description": "Log a one-sentence work summary as a project or global fact"},
    )
    commands.setdefault(
        "update-docs",
        {"template": "Update project docs from the current codebase: $ARGUMENTS",
         "description": "Refresh CODEMAP, CHANGELOG, TECHNICAL_DOCUMENTATION and manifest docs"},
    )
    data["command"] = commands

    # Back up (and rewrite) only when something actually changes: re-running the
    # installer used to pile up a timestamped copy every single time.
    merged = json.dumps(data, indent=2) + "\n"
    if original == merged:
        backup = None
    else:
        if path.exists():
            backup = _backup(path)
        _write_json(path, data)

    # 2b. Global ambient instructions — OpenCode V2 loads ~/.config/opencode/
    #     AGENTS.md, so this is what actually reaches the model (the
    #     `instructions[]` entry above is inert under V2, kept for V1).
    agents_path = _sync_agents_block()

    print(f"[perun] config:      {path}")
    if backup:
        print(f"[perun] backed up:   {backup}")
    print(f"[perun] instructions: {agents_path}")
    print(f"[perun] mcp '{MCP_NAME}': {' '.join(_python_cmd())}")
    if installed_commands:
        print(f"[perun] commands:    {', '.join(installed_commands)}")
    print("[perun] global install complete — every OpenCode project now inherits Perun.")

    # 3. Register this repo + discover siblings, so the dashboard is not empty.
    try:
        from .. import db, discovery, registry
        db.init_db()
        if not registry.get_project_by_path(str(config.REPO_ROOT)):
            registry.add_project("Perun", str(config.REPO_ROOT),
                                 description="Overseer & orchestrator for all OpenCode projects")
        settings = config.Settings.load()
        discovery.discover_and_sync(settings.roots, max_depth=1)
        settings.auto_discover_on_start = True
        settings.save()
        if index:
            from .. import memory
            for proj in registry.list_projects():
                try:
                    memory.index_project_docs(proj["id"])
                except Exception:
                    pass
        # Index the handbook into the Perun Context store so `perun_context`
        # can serve the reference material that used to live in full inside
        # the AGENTS.md block.
        try:
            from .. import handbook

            res = handbook.ensure_indexed()
            if res.get("ok"):
                print(f"[perun] handbook:      {res.get('indexed', 0)} doc(s) indexed, "
                      f"{res.get('unchanged', 0)} unchanged")
        except Exception as exc:  # pragma: no cover - best effort
            print(f"[perun] warning: could not index the handbook ({exc})")
    except Exception as exc:  # pragma: no cover - best effort
        print(f"[perun] warning: could not seed the registry ({exc})")

    print("[perun] start the dashboard with:  python -m perun serve")
    return 0


def uninstall() -> int:
    path = opencode_json()
    try:
        data = _read_json(path)
    except ConfigParseError as exc:
        print(f"[perun] ERROR: {exc}")
        return 2
    changed = False

    from .. import mcp_servers as _mcp

    servers = _mcp.server_entries(data)
    if MCP_NAME in servers:
        _mcp.set_server_entries(
            data, {k: v for k, v in servers.items() if k != MCP_NAME},
            _mcp.mcp_style(),
        )
        changed = True

    instr = data.get("instructions")
    if isinstance(instr, list):
        filtered = [i for i in instr if PERUN_SUBDIR not in str(i)]
        if filtered != instr:
            data["instructions"] = filtered
            changed = True

    commands = data.get("command")
    if isinstance(commands, dict):
        for key in list(commands):
            if key.startswith("perun-"):
                commands.pop(key)
        if not commands:
            data.pop("command", None)
        changed = True

    if changed and path.exists():
        _backup(path)
        _write_json(path, data)
        print(f"[perun] removed Perun entries from {path}")

    # Remove the Perun block from the global AGENTS.md (keep user content).
    agents_path = agents_target()
    if agents_path.is_file():
        existing = agents_path.read_text(encoding="utf-8")
        remaining = _strip_block(existing).strip()
        _backup(agents_path)
        if remaining:
            agents_path.write_text(remaining + "\n", encoding="utf-8")
            print(f"[perun] removed Perun block from {agents_path}")
        else:
            agents_path.unlink()
            print(f"[perun] removed {agents_path}")

    # Remove the copied instructions + commands we own.
    for name in ("perun-projects.md", "perun-ask.md", "logwork.md", "update-docs.md"):
        f = config_dir() / "commands" / name
        if f.is_file():
            f.unlink()
    ins = instructions_target()
    if ins.is_file():
        ins.unlink()
    try:
        perun_dir().rmdir()
    except OSError:
        pass

    print("[perun] global uninstall complete (runtime data in ~/.perun is kept).")
    return 0


def status() -> dict[str, Any]:
    try:
        data = _read_json(opencode_json())
        config_error = ""
    except ConfigParseError as exc:
        data, config_error = {}, str(exc)
    from .. import mcp_servers as _mcp

    mcp = _mcp.server_entries(data).get(MCP_NAME)
    agents_path = agents_target()
    agents_block = False
    if agents_path.is_file():
        agents_block = AGENTS_BEGIN in agents_path.read_text(encoding="utf-8")
    return {
        "config": str(opencode_json()),
        "installed": bool(mcp),
        "mcp": mcp,
        "config_error": config_error,
        "instructions_present": instructions_target().is_file(),
        "agents_path": str(agents_path),
        "agents_block": agents_block,
        "commands": sorted(
            (config_dir() / "commands").glob("perun-*.md")
        ) if (config_dir() / "commands").is_dir() else [],
    }
