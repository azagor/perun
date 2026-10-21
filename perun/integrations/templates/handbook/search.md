# Code search: which tool, when

Perun ships its own search layer (`perun/grep.py`, `perun/search.py`) so you
never need a separate search MCP server. It wraps **ripgrep** and
**ast-grep**, with pure-Python fallbacks when neither CLI is installed.

## The priority order

1. **Start with `docs/CODEMAP.md`.** It is the navigation source of truth for
   every Perun-managed project. Use its file/identifier tables to find the
   right module *before* doing a broad search. Do not grep a codebase you have
   a map for.
2. **Use `perun_unified_search` as the single entry point for cross-project
   discovery.** It fuses vector memory, global ripgrep and global ast-grep in
   one call and re-ranks the code-level results with Reciprocal Rank Fusion
   (RRF), so one call beats three.
3. **For a targeted second pass, use the global code-search tools:**
   * `perun_global_grep` — regex/lexical search across every registered project.
   * `perun_global_ast_grep` — structural/AST search across every registered project.
4. **For work inside a single project, use `perun_grep` and `perun_ast_grep`.**
5. **Use OpenCode's built-in `grep`/`read` only as a last resort** — when the
   Perun search tools are unavailable, or when the query is trivial and the
   file is already known.

## `perun_unified_search`

```
perun_unified_search(
    query: str = "",        # natural language -> vector memory
    pattern: str = "",      # POSIX regex   -> global grep
    rule: str = "",         # ast-grep rule -> global ast-grep
    projects: list[str] | None = None,   # restrict to these slugs
    memory: bool = True,    # include vector memory
    grep: bool = True,      # include grep (used when pattern is given)
    ast_grep: bool = True,  # include ast-grep (used when rule is given)
    max_results: int = 20,  # code-level results (max 200)
    max_memory: int = 6,    # memory results (1-50)
)
```

The three channels are independent: give it a `query` only, a `pattern` only,
a `rule` only, or any combination. `pattern` and `rule` are ignored when
empty, so you never pay for a channel you did not ask for.

## `perun_grep` / `perun_ast_grep` (single project)

```
perun_grep(pattern: str, project: str | None = None, path: str = "",
           max_results: int = 100, case_sensitive: bool = True)
perun_ast_grep(rule: str, project: str | None = None, path: str = "",
               max_results: int = 50)
```

`pattern` is a regex. `rule` is an ast-grep rule — either the shorthand
`$F($$$ARGS)` style, or a full YAML rule. `path` is relative to the project
root.

## Safety rules the tools already enforce

* Patterns that could be catastrophic (nested quantifiers) are rejected.
* `max_results` is clamped; a hostile pattern cannot ask for 10^9 rows.
* Paths that escape the project root are refused.
* Every search degrades gracefully: a missing ripgrep falls back to a Python
  scan, a missing Ollama falls back to keyword + recency ranking, and a
  failing backend is reported rather than silently returning nothing.

## Practical notes

* `.gitignore` is honoured by the Python fallback, so vendored directories do
  not pollute results.
* Doc chunks carry extracted `paths` and `identifiers` metadata, so a memory
  hit can tell you which file and symbol it came from.
* `perun_docs_index` refreshes a project's `docs/*` into the vector store.
  Run it after significant doc changes, or use `/update-docs`.
