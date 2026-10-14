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
    pack_vector,
    unpack_vector,
)

FRESHNESS_HALF_LIFE_DAYS = 30.0

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
    re.compile(r"password\s*[:=]\s*['\"]?[^\s\"']{8,}", re.I),
    re.compile(r"token\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}", re.I),
    re.compile(r"bearer\s+[A-Za-z0-9_\-]{20,}", re.I),
    re.compile(r"-----BEGIN .*?-----"),
]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:32]


def is_sensitive(text: str) -> bool:
    return any(p.search(text) for p in _SENSITIVE)


def _freshness_weight(ts: str) -> float:
    try:
        t = datetime.fromisoformat(ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        age_days = max((datetime.now(timezone.utc) - t).total_seconds() / 86400, 0)
        return math.pow(0.5, age_days / FRESHNESS_HALF_LIFE_DAYS)
    except Exception:
        return 1.0


def _keywords(query: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z0-9_]{3,}", query.lower()) if len(w) >= 3]


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
             topic: str = "general", source: str = "manual") -> dict[str, Any]:
    content = (content or "").strip()
    if not content:
        return {"ok": False, "reason": "empty"}
    if is_sensitive(content):
        return {"ok": False, "reason": "sensitive"}
    if scope is None:
        scope = "global" if not project_id else "project"
    pid = 0 if scope == "global" else project_id
    h = content_hash(content)
    ts = registry.now_iso()
    with db.db() as conn:
        existing = db.one(
            conn,
            "SELECT id FROM memory WHERE scope=? AND project_id=? AND content_hash=?",
            (scope, pid, h),
        )
        if existing:
            return {"ok": True, "duplicate": True, "id": existing["id"]}
        emb = _embed(content, QUERY_PREFIX)
        cur = conn.execute(
            """INSERT INTO memory
               (project_id,scope,topic,content,embedding,source,content_hash,ts)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pid, scope, topic, content, emb, source, h, ts),
        )
        rid = cur.lastrowid
    return {"ok": True, "id": rid, "embedded": emb is not None}


def _fact_rows(conn: sqlite3.Connection, project_id: int,
               include_global: bool) -> list[sqlite3.Row]:
    if include_global:
        return db.rows(
            conn,
            "SELECT * FROM memory WHERE project_id=? OR (scope='global' AND project_id=0)",
            (project_id,),
        )
    return db.rows(conn, "SELECT * FROM memory WHERE project_id=?", (project_id,))


def search_facts(query: str, project_id: int = 0, *, top_k: int = 6,
                 include_global: bool = True) -> list[dict[str, Any]]:
    q_emb = None
    try:
        q_emb = embed_texts([query], prefix=QUERY_PREFIX)[0]
    except Exception:
        q_emb = None
    words = _keywords(query)
    low_q = query.lower()
    with db.db() as conn:
        rows = _fact_rows(conn, project_id, include_global)
    scored: list[dict[str, Any]] = []
    for r in rows:
        content = r["content"] or ""
        score = 0.0
        if q_emb and r["embedding"]:
            score = cosine_similarity(q_emb, unpack_vector(r["embedding"]))
        low = ((r["topic"] or "") + " " + content).lower()
        if low_q in low:
            score += 0.3
        elif words and any(w in low for w in words):
            score += 0.15
        score *= _freshness_weight(r["ts"])
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
                "score": round(score, 4),
            })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# --------------------------------------------------------------------------- #
# Document chunks
# --------------------------------------------------------------------------- #
def chunk_markdown(text: str) -> list[tuple[str, str]]:
    """Split Markdown into ``(section, content)`` pairs at level-2 headings."""
    text = text.replace("\r\n", "\n")
    sections: list[tuple[str, str]] = []
    current_title = "(top)"
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append((current_title, body))

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            buf = []
            current_title = line[3:].strip() or "(untitled)"
        else:
            buf.append(line)
    flush()
    if not sections and text.strip():
        sections.append(("(document)", text.strip()))
    return sections


def index_text(project_id: int, source_file: str, text: str) -> int:
    """Index/refresh one document's chunks. Returns number of chunks upserted."""
    ts = registry.now_iso()
    sections = chunk_markdown(text)
    changed = 0
    with db.db() as conn:
        for section, content in sections:
            h = content_hash(content)
            row = db.one(
                conn,
                """SELECT id,content_hash FROM doc_chunks
                   WHERE project_id=? AND source_file=? AND section=?""",
                (project_id, source_file, section),
            )
            if row and row["content_hash"] == h:
                continue
            emb = _embed(content, DOC_PREFIX)
            conn.execute(
                """INSERT INTO doc_chunks
                   (project_id,source_file,section,content,embedding,content_hash,updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(project_id,source_file,section) DO UPDATE SET
                     content=excluded.content, embedding=excluded.embedding,
                     content_hash=excluded.content_hash, updated_at=excluded.updated_at""",
                (project_id, source_file, section, content, emb, h, ts),
            )
            changed += 1
    return changed


def index_project_docs(project_id: int) -> dict[str, Any]:
    """Index the standard doc set for a project (files that exist)."""
    project = registry.get_project(project_id)
    if not project:
        return {"ok": False, "reason": "no such project"}
    root = Path(project["path"])
    indexed: list[str] = []
    total = 0
    for rel in DOC_CANDIDATES:
        f = root / rel
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            total += index_text(project_id, rel, text)
            indexed.append(rel)
    registry.log_activity(project_id, "index",
                          f"Indexed {total} chunk(s) from {len(indexed)} doc(s)",
                          {"files": indexed})
    return {"ok": True, "files": indexed, "chunks": total}


def search_docs(query: str, project_id: int = 0, *, top_k: int = 6,
                all_projects: bool = False) -> list[dict[str, Any]]:
    q_emb = None
    try:
        q_emb = embed_texts([query], prefix=QUERY_PREFIX)[0]
    except Exception:
        q_emb = None
    words = _keywords(query)
    with db.db() as conn:
        if all_projects:
            rows = db.rows(conn, "SELECT * FROM doc_chunks")
        else:
            rows = db.rows(conn, "SELECT * FROM doc_chunks WHERE project_id=?", (project_id,))
    scored: list[dict[str, Any]] = []
    for r in rows:
        content = r["content"] or ""
        score = 0.0
        if q_emb and r["embedding"]:
            score = cosine_similarity(q_emb, unpack_vector(r["embedding"]))
        if words:
            low = content.lower()
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
                   all_projects: bool = False) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    if include_facts:
        merged += search_facts(query, project_id, top_k=k,
                               include_global=include_global)
    if include_docs:
        merged += search_docs(query, project_id, top_k=k, all_projects=all_projects)
    merged.sort(key=lambda x: x["score"], reverse=True)
    return merged[:k]


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
        out["ollama_ok"] = _embed("ping", QUERY_PREFIX) is not None
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
    cutoff = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).isoformat()
    with db.db() as conn:
        where = "project_id=?" if project_id else "1"
        params: tuple = (project_id,) if project_id else ()
        cur = conn.execute(
            f"DELETE FROM memory WHERE {where} AND scope!='global' AND ts < ? "
            f"AND id NOT IN (SELECT id FROM memory ORDER BY id DESC LIMIT ?)",
            (*params, cutoff, keep))
        return cur.rowcount


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
