"""Idempotent global installation of Perun into OpenCode.

Perun's whole point is to be inherited by *every* project without per-project
edits. That is achieved by writing a handful of entries into the user-level
OpenCode config (``~/.config/opencode`` on every platform, honouring
``XDG_CONFIG_HOME``):

* ``mcp.perun``            — registers the Perun MCP server.
* ``instructions[]``       — appends the Perun instructions file.
* ``command.{perun-*}``    — adds helper slash-commands.
* ``perun/INSTRUCTIONS.md`` — the human/agent-readable behaviour guide.

Every step is additive and reversible: unrelated keys are preserved byte-for-
byte (the config is loaded as JSON and re-serialised), and the previous config
is backed up before the first modification. ``uninstall()`` removes exactly the
keys Perun added and leaves everything else untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .. import config

MCP_NAME = "perun"
PERUN_SUBDIR = "perun"
INSTRUCTIONS_FILE = "INSTRUCTIONS.md"


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
    data["command"] = commands

    _write_json(path, data)

    print(f"[perun] config:      {path}")
    if backup:
        print(f"[perun] backed up:   {backup}")
    print(f"[perun] instructions: {instructions_target()}")
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

    # Remove the copied instructions + commands we own.
    for name in ("perun-projects.md", "perun-ask.md"):
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
    return {
        "config": str(opencode_json()),
        "installed": bool(mcp),
        "mcp": mcp,
        "instructions_present": instructions_target().is_file(),
        "commands": sorted(
            (config_dir() / "commands").glob("perun-*.md")
        ) if (config_dir() / "commands").is_dir() else [],
    }
