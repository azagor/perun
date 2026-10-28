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
    ollama_ok,
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

# Cache for manifest parses used by anchor resolution. Keyed by project id;
# invalidated when the manifest file's mtime or size changes.
# The cached tuple is (mtime, size, manifest_snapshot, label_embeddings).
_manifest_cache: dict[int, tuple[float, int, dict[str, Any], dict[str, list[float]]]] = {}


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
        "content_hash": row["content_hash"] or "",
        "embedded_hash": row["embedded_hash"] or "",
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
# Anchor resolution
# --------------------------------------------------------------------------- #
def _anchor_label_set(anchors: list[dict[str, Any]], docs: dict[str, str]) -> list[tuple[str, dict[str, Any]]]:
    """Return (label, anchor) pairs for manifest anchors plus synthetic doc anchors."""
    synth_labels = {
        "readme": "README",
        "agents": "Agent guide",
        "changelog": "Changelog",
        "technical": "Technical documentation",
        "codemap": "CODEMAP",
    }
    seen_paths: set[str] = set()
    pairs: list[tuple[str, dict[str, Any]]] = []
    for a in anchors:
        if not isinstance(a, dict):
            continue
        label = str(a.get("label") or "").strip()
        if label:
            pairs.append((label, dict(a)))
    for key, rel in (docs or {}).items():
        rel = str(rel or "").strip()
        if not rel or rel in seen_paths:
            continue
        seen_paths.add(rel)
        label = synth_labels.get(key, _capitalize_words(key))
        pairs.append((label, {"label": label, "kind": "doc", "path": rel}))
    pairs.append(("manifest", {"label": "manifest", "kind": "doc", "path": "perun.md"}))
    return pairs


def _embed_labels(labels: list[str]) -> dict[str, list[float]] | None:
    """Embed anchor labels when Ollama is available."""
    if not labels or not ollama_ok():
        return None
    try:
        vectors = embed_texts(labels, prefix=QUERY_PREFIX)
        return {label: vec for label, vec in zip(labels, vectors)}
    except Exception:
        return None


def _load_manifest(project_id: int) -> dict[str, Any] | None:
    """Return the parsed manifest for *project_id*, cached by mtime/size."""
    if not project_id:
        return None
    project = registry.get_project(project_id)
    if not project:
        return None
    try:
        from . import manifest as manifest_mod

        path = Path(project["path"]) / manifest_mod.MANIFEST_MD
        st = path.stat()
        cache_key = (project_id,)
        cached = _manifest_cache.get(cache_key)
        if cached and cached[:2] == (st.st_mtime, st.st_size):
            return cached[2]
        snap = manifest_mod.load(project["path"])
        data = snap.get("data") or {}
        anchors = data.get("anchors") or []
        docs = data.get("docs") or {}
        label_vectors = _embed_labels([label for label, _ in _anchor_label_set(anchors, docs)])
        _manifest_cache[cache_key] = (st.st_mtime, st.st_size, snap, label_vectors or {})
        return snap
    except Exception:
        return None


def _token_set(text: str) -> set[str]:
    """The stemmed content tokens of *text* (same rules as ``_topic_terms``)."""
    return _topic_terms(text)


def _path_tokens(path: str) -> set[str]:
    """Tokens extracted from a relative path."""
    segments = re.split(r"[\\/]", path)
    out: set[str] = set()
    for segment in segments:
        for piece in segment.split("."):
            for w in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", piece.casefold()):
                if w not in _STOPWORDS:
                    out.add(_stem(w))
    return out


def _capitalize_words(text: str) -> str:
    """Capitalise each word in a snake/kebab token."""
    return " ".join(w.capitalize() for w in re.sub(r"[_-]+", " ", text).split())


def _rank_anchors(wanted: str, anchors: list[dict[str, Any]],
                  docs: dict[str, str]) -> list[dict[str, Any]]:
    """Return anchors (plus synthetic doc anchors) sorted by match quality."""
    wanted_tokens = _token_set(wanted)
    wanted_path_tokens = _path_tokens(wanted)

    synth_labels = {
        "readme": "README",
        "agents": "Agent guide",
        "changelog": "Changelog",
        "technical": "Technical documentation",
        "codemap": "CODEMAP",
    }
    seen_paths: set[str] = set()
    all_anchors: list[dict[str, Any]] = [dict(a) for a in anchors
                                          if isinstance(a, dict)]
    for key, rel in (docs or {}).items():
        rel = str(rel or "").strip()
        if not rel or rel in seen_paths:
            continue
        seen_paths.add(rel)
        all_anchors.append({
            "label": synth_labels.get(key, _capitalize_words(key)),
            "kind": "doc",
            "path": rel,
        })
    # The manifest itself is also a document the agent may ask for.
    all_anchors.append({"label": "manifest", "kind": "doc", "path": "perun.md"})

    scored: list[tuple[int, dict[str, Any]]] = []
    for anchor in all_anchors:
        label = str(anchor.get("label") or "").strip().casefold()
        path = str(anchor.get("path") or "").strip().casefold()
        mem = str(anchor.get("memory") or "").strip()
        score = 0

        if label:
            if label == wanted:
                score = 100
            elif wanted_tokens and _token_set(label) <= wanted_tokens \
                    and any(len(t) >= 4 for t in _token_set(label)):
                score = 50
            elif label in wanted:
                score = 30
            elif wanted_tokens and wanted_tokens & _token_set(label):
                score = 10

        if path:
            if path == wanted or wanted.endswith(path) or path.endswith(wanted):
                score = max(score, 40)
            elif wanted_path_tokens and wanted_path_tokens & _path_tokens(path):
                score = max(score, 20)

        if mem and normalize_key(mem) == normalize_key(wanted):
            score = max(score, 80)

        if score:
            scored.append((score, anchor))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [anchor for _, anchor in scored]


def _anchor_path_hit(project_id: int, root: Path, anchor: dict[str, Any],
                     wanted: str, q_vec: list[float] | None = None) -> dict[str, Any] | None:
    """Resolve a file/doc anchor to a doc-chunk hit or a bounded stub."""
    path = str(anchor.get("path") or "").strip()
    label = str(anchor.get("label") or "").strip()
    if not path or not root:
        return None

    full = root / path
    # 1. If the file is already indexed in doc_chunks, return the best section.
    try:
        with db.db() as conn:
            rows = db.rows(
                conn,
                "SELECT section, content, embedding, content_hash, embedded_hash "
                "FROM doc_chunks WHERE project_id=? AND source_file=? ORDER BY section",
                (project_id, path),
            )
        if rows:
            wanted_tokens = _token_set(wanted) or _token_set(label)
            if q_vec is None and ollama_ok():
                try:
                    q_vec = embed_texts([wanted or label], prefix=QUERY_PREFIX)[0]
                except Exception:
                    q_vec = None
            best: sqlite3.Row | None = None
            best_score = -1.0
            for r in rows:
                section = r["section"] or ""
                score = 0.0
                emb_blob = r["embedding"]
                emb_hash = (r.get("embedded_hash") or "")
                content_hash = (r.get("content_hash") or "")
                if (q_vec is not None and emb_blob
                        and emb_hash and emb_hash == content_hash):
                    score = cosine_similarity(q_vec, unpack_vector(emb_blob))
                # Keyword bonuses keep short/unembedded labels working.
                if section == "(top)":
                    score += 0.05
                if wanted_tokens:
                    inter = wanted_tokens & _token_set(section)
                    score += 0.1 * len(inter)
                if label.casefold() in section.casefold():
                    score += 0.15
                if score > best_score:
                    best_score = score
                    best = r
            if best is None:
                best = rows[0]
            return {
                "key": f"doc.{normalize_key(label) or normalize_key(path)}",
                "title": label or path,
                "body": best["content"],
                "source_file": path,
                "section": best["section"],
                "scope": "project",
                "matched_by": "anchor-doc",
            }
    except Exception:
        pass

    # 2. Otherwise return a small stub so the agent knows the anchor exists and
    #    how to fetch the real content.
    stub_lines: list[str] = [f"[{label or path}] {path}"]
    kind = str(anchor.get("kind") or "").strip()
    if kind:
        stub_lines.append(f"kind={kind}")
    if full.is_file():
        stub_lines.append("status=exists")
        try:
            headings = [
                line.strip()
                for line in full.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.startswith("## ") or line.startswith("### ")
            ]
            if headings:
                stub_lines.append("Sections:")
                stub_lines.extend(f"  {h}" for h in headings[:20])
        except Exception:
            pass
    else:
        stub_lines.append("status=not found")
    stub_lines.append("")
    stub_lines.append(
        f"Fetch the full content with perun_grep or perun_read_file(\"{path}\")."
    )
    return {
        "key": f"anchor.{normalize_key(label) or normalize_key(path)}",
        "title": label or path,
        "body": "\n".join(stub_lines),
        "source_file": path,
        "scope": "project",
        "matched_by": "anchor-stub",
    }


def _manifest_cache_entry(project_id: int) -> tuple[dict[str, Any], dict[str, list[float]]] | None:
    """Return (manifest_snapshot, label_embeddings) from cache or disk."""
    cache_key = (project_id,)
    cached = _manifest_cache.get(cache_key)
    if cached is None:
        _load_manifest(project_id)
        cached = _manifest_cache.get(cache_key)
    if cached is None:
        return None
    return cached[2], cached[3]


def _embedding_best_anchor(topic: str, pairs: list[tuple[str, dict[str, Any]]],
                           label_vectors: dict[str, list[float]],
                           q_vec: list[float] | None = None) -> dict[str, Any] | None:
    """Return the anchor whose label is most similar to *topic* by embedding."""
    if not label_vectors or not pairs:
        return None
    if q_vec is None:
        try:
            q_vec = embed_texts([topic], prefix=QUERY_PREFIX)[0]
        except Exception:
            return None
    best_score = 0.0
    best: dict[str, Any] | None = None
    for label, anchor in pairs:
        vec = label_vectors.get(label)
        if not vec:
            continue
        score = cosine_similarity(q_vec, vec)
        if score > best_score:
            best_score = score
            best = anchor
    # Threshold: must be meaningfully similar, not just the least-distant option.
    if best_score < 0.55:
        return None
    return best


def _anchor_lookup(topic: str, project_id: int) -> dict[str, Any] | None:
    """Resolve an anchor *label* (or its ``memory`` key) through the manifest.

    Matches are, in order of preference:

    1. exact label or memory-key equality;
    2. token-bounded label overlap (``read the codemap`` → ``CODEMAP``);
    3. local embedding similarity over anchor labels;
    4. manifest ``docs`` entries and the manifest itself as synthetic anchors;
    5. path overlap.

    A matching anchor is returned as either the bound context doc, a
    ``doc_chunks`` section, or a small stub with fetch instructions.
    """
    if not project_id:
        return None
    entry = _manifest_cache_entry(project_id)
    if entry is None:
        return None
    snap, label_vectors = entry
    if not snap.get("found"):
        return None
    data = snap.get("data") or {}
    anchors = data.get("anchors") or []
    docs = data.get("docs") or {}
    wanted = topic.strip().casefold()

    project = registry.get_project(project_id)
    root = Path(project["path"]) if project else None

    pairs = _anchor_label_set(anchors, docs)

    # Embed the query once, then reuse it for label similarity and section
    # selection. Deterministic rules still run first.
    q_vec: list[float] | None = None
    if label_vectors:
        try:
            q_vec = embed_texts([topic], prefix=QUERY_PREFIX)[0]
        except Exception:
            q_vec = None

    # 1. Deterministic ranking.
    candidates = _rank_anchors(wanted, anchors, docs)
    for anchor in candidates:
        resolved = _resolve_anchor(project_id, root, anchor, wanted, q_vec=q_vec)
        if resolved:
            return resolved

    # 2. Embedding label similarity (local model, fallback to deterministic).
    if label_vectors and q_vec is not None:
        best = _embedding_best_anchor(topic, pairs, label_vectors, q_vec=q_vec)
        if best:
            resolved = _resolve_anchor(project_id, root, best, wanted, q_vec=q_vec)
            if resolved:
                return resolved

    return None


def _resolve_anchor(project_id: int, root: Path | None, anchor: dict[str, Any],
                    wanted: str, q_vec: list[float] | None = None) -> dict[str, Any] | None:
    """Resolve a single matched anchor into a doc, chunk, or stub."""
    mem = str(anchor.get("memory") or "").strip()
    if mem:
        doc = get(mem, project_id=project_id)
        if doc:
            return doc

    kind = str(anchor.get("kind") or "").lower()
    if kind in ("secret", "command", "url"):
        return None

    path = str(anchor.get("path") or "").strip()
    if path and root is not None:
        return _anchor_path_hit(project_id, root, anchor, wanted, q_vec=q_vec)
    return None


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
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
        # Only trust an embedding that matches the current body; an edited doc
        # indexed while Ollama was down can keep a stale vector otherwise.
        emb_hash = (row.get("embedded_hash") or "")
        content_hash = (row.get("content_hash") or "")
        if q_emb and row.get("embedding") and emb_hash and emb_hash == content_hash:
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


# Small equivalence map for forms that the suffix rules miss or over-stem.
_STEM_EQUIV = {
    "configuration": "configure",
    "configura": "configure",
    "configur": "configure",
    "deployment": "deploy",
    "releas": "release",
    "releasing": "release",
    "released": "release",
    "testing": "test",
    "indexing": "index",
    "searching": "search",
    "credential": "credential",
    "credentials": "credential",
    "validating": "validate",
    "validat": "validate",
    "orchestrating": "orchestrate",
    "orchestrat": "orchestrate",
    "routing": "route",
}


def _stem(word: str) -> str:
    """A deterministic stemmer covering plurals and common English morphology.

    No embeddings are involved, so the pre-fetch gate stays Ollama-free.
    """
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 5 and word.endswith("ings"):
        word = word[:-4]
    elif len(word) > 4 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 5 and word.endswith("ated"):
        word = word[:-3]  # validate -> valid; configure -> configure? no
    elif len(word) > 5 and word.endswith("ator"):
        word = word[:-4] + "e"
    elif len(word) > 6 and word.endswith("ations"):
        word = word[:-5]
    elif len(word) > 5 and word.endswith("ation"):
        word = word[:-4]
    elif len(word) > 5 and word.endswith("itions"):
        word = word[:-5]
    elif len(word) > 4 and word.endswith("ition"):
        word = word[:-4]
    elif len(word) > 5 and word.endswith("tions"):
        word = word[:-4]
    elif len(word) > 4 and word.endswith("tion"):
        word = word[:-3]
    elif len(word) > 5 and word.endswith("sions"):
        word = word[:-4]
    elif len(word) > 4 and word.endswith("sion"):
        word = word[:-3]
    elif len(word) > 5 and word.endswith("nesses"):
        word = word[:-5]
    elif len(word) > 4 and word.endswith("ness"):
        word = word[:-4]
    elif len(word) > 3 and word.endswith("ly"):
        word = word[:-2]
    elif len(word) > 4 and word.endswith("ed") and not word.endswith("eed"):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("er"):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("es") and not word.endswith("ss"):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return _STEM_EQUIV.get(word, word)


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

    The gate is deliberately conservative: wrong injections cost tokens and
    can mislead, so deterministic rules are tried first. If a local embedding
    model (Ollama) is available, it is used as a second-stage fallback; if not,
    the deterministic path still works.

    1. an explicit key is a deliberate request and is always honoured;
    2. an anchor label resolves through the project's manifest;
    3. a short topic may name a key fragment;
    4. the request's **subject** must match the document's declared subject;
    5. local embedding similarity over context doc subjects (when Ollama is up).

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
        remaining = max_chars - used
        if remaining <= 0:
            return
        body = hit.get("body") or ""
        if len(body) <= remaining:
            used += len(body)
            taken = dict(hit)
            taken["matched_by"] = reason
            out.append(taken)
            return
        # The first matching document is always admitted, but truncated to the
        # remaining budget so the agent gets the beginning of the right doc
        # instead of a smaller, unrelated one.
        if not out:
            note = f"\n\n... [truncated at {remaining} of {len(body)} chars]"
            if len(note) >= remaining:
                note = "\n...[truncated]"
            take_len = max(0, remaining - len(note))
            truncated = body[:take_len].rstrip() + note
            used += len(truncated)
            taken = dict(hit)
            taken["body"] = truncated
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

    # 4. Deterministic subject match against every visible document.
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

    # 5. Local embedding similarity (fallback if Ollama is up and the
    #    deterministic gate found nothing).
    if not out and ollama_ok():
        try:
            sem = _semantic(topic, rows, k * 2)
            for hit in sem:
                if hit.get("score", 0) >= 0.6:
                    _take(hit, "semantic")
                if len(out) >= k:
                    break
        except Exception:
            pass

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
