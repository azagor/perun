"""Unified, project-namespaced memory + semantic document store.

Generalises the per-project ``scripts/memory.py`` + ``tools/semantic_memory.py``
into a single store shared by every project Perun knows about:

* **facts** — atomic, agent-authored memories, scoped ``global`` (project_id=0)
  or ``project`` (project_id=N).
* **doc chunks** — Markdown section-chunks from a project's docs
  (CHANGELOG / TECHNICAL_DOCUMENTATION / CODEMAP / any ``docs/*.md``), embedded
  for semantic search and annotated with CODEMAP paths/identifiers.

Everything degrades gracefully: when Ollama is unavailable, rows are stored
without an embedding and search falls back to keyword + recency ranking.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db, registry
from .embeddings import (
    DOC_PREFIX,
    QUERY_PREFIX,
    cosine_similarity,
    embed_texts,
    extract_metadata,
    ollama_ok,
    pack_vector,
    unpack_vector,
)

FRESHNESS_HALF_LIFE_DAYS = 30.0
# Global facts are "rules for ever": they still rank below fresher ones, but
# decay so slowly that they effectively never disappear from results.
GLOBAL_FRESHNESS_HALF_LIFE_DAYS = 365.0

# Guard rails for search input and fact storage (see audit 2026-10-18).
MAX_QUERY_WORDS = 16          # keywords per query fed into the SQL LIKE tree
MAX_KEYWORD_CHARS = 64        # per-keyword truncation (LIKE pattern safety)
MAX_FACT_CHARS = 16 * 1024    # single-fact size cap (doc chunks cap at 8000)
MAX_TOP_K = 100               # upper clamp for search result limits

# Documents indexed per project by default, in preference order.
DOC_CANDIDATES = [
    "docs/CODEMAP.md",
    "docs/TECHNICAL_DOCUMENTATION.md",
    "docs/CHANGELOG.md",
    "docs/README.md",
    "AGENTS.md",
    "README.md",
]

_SENSITIVE = [
    re.compile(r"-----BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY-----"),
    re.compile(r"api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}", re.I),
    re.compile(r"api[_-]?secret\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}", re.I),
    re.compile(r"client[_-]?secret\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}", re.I),
    re.compile(r"(?:password|passwd|hasło|haslo)\s*[:=]\s*['\"]?[^\s\"']{8,}", re.I),
    re.compile(r"token\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}", re.I),
    re.compile(r"bearer\s+[A-Za-z0-9_\-]{20,}", re.I),
    re.compile(r"-----BEGIN .*?-----"),
    # Credential prefixes used "bare" (no key=value wrapper).
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                     # AWS access key id
    re.compile(r"\baws_secret_access_key\s*[:=]\s*\S{16,}", re.I),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36}\b"),              # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),                         # GitHub fine-grained PAT
    re.compile(r"\bxox[abpos]-[A-Za-z0-9\-]{10,}\b"),                        # Slack tokens
    re.compile(r"\bsk_live_[0-9a-zA-Z]{24,}\b"),                             # Stripe live keys
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),                           # Anthropic keys
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),  # JWT
    # Connection strings carrying inline credentials (postgres://user:pass@host…).
    re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@'\"\"]{1,64}:[^\s@'\"\"]{4,}@"),
    # JSON form: the quote before the colon breaks the plain ``token\s*[:=]``
    # patterns, so match quoted credential-ish keys explicitly.
    re.compile(
        r"\"[^\"]*(?:key|token|secret|pass|auth|credential)[^\"]*\"\s*:\s*\"[^\"']{12,}\"",
        re.I),
]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


def is_sensitive(text: str) -> bool:
    return any(p.search(text) for p in _SENSITIVE)


def _freshness_weight(ts: str, *, half_life_days: float | None = None) -> float:
    try:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        age_days = max((datetime.now(timezone.utc) - t).total_seconds() / 86400, 0)
        return math.pow(0.5, age_days / (half_life_days or FRESHNESS_HALF_LIFE_DAYS))
    except Exception:
        return 1.0


def _keywords(query: str) -> list[str]:
    r"""Extract search keywords: Unicode-aware, bounded in count and length.

    Bounding both keeps hostile input (100 kB of words, one 100 kB token)
    from producing oversized SQL ``LIKE`` trees or "pattern too complex"
    errors, and ``\w`` with UNICODE keeps non-ASCII (e.g. Polish) queries
    searchable.
    """
    words = re.findall(r"\w{3,}", query.casefold(), re.UNICODE)
    return [w[:MAX_KEYWORD_CHARS] for w in words[:MAX_QUERY_WORDS]]


def _embed(text: str, prefix: str) -> bytes | None:
    try:
        vec = embed_texts([text], prefix=prefix)[0]
        return pack_vector(vec)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #
def remember(content: str, *, project_id: int = 0, scope: str | None = None,
             topic: str = "general", source: str = "manual",
             meta: dict[str, Any] | None = None,
             room_id: str = "") -> dict[str, Any]:
    content = (content or "").strip()
    if not content:
        return {"ok": False, "reason": "empty"}
    if len(content) > MAX_FACT_CHARS:
        # A multi-megabyte "fact" would be inlined into every search's
        # candidate window; point the caller at the chunked doc store instead.
        return {"ok": False, "reason": f"too_long (max {MAX_FACT_CHARS} chars)"}
    if is_sensitive(content):
        return {"ok": False, "reason": "sensitive"}
    if scope is None:
        scope = "global" if not project_id else "project"
    pid = 0 if scope == "global" else project_id
    h = content_hash(content)
    ts = registry.now_iso()
    import json
    meta_json = json.dumps(meta or {})
    room = str(room_id or "").strip()
    # Compute the embedding before opening the connection: _embed performs an
    # HTTP call and a DB connection must never be held across a network call.
    emb = _embed(content, QUERY_PREFIX)
    with db.db() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO memory
                   (project_id,scope,topic,content,embedding,source,content_hash,ts,meta,room_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(scope, project_id, content_hash) DO NOTHING""",
                (pid, scope, topic, content, emb, source, h, ts, meta_json, room),
            )
        except sqlite3.OperationalError:
            cur = conn.execute(
                """INSERT INTO memory
                   (project_id,scope,topic,content,embedding,source,content_hash,ts,meta)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(scope, project_id, content_hash) DO NOTHING""",
                (pid, scope, topic, content, emb, source, h, ts, meta_json),
            )
        if cur.lastrowid:
            return {"ok": True, "id": cur.lastrowid, "embedded": emb is not None}
        existing = db.one(
            conn,
            "SELECT id FROM memory WHERE scope=? AND project_id=? AND content_hash=?",
            (scope, pid, h),
        )
        return {"ok": True, "duplicate": True, "id": existing["id"] if existing else None}


def _fact_rows(conn: sqlite3.Connection, project_id: int,
               include_global: bool, limit: int = 200) -> list[sqlite3.Row]:
    if include_global:
        return db.rows(
            conn,
            "SELECT * FROM memory WHERE project_id=? OR (scope='global' AND project_id=0) LIMIT ?",
            (project_id, limit),
        )
    return db.rows(conn, "SELECT * FROM memory WHERE project_id=? LIMIT ?", (project_id, limit))


def _keyword_clause(columns: list[str], words: list[str]) -> tuple[str, list[str]]:
    """Build a SQL LIKE clause and parameter list for keyword pre-filtering.

    ``%``/``_``/``\\`` in keywords are escaped so an underscore is a literal,
    not a single-character wildcard.
    """
    patterns = []
    for w in words:
        esc = w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        patterns.append(f"%{esc}%")
    clauses = []
    for col in columns:
        for _ in words:
            clauses.append(f"LOWER({col}) LIKE ? ESCAPE '\\'")
    return " OR ".join(clauses), patterns * len(columns)


def search_facts(query: str, project_id: int = 0, *, top_k: int = 6,
                 include_global: bool = True, q_emb: Any = None) -> list[dict[str, Any]]:
    if top_k < 1:
        return []
    query = (query or "").strip()
    if not query:
        return []
    words = _keywords(query)
    low_q = query.casefold()
    with db.db() as conn:
        if words:
            kw_clause, patterns = _keyword_clause(["content", "topic"], words)
            if include_global:
                sql = (
                    f"SELECT * FROM memory WHERE "
                    f"(project_id=? OR (scope='global' AND project_id=0)) AND ({kw_clause}) "
                    f"ORDER BY id DESC LIMIT ?"
                )
                params = (project_id, *patterns, MAX_SEARCH_CANDIDATES)
            else:
                sql = (f"SELECT * FROM memory WHERE project_id=? AND ({kw_clause}) "
                       f"ORDER BY id DESC LIMIT ?")
                params = (project_id, *patterns, MAX_SEARCH_CANDIDATES)
            rows = db.rows(conn, sql, params)
        else:
            rows = _fact_rows(conn, project_id, include_global, limit=MAX_SEARCH_CANDIDATES)
    if q_emb is None:
        try:
            q_emb = embed_texts([query], prefix=QUERY_PREFIX)[0]
        except Exception:
            q_emb = None
    scored: list[dict[str, Any]] = []
    for r in rows:
        content = r["content"] or ""
        raw = 0.0
        if q_emb and r["embedding"]:
            raw = cosine_similarity(q_emb, unpack_vector(r["embedding"]))
        low = ((r["topic"] or "") + " " + content).casefold()
        if low_q in low:
            raw += 0.3
        elif words and any(w in low for w in words):
            raw += 0.15
        # Freshness *ranks*, it does not filter: the relevance threshold is
        # applied to the raw score, and global facts ("rules for ever") decay
        # with a much longer half-life. Only the ordering score is weighted.
        if raw > 0.15:
            half_life = (GLOBAL_FRESHNESS_HALF_LIFE_DAYS
                         if r["scope"] == "global" else None)
            freshness = _freshness_weight(r["ts"], half_life_days=half_life)
            import json as _json
            meta = {}
            try:
                if "meta" in r.keys() and r["meta"]:
                    meta = _json.loads(r["meta"]) if isinstance(r["meta"], str) else (r["meta"] or {})
            except Exception:
                meta = {}
            scored.append({
                "id": r["id"],
                "source_type": "fact",
                "scope": r["scope"],
                "project_id": r["project_id"],
                "topic": r["topic"],
                "content": content,
                "source": r["source"],
                "ts": r["ts"],
                "meta": meta,
                "score": round(raw * freshness, 4),
                "_rank": raw * freshness,
            })
    scored.sort(key=lambda x: x["_rank"], reverse=True)
    for s in scored:
        s.pop("_rank", None)
    return scored[:min(top_k, MAX_TOP_K)]


def search_room_facts(query: str, room_id: str, *, project_id: int = -1,
                      top_k: int = 6) -> list[dict[str, Any]]:
    """Search facts scoped to a PerunChat room.

    Room facts are stored with ``room_id`` set and ``meta["room"] == room_id``.
    Project rooms use the real project_id; public/private rooms use
    project_id=-1. The search returns the most relevant room facts, boosting
    durable (non-transcript) facts so decisions and preferences surface above
    raw message noise.
    """
    if not room_id or not (query or "").strip():
        return []
    top_k = max(1, min(int(top_k), MAX_TOP_K))
    words = _keywords(query)
    low_q = query.casefold()
    import json as _json
    room_pat = f'%"room": {_json.dumps(str(room_id))}%'
    with db.db() as conn:
        if words:
            kw_clause, patterns = _keyword_clause(["content", "topic"], words)
            sql = (
                f"SELECT * FROM memory WHERE project_id=? AND "
                f"(room_id=? OR (room_id='' AND meta LIKE ?)) "
                f"AND ({kw_clause}) ORDER BY id DESC LIMIT ?"
            )
            params = (project_id, room_id, room_pat, *patterns, MAX_SEARCH_CANDIDATES)
        else:
            sql = (
                "SELECT * FROM memory WHERE project_id=? AND "
                "(room_id=? OR (room_id='' AND meta LIKE ?)) "
                "ORDER BY id DESC LIMIT ?"
            )
            params = (project_id, room_id, room_pat, MAX_SEARCH_CANDIDATES)
        rows = db.rows(conn, sql, params)
    try:
        q_emb = embed_texts([query], prefix=QUERY_PREFIX)[0]
    except Exception:
        q_emb = None
    scored: list[dict[str, Any]] = []
    for r in rows:
        content = r["content"] or ""
        raw = 0.0
        if q_emb and r["embedding"]:
            raw = cosine_similarity(q_emb, unpack_vector(r["embedding"]))
        low = ((r["topic"] or "") + " " + content).casefold()
        if low_q in low:
            raw += 0.3
        elif words and any(w in low for w in words):
            raw += 0.15
        # Boost durable facts slightly over raw transcripts so decisions and
        # preferences surface above message noise.
        meta = {}
        try:
            if "meta" in r.keys() and r["meta"]:
                meta = _json.loads(r["meta"]) if isinstance(r["meta"], str) else (r["meta"] or {})
        except Exception:
            meta = {}
        if meta.get("type") != "transcript":
            raw += 0.05
        if raw > 0.15:
            freshness = _freshness_weight(r["ts"])
            scored.append({
                "id": r["id"],
                "source_type": "fact",
                "scope": r["scope"],
                "project_id": r["project_id"],
                "topic": r["topic"],
                "content": content,
                "source": r["source"],
                "ts": r["ts"],
                "meta": meta,
                "score": round(raw * freshness, 4),
                "_rank": raw * freshness,
            })
    scored.sort(key=lambda x: x["_rank"], reverse=True)
    for s in scored:
        s.pop("_rank", None)
    return scored[:top_k]


# --------------------------------------------------------------------------- #
# Document chunks
# --------------------------------------------------------------------------- #
MAX_CHUNK_CHARS = 8000
MAX_SEARCH_CANDIDATES = 200


def chunk_markdown(text: str) -> list[tuple[str, str]]:
    """Split Markdown into ``(section, content)`` pairs at level-2 headings.

    Oversized sections are split further so that no single chunk exceeds
    ``MAX_CHUNK_CHARS``. This keeps embedding calls bounded and avoids storing
    pathological multi-megabyte rows.
    """
    text = text.replace("\r\n", "\n")
    sections: list[tuple[str, str]] = []
    current_title = "(top)"
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if not body:
            return
        # A buffer holding nothing but headings carries no content worth
        # embedding (the title already lives in the section key).
        if all(l.lstrip().startswith("#") for l in body.splitlines()):
            return
        if len(body) <= MAX_CHUNK_CHARS:
            sections.append((current_title, body))
            return
        # Split the section into roughly equal parts under the cap.
        part_no = 1
        start = 0
        while start < len(body):
            end = start + MAX_CHUNK_CHARS
            if end >= len(body):
                part = body[start:].strip()
                if part:
                    sections.append((f"{current_title} (part {part_no})", part))
                break
            # Try to break at a newline to keep chunks readable.
            nl = body.rfind("\n", start, end)
            if nl > start:
                end = nl
            part = body[start:end].strip()
            if part:
                sections.append((f"{current_title} (part {part_no})", part))
                part_no += 1
            start = end

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            buf = []
            current_title = line[3:].strip() or "(untitled)"
        else:
            buf.append(line)
    flush()
    if not sections and text.strip():
        sections.append(("(document)", text.strip()[:MAX_CHUNK_CHARS]))
    # Two identical headings in one file must not silently overwrite each
    # other under the UNIQUE(project, source_file, section) key.
    seen: dict[str, int] = {}
    deduped: list[tuple[str, str]] = []
    for title, body in sections:
        n = seen.get(title, 0) + 1
        seen[title] = n
        deduped.append((title if n == 1 else f"{title} ({n})", body))
    return deduped


def _index_text_impl(project_id: int, source_file: str, text: str) -> tuple[int, int]:
    """Index/refresh one document's chunks.

    Returns ``(upserted, skipped_sensitive)``. Secret-bearing chunks are never
    persisted or embedded. Stale chunks (renamed/removed headings) are deleted
    so the index always mirrors the file. The DB connection is never held
    across the embedding network call.
    """
    ts = registry.now_iso()
    all_sections = chunk_markdown(text)
    sections = [
        (section, content) for section, content in all_sections
        # One control pass: a secret in a README would otherwise leave the
        # machine via the embedding call and stay retrievable forever.
        if not is_sensitive(content)
    ]
    skipped_sensitive = len(all_sections) - len(sections)
    # Phase 1: decide what changed without holding a connection open.
    with db.db() as conn:
        rows = db.rows(
            conn,
            "SELECT section, content_hash FROM doc_chunks WHERE project_id=? AND source_file=?",
            (project_id, source_file),
        )
    existing = {r["section"]: r["content_hash"] for r in rows}
    kept_sections = {section for section, _ in sections}
    stale = [s for s in existing if s not in kept_sections]
    changed: list[tuple[str, str, str]] = []
    for section, content in sections:
        h = content_hash(content)
        if existing.get(section) == h:
            continue
        changed.append((section, content, h))
    if not changed and not stale:
        return 0, skipped_sensitive
    # Phase 2: embed outside any DB connection (network call, up to 180 s).
    vectors: list = [None] * len(changed)
    if changed:
        try:
            vectors = embed_texts([c for _, c, _ in changed], prefix=DOC_PREFIX)
        except Exception:
            vectors = [None] * len(changed)
    # Phase 3: persist.
    with db.db() as conn:
        for (section, content, h), vec in zip(changed, vectors):
            emb = pack_vector(vec) if vec is not None else None
            conn.execute(
                """INSERT INTO doc_chunks
                   (project_id,source_file,section,content,embedding,content_hash,
                    embedded_hash,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(project_id,source_file,section) DO UPDATE SET
                     content=excluded.content,
                     embedding=CASE WHEN excluded.embedding IS NOT NULL
                                     THEN excluded.embedding
                                     ELSE CASE WHEN doc_chunks.embedded_hash = excluded.content_hash
                                               THEN doc_chunks.embedding END END,
                     content_hash=excluded.content_hash,
                     embedded_hash=CASE WHEN excluded.embedding IS NOT NULL
                                        THEN excluded.content_hash
                                        ELSE CASE WHEN doc_chunks.embedded_hash = excluded.content_hash
                                                  THEN doc_chunks.embedded_hash END END,
                     updated_at=excluded.updated_at""",
                (project_id, source_file, section, content, emb, h, h if emb else "", ts),
            )
        if stale:
            marks = ",".join("?" for _ in stale)
            conn.execute(
                f"DELETE FROM doc_chunks WHERE project_id=? AND source_file=? "
                f"AND section IN ({marks})",
                (project_id, source_file, *stale),
            )
    return len(changed), skipped_sensitive


def index_text(project_id: int, source_file: str, text: str) -> int:
    """Index/refresh one document's chunks. Returns number of chunks upserted."""
    return _index_text_impl(project_id, source_file, text)[0]


def index_project_docs(project_id: int) -> dict[str, Any]:
    """Index the standard doc set for a project (files that exist)."""
    project = registry.get_project(project_id)
    if not project:
        return {"ok": False, "reason": "no such project"}
    root = Path(project["path"])
    indexed: list[str] = []
    total = 0
    skipped_sensitive = 0
    for rel in DOC_CANDIDATES:
        f = root / rel
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            upserted, skipped = _index_text_impl(project_id, rel, text)
            total += upserted
            skipped_sensitive += skipped
            indexed.append(rel)
    registry.log_activity(project_id, "index",
                          f"Indexed {total} chunk(s) from {len(indexed)} doc(s)",
                          {"files": indexed, "skipped_sensitive": skipped_sensitive})
    return {"ok": True, "files": indexed, "chunks": total,
            "skipped_sensitive": skipped_sensitive}


def search_docs(query: str, project_id: int = 0, *, top_k: int = 6,
                all_projects: bool = False, q_emb: Any = None) -> list[dict[str, Any]]:
    if top_k < 1:
        return []
    query = (query or "").strip()
    if not query:
        return []
    words = _keywords(query)
    low_q = query.casefold()
    with db.db() as conn:
        if words:
            kw_clause, patterns = _keyword_clause(["content"], words)
            if all_projects:
                sql = (f"SELECT * FROM doc_chunks WHERE {kw_clause} "
                       f"ORDER BY id DESC LIMIT ?")
                params = (*patterns, MAX_SEARCH_CANDIDATES)
            else:
                sql = (f"SELECT * FROM doc_chunks WHERE project_id=? AND ({kw_clause}) "
                       f"ORDER BY id DESC LIMIT ?")
                params = (project_id, *patterns, MAX_SEARCH_CANDIDATES)
            rows = db.rows(conn, sql, params)
        else:
            if all_projects:
                rows = db.rows(conn, "SELECT * FROM doc_chunks ORDER BY id DESC LIMIT ?",
                               (MAX_SEARCH_CANDIDATES,))
            else:
                rows = db.rows(conn,
                               "SELECT * FROM doc_chunks WHERE project_id=? "
                               "ORDER BY id DESC LIMIT ?",
                               (project_id, MAX_SEARCH_CANDIDATES))
    if q_emb is None:
        try:
            q_emb = embed_texts([query], prefix=QUERY_PREFIX)[0]
        except Exception:
            q_emb = None
    scored: list[dict[str, Any]] = []
    for r in rows:
        content = r["content"] or ""
        score = 0.0
        if q_emb and r["embedding"]:
            score = cosine_similarity(q_emb, unpack_vector(r["embedding"]))
        low = content.casefold()
        if low_q in low:
            score += 0.3
        elif words:
            hits = sum(1 for w in words if w in low)
            score += 0.05 * min(hits, 4)
        if score > 0.15:
            meta = extract_metadata(content)
            scored.append({
                "id": r["id"],
                "source_type": "doc",
                "project_id": r["project_id"],
                "source_file": r["source_file"],
                "section": r["section"],
                "content": content,
                "paths": meta["paths"],
                "identifiers": meta["identifiers"],
                "ts": r["updated_at"],
                "score": round(score, 4),
            })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# --------------------------------------------------------------------------- #
# Unified search
# --------------------------------------------------------------------------- #
def unified_search(query: str, project_id: int = 0, *, k: int = 6,
                   include_facts: bool = True, include_docs: bool = True,
                   include_global: bool = True,
                   all_projects: bool = False, q_emb: Any = None) -> list[dict[str, Any]]:
    if k < 1 or not (query or "").strip():
        return []
    # Embed the query once and share it with both sources (audit 2026-10-18:
    # two independent embedding calls per unified_search).
    if q_emb is None and (include_facts or include_docs):
        try:
            q_emb = embed_texts([query.strip()], prefix=QUERY_PREFIX)[0]
        except Exception:
            q_emb = None
    # Fetch more candidates from each source than the final k, then pool and
    # truncate once so a high-scoring doc is not displaced by lower-scoring facts.
    candidates: list[dict[str, Any]] = []
    if include_facts:
        candidates += search_facts(query, project_id, top_k=k * 2,
                                   include_global=include_global, q_emb=q_emb)
    if include_docs:
        candidates += search_docs(query, project_id, top_k=k * 2,
                                  all_projects=all_projects, q_emb=q_emb)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:k]


def stats(project_id: int | None = None) -> dict[str, Any]:
    with db.db() as conn:
        out: dict[str, Any] = {}
        if project_id:
            out["facts_project"] = db.one(
                conn, "SELECT COUNT(*) c FROM memory WHERE project_id=?", (project_id,))["c"]
            out["chunks"] = db.one(
                conn, "SELECT COUNT(*) c FROM doc_chunks WHERE project_id=?", (project_id,))["c"]
        out["facts_global"] = db.one(
            conn, "SELECT COUNT(*) c FROM memory WHERE scope='global'")["c"]
        out["facts_total"] = db.one(conn, "SELECT COUNT(*) c FROM memory")["c"]
        out["chunks_total"] = db.one(conn, "SELECT COUNT(*) c FROM doc_chunks")["c"]
        embedded = db.one(
            conn, "SELECT COUNT(*) c FROM memory WHERE embedding IS NOT NULL")["c"]
        out["facts_embedded"] = embedded
        out["facts_pending_embed"] = out["facts_total"] - embedded
        out["chunks_pending_embed"] = db.one(
            conn,
            "SELECT COUNT(*) c FROM doc_chunks "
            "WHERE embedding IS NULL OR COALESCE(embedded_hash,'') != content_hash",
        )["c"]
    # Health-check must not run inside the connection context (network call).
    out["ollama_ok"] = ollama_ok()
    return out


def recent_facts(project_id: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
    with db.db() as conn:
        if project_id:
            rows = db.rows(
                conn,
                "SELECT * FROM memory WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit))
        else:
            rows = db.rows(conn, "SELECT * FROM memory ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {k: r[k] for k in ("id", "project_id", "scope", "topic", "content", "source", "ts")}
            for r in rows
        ]


def prune(project_id: int | None = None, keep: int = 200,
          min_age_days: float = 30.0) -> int:
    from datetime import timedelta
    keep = max(0, int(keep))       # negatives are nonsense; keep=0 = "delete all eligible"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    with db.db() as conn:
        where = "project_id=?" if project_id else "1"
        params: tuple = (project_id,) if project_id else ()
        # Scope the retention window to rows the outer DELETE could actually
        # delete (same filter, excluding global facts).
        retention_where = f"{where} AND scope!='global'"
        cur = conn.execute(
            f"DELETE FROM memory WHERE {where} AND scope!='global' AND ts < ? "
            f"AND id NOT IN (SELECT id FROM memory WHERE {retention_where} ORDER BY id DESC LIMIT ?)",
            (*params, cutoff, *params, keep))
        return cur.rowcount


def reembed_missing(limit: int = 64) -> dict[str, Any]:
    """Embed rows that are missing a vector (or whose chunk vector is stale).

    Facts and doc chunks written while Ollama was down used to stay vector-less
    forever; chunks edited during an outage kept the old content's vector.
    Batch-embeds up to *limit* rows per call so one invocation stays bounded.
    """
    limit = max(1, int(limit))
    embedded_facts = 0
    embedded_chunks = 0
    with db.db() as conn:
        fact_rows = db.rows(
            conn,
            "SELECT id, content FROM memory WHERE embedding IS NULL LIMIT ?",
            (limit,),
        )
    for r in fact_rows:
        vec = _embed(r["content"] or "", QUERY_PREFIX)
        if vec is None:
            break  # Ollama went away mid-run; try again next time
        with db.db() as conn:
            conn.execute("UPDATE memory SET embedding=? WHERE id=?", (vec, r["id"]))
        embedded_facts += 1
    with db.db() as conn:
        chunk_rows = db.rows(
            conn,
            "SELECT id, content, content_hash FROM doc_chunks "
            "WHERE embedding IS NULL OR COALESCE(embedded_hash,'') != content_hash "
            "LIMIT ?",
            (limit,),
        )
    # Re-embed in batches; a None vector invalidates the whole batch, so fall
    # back to one-by-one to salvage what we can.
    if chunk_rows:
        try:
            vecs = embed_texts([r["content"] or "" for r in chunk_rows], prefix=DOC_PREFIX)
        except Exception:
            vecs = [None] * len(chunk_rows)
        if any(v is None for v in vecs):
            vecs = [_embed(r["content"] or "", DOC_PREFIX) for r in chunk_rows]
        for r, vec in zip(chunk_rows, vecs):
            if vec is None:
                continue
            with db.db() as conn:
                conn.execute(
                    "UPDATE doc_chunks SET embedding=?, embedded_hash=? WHERE id=?",
                    (vec, r["content_hash"], r["id"]))
            embedded_chunks += 1
    return {"ok": True, "facts_embedded": embedded_facts,
            "chunks_embedded": embedded_chunks}


def import_legacy_memory(legacy_path: str | Path | None = None) -> dict[str, Any]:
    """Copy facts from a legacy ``~/.config/opencode/memory.db`` into Perun.

    All imported rows become ``global`` scope so they remain visible to every
    project (the old DB was already shared across projects).
    """
    path = Path(legacy_path) if legacy_path else Path.home() / ".config" / "opencode" / "memory.db"
    if not path.is_file():
        return {"ok": False, "reason": f"not found: {path}"}
    imported = 0
    skipped = 0
    src = sqlite3.connect(str(path))
    src.row_factory = sqlite3.Row
    try:
        table = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "memories" not in table:
            return {"ok": False, "reason": "no memories table"}
        for r in src.execute("SELECT topic,content,source,embedding FROM memories ORDER BY id"):
            content = (r["content"] or "").strip()
            if not content or content.startswith("Subsystem:"):
                # CODEMAP subsystem rows are re-indexed per project instead.
                skipped += 1
                continue
            topic = "codemap" if r["topic"] == "codemap" else (r["topic"] or "general")
            res = remember(content, scope="global", topic=topic,
                           source=r["source"] or "legacy")
            if res.get("ok") and not res.get("duplicate"):
                imported += 1
            else:
                skipped += 1
    finally:
        src.close()
    registry.log_activity(None, "memory",
                          f"Imported {imported} legacy fact(s) from {path.name}")
    return {"ok": True, "imported": imported, "skipped": skipped}
