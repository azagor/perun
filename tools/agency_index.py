"""Generate ``perun/data/agency_index.json`` from the upstream agent roster.

**Build-time only.** The service never downloads, converts or parses upstream
markdown: :mod:`perun.agent_catalog` reads the committed JSON so recognition
works offline and deterministically. Run this deliberately when upstream adds
agents, then commit the result:

    python tools/agency_index.py                 # resolve main, rewrite the index
    python tools/agency_index.py --ref <sha>     # pin an explicit commit
    python tools/agency_index.py --check         # exit 1 if the index is stale

The only logic borrowed from upstream is the slug rule (``scripts/lib.sh``
``slugify``), because the slug is the installed filename and must match exactly.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

SOURCE_URL = "https://github.com/msitarzewski/agency-agents"
COMMIT_API = "https://api.github.com/repos/msitarzewski/agency-agents/commits/main"
TARBALL_URL = "https://codeload.github.com/msitarzewski/agency-agents/tar.gz/{ref}"

OUT_PATH = Path(__file__).resolve().parent.parent / "perun" / "data" / "agency_index.json"

# Directories that are not agent divisions: the converted outputs, the
# strategy/runbook material, and repository plumbing.
NON_DIVISION_DIRS = {
    ".github", ".git", "assets", "docs", "examples", "integrations",
    "public", "scripts", "strategy", "tools",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Upstream's ``slugify``: lowercase, non-alphanumerics to ``-``, trimmed."""
    return _SLUG_RE.sub("-", (name or "").lower()).strip("-")


def _fetch(url: str, timeout: int = 90) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "perun-agency-index"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def resolve_ref() -> str:
    """The commit sha ``main`` currently points at, or ``main`` if that fails."""
    try:
        payload = json.loads(_fetch(COMMIT_API, timeout=30).decode("utf-8"))
        sha = str(payload.get("sha") or "").strip()
        if sha:
            return sha
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return "main"


def _frontmatter(text: str) -> Dict[str, str]:
    """Read ``name``/``description``/``color`` from an agent Markdown file."""
    out: Dict[str, str] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        if key in {"name", "description", "color", "emoji"}:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[key] = value
    return out


def _members(archive: bytes) -> Iterable[tuple[str, bytes]]:
    """Yield ``(relative_path, content)`` for the markdown files in a tarball."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for item in tar.getmembers():
            if not item.isfile() or not item.name.endswith(".md"):
                continue
            parts = item.name.split("/")
            if len(parts) < 3:  # <root>/<division>/<file>.md
                continue
            relative = "/".join(parts[1:])
            handle = tar.extractfile(item)
            if handle is None:
                continue
            yield relative, handle.read()


def build(ref: str, *, archive: Optional[bytes] = None) -> Dict[str, Any]:
    archive = archive if archive is not None else _fetch(TARBALL_URL.format(ref=ref))
    labels: Dict[str, str] = {}
    raw_agents: list[Dict[str, Any]] = []

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for item in tar.getmembers():
            if item.name.endswith("/divisions.json"):
                handle = tar.extractfile(item)
                if handle is None:
                    continue
                try:
                    payload = json.loads(handle.read().decode("utf-8"))
                except ValueError:
                    continue
                for key, meta in (payload.get("divisions") or {}).items():
                    if isinstance(meta, dict) and meta.get("label"):
                        labels[str(key)] = str(meta["label"])

    for relative, content in _members(archive):
        division, _, filename = relative.partition("/")
        if division in NON_DIVISION_DIRS or not filename.endswith(".md"):
            continue
        if "/" in filename:  # nested markdown is documentation, not an agent
            continue
        fields = _frontmatter(content.decode("utf-8", errors="replace"))
        name = (fields.get("name") or "").strip()
        description = " ".join((fields.get("description") or "").split())
        if not name or not description:
            continue
        raw_agents.append({
            "slug": slugify(name),
            "name": name,
            "description": description,
            "division": division,
            "color": (fields.get("color") or "").strip(),
            "emoji": (fields.get("emoji") or "").strip(),
            "path": relative,
        })

    counts: Dict[str, int] = {}
    for agent in raw_agents:
        counts[agent["division"]] = counts.get(agent["division"], 0) + 1
    divisions = {
        name: {"label": labels.get(name) or name.replace("-", " ").title(),
               "count": count}
        for name, count in sorted(counts.items())
    }

    raw_agents.sort(key=lambda a: (a["division"], a["slug"], a["path"]))
    duplicates: Dict[str, int] = {}
    for agent in raw_agents:
        duplicates[agent["slug"]] = duplicates.get(agent["slug"], 0) + 1
    collisions = sorted(slug for slug, count in duplicates.items() if count > 1)

    return {
        "source": SOURCE_URL,
        "commit": ref,
        "agents": raw_agents,
        "divisions": divisions,
        "collisions": collisions,
    }


def render(index: Dict[str, Any]) -> str:
    """One agent per line, so a regenerated index diffs readably."""
    head = [f'  "source": {json.dumps(index["source"])}',
            f'  "commit": {json.dumps(index["commit"])}']
    agents = ",\n".join(
        "    " + json.dumps(agent, ensure_ascii=False, sort_keys=True)
        for agent in index["agents"])
    divisions = ",\n".join(
        f'    {json.dumps(name)}: {json.dumps(meta, ensure_ascii=False, sort_keys=True)}'
        for name, meta in index["divisions"].items())
    collisions = json.dumps(index["collisions"], ensure_ascii=False)
    return (
        "{\n" + ",\n".join(head) + ",\n"
        '  "agents": [\n' + agents + "\n  ],\n"
        '  "divisions": {\n' + divisions + "\n  },\n"
        '  "collisions": ' + collisions + "\n}\n"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="", help="commit sha (default: current main)")
    parser.add_argument("--out", default=str(OUT_PATH), help="output path")
    parser.add_argument("--check", action="store_true",
                        help="compare against the committed index, write nothing")
    args = parser.parse_args(argv)

    ref = args.ref.strip() or resolve_ref()
    try:
        index = build(ref)
    except (urllib.error.URLError, OSError) as exc:
        print(f"[agency-index] download failed: {exc}", file=sys.stderr)
        return 2

    text = render(index)
    target = Path(args.out)
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != text:
            print(f"[agency-index] stale: {target} does not match {ref}", file=sys.stderr)
            return 1
        print(f"[agency-index] up to date at {ref}")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"[agency-index] {len(index['agents'])} agents in "
          f"{len(index['divisions'])} divisions -> {target}")
    if index["collisions"]:
        print(f"[agency-index] WARNING duplicate slugs: {', '.join(index['collisions'])}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
