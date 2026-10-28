"""The context map — the losslessness contract for an ``AGENTS.md`` migration.

The problem this solves
-----------------------
Phase 4 of the AGENTS.md optimisation *moves* reference material out of a
project's ``AGENTS.md`` and into the Context layer. Moving material is safe
only if you can prove that **nothing was lost**. "I read the file and moved
the obvious bits" is not a proof: a section can be dropped by accident, a rule
can be paraphrased into meaninglessness, and a credential can be moved into a
document that will be embedded and leave the machine.

The context map is the proof. It is a small JSON file in the project
(``.perun/context-map.json``) that records, **for every section of the
original ``AGENTS.md``, where that content went**:

.. code-block:: json

    {
      "version": 1,
      "project": "perun",
      "source": "AGENTS.md",
      "source_hash": "…",
      "retained": "perun.agents.full",
      "sections": [
        {
          "heading": "Core rules for working here",
          "hash": "…",
          "bytes": 1941,
          "dest": [
            {"kind": "inline",
             "required": ["CODEMAP-first.", "Never block the event loop."]}
          ]
        },
        {
          "heading": "Commands",
          "hash": "…",
          "bytes": 1179,
          "dest": [
            {"kind": "context", "key": "perun.context.commands"}
          ]
        }
      ]
    }

The four destination kinds:

``inline``
    The content stays in the project's ``AGENTS.md``. Every ``required``
    fragment must still be present there, verbatim. This is the checkable
    form of the parity rule *"behavioural rules are never moved"*.

``context``
    The content was relocated to a Context-layer doc (by key). The doc must
    exist in the project's scope and contain the ``required`` fragments.

``secret``
    The content was a credential and now lives in the encrypted secret
    store, under a name. The secret must exist. Its *value* is never checked
    and never revealed — the store is the point.

``retained``
    The content lives only in the retained original
    (``<slug>.agents.full``), which is kept verbatim and is reachable in one
    call. This is the honest destination for bulk material (a 24 kB version
    history) that nobody wants inlined and that does not deserve its own
    document.

What :func:`check` proves
-------------------------
1. **Nothing was deleted.** The retained original is still in the store and
   still hashes to the map's ``source_hash``.
2. **Nothing is unaccounted for.** Every section of the retained original
   appears in the map. A section that was moved but never recorded fails.
3. **Every destination holds its content.** Each ``required`` fragment is
   found at its destination.

Together: the map is a *complete* and *checkable* account of the migration.
``perun context verify`` runs it.

The map is a plain file in the project, so it is reviewable in a pull request
and a change to it is a visible, deliberate act — which is exactly what a
losslessness proof has to be.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import memory

MAP_VERSION = 1

# Where the map lives inside a project.
MAP_DIR = ".perun"
MAP_FILE = "context-map.json"

# Where per-project context docs live inside a project. A file
# ``.perun/context/commands.md`` becomes the context key
# ``<slug>.context.commands``.
CONTEXT_SUBDIR = "context"

# The kind of section split. Level-2 headings — the same granularity the
# CODEMAP-style doc indexer uses.
_SECTION_RE = re.compile(r"^##\s+(.*)$", re.M)

DEST_KINDS = ("inline", "context", "secret", "retained")


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def split_sections(text: str) -> list[dict[str, Any]]:
    """Split Markdown into ``{heading, body, hash, bytes}`` at level-2 headings.

    The text before the first heading is a section too, titled ``"(top)"`` —
    it usually holds the title and the one-paragraph overview, and it is
    exactly the kind of content a migration must not lose track of.
    """
    sections: list[dict[str, Any]] = []
    matches = list(_SECTION_RE.finditer(text or ""))
    if not matches:
        body = (text or "").strip()
        return [{"heading": "(top)", "body": body,
                 "hash": memory.content_hash(body),
                 "bytes": len(body.encode("utf-8"))}] if body else []

    head = text[: matches[0].start()].strip()
    if head:
        sections.append({"heading": "(top)", "body": head,
                         "hash": memory.content_hash(head),
                         "bytes": len(head.encode("utf-8"))})
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.start(): end].strip()
        sections.append({
            "heading": (m.group(1).strip() or "(untitled)"),
            "body": body,
            "hash": memory.content_hash(body),
            "bytes": len(body.encode("utf-8")),
        })
    return sections


def _norm(text: str) -> str:
    """Normalise whitespace, so a fragment survives a re-wrap."""
    return " ".join((text or "").split())


def contains(haystack: str, needle: str) -> bool:
    """Whitespace-insensitive substring test."""
    n = _norm(needle)
    return bool(n) and n in _norm(haystack)


def _verbatim(haystack: str, section_body: str) -> bool:
    """Is *section_body* present in *haystack* unchanged?

    Two ways it can be true: the body appears as a substring (ignoring
    re-wrapping), or the destination contains a section with the same
    content hash — the hash covers the heading line too, so that is an
    exact match of the whole section.
    """
    if contains(haystack, section_body):
        return True
    target = memory.content_hash(section_body)
    return any(s["hash"] == target for s in split_sections(haystack))


def _live_section(text: str, heading: str) -> str:
    """Return the body of the ``## heading`` section from *text*, if present."""
    target = heading.strip().casefold()
    lines = (text or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and line[3:].strip().casefold() == target:
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start + 1:end]).strip()


def _inline_subsection_duplicates(body: str) -> list[tuple[str, int]]:
    """Return repeated level-3 blocks inside an inline section.

    A long inline section that copy-pastes the same ``###`` module multiple
    times pays the same tokens repeatedly.  The return value is a list of
    ``(heading, copies)`` for duplicates with non-trivial bodies.
    """
    counts: dict[str, int] = {}
    headings: dict[str, str] = {}
    buf: list[str] = []
    current_heading = "(tail)"
    for line in (body or "").splitlines():
        if line.startswith("### "):
            block = "\n".join(buf).strip()
            if block and len(block) > 200:
                h = memory.content_hash(block)
                counts[h] = counts.get(h, 0) + 1
                headings.setdefault(h, current_heading)
            current_heading = line[4:].strip() or "(untitled)"
            buf = []
        else:
            buf.append(line)
    block = "\n".join(buf).strip()
    if block and len(block) > 200:
        h = memory.content_hash(block)
        counts[h] = counts.get(h, 0) + 1
        headings.setdefault(h, current_heading)
    return [(headings[h], n) for h, n in counts.items() if n > 1]


def _inline_subsection_sizes(body: str) -> list[tuple[str, int]]:
    """Return the size of every level-3 block inside an inline section.

    Used to report which subsections dominate an inline section's byte budget.
    """
    sizes: list[tuple[str, int]] = []
    buf: list[str] = []
    current_heading = "(tail)"
    for line in (body or "").splitlines():
        if line.startswith("### "):
            block = "\n".join(buf).strip()
            if block:
                sizes.append((current_heading, len(block.encode("utf-8"))))
            current_heading = line[4:].strip() or "(untitled)"
            buf = []
        else:
            buf.append(line)
    block = "\n".join(buf).strip()
    if block:
        sizes.append((current_heading, len(block.encode("utf-8"))))
    return sizes


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def map_path(project_path: str | Path) -> Path:
    """Return the map path for a project."""
    return Path(project_path) / MAP_DIR / MAP_FILE


def context_dir(project_path: str | Path) -> Path:
    """Return the per-project context-doc directory."""
    return Path(project_path) / MAP_DIR / CONTEXT_SUBDIR


def context_key(slug: str, stem: str) -> str:
    """The context key for a per-project context doc."""
    from . import context

    return context.normalize_key(f"{slug}.context.{stem}")


def retained_key(slug: str) -> str:
    """The context key that holds a project's original ``AGENTS.md``."""
    from . import context

    return context.normalize_key(f"{slug}.agents.full")


# --------------------------------------------------------------------------- #
# Reading and writing the map
# --------------------------------------------------------------------------- #
def load_map(project_path: str | Path) -> dict[str, Any] | None:
    """Load a project's context map, or ``None`` when there is none."""
    p = map_path(project_path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_map(project_path: str | Path, data: dict[str, Any]) -> Path:
    """Write a project's context map. Returns the path written."""
    p = map_path(project_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    return p


def build_map(slug: str, source_text: str, retained: str = "") -> dict[str, Any]:
    """Build a *draft* map for a project's ``AGENTS.md``.

    Every section starts unclassified. The draft is the input to a migration:
    fill in each section's destinations, then let :func:`check` prove the
    result.
    """
    sections = split_sections(source_text)
    return {
        "version": MAP_VERSION,
        "project": slug,
        "source": "AGENTS.md",
        "source_hash": memory.content_hash(source_text),
        "retained": retained or retained_key(slug),
        "sections": [
            {
                "heading": s["heading"],
                "hash": s["hash"],
                "bytes": s["bytes"],
                "dest": [],
            }
            for s in sections
        ],
    }


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #
def check(project: dict[str, Any], *, agents_path: str | Path | None = None) -> dict[str, Any]:
    """Run the losslessness check for one project.

    Returns ``{ok, problems, report, sections}``. ``ok`` is ``True`` only when
    the migration is provably lossless.
    """
    from . import context, secret_store

    report: list[str] = []
    problems: list[str] = []
    slug = str(project.get("slug") or "")
    project_id = int(project.get("id") or 0)
    root = Path(project.get("path") or "")
    data = load_map(root)
    if not data:
        return {"ok": True, "problems": [], "report": [],
                "sections": 0, "skipped": True}

    sections = data.get("sections")
    if not isinstance(sections, list):
        problems.append("map has no 'sections' list")
        report.append(f"{slug}: MAP MALFORMED")
        return {"ok": False, "problems": problems, "report": report,
                "sections": 0, "skipped": False}

    retained = str(data.get("retained") or retained_key(slug))
    live = Path(agents_path) if agents_path else (root / "AGENTS.md")
    live_text = live.read_text(encoding="utf-8", errors="replace") \
        if live.is_file() else ""

    # 1. Nothing was deleted: the retained original is intact.
    doc = context.get(retained, project_id=project_id, scope=context.PROJECT_SCOPE)
    if not doc:
        problems.append(f"the retained original '{retained}' is missing from the store")
        report.append(f"{slug}: RETAINED MISSING")
        return {"ok": False, "problems": problems, "report": report,
                "sections": len(sections), "skipped": False}
    if memory.content_hash(doc["body"]) != str(data.get("source_hash") or ""):
        problems.append(
            f"the retained original '{retained}' no longer matches the map's "
            f"source_hash — the original was overwritten")
        report.append(f"{slug}: RETAINED ALTERED")
    else:
        report.append(f"{slug}: retained original ok ({retained})")

    # 2. Nothing is unaccounted for: every section of the original is mapped.
    original = split_sections(doc["body"])
    mapped = {str(s.get("hash") or ""): s for s in sections
              if isinstance(s, dict)}
    unaccounted = [s["heading"] for s in original
                   if s["hash"] not in mapped]
    if unaccounted:
        problems.append(
            f"sections of the original not accounted for: {', '.join(unaccounted)}")
        report.append(f"{slug}: UNACCOUNTED {', '.join(unaccounted)}")

    # 3. Every destination holds its content.
    by_hash = {s["hash"]: s for s in original}

    # Count how many times each inline-classified body appears in the live file.
    # A body that is classified as inline but appears more than once is a
    # contract violation: the map says it must be present, but repetition means
    # the ambient prompt pays for it multiple times without adding awareness.
    inline_hits: dict[str, list[str]] = {}
    for section in sections:
        if not isinstance(section, dict):
            continue
        for dest in (section.get("dest") or []):
            if isinstance(dest, dict) and str(dest.get("kind") or "").strip().lower() == "inline":
                source = by_hash.get(str(section.get("hash") or ""))
                source_body = source["body"] if source else ""
                if source_body:
                    key = memory.content_hash(source_body)
                    inline_hits.setdefault(key, []).append(
                        str(section.get("heading") or "(untitled)"))

    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading") or "(untitled)")
        dests = section.get("dest")
        if not isinstance(dests, list) or not dests:
            problems.append(f"section '{heading}' has no destination")
            continue
        source = by_hash.get(str(section.get("hash") or ""))
        source_body = source["body"] if source else ""
        for dest in dests:
            if not isinstance(dest, dict):
                problems.append(f"section '{heading}' has a malformed destination")
                continue
            kind = str(dest.get("kind") or "").strip().lower()
            required = [str(r) for r in (dest.get("required") or [])]
            # The part of the section that is actually inline in the live file.
            # For mixed destinations this is only the retained bullets, so it
            # is the honest measure of the ambient prompt cost.
            live_section_body = _live_section(live_text, heading) if kind == "inline" else ""

            if kind == "inline":
                missing = [r for r in required if not contains(live_text, r)]
                if missing:
                    problems.append(
                        f"section '{heading}' lost content that must stay inline: "
                        f"{missing[0][:60]!r}")
                # Size guardrail: the *live* inline portion over the budget
                # should be reconsidered, but size alone is not a losslessness
                # failure. For mixed destinations this measures only what is
                # still ambient.
                live_bytes = len(live_section_body.encode("utf-8"))
                if live_bytes > 4_000:
                    report.append(
                        f"{slug}: section '{heading}' is {live_bytes:,} B "
                        f"inline — consider moving to a context doc")
                # Internal duplication guardrail: repeated ``###`` blocks inside
                # the live inline section are wasted tokens, not extra awareness.
                for dup_heading, copies in _inline_subsection_duplicates(live_section_body):
                    problems.append(
                        f"section '{heading}' repeats subsection "
                        f"'{dup_heading}' {copies} times")
                    report.append(
                        f"{slug}: section '{heading}' has {copies} copies of "
                        f"'{dup_heading[:50]}'")
                # Size detail for large inline sections: list the subsections so
                # the owner knows what to move next.
                if live_bytes > 4_000:
                    big_subs = sorted(
                        _inline_subsection_sizes(live_section_body),
                        key=lambda x: -x[1],
                    )[:5]
                    for sub_heading, sub_bytes in big_subs:
                        if sub_bytes > 800:
                            report.append(
                                f"{slug}: section '{heading}' subsection "
                                f"'{sub_heading[:50]}' is {sub_bytes:,} B")

            elif kind == "context":
                key = str(dest.get("key") or "")
                cdoc = context.get(key, project_id=project_id,
                                   scope=context.PROJECT_SCOPE)
                if not cdoc:
                    problems.append(
                        f"section '{heading}' points at context doc '{key}', "
                        f"which does not exist")
                    continue
                # For mixed destinations (inline + context) the whole source
                # body need not live in the context doc; require only the
                # declared fragments.  When context is the sole destination, the
                # full verbatim parity contract still applies.
                context_is_sole = len(dests) == 1
                if context_is_sole and source_body and not _verbatim(cdoc["body"], source_body):
                    problems.append(
                        f"section '{heading}' is not present verbatim in "
                        f"'{key}' — the relocation is not lossless")
                missing = [r for r in required if not contains(cdoc["body"], r)]
                if missing:
                    problems.append(
                        f"section '{heading}' is not fully present in '{key}': "
                        f"{missing[0][:60]!r}")
            elif kind == "secret":
                name = str(dest.get("key") or "")
                if not name:
                    problems.append(f"section '{heading}' names no secret")
                elif not secret_store.get_secret(
                        name, project_id=project_id, reveal=False):
                    problems.append(
                        f"section '{heading}' names secret '{name}', "
                        f"which is not in the store")
            elif kind == "retained":
                missing = [r for r in required if not contains(doc["body"], r)]
                if missing:
                    problems.append(
                        f"section '{heading}' is not in the retained original: "
                        f"{missing[0][:60]!r}")
            else:
                problems.append(
                    f"section '{heading}' has an unknown destination kind "
                    f"'{kind}' (expected one of {', '.join(DEST_KINDS)})")

    # Inline duplication is a contract failure: the same content is classified
    # as "must stay inline" but appears multiple times in the live file.
    for key, headings in inline_hits.items():
        if len(headings) > 1:
            problems.append(
                f"inline content appears {len(headings)} times: "
                f"{', '.join(h[:50] for h in headings)}")
            report.append(
                f"{slug}: DUPLICATE-INLINE {len(headings)} copies of "
                f"'{headings[0][:50]}'")

    # Informational: context docs the map does not mention.
    known = {str(d.get("key") or "") for s in sections
             if isinstance(s, dict) for d in (s.get("dest") or [])
             if isinstance(d, dict) and d.get("kind") == "context"}
    unmentioned = [k["key"] for k in context.list_keys(
        project_id=project_id, scope=context.PROJECT_SCOPE)
        if k["key"].startswith(f"{slug}.context.") and k["key"] not in known]
    if unmentioned:
        report.append(f"{slug}: note — context docs not in the map: "
                      f"{', '.join(unmentioned)}")

    report.append(f"{slug}: {len(sections)} section(s) accounted for"
                  if not problems else f"{slug}: {len(problems)} problem(s)")
    return {"ok": not problems, "problems": problems, "report": report,
            "sections": len(sections), "skipped": False}


# --------------------------------------------------------------------------- #
# Project context docs
# --------------------------------------------------------------------------- #
def project_context_files(project_path: str | Path) -> list[Path]:
    """Return the per-project context docs, sorted by name."""
    d = context_dir(project_path)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.md") if p.is_file())


def project_context_key(slug: str, path: Path) -> str:
    """The context key for one per-project context doc."""
    return context_key(slug, path.stem)


def write_context_doc(project_path: str | Path, name: str, body: str) -> Path:
    """Write a per-project context doc. Returns the path written."""
    if not name.endswith(".md"):
        name += ".md"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    if not safe:
        raise ValueError("a context doc needs a usable file name")
    d = context_dir(project_path)
    d.mkdir(parents=True, exist_ok=True)
    p = d / safe
    p.write_text(body, encoding="utf-8", newline="\n")
    return p
