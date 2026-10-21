"""Perun Context — keyed, vector-backed reference docs for agents.

The problem this solves
-----------------------
``AGENTS.md`` is ambient context: it is paid in full, on every request, in
every session. It also grows without bound, because every project keeps its
own copy of the same material (the MCP tool table, the shared instruction
modules, the memory protocol). This module provides the alternative: a store
of *context docs* — named, retrievable, individually scoped reference bodies —
so that reference material can be **relocated** out of the prompt and fetched
exactly when it is relevant.

The design is built around one hard constraint: **the agent's awareness of a
project must not fall below its current level.** Deferring content without a
discovery path is a silent capability loss. Three mechanisms prevent that:

* **Nothing is deleted, only relocated.** The complete original ``AGENTS.md``
  of a project is retained verbatim as a context doc and stays retrievable in
  one call.
* **The inventory is always visible.** ``list_keys`` enumerates every key in
  scope, so the agent always knows what it does not currently have.
* **Relevant content is pre-fetched, not requested.** The agent states *what
  it is about to do*; this module resolves that into the relevant docs. The
  agent does not have to know what is relevant in advance.

Retrieval order (``mode="auto"``)
---------------------------------
1. **Exact key** — ``handbook.tools``. Deterministic, free, never needs Ollama.
2. **Anchor label** — resolved through the project's ``perun.md`` manifest, so
   the agent can ask by human name.
3. **Key substring** — ``ssh`` → every key containing ``ssh``. Short topics
   only, so a task description cannot accidentally substring-match.
4. **Semantic** — embed the topic and rank by cosine + keyword overlap.

Deterministic first, fuzzy second: a semantic match for a hard fact (a host
name, a credential) is unreliable, and a flaky lookup is worse than inline
text. The key path is exact; the vector path only *adds* reach.

Storage
-------
Context docs live in their own table (``context_docs``), deliberately separate
from ``doc_chunks``:

* a context doc has a **stable key**, which is the whole point — ``doc_chunks``
  is keyed by *file path*;
* project-scoped context docs must **not** leak into another project's
  cross-project document search, which ``doc_chunks`` would do;
* the two have different lifecycles (authored reference vs mirrored files) and
  different retention rules.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from . import db, memory, registry
from .embeddings import (
    DOC_PREFIX,
    QUERY_PREFIX,
    cosine_similarity,
    embed_texts,
    pack_vector,
    unpack_vector,
)

GLOBAL_SCOPE = "global"
PROJECT_SCOPE = "project"

# Bounds (same spirit as memory.MAX_FACT_CHARS / MAX_TOP_K).
MAX_BODY_CHARS = 120_000     # a single context doc
MAX_TOP_K = 8                # clamp on results per call
SUBSTRING_MAX = 48           # longest topic still eligible for substring match
SEMANTIC_MIN_SCORE = 0.15    # relevance floor for the semantic path
MAX_INLINE_CHARS = 12_000    # above this a body is returned as an outline
MAX_OUTLINE_SECTIONS = 60

_KEY_CLEAN = re.compile(r"[^a-z0-9._/-]+")


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
def normalize_key(key: str) -> str:
    """Normalise a context key to its canonical form.

    Keys are case-insensitive and whitespace-insensitive: ``"Handbook Tools"``
    and ``"handbook-tools"`` are the same key.
    """
    raw = re.sub(r"\s+", "-", (key or "").strip().casefold())
    return _KEY_CLEAN.sub("-", raw).strip("-")


def _split_section(topic: str) -> tuple[str, str]:
    """Split ``"key#Section Name"`` into ``("key", "Section Name")``."""
    text = (topic or "").strip()
    if "#" not in text:
        return text, ""
    key, _, section = text.partition("#")
    return key.strip(), section.strip()


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #
def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": row["key"],
        "scope": row["scope"],
        "project_id": row["project_id"],
        "title": row["title"] or "",
        "summary": row["summary"] or "",
        "body": row["body"] or "",
        "source_file": row["source_file"] or "",
        "updated_at": row["updated_at"],
        "embedding": row["embedding"],
    }


def _scope_rows(scope: str, project_id: int) -> list[dict[str, Any]]:
    """Load context rows visible from *project_id* under *scope*."""
    with db.db() as conn:
        if scope == GLOBAL_SCOPE:
            rows = db.rows(conn, "SELECT * FROM context_docs WHERE scope='global' "
                                "ORDER BY key")
        elif scope == PROJECT_SCOPE:
            rows = db.rows(conn, "SELECT * FROM context_docs WHERE scope='project' "
                                "AND project_id=? ORDER BY key", (int(project_id),))
        else:  # "any" — global plus this project's own.
            # The project scope is listed first, so a project-scoped doc
            # shadows a global one of the same key — the same resolution
            # order the secret store uses.
            rows = db.rows(
                conn,
                "SELECT * FROM context_docs WHERE (scope='global' AND project_id=0) "
                "OR (scope='project' AND project_id=?) "
                "ORDER BY CASE scope WHEN 'project' THEN 0 ELSE 1 END, key",
                (int(project_id),),
            )
    return [_row_to_dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def index_docs(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Index a batch of context docs.

    Each entry accepts ``key``, ``title``, ``body``, ``summary``, ``source_file``
    and optionally ``scope`` (``global`` / ``project``) + ``project_id``.

    Embeddings are computed in a **single batch** and *outside* any database
    connection — the house rule is that a connection is never held across a
    network call. Docs whose body has not changed are left untouched, so
    re-indexing the handbook on every process start is free.

    A body that trips the **sensitive-data guard** is still stored — dropping
    it would break the parity contract, which requires the original document
    to survive verbatim — but it is **never embedded**. The embedding is the
    only thing that leaves the machine, so that is where the guard belongs.
    Such a doc is marked with ``embedded_hash == content_hash`` and a NULL
    vector, which tells the repair path to leave it alone.
    """
    prepared: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    notices: list[dict[str, str]] = []
    for raw in docs or []:
        key = normalize_key(str(raw.get("key") or ""))
        body = (raw.get("body") or "").strip()
        if not key:
            skipped.append({"key": str(raw.get("key") or ""), "reason": "empty key"})
            continue
        if not body:
            skipped.append({"key": key, "reason": "empty body"})
            continue
        if len(body) > MAX_BODY_CHARS:
            skipped.append({"key": key, "reason": f"too_long (max {MAX_BODY_CHARS})"})
            continue
        scope = PROJECT_SCOPE if raw.get("scope") == PROJECT_SCOPE else GLOBAL_SCOPE
        project_id = int(raw.get("project_id") or 0)
        if scope == PROJECT_SCOPE and not project_id:
            skipped.append({"key": key, "reason": "project scope needs project_id"})
            continue
        if scope == GLOBAL_SCOPE:
            project_id = 0
        sensitive = memory.is_sensitive(body)
        if sensitive:
            notices.append({
                "key": key,
                "reason": "looks sensitive — stored, but never embedded",
            })
        prepared.append({
            "key": key,
            "title": str(raw.get("title") or key),
            "summary": str(raw.get("summary") or ""),
            "body": body,
            "source_file": str(raw.get("source_file") or ""),
            "scope": scope,
            "project_id": project_id,
            "content_hash": memory.content_hash(body),
            "sensitive": sensitive,
        })

    if not prepared:
        return {"ok": True, "indexed": 0, "unchanged": 0, "skipped": skipped,
                "notices": notices}

    # Which of these already exist, and do they still have a usable vector?
    # A doc indexed while Ollama was down keeps its body but has no vector,
    # so it counts as changed and is embedded now — otherwise the semantic
    # path would stay degraded to keyword matching forever.
    with db.db() as conn:
        existing: dict[tuple[str, int, str], tuple[str, str]] = {}
        for row in db.rows(conn, "SELECT scope, project_id, key, content_hash, "
                                "embedded_hash FROM context_docs"):
            existing[(row["scope"], int(row["project_id"]), row["key"])] = (
                row["content_hash"] or "", row["embedded_hash"] or "")

    def _needs_write(doc: dict[str, Any]) -> bool:
        prev = existing.get((doc["scope"], doc["project_id"], doc["key"]))
        if prev is None:
            return True
        content_hash, embedded_hash = prev
        return content_hash != doc["content_hash"] or embedded_hash != doc["content_hash"]

    changed = [d for d in prepared if _needs_write(d)]
    unchanged = len(prepared) - len(changed)

    # Embed outside the connection. One batch for the whole set. Sensitive
    # docs are excluded: their body must not reach the embedding endpoint.
    embeddable = [d for d in changed if not d["sensitive"]]
    vectors: dict[int, Any] = {}
    if embeddable:
        try:
            vecs = embed_texts([d["body"] for d in embeddable], prefix=DOC_PREFIX)
        except Exception:
            vecs = [None] * len(embeddable)
        vectors = {id(d): v for d, v in zip(embeddable, vecs)}

    ts = registry.now_iso()
    changed_ids = {id(d) for d in changed}
    with db.db() as conn:
        for doc in changed:
            vec = vectors.get(id(doc))
            emb = pack_vector(vec) if vec is not None else None
            h = doc["content_hash"]
            if doc["sensitive"]:
                # Deliberately vector-less: embedded_hash == content_hash marks
                # it as "do not embed", so the repair path leaves it alone.
                emb = None
                embedded_hash = h
            else:
                embedded_hash = h if emb else ""
            conn.execute(
                """INSERT INTO context_docs
                   (scope, project_id, key, title, summary, body, source_file,
                    embedding, content_hash, embedded_hash, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(scope, project_id, key) DO UPDATE SET
                     title=excluded.title,
                     summary=excluded.summary,
                     body=excluded.body,
                     source_file=excluded.source_file,
                     embedding=excluded.embedding,
                     content_hash=excluded.content_hash,
                     embedded_hash=excluded.embedded_hash,
                     updated_at=excluded.updated_at""",
                (doc["scope"], doc["project_id"], doc["key"], doc["title"],
                 doc["summary"], doc["body"], doc["source_file"], emb, h,
                 embedded_hash, ts),
            )
        # Unchanged docs keep their vector but should still track title/summary.
        for doc in prepared:
            if id(doc) in changed_ids:
                continue
            conn.execute(
                "UPDATE context_docs SET title=?, summary=?, source_file=? "
                "WHERE scope=? AND project_id=? AND key=?",
                (doc["title"], doc["summary"], doc["source_file"],
                 doc["scope"], doc["project_id"], doc["key"]),
            )

    return {
        "ok": True,
        "indexed": len(changed),
        "unchanged": unchanged,
        "embedded": sum(1 for v in vectors.values() if v is not None),
        "skipped": skipped,
        "notices": notices,
    }


def index_doc(key: str, title: str, body: str, *, scope: str = GLOBAL_SCOPE,
              project_id: int = 0, summary: str = "", source_file: str = "") -> dict[str, Any]:
    """Index a single context doc.

    Unlike the batch form, a doc that was rejected (empty key, empty body,
    oversized, project scope without a project) reports ``ok=False`` — the
    caller asked for exactly one document and did not get it.
    """
    res = index_docs([{
        "key": key, "title": title, "body": body, "summary": summary,
        "source_file": source_file, "scope": scope, "project_id": project_id,
    }])
    if res.get("skipped") and not res.get("indexed") and not res.get("unchanged"):
        res["ok"] = False
    return res


def delete(key: str, *, scope: str = GLOBAL_SCOPE, project_id: int = 0) -> bool:
    """Delete one context doc. Returns ``True`` when a row was removed."""
    key = normalize_key(key)
    if scope == GLOBAL_SCOPE:
        project_id = 0
    with db.db() as conn:
        cur = conn.execute(
            "DELETE FROM context_docs WHERE scope=? AND project_id=? AND key=?",
            (scope, int(project_id), key),
        )
        return cur.rowcount > 0


def reembed_missing(limit: int = 64) -> dict[str, Any]:
    """Embed context docs whose vector is missing or stale.

    A doc indexed while Ollama was down stays vector-less forever otherwise,
    and the semantic path silently degrades to keyword matching — which is
    exactly the failure mode the parity contract cannot tolerate.
    """
    limit = max(1, int(limit))
    with db.db() as conn:
        # ``embedded_hash == content_hash`` marks a doc as deliberately
        # vector-less (it tripped the sensitive-data guard), so it must not be
        # picked up here — that is the one thing that must never leave the
        # machine.
        rows = db.rows(
            conn,
            "SELECT id, body, content_hash FROM context_docs "
            "WHERE embedding IS NULL AND COALESCE(embedded_hash,'') != content_hash "
            "LIMIT ?",
            (limit,),
        )
    if not rows:
        return {"ok": True, "embedded": 0}
    try:
        vecs = embed_texts([r["body"] or "" for r in rows], prefix=DOC_PREFIX)
    except Exception:
        vecs = [None] * len(rows)
    embedded = 0
    for row, vec in zip(rows, vecs):
        if vec is None:
            continue
        if memory.is_sensitive(row["body"] or ""):
            # Belt and braces: mark it as deliberately vector-less so it is
            # never selected again.
            with db.db() as conn:
                conn.execute(
                    "UPDATE context_docs SET embedded_hash=? WHERE id=?",
                    (row["content_hash"], row["id"]),
                )
            continue
        with db.db() as conn:
            conn.execute(
                "UPDATE context_docs SET embedding=?, embedded_hash=? WHERE id=?",
                (pack_vector(vec), row["content_hash"], row["id"]),
            )
        embedded += 1
    return {"ok": True, "embedded": embedded}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def get(key: str, *, project_id: int = 0, scope: str = "any") -> dict[str, Any] | None:
    """Fetch one context doc by exact key. Project scope wins over global."""
    key = normalize_key(key)
    if not key:
        return None
    rows = _scope_rows(scope, project_id)
    for row in rows:
        if row["key"] == key:
            return row
    return None


def list_keys(*, project_id: int = 0, scope: str = "any") -> list[dict[str, Any]]:
    """Return the key inventory visible from *project_id*."""
    return [
        {
            "key": r["key"],
            "title": r["title"],
            "summary": r["summary"],
            "scope": r["scope"],
            "source_file": r["source_file"],
            "updated_at": r["updated_at"],
            "chars": len(r["body"]),
        }
        for r in _scope_rows(scope, project_id)
    ]


# --------------------------------------------------------------------------- #
# Sections (for large documents)
# --------------------------------------------------------------------------- #
def outline(body: str) -> list[str]:
    """Return the level-2 headings of *body*, in order."""
    return [line[3:].strip() for line in (body or "").splitlines()
            if line.startswith("## ") and line[3:].strip()]


def slice_section(body: str, section: str) -> str:
    """Return one ``## section`` of *body*.

    Falls back to the whole body when the section is not found, so a caller
    never silently receives nothing.
    """
    if not section:
        return body or ""
    lines = (body or "").splitlines()
    wanted = section.strip().casefold()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and line[3:].strip().casefold() == wanted:
            start = i
            break
    if start is None:
        # Tolerate a partial match ("build" → "Build Pipeline").
        for i, line in enumerate(lines):
            if line.startswith("## ") and wanted in line[3:].strip().casefold():
                start = i
                break
    if start is None:
        return body or ""
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end]).strip()


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def _anchor_lookup(topic: str, project_id: int) -> dict[str, Any] | None:
    """Resolve an anchor *label* (or its ``memory`` key) through the manifest."""
    if not project_id:
        return None
    try:
        from . import manifest as manifest_mod

        project = registry.get_project(project_id)
        if not project:
            return None
        data = manifest_mod.load(project["path"]).get("data") or {}
        anchors = data.get("anchors") or []
    except Exception:
        return None
    wanted = topic.strip().casefold()
    for anchor in anchors:
        if not isinstance(anchor, dict):
            continue
        label = str(anchor.get("label") or "").strip().casefold()
        mem = str(anchor.get("memory") or "").strip()
        if (label and label == wanted) or (mem and normalize_key(mem) == normalize_key(topic)):
            if mem:
                doc = get(mem, project_id=project_id)
                if doc:
                    return doc
            path = str(anchor.get("path") or "")
            if path:
                for row in _scope_rows("any", project_id):
                    if row["source_file"] == path:
                        return row
    return None


def _semantic(topic: str, rows: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """Rank context rows against a free-text topic."""
    if not rows:
        return []
    q_emb = None
    try:
        q_emb = embed_texts([topic], prefix=QUERY_PREFIX)[0]
    except Exception:
        q_emb = None
    words = [w for w in re.findall(r"\w{3,}", topic.casefold())][:16]
    low_topic = topic.casefold()
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        score = 0.0
        if q_emb and row.get("embedding"):
            score = cosine_similarity(q_emb, unpack_vector(row["embedding"]))
        hay = " ".join([
            row["key"], row["title"], row["summary"], row["body"],
        ]).casefold()
        hits = sum(1 for w in words if w in hay)
        if hits:
            score += 0.05 * min(hits, 6)
        if low_topic and low_topic in hay:
            score += 0.25
        if score > SEMANTIC_MIN_SCORE:
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    out = []
    for score, row in scored[:k]:
        hit = dict(row)
        hit["score"] = round(score, 4)
        hit["matched_by"] = "semantic"
        out.append(hit)
    return out


def search(topic: str, *, project_id: int = 0, k: int = 3,
           mode: str = "auto") -> list[dict[str, Any]]:
    """Resolve a topic into context docs.

    ``mode``:

    * ``"auto"``   — exact key → anchor label → key substring → semantic.
    * ``"exact"``  — exact key → anchor label only.
    * ``"list"``   — the key inventory (also used when *topic* is empty).
    """
    mode = (mode or "auto").strip().lower()
    k = max(1, min(int(k or 3), MAX_TOP_K))
    topic = (topic or "").strip()

    if mode == "list" or not topic:
        return list_keys(project_id=project_id)

    key_part, section = _split_section(topic)
    rows = _scope_rows("any", project_id)
    nk = normalize_key(key_part)

    if mode in ("auto", "exact"):
        # 1. exact key
        for row in rows:
            if row["key"] == nk:
                hit = dict(row)
                hit["score"] = 1.0
                hit["matched_by"] = "exact-key"
                return [_apply_section(hit, section)]
        # 2. anchor label (through the project's manifest)
        anchor = _anchor_lookup(key_part, project_id)
        if anchor:
            hit = dict(anchor)
            hit["score"] = 1.0
            hit["matched_by"] = "anchor"
            return [_apply_section(hit, section)]
        # 3. key substring — short topics only, so a long task description
        #    cannot accidentally match an unrelated key.
        if mode == "auto" and len(key_part) <= SUBSTRING_MAX and nk:
            subs = [r for r in rows if nk in r["key"]]
            if subs:
                out = []
                for row in subs[:k]:
                    hit = dict(row)
                    hit["score"] = 0.9
                    hit["matched_by"] = "key-match"
                    out.append(_apply_section(hit, section))
                return out
        if mode == "exact":
            return []

    # 4. semantic pre-fetch
    hits = _semantic(key_part, rows, k)
    return [_apply_section(h, section) for h in hits]


def _apply_section(hit: dict[str, Any], section: str) -> dict[str, Any]:
    """Narrow a hit to one section, when the caller asked for one."""
    if section:
        hit = dict(hit)
        hit["body"] = slice_section(hit.get("body") or "", section)
        hit["section"] = section
    return hit


# --------------------------------------------------------------------------- #
# Task-driven pre-fetch
# --------------------------------------------------------------------------- #
# Words that carry no topic. Kept small on purpose: an over-eager stopword
# list would strip the very tokens a task is about.
_STOPWORDS = {
    "a", "an", "and", "the", "for", "with", "into", "from", "that", "this",
    "these", "those", "how", "what", "which", "when", "where", "who", "why",
    "does", "do", "is", "are", "was", "were", "be", "been", "being", "it",
    "its", "of", "to", "in", "on", "at", "by", "as", "or", "if", "then",
    "else", "my", "our", "your", "their", "they", "not", "but", "so", "than",
    "too", "very", "can", "will", "just", "should", "now", "new", "use",
    "using", "used", "make", "made", "need", "want", "get", "got", "add",
    "also", "more", "most", "some", "any", "all", "each", "out", "over",
    "under", "again", "further", "once", "here", "there", "about", "through",
    "during", "before", "after", "above", "below", "between", "both", "same",
    "other", "such", "only", "own", "does", "done", "have", "has", "had",
    "please", "help", "need", "let", "lets", "like", "get", "got", "way",
}

PREFETCH_MIN_OVERLAP = 0.5   # share of the smaller topic set that must match
PREFETCH_MAX_DOCS = 2
PREFETCH_MAX_CHARS = 4_000


def _stem(word: str) -> str:
    """A deliberately crude stemmer — enough to match ``credential``/``credentials``."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("es") and not word.endswith("ss"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _topic_terms(text: str) -> set[str]:
    """The content words of *text* — the words that carry its topic."""
    return {
        _stem(w)
        for w in re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", (text or "").casefold())
        if w not in _STOPWORDS
    }


def topical_overlap(topic: str, hit: dict[str, Any]) -> float:
    """How well the *topic* of a hit matches the topic of the request.

    Deliberately computed against the hit's **key, title and summary** — its
    declared subject — and not its body. A body match only says the words
    occur somewhere; a title match says the document is *about* the task.
    """
    task = _topic_terms(topic)
    if not task:
        return 0.0
    doc = _topic_terms(f"{hit.get('key', '')} {hit.get('title', '')} "
                       f"{hit.get('summary', '')}")
    if not doc:
        return 0.0
    inter = task & doc
    if not inter:
        return 0.0
    return len(inter) / min(len(task), len(doc))


def prefetch(topic: str, *, project_id: int = 0, k: int = PREFETCH_MAX_DOCS,
             min_overlap: float = PREFETCH_MIN_OVERLAP,
             max_chars: int = PREFETCH_MAX_CHARS) -> list[dict[str, Any]]:
    """Retrieve the context docs that *topic* actually needs.

    This is the orchestrated path's half of the awareness-parity contract:
    the agent does not have to know that context exists — Perun resolves the
    request into the reference material it needs and injects it *before* the
    run starts.

    The gate is deliberately conservative and **deterministic** — no embedding
    is involved, so the run path never depends on Ollama being up:

    1. an explicit key is a deliberate request and is always honoured;
    2. an anchor label resolves through the project's manifest;
    3. a short topic may name a key fragment;
    4. otherwise the request's **subject** must match the document's declared
       subject.

    A wrong injection costs tokens *and* can mislead, so the default is to
    inject nothing rather than something plausible. The total injected size
    is capped, so this can never replace the ambient context it complements.
    """
    topic = (topic or "").strip()
    if not topic:
        return []
    rows = _scope_rows("any", project_id)
    if not rows:
        return []

    out: list[dict[str, Any]] = []
    used = 0

    def _take(hit: dict[str, Any], reason: str) -> None:
        nonlocal used
        body = hit.get("body") or ""
        if used + len(body) > max_chars:
            return
        used += len(body)
        taken = dict(hit)
        taken["matched_by"] = reason
        out.append(taken)

    nk = normalize_key(topic)

    # 1. An explicit key.
    for row in rows:
        if row["key"] == nk:
            _take(row, "exact-key")
            break

    # 2. An anchor label, through the project's manifest.
    if not out:
        anchor = _anchor_lookup(topic, project_id)
        if anchor:
            _take(anchor, "anchor")

    # 3. A short topic naming a key fragment.
    if not out and len(topic) <= SUBSTRING_MAX and nk:
        for row in rows:
            if nk in row["key"]:
                _take(row, "key-match")
                if len(out) >= k:
                    break

    # 4. Subject match against every visible document.
    if not out:
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            overlap = topical_overlap(topic, row)
            if overlap >= min_overlap:
                scored.append((overlap, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        for overlap, row in scored:
            hit = dict(row)
            hit["overlap"] = round(overlap, 3)
            _take(hit, "topical")
            if len(out) >= k:
                break

    return out[:k]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_hit(hit: dict[str, Any], *, max_chars: int = MAX_INLINE_CHARS) -> str:
    """Render one hit as the text an agent reads."""
    body = hit.get("body") or ""
    lines = [
        f"[{hit.get('key')}] {hit.get('title') or ''}".rstrip(),
        f"scope={hit.get('scope')} updated={hit.get('updated_at')} "
        f"matched_by={hit.get('matched_by', 'exact-key')}",
    ]
    if hit.get("summary"):
        lines.append(f"summary: {hit['summary']}")
    if hit.get("source_file"):
        lines.append(f"source: {hit['source_file']}")
    lines.append("-" * 60)
    if len(body) > max_chars:
        sections = outline(body)[:MAX_OUTLINE_SECTIONS]
        lines.append(body[:max_chars])
        lines.append("")
        lines.append(f"... [truncated at {max_chars} of {len(body)} chars]")
        if sections:
            lines.append("Sections in this document:")
            lines.extend(f"  - {s}" for s in sections)
            lines.append(
                "Fetch one with: perun_context(\"<key>#<Section Name>\")"
            )
    else:
        lines.append(body)
    return "\n".join(lines)


def render_hits(hits: list[dict[str, Any]], *, max_chars: int = MAX_INLINE_CHARS) -> str:
    """Render a list of hits as the text an agent reads."""
    if not hits:
        return ("No context matched. Call perun_context with mode=\"list\" to see "
                "the available keys.")
    return "\n\n".join(render_hit(h, max_chars=max_chars) for h in hits)


def render_inventory(keys: list[dict[str, Any]]) -> str:
    """Render the key inventory as a compact table."""
    if not keys:
        return "No context docs are indexed yet."
    lines = ["| Key | Scope | Covers |", "|---|---|---|"]
    for k in keys:
        lines.append(f"| `{k['key']}` | {k['scope']} | {k['summary'] or k['title']} |")
    return "\n".join(lines)
