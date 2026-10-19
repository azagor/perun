"""
Central, explicit registry of known Perun project roots.

Goal: prevent agents (and humans) from guessing cross-project paths like
``.\\my_apps\\perun\\AGENTS.md``, which silently resolves relative to the
current working directory and causes wrong-file edits or failed edits.

Usage:
    from perun.project_roots import resolve
    resolve("perun", "AGENTS.md")  # C:\\my_apps\\perun\\AGENTS.md
    resolve("perun_chat", "perunchat/api.py")

Roots are discovered in this order:
1. Environment variable ``PERUN_PROJECT_<NAME>`` (highest priority).
2. ``<PERUN_HOME>/project-roots.json``.
3. Built-in defaults for the three core projects on this machine:
   - perun -> C:\\my_apps\\perun
   - perun_chat -> C:\\my_apps\\perun_chat
   - perun_in_the_machine -> C:\\my_apps\\perun_in_the_machine

Additionally, whenever a project is registered with Perun's registry and its
path is not within an existing root, the registry is auto-updated so new
projects are reachable too.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict

from . import config

DEFAULTS: Dict[str, str] = {
    "perun": r"C:\my_apps\perun",
    "perun_chat": r"C:\my_apps\perun_chat",
    "perun_in_the_machine": r"C:\my_apps\perun_in_the_machine",
}

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _roots_file() -> Path:
    return config._resolve_home() / "project-roots.json"


def _load_user_roots() -> Dict[str, str]:
    path = _roots_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _save_user_roots(roots: Dict[str, str]) -> None:
    path = _roots_file()
    config.ensure_dirs()
    path.write_text(json.dumps(roots, indent=2), encoding="utf-8")


def _env_root(name: str) -> str | None:
    return os.environ.get(f"PERUN_PROJECT_{name.upper()}")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower() or "project"


def get_root(name: str) -> Path:
    """Return the absolute root path for *name*, raising if unknown."""
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid project root name: {name}")
    raw = _env_root(name) or _load_user_roots().get(name) or DEFAULTS.get(name)
    if not raw:
        raise KeyError(
            f"unknown project root: {name}. "
            f"Register it with: perun project-roots --add {name} <path>"
        )
    return Path(raw).resolve()


def resolve(name: str, relative_path: str = "") -> Path:
    """Resolve *relative_path* under the root for *name*.

    If *relative_path* is empty, returns the root itself.
    The result is always an absolute, normalized path.
    """
    root = get_root(name)
    if not relative_path:
        return root
    target = (root / relative_path).resolve()
    # Prevent path-escape mistakes (e.g. ../../../etc/passwd).
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"resolved path escapes project root: {target}") from exc
    return target


def list_roots() -> Dict[str, str]:
    """Return the effective roots for all known projects."""
    merged = dict(DEFAULTS)
    merged.update(_load_user_roots())
    for name in list(merged):
        env = _env_root(name)
        if env:
            merged[name] = env
    return merged


def add_root(name: str, path: str) -> None:
    """Persist a user-defined project root override."""
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid project root name: {name}")
    roots = _load_user_roots()
    roots[name] = str(Path(path).resolve())
    _save_user_roots(roots)


def remove_root(name: str) -> None:
    """Remove a user-defined project root override."""
    roots = _load_user_roots()
    roots.pop(name, None)
    _save_user_roots(roots)


def auto_register(slug: str, path: str) -> bool:
    """Auto-register a project root when it is discovered by Perun.

    Returns True if a new root was added, False if it already existed or is
    within an existing root.
    """
    roots = list_roots()
    target = Path(path).resolve()
    for root in roots.values():
        try:
            target.relative_to(Path(root).resolve())
            return False  # inside an existing root
        except ValueError:
            continue
    name = _slugify(slug)
    if name in DEFAULTS:
        return False
    add_root(name, str(target))
    return True
