"""Release notes for the running Perun version.

The Windows installer excludes ``docs/`` (internal project content), so an
installed machine has no changelog to read. The release pipeline therefore bundles
the last releases as ``perun/release_notes.json``; a git checkout falls back to
``docs/CHANGELOG.md`` (the single source of truth). The dashboard's self-update
page shows the installed version's notes from whichever source is available.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__

#: How many releases the bundled file keeps.
MAX_BUNDLED = 10
BUNDLED_PATH = Path(__file__).with_name("release_notes.json")

_VERSION_HEADER = re.compile(r"^##\s+v(\d+\.\d+\.\d+)\s*$")
_RELEASED = re.compile(r"^\*Released\s+(\d{4}-\d{2}-\d{2})\.\*$")


def parse_changelog(text: str) -> List[Dict[str, Any]]:
    """``## vX.Y.Z`` sections, newest first, as ``{version, date, body}``.

    ``## Unreleased`` and any other ``## `` heading end the previous section;
    only final releases (``vX.Y.Z``) are returned.
    """
    releases: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    def _close() -> None:
        if current is None:
            return
        current["body"] = "\n".join(current.pop("_lines")).strip()
        releases.append(current)

    for line in (text or "").splitlines():
        if line.startswith("## "):
            _close()
            current = None
            header = _VERSION_HEADER.match(line.strip())
            if header:
                current = {"version": header.group(1), "date": "", "_lines": []}
            continue
        if current is None:
            continue
        released = _RELEASED.match(line.strip())
        if released and not current["date"]:
            current["date"] = released.group(1)
            continue
        current["_lines"].append(line)
    _close()
    return releases


def build_bundled(changelog_text: str, *, limit: int = MAX_BUNDLED) -> Dict[str, Any]:
    """The payload written to ``perun/release_notes.json`` by the release script."""
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "releases": parse_changelog(changelog_text)[:max(1, int(limit))],
    }


def _normalized(version: str) -> str:
    return str(version or "").strip().lstrip("vV")


def bundled() -> List[Dict[str, Any]]:
    try:
        data = json.loads(BUNDLED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: List[Dict[str, Any]] = []
    for entry in (data.get("releases") if isinstance(data, dict) else None) or []:
        if isinstance(entry, dict) and entry.get("version"):
            out.append({
                "version": _normalized(entry["version"]),
                "date": str(entry.get("date") or ""),
                "body": str(entry.get("body") or ""),
            })
    return out


def changelog_paths() -> List[Path]:
    """Where a git checkout keeps the canonical changelog."""
    roots = [Path(__file__).resolve().parent.parent]
    try:
        from . import config

        if Path(config.REPO_ROOT) not in roots:
            roots.append(Path(config.REPO_ROOT))
    except Exception:  # pragma: no cover - config is importable in practice
        pass
    return [root / "docs" / "CHANGELOG.md" for root in roots]


def from_changelog() -> List[Dict[str, Any]]:
    for path in changelog_paths():
        try:
            return parse_changelog(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return []


def for_version(version: str = "") -> Dict[str, Any]:
    """Notes for *version* (default: the running build), with their source.

    ``source`` is ``bundled`` (installed machine), ``changelog`` (git checkout) or
    ``unavailable``; an empty body means the UI hides the card rather than
    inventing release notes.
    """
    wanted = _normalized(version) or _normalized(__version__)
    for source, entries in (("bundled", bundled()), ("changelog", from_changelog())):
        for entry in entries:
            if entry["version"] == wanted:
                return {**entry, "source": source,
                        "installed": wanted == _normalized(__version__)}
    return {"version": wanted, "date": "", "body": "", "source": "unavailable",
            "installed": wanted == _normalized(__version__)}
