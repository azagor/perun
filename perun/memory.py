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
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
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
MAX_SEARCH_CANDIDATES = 1000  # hard ceiling on rows scored per source

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "above", "below",
    "between", "under", "and", "but", "or", "yet", "so", "if", "because",
    "although", "though", "while", "where", "when", "that", "this", "these",
    "those", "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "us", "them", "my", "your", "his", "its", "our", "their", "what", "which",
    "who", "whom", "whose", "how", "why", "where", "all", "any", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "not",
    "only", "own", "same", "than", "too", "very", "just", "now", "then",
    "also", "here", "there", "up", "down", "out", "off", "over", "again",
    "further", "once", "about", "against", "until", "since", "while", "per",
}

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
    return [
        w[:MAX_KEYWORD_CHARS]
        for w in words
        if w not in _STOPWORDS
    ][:MAX_QUERY_WORDS]


def _embed(text: str, prefix: str) -> bytes | None:
    try:
        vec = embed_texts([text], prefix=prefix)[0]
        return pack_vector(vec)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #
def _normalize_kind(kind: str, meta: dict[str, Any] | None) -> str:
    kind_in = str(kind or "").strip().lower()
    if kind_in and kind_in in ("rule", "decision", "note", "log", "transcript"):
        return kind_in
    if meta:
        mtype = str(meta.get("type") or "").strip().lower()
        if mtype == "decision":
            return "decision"
        if mtype == "transcript":
            return "transcript"
    return "note"


def _cosine_near_duplicate(content: str, project_id: int, scope: str,
                           threshold: float = 0.93) -> dict[str, Any] | None:
    """Return an existing fact whose embedding is semantically very close."""
    try:
        emb = embed_texts([content], prefix=QUERY_PREFIX)[0]
    except Exception:
        return None
    with db.db() as conn:
        rows = db.rows(
            conn,
            "SELECT id, content, embedding FROM memory "
            "WHERE scope=? AND project_id=? AND embedding IS NOT NULL AND valid_to=''",
            (scope, project_id),
        )
    best: tuple[float, sqlite3.Row | None] = (0.0, None)
    for r in rows:
        try:
            vec = unpack_vector(r["embedding"])
        except Exception:
            continue
        sim = cosine_similarity(emb, vec)
        if sim > best[0]:
            best = (sim, r)
    if best[0] >= threshold and best[1]:
        return {"id": best[1]["id"], "score": round(best[0], 4), "content": best[1]["content"]}
    return None


def remember(content: str, *, project_id: int = 0, scope: str | None = None,
             topic: str = "general", source: str = "manual",
             meta: dict[str, Any] | None = None,
             room_id: str = "", kind: str = "",
             _supersede_id: int | None = None,
             valid_to: str = "", merge_near_duplicates: bool = True) -> dict[str, Any]:
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
    kind_in = _normalize_kind(kind, meta)
    valid = str(valid_to or "").strip()

    # Supersede path: replace an existing fact with this one.
    if _supersede_id:
        with db.db() as conn:
            old = db.one(conn, "SELECT id FROM memory WHERE id=?", (_supersede_id,))
        if old:
            # Compute the embedding *outside* the DB context: _embed performs an
            # HTTP call and a DB connection must never be held across a network call.
            try:
                emb = _embed(content, QUERY_PREFIX)
            except Exception:
                emb = None
            with db.db() as conn:
                conn.execute(
                    "UPDATE memory SET project_id=?, scope=?, topic=?, content=?, "
                    "embedding=?, source=?, content_hash=?, ts=?, meta=?, room_id=?, "
                    "kind=?, valid_to=? WHERE id=?",
                    (pid, scope, topic, content, emb, source, h, ts, meta_json, room,
                     kind_in, valid, _supersede_id),
                )
            return {"ok": True, "id": _supersede_id, "superseded": True,
                    "embedded": emb is not None}

    with db.db() as conn:
        existing = db.one(
            conn,
            "SELECT id FROM memory WHERE scope=? AND project_id=? AND content_hash=?",
            (scope, pid, h),
        )
        if existing:
            return {"ok": True, "duplicate": True, "id": existing["id"]}

    # Near-duplicate merge for durable kinds (rules/decisions).
    if merge_near_duplicates and kind_in in ("rule", "decision"):
        dup = _cosine_near_duplicate(content, pid, scope)
        if dup:
            # Compute the embedding outside the DB context.
            try:
                emb = _embed(content, QUERY_PREFIX)
            except Exception:
                emb = None
            with db.db() as conn:
                conn.execute(
                    "UPDATE memory SET content=?, embedding=?, content_hash=?, ts=?, "
                    "meta=?, kind=?, valid_to=? WHERE id=?",
                    (content, emb, h, ts, meta_json, kind_in, valid, dup["id"]),
                )
            return {"ok": True, "id": dup["id"], "merged": True,
                    "previous_content": dup["content"], "score": dup["score"]}

    # Compute the embedding after the duplicate check: _embed performs an
    # HTTP call and a DB connection must never be held across a network call.
    emb = _embed(content, QUERY_PREFIX)
    with db.db() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO memory
                   (project_id,scope,topic,content,embedding,source,content_hash,ts,meta,room_id,kind,
                    last_used_at,hit_count,valid_to)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(scope, project_id, content_hash) DO NOTHING""",
                (pid, scope, topic, content, emb, source, h, ts, meta_json, room, kind_in, "", 0, valid),
            )
        except sqlite3.OperationalError:
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


def touch_fact(fact_id: int) -> bool:
    """Bump usage metrics for a fact. Callers should not fail if this fails."""
    try:
        ts = registry.now_iso()
        with db.db() as conn:
            cur = conn.execute(
                "UPDATE memory SET hit_count = hit_count + 1, last_used_at = ? "
                "WHERE id = ?",
                (ts, fact_id),
            )
            return cur.rowcount > 0
    except Exception:
        return False


def forget(fact_id: int) -> dict[str, Any]:
    """Delete one fact by id. Returns whether a row was removed."""
    with db.db() as conn:
        cur = conn.execute("DELETE FROM memory WHERE id=?", (fact_id,))
        return {"ok": True, "removed": cur.rowcount > 0, "id": fact_id}


def supersede(fact_id: int, content: str, *, topic: str | None = None,
              meta: dict[str, Any] | None = None, kind: str = "",
              valid_to: str = "") -> dict[str, Any]:
    """Replace an existing fact, preserving its id for stable references."""
    import json as _json
    content = (content or "").strip()
    if not content:
        return {"ok": False, "reason": "empty"}
    with db.db() as conn:
        old = db.one(conn, "SELECT * FROM memory WHERE id=?", (fact_id,))
    if not old:
        return {"ok": False, "reason": "not_found"}
    old_meta = {}
    try:
        if old["meta"]:
            old_meta = _json.loads(old["meta"])
    except Exception:
        pass
    return remember(
        content,
        project_id=old["project_id"],
        scope=old["scope"],
        topic=topic if topic is not None else (old["topic"] or "general"),
        source=old["source"] or "manual",
        meta=meta if meta is not None else old_meta,
        room_id=old["room_id"] or "",
        kind=kind or old["kind"] or "note",
        _supersede_id=fact_id,
        valid_to=valid_to,
        merge_near_duplicates=False,
    )


def _keyword_boost(query_low: str, words: list[str], content: str,
                    topic: str | None = "") -> float:
    """Additive bonus for lexical overlap so exact matches rise above noise.

    The bonus is capped below the semantic range (0..1) so a strong unrelated
    embedding still wins, but a genuine keyword hit beats a coincidental
    embedding similarity and short unembedded facts remain discoverable.
    """
    low = ((topic or "") + " " + (content or "")).casefold()
    if query_low in low:
        return 0.25
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in low)
    return 0.015 * min(hits, 4)


def search_facts(query: str, project_id: int = 0, *, top_k: int = 6,
                 include_global: bool = True, q_emb: Any = None) -> list[dict[str, Any]]:
    if top_k < 1:
        return []
    query = (query or "").strip()
    if not query:
        return []
    words = _keywords(query)
    low_q = query.casefold()
    now = registry.now_iso()
    with db.db() as conn:
        # Filter out expired facts; empty valid_to means no expiry.
        expiry_clause = "AND (valid_to='' OR valid_to > ?)"
        if words:
            kw_clause, patterns = _keyword_clause(["content", "topic"], words)
            if include_global:
                sql = (
                    f"SELECT * FROM memory WHERE "
                    f"(project_id=? OR (scope='global' AND project_id=0)) AND ({kw_clause}) "
                    f"{expiry_clause} LIMIT ?"
                )
                params = (project_id, *patterns, now, MAX_SEARCH_CANDIDATES)
            else:
                sql = (f"SELECT * FROM memory WHERE project_id=? AND ({kw_clause}) "
                       f"{expiry_clause} LIMIT ?")
                params = (project_id, *patterns, now, MAX_SEARCH_CANDIDATES)
            rows = db.rows(conn, sql, params)
        else:
            if include_global:
                sql = (
                    f"SELECT * FROM memory WHERE "
                    f"(project_id=? OR (scope='global' AND project_id=0)) "
                    f"{expiry_clause} LIMIT ?"
                )
                params = (project_id, now, MAX_SEARCH_CANDIDATES)
            else:
                sql = (
                    f"SELECT * FROM memory WHERE project_id=? "
                    f"{expiry_clause} LIMIT ?"
                )
                params = (project_id, now, MAX_SEARCH_CANDIDATES)
            rows = db.rows(conn, sql, params)
    if q_emb is None:
        try:
            q_emb = embed_texts([query], prefix=QUERY_PREFIX)[0]
        except Exception:
            q_emb = None

    scored: list[dict[str, Any]] = []
    touched_ids: list[int] = []
    for r in rows:
        content = r["content"] or ""
        sem = 0.0
        if q_emb and r["embedding"]:
            try:
                sem = cosine_similarity(q_emb, unpack_vector(r["embedding"]))
            except Exception:
                sem = 0.0
        # Calibrated keyword boost: small additive, not a dominant signal.
        kw = _keyword_boost(low_q, words, content, r["topic"])
        score = sem + kw
        # Freshness is a tie-breaker, not a multiplier, so global rules stay
        # competitive without being erased by age.
        half_life = (GLOBAL_FRESHNESS_HALF_LIFE_DAYS
                     if r["scope"] == "global" else None)
        freshness = _freshness_weight(r["ts"], half_life_days=half_life)
        score += 0.001 * freshness
        # Popularity tie-breaker.
        score += 0.0001 * min(r["hit_count"], 100)
        if score > 0.15:
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
                "score": round(score, 4),
                "_sem": sem,
                "_kw": kw,
            })
            touched_ids.append(r["id"])
    scored.sort(key=lambda x: (x["score"], x["_sem"], x["_kw"]), reverse=True)
    for s in scored:
        s.pop("_sem", None)
        s.pop("_kw", None)
    # Bump usage for returned facts, but don't let metric updates block search.
    for fid in touched_ids[:min(top_k, MAX_TOP_K)]:
        try:
            touch_fact(fid)
        except Exception:
            pass
    out = scored[:min(top_k, MAX_TOP_K)]
    _record_usage("search_facts", project_id, query, len(out))
    return out


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
                f"AND ({kw_clause}) LIMIT ?"
            )
            params = (project_id, room_id, room_pat, *patterns, MAX_SEARCH_CANDIDATES)
        else:
            sql = (
                "SELECT * FROM memory WHERE project_id=? AND "
                "(room_id=? OR (room_id='' AND meta LIKE ?)) "
                "LIMIT ?"
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
        sem = 0.0
        if q_emb and r["embedding"]:
            try:
                sem = cosine_similarity(q_emb, unpack_vector(r["embedding"]))
            except Exception:
                sem = 0.0
        kw = _keyword_boost(low_q, words, content, r["topic"])
        score = sem + kw
        meta = {}
        try:
            if "meta" in r.keys() and r["meta"]:
                meta = _json.loads(r["meta"]) if isinstance(r["meta"], str) else (r["meta"] or {})
        except Exception:
            meta = {}
        # Durable (non-transcript) facts get a small boost over raw transcripts
        # so decisions and preferences surface above message noise.
        if meta.get("type") != "transcript":
            score += 0.03
        # Freshness is a tie-breaker, not a multiplier.
        score += 0.001 * _freshness_weight(r["ts"])
        if score > 0.15:
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
                "score": round(score, 4),
                "_sem": sem,
                "_kw": kw,
            })
    scored.sort(key=lambda x: (x["score"], x["_sem"], x["_kw"]), reverse=True)
    for s in scored:
        s.pop("_sem", None)
        s.pop("_kw", None)
    return scored[:top_k]


# --------------------------------------------------------------------------- #
# Document chunks
# --------------------------------------------------------------------------- #
MAX_CHUNK_CHARS = 8000
CODEMAP_TARGET_CHUNK = 2500


def _split_oversized(title: str, content: str, max_chars: int = CODEMAP_TARGET_CHUNK,
                     heading_prefix: str = "###") -> list[tuple[str, str]]:
    """Split a single oversized content block, repeating the heading in each part."""
    if len(content) <= max_chars:
        return [(title, f"{heading_prefix} {title.split(' › ')[-1]}\n\n{content}".strip())]
    heading = f"{heading_prefix} {title.split(' › ')[-1]}"
    header_len = len(heading) + 2
    available = max(1, max_chars - header_len)
    parts: list[str] = []
    start = 0
    length = len(content)
    while start < length:
        end = min(start + available, length)
        if end < length:
            nl = content.rfind("\n", start, end)
            if nl > start:
                end = nl
        part = content[start:end].strip()
        if part:
            parts.append(f"{heading}\n\n{part}")
        if end == start:
            end = start + available
        start = end
    return [(title if i == 0 else f"{title} (part {i + 1})", p)
            for i, p in enumerate(parts)]


def _chunk_codemap(text: str) -> list[tuple[str, str]]:
    """Split a CODEMAP-style document at ``###`` modules.

    Each chunk is titled ``<H2> › <H3>`` so a retrieval names the file it
    describes, and consecutive small modules are grouped until
    ``CODEMAP_TARGET_CHUNK`` is reached.
    """
    text = text.replace("\r\n", "\n")
    parts = re.split(r"(?m)^## ", text)
    sections: list[tuple[str, str]] = []

    top = parts[0].strip() if parts else ""
    if top:
        sections.append(("(top)", top))

    h3_heading_re = re.compile(r"(?m)^### ")

    for part in parts[1:]:
        lines = part.splitlines()
        h2 = lines[0].strip() if lines else "(untitled)"
        body = "\n".join(lines[1:]).strip()
        if not body:
            continue

        if not h3_heading_re.search(body):
            sections.append((h2, f"## {h2}\n\n{body}"))
            continue

        h3_parts = h3_heading_re.split(body)
        prefix = h3_parts[0].strip()
        group: list[str] = []
        group_titles: list[str] = []
        group_len = 0

        def flush_group() -> None:
            if not group or not group_titles:
                return
            title = f"{h2} › {group_titles[0]}"
            if len(group_titles) > 1:
                title += f" (+{len(group_titles) - 1} more)"
            sections.append((title, "\n\n".join(group).strip()))

        for i, sub in enumerate(h3_parts[1:], start=1):
            sub_lines = sub.splitlines()
            h3_title = sub_lines[0].strip() if sub_lines else "(untitled)"
            h3_body = "\n".join(sub_lines[1:]).strip()
            if i == 1 and prefix:
                h3_body = f"{prefix}\n\n{h3_body}".strip()
            block_text = f"### {h3_title}\n\n{h3_body}".strip()

            if len(block_text) > CODEMAP_TARGET_CHUNK:
                if group:
                    flush_group()
                    group = []
                    group_titles = []
                    group_len = 0
                sections.extend(_split_oversized(
                    f"{h2} › {h3_title}", h3_body, CODEMAP_TARGET_CHUNK))
                continue

            if group and group_len + 2 + len(block_text) > CODEMAP_TARGET_CHUNK:
                flush_group()
                group = [block_text]
                group_titles = [h3_title]
                group_len = len(block_text)
            else:
                group.append(block_text)
                group_titles.append(h3_title)
                group_len += (2 if group_len else 0) + len(block_text)

        if group:
            flush_group()

    seen: dict[str, int] = {}
    deduped: list[tuple[str, str]] = []
    for title, body in sections:
        n = seen.get(title, 0) + 1
        seen[title] = n
        deduped.append((title if n == 1 else f"{title} ({n})", body))
    return deduped


def chunk_markdown(text: str, source_file: str = "") -> list[tuple[str, str]]:
    """Split Markdown into ``(section, content)`` pairs at level-2 headings.

    Oversized sections are split further so that no single chunk exceeds
    ``MAX_CHUNK_CHARS``. CODEMAP-style documents are split at ``###`` modules
    for module-granular retrieval.
    """
    src = source_file.replace("\\", "/")
    if src.endswith("CODEMAP.md") or (
        text.strip() and text.splitlines()[0].upper().startswith("#") and "CODEMAP" in text.splitlines()[0]
    ):
        return _chunk_codemap(text)

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


CHANGELOG_REL = "docs/CHANGELOG.md"
CHANGELOG_FULL_INDEX_MAX_CHARS = 20_000
RELEASE_INDEX_SECTION = "Release index"
RELEASE_INDEX_MAX_LINES = 120


def _changelog_headings(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out = []
    count = 0
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if re.match(r"^##\s+.+", s):
            if "(part" in s.lower():
                continue
            out.append(f"- {s[3:].strip()}")
            count += 1
            if count >= RELEASE_INDEX_MAX_LINES:
                break
    if not out:
        return ""
    body = "## " + RELEASE_INDEX_SECTION + "\n\n" + "\n".join(out)
    body += "\n\n… older releases truncated in the index; grep the file for full details."
    return body


def _prepare_changelog(rel: str, text: str) -> str:
    if rel.replace("\\", "/") != CHANGELOG_REL:
        return text
    if len(text or "") <= CHANGELOG_FULL_INDEX_MAX_CHARS:
        return text
    h = _changelog_headings(text)
    if not h:
        return text
    return h


def _index_text_impl(project_id: int, source_file: str, text: str) -> tuple[int, int]:
    """Index/refresh one document's chunks.

    Returns ``(upserted, skipped_sensitive)``. Secret-bearing chunks are never
    persisted or embedded. Stale chunks (renamed/removed headings) are deleted
    so the index always mirrors the file. The DB connection is never held
    across the embedding network call.
    """
    ts = registry.now_iso()
    src = source_file.replace("\\", "/")
    text = _prepare_changelog(src, text)
    all_sections = chunk_markdown(text, source_file=source_file)
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
            chunk = 900
            for i in range(0, len(stale), chunk):
                batch = stale[i:i + chunk]
                marks = ",".join("?" for _ in batch)
                conn.execute(
                    f"DELETE FROM doc_chunks WHERE project_id=? AND source_file=? "
                    f"AND section IN ({marks})",
                    (project_id, source_file, *batch),
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
                       f"LIMIT ?")
                params = (*patterns, MAX_SEARCH_CANDIDATES)
            else:
                sql = (f"SELECT * FROM doc_chunks WHERE project_id=? AND ({kw_clause}) "
                       f"LIMIT ?")
                params = (project_id, *patterns, MAX_SEARCH_CANDIDATES)
            rows = db.rows(conn, sql, params)
        else:
            if all_projects:
                rows = db.rows(conn, "SELECT * FROM doc_chunks LIMIT ?",
                               (MAX_SEARCH_CANDIDATES,))
            else:
                rows = db.rows(conn,
                               "SELECT * FROM doc_chunks WHERE project_id=? "
                               "LIMIT ?",
                               (project_id, MAX_SEARCH_CANDIDATES))
    if q_emb is None:
        try:
            q_emb = embed_texts([query], prefix=QUERY_PREFIX)[0]
        except Exception:
            q_emb = None

    semantic: list[tuple[float, sqlite3.Row]] = []
    keyword: list[tuple[float, sqlite3.Row]] = []
    for r in rows:
        content = r["content"] or ""
        low = content.casefold()
        kw = 0.0
        if low_q in low:
            kw = 1.0
        elif words:
            hits = sum(1 for w in words if w in low)
            kw = min(hits, 4) / 4.0
        keyword.append((kw, r))
        sem = 0.0
        emb = r["embedding"]
        emb_hash = r["embedded_hash"] if "embedded_hash" in r.keys() else ""
        content_hash = r["content_hash"] if "content_hash" in r.keys() else ""
        if q_emb and emb and emb_hash and content_hash and emb_hash == content_hash:
            try:
                sem = cosine_similarity(q_emb, unpack_vector(emb))
            except Exception:
                sem = 0.0
        semantic.append((sem, r))

    scored: list[dict[str, Any]] = []
    for r in rows:
        content = r["content"] or ""
        sem = 0.0
        emb = r["embedding"]
        emb_hash = r["embedded_hash"] if "embedded_hash" in r.keys() else ""
        content_hash = r["content_hash"] if "content_hash" in r.keys() else ""
        if q_emb and emb and emb_hash and content_hash and emb_hash == content_hash:
            try:
                sem = cosine_similarity(q_emb, unpack_vector(emb))
            except Exception:
                sem = 0.0
        kw = _keyword_boost(low_q, words, content, "")
        score = sem + kw
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
                "_sem": sem,
                "_kw": kw,
            })
    scored.sort(key=lambda x: (x["score"], x["_sem"], x["_kw"]), reverse=True)
    for s in scored:
        s.pop("_sem", None)
        s.pop("_kw", None)
    out = scored[:top_k]
    _record_usage("search_docs", project_id, query, len(out))
    return out


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
    # truncate once. Both sources now emit comparable 0..1-ish scores.
    candidates: list[dict[str, Any]] = []
    if include_facts:
        candidates += search_facts(query, project_id, top_k=k * 3,
                                   include_global=include_global, q_emb=q_emb)
    if include_docs:
        candidates += search_docs(query, project_id, top_k=k * 3,
                                  all_projects=all_projects, q_emb=q_emb)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    out = candidates[:k]
    _record_usage("unified_search", project_id, query, len(out))
    return out


def stats(project_id: int | None = None) -> dict[str, Any]:
    now = registry.now_iso()
    with db.db() as conn:
        out: dict[str, Any] = {}
        if project_id is not None:
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
        out["facts_expired"] = db.one(
            conn, "SELECT COUNT(*) c FROM memory WHERE valid_to != '' AND valid_to < ?", (now,))["c"]
        out["facts_by_kind"] = {
            r["kind"]: r["c"]
            for r in db.rows(conn, "SELECT kind, COUNT(*) c FROM memory GROUP BY kind")
        }
        out["facts_hit_count_total"] = db.one(
            conn, "SELECT COALESCE(SUM(hit_count),0) c FROM memory")["c"]
        out["facts_top_hits"] = [
            dict(r)
            for r in db.rows(
                conn,
                "SELECT id, content, hit_count FROM memory ORDER BY hit_count DESC LIMIT 5"
            )
        ]
        out["facts_zero_hit"] = db.one(
            conn,
            "SELECT COUNT(*) c FROM memory WHERE COALESCE(hit_count,0)=0 "
            "AND COALESCE(kind,'note') IN ('rule','decision')",
        )["c"]
        oldest_unhit = db.one(
            conn,
            "SELECT id, content, ts FROM memory WHERE COALESCE(hit_count,0)=0 "
            "AND COALESCE(kind,'note') IN ('rule','decision') "
            "AND (valid_to='' OR valid_to > ?) "
            "ORDER BY ts ASC LIMIT 1",
            (now,),
        )
        out["oldest_unhit_rule"] = dict(oldest_unhit) if oldest_unhit else None
        d7 = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        d30 = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        out["facts_expiring_7d"] = db.one(
            conn,
            "SELECT COUNT(*) c FROM memory WHERE valid_to != '' AND valid_to BETWEEN ? AND ?",
            (now, d7),
        )["c"]
        out["facts_expiring_30d"] = db.one(
            conn,
            "SELECT COUNT(*) c FROM memory WHERE valid_to != '' AND valid_to BETWEEN ? AND ?",
            (now, d30),
        )["c"]
    # Health-check must not run inside the connection context (network call).
    out["ollama_ok"] = ollama_ok()
    try:
        if _last_reembed:
            out["last_reembed"] = dict(_last_reembed)
    except Exception:
        pass
    return out


def usage_stats(project_id: int | None = None, window: int = 1000) -> dict[str, Any]:
    """Return a snapshot of recent search/prefetch activity.

    Metrics are computed over the most recent ``window`` in-memory events.
    The window is global; passing ``project_id`` scopes the returned numbers
    to that project while still drawing from the same rolling buffer.
    """
    window = max(1, min(int(window), _MAX_USAGE_EVENTS))
    with _usage_lock:
        events = list(_usage_events)[-window:]
    if project_id is not None:
        events = [e for e in events if e.get("project_id") == project_id]
    total = len(events)
    zero = sum(1 for e in events if e.get("result_count", 0) == 0)
    by_kind: dict[str, dict[str, Any]] = {}
    for e in events:
        k = e.get("kind", "unknown")
        bucket = by_kind.setdefault(k, {"total": 0, "zero": 0})
        bucket["total"] += 1
        if e.get("result_count", 0) == 0:
            bucket["zero"] += 1
    avg = round(sum(e.get("result_count", 0) for e in events) / max(total, 1), 2)
    return {
        "window": window,
        "total_searches": total,
        "zero_result_searches": zero,
        "zero_result_rate": round(zero / max(total, 1), 4),
        "avg_results_per_search": avg,
        "by_kind": by_kind,
        "recent_zero_queries": [
            e["query"] for e in events[-50:] if e.get("result_count", 0) == 0
        ],
    }


def recent_facts(project_id: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
    with db.db() as conn:
        if project_id is not None:
            rows = db.rows(
                conn,
                "SELECT * FROM memory WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit))
        else:
            rows = db.rows(conn, "SELECT * FROM memory ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = {k: r[k] for k in ("id", "project_id", "scope", "topic", "content", "source", "ts")}
            keys = r.keys()
            if "kind" in keys:
                d["kind"] = r["kind"]
            if "hit_count" in keys:
                d["hit_count"] = r["hit_count"]
            if "last_used_at" in keys:
                d["last_used_at"] = r["last_used_at"]
            if "valid_to" in keys:
                d["valid_to"] = r["valid_to"]
            out.append(d)
        return out


def prune(project_id: int | None = None, keep: int = 200,
          min_age_days: float = 30.0, *, include_expired: bool = True) -> dict[str, int]:
    from datetime import timedelta
    keep = max(0, int(keep))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    now = registry.now_iso()
    with db.db() as conn:
        if project_id is None:
            where = "1"
            params: tuple = ()
        else:
            where = "project_id=?"
            params = (project_id,)
        # Scope the retention window to rows the outer DELETE could actually
        # delete (same filter, excluding global facts).
        retention_where = f"{where} AND scope!='global'"
        cur = conn.execute(
            f"DELETE FROM memory WHERE {where} AND scope!='global' AND ts < ? "
            f"AND id NOT IN (SELECT id FROM memory WHERE {retention_where} ORDER BY id DESC LIMIT ?)",
            (*params, cutoff, *params, keep))
        removed_age = cur.rowcount
        removed_expired = 0
        if include_expired:
            cur2 = conn.execute(
                f"DELETE FROM memory WHERE {where} AND valid_to != '' AND valid_to < ? "
                f"AND scope!='global'",
                (*params, now),
            )
            removed_expired = cur2.rowcount
    return {"removed_age": removed_age, "removed_expired": removed_expired,
            "removed_total": removed_age + removed_expired}


_last_reembed: dict[str, Any] = {}

# Lightweight, in-memory search/prefetch telemetry. Not persisted: the point is
# a live operational window, not historical analytics.
_MAX_USAGE_EVENTS = 2000
_usage_events: deque[dict[str, Any]] = deque(maxlen=_MAX_USAGE_EVENTS)
_usage_lock = threading.Lock()


def _record_usage(kind: str, project_id: int, query: str, result_count: int) -> None:
    """Append a tiny search/prefetch event to the rolling window."""
    with _usage_lock:
        _usage_events.append({
            "kind": kind,
            "project_id": project_id,
            "query": query[:200],
            "result_count": max(0, int(result_count)),
            "ts": registry.now_iso(),
        })


def prefetch_rules(prompt: str, project_id: int = 0, *, k: int = 3,
                    max_chars: int = 800) -> list[dict[str, Any]]:
    """Deterministic, Ollama-free prefetch of durable rules/decisions."""
    if k < 1 or not (prompt or "").strip():
        return []
    from . import context as _ctx

    terms = _ctx._topic_terms(prompt)
    if len(terms) < 1:
        return []
    log_verbs = re.compile(r"^(Released|Session checkpoint|Pushed|Applied|Fixed|Shipped|Implemented|Added|Synced|Updated|Committed|Deployed|Merged|Perun\b.*v\d|PerunChat\b.*v\d)", re.I)
    now = registry.now_iso()
    with db.db() as conn:
        try:
            rows = db.rows(
                conn,
                "SELECT id, project_id, scope, topic, source, content, meta, ts, room_id, kind, "
                "hit_count, last_used_at FROM memory "
                "WHERE (project_id=? OR (scope='global' AND project_id=0)) AND COALESCE(room_id,'')='' "
                "AND COALESCE(kind,'note') IN ('rule','decision') "
                "AND (valid_to='' OR valid_to > ?) "
                "ORDER BY id DESC LIMIT 400",
                (project_id, now),
            )
        except Exception:
            rows = db.rows(
                conn,
                "SELECT id, project_id, scope, topic, source, content, meta, ts, room_id FROM memory "
                "WHERE (project_id=? OR (scope='global' AND project_id=0)) AND COALESCE(room_id,'')='' "
                "ORDER BY id DESC LIMIT 400",
                (project_id,),
            )
    scored = []
    for r in rows:
        src = r["source"] or ""
        if src == "perunchat":
            continue
        content = r["content"] or ""
        if not content:
            continue
        if is_sensitive(content):
            continue
        if log_verbs.match(content.strip()):
            continue
        doc = _ctx._topic_terms(content + " " + (r["topic"] or ""))
        if not doc:
            continue
        inter = terms & doc
        if len(inter) < 2:
            continue
        if len(inter) / max(1, len(terms)) < (1.0 / 3.0):
            continue
        ov = len(inter) / min(len(terms), len(doc))
        scored.append((ov, len(inter), r))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]["id"]))
    out = []
    total = 0
    touched: list[int] = []
    for _ov, _ni, r in scored:
        c = r["content"] or ""
        if total + len(c) > max_chars and out:
            break
        out.append({
            "id": r["id"],
            "project_id": r["project_id"],
            "scope": r["scope"],
            "source": r["source"],
            "topic": r["topic"],
            "content": c,
            "ts": r["ts"],
            "kind": r["kind"] if "kind" in r.keys() else "rule",
        })
        touched.append(r["id"])
        total += len(c)
        if len(out) >= k:
            break
    for fid in touched:
        try:
            touch_fact(fid)
        except Exception:
            pass
    _record_usage("prefetch", project_id, prompt, len(out))
    return out


def reembed_missing(limit: int = 64) -> dict[str, Any]:
    """Embed rows that are missing a vector (or whose chunk vector is stale).

    Facts and doc chunks written while Ollama was down used to stay vector-less
    forever; chunks edited during an outage kept the old content's vector.
    Batch-embeds up to *limit* rows per call so one invocation stays bounded.
    """
    from datetime import datetime, timezone

    limit = max(1, int(limit))
    embedded_facts = 0
    embedded_chunks = 0
    error = None
    try:
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
            updates = []
            for r, vec in zip(chunk_rows, vecs):
                if vec is None:
                    continue
                updates.append((pack_vector(vec), r["content_hash"], r["id"]))
            if updates:
                with db.db() as conn:
                    conn.executemany(
                        "UPDATE doc_chunks SET embedding=?, embedded_hash=? WHERE id=?",
                        updates)
                embedded_chunks = len(updates)
    except Exception as exc:
        error = str(exc)
    result = {"ok": error is None, "facts_embedded": embedded_facts,
              "chunks_embedded": embedded_chunks}
    if error:
        result["error"] = error
    global _last_reembed
    _last_reembed = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "facts": embedded_facts,
        "chunks": embedded_chunks,
        "error": error,
    }
    return result


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
