"""Unified search layer for Perun.

Fuses three complementary search backends into a single, ranked result set:

* **memory** — semantic/conceptual matches from Perun's vector memory store.
* **ripgrep** — fast regex/lexical matches across registered projects.
* **ast-grep** — structural/AST matches across registered projects.

The three backends run in parallel. Code results from ripgrep and ast-grep are
fused with Reciprocal Rank Fusion (RRF) and deduplicated before truncation.
"""

from __future__ import annotations

import concurrent.futures
from typing import Any

from . import grep, memory, registry


# Per-future timeout; each backend can take up to this long, so callers should
# expect up to ~MAX_TIMEOUT per active backend.
MAX_TIMEOUT = 60.0
_TRUNCATE_LEN = 160


def _resolve_projects(projects: list[str] | None = None) -> list[dict[str, Any]] | None:
    """Return registry projects, optionally filtered to the given slugs.

    Returns ``None`` when the caller explicitly supplied a filter but it
    resolved to no projects. This lets ``unified_search`` return empty results
    rather than falling back to a global search.
    """
    all_projects = registry.list_projects()
    if projects is None:
        return all_projects
    allowed = {p.strip().lower() for p in projects if p and isinstance(p, str)}
    matched = [p for p in all_projects if (p.get("slug") or "").lower() in allowed]
    if not matched:
        return None
    return matched


def _truncate(text: str, length: int = _TRUNCATE_LEN) -> str:
    """Truncate *text* to *length*, appending ``…`` when trimmed."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = text.rstrip()
    if len(text) <= length:
        return text
    return text[: length - 1] + "…"


def _search_memory(
    query: str,
    project_list: list[dict[str, Any]],
    max_memory: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Run vector-memory search and normalise results.

    When *project_list* is empty the search is global. Otherwise each resolved
    project is queried individually and the per-project results are merged,
    re-sorted by score, and truncated to *max_memory*.

    Returns ``(normalised_results, tool_name)`` where ``tool_name`` is
    ``"memory"`` on success or ``None`` on failure/empty results.
    """
    raw: list[dict[str, Any]]
    if not project_list:
        raw = memory.unified_search(
            query,
            project_id=0,
            all_projects=True,
            k=max_memory,
        )
    else:
        raw = []
        for project in project_list:
            pid = project["id"]
            if pid == 0 or str(pid) == "0":
                # Treat id 0 as the global scope.
                raw.extend(
                    memory.unified_search(
                        query,
                        project_id=0,
                        all_projects=True,
                        k=max_memory,
                    )
                )
            else:
                raw.extend(
                    memory.unified_search(
                        query,
                        project_id=pid,
                        all_projects=False,
                        k=max_memory,
                    )
                )
        raw.sort(key=lambda r: r.get("score", 0.0), reverse=True)
        raw = raw[:max_memory]

    if not raw:
        return [], "memory"

    id_to_slug: dict[Any, str] = {}
    for project in project_list:
        pid = project["id"]
        slug = project.get("slug") or project.get("name") or str(pid)
        id_to_slug[pid] = slug
        # Accept either numeric ids or numeric strings from the registry.
        try:
            id_to_slug[int(pid)] = slug
        except (ValueError, TypeError):
            pass
        id_to_slug[str(pid)] = slug

    normalized: list[dict[str, Any]] = []
    for r in raw:
        pid = r.get("project_id", 0)
        if pid == 0 or str(pid) == "0":
            slug = "global"
        else:
            slug = id_to_slug.get(pid, str(pid))
        is_doc = r.get("source_type") == "doc"
        file_name = r.get("source_file", "") if is_doc else ""
        section = r.get("section", r.get("topic", ""))
        content = r.get("content", "")
        normalized.append({
            "source": "memory",
            "project": slug,
            "file": file_name,
            "section": section,
            "text": _truncate(content[:200] if len(content) > 200 else content),
            "score": r.get("score", 0.0),
        })
    return normalized, "memory"


def _search_code_grep(
    pattern: str,
    project_list: list[dict[str, Any]],
    max_code: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Run global ripgrep search and normalise matches."""
    resp = grep.rg_search_global(pattern, project_list, max_results=max_code * 2)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "ripgrep search failed"))

    tool = resp.get("tool", "rg")
    source = "rg" if tool.startswith(("rg", "python-grep")) else tool
    matches = resp.get("matches", [])
    for m in matches:
        m["source"] = source
    return matches, "rg"


def _search_code_ast(
    rule: str,
    project_list: list[dict[str, Any]],
    max_code: int,
) -> tuple[list[dict[str, Any]], str | None]:
    """Run global ast-grep search and normalise matches."""
    resp = grep.ast_grep_search_global(rule, project_list, max_results=max_code * 2)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", "ast-grep search failed"))

    tool = resp.get("tool", "ast-grep")
    source = "ast-grep" if tool.startswith(("ast-grep", "python-ast")) else tool
    matches = resp.get("matches", [])
    for m in matches:
        m["source"] = source
    return matches, "ast-grep"


def _rrf_fuse(
    grep_results: dict[str, Any] | list[dict[str, Any]],
    ast_results: dict[str, Any] | list[dict[str, Any]],
    max_code: int,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """Fuse two ranked code-result lists using Reciprocal Rank Fusion.

    Each result list contributes ``1 / (rrf_k + rank)`` for its rank. Scores
    from multiple lists are summed for the same match. The representative match
    for a deduplication key is taken from the source list where that key ranks
    highest. Results are deduplicated by ``(project, file, line, column)`` when
    a column is present and by ``(project, file, line)`` otherwise, then
    truncated to *max_code*.

    Accepts either raw search response dicts (with ``matches`` and ``tool``)
    or already-extracted lists of matches.
    """
    scores: dict[tuple[Any, ...], float] = {}
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    best_rank: dict[tuple[Any, ...], int] = {}

    def _key(m: dict[str, Any]) -> tuple[Any, ...]:
        project = str(m.get("project", ""))
        file = str(m.get("file", ""))
        line = int(m.get("line", 0) or 0)
        column = m.get("column")
        if column is not None:
            return (project, file, line, int(column or 0))
        return (project, file, line)

    def _extract(resp: dict[str, Any] | list[dict[str, Any]], default_source: str) -> tuple[list[dict[str, Any]], str]:
        if isinstance(resp, dict):
            matches = resp.get("matches", []) or []
            tool = resp.get("tool", default_source)
            source = "rg" if tool.startswith(("rg", "python-grep")) else (
                "ast-grep" if tool.startswith(("ast-grep", "python-ast")) else tool
            )
            return matches, source
        return resp, default_source

    def _add_results(resp: dict[str, Any] | list[dict[str, Any]], default_source: str) -> None:
        matches, fallback_source = _extract(resp, default_source)
        for rank, m in enumerate(matches):
            key = _key(m)
            score = 1.0 / (rrf_k + rank + 1)
            scores[key] = scores.get(key, 0.0) + score

            # Keep the match from the highest-ranked source list for this key.
            if key not in best_rank or rank < best_rank[key]:
                best_rank[key] = rank
                best[key] = dict(m)
                if "source" not in best[key]:
                    best[key]["source"] = m.get("source", fallback_source)

    _add_results(grep_results, "rg")
    _add_results(ast_results, "ast-grep")

    fused = sorted(best.values(), key=lambda m: scores[_key(m)], reverse=True)
    out: list[dict[str, Any]] = []
    for m in fused[:max_code]:
        key = _key(m)
        out.append({
            "source": m.get("source", "code"),
            "project": m.get("project", ""),
            "file": m.get("file", ""),
            "line": m.get("line", 0),
            "column": m.get("column", 0),
            "text": _truncate(m.get("text", "")),
            "match": _truncate(m.get("match", "")),
            "score": round(scores[key], 6),
        })
    return out


def capabilities() -> dict[str, bool]:
    """Return which search backends are available on this machine."""
    return {
        "memory": True,
        "rg": grep.rg_available(),
        "ast_grep": grep.ast_grep_available(),
        "unified": True,
    }


def unified_search(
    query: str = "",
    pattern: str = "",
    rule: str = "",
    projects: list[str] | None = None,
    *,
    use_memory: bool = True,
    use_grep: bool = True,
    use_ast_grep: bool = True,
    max_memory: int = 6,
    max_code: int = 20,
    rrf_k: int = 60,
) -> dict[str, Any]:
    """Run a unified search across memory, ripgrep, and ast-grep.

    Args:
        query: natural-language query for vector memory search.
        pattern: regex pattern for ripgrep lexical search.
        rule: ast-grep structural rule/pattern.
        projects: optional list of project slugs to restrict the search to.
        use_memory: enable memory search when ``query`` is provided.
        use_grep: enable ripgrep search when ``pattern`` is provided.
        use_ast_grep: enable ast-grep search when ``rule`` is provided.
        max_memory: maximum number of memory results to return.
        max_code: maximum number of fused code results to return.
        rrf_k: RRF constant for rank scoring.

    Returns:
        ``{"ok": true, "memory": [...], "code": [...], "tools_used": [...],
        "projects_searched": N, "total": T, "source_errors": {...}}`` or an
        error dict when every active source fails.
    """
    will_search_memory = use_memory and query.strip()
    will_search_grep = use_grep and pattern.strip()
    will_search_ast = use_ast_grep and rule.strip()

    if not (will_search_memory or will_search_grep or will_search_ast):
        return {
            "ok": False,
            "error_kind": "invalid_input",
            "error": "provide query (memory), pattern (grep), or rule (ast-grep)",
        }

    project_list = _resolve_projects(projects)
    if project_list is None:
        return {
            "ok": True,
            "memory": [],
            "code": [],
            "tools_used": [],
            "projects_searched": 0,
            "total": 0,
            "source_errors": {},
        }
    tools_used: list[str] = []
    source_errors: dict[str, str] = {}

    memory_results: list[dict[str, Any]] = []
    grep_results: list[dict[str, Any]] = []
    ast_results: list[dict[str, Any]] = []

    futures: dict[str, Any] = {}
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    try:
        if will_search_memory:
            futures["memory"] = executor.submit(
                _search_memory, query.strip(), project_list, max_memory
            )
        if will_search_grep:
            futures["grep"] = executor.submit(
                _search_code_grep, pattern.strip(), project_list, max_code
            )
        if will_search_ast:
            futures["ast"] = executor.submit(
                _search_code_ast, rule.strip(), project_list, max_code
            )

        for name, future in futures.items():
            try:
                results, tool = future.result(timeout=MAX_TIMEOUT)
                if tool:
                    tools_used.append(tool)
                if name == "memory":
                    memory_results = results
                elif name == "grep":
                    grep_results = results
                elif name == "ast":
                    ast_results = results
            except Exception as exc:
                source_errors[name] = str(exc)
    finally:
        # Do not block on a hung backend: waiting would defeat the per-future
        # timeout above. Running threads are left to finish in the background.
        executor.shutdown(wait=False, cancel_futures=True)

    code_results = _rrf_fuse(grep_results, ast_results, max_code, rrf_k=rrf_k)

    if not tools_used and source_errors:
        return {
            "ok": False,
            "error_kind": "unknown",
            "error": "all search sources failed",
            "source_errors": source_errors,
        }

    total = len(memory_results) + len(code_results)
    return {
        "ok": True,
        "memory": memory_results,
        "code": code_results,
        "tools_used": tools_used,
        "projects_searched": len(project_list),
        "total": total,
        "source_errors": source_errors,
    }


if __name__ == "__main__":
    print("capabilities:", capabilities())
    print("-" * 40)
    demo = unified_search(
        query="orchestrator",
        pattern="def unified_search",
        rule="def unified_search($$$ARGS)",
        max_memory=3,
        max_code=5,
    )
    print("demo ok:", demo.get("ok"))
    print("tools used:", demo.get("tools_used"))
    print("memory hits:", len(demo.get("memory", [])))
    print("code hits:", len(demo.get("code", [])))
