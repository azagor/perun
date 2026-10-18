"""Aggressive/adversarial tests for perun.memory + perun.embeddings.

Purpose: probe the memory store for real defects (secret-filter gaps, search
ranking/robustness, storage limits, SQLite concurrency, Ollama-down fallback).

Conventions:
* Every test runs with the Ollama HTTP transport patched to raise
  ``ConnectionError`` (autouse fixture ``ollama_down``) so behaviour is
  deterministic and no network is used. Tests that need vectors patch
  ``perun.memory.embed_texts`` explicitly (see ``use_vectors``).
* Tests are written against the *correct* expected behaviour. A failing test is
  a finding, never something to bend the assertion around.
"""

from __future__ import annotations

import math
import threading
from datetime import datetime, timedelta, timezone

import pytest

from perun import db, embeddings, memory, registry

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _conn_refused(*args, **kwargs):
    raise ConnectionError("mock: Ollama unreachable")


@pytest.fixture(autouse=True)
def ollama_down(monkeypatch):
    """Simulate a dead Ollama server at the HTTP layer."""
    monkeypatch.setattr(embeddings.urllib.request, "urlopen", _conn_refused)
    embeddings._OLLAMA_HEALTH_CACHE.update(ts=0.0, ok=False)
    yield
    embeddings._OLLAMA_HEALTH_CACHE.update(ts=0.0, ok=False)


class _FakeResponse:
    """Minimal stand-in for ``urllib`` response objects."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _DictEmbed:
    """Deterministic stand-in for ``embed_texts`` (dimension 8)."""

    def __init__(self, dims: int = 8):
        self.dims = dims

    def __call__(self, texts, prefix: str = "", *args, **kwargs):
        out = []
        for t in texts:
            # Any deterministic vector; only dimensions/lengths matter here.
            out.append([float((len(t) + i) % 7 + 1) for i in range(self.dims)])
        return out


def use_vectors(monkeypatch, dims: int = 8):
    monkeypatch.setattr(memory, "embed_texts", _DictEmbed(dims))


def _raw_insert(content: str, *, ts: str, scope: str = "global", project_id: int = 0,
                topic: str = "general", embedding: bytes | None = None) -> int:
    h = memory.content_hash(content)
    with db.db() as conn:
        cur = conn.execute(
            "INSERT INTO memory (project_id,scope,topic,content,embedding,source,"
            "content_hash,ts) VALUES (?,?,?,?,?,?,?,?)",
            (project_id, scope, topic, content, embedding, "test", h, ts),
        )
        return cur.lastrowid


def _old_ts(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _run_threads(fn, n: int = 8) -> list[str]:
    errors: list[str] = []

    def wrap(i: int) -> None:
        try:
            fn(i)
        except Exception as exc:  # noqa: BLE001 - want the raw failure text
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def _project(name: str = "AdvP") -> dict:
    return registry.add_project(name, f"/tmp/adv-{name}")


# --------------------------------------------------------------------------- #
# (a) Secret filter — one test per credential shape
# --------------------------------------------------------------------------- #

# Construct the literal token strings at runtime so GitHub's push-protection
# secret scanner does not see high-entropy fake credentials in the source.


def _repeat(prefix: str, count: int) -> str:
    return prefix + "0" * count


def test_secret_aws_access_key_rejected():
    """AWS AKIA... access key ids (and aws_secret_access_key) must be rejected.

    Correct behaviour: a bare AWS access-key id is the canonical leaked
    credential; the filter currently only knows ``api_key=``-style prefixes, so
    this should be refused with reason 'sensitive'.
    """
    r = memory.remember(_repeat("AKIA", 16))
    assert r["ok"] is False and r.get("reason") == "sensitive", r
    r2 = memory.remember(
        "aws_secret_access_key = " + "0" * 40)
    assert r2["ok"] is False and r2.get("reason") == "sensitive", r2


def test_secret_github_token_rejected():
    """GitHub ghp_ / github_pat_ tokens must be rejected (bare, no key= prefix)."""
    r = memory.remember(_repeat("ghp_", 36))
    assert r["ok"] is False and r.get("reason") == "sensitive", r
    r2 = memory.remember(
        "github_pat_" + "0" * 27)
    assert r2["ok"] is False and r2.get("reason") == "sensitive", r2


def test_secret_slack_token_rejected():
    """Slack bot tokens (xoxb-) must be rejected."""
    r = memory.remember("xoxb-" + "0" * 12 + "-" + "0" * 13 + "-" + "0" * 24)
    assert r["ok"] is False and r.get("reason") == "sensitive", r


def test_secret_stripe_live_key_rejected():
    """Stripe live secret keys (sk_live_) must be rejected."""
    r = memory.remember(_repeat("sk_live_", 24))
    assert r["ok"] is False and r.get("reason") == "sensitive", r


def test_secret_jwt_rejected():
    """A bare JWT (three dot-separated base64 segments) is a credential."""
    segment = "eyJ0ZXN0IjoidGVzdCJ9"
    jwt = f"{segment}.{segment}.notrealjwtvalue12345678"
    r = memory.remember(jwt)
    assert r["ok"] is False and r.get("reason") == "sensitive", r


def test_secret_url_with_password_rejected():
    """A connection URL carrying a password (postgres://user:pass@host) leaks it."""
    r = memory.remember("postgres://admin:" + "0" * 8 + "@localhost:5432/demo")
    assert r["ok"] is False and r.get("reason") == "sensitive", r


def test_secret_private_key_with_crlf_rejected():
    """Private key blocks must be caught even with Windows CRLF line endings."""
    pem = ("-----BEGIN RSA PRIVATE KEY-----\r\n"
           "MIIEowIBAAKCAQEA1234567890abcdef\r\n"
           "-----END RSA PRIVATE KEY-----")
    r = memory.remember(pem)
    assert r["ok"] is False and r.get("reason") == "sensitive", r


def test_secret_polish_password_word_rejected():
    """Polish phrasing ('hasło: ...') must be treated like 'password: ...'."""
    r = memory.remember("hasło: Sup3rTajneHasloDoBazy")
    assert r["ok"] is False and r.get("reason") == "sensitive", r


def test_secret_json_token_rejected():
    """JSON payloads ({"token": "..."}) place a quote before the colon.

    Correct behaviour: the value is a 30+ char token, so this must be refused.
    """
    r = memory.remember('{"token": "' + _repeat("ghp_", 36) + '"}')
    assert r["ok"] is False and r.get("reason") == "sensitive", r


def test_secret_common_forms_still_rejected():
    """Sanity check: the shapes the filter *does* know must keep being refused."""
    assert memory.is_sensitive("api_key = 'abcdef0123456789abcdef'")
    assert memory.is_sensitive("Authorization: Bearer abcdefghijklmnopqrstuvwx")
    assert memory.is_sensitive("password: hunter2hunter2")
    assert memory.is_sensitive("-----BEGIN OPENSSH PRIVATE KEY----- x")
    assert memory.remember("token = 'abcdefghijklmnopqrstuvwx1234'")["ok"] is False


def test_secret_filter_applies_to_doc_chunks():
    """Secrets must never be persisted — including the doc-chunk indexing path.

    ``index_text`` bypasses ``is_sensitive`` entirely, so an AWS key embedded in
    a Markdown file gets stored verbatim and is returned by ``search_docs``.
    """
    p = _project("SecDoc")
    aws_key = _repeat("AKIA", 16)
    doc = f"# Keys\n\n## Prod\n\n{aws_key} and password: hunter2hunter2\n"
    memory.index_text(p["id"], "docs/SECRETS.md", doc)
    assert memory.stats(p["id"])["chunks"] == 0, "secret-bearing doc was indexed"
    assert memory.search_docs(aws_key, p["id"]) == []


# --------------------------------------------------------------------------- #
# (b) Search
# --------------------------------------------------------------------------- #


def test_search_on_empty_database():
    assert memory.search_facts("anything at all") == []
    assert memory.search_docs("anything at all", 0) == []
    assert memory.unified_search("anything at all", 0) == []


def test_search_empty_query_returns_nothing():
    """An empty/whitespace query must not dump the whole store.

    Correct behaviour: no query -> no results (and certainly no full-table
    dump across every project). Currently every stored fact scores 0.3 from the
    ``low_q in low`` branch because ``""`` is a substring of everything.
    """
    memory.remember("unrelated fact about widgets")
    memory.remember("another unrelated fact about sprockets", scope="global")
    assert memory.search_facts("") == []
    assert memory.search_facts("   ") == []


def test_search_100kb_word_query_does_not_crash():
    """A 100 kB query of ordinary words must degrade gracefully, not raise."""
    memory.remember("target widget fact for the long query")
    query = ("alpha bravo charlie delta echo foxtrot golf hotel " * 1800)[:100_000]
    memory.search_facts(query)  # must not raise


def test_search_single_100kb_token_does_not_crash():
    """A 100 kB single token becomes a >50 kB SQL LIKE pattern.

    Correct behaviour: long tokens must be truncated/ignored, not turned into a
    LIKE pattern that SQLite rejects ("LIKE or GLOB pattern too complex").
    """
    memory.remember("target widget fact")
    memory.search_facts("x" * 100_000)  # must not raise


def test_search_polish_only_query_finds_docs():
    """Non-ASCII queries must still match document chunks.

    ``_keywords`` only keeps [A-Za-z0-9_], so a Polish-only query yields zero
    keywords -> zero score -> zero results, even for an exact phrase match.
    """
    p = _project("PlDoc")
    memory.index_text(p["id"], "docs/pl.md", "## Sekcja\n\nZażółć gęślą jaźń.\n")
    assert memory.search_docs("zażółć gęślą jaźń", p["id"]), "exact phrase not found"


def test_search_polish_single_word_fact_found():
    """Sanity: a one-word Polish query already reaches the Python rescoring."""
    memory.remember("Kawa jest gorąca, żółć gęślą jaźń")
    assert memory.search_facts("żółć")


def test_search_emoji_query_and_content():
    """Emoji must be searchable and must not crash the scorer."""
    memory.remember("Deploy 🚀 release marker for rocket service")
    assert memory.search_facts("🚀")
    assert memory.search_facts("🚀🚀🚀 nie ma takiego") == []


def test_cosine_similarity_zero_vector():
    """The zero vector has no direction: similarity must be 0.0, not NaN/ZeroDivision."""
    assert embeddings.cosine_similarity([0.0] * 8, [1.0] * 8) == 0.0
    assert embeddings.cosine_similarity([0.0] * 8, [0.0] * 8) == 0.0
    assert embeddings.cosine_similarity([], [1.0] * 8) == 0.0


def test_search_zero_vector_embedding_scores_are_finite(monkeypatch):
    """Stored/query zero vectors must not poison scores with NaN."""
    use_vectors(monkeypatch)
    _raw_insert("zero vector fact about ontology",
                ts=registry.now_iso(), embedding=embeddings.pack_vector([0.0] * 8))
    res = memory.search_facts("zero vector fact about ontology")
    assert res, "keyword hit expected"
    for hit in res:
        assert math.isfinite(hit["score"]), hit


def test_cosine_similarity_large_values_finite():
    """Large but finite vectors must not overflow float32 into NaN.

    Correct behaviour: cosine similarity is scale-invariant, so
    cos([1e20]*768, [1e20]*768) == 1.0 (currently inf/inf -> nan).
    """
    a = [1e20] * 768
    b = [1e20] * 768
    sim = embeddings.cosine_similarity(a, b)
    assert not math.isnan(sim), "cosine overflowed to NaN"
    assert sim == pytest.approx(1.0, abs=1e-3)


def test_search_fact_without_valid_ts_is_findable():
    """Rows with an empty/garbage timestamp must still be searchable (weight 1.0)."""
    _raw_insert("fact with no timestamp about latency", ts="")
    _raw_insert("fact with garbage timestamp about throughput", ts="not-a-date")
    assert memory.search_facts("fact with no timestamp about latency")
    assert memory.search_facts("fact with garbage timestamp about throughput")


def test_search_future_dated_fact_is_findable():
    """A clock-skewed (future) timestamp must not blow up the recency weight."""
    future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    _raw_insert("future dated fact about scheduling", ts=future)
    res = memory.search_facts("future dated fact about scheduling")
    assert res and res[0]["score"] <= 1.0, res


def test_search_top_k_zero_returns_nothing():
    memory.remember("widget fact one")
    assert memory.search_facts("widget fact one", top_k=0) == []


def test_search_top_k_negative_returns_nothing():
    """top_k=-1 must not be interpreted as a slice that hides exactly one row.

    Correct behaviour: a non-positive limit yields no results (or raises);
    ``scored[:-1]`` silently returns everything but the last hit.
    """
    for i in range(3):
        memory.remember(f"negative limit widget fact number {i}")
    assert memory.search_facts("negative limit widget fact", top_k=-1) == []
    assert memory.search_docs("negative limit widget fact", 0, top_k=-1) == []


def test_search_top_k_huge_returns_all():
    for i in range(3):
        memory.remember(f"huge limit sprocket fact number {i}")
    res = memory.search_facts("huge limit sprocket fact", top_k=10_000)
    assert len(res) == 3


def test_search_underscore_is_not_a_wildcard():
    """A keyword containing '_' must not be expanded into a LIKE wildcard."""
    memory.remember("abcXdef totally unrelated filler body")
    # 'abc_def' is a literal keyword; it must not match 'abcXdef' via LIKE.
    assert memory.search_facts("abc_def unrelated") == []


def test_search_recency_weight_keeps_old_relevant_fact():
    """Freshness must re-rank, not silently delete old-but-relevant facts.

    Correct behaviour: an exact-match fact is returned with a decayed score.
    Currently score = 0.3 * 0.5^(60/30) = 0.075, below the 0.15 cutoff, so a
    two-month-old fact becomes unreachable no matter how well it matches.
    """
    _raw_insert("deployment rollback procedure for the payment service",
                ts=_old_ts(60))
    res = memory.search_facts("deployment rollback procedure for the payment service")
    assert res, "60-day-old exact match was filtered out by the freshness weight"
    assert res[0]["score"] < 0.3


def test_search_sees_more_than_first_200_candidates():
    """The 200-candidate cap has no ORDER BY, so recent rows can be invisible.

    Correct behaviour: the best/newest match must be found even when the table
    holds more than MAX_SEARCH_CANDIDATES matching rows.
    """
    for i in range(249):
        memory.remember(f"zeta filler entry number {i}")
    memory.remember("zeta unique marker definitive target row")
    res = memory.search_facts("zeta unique marker definitive target row")
    assert any("definitive target row" in h["content"] for h in res), (
        "newest matching fact was dropped by the unordered candidate LIMIT")


# --------------------------------------------------------------------------- #
# (c) Storage: size, duplicates, concurrency
# --------------------------------------------------------------------------- #


def test_store_rejects_pathological_5mb_fact():
    """A 5 MB fact has no size cap and is stored/inlined into every search.

    Correct behaviour: ``remember`` should refuse pathological payloads (the
    module caps doc chunks at 8000 chars for exactly this reason).
    """
    blob = "A" * (5 * 1024 * 1024)
    r = memory.remember(blob)
    assert r["ok"] is False, "5 MB fact accepted with no size limit"


def test_duplicate_fact_deduped():
    r1 = memory.remember("duplicate widget fact")
    r2 = memory.remember("duplicate widget fact")
    assert r1["ok"] and not r1.get("duplicate")
    assert r2.get("duplicate") and r2["id"] == r1["id"]
    assert memory.stats(None)["facts_total"] == 1


def test_same_content_in_different_scopes_is_distinct():
    p = _project("DupScope")
    g = memory.remember("shared widget fact", scope="global")
    pr = memory.remember("shared widget fact", project_id=p["id"], scope="project")
    assert g["id"] != pr["id"]
    assert memory.stats(None)["facts_total"] == 2


def test_concurrent_writes_8_threads_distinct_content():
    """8 concurrent writers must all succeed under SQLite WAL."""
    errors = _run_threads(
        lambda i: memory.remember(f"concurrent distinct fact number {i} widgets"))
    assert errors == []
    assert memory.stats(None)["facts_total"] == 8


def test_concurrent_writes_8_threads_same_content_dedupes():
    """8 concurrent writers of identical content must produce one row, no error."""
    errors = _run_threads(
        lambda i: memory.remember("concurrent identical fact about widgets"))
    assert errors == []
    assert memory.stats(None)["facts_total"] == 1


# --------------------------------------------------------------------------- #
# (d) Ollama down: graceful degradation
# --------------------------------------------------------------------------- #


def test_ollama_down_store_and_fact_search_still_work():
    """With Ollama dead, remember() must store and search must fall back to keywords."""
    r = memory.remember("ollama down fallback fact about widgets")
    assert r["ok"] is True
    assert r["embedded"] is False
    res = memory.search_facts("ollama down fallback fact")
    assert res and res[0]["content"].startswith("ollama down fallback")
    assert memory.stats(None)["ollama_ok"] is False


def test_ollama_down_doc_index_and_search_still_work():
    p = _project("NoOllamaDoc")
    changed = memory.index_text(
        p["id"], "docs/d.md", "## Gate\n\nThe request gate prevents 429 storms.\n")
    assert changed == 1
    assert memory.search_docs("request gate prevents 429 storms", p["id"])


def test_ollama_malformed_response_raises_embedding_error(monkeypatch):
    """A non-JSON reply from Ollama is a transport failure and must surface as
    ``EmbeddingError``, the error type every caller in memory.py is written to
    catch."""
    monkeypatch.setattr(embeddings.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResponse(b"<html>gateway error</html>"))
    with pytest.raises(embeddings.EmbeddingError):
        embeddings.embed_texts(["hello"], prefix=embeddings.QUERY_PREFIX)


def test_ollama_malformed_response_embed_one_returns_none(monkeypatch):
    """``embed_one`` promises ``None`` when Ollama misbehaves; a JSON decode
    error escapes it as an unexpected exception type."""
    monkeypatch.setattr(embeddings.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResponse(b"not json at all"))
    assert embeddings.embed_one("hello") is None
