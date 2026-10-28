"""Read/write the user-level OpenCode configuration.

OpenCode's own config is the single source of truth for providers, models and
MCP servers. Perun does **not** store provider credentials itself; it only edits
the user's ``opencode.json[c]`` so the OpenCode CLI/server pick up the change.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import config


def _creationflags() -> int:
    """Hide console windows for background ``opencode`` subprocesses on Windows."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _config_file() -> Path:
    """The OpenCode config file Perun should edit.

    Honours ``OPENCODE_CONFIG`` (the same variable OpenCode itself reads) so
    dashboard edits and the running OpenCode instance can never disagree about
    which file is the real one.
    """
    env = (os.environ.get("OPENCODE_CONFIG") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    base = config.OPENCODE_CONFIG_DIR
    for name in ("opencode.json", "opencode.jsonc"):
        p = base / name
        if p.is_file():
            return p
    return base / "opencode.json"


def global_config_dirs() -> list[Path]:
    """Return OpenCode config directories to read from, in precedence order.

    Perun historically writes to ``~/.config/opencode`` (or ``XDG_CONFIG_HOME``).
    OpenCode's desktop/TUI builds on Windows may store config under the
    APPDATA/LOCALAPPDATA opencode directories instead. To avoid missing
    providers, MCP servers, or other config the user added through the TUI,
    we read from every known location and merge. The first directory is still
    the one Perun writes to, so existing installs are not disrupted.
    """
    # Use the canonical config file's parent so tests that patch _config_file()
    # do not accidentally pick up the real user config.
    primary = _config_file().parent
    dirs: list[Path] = [primary]
    if os.name == "nt":
        for env_var in ("APPDATA", "LOCALAPPDATA"):
            val = (os.environ.get(env_var) or "").strip()
            if val:
                candidate = Path(val).expanduser() / "opencode"
                if candidate.resolve() not in {d.resolve() for d in dirs}:
                    dirs.append(candidate)
    return dirs


def global_config_files() -> list[Path]:
    """Return all existing global OpenCode config files across known dirs."""
    files: list[Path] = []
    seen: set[Path] = set()
    for base in global_config_dirs():
        for name in ("opencode.json", "opencode.jsonc"):
            p = (base / name).resolve()
            if p.is_file() and p not in seen:
                files.append(p)
                seen.add(p)
    return files


# Keep older private names for backwards compatibility inside this module.
_global_config_dirs = global_config_dirs
_global_config_files = global_config_files


# --------------------------------------------------------------------------- #
# JSON / JSONC helpers
# --------------------------------------------------------------------------- #
def _strip_jsonc(text: str) -> str:
    """Drop ``//`` / ``/* */`` comments and trailing commas from JSONC.

    Mirrors the logic in :mod:`perun.mcp_servers` so both modules parse
    OpenCode config the same way.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    escaped = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _read_json(path: Path) -> dict[str, Any]:
    """Parse a JSON/JSONC file, returning an empty dict when it does not exist."""
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8")
    last: json.JSONDecodeError | None = None
    for candidate in (raw, _strip_jsonc(raw)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last = exc
            continue
        if isinstance(data, dict):
            return data
        return {}
    raise ValueError(f"{path} is not valid JSON/JSONC: {last}")


def _backup(path: Path) -> Path | None:
    """Create a timestamped backup next to *path* before we rewrite it."""
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_suffix(path.suffix + f".perun-backup-{stamp}")
    counter = 2
    while dest.exists():
        dest = path.with_suffix(path.suffix + f".perun-backup-{stamp}-{counter}")
        counter += 1
    shutil.copy2(path, dest)
    return dest


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Public load/save
# --------------------------------------------------------------------------- #
def load_config(path: Path | None = None) -> tuple[dict[str, Any], Path, bool]:
    """Return ``(data, path, had_comments)`` for the user's OpenCode config.

    ``had_comments`` is ``True`` when the file contained JSONC comments or
    trailing commas that will be lost on rewrite.
    """
    target = path or _config_file()
    try:
        if not target.is_file():
            return {}, target, False
        raw = target.read_text(encoding="utf-8")
        data = _read_json(target)
        stripped = _strip_jsonc(raw)
        # Normalise whitespace so we only flag real content differences.
        had_comments = " ".join(stripped.split()) != " ".join(raw.split())
        return data, target, had_comments
    except Exception as exc:
        raise RuntimeError(f"Cannot read OpenCode config {target}: {exc}") from exc


def save_config(data: dict[str, Any], path: Path | None = None) -> Path:
    """Write *data* back to the user's OpenCode config, backing up first."""
    path = path or _config_file()
    _backup(path)
    _write_json(path, data)
    return path


def providers_section(data: dict[str, Any]) -> dict[str, Any] | list[Any]:
    """Return the provider map/list, honouring both legacy ``provider`` and V2 ``providers``."""
    for key in ("providers", "provider"):
        val = data.get(key)
        if isinstance(val, (dict, list)):
            return val
    return {}


def set_providers_section(data: dict[str, Any], providers: dict[str, Any]) -> None:
    """Write the provider map under the canonical ``providers`` key.

    Drops the legacy ``provider`` key so OpenCode does not see a duplicate.
    """
    data["providers"] = providers
    data.pop("provider", None)


# --------------------------------------------------------------------------- #
# Provider helpers
# --------------------------------------------------------------------------- #
def configured_providers() -> tuple[list[dict[str, Any]], bool]:
    """Return ``(providers, had_comments)`` for the OpenCode config.

    Tolerates both the canonical dict form (``providers: {"id": {...}}``) and
    list forms (``providers: [{"id": "...", ...}]``) that some OpenCode TUI
    flows produce. Providers are merged from all known global config directories
    so entries added by the OpenCode desktop/TUI (which may use a different
    platform-specific config dir) are still visible in Perun.
    """
    had_comments = False
    seen_ids: set[str] = set()
    out: list[dict[str, Any]] = []

    def _parse_entry(entry: Any, source_path: Path, fallback_id: str = "") -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        pid = (
            entry.get("id")
            or entry.get("providerId")
            or entry.get("provider")
            or fallback_id
        )
        if not pid or not isinstance(pid, str):
            return None
        name = entry.get("name") or pid
        settings = entry.get("settings") or entry.get("options") or {}
        if not isinstance(settings, dict):
            settings = {}
        # API key / base URL may live either under ``settings`` or at the top
        # level of the provider entry depending on the OpenCode version.
        api_key = settings.get("apiKey") or entry.get("apiKey") or entry.get("api_key")
        base_url = settings.get("baseURL") or entry.get("baseURL") or entry.get("base_url")
        return {
            "id": pid,
            "name": name,
            "configured": True,
            "has_key": bool(api_key),
            "base_url": base_url or "",
            "source": str(source_path),
        }

    for path in global_config_files():
        try:
            data, _, file_had_comments = load_config(path)
        except Exception:
            continue
        if file_had_comments:
            had_comments = True
        section = providers_section(data)
        if isinstance(section, dict):
            for pid, entry in section.items():
                parsed = _parse_entry(entry, path, fallback_id=pid)
                if parsed is not None and parsed["id"] not in seen_ids:
                    seen_ids.add(parsed["id"])
                    out.append(parsed)
        elif isinstance(section, list):
            for entry in section:
                parsed = _parse_entry(entry, path)
                if parsed is not None and parsed["id"] not in seen_ids:
                    seen_ids.add(parsed["id"])
                    out.append(parsed)
    return out, had_comments


def configure_provider(
    provider_id: str,
    fields: dict[str, Any],
    *,
    reload: bool = True,
) -> dict[str, Any]:
    """Create or update a provider entry in the OpenCode config.

    *fields* may contain ``name``, ``package``, ``apiKey``, ``baseURL`` and
    ``models`` (a list of model ids). The config file is backed up before
    writing and OpenCode is asked to reload its configuration.
    """
    if not provider_id or not isinstance(provider_id, str):
        raise ValueError("provider id is required")
    data, path, had_comments = load_config()
    providers = dict(providers_section(data))
    entry = dict(providers.get(provider_id) or {})

    name = (fields.get("name") or "").strip()
    if name:
        entry["name"] = name

    package = (fields.get("package") or "").strip()
    if package:
        entry["package"] = package

    settings: dict[str, Any] = {}
    old_settings = entry.get("settings") or entry.get("options") or {}
    if isinstance(old_settings, dict):
        settings.update(old_settings)
    for key in ("apiKey", "baseURL"):
        val = (fields.get(key) or "").strip()
        if val:
            settings[key] = val
    if settings:
        entry["settings"] = settings
        entry.pop("options", None)

    models = fields.get("models")
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    if isinstance(models, list):
        entry.setdefault("models", {})
        if not isinstance(entry["models"], dict):
            entry["models"] = {}
        for mid in models:
            if mid and isinstance(mid, str):
                entry["models"][mid] = entry["models"].get(mid) or {"name": mid}

    providers[provider_id] = entry
    set_providers_section(data, providers)
    save_config(data, path)
    reload_status = reload_opencode() if reload else {"ok": None}
    return {
        "provider_id": provider_id,
        "reloaded": reload_status.get("ok"),
        "path": str(path),
        "had_comments": had_comments,
    }


def remove_provider(provider_id: str, *, reload: bool = True) -> dict[str, Any]:
    """Remove a provider entry from the OpenCode config."""
    data, path, had_comments = load_config()
    providers = dict(providers_section(data))
    if provider_id not in providers:
        return {"removed": False, "path": str(path), "had_comments": had_comments}
    del providers[provider_id]
    set_providers_section(data, providers)
    save_config(data, path)
    reload_status = reload_opencode() if reload else {"ok": None}
    return {"removed": True, "reloaded": reload_status.get("ok"), "path": str(path), "had_comments": had_comments}


# --------------------------------------------------------------------------- #
# OpenCode reload
# --------------------------------------------------------------------------- #
def reload_opencode(timeout: float = 10.0) -> dict[str, Any]:
    """Ask the OpenCode background service to reload its configuration.

    Best-effort: failures are returned, not raised, so a disconnected or
    missing OpenCode service never blocks the dashboard flow.
    """
    exe = shutil.which("opencode") or "opencode"
    try:
        proc = subprocess.run(
            [exe, "reload"],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            creationflags=_creationflags(),
        )
        return {"ok": proc.returncode == 0, "stdout": proc.stdout, "stderr": proc.stderr}
    except Exception as exc:  # pragma: no cover - best effort
        return {"ok": False, "error": str(exc)}
