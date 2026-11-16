"""Custody for third-party agent rosters Perun does **not** install.

The Agency Agents roster (<https://github.com/msitarzewski/agency-agents>) ships
its own converter and installer; that script — or their desktop app — owns
acquisition. Perun only keeps custody of what landed on disk:

* recognise an agent from a build-time index (``perun/data/agency_index.json``,
  no network, no markdown parsing at runtime);
* record provenance and a sha256 per file (``PERUN_HOME/agent_catalog.json``);
* report what is installed, what the OpenCode runtime actually registered, and
  how much room is left before the ~119-agent ceiling;
* protect a hand-edited copy before the upstream installer overwrites it;
* remove cleanly, and generate a curated ``--agents-file`` list.

Two rules are load-bearing:

* Perun never installs, converts, or overwrites a roster file.
* A file whose hash no longer matches is ``modified`` and is **never** deleted or
  silently re-baselined; the operator has to accept it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import agent_assignments, backups, config

LOG = logging.getLogger("perun.agent_catalog")

SOURCE_URL = "https://github.com/msitarzewski/agency-agents"
TARBALL_URL = "https://codeload.github.com/msitarzewski/agency-agents/tar.gz/{ref}"
INDEX_PATH = Path(__file__).resolve().parent / "data" / "agency_index.json"
# Upstream reports that the runtime registers only ~119 agents and silently drops
# the rest (anomalyco/opencode#27988). Measured on OpenCode v2.2.5 (2026-11-16)
# with `opencode debug agents --standalone`: 307 agents registered from 300 files
# plus 7 built-ins, with nothing dropped, and the count tracked the file count 1:1
# from 110 to 300. The guard stays — a future build may reintroduce the cap — but
# at a ceiling this build actually honours, and it is per machine.
DEFAULT_LIMIT = 300
LIMIT_SOURCE = ("measured on OpenCode v2.2.5: 307 registered, nothing dropped "
                "(`opencode debug agents --standalone`); upstream #27988 is stale")

MANIFEST_SCHEMA = 1
_INDEX_MAX_BYTES = 8 * 1024 * 1024
_ARCHIVE_MAX_BYTES = 64 * 1024 * 1024
AGENT_MAX_BYTES = 512 * 1024
_MEASURE_TTL = 60.0

# Upstream's ``resolve_opencode_color``: CSS names where one exists, Tailwind
# 500 otherwise, and a grey fallback for anything unrecognised.
NAMED_COLORS = {
    "cyan": "#00FFFF", "blue": "#3498DB", "green": "#2ECC71", "red": "#E74C3C",
    "purple": "#9B59B6", "orange": "#F39C12", "teal": "#008080",
    "indigo": "#6366F1", "pink": "#E84393", "gold": "#EAB308",
    "amber": "#F59E0B", "neon-green": "#10B981", "neon-cyan": "#06B6D4",
    "metallic-blue": "#3B82F6", "yellow": "#EAB308", "violet": "#8B5CF6",
    "rose": "#F43F5E", "lime": "#84CC16", "gray": "#6B7280",
    "fuchsia": "#D946EF", "slate": "#64748B", "navy": "#000080",
}
FALLBACK_COLOR = "#6B7280"


class CatalogError(Exception):
    """A refused or impossible custody action (surfaced as HTTP 400)."""


_index_cache: Dict[str, Any] = {"key": None, "data": None}
_measure_cache: Dict[str, Any] = {"key": None, "ts": 0.0, "data": None}
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Index (build-time artifact, offline at runtime)
# --------------------------------------------------------------------------- #

def _empty_index(error: str = "") -> Dict[str, Any]:
    return {"available": False, "source": SOURCE_URL, "commit": "", "agents": [],
            "by_slug": {}, "divisions": {}, "collisions": [], "error": error}


def load_index(force: bool = False) -> Dict[str, Any]:
    """Read the committed roster index; never raises.

    A missing or corrupt file is a normal degraded state: the catalogue simply
    reports nothing rather than taking ``serve`` down with it.
    """
    try:
        stat = INDEX_PATH.stat()
        key = (str(INDEX_PATH), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return _empty_index("index missing")
    if not force and _index_cache["data"] is not None and _index_cache["key"] == key:
        return _index_cache["data"]

    if stat.st_size > _INDEX_MAX_BYTES:
        return _empty_index("index too large")
    try:
        payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOG.debug("agent index unreadable: %s", exc)
        return _empty_index("index unreadable")

    if not isinstance(payload, dict) or not isinstance(payload.get("agents"), list):
        return _empty_index("index malformed")
    agents: List[Dict[str, Any]] = []
    by_slug: Dict[str, Dict[str, Any]] = {}
    for raw in payload["agents"]:
        if not isinstance(raw, dict):
            continue
        slug = str(raw.get("slug") or "").strip()
        # The slug becomes a filename on install; the index is data, so never
        # let a hand-edited one point outside the agents directory.
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", slug) or slug in by_slug:
            continue
        entry = {
            "slug": slug,
            "name": str(raw.get("name") or slug),
            "description": str(raw.get("description") or ""),
            "division": str(raw.get("division") or ""),
            "color": str(raw.get("color") or ""),
            "emoji": str(raw.get("emoji") or ""),
            "path": str(raw.get("path") or ""),
        }
        agents.append(entry)
        by_slug[slug] = entry
    divisions = payload.get("divisions") if isinstance(payload.get("divisions"), dict) else {}
    data = {
        "available": bool(agents),
        "source": str(payload.get("source") or SOURCE_URL),
        "commit": str(payload.get("commit") or ""),
        "agents": agents,
        "by_slug": by_slug,
        "divisions": {str(k): v for k, v in divisions.items() if isinstance(v, dict)},
        "collisions": [str(s) for s in (payload.get("collisions") or [])],
        "error": "" if agents else "index empty",
    }
    _index_cache.update(key=key, data=data)
    return data


# --------------------------------------------------------------------------- #
# Locations and hashing
# --------------------------------------------------------------------------- #

def agent_dirs(project_path: str | Path | None = None) -> List[Path]:
    """Every directory OpenCode reads agents from, global first.

    Both spellings (``agent/`` and ``agents/``) are scanned, matching
    :mod:`perun.agents`; upstream's installer writes ``agents/``.
    """
    roots = [Path(config.OPENCODE_CONFIG_DIR)]
    if project_path:
        roots.append(Path(project_path) / ".opencode")
    dirs: List[Path] = []
    for root in roots:
        dirs.append(root / "agents")
        dirs.append(root / "agent")
    return dirs


def _md_files(dirs: Iterable[Path]) -> List[Path]:
    files: List[Path] = []
    seen: set[str] = set()
    for base in dirs:
        if not base.is_dir():
            continue
        try:
            children = sorted(base.glob("*.md"))
        except OSError:  # pragma: no cover - unreadable directory
            continue
        for path in children:
            if not path.is_file():
                continue
            key = _key(path).lower()
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    return files


def _key(path: str | Path) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:  # pragma: no cover - a path that cannot be resolved
        return str(path)


_HASH_CACHE_MAX = 4096
_hash_cache: Dict[str, tuple[int, int, str]] = {}
_mode_cache: Dict[str, tuple[int, int, str]] = {}


def _hash(path: Path) -> str:
    """sha256 of a file, cached by (mtime_ns, size).

    The Settings card calls ``status`` and ``installed`` on every render, and
    each walks every agent file; re-reading ~100 unchanged files per click is
    pointless. A same-size, same-timestamp rewrite is the only way to fool this,
    and the writers below invalidate the entry they touch.
    """
    stat = path.stat()
    key = str(path)
    cached = _hash_cache.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    if len(_hash_cache) >= _HASH_CACHE_MAX:
        _hash_cache.clear()
    _hash_cache[key] = (stat.st_mtime_ns, stat.st_size, value)
    return value


def _forget_hash(path: str | Path) -> None:
    _hash_cache.pop(str(path), None)
    _mode_cache.pop(str(path), None)


def _agent_mode(path: Path) -> str:
    """The file's own ``mode:`` value (``""`` when absent), cached by mtime/size.

    Read from the file rather than the manifest: a hand edit would make
    ``mode_intent`` a lie, and the UI offers a toggle based on this value.
    """
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = str(path)
    cached = _mode_cache.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    mode = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = []
            for index, line in enumerate(handle):
                if index > 80:  # frontmatter is at the head of the file
                    break
                lines.append(line)
    except OSError:
        return ""
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            name, sep, value = line.partition(":")
            if sep and name.strip().lower() == "mode":
                mode = value.strip().strip("'\"")
                break
    if len(_mode_cache) >= _HASH_CACHE_MAX:
        _mode_cache.clear()
    _mode_cache[key] = (stat.st_mtime_ns, stat.st_size, mode)
    return mode


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

def manifest_path() -> Path:
    return Path(config.HOME) / "agent_catalog.json"


def _load_manifest() -> Dict[str, Any]:
    path = manifest_path()
    if not path.is_file():
        return {"schema": MANIFEST_SCHEMA, "agents": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOG.debug("agent catalog manifest unreadable", exc_info=True)
        return {"schema": MANIFEST_SCHEMA, "agents": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("agents"), dict):
        return {"schema": MANIFEST_SCHEMA, "agents": {}}
    entries = {str(k): v for k, v in payload["agents"].items() if isinstance(v, dict)}
    # Keep sibling keys (the configured ceiling) instead of rebuilding the dict.
    out = dict(payload)
    out["schema"] = MANIFEST_SCHEMA
    out["agents"] = entries
    return out


def _save_manifest(payload: Dict[str, Any]) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.urandom(4).hex()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def _manifest_txn(*, save_on_error: bool = False):
    """Hold the lock across read-modify-write so two mutations cannot lose data.

    ``save_on_error`` is for the install loop: files already written must stay
    tracked even when a later one fails.
    """
    with _lock:
        payload = _load_manifest()
        try:
            yield payload
        except BaseException:
            if save_on_error:
                _save_manifest(payload)
            raise
        _save_manifest(payload)


LIMIT_MIN, LIMIT_MAX = 1, 1000


def configured_limit() -> int:
    """The operator's ceiling, defaulting to the upstream-reported ~119.

    The runtime accepted 120 on the machine this was measured on, so the number
    is advisory; an operator who has re-measured their build can raise it without
    waiting for a Perun release.
    """
    raw = _load_manifest().get("limit", DEFAULT_LIMIT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(LIMIT_MIN, min(LIMIT_MAX, value))


def set_limit(value: int) -> Dict[str, Any]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise CatalogError("the agent ceiling must be a whole number") from None
    if not LIMIT_MIN <= number <= LIMIT_MAX:
        raise CatalogError(
            f"the agent ceiling must be between {LIMIT_MIN} and {LIMIT_MAX}")
    with _manifest_txn() as manifest:
        manifest["limit"] = number
    return {"ok": True, "limit": number}


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def discover(project_path: str | Path | None = None) -> List[Dict[str, Any]]:
    """Every agent file Perun knows about, with its custody state.

    ``candidate`` — an installed roster agent Perun has not adopted yet;
    ``adopted``   — hash matches the recorded one;
    ``modified``  — hash differs (never deleted, never re-baselined automatically);
    ``unlisted``  — adopted, but the current index no longer describes it.
    """
    index = load_index()
    by_slug = index["by_slug"]
    manifest = _load_manifest()
    protected = protected_ids()
    out: List[Dict[str, Any]] = []

    for path in [*_md_files(agent_dirs(project_path)),
                 *_md_files([agent_assignments.library_dir()])]:
        key = _key(path)
        entry = manifest["agents"].get(key)
        slug = path.stem
        if slug.startswith("perun-") or slug in protected:
            continue
        meta = by_slug.get(slug) or {}
        try:
            digest = _hash(path)
        except OSError:  # pragma: no cover - vanished mid-scan
            continue
        if entry:
            mode_intent = str(entry.get("mode_intent") or "")
            if digest != str(entry.get("sha256") or ""):
                state = "modified"
            elif slug not in by_slug:
                state = "unlisted"
            else:
                state = "adopted"
        elif slug in by_slug:
            state, mode_intent = "candidate", ""
        else:
            continue

        out.append({
            "path": key,
            "slug": slug,
            "name": str(meta.get("name") or (entry or {}).get("name") or slug),
            "description": str(meta.get("description") or ""),
            "division": str((entry or {}).get("division") or meta.get("division") or ""),
            "color": str(meta.get("color") or ""),
            "emoji": str(meta.get("emoji") or ""),
            "state": state,
            "sha256": digest,
            "mode_intent": mode_intent,
            "adopted_at": str((entry or {}).get("adopted_at") or ""),
            "scope": "project" if project_path and _under(path, Path(project_path)) else "global",
            "assignment": agent_assignments.assignment_of(path),
        })

    out.sort(key=lambda a: (a["division"], a["slug"], a["path"]))
    return out


def _under(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def protected_ids() -> set[str]:
    """Agent ids Perun owns: never adopt, promote, overwrite or delete them."""
    from . import managed_agent

    try:
        ids = managed_agent.managed_ids()
    except Exception:  # pragma: no cover - the state file is best effort
        LOG.debug("could not read managed agent ids", exc_info=True)
        ids = set()
    return {str(i) for i in ids if i} | {"perunchat"}


def _assert_touchable(target: Path) -> None:
    if (target.stem.startswith("perun-")
            or target.stem in protected_ids()):
        raise CatalogError(
            f"{target.stem} is a Perun agent; the roster never touches it")


# --------------------------------------------------------------------------- #
# Runtime measurement
# --------------------------------------------------------------------------- #

def registered(project_path: str | Path | None = None, force: bool = False) -> Dict[str, Any]:
    """Ask the OpenCode runtime which agents it actually registered.

    ``opencode debug agents`` is authoritative in a way files on disk are not:
    it shows the silent drop past the ceiling. Cached briefly because the
    dashboard polls; the subprocess is ~200 ms and degrades to ``None`` when
    OpenCode is missing.
    """
    key = _key(project_path) if project_path else ""
    now = time.time()
    if (not force and _measure_cache["data"] is not None
            and _measure_cache["key"] == key
            and now - _measure_cache["ts"] < _MEASURE_TTL):
        return _measure_cache["data"]

    result: Dict[str, Any] = {"ok": False, "count": None, "ids": [], "error": ""}
    try:
        from .orchestrator import resolve_opencode

        argv = resolve_opencode("opencode") + ["debug", "agents"]
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
            cwd=str(project_path) if project_path else None,
        )
        if proc.returncode == 0:
            payload = json.loads(proc.stdout or "[]")
            ids = [str(a.get("id")) for a in payload
                   if isinstance(a, dict) and a.get("id")]
            result = {"ok": True, "count": len(ids), "ids": ids, "error": ""}
        else:
            result["error"] = (proc.stderr or "").strip()[:300] or "debug agents failed"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)[:300]

    _measure_cache.update(key=key, ts=now, data=result)
    return result


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

def slots(project_path: str | Path | None = None, measure: bool = True) -> Dict[str, Any]:
    """Installed files versus the runtime's registration ceiling.

    ``baseline`` is the part of the registered count that is *not* a roster file
    (built-ins, Perun's own agents, hand-written ones). A budget check has to add
    it, or installing 115 files would register 126 and silently drop 7.
    """
    global_files = _md_files(agent_dirs())
    project_files = _md_files(agent_dirs(project_path)) if project_path else []
    probe = registered(project_path) if measure else {"ok": False, "count": None, "ids": []}
    installed_ids = {p.stem for p in [*global_files, *project_files]}
    registered_ids = set(probe.get("ids") or [])
    registered_count = probe.get("count")
    overlap = len(installed_ids & registered_ids)
    ceiling = configured_limit()
    # The baseline is the registered agents that are *not* roster files (built-ins,
    # Perun's own agents, hand-written ones). Only trust it once the runtime
    # demonstrably sees at least one of our files: otherwise the runtime is
    # looking at another directory and the two counts describe different machines.
    baseline = (max(0, int(registered_count) - overlap)
                if registered_count is not None and installed_ids and overlap
                else None)
    unregistered = sorted(installed_ids - registered_ids) if probe.get("ok") else []
    return {
        "installed_global": len(global_files),
        "installed_project": len(project_files),
        "installed": len(installed_ids),
        "registered": registered_count,
        "registered_ok": bool(probe.get("ok")),
        "registered_error": probe.get("error") or "",
        "baseline": baseline,
        "baseline_source": "runtime" if baseline is not None else "files",
        "projected": (baseline + len(installed_ids)) if baseline is not None else len(installed_ids),
        "unregistered": unregistered,
        "limit": ceiling,
        "limit_source": LIMIT_SOURCE,
        "remaining": (ceiling - int(registered_count)) if registered_count is not None else None,
    }


def status(project_path: str | Path | None = None, measure: bool = True,
           agents: bool = True) -> Dict[str, Any]:
    """The catalogue payload; ``agents=False`` drops the per-agent list."""
    index = load_index()
    entries = discover(project_path)
    counts = {"adopted": 0, "modified": 0, "candidate": 0, "unlisted": 0}
    for entry in entries:
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    return {
        "ok": True,
        "index": {
            "available": index["available"],
            "source": index["source"],
            "commit": index["commit"],
            "agents": len(index["agents"]),
            "divisions": index["divisions"],
            "collisions": index["collisions"],
            "error": index["error"],
        },
        "slots": slots(project_path, measure=measure),
        "counts": counts,
        "agents": entries if agents else [],
        "agents_count": len(entries),
        "upstream": {
            "url": SOURCE_URL,
            "shell": _has_shell(),
            "hint": _install_hint(),
        },
    }


def _has_shell() -> bool:
    """Upstream's installer is bash-only; the desktop app is the fallback."""
    return bool(shutil.which("bash") or shutil.which("git"))


def _install_hint() -> str:
    if _has_shell():
        return ("./scripts/convert.sh --tool opencode && "
                "./scripts/install.sh --tool opencode --division <name>")
    return ("Upstream's installer needs bash (Git for Windows or WSL); "
            "otherwise use the Agency Agents desktop app.")


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #

def _resolve_target(path: str | Path, project_path: str | Path | None = None) -> Path:
    """Validate a target: a real ``.md`` file inside a scanned agent directory."""
    target = Path(path)
    if target.suffix.lower() != ".md":
        raise CatalogError(f"not an agent file: {target.name}")
    resolved = target.resolve()
    allowed = [d.resolve() for d in agent_dirs(project_path) if d.is_dir()]
    lib = agent_assignments.library_dir().resolve()
    if lib.is_dir():
        allowed.append(lib)
    if not any(resolved.parent == base for base in allowed):
        raise CatalogError(f"outside the agent directories: {target}")
    if not resolved.is_file():
        raise CatalogError(f"missing: {target}")
    return resolved


def adopt(paths: Optional[Iterable[str | Path]] = None,
          project_path: str | Path | None = None) -> Dict[str, Any]:
    """Record provenance for installed roster agents Perun did not install."""
    index = load_index()
    if not index["available"]:
        raise CatalogError("agent index unavailable")
    wanted = [Path(p) for p in paths] if paths else [
        Path(a["path"]) for a in discover(project_path) if a["state"] == "candidate"
    ]
    adopted: List[str] = []
    skipped: List[Dict[str, str]] = []
    protected = protected_ids()
    with _manifest_txn() as manifest:
        for path in wanted:
            try:
                target = _resolve_target(path, project_path)
            except CatalogError as exc:
                skipped.append({"path": str(path), "reason": str(exc)})
                continue
            if target.stem.startswith("perun-") or target.stem in protected:
                skipped.append({"path": str(target),
                                "reason": "a Perun agent; the roster never touches it"})
                continue
            meta = index["by_slug"].get(target.stem)
            if not meta:
                skipped.append({"path": str(target), "reason": "not in the agent index"})
                continue
            manifest["agents"][_key(target)] = {
                "source": "agency-agents",
                "commit": index["commit"],
                "division": meta["division"],
                "slug": target.stem,
                "sha256": _hash(target),
                "mode_intent": "",
                "adopted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            adopted.append(str(target))
    return {"ok": True, "adopted": adopted, "skipped": skipped}


def _entry(manifest: Dict[str, Any], target: Path) -> Dict[str, Any]:
    entry = manifest["agents"].get(str(target))
    if not entry:
        raise CatalogError(f"not adopted: {target.name}")
    return entry


def accept(path: str | Path) -> Dict[str, Any]:
    """Re-baseline a file the operator has edited (and wants to keep)."""
    target = _target_for(path)
    with _manifest_txn() as manifest:
        _assert_touchable(target)
        entry = _entry(manifest, target)
        if not target.is_file():
            raise CatalogError(f"missing: {target}")
        try:
            entry["sha256"] = _hash(target)
        except OSError as exc:
            raise CatalogError(f"could not read {target.name}: {exc}") from exc
        manifest["agents"][str(target)] = entry
    return {"ok": True, "path": str(target), "sha256": entry["sha256"]}


def forget(path: str | Path) -> Dict[str, Any]:
    """Drop the manifest entry and leave the file exactly where it is."""
    target = Path(path).resolve()
    with _manifest_txn() as manifest:
        _assert_touchable(target)
        _entry(manifest, target)
        manifest["agents"].pop(str(target), None)
    return {"ok": True, "forgotten": str(target)}


def _remove_locked(manifest: Dict[str, Any], path: str | Path, *,
                   backup_first: bool) -> str:
    """Delete one tracked, unmodified file inside an open manifest transaction."""
    target = Path(path).resolve()
    _assert_touchable(target)
    entry = _entry(manifest, target)
    if not target.is_file():
        raise CatalogError(f"missing: {target}")
    if _hash(target) != str(entry.get("sha256") or ""):
        raise CatalogError(
            "this agent was edited since it was adopted; forget it or accept the "
            "current content instead of deleting it")
    copy = backups.save_copy(target, label="agency") if backup_first else None
    target.unlink()
    _forget_hash(target)
    manifest["agents"].pop(str(target), None)
    return str(copy) if copy else ""


def remove(path: str | Path, *, backup_first: bool = True) -> Dict[str, Any]:
    """Delete an **unmodified** adopted file, after a backup."""
    target = Path(path).resolve()
    with _manifest_txn() as manifest:
        copy = _remove_locked(manifest, target, backup_first=backup_first)
    return {"ok": True, "removed": str(target), "backup": copy}


def _target_for(ref: str | Path) -> Path:
    """Resolve an agent reference (path or slug) to the file that owns it.

    Every owner operation goes through here so the storage can move without
    touching the API: today it returns the tracked file, and the assignment work
    redirects it to the library copy.
    """
    raw = str(ref).strip()
    if not raw:
        raise CatalogError("an agent path or slug is required")
    if os.sep in raw or (os.altsep and os.altsep in raw):
        return Path(raw).resolve()
    slug = raw[:-3] if raw.endswith(".md") else raw
    manifest = _load_manifest()
    lib = agent_assignments.library_dir()
    owner = ""
    fallback = ""
    for key, entry in manifest["agents"].items():
        if str(entry.get("slug") or "") != slug:
            continue
        if _under(Path(key), lib):
            owner = key
        elif not fallback:
            fallback = key
    if owner:
        return Path(owner)
    if fallback:
        return Path(fallback)
    for base in agent_dirs():
        candidate = base / f"{slug}.md"
        if candidate.is_file():
            return candidate.resolve()
    raise CatalogError(f"unknown agent: {slug}")


def promote(path: str | Path, mode: str = "all") -> Dict[str, Any]:
    """Make an adopted agent selectable as a session primary.

    Upstream writes ``mode: subagent``, which a Perun Run cannot pick. This is an
    explicit edit, so the file is backed up, the hash re-baselined and the intent
    remembered.
    """
    if mode not in {"all", "primary", "subagent"}:
        raise CatalogError(f"unsupported mode: {mode}")
    target = _target_for(path)
    with _manifest_txn() as manifest:
        _assert_touchable(target)
        entry = _entry(manifest, target)
        if not target.is_file():
            raise CatalogError(f"missing: {target}")
        if _hash(target) != str(entry.get("sha256") or ""):
            raise CatalogError(
                "this agent was edited since it was adopted; accept the current "
                "content before changing how it runs")
        backups.save_copy(target, label="agency")
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CatalogError(f"could not read {target.name}: {exc}") from exc
        tmp = target.with_suffix(f".{os.urandom(4).hex()}.tmp")
        try:
            tmp.write_text(_rewrite_mode(text, mode), encoding="utf-8", newline="\n")
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort
                pass
            raise CatalogError(f"could not rewrite {target.name}: {exc}") from exc
        _forget_hash(target)
        entry["sha256"] = _hash(target)
        entry["mode_intent"] = mode
        manifest["agents"][str(target)] = entry
    spread = agent_assignments.fan_out(target.stem, owner=target)
    return {"ok": True, "path": str(target), "mode": mode, "fan_out": spread}


def _rewrite_mode(text: str, mode: str) -> str:
    """Replace ``mode:`` inside the frontmatter, byte-identical otherwise."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return f"---\nmode: {mode}\n---\n" + text
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            break
        if line.split(":", 1)[0].strip().lower() == "mode":
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f"mode: {mode}{newline}"
            return "".join(lines)
    # No `mode:` yet: put it after `name:` when present, else first.
    insert_at = 1
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            break
        if line.split(":", 1)[0].strip().lower() == "name":
            insert_at = index + 1
            break
    lines.insert(insert_at, f"mode: {mode}\n")
    return "".join(lines)


def backup_modified(project_path: str | Path | None = None) -> Dict[str, Any]:
    """Copy every ``modified`` adopted file before the upstream installer runs."""
    copies: List[Dict[str, str]] = []
    for entry in discover(project_path):
        if entry["state"] != "modified":
            continue
        saved = backups.save_copy(entry["path"], label="agency")
        copies.append({"path": entry["path"], "backup": str(saved) if saved else ""})
    return {"ok": True, "copies": copies, "count": len(copies)}


def preflight(project_path: str | Path | None = None) -> Dict[str, Any]:
    """What a re-run of the upstream installer would overwrite right now.

    Perun cannot intercept a process it does not start, so this is advice: the
    installer copies over every matching slug without a backup.
    """
    entries = discover(project_path)
    adopted = [a for a in entries if a["state"] in {"adopted", "modified", "unlisted"}]
    modified = [a for a in adopted if a["state"] == "modified"]
    return {
        "ok": True,
        "will_overwrite": [a["path"] for a in adopted],
        "modified": [a["path"] for a in modified],
        "backup_first": bool(modified),
        "message": (
            f"{len(adopted)} adopted agent(s) would be overwritten; "
            f"{len(modified)} of them were edited since adoption"
        ),
    }


def team_file(divisions: Optional[Iterable[str]] = None,
              limit: Optional[int] = None) -> Dict[str, Any]:
    """A plain ``--agents-file`` list, refused when it would exceed the ceiling."""
    index = load_index()
    if not index["available"]:
        raise CatalogError("agent index unavailable")
    wanted = {d for d in (divisions or []) if d}
    selected = [a for a in index["agents"]
                if not wanted or a["division"] in wanted]
    selected.sort(key=lambda a: (a["division"], a["slug"]))
    ceiling = int(limit or configured_limit())
    if len(selected) > ceiling:
        raise CatalogError(
            f"{len(selected)} agents exceed the {ceiling}-agent ceiling; "
            f"narrow the divisions (available: {', '.join(sorted(index['divisions']))})")
    return {
        "ok": True,
        "count": len(selected),
        "limit": ceiling,
        "divisions": sorted(wanted & set(index["divisions"])),
        "agents": [a["slug"] for a in selected],
        "text": "\n".join(a["slug"] for a in selected) + "\n",
    }


# --------------------------------------------------------------------------- #
# Acquisition (explicit and pinned)
#
# This is the one path where Perun writes an agent file. It installs from the
# *pinned index commit*, converts with upstream's rules, and refuses to touch
# anything it does not already track. Upstream's own installer still works; its
# output is adopted instead.
# --------------------------------------------------------------------------- #

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Upstream's slug rule: it becomes the filename, so it must match exactly."""
    return _SLUG_RE.sub("-", (name or "").lower()).strip("-")


def resolve_color(value: str) -> str:
    """Upstream's ``resolve_opencode_color`` (CSS names, Tailwind 500, fallback)."""
    raw = (value or "").strip().lower()
    mapped = NAMED_COLORS.get(raw, raw)
    if re.fullmatch(r"#[0-9a-fA-F]{6}", mapped):
        return "#" + mapped[1:].upper()
    if re.fullmatch(r"[0-9a-fA-F]{6}", mapped):
        return "#" + mapped.upper()
    return FALLBACK_COLOR


def _yaml_quote(value: str) -> str:
    """Single-quoted YAML: keeps colons, hashes and Unicode literal."""
    return "'" + (value or "").replace("'", "''") + "'"


def parse_agent(text: str) -> tuple[Dict[str, str], str]:
    """Split an upstream agent file into frontmatter fields and body."""
    lines = text.splitlines()
    fields: Dict[str, str] = {}
    body_start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() != "---":
                continue
            for line in lines[1:index]:
                key, sep, value = line.partition(":")
                if not sep:
                    continue
                key = key.strip().lower()
                if key in {"name", "description", "color", "emoji"}:
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    fields[key] = value
            body_start = index + 1
            break
    return fields, "\n".join(lines[body_start:]).strip("\n")


def render_agent(fields: Dict[str, str], body: str, *, mode: str = "subagent") -> str:
    """Upstream's OpenCode output: quoted scalars, hex colour, body verbatim."""
    name = (fields.get("name") or "").strip()
    description = " ".join((fields.get("description") or "").split())
    if not name or not description:
        raise CatalogError("agent file has no name/description")
    return (
        "---\n"
        f"name: {_yaml_quote(name)}\n"
        f"description: {_yaml_quote(description)}\n"
        f"mode: {mode}\n"
        f"color: '{resolve_color(fields.get('color', ''))}'\n"
        "---\n"
        f"{body}\n"
    )


def archive_path(ref: str) -> Path:
    safe = "".join(c for c in (ref or "") if c.isalnum() or c in "-_.")[:60] or "main"
    return Path(config.HOME) / "agent_catalog" / "agency" / f"{safe}.tar.gz"


def roster_archive(ref: str, *, force: bool = False) -> Path:
    """The roster tarball for *ref*, cached so a re-install (and offline) works."""
    path = archive_path(ref)
    if path.is_file() and not force:
        return path
    request = urllib.request.Request(
        TARBALL_URL.format(ref=ref), headers={"User-Agent": "perun-agent-catalog"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            chunks: List[bytes] = []
            total = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _ARCHIVE_MAX_BYTES:
                    raise CatalogError("roster archive is unreasonably large")
                chunks.append(chunk)
    except (urllib.error.URLError, OSError) as exc:
        if path.is_file():
            LOG.warning("roster download failed (%s); using the cached snapshot", exc)
            return path
        raise CatalogError(
            "could not download the roster; install with upstream's script and adopt "
            f"the result instead ({exc})") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.urandom(4).hex()}.tmp")
    tmp.write_bytes(b"".join(chunks))
    os.replace(tmp, path)
    return path


def _read_bodies(path: Path, wanted: Dict[str, str]) -> Dict[str, str]:
    """``slug -> raw markdown`` for the archive members the index points at."""
    out: Dict[str, str] = {}
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            for item in tar.getmembers():
                if not item.isfile() or not item.name.endswith(".md"):
                    continue
                parts = item.name.split("/")
                if len(parts) < 3:
                    continue
                slug = wanted.get("/".join(parts[1:]))
                if not slug or slug in out:
                    continue
                # A roster agent is a few KB; the cap only bounds a corrupt or
                # hostile archive (the compressed download is capped separately).
                if item.size > AGENT_MAX_BYTES:
                    LOG.warning("skipping oversized roster member: %s", item.name)
                    continue
                handle = tar.extractfile(item)
                if handle is None:
                    continue
                out[slug] = handle.read().decode("utf-8", errors="replace")
    except (tarfile.TarError, OSError) as exc:
        raise CatalogError(f"roster archive unreadable: {exc}") from exc
    return out


def install(divisions: Optional[Iterable[str]] = None,
            agents: Optional[Iterable[str]] = None, *,
            scope: str = "global",  # product surfaces pass "library" (unassigned)
            project_path: str | Path | None = None,
            limit: Optional[int] = None) -> Dict[str, Any]:
    """Install roster agents from the pinned index commit.

    Refuses to clobber a file Perun does not track, or one edited since install.
    """
    index = load_index()
    if not index["available"]:
        raise CatalogError("agent index unavailable; regenerate it with tools/agency_index.py")
    wanted_divs = {d for d in (divisions or []) if d}
    wanted_slugs = {s for s in (agents or []) if s}
    if wanted_slugs:
        selected = [a for a in index["agents"] if a["slug"] in wanted_slugs]
    elif wanted_divs:
        selected = [a for a in index["agents"] if a["division"] in wanted_divs]
    else:
        selected = []
    if not selected:
        raise CatalogError("no agents selected")

    if scope not in {"library", "global", "project"}:
        raise CatalogError(f"unsupported scope: {scope}")
    if scope == "project" and not project_path:
        raise CatalogError("a project is required for a project-scoped install")
    # The library is the master copy: OpenCode never reads it, so nothing is
    # registered until the operator assigns the agent somewhere.
    base = (agent_assignments.library_dir() if scope == "library"
            else (Path(project_path) / ".opencode" / "agents" if scope == "project"
                  else Path(config.OPENCODE_CONFIG_DIR) / "agents"))

    existing = {p.stem for p in _md_files(agent_dirs(project_path))}
    global_stems = {p.stem for p in _md_files(agent_dirs())}
    new_slugs = {a["slug"] for a in selected} - existing
    planned = existing | new_slugs
    ceiling = int(configured_limit() if limit is None else limit)

    # The ceiling is about *registered* agents, and the runtime also registers
    # built-ins and Perun's own agents. Counting only files is optimistic by
    # exactly that baseline, which is the silent drop this guard exists to stop.
    if scope == "library":
        budget = {"baseline": None}
    else:
        budget = slots(project_path, measure=True)
    baseline = budget["baseline"]
    projected = len(planned) + (baseline or 0)
    if scope != "library" and projected > ceiling:
        detail = (f"{baseline} non-roster + {len(planned)} roster"
                  if baseline is not None
                  else f"{len(planned)} roster; no runtime baseline applied")
        raise CatalogError(
            f"{projected} agents would be registered, over the {ceiling}-agent "
            f"ceiling ({detail}: {len(existing)} installed, {len(new_slugs)} new); "
            f"narrow the divisions, or raise the ceiling with "
            f"`perun agent-catalog limit {projected}`")

    raw_bodies = _read_bodies(
        roster_archive(index["commit"]), {a["path"]: a["slug"] for a in selected})
    base.mkdir(parents=True, exist_ok=True)
    installed: List[str] = []
    skipped: List[Dict[str, str]] = []
    protected = protected_ids()
    with _manifest_txn(save_on_error=True) as manifest:
        for agent in selected:
            raw = raw_bodies.get(agent["slug"])
            if raw is None:
                skipped.append({"path": agent["path"], "reason": "missing from the roster archive"})
                continue
            if agent["slug"].startswith("perun-") or agent["slug"] in protected:
                skipped.append({"path": agent["slug"],
                                "reason": "a Perun agent; the roster never touches it"})
                continue
            if scope == "project" and agent["slug"] in global_stems:
                skipped.append({"path": agent["slug"],
                                "reason": "already installed globally"})
                continue
            target = base / f"{agent['slug']}.md"
            key = _key(target)
            entry = manifest["agents"].get(key)
            if target.exists():
                if not entry:
                    skipped.append({"path": str(target),
                                    "reason": "exists and is not tracked by Perun"})
                    continue
                if _hash(target) != str(entry.get("sha256") or ""):
                    skipped.append({"path": str(target),
                                    "reason": "edited since it was installed"})
                    continue
                # An upgrade replaces the file; keep the previous bytes.
                backups.save_copy(target, label="agency")
            try:
                text = render_agent(*parse_agent(raw))
            except CatalogError as exc:
                skipped.append({"path": agent["path"], "reason": str(exc)})
                continue
            tmp = target.with_suffix(f".{os.urandom(4).hex()}.tmp")
            try:
                tmp.write_text(text, encoding="utf-8", newline="\n")
                os.replace(tmp, target)
            except OSError as exc:
                # A locked file (an open editor) is a skip, not a failed install.
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:  # pragma: no cover - best effort
                    pass
                skipped.append({"path": str(target),
                                "reason": f"could not be written: {exc}"})
                continue
            _forget_hash(target)
            manifest["agents"][key] = {
                "source": "agency-agents",
                "commit": index["commit"],
                "division": agent["division"],
                "slug": agent["slug"],
                "sha256": _hash(target),
                "mode_intent": "subagent",
                "installed_by": "perun",
                "adopted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            installed.append(str(target))
    return {"ok": True, "commit": index["commit"], "scope": scope,
            "installed": installed, "skipped": skipped,
            "count": len(installed), "skipped_count": len(skipped),
            "projected": projected, "limit": ceiling}


def installed(project_path: str | Path | None = None) -> Dict[str, Any]:
    """One row per agent: custody state, mode and where it is assigned."""
    rows: Dict[str, Dict[str, Any]] = {}
    for entry in discover(project_path):
        if entry["state"] not in {"adopted", "modified", "unlisted", "candidate"}:
            continue
        row = rows.setdefault(entry["slug"], {
            "slug": entry["slug"], "division": entry["division"],
            "state": "adopted", "mode": "", "path": entry["path"],
            "assignments": [], "paths": {}})
        assignment = entry.get("assignment") or "global"
        if assignment not in row["assignments"]:
            row["assignments"].append(assignment)
        row["paths"][assignment] = entry["path"]
        row["mode"] = _agent_mode(Path(entry["path"]))
        if entry["state"] == "modified" or row["state"] == "modified":
            row["state"] = "modified"
        elif entry["state"] == "candidate" and row["state"] != "modified":
            row["state"] = "candidate"

    ordered = sorted(rows.values(), key=lambda r: (r["division"], r["slug"]))
    by_division: Dict[str, int] = {}
    for row in ordered:
        key = row["division"] or "other"
        by_division[key] = by_division.get(key, 0) + 1
    unassigned = sorted(row["slug"] for row in ordered
                        if set(row["assignments"]) == {"library"})
    return {"ok": True, "count": len(ordered), "agents": ordered,
            "by_division": by_division, "unassigned": unassigned}


def remove_many(paths: Optional[Iterable[str | Path]] = None, *,
                division: str = "",
                project_path: str | Path | None = None,
                backup_first: bool = True,
                dry_run: bool = False) -> Dict[str, Any]:
    """Uninstall by explicit paths or by division; one refusal never stops the rest."""
    if paths:
        targets = [str(p) for p in paths]
    elif division:
        # Only the scope the caller named: asking for a project's division must
        # not also delete the machine-wide copies.
        wanted_scope = "project" if project_path else "global"
        targets = [entry["path"] for entry in discover(project_path)
                   if entry["division"] == division
                   and entry["state"] in {"adopted", "modified", "unlisted"}
                   and entry["scope"] == wanted_scope]
    else:
        raise CatalogError("choose agents (paths) or a division to remove")
    if not targets:
        raise CatalogError(f"nothing installed in {division or 'that selection'}")
    if dry_run:
        return {"ok": True, "dry_run": True, "would_remove": targets,
                "removed": [], "skipped": [], "count": 0, "skipped_count": 0}

    removed: List[str] = []
    skipped: List[Dict[str, str]] = []
    # One transaction for the whole batch: 66 agents are one manifest write, and
    # a failure midway keeps every earlier removal.
    with _manifest_txn() as manifest:
        for target in targets:
            try:
                _remove_locked(manifest, target, backup_first=backup_first)
                removed.append(str(Path(target).resolve()))
            except (CatalogError, OSError) as exc:
                skipped.append({"path": target, "reason": str(exc)})
    return {"ok": True, "removed": removed, "skipped": skipped,
            "count": len(removed), "skipped_count": len(skipped)}


def available(division: str = "",
              project_path: str | Path | None = None) -> Dict[str, Any]:
    """What the index offers, with what is already on disk marked."""
    index = load_index()
    if not index["available"]:
        raise CatalogError("agent index unavailable")
    present = _md_files([agent_assignments.library_dir(), *agent_dirs(project_path)])
    installed = {p.stem for p in present}
    agents = [dict(agent, installed=agent["slug"] in installed)
              for agent in index["agents"]
              if not division or agent["division"] == division]
    agents.sort(key=lambda a: (a["division"], a["slug"]))
    return {"ok": True, "commit": index["commit"], "count": len(agents),
            "divisions": index["divisions"], "agents": agents}
