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
import time
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
def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "opencode"


def opencode_json() -> Path:
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
def _managed_block() -> str:
    """The AGENTS.md block, sourced from the packaged INSTRUCTIONS.md."""
    src = config.TEMPLATES_DIR / INSTRUCTIONS_FILE
    body = src.read_text(encoding="utf-8").strip() if src.is_file() else ""
    return f"{AGENTS_BEGIN}\n{body}\n{AGENTS_END}\n"


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
def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _backup(path: Path) -> Path | None:
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_suffix(path.suffix + f".perun-backup-{stamp}")
    shutil.copy2(path, dest)
    return dest


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

    # 1. Write the Perun instructions + command templates.
    perun_dir().mkdir(parents=True, exist_ok=True)
    templates = config.TEMPLATES_DIR
    ins_src = templates / INSTRUCTIONS_FILE
    if ins_src.is_file():
        shutil.copy2(ins_src, instructions_target())
    commands_dir = cfg_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    installed_commands: list[str] = []
    for tpl in sorted(templates.glob("command_*.md")):
        name = tpl.name[len("command_"):]
        shutil.copy2(tpl, commands_dir / name)
        installed_commands.append(name[:-3])

    # 2. Merge into opencode.json.
    path = opencode_json()
    data = _read_json(path)
    backup = _backup(path) if path.exists() else None

    mcp = data.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        mcp = {}
        data["mcp"] = mcp
    mcp[MCP_NAME] = {
        "type": "local",
        "command": _python_cmd(),
        "enabled": True,
    }

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
    except Exception as exc:  # pragma: no cover - best effort
        print(f"[perun] warning: could not seed the registry ({exc})")

    print("[perun] start the dashboard with:  python -m perun serve")
    return 0


def uninstall() -> int:
    path = opencode_json()
    data = _read_json(path)
    changed = False

    mcp = data.get("mcp")
    if isinstance(mcp, dict) and MCP_NAME in mcp:
        mcp.pop(MCP_NAME, None)
        if not mcp:
            data.pop("mcp", None)
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
    data = _read_json(opencode_json())
    mcp = (data.get("mcp") or {}).get(MCP_NAME)
    agents_path = agents_target()
    agents_block = False
    if agents_path.is_file():
        agents_block = AGENTS_BEGIN in agents_path.read_text(encoding="utf-8")
    return {
        "config": str(opencode_json()),
        "installed": bool(mcp),
        "mcp": mcp,
        "instructions_present": instructions_target().is_file(),
        "agents_path": str(agents_path),
        "agents_block": agents_block,
        "commands": sorted(
            (config_dir() / "commands").glob("perun-*.md")
        ) if (config_dir() / "commands").is_dir() else [],
    }
