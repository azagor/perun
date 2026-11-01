"""Managed agent files: write once, refresh only while untouched.

Perun installs a few OpenCode agents of its own (``perun-orchestrator``, and the
restricted ``perun-docs``). They share one policy, so the policy lives here once:

* first install -> write the shipped template and record its sha256;
* a later Perun version -> refresh **only** if the file still matches that hash;
* a hand-edited file -> never overwritten (reported as ``customized``);
* :func:`reset` restores the template on explicit request.

The recorded hashes live in ``PERUN_HOME/agent_templates.json`` keyed by agent id,
so one corrupt entry cannot take the others with it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from . import config

LOG = logging.getLogger("perun.agents")

_lock = threading.Lock()


def state_path() -> Path:
    return Path(config.HOME) / "agent_templates.json"


def agent_path(agent_id: str) -> Path:
    """Where *agent_id*'s Markdown file lives.

    ``OPENCODE_CONFIG`` wins when set, so an install with
    ``OPENCODE_CONFIG=/opt/oc/opencode.json`` puts the agent next to the config
    OpenCode actually reads.
    """
    env = (os.environ.get("OPENCODE_CONFIG") or "").strip()
    base = Path(env).expanduser().parent if env else config.OPENCODE_CONFIG_DIR
    return base / "agents" / f"{agent_id}.md"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_state() -> Dict[str, Any]:
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: Dict[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".tmp", prefix=path.name + ".",
                                   dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:  # pragma: no cover - best effort, the file is informational
        LOG.debug("could not write %s", path, exc_info=True)


def _write_agent(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".tmp", prefix=target.name + ".",
                               dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _entry(state: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    entry = state.get(agent_id)
    return entry if isinstance(entry, dict) else {}


def recorded_version(entry: Dict[str, Any]) -> int:
    """The template version Perun recorded, tolerant of a hand-edited state file."""
    try:
        return int(entry.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def is_customized(agent_id: str) -> bool:
    """True when the file on disk is not the template Perun last wrote."""
    target = agent_path(agent_id)
    if not target.is_file():
        return False
    recorded = str(_entry(_read_state(), agent_id).get("sha256") or "")
    try:
        current = _digest(target.read_text(encoding="utf-8"))
    except OSError:  # pragma: no cover - unreadable file
        return False
    if not recorded:
        # A file Perun did not write: treat it as the user's.
        return True
    return current != recorded


def ensure(agent_id: str, template: str, version: int, *,
           force: bool = False) -> str:
    """Install or refresh one agent file.

    Returns ``created``/``updated``/``unchanged``/``customized``/``failed`` and
    never raises: it is called from every process that starts such an agent.
    """
    wanted = _digest(template)
    target = agent_path(agent_id)
    with _lock:
        state = _read_state()
        entry = _entry(state, agent_id)
        recorded = str(entry.get("sha256") or "")
        try:
            exists = target.is_file()
            current = _digest(target.read_text(encoding="utf-8")) if exists else ""
        except OSError:  # pragma: no cover - unreadable file
            return "failed"
        if exists and not force:
            if current != wanted and not recorded:
                LOG.info("agent %s exists and was not written by Perun; leaving it",
                         agent_id)
                return "customized"
            if current != recorded:
                LOG.info("agent %s was edited by the user; leaving it", agent_id)
                return "customized"
            if current == wanted:
                if recorded_version(entry) != version:
                    state[agent_id] = {"version": version, "sha256": wanted}
                    _write_state(state)
                return "unchanged"
        action = "updated" if exists else "created"
        try:
            _write_agent(target, template)
        except OSError:
            LOG.debug("could not write the %s agent", agent_id, exc_info=True)
            return "failed"
        state[agent_id] = {"version": version, "sha256": wanted}
        _write_state(state)
    LOG.info("agent %s %s at %s", agent_id, action, target)
    return action


def reset(agent_id: str, template: str, version: int) -> Dict[str, Any]:
    """Restore the shipped template, discarding local edits (explicit request)."""
    action = ensure(agent_id, template, version, force=True)
    return {"ok": action not in ("failed",), "action": action,
            "path": str(agent_path(agent_id))}


def status(agent_id: str, version: int) -> Dict[str, Any]:
    target = agent_path(agent_id)
    entry = _entry(_read_state(), agent_id)
    return {
        "agent_id": agent_id,
        "path": str(target),
        "installed": target.is_file(),
        "customized": is_customized(agent_id),
        "template_version": version,
        "installed_version": recorded_version(entry),
    }


def available(agent_id: Optional[str] = None) -> bool:
    """Whether *agent_id* can be passed to ``opencode run --agent``."""
    agent_id = (agent_id or "").strip()
    if not agent_id:
        return True  # an empty id means "OpenCode's default"
    if agent_id.startswith("perun-"):
        return agent_path(agent_id).is_file()
    return True  # another agent (e.g. ``build``) is OpenCode's business
