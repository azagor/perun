from unittest.mock import MagicMock

import pytest

from perun import search


def test_capabilities():
    caps = search.capabilities()
    assert isinstance(caps, dict)
    assert caps["memory"] is True
    assert isinstance(caps["rg"], bool)
    assert isinstance(caps["ast_grep"], bool)
    assert caps.get("unified") is True


def test_truncate():
    assert search._truncate("short", 10) == "short"
    assert search._truncate("a" * 200, 10) == "a" * 9 + "…"


def test_resolve_projects_no_filter(monkeypatch) -> None:
    fake = [{"id": 1, "slug": "a", "path": "/x/a"}, {"id": 2, "slug": "b", "path": "/x/b"}]
    monkeypatch.setattr("perun.registry.list_projects", lambda: fake)
    assert search._resolve_projects(None) == fake


def test_resolve_projects_with_filter(monkeypatch) -> None:
    fake = [{"id": 1, "slug": "a", "path": "/x/a"}, {"id": 2, "slug": "b", "path": "/x/b"}]
    monkeypatch.setattr("perun.registry.list_projects", lambda: fake)
    assert search._resolve_projects(["b"]) == [fake[1]]


def test_resolve_projects_unknown_filter(monkeypatch) -> None:
    fake = [{"id": 1, "slug": "a", "path": "/x/a"}, {"id": 2, "slug": "b", "path": "/x/b"}]
    monkeypatch.setattr("perun.registry.list_projects", lambda: fake)
    assert search._resolve_projects(["z"]) is None


def test_rrf_fuse_basic():
    grep_results = {
        "ok": True,
        "matches": [
            {"project": "p1", "file": "a.py", "line": 10, "text": "foo"},
            {"project": "p1", "file": "a.py", "line": 20, "text": "bar"},
        ],
        "tool": "rg-global",
    }
    ast_results = {
        "ok": True,
        "matches": [
            {"project": "p1", "file": "a.py", "line": 10, "text": "foo"},  # dup
            {"project": "p1", "file": "a.py", "line": 30, "text": "baz"},
        ],
        "tool": "ast-grep-global",
    }
    fused = search._rrf_fuse(grep_results, ast_results, max_code=10, rrf_k=60)
    assert len(fused) == 3
    # Duplicate line 10 should have the highest score (sum of both lists)
    assert fused[0]["line"] == 10
    assert fused[0]["score"] > fused[1]["score"]


def test_rrf_fuse_representative_source():
    """The representative match comes from the source list with the highest rank."""
    grep_results = {
        "ok": True,
        "matches": [
            {"project": "p1", "file": "a.py", "line": 5, "text": "first"},
            {"project": "p1", "file": "a.py", "line": 10, "text": "grep-hit"},
        ],
        "tool": "rg-global",
    }
    ast_results = {
        "ok": True,
        "matches": [
            {"project": "p1", "file": "a.py", "line": 10, "text": "ast-hit"},
        ],
        "tool": "ast-grep-global",
    }
    fused = search._rrf_fuse(grep_results, ast_results, max_code=10)
    line10 = [m for m in fused if m["line"] == 10][0]
    assert line10["source"] == "ast-grep"


def test_rrf_fuse_one_source():
    grep_results = {
        "ok": True,
        "matches": [{"project": "p1", "file": "a.py", "line": 5, "text": "x"}],
        "tool": "rg-global",
    }
    fused = search._rrf_fuse(grep_results, {"ok": True, "matches": [], "tool": "ast-grep-global"}, max_code=10)
    assert len(fused) == 1
    assert fused[0]["source"] == "rg"


def test_unified_search_no_sources():
    result = search.unified_search()
    assert not result["ok"]
    assert "provide" in result["error"].lower()
    assert result["error_kind"] == "invalid_input"


def test_unified_search_memory_only(monkeypatch) -> None:
    fake_projects = [{"id": 1, "slug": "proj", "path": "/x/proj"}]
    fake_memory = [{"project_id": 1, "source_file": "README.md", "section": "intro", "content": "hello world", "score": 0.9, "source_type": "doc"}]
    monkeypatch.setattr("perun.registry.list_projects", lambda: fake_projects)
    monkeypatch.setattr("perun.memory.unified_search", lambda *a, **k: fake_memory)
    monkeypatch.setattr("perun.grep.rg_search_global", lambda *a, **k: {"ok": True, "matches": [], "tool": "rg-global"})
    monkeypatch.setattr("perun.grep.ast_grep_search_global", lambda *a, **k: {"ok": True, "matches": [], "tool": "ast-grep-global"})

    result = search.unified_search(query="hello", use_memory=True, use_grep=False, use_ast_grep=False)
    assert result["ok"]
    assert "memory" in result["tools_used"]
    assert len(result["memory"]) == 1
    assert result["memory"][0]["project"] == "proj"


def test_unified_search_code_fusion(monkeypatch) -> None:
    fake_projects = [{"id": 1, "slug": "proj", "path": "/x/proj"}]
    monkeypatch.setattr("perun.registry.list_projects", lambda: fake_projects)
    monkeypatch.setattr("perun.memory.unified_search", lambda *a, **k: [])
    monkeypatch.setattr("perun.grep.rg_search_global", lambda *a, **k: {
        "ok": True,
        "matches": [{"project": "proj", "file": "a.py", "line": 1, "column": 0, "text": "foo", "match": "foo", "source": "rg"}],
        "tool": "rg-global",
    })
    monkeypatch.setattr("perun.grep.ast_grep_search_global", lambda *a, **k: {
        "ok": True,
        "matches": [{"project": "proj", "file": "a.py", "line": 2, "column": 0, "text": "bar", "match": "bar", "source": "ast-grep"}],
        "tool": "ast-grep-global",
    })

    result = search.unified_search(pattern="foo", rule="bar", use_memory=False)
    assert result["ok"]
    assert "rg" in result["tools_used"]
    assert "ast-grep" in result["tools_used"]
    assert len(result["code"]) == 2


def test_unified_search_project_filter(monkeypatch) -> None:
    fake_projects = [{"id": 1, "slug": "a", "path": "/x/a"}, {"id": 2, "slug": "b", "path": "/x/b"}]
    monkeypatch.setattr("perun.registry.list_projects", lambda: fake_projects)
    called_with = []
    def fake_grep(pattern, projects, **k):
        called_with.extend([p["slug"] for p in projects])
        return {"ok": True, "matches": [], "tool": "rg-global"}
    monkeypatch.setattr("perun.grep.rg_search_global", fake_grep)
    monkeypatch.setattr("perun.memory.unified_search", lambda *a, **k: [])
    monkeypatch.setattr("perun.grep.ast_grep_search_global", lambda *a, **k: {"ok": True, "matches": [], "tool": "ast-grep-global"})

    result = search.unified_search(pattern="x", projects=["b"], use_memory=False, use_ast_grep=False)
    assert result["ok"]
    assert called_with == ["b"]


def test_unified_search_graceful_source_failure(monkeypatch) -> None:
    fake_projects = [{"id": 1, "slug": "proj", "path": "/x/proj"}]
    monkeypatch.setattr("perun.registry.list_projects", lambda: fake_projects)
    monkeypatch.setattr("perun.memory.unified_search", lambda *a, **k: [])
    monkeypatch.setattr("perun.grep.rg_search_global", lambda *a, **k: {"ok": False, "error": "boom"})
    monkeypatch.setattr("perun.grep.ast_grep_search_global", lambda *a, **k: {"ok": True, "matches": [], "tool": "ast-grep-global"})

    result = search.unified_search(pattern="x", rule="y")
    assert result["ok"]
    assert "rg" not in result["tools_used"]
    assert "ast-grep" in result["tools_used"]
    assert result["source_errors"].get("grep") == "boom"


def test_unified_search_all_sources_fail(monkeypatch) -> None:
    fake_projects = [{"id": 1, "slug": "proj", "path": "/x/proj"}]

    def bad_memory(*args, **kwargs):
        raise RuntimeError("memory boom")

    monkeypatch.setattr("perun.registry.list_projects", lambda: fake_projects)
    monkeypatch.setattr("perun.memory.unified_search", bad_memory)
    monkeypatch.setattr("perun.grep.rg_search_global", lambda *a, **k: {"ok": False, "error": "grep boom"})
    monkeypatch.setattr("perun.grep.ast_grep_search_global", lambda *a, **k: {"ok": False, "error": "ast boom"})

    result = search.unified_search(query="x", pattern="y", rule="z")
    assert not result["ok"]
    assert "all search sources failed" in result["error"].lower()
    assert result["error_kind"] == "unknown"
    assert "memory" in result["source_errors"]
    assert "grep" in result["source_errors"]
    assert "ast" in result["source_errors"]


def test_unified_search_partial_failure(monkeypatch) -> None:
    fake_projects = [{"id": 1, "slug": "proj", "path": "/x/proj"}]
    monkeypatch.setattr("perun.registry.list_projects", lambda: fake_projects)
    monkeypatch.setattr("perun.memory.unified_search", lambda *a, **k: [])
    monkeypatch.setattr("perun.grep.rg_search_global", lambda *a, **k: {"ok": False, "error": "grep boom"})
    monkeypatch.setattr("perun.grep.ast_grep_search_global", lambda *a, **k: {"ok": True, "matches": [], "tool": "ast-grep-global"})

    result = search.unified_search(query="x", pattern="y", rule="z")
    assert result["ok"]
    assert result["source_errors"].get("grep") == "grep boom"
    assert "memory" in result["tools_used"]
    assert "ast-grep" in result["tools_used"]


def test_rrf_sums_scores():
    grep_results = {
        "ok": True,
        "tool": "rg-global",
        "matches": [
            {"project": "p1", "file": "a.py", "line": 10, "column": 0, "text": "common", "match": "common"},
            {"project": "p1", "file": "a.py", "line": 20, "column": 0, "text": "grep-only", "match": "grep-only"},
        ],
    }
    ast_results = {
        "ok": True,
        "tool": "ast-grep-global",
        "matches": [
            {"project": "p1", "file": "a.py", "line": 30, "column": 0, "text": "ast-1", "match": "ast-1"},
            {"project": "p1", "file": "a.py", "line": 40, "column": 0, "text": "ast-2", "match": "ast-2"},
            {"project": "p1", "file": "a.py", "line": 10, "column": 0, "text": "common", "match": "common"},
        ],
    }
    fused = search._rrf_fuse(grep_results, ast_results, max_code=10)
    common = [m for m in fused if m["line"] == 10][0]
    expected = 1.0 / (60 + 1) + 1.0 / (60 + 3)
    assert common["score"] == pytest.approx(round(expected, 6))
    assert common["source"] == "rg"


def test_all_sources_fail_returns_error(monkeypatch) -> None:
    fake_projects = [{"id": 1, "slug": "proj", "path": "/x/proj"}]

    def raise_memory(*args, **kwargs):
        raise RuntimeError("memory boom")

    def raise_ast(*args, **kwargs):
        raise RuntimeError("ast boom")

    monkeypatch.setattr("perun.registry.list_projects", lambda: fake_projects)
    monkeypatch.setattr("perun.memory.unified_search", raise_memory)
    monkeypatch.setattr("perun.grep.rg_search_global", lambda *a, **k: {"ok": False, "error": "grep boom"})
    monkeypatch.setattr("perun.grep.ast_grep_search_global", raise_ast)

    result = search.unified_search(query="x", pattern="y", rule="z")
    assert not result["ok"]
    assert "source_errors" in result
    assert "memory" in result["source_errors"]
    assert "grep" in result["source_errors"]
    assert "ast" in result["source_errors"]


def test_partial_failure_surfaces_error(monkeypatch) -> None:
    fake_projects = [{"id": 1, "slug": "proj", "path": "/x/proj"}]
    monkeypatch.setattr("perun.registry.list_projects", lambda: fake_projects)
    monkeypatch.setattr("perun.memory.unified_search", lambda *a, **k: [])
    monkeypatch.setattr("perun.grep.rg_search_global", lambda *a, **k: {"ok": True, "matches": [], "tool": "rg-global"})
    monkeypatch.setattr("perun.grep.ast_grep_search_global", lambda *a, **k: {"ok": False, "error": "ast boom"})

    result = search.unified_search(query="x", pattern="y", rule="z")
    assert result["ok"]
    assert "source_errors" in result
    assert result["source_errors"].get("ast") == "ast boom"
    assert "memory" in result["tools_used"]
    assert "rg" in result["tools_used"]


def test_memory_filtered_by_project(monkeypatch) -> None:
    fake_projects = [
        {"id": 1, "slug": "a", "path": "/x/a"},
        {"id": 2, "slug": "b", "path": "/x/b"},
    ]

    def fake_memory(query, project_id, all_projects, k):
        if project_id == 1:
            return [{"project_id": 1, "source_file": "a.md", "section": "s", "content": "hello a", "score": 0.8, "source_type": "doc"}]
        if project_id == 2:
            return [{"project_id": 2, "source_file": "b.md", "section": "s", "content": "hello b", "score": 0.9, "source_type": "doc"}]
        return []

    monkeypatch.setattr("perun.registry.list_projects", lambda: fake_projects)
    monkeypatch.setattr("perun.memory.unified_search", fake_memory)

    result = search.unified_search(query="hello", projects=["b"], use_grep=False, use_ast_grep=False)
    assert result["ok"]
    assert len(result["memory"]) == 1
    assert result["memory"][0]["project"] == "b"


def test_unified_search_source_timeout_is_bounded(monkeypatch) -> None:
    """A hung backend is reported as a source error and must not block the result."""
    import time

    fake_projects = [{"id": 1, "slug": "proj", "path": "/x/proj"}]
    monkeypatch.setattr("perun.registry.list_projects", lambda: fake_projects)
    monkeypatch.setattr(search, "MAX_TIMEOUT", 0.1)

    def slow_grep(*args, **kwargs):
        time.sleep(1.0)
        return {"ok": True, "matches": [], "tool": "rg-global"}

    monkeypatch.setattr("perun.grep.rg_search_global", slow_grep)
    monkeypatch.setattr("perun.memory.unified_search", lambda *a, **k: [])
    monkeypatch.setattr(
        "perun.grep.ast_grep_search_global",
        lambda *a, **k: {"ok": True, "matches": [], "tool": "ast-grep-global"},
    )

    start = time.monotonic()
    result = search.unified_search(pattern="x", rule="y", use_memory=False)
    elapsed = time.monotonic() - start

    # Returns well before the hung backend (1.0s) would have finished.
    assert elapsed < 0.6
    assert "grep" in result["source_errors"]
    # The healthy backend still contributes.
    assert "ast-grep" in result["tools_used"]


def test_unified_search_parameter_rename(monkeypatch) -> None:
    fake_projects = [{"id": 1, "slug": "proj", "path": "/x/proj"}]
    monkeypatch.setattr("perun.registry.list_projects", lambda: fake_projects)
    monkeypatch.setattr(
        "perun.memory.unified_search",
        lambda *a, **k: [{"project_id": 1, "source_file": "README.md", "section": "intro", "content": "hello world", "score": 0.9, "source_type": "doc"}],
    )
    monkeypatch.setattr("perun.grep.rg_search_global", lambda *a, **k: {"ok": True, "matches": [], "tool": "rg-global"})
    monkeypatch.setattr("perun.grep.ast_grep_search_global", lambda *a, **k: {"ok": True, "matches": [], "tool": "ast-grep-global"})

    result = search.unified_search(query="hello", use_memory=True, use_grep=False, use_ast_grep=False)
    assert result["ok"]
    assert "memory" in result["tools_used"]
    assert len(result["memory"]) == 1
