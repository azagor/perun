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
    assert removed["removed_total"] == 1


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


def test_reembed_missing_repairs_chunk_embedding(monkeypatch):
    from perun import config, db, embeddings

    p = registry.add_project("ChunkRepair", r"C:\cr")
    content = "chunk repair me"
    h = memory.content_hash(content)
    with db.db() as conn:
        cur = conn.execute(
            "INSERT INTO doc_chunks (project_id, source_file, section, content, "
            "embedding, content_hash, embedded_hash, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (p["id"], "docs/README.md", "x", content, None, h, "",
             registry.now_iso()),
        )
        chunk_id = cur.lastrowid

    fake_vec = [0.1] * config.Settings().embed_dim
    monkeypatch.setattr(
        memory, "embed_texts",
        lambda texts, prefix="": [fake_vec for _ in texts])

    result = memory.reembed_missing(limit=8)
    assert result["chunks_embedded"] == 1

    with db.db() as conn:
        row = db.one(conn, "SELECT embedding, embedded_hash, content_hash FROM doc_chunks WHERE id=?", (chunk_id,))
    assert row["embedding"] is not None
    assert row["embedded_hash"] == row["content_hash"]
    unpacked = embeddings.unpack_vector(row["embedding"])
    assert unpacked == pytest.approx(fake_vec)


def test_reembed_missing_records_error_on_bind_failure(monkeypatch):
    """Simulate the original bug: raw list passed to SQLite raises ProgrammingError."""
    from perun import db

    p = registry.add_project("ReembedFail", r"C:\rf")
    content = "chunk to fail"
    h = memory.content_hash(content)
    with db.db() as conn:
        conn.execute(
            "INSERT INTO doc_chunks (project_id, source_file, section, content, "
            "embedding, content_hash, embedded_hash, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (p["id"], "docs/README.md", "x", content, None, h, "", registry.now_iso()),
        )

    fake_vec = [0.1] * 10
    monkeypatch.setattr(memory, "embed_texts", lambda texts, prefix="": [fake_vec for _ in texts])
    monkeypatch.setattr(memory, "pack_vector", lambda vec: vec)  # wrong type, should be bytes
    result = memory.reembed_missing(limit=2)
    assert result["ok"] is False
    assert "type 'list' is not supported" in result.get("error", "") or "Error binding" in result.get("error", "")


def test_prefetch_rules_selects_rules_only():
    p = registry.add_project("Prefetch", r"C:\pf")
    memory.remember("deploy rule one", project_id=p["id"], scope="project",
                    topic="deployment", source="user", kind="rule")
    memory.remember("work log entry", project_id=p["id"], scope="project",
                    topic="work", source="mcp", kind="log")
    # Query must share at least 2 content terms with the rule to pass the gate.
    rules = memory.prefetch_rules("deploy rule for this project", project_id=p["id"], k=3)
    assert len(rules) == 1
    assert rules[0]["content"] == "deploy rule one"


def test_prefetch_rules_rejects_release_announcements():
    p = registry.add_project("PrefetchRel", r"C:\pfr")
    memory.remember("Released Perun v9.9.9 with deploy fix", project_id=p["id"],
                    scope="project", topic="deployment", source="user", kind="rule")
    rules = memory.prefetch_rules("deploy fix", project_id=p["id"], k=3)
    assert rules == []


def test_chunk_markdown_headings_only_for_large_changelog():
    big = "x" * (memory.CHANGELOG_FULL_INDEX_MAX_CHARS + 1000)
    text = f"# Changelog\n\n## Release 1\n\n{big}\n\n## Release 2\n\nmore\n"
    prepared = memory._prepare_changelog("docs/CHANGELOG.md", text)
    assert memory.RELEASE_INDEX_SECTION in prepared
    assert "Release 1" in prepared
    assert "Release 2" in prepared
    # Big body should be gone from the prepared text.
    assert big not in prepared


def test_chunk_codemap_splits_at_modules():
    text = (
        "# Perun — CODEMAP\n\n"
        "## Package perun/\n\n"
        "### `perun/a.py`\n\nModule A does alpha.\n\n"
        "### `perun/b.py`\n\nModule B does beta.\n\n"
        "## Web dashboard `web/`\n\n"
        "* `web/index.html` — shell.\n"
    )
    chunks = memory.chunk_markdown(text, source_file="docs/CODEMAP.md")
    titles = [t for t, _ in chunks]
    bodies = "\n".join(b for _, b in chunks)
    assert any("perun/a.py" in t for t in titles)
    assert "perun/b.py" in bodies
    assert any("Web dashboard" in t for t in titles)
    for t, body in chunks:
        assert len(body) <= memory.CODEMAP_TARGET_CHUNK
        if "perun" in t:
            assert body.startswith("### ")


def test_chunk_codemap_caps_oversized_modules():
    big = "x" * 4000
    text = (
        "# Perun — CODEMAP\n\n"
        "## Package perun/\n\n"
        f"### `perun/big.py`\n\n{big}\n"
    )
    chunks = memory.chunk_markdown(text, source_file="docs/CODEMAP.md")
    assert len(chunks) >= 2
    assert all(len(body) <= memory.CODEMAP_TARGET_CHUNK for _, body in chunks)


def test_codemap_outline_is_compact():
    from perun import scaffold

    text = (
        "# CODEMAP\n\n"
        "## Package perun/\n\n"
        "### `perun/a.py`\n\nModule A summary.\n\n"
        "### `perun/b.py`\n\nModule B summary.\n"
    )
    outline = scaffold.generate_codemap_outline(text, "perun")
    assert "perun/a.py" in outline
    assert "Module A summary" in outline
    assert len(outline) <= scaffold._CODEMAP_OUTLINE_MAX_CHARS


def test_forget_removes_fact():
    r = memory.remember("forget me", scope="global")
    assert r["ok"] and r["id"]
    f = memory.forget(r["id"])
    assert f["removed"] is True
    assert memory.search_facts("forget me", 0, top_k=5) == []


def test_supersede_updates_content():
    r = memory.remember("old rule", scope="global", kind="rule")
    s = memory.supersede(r["id"], "new rule", kind="rule")
    assert s["ok"] and s["superseded"]
    hits = memory.search_facts("new rule", 0, top_k=5)
    assert any(h["content"] == "new rule" for h in hits)


def test_expired_fact_not_searchable():
    from datetime import datetime, timedelta, timezone
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r = memory.remember("expired rule", scope="global", kind="rule", valid_to=past)
    assert r["ok"]
    assert memory.search_facts("expired rule", 0, top_k=5) == []


def test_remember_valid_to_is_stored():
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r = memory.remember("future rule", scope="global", kind="rule", valid_to=future)
    assert r["ok"]
    assert memory.search_facts("future rule", 0, top_k=5)


def test_search_facts_records_hit_count():
    r = memory.remember("hit counter rule", scope="global", kind="rule")
    assert r["ok"]
    memory.search_facts("hit counter rule", 0, top_k=5)
    rows = memory.recent_facts(None, limit=1)
    row = next((x for x in rows if x["content"] == "hit counter rule"), None)
    assert row is not None
    assert row.get("hit_count", 0) >= 1


def test_prune_removes_expired():
    from datetime import datetime, timedelta, timezone
    from perun import db
    p = registry.add_project("PruneExp", r"C:\px")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    memory.remember("expired project rule", project_id=p["id"], scope="project", valid_to=past)
    memory.remember("fresh project rule", project_id=p["id"], scope="project")
    res = memory.prune(project_id=p["id"], keep=10, min_age_days=30)
    with db.db() as conn:
        after = db.one(conn, "SELECT COUNT(*) c FROM memory WHERE project_id=?", (p["id"],))["c"]
    # keep=10 protects the fresh rule; only the expired one is removed.
    assert after == 1
    assert res["removed_expired"] >= 1


def test_stopwords_do_not_pollute_results():
    """Common words like 'the' must not turn every fact into a candidate."""
    p = registry.add_project("Stopwords", r"C:\sw")
    memory.remember("database connection uses postgres port", project_id=p["id"],
                    scope="project", kind="rule")
    memory.remember("the quick brown fox jumps over the lazy dog", project_id=p["id"],
                    scope="project", kind="log")
    results = memory.search_facts("what is the postgres port", p["id"], top_k=5)
    assert results
    # The unrelated log fact should not outrank the postgres rule.
    assert results[0]["content"] == "database connection uses postgres port"


def test_old_global_rule_not_dropped_by_recency_cap():
    """Without ORDER BY id DESC, an old global rule can still rank above noise."""
    from datetime import datetime, timedelta, timezone
    from perun import db
    p = registry.add_project("OldRule", r"C:\or")
    old = (datetime.now(timezone.utc) - timedelta(days=365 * 2)).isoformat()
    memory.remember("ancient global rule about deployment", scope="global", kind="rule")
    with db.db() as conn:
        conn.execute("UPDATE memory SET ts=? WHERE content=?", (old, "ancient global rule about deployment"))
    # Create many newer project facts to overflow any implicit recency window.
    for i in range(250):
        memory.remember(f"newer project fact {i}", project_id=p["id"], scope="project", kind="log")
    results = memory.search_facts("deployment rule", p["id"], top_k=5, include_global=True)
    assert any(r["content"] == "ancient global rule about deployment" for r in results)


def test_search_room_facts_returns_room_scoped_rows():
    room = "priv:test"
    memory.remember(
        "alice: project update",
        project_id=-1, scope="room", topic="perunchat", source="perunchat",
        meta={"room": room, "sender": "alice", "type": "transcript"},
        room_id=room,
    )
    memory.remember(
        "bob: unrelated",
        project_id=-1, scope="room", topic="perunchat", source="perunchat",
        meta={"room": "priv:other", "sender": "bob", "type": "transcript"},
        room_id="priv:other",
    )
    results = memory.search_room_facts("project update", room, project_id=-1)
    assert len(results) == 1
    assert results[0]["content"] == "alice: project update"
    assert results[0]["meta"]["room"] == room


def test_search_room_facts_empty_query_or_room():
    assert memory.search_room_facts("", "priv:test", project_id=-1) == []
    assert memory.search_room_facts("hello", "", project_id=-1) == []


def test_search_room_facts_project_room_uses_project_id(tmp_path):
    project = registry.add_project("room-mem", str(tmp_path))
    registry.set_project_chat(project["id"], "grp:perun:room-mem:chat", "perun", "active", 1)
    room = "grp:perun:room-mem:chat"
    memory.remember(
        "alice: project update",
        project_id=project["id"], scope="project", topic="perunchat", source="perunchat",
        meta={"room": room, "sender": "alice", "type": "transcript"},
        room_id=room,
    )
    # Searching with the wrong project_id should find nothing.
    assert memory.search_room_facts("update", room, project_id=-1) == []
    results = memory.search_room_facts("update", room, project_id=project["id"])
    assert len(results) == 1
    assert results[0]["project_id"] == project["id"]


def test_search_room_facts_boosts_durable_facts_over_transcripts():
    room = "priv:test"
    memory.remember(
        "alice: project update",
        project_id=-1, scope="room", topic="perunchat", source="perunchat",
        meta={"room": room, "sender": "alice", "type": "transcript"},
        room_id=room,
    )
    memory.remember(
        "Decision: use FastAPI",
        project_id=-1, scope="room", topic="perunchat", source="perunchat",
        meta={"room": room, "sender": "alice", "type": "decision"},
        room_id=room,
    )
    results = memory.search_room_facts("FastAPI", room, project_id=-1)
    assert results
    assert results[0]["content"] == "Decision: use FastAPI"
    assert results[0]["meta"]["type"] == "decision"


def test_search_room_facts_falls_back_to_meta_room():
    """Rows with room only in meta (pre-v7 style) are still searchable."""
    room = "priv:legacy"
    from perun import db
    with db.db() as conn:
        conn.execute(
            "INSERT INTO memory (project_id, scope, topic, content, content_hash, ts, meta, room_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (-1, "room", "perunchat", "alice: legacy fact",
             memory.content_hash("alice: legacy fact"), registry.now_iso(),
             '{"room": "priv:legacy", "sender": "alice", "type": "transcript"}', "")
        )
    results = memory.search_room_facts("legacy fact", room, project_id=-1)
    assert len(results) == 1
    assert results[0]["content"] == "alice: legacy fact"
    assert results[0]["meta"]["room"] == room


def test_usage_stats_tracks_searches():
    # Clear in-memory window for a deterministic test.
    memory._usage_events.clear()
    memory.search_facts("unique query for usage stats", 0, top_k=5)
    usage = memory.usage_stats(0)
    assert usage["total_searches"] >= 1
    assert "search_facts" in usage["by_kind"]


def test_stats_reports_zero_hit_rules():
    memory.remember("zero hit rule for stats", scope="global", kind="rule")
    stats = memory.stats()
    assert "facts_zero_hit" in stats
    assert stats["facts_zero_hit"] >= 1
    assert "oldest_unhit_rule" in stats


def test_stats_reports_expiring_facts():
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    memory.remember("expiring soon", scope="global", kind="rule", valid_to=future)
    stats = memory.stats()
    assert stats["facts_expiring_7d"] >= 1
    assert stats["facts_expiring_30d"] >= 1
