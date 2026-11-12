"""VS Code discovery, workspace generation and fixed-argv launch helpers.

Perun never installs VS Code silently and never builds a shell string from
project data. The `code` CLI on Windows is a ``.cmd`` shim, which cannot be
passed to ``CreateProcess`` directly; :func:`_code_argv` wraps it in ``cmd /c``
and every argument stays a separate list element.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import config

LOG = logging.getLogger("perun.vscode")

WORKSPACE_VERSION = 1
_DETECT_LOCK = threading.Lock()
_DETECT_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}
_DETECT_TTL = 60.0

_EXTENSION_ID = "perun.perun-overseer"
_OFFICIAL_EXTENSION_ID = "sst-dev.opencode"


def workspace_dir() -> Path:
    return config.HOME / "workspaces"


def heartbeat_path() -> Path:
    return config.HOME / "vscode" / "extension.json"


def _candidate_paths() -> list[str]:
    values: list[str] = []
    found = shutil.which("code")
    if found:
        values.append(found)
    system = platform.system()
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA") or ""
        program_files = os.environ.get("ProgramFiles") or ""
        if local:
            values += [
                str(Path(local) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd"),
                str(Path(local) / "Programs" / "Microsoft VS Code" / "Code.exe"),
            ]
        if program_files:
            values += [
                str(Path(program_files) / "Microsoft VS Code" / "bin" / "code.cmd"),
                str(Path(program_files) / "Microsoft VS Code" / "Code.exe"),
            ]
    elif system == "Darwin":
        values += [
            "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
            "/usr/local/bin/code",
        ]
    else:
        values += ["/usr/bin/code", "/usr/share/code/bin/code", "/snap/bin/code"]
    return values


def _code_argv(path: str, args: list[str]) -> list[str]:
    """A safe argv for the shim or the real executable."""
    suffix = Path(path).suffix.lower()
    if os.name == "nt" and suffix in (".cmd", ".bat"):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", path, *args]
    return [path, *args]


def _version(path: str) -> str:
    try:
        proc = subprocess.run(
            _code_argv(path, ["--version"]),
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = (proc.stdout or "").splitlines()
    return lines[0].strip() if lines else ""


def detect(*, force: bool = False) -> Optional[Dict[str, Any]]:
    """Return ``{path, version, kind}`` for the first working VS Code CLI."""
    now = time.time()
    with _DETECT_LOCK:
        cached = _DETECT_CACHE.get("value")
        if not force and cached and now - float(_DETECT_CACHE.get("at") or 0) < _DETECT_TTL:
            return cached
    value = None
    for candidate in _candidate_paths():
        path = Path(candidate)
        if not path.is_file():
            continue
        version = _version(str(path))
        if not version and path.suffix.lower() not in (".exe",):
            continue
        value = {
            "path": str(path),
            "version": version,
            "kind": "exe" if path.suffix.lower() == ".exe" else "cli",
        }
        break
    with _DETECT_LOCK:
        _DETECT_CACHE["value"] = value
        _DETECT_CACHE["at"] = now
    return value


def workspace_path(slug: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in str(slug or "project"))
    return workspace_dir() / f"{safe}.code-workspace"


def _workspace_data(project: Dict[str, Any], *, managed: bool = True) -> Dict[str, Any]:
    root = str(project.get("path") or "")
    name = str(project.get("name") or project.get("slug") or "Project")
    return {
        "folders": [{"path": root}],
        "settings": {
            "window.title": f"{name} — Perun",
            "perun.projectSlug": str(project.get("slug") or ""),
            "perun.managedWorkspace": bool(managed),
            "extensions.recommendations": [_OFFICIAL_EXTENSION_ID, _EXTENSION_ID],
        },
    }


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def generate(project: Dict[str, Any], *, workspace_in_project: bool = False) -> str:
    """Create/regenerate the workspace file and return its absolute path."""
    if workspace_in_project:
        target = Path(str(project.get("path") or "")) / "perun.code-workspace"
        _ensure_git_exclude(Path(str(project.get("path") or "")), "perun.code-workspace")
    else:
        target = workspace_path(str(project.get("slug") or "project"))
    _write_json(target, _workspace_data(project))
    return str(target)


def _ensure_git_exclude(root: Path, entry: str) -> None:
    """Keep an in-project workspace out of the repository without a tracked file."""
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return
    exclude = git_dir / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        lines = {line.strip() for line in existing.splitlines()}
        if entry in lines:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "\n" if existing and not existing.endswith("\n") else ""
        exclude.write_text(existing + prefix + entry + "\n", encoding="utf-8")
    except OSError:
        # An exclude file is a convenience; the workspace still works without it.
        pass


def is_managed_workspace(path: str) -> bool:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    settings = data.get("settings") if isinstance(data, dict) else None
    return bool(isinstance(settings, dict) and settings.get("perun.managedWorkspace"))


def remove_workspace(path: str) -> bool:
    """Delete a Perun-managed workspace file; never a user's own file."""
    target = Path(str(path or ""))
    if not target.is_file() or not is_managed_workspace(str(target)):
        return False
    try:
        target.unlink()
    except OSError:
        return False
    return True


def _launch(args: list[str], *, cwd: Optional[str] = None) -> Dict[str, Any]:
    detected = detect()
    if not detected:
        return {"ok": False, "error": "Visual Studio Code was not found"}
    argv = _code_argv(str(detected["path"]), args)
    try:
        subprocess.Popen(
            argv,
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except OSError as exc:
        return {"ok": False, "error": f"could not launch VS Code: {exc}"}
    return {"ok": True}


def open_workspace(workspace: str, *, reuse_window: bool = True) -> Dict[str, Any]:
    target = str(workspace or "")
    if not target or not Path(target).is_file():
        return {"ok": False, "error": "workspace file not found"}
    args = ["--reuse-window"] if reuse_window else []
    return _launch([*args, target])


def reveal(file_path: str, line: int = 1, *, reuse_window: bool = True) -> Dict[str, Any]:
    """Open *file_path* at *line* in the active VS Code window (no extension needed)."""
    target = Path(str(file_path or ""))
    if not target.is_file():
        return {"ok": False, "error": "file not found"}
    arg = f"{target}:{max(1, int(line or 1))}"
    args = (["--reuse-window"] if reuse_window else []) + ["--goto", arg]
    return _launch(args)


def read_heartbeat() -> Dict[str, Any]:
    path = heartbeat_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def status(*, linked_projects: int = 0, clients: int = 0) -> Dict[str, Any]:
    detected = detect()
    heartbeat = read_heartbeat()
    return {
        "detected": bool(detected),
        "code_path": (detected or {}).get("path", ""),
        "code_version": (detected or {}).get("version", ""),
        "extension_installed": bool(heartbeat.get("version")),
        "extension_version": str(heartbeat.get("version") or ""),
        "extension_last_seen": str(heartbeat.get("last_seen") or ""),
        "linked_projects": int(linked_projects),
        "connected_clients": int(clients),
        "install_command": (
            'code --install-extension "<PERUN_DIR>\\vscode\\perun-overseer.vsix" --force'
            if detected else ""
        ),
    }


__all__ = [
    "detect",
    "workspace_dir",
    "workspace_path",
    "generate",
    "remove_workspace",
    "open_workspace",
    "reveal",
    "status",
    "read_heartbeat",
    "heartbeat_path",
]
