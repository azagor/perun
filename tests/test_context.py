"""Perun Context tests (Ollama-independent paths).

The Context layer is the store behind ``perun_context`` and the orchestrator's
task-driven pre-fetch. These tests cover the behaviour the awareness-parity
contract depends on: nothing is deleted, the inventory is visible, the
deterministic paths never need an embedding, and nothing sensitive is ever
embedded.
"""

from __future__ import annotations

import pytest

from perun import context, registry


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
def test_normalize_key_is_case_and_space_insensitive():
    assert context.normalize_key("Handbook Tools") == "handbook-tools"
    assert context.normalize_key("  handbook.tools ") == "handbook.tools"
    assert context.normalize_key("SSH/Prod_Key") == "ssh/prod_key"
    assert context.normalize_key("") == ""


def test_normalize_key_keeps_path_separators():
    """A key may address a nested namespace, so ``/`` survives normalisation."""
    assert context.normalize_key("trading_bot/build") == "trading_bot/build"
    assert context.normalize_key("--edge--") == "edge"
    assert context.normalize_key("A B/C") == "a-b/c"


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def test_index_doc_stores_and_reads_back():
    res = context.index_doc("demo.key", "Demo", "the body of the demo doc")
    assert res["ok"] and res["indexed"] == 1
    row = context.get("demo.key")
    assert row is not None
    assert row["title"] == "Demo"
    assert row["body"] == "the body of the demo doc"
    assert row["scope"] == context.GLOBAL_SCOPE


def test_index_doc_is_idempotent():
    context.index_doc("demo.key", "Demo", "body")
    again = context.index_doc("demo.key", "Demo", "body")
    assert again["indexed"] == 0 and again["unchanged"] == 1


def test_index_doc_rejects_empty_key_and_body():
    assert context.index_doc("", "T", "body")["ok"] is False
    assert context.index_doc("k", "T", "   ")["ok"] is False


def test_index_doc_rejects_oversized_body():
    huge = "x" * (context.MAX_BODY_CHARS + 1)
    res = context.index_doc("big", "Big", huge)
    assert res["ok"] is False
    assert res["indexed"] == 0
    assert any("too_long" in s["reason"] for s in res["skipped"])


def test_project_scope_requires_a_project():
    res = context.index_doc("k", "T", "body", scope=context.PROJECT_SCOPE)
    assert res["ok"] is False and res["indexed"] == 0
    assert any("project_id" in s["reason"] for s in res["skipped"])


def test_project_scope_wins_over_global():
    context.index_doc("shared", "Global", "global body")
    proj = registry.add_project("P", r"C:\p")
    context.index_doc("shared", "Project", "project body",
                      scope=context.PROJECT_SCOPE, project_id=proj["id"])
    assert context.get("shared", project_id=proj["id"])["title"] == "Project"
    assert context.get("shared", project_id=0)["title"] == "Global"


# --------------------------------------------------------------------------- #
# The sensitive-data guard
# --------------------------------------------------------------------------- #
def test_sensitive_body_is_stored_but_never_embedded(monkeypatch):
    """A credential-looking doc must survive verbatim but never be embedded."""
    secretish = "api_key = 'abcdef0123456789abcdef' and more prose"
    called = []
    monkeypatch.setattr(context, "embed_texts",
                        lambda texts, prefix="": called.append(list(texts)) or
                        [[0.0] for _ in texts])
    res = context.index_doc("cred", "Cred", secretish)
    assert res["ok"] and res["indexed"] == 1
    # The body was never handed to the embedding endpoint.
    assert called == []
    row = context.get("cred")
    assert row["body"] == secretish
    assert row["embedding"] is None


def test_sensitive_doc_is_not_picked_up_by_the_repair_path():
    context.index_doc("cred2", "Cred",
                      "api_key = 'abcdef0123456789abcdef' and more prose")
    res = context.reembed_missing(limit=8)
    assert res["ok"] and res["embedded"] == 0


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
def test_list_keys_enumerates_the_inventory():
    context.index_doc("a.one", "One", "body one")
    context.index_doc("b.two", "Two", "body two")
    keys = {k["key"] for k in context.list_keys()}
    assert {"a.one", "b.two"} <= keys


def test_render_inventory_is_a_table():
    context.index_doc("a.one", "One", "body one", summary="the first one")
    text = context.render_inventory(context.list_keys())
    assert "`a.one`" in text and "the first one" in text


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
def test_search_resolves_an_exact_key_without_an_embedding(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("the exact-key path must not need Ollama")
    monkeypatch.setattr(context, "embed_texts", _boom)
    context.index_doc("handbook.demo", "Demo", "body")
    hits = context.search("handbook.demo")
    assert len(hits) == 1
    assert hits[0]["matched_by"] == "exact-key"
    assert hits[0]["body"] == "body"


def test_search_exact_mode_does_not_fall_through(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("exact mode must not embed")
    monkeypatch.setattr(context, "embed_texts", _boom)
    context.index_doc("handbook.demo", "Demo", "body")
    assert context.search("something else entirely", mode="exact") == []


def test_search_matches_a_key_fragment():
    context.index_doc("trading_bot.build", "Build", "body")
    hits = context.search("build")
    assert hits and hits[0]["key"] == "trading_bot.build"
    assert hits[0]["matched_by"] == "key-match"


def test_search_list_mode_returns_the_inventory():
    context.index_doc("a.one", "One", "body")
    hits = context.search("", mode="list")
    assert {h["key"] for h in hits} >= {"a.one"}


def test_search_resolves_an_anchor_label(tmp_path):
    """An agent may ask for a document by its human label, not its key."""
    proj_dir = tmp_path / "anchored"
    proj_dir.mkdir()
    (proj_dir / "perun.md").write_text(
        "---\n"
        "name: Anchored\n"
        "slug: anchored\n"
        "anchors:\n"
        "- label: The Doc\n"
        "  kind: doc\n"
        "  path: x.md\n"
        "  memory: proj.secret-doc\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )
    proj = registry.add_project("Anchored", str(proj_dir))
    context.index_doc("proj.secret-doc", "The Doc", "body",
                      scope=context.PROJECT_SCOPE, project_id=proj["id"])
    hits = context.search("The Doc", project_id=proj["id"])
    assert hits and hits[0]["key"] == "proj.secret-doc"
    assert hits[0]["matched_by"] == "anchor"


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def test_outline_lists_level_two_headings():
    body = "# Title\n\nintro\n\n## One\n\naaa\n\n## Two\n\nbbb\n"
    assert context.outline(body) == ["One", "Two"]


def test_slice_section_returns_one_section():
    body = "intro\n\n## One\n\naaa\n\n## Two\n\nbbb\n"
    assert context.slice_section(body, "One").strip() == "## One\n\naaa"
    assert context.slice_section(body, "Two").strip() == "## Two\n\nbbb"


def test_slice_section_falls_back_to_the_whole_body():
    body = "intro\n\n## One\n\naaa\n"
    assert context.slice_section(body, "Missing") == body


def test_search_can_address_a_section():
    context.index_doc("doc.parts", "Parts", "intro\n\n## One\n\naaa\n\n## Two\n\nbbb\n")
    hits = context.search("doc.parts#Two")
    assert hits and "bbb" in hits[0]["body"]
    assert "aaa" not in hits[0]["body"]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def test_render_hit_truncates_a_large_body_and_lists_its_sections():
    body = "## One\n" + ("x" * 20_000) + "\n\n## Two\n"
    context.index_doc("big.doc", "Big", body)
    hit = context.get("big.doc")
    text = context.render_hit(hit)
    assert "truncated" in text
    assert "One" in text and "Two" in text
    assert "perun_context(" in text


def test_render_hits_reports_an_empty_result():
    assert "mode=\"list\"" in context.render_hits([])


# --------------------------------------------------------------------------- #
# Pre-fetch
# --------------------------------------------------------------------------- #
def test_prefetch_injects_nothing_for_an_unrelated_request():
    context.index_doc("handbook.search", "Code search",
                      "grep and ast-grep", summary="grep / ast-grep routing")
    assert context.prefetch("asdkjhasd kjhasdkjh") == []


def test_prefetch_matches_the_subject_of_the_document():
    context.index_doc("handbook.search", "Code search: which tool, when",
                      "grep and ast-grep", summary="grep / ast-grep / unified search, "
                      "RRF ranking, fallbacks")
    hits = context.prefetch("which perun tool do I use for cross project code search")
    assert hits and hits[0]["key"] == "handbook.search"
    assert hits[0]["matched_by"] == "topical"


def test_prefetch_honours_an_explicit_key():
    context.index_doc("handbook.explicit", "Explicit", "body")
    hits = context.prefetch("handbook.explicit")
    assert hits and hits[0]["matched_by"] == "exact-key"


def test_prefetch_never_needs_an_embedding(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("pre-fetch must not need Ollama")
    monkeypatch.setattr(context, "embed_texts", _boom)
    context.index_doc("handbook.search", "Code search: which tool, when",
                      "grep and ast-grep", summary="grep / ast-grep / unified search, "
                      "RRF ranking, fallbacks")
    hits = context.prefetch("which perun tool do I use for cross project code search")
    assert hits and hits[0]["key"] == "handbook.search"


def test_prefetch_respects_the_size_budget():
    context.index_doc("a.big", "Big", "x" * (context.PREFETCH_MAX_CHARS + 500),
                      summary="cross project code search routing")
    hits = context.prefetch("cross project code search routing")
    assert hits
    assert hits[0]["key"] == "a.big"
    assert "truncated" in hits[0]["body"]
    assert len(hits[0]["body"]) <= context.PREFETCH_MAX_CHARS


def test_prefetch_truncates_an_oversized_exact_key_instead_of_skipping():
    big_body = "x" * (context.PREFETCH_MAX_CHARS + 500)
    context.index_doc("handbook.big", "Big", big_body)
    hits = context.prefetch("handbook.big")
    assert len(hits) == 1
    assert hits[0]["matched_by"] == "exact-key"
    assert "truncated" in hits[0]["body"]


def test_anchor_lookup_matches_a_label_inside_a_longer_topic(tmp_path):
    proj_dir = tmp_path / "anchored"
    proj_dir.mkdir()
    (proj_dir / "docs").mkdir()
    (proj_dir / "perun.md").write_text(
        "---\n"
        "name: Anchored\n"
        "slug: anchored\n"
        "anchors:\n"
        "- label: Technical documentation\n"
        "  kind: doc\n"
        "  path: docs/TECHNICAL.md\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )
    (proj_dir / "docs" / "TECHNICAL.md").write_text(
        "# Tech\n\n## Build\n\nrun make\n", encoding="utf-8")
    proj = registry.add_project("Anchored", str(proj_dir))
    hits = context.search("read the technical documentation", project_id=proj["id"])
    assert hits
    assert hits[0]["source_file"] == "docs/TECHNICAL.md"
    assert hits[0]["matched_by"] == "anchor"


def test_anchor_lookup_returns_a_stub_for_an_unindexed_file_anchor(tmp_path):
    proj_dir = tmp_path / "anchored"
    proj_dir.mkdir()
    (proj_dir / "perun.md").write_text(
        "---\n"
        "name: Anchored\n"
        "slug: anchored\n"
        "anchors:\n"
        "- label: Makefile\n"
        "  kind: file\n"
        "  path: Makefile\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )
    (proj_dir / "Makefile").write_text("build:\n\techo ok\n", encoding="utf-8")
    proj = registry.add_project("Anchored", str(proj_dir))
    hits = context.search("Makefile", project_id=proj["id"])
    assert hits
    assert hits[0]["source_file"] == "Makefile"
    assert hits[0]["matched_by"] == "anchor"
    assert "perun_read_file" in hits[0]["body"]


def test_stemmer_collapses_inflections():
    assert context._stem("releasing") == context._stem("released") == context._stem("release")
    assert context._stem("deploying") == context._stem("deployment") == context._stem("deploy")
    assert context._stem("configuration") == context._stem("configure")

def test_topical_overlap_ignores_body_only_matches():
    """A word that occurs in the body but not in the title is not a subject match."""
    hit = {"key": "a.doc", "title": "Title", "summary": "summary",
           "body": "a long body mentioning android and gradle"}
    assert context.topical_overlap("android gradle build", hit) == 0.0


# --------------------------------------------------------------------------- #
# The handbook
# --------------------------------------------------------------------------- #
def test_handbook_manifest_keys_are_unique():
    from perun import handbook

    keys = [e["key"] for e in handbook.ENTRIES]
    assert len(keys) == len(set(keys))


def test_handbook_key_table_lists_every_entry():
    from perun import handbook

    table = handbook.render_key_table()
    for entry in handbook.ENTRIES:
        assert f"`{entry['key']}`" in table


def test_render_kernel_fills_the_placeholder():
    from perun import handbook

    out = handbook.render_kernel("before\n{{KEY_TABLE}}\nafter")
    assert "{{KEY_TABLE}}" not in out
    assert "handbook.tools" in out
    assert out.startswith("before") and out.endswith("after")


def test_ensure_indexed_is_idempotent():
    from perun import handbook

    first = handbook.ensure_indexed()
    assert first["ok"]
    second = handbook.ensure_indexed()
    assert second["ok"]
    assert second["indexed"] == 0


def test_handbook_keeps_the_original_global_guide():
    """P1: the previous global guide must still exist, verbatim."""
    from perun import handbook

    entry = next(e for e in handbook.ENTRIES if e["key"] == "handbook.global")
    path = handbook.HANDBOOK_DIR / entry["file"]
    assert path.is_file()
    body = path.read_text(encoding="utf-8")
    # The complete original tool table — nothing was deleted, only relocated.
    assert "perun_list_projects" in body
    assert "perun_unified_search" in body
    assert "MCP tools" in body


# --------------------------------------------------------------------------- #
# Embedding fallback paths
# --------------------------------------------------------------------------- #
def test_prefetch_embedding_fallback_matches_when_deterministic_misses(monkeypatch):
    """When Ollama is up and deterministic rules miss, semantic fallback fires."""
    def fake_ok():
        return True

    vec = [0.5] * 10
    monkeypatch.setattr(context, "ollama_ok", fake_ok)
    monkeypatch.setattr(context, "embed_texts", lambda texts, prefix="": [vec for _ in texts])
    context.index_doc("handbook.widgets", "Widgets", "how to use the frobnitz",
                      summary="summary")
    hits = context.prefetch("frobnitz")
    assert hits and hits[0]["key"] == "handbook.widgets"
    assert hits[0]["matched_by"] == "semantic"


def test_semantic_ignores_stale_embeddings(monkeypatch):
    """A vector for an old body must not rank after the body is updated."""
    from perun import db

    def fake_ok():
        return True

    alpha_vec = [0.9] + [0.0] * 9
    beta_vec = [0.0] * 9 + [0.9]

    def fake_embed(texts, prefix=""):
        return [alpha_vec if "alpha" in t.lower() else beta_vec for t in texts]

    monkeypatch.setattr(context, "ollama_ok", fake_ok)
    monkeypatch.setattr(context, "embed_texts", fake_embed)
    context.index_doc("stale.doc", "Stale", "alpha alpha alpha")

    new_body = "beta beta beta"
    new_hash = context.memory.content_hash(new_body)
    with db.db() as conn:
        conn.execute(
            "UPDATE context_docs SET body=?, content_hash=? WHERE key=?",
            (new_body, new_hash, "stale.doc"),
        )

    hits = context._semantic("alpha", context._scope_rows("any", 0), 3)
    assert not any(h["key"] == "stale.doc" for h in hits)


def test_anchor_lookup_embedding_fallback_for_unrelated_tokens(tmp_path, monkeypatch):
    """Local embeddings can bridge labels that share no stemmed tokens."""
    proj_dir = tmp_path / "anchored"
    proj_dir.mkdir()
    (proj_dir / "perun.md").write_text(
        "---\n"
        "name: Anchored\n"
        "slug: anchored\n"
        "anchors:\n"
        "- label: Runbook\n"
        "  kind: doc\n"
        "  path: docs/RUNBOOK.md\n"
        "---\n\nBody\n",
        encoding="utf-8",
    )
    (proj_dir / "docs").mkdir()
    (proj_dir / "docs" / "RUNBOOK.md").write_text(
        "# Runbook\n\n## Steps\n\n1. run\n", encoding="utf-8")
    proj = registry.add_project("Anchored", str(proj_dir))

    def fake_ok():
        return True

    same_vec = [0.5] * 10
    monkeypatch.setattr(context, "ollama_ok", fake_ok)
    monkeypatch.setattr(context, "embed_texts",
                        lambda texts, prefix="": [same_vec for _ in texts])

    hits = context.search("how do I deploy this", project_id=proj["id"])
    assert hits
    assert hits[0]["source_file"] == "docs/RUNBOOK.md"
    assert hits[0]["matched_by"] == "anchor"
