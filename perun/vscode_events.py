"""Extract VS Code-relevant edit events from OpenCode's event stream.

Perun already receives every OpenCode event through
:class:`perun.opencode_api.EventHub` (the attention watcher uses the same global
subscription). This module turns a raw event into a small, path-validated
``{path, start_line, end_line, kind}`` record for the VS Code companion.

The parsing is deliberately defensive: OpenCode's event/part shape is not a
stable public contract, so a missing field degrades to "no edit" instead of an
exception. Paths are always relative to the project root; an event that points
outside the registered project is ignored.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

LOG = logging.getLogger("perun.vscode.events")

#: Tool names that write files. Everything else is not interesting to the editor.
EDIT_TOOLS = {
    "write", "edit", "patch", "apply_patch", "multiedit", "multi_edit",
    "create", "write_file", "edit_file",
}

_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", re.M)
_PATCH_HUNK = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@", re.M)


def _data(event: Dict[str, Any]) -> Dict[str, Any]:
    value = event.get("data")
    return value if isinstance(value, dict) else event


def session_id(event: Dict[str, Any]) -> str:
    data = _data(event)
    sid = data.get("sessionID") or data.get("session_id")
    if not sid and isinstance(data.get("info"), dict):
        sid = data["info"].get("sessionID")
    if not sid and isinstance(event.get("session"), dict):
        sid = event["session"].get("id")
    return str(sid or "")


def location(event: Dict[str, Any]) -> str:
    data = _data(event)
    for key in ("location", "directory", "cwd"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    props = data.get("properties")
    if isinstance(props, dict):
        for key in ("location", "directory", "cwd"):
            value = props.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _part(event: Dict[str, Any]) -> Dict[str, Any]:
    data = _data(event)
    part = data.get("part") or event.get("part")
    return part if isinstance(part, dict) else {}


def _input(event: Dict[str, Any]) -> Dict[str, Any]:
    part = _part(event)
    state = part.get("state") or {}
    if isinstance(state, dict) and isinstance(state.get("input"), dict):
        return state["input"]
    for key in ("input", "arguments", "args"):
        value = part.get(key) or _data(event).get(key)
        if isinstance(value, dict):
            return value
    return {}


def _tool(event: Dict[str, Any]) -> str:
    part = _part(event)
    return str(part.get("tool") or part.get("name") or "").lower()


def _status(event: Dict[str, Any]) -> str:
    part = _part(event)
    state = part.get("state") or {}
    return str(state.get("status") or "").lower() if isinstance(state, dict) else ""


def _relpath(value: str, project_path: str) -> Optional[str]:
    """A project-relative POSIX path, or None when it escapes the project."""
    raw = str(value or "").strip().strip('"').strip("'")
    if not raw:
        return None
    root = Path(project_path).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return None
    if not rel.parts or rel.parts[0] == ".git":
        # A tool may touch .git internally; never reveal it in the editor.
        return None
    return rel.as_posix()


def _line_range(text: str, needle: str, *, fallback: int = 1) -> tuple[int, int]:
    if not needle:
        return fallback, fallback
    index = text.find(needle)
    if index < 0:
        return fallback, fallback
    start = text.count("\n", 0, index) + 1
    end = start + max(0, needle.count("\n"))
    return start, end


def _from_patch(patch: str, project_path: str) -> Optional[Dict[str, Any]]:
    path_match = _PATCH_FILE.search(patch)
    hunk_match = _PATCH_HUNK.search(patch)
    if not path_match:
        return None
    rel = _relpath(path_match.group(1), project_path)
    if not rel:
        return None
    start = int(hunk_match.group(1)) if hunk_match else 1
    count = int(hunk_match.group(2) or 1) if hunk_match else 1
    return {
        "path": rel,
        "start_line": start,
        "end_line": start + max(0, count - 1),
        "kind": "changed",
        "tool": "patch",
    }


def is_edit_event(event: Dict[str, Any]) -> bool:
    """Cheap pre-filter so the global stream does not hit the DB per text token."""
    tool = _tool(event)
    if tool and tool not in EDIT_TOOLS:
        return False
    if _status(event) in ("pending", "running"):
        return False
    payload = _input(event)
    return bool(
        payload.get("filePath") or payload.get("file_path")
        or payload.get("path") or payload.get("patchText")
        or payload.get("patch") or payload.get("diff")
    )


def extract_edit(event: Dict[str, Any], project_path: str) -> Optional[Dict[str, Any]]:
    """Return one edit record for a tool event, or None.

    ``project_path`` is the registered project root. The returned ``path`` is
    always relative to it; absolute or escaping paths are refused.
    """
    if not project_path:
        return None
    tool = _tool(event)
    if tool and tool not in EDIT_TOOLS:
        return None
    status = _status(event)
    if status in ("pending", "running"):
        # Wait for the completed state: the file may not exist yet.
        return None

    payload = _input(event)
    if not payload:
        return None

    # Patch tools carry the target paths and hunks inside one string.
    patch = payload.get("patchText") or payload.get("patch") or payload.get("diff")
    if isinstance(patch, str) and patch.strip():
        return _from_patch(patch, project_path)

    raw_path = (
        payload.get("filePath") or payload.get("file_path")
        or payload.get("path") or payload.get("filename")
    )
    if not isinstance(raw_path, str):
        return None
    rel = _relpath(raw_path, project_path)
    if not rel:
        return None

    old = payload.get("oldString") or payload.get("old_string") or ""
    new = payload.get("newString") or payload.get("new_string")
    content = payload.get("content")
    if new is None:
        new = content
    new = new if isinstance(new, str) else ""
    old = old if isinstance(old, str) else ""

    kind = "changed"
    if new and not old:
        kind = "added" if tool in ("write", "create", "write_file") else "changed"
    elif old and not new:
        kind = "deleted"

    start = end = 1
    try:
        target = (Path(project_path) / rel).resolve()
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if text:
        if new and new in text:
            start, end = _line_range(text, new)
        elif old and old in text:
            # The tool may report the pre-image only (or the file write has
            # already normalised the text); reveal where the change was.
            start, end = _line_range(text, old)
        elif new:
            start, end = _line_range(text, new)
    if kind == "added" and tool in ("write", "create", "write_file"):
        # A whole-file write: reveal the file start, not a guessed substring.
        start, end = 1, max(1, len(new.splitlines()) if new else 1)

    return {
        "path": rel,
        "start_line": int(start),
        "end_line": int(end),
        "kind": kind,
        "tool": tool or "edit",
    }


def selection_ref(path: str, start_line: int, end_line: int) -> str:
    """The OpenCode ``@file#lines`` reference for a selection."""
    rel = str(path or "").replace("\\", "/").lstrip("/")
    start = max(1, int(start_line or 1))
    end = max(start, int(end_line or start))
    return f"@{rel}#{start}" if start == end else f"@{rel}#{start}-{end}"


def validate_selection(payload: Dict[str, Any], project_path: str) -> Optional[Dict[str, Any]]:
    """Validate and normalise a selection sent by the extension."""
    rel = _relpath(str(payload.get("path") or ""), project_path)
    if not rel:
        return None
    try:
        start = max(1, int(payload.get("start_line") or 1))
        end = max(start, int(payload.get("end_line") or start))
    except (TypeError, ValueError):
        return None
    text = str(payload.get("text") or "")
    if len(text) > 32_000:
        text = text[:32_000]
    return {
        "path": rel,
        "start_line": start,
        "end_line": end,
        "language": str(payload.get("language") or "")[:40],
        "text": text,
        "dirty": bool(payload.get("dirty")),
        "ref": selection_ref(rel, start, end),
    }


def event_debug_summary(event: Dict[str, Any]) -> str:
    """A bounded one-line summary for debug logs (never file contents)."""
    try:
        return json.dumps({
            "session": session_id(event),
            "tool": _tool(event),
            "status": _status(event),
        })
    except Exception:  # pragma: no cover - defensive
        return "{}"


__all__ = [
    "EDIT_TOOLS",
    "extract_edit",
    "is_edit_event",
    "session_id",
    "location",
    "selection_ref",
    "validate_selection",
]
