"""Memory subsystem tests (Ollama-independent paths)."""

from __future__ import annotations

from pathlib import Path

import pytest

from perun import memory, registry


def test_remember_global_and_dedupe():
    r1 = memory.remember("The sky is blue", scope="global")
    assert r1["ok"] and not r1.get("duplicate")
    r2 = memory.remember("The sky is blue", scope="global")
    assert r2["ok"] and r2.get("duplicate")
    assert r1["id"] == r2["id"]


def test_remember_project_scope_and_visibility():
    p = registry.add_project("P", r"C:\p")
    memory.remember("project secret", project_id=p["id"], scope="project")
    memory.remember("shared truth", scope="global")

    visible = memory.recent_facts(p["id"])
    contents = {f["content"] for f in visible}
    # recent_facts(project) only returns project rows; global lives separately
    assert "project secret" in contents

    global_facts = memory.recent_facts(None)
    assert any(f["content"] == "shared truth" for f in global_facts)


def test_remember_rejects_sensitive():
    bad = memory.remember("api_key = 'abcdef0123456789abcdef'")
    assert bad["ok"] is False and bad["reason"] == "sensitive"
    assert memory.is_sensitive("-----BEGIN RSA PRIVATE KEY----- x")


def test_remember_rejects_empty():
    assert memory.remember("   ")["ok"] is False


def test_chunk_markdown_splits_on_h2():
    md = "# Title\n\nintro\n\n## One\n\naaa\n\n## Two\n\nbbb\n"
    chunks = memory.chunk_markdown(md)
    titles = [t for t, _ in chunks]
    assert "(top)" in titles and "One" in titles and "Two" in titles
    body = dict(chunks)
    assert body["One"].strip() == "aaa"


def test_chunk_markdown_fallback():
    chunks = memory.chunk_markdown("just text no headings")
    assert chunks and chunks[0][0] == "(top)"
    assert chunks[0][1] == "just text no headings"


def test_chunk_markdown_caps_section_size():
    big = "x" * (memory.MAX_CHUNK_CHARS + 100)
    md = f"# Title\n\n## Big\n\n{big}\n"
    chunks = memory.chunk_markdown(md)
    assert all(len(c) <= memory.MAX_CHUNK_CHARS for _, c in chunks)
    big_chunks = [(t, c) for t, c in chunks if t != "(top)"]
    assert len(big_chunks) >= 2
    assert all("Big" in t for t, _ in big_chunks)


def test_index_text_and_search_docs_keyword():
    p = registry.add_project("P", r"C:\p")
    text = "# Doc\n\n## Request gate\n\nThe request gate prevents 429 storms.\n"
    changed = memory.index_text(p["id"], "docs/CODEMAP.md", text)
    assert changed >= 1
    # Exact-substring query guarantees a keyword hit regardless of embeddings.
    results = memory.search_docs("request gate prevents 429 storms", p["id"])
    assert results, "expected a keyword match"
    assert results[0]["source_type"] == "doc"
    assert results[0]["project_id"] == p["id"]


def test_index_text_skips_unchanged():
    p = registry.add_project("P", r"C:\p")
    text = "## Stable\n\nunchanged body"
    assert memory.index_text(p["id"], "d.md", text) == 1
    assert memory.index_text(p["id"], "d.md", text) == 0


def test_index_project_docs(workdir):
    (workdir / "docs").mkdir(parents=True, exist_ok=True)
    (workdir / "docs" / "CODEMAP.md").write_text(
        "# Map\n\n## Section A\n\ncontent a\n\n## Section B\n\ncontent b\n",
        encoding="utf-8")
    (workdir / "README.md").write_text("# Readme\n\ntext\n", encoding="utf-8")
    p = registry.add_project("DocsProj", str(workdir))
    res = memory.index_project_docs(p["id"])
    assert res["ok"]
    assert "docs/CODEMAP.md" in res["files"]
    assert res["chunks"] >= 2


def test_unified_search_merges_scopes():
    p = registry.add_project("P", r"C:\p")
    memory.remember("alpha project detail about widgets", project_id=p["id"],
                    scope="project")
    memory.remember("alpha global detail about widgets", scope="global")
    results = memory.unified_search("alpha", p["id"], k=10)
    types = {r["source_type"] for r in results}
    assert "fact" in types
    assert len(results) >= 2


def test_stats():
    p = registry.add_project("P", r"C:\p")
    memory.remember("f1", project_id=p["id"], scope="project")
    memory.remember("f2", scope="global")
    s = memory.stats(p["id"])
    assert s["facts_project"] == 1
    assert s["facts_global"] == 1
    assert s["facts_total"] == 2


def test_prune_removes_old_project_facts():
    from datetime import datetime, timedelta, timezone
    from perun import db
    p = registry.add_project("P", r"C:\p")
    memory.remember("keep me fresh", project_id=p["id"], scope="project")
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    with db.db() as conn:
        conn.execute("UPDATE memory SET ts=? WHERE project_id=?", (old, p["id"]))
    removed = memory.prune(project_id=p["id"], keep=0, min_age_days=30)
    assert removed == 1


def test_search_facts_casefolds_german_eszett():
    """casefold() maps 'ß' to 'ss', unlike lower()."""
    p = registry.add_project("Casefold", r"C:\cf")
    memory.remember("Meissner rule about strasse", project_id=p["id"], scope="project")
    results = memory.search_facts("Meißner straße", p["id"])
    assert results


def test_index_project_docs_reports_skipped_sensitive(workdir):
    (workdir / "README.md").write_text(
        "# Readme\n\nghp_deadbeefdeadbeefdeadbeefdeadbeefdead\n",
        encoding="utf-8")
    p = registry.add_project("SkipSec", str(workdir))
    res = memory.index_project_docs(p["id"])
    assert res["ok"]
    assert res["skipped_sensitive"] >= 1


def test_reembed_missing_repairs_fact_embedding(monkeypatch):
    from perun import config, db, embeddings

    # Insert a fact with a missing embedding by bypassing remember().
    p = registry.add_project("Reembed", r"C:\re")
    content = "repair me"
    with db.db() as conn:
        cur = conn.execute(
            "INSERT INTO memory (project_id, scope, content, content_hash, ts, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (p["id"], "project", content, memory.content_hash(content),
             registry.now_iso(), None),
        )
        fact_id = cur.lastrowid

    fake_vec = [0.1] * config.Settings().embed_dim
    monkeypatch.setattr(
        memory, "embed_texts",
        lambda texts, prefix="": [fake_vec for _ in texts])

    result = memory.reembed_missing(limit=8)
    assert result["facts_embedded"] == 1
    assert result["chunks_embedded"] == 0

    with db.db() as conn:
        row = db.one(conn, "SELECT embedding FROM memory WHERE id=?", (fact_id,))
    assert row["embedding"] is not None
    unpacked = embeddings.unpack_vector(row["embedding"])
    assert unpacked == pytest.approx(fake_vec)
