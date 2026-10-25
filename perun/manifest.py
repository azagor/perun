"""Perun project manifest — self-describing metadata that lives in the repo.

The canonical manifest is ``perun.md``: a Markdown file whose **YAML
front-matter** block holds the structured data the dashboard needs (identity,
tags, links, docs pointers, commands, and *anchors* into project content),
followed by a free-form Markdown body that is rendered as the project overview.

A plain ``perun.json`` is also accepted for projects that prefer JSON.

Design rules
------------
* The manifest is **declarative input**. Perun reads it into the registry when a
  project is discovered/indexed; the dashboard never silently writes to your
  working tree (there is an explicit *export* action instead).
* Unknown keys are **preserved**, so the format stays forward-compatible.
* Parsing is tolerant: a malformed or missing manifest degrades gracefully to
  discovery heuristics and reports warnings instead of failing.
* Paths are expected to be **relative to the project root**.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MANIFEST_MD = "perun.md"
MANIFEST_JSON = "perun.json"
MANIFEST_NAMES = (MANIFEST_MD, MANIFEST_JSON)
SCHEMA_VERSION = 1

ANCHOR_KINDS = ("file", "doc", "url", "command", "symbol", "secret")

_FENCE = re.compile(r"^---\s*$")
_STR_KEYS = ("name", "slug", "kind", "description", "model", "agent", "instructions")
_MAP_KEYS = ("docs", "commands", "links")


class ManifestError(Exception):
    """A user-facing failure while reading or writing a manifest."""


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def find(project_path: str | Path) -> Path | None:
    base = Path(project_path)
    if not base.is_dir():
        return None
    for name in MANIFEST_NAMES:
        f = base / name
        if f.is_file():
            return f
    return None


def _split_front_matter(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or not _FENCE.match(lines[0].strip()):
        return "", text
    for i in range(1, len(lines)):
        if _FENCE.match(lines[i].strip()):
            front = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:]).strip("\n")
            return front, body
    # Unterminated front matter → treat the whole thing as body.
    return "", text


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in value.items():
        if v is None:
            continue
        key = str(k).strip()
        if not key:
            continue
        out[key] = str(v).strip() if not isinstance(v, (dict, list)) else json.dumps(v)
    return out


def _normalize_anchors(value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    out: list[dict[str, Any]] = []
    if value is None:
        return out, warnings
    if not isinstance(value, list):
        return out, ["anchors must be a list"]
    for i, item in enumerate(value, 1):
        if isinstance(item, str):
            item = {"label": item, "path": item}
        if not isinstance(item, dict):
            warnings.append(f"anchor #{i} is not an object")
            continue
        label = str(item.get("label") or item.get("name") or item.get("path")
                    or item.get("url") or item.get("command") or "").strip()
        if not label:
            warnings.append(f"anchor #{i} has no label")
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if not kind:
            if item.get("url"):
                kind = "url"
            elif item.get("command"):
                kind = "command"
            elif item.get("symbol"):
                kind = "symbol"
            elif str(item.get("path", "")).lower().endswith((".md", ".rst", ".txt")):
                kind = "doc"
            else:
                kind = "file"
        anchor: dict[str, Any] = {"label": label, "kind": kind}
        for key in ("path", "url", "command", "symbol", "section", "description",
                    "memory"):
            if item.get(key) not in (None, ""):
                anchor[key] = str(item[key])
        # A command anchor may name a command defined in `commands`.
        if kind == "command" and not anchor.get("command") and anchor.get("path"):
            anchor["command"] = anchor.pop("path")
        if kind in ("file", "doc", "symbol") and not anchor.get("path"):
            warnings.append(f"anchor '{label}' ({kind}) is missing 'path'")
            continue
        if kind == "secret" and not anchor.get("path"):
            warnings.append(f"anchor '{label}' (secret) is missing 'path'")
            continue
        if kind == "url" and not anchor.get("url"):
            warnings.append(f"anchor '{label}' (url) is missing 'url'")
            continue
        if kind == "command" and not anchor.get("command"):
            warnings.append(f"anchor '{label}' (command) is missing 'command'")
            continue
        if kind not in ANCHOR_KINDS:
            warnings.append(f"anchor '{label}' has unknown kind '{kind}'")
            continue
        out.append(anchor)
    return out, warnings


def normalize(data: Any) -> tuple[dict[str, Any], list[str]]:
    """Return a validated manifest dict and a list of human-readable warnings."""
    if not isinstance(data, dict):
        return {"version": SCHEMA_VERSION, "tags": [], "docs": {}, "commands": {},
                "links": {}, "anchors": [], "git": {}}, ["manifest must be a mapping"]
    warnings: list[str] = []
    out = dict(data)  # preserve unknown keys for forward compatibility

    try:
        out["version"] = int(out.get("version", SCHEMA_VERSION))
    except (TypeError, ValueError):
        out["version"] = SCHEMA_VERSION
        warnings.append("version must be an integer")

    for key in _STR_KEYS:
        if key in out and out[key] is not None and not isinstance(out[key], str):
            out[key] = str(out[key])
            warnings.append(f"{key} coerced to a string")

    out["tags"] = _string_list(out.get("tags"))
    for key in _MAP_KEYS:
        out[key] = _string_map(out.get(key))

    anchors, aw = _normalize_anchors(out.get("anchors"))
    out["anchors"] = anchors
    warnings += aw

    git = out.get("git")
    out["git"] = _string_map(git) if isinstance(git, dict) else {}

    try:
        from . import shared_projects

        out, sw = shared_projects.normalize(out)
        warnings += sw
    except Exception:  # pragma: no cover - shared_projects import should not break parsing
        pass
    return out, warnings


def parse(text: str) -> tuple[dict[str, Any], str, list[str]]:
    """Parse a Markdown (front-matter) manifest into (data, body, warnings)."""
    import yaml  # PyYAML is a declared dependency; imported lazily.

    front, body = _split_front_matter(text)
    warnings: list[str] = []
    loaded: Any = None
    if front.strip():
        try:
            loaded = yaml.safe_load(front)
        except yaml.YAMLError as exc:  # pragma: no cover - malformed input
            warnings.append(f"invalid YAML front matter: {exc}")
            loaded = None
        if loaded is not None and not isinstance(loaded, dict):
            warnings.append("front matter is not a mapping; ignored")
            loaded = None
    data, nw = normalize(loaded or {})
    return data, body, warnings + nw


def parse_json(text: str) -> tuple[dict[str, Any], str, list[str]]:
    warnings: list[str] = []
    loaded: Any = None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        warnings.append(f"invalid JSON: {exc}")
    body = ""
    if isinstance(loaded, dict):
        body = str(loaded.pop("body", "") or "")
    data, nw = normalize(loaded if isinstance(loaded, dict) else {})
    return data, body, warnings + nw


def load(project_path: str | Path) -> dict[str, Any]:
    """Read the manifest for a project directory.

    Returns ``{found, path, data, body, warnings}``; never raises for a missing
    or malformed manifest.
    """
    f = find(project_path)
    if not f:
        return {"found": False, "path": "", "data": {}, "body": "", "warnings": []}
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"found": False, "path": str(f), "data": {}, "body": "",
                "warnings": [f"could not read manifest: {exc}"]}
    if f.suffix.lower() == ".json":
        data, body, warnings = parse_json(text)
    else:
        data, body, warnings = parse(text)
    return {"found": True, "path": str(f), "data": data, "body": body,
            "warnings": warnings}


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def dump(data: dict[str, Any], body: str = "") -> str:
    """Serialise a manifest dict to ``perun.md`` text.

    Empty defaults (``{}`` / ``[]`` / ``""``) are omitted so the generated file
    stays readable; ``version`` is always kept.
    """
    import yaml

    data, _ = normalize(data)
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if key != "version" and value in ({}, [], "", None):
            continue
        clean[key] = value
    front = yaml.safe_dump(clean, sort_keys=False, allow_unicode=True,
                           default_flow_style=False).strip()
    text = f"---\n{front}\n---\n"
    body = (body or "").strip()
    if body:
        text += f"\n{body}\n"
    return text


def save(project_path: str | Path, data: dict[str, Any], body: str = "") -> Path:
    """Write a manifest to disk.

    If the project already uses a ``perun.json`` manifest, that format is
    preserved (the Markdown body is stored under a ``body`` key); otherwise a
    ``perun.md`` is written.
    """
    base = Path(project_path)
    if not base.is_dir():
        raise ManifestError(f"path does not exist: {base}")
    existing = find(base)
    try:
        if existing is not None and existing.suffix.lower() == ".json":
            payload, _ = normalize(data)
            if body.strip():
                payload["body"] = body.strip()
            existing.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8")
            return existing
        target = base / MANIFEST_MD
        target.write_text(dump(data, body), encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"could not write manifest in {base}: {exc}")
    return target


# --------------------------------------------------------------------------- #
# Registry integration
# --------------------------------------------------------------------------- #
def snapshot(manifest: dict[str, Any]) -> str:
    """Compact JSON stored in the ``projects.manifest`` column."""
    return json.dumps({
        "data": manifest.get("data") or {},
        "body": manifest.get("body") or "",
        "source": manifest.get("path") or "",
    }, ensure_ascii=False)


def to_project_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Map manifest data onto registry/DB project columns (declared fields only)."""
    data, _ = normalize(data)
    fields: dict[str, Any] = {}
    for key in ("name", "kind", "description", "model", "agent", "instructions"):
        value = data.get(key)
        if value not in (None, ""):
            fields[key] = value
    if data.get("tags"):
        fields["tags"] = ",".join(data["tags"])
    git = data.get("git") or {}
    if git.get("remote"):
        fields["git_remote"] = git["remote"]
    if git.get("branch"):
        fields["git_branch"] = git["branch"]
    return fields


def from_project(project: dict[str, Any]) -> dict[str, Any]:
    """Build manifest data from a registry project row (used by export)."""
    data: dict[str, Any] = {"version": SCHEMA_VERSION}
    for key in ("name", "slug", "kind", "description", "model", "agent"):
        value = project.get(key)
        if value:
            data[key] = value
    tags = [t for t in (project.get("tags") or "").split(",") if t.strip()]
    if tags:
        data["tags"] = tags
    git: dict[str, str] = {}
    if project.get("git_remote"):
        git["remote"] = project["git_remote"]
    if project.get("git_branch"):
        git["branch"] = project["git_branch"]
    if git:
        data["git"] = git

    # Suggest anchors for the well-known docs when they exist on disk.
    base = Path(project.get("path") or "")
    docs = {"readme": "README.md", "agents": "AGENTS.md",
            "changelog": "docs/CHANGELOG.md",
            "technical": "docs/TECHNICAL_DOCUMENTATION.md",
            "codemap": "docs/CODEMAP.md"}
    present = {k: v for k, v in docs.items() if (base / v).is_file()}
    if present:
        data["docs"] = present
    anchors = [{"label": k.capitalize(), "kind": "doc", "path": v}
               for k, v in present.items()]
    if (base / "opencode.json").is_file():
        anchors.append({"label": "opencode.json", "kind": "file",
                        "path": "opencode.json"})
    if anchors:
        data["anchors"] = anchors
    return data


def default_body(project: dict[str, Any]) -> str:
    name = project.get("name") or "Project"
    description = project.get("description") or ""
    text = f"# {name}\n"
    if description:
        text += f"\n{description}\n"
    text += ("\nThis file is the Perun project manifest. The YAML front matter "
             "above drives the Perun dashboard; everything below is free-form "
             "notes shown on the project page.\n")
    return text
