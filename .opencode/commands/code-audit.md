---
description: Audit code changes made in the current session for thread safety, security risks, test coverage, and production readiness in the Perun codebase.
---

Perform a production-readiness code audit on the changes made in the current session.

## 1. Identify the changes

Prefer the working-tree diff over git history:

- Run `git status --short` and `git diff --stat` to list uncommitted modifications, additions, and deletions.
- If the working tree is clean, fall back to the most recent commit: `git log --oneline -1` and `git diff HEAD~1 --stat`.
- If the user provided arguments (`$ARGUMENTS`), narrow the audit to those files/paths.
- Treat the working tree as the source of truth. Do **not** revert, stash, or overwrite uncommitted work.

## 2. CODEMAP-first context

For every touched subsystem, read the relevant documentation first:

- `docs/CODEMAP.md` — locate the subsystem, files, and identifiers involved.
- `docs/TECHNICAL_DOCUMENTATION.md` — read the section(s) covering the changed behavior.
- `AGENTS.md` — respect critical constraints (SQLite per-operation, no secrets in memory, graceful degradation, Telegram bridge singleton, etc.).

## 3. Audit dimensions

Review each changed file across these dimensions:

- **FastAPI/SQLite rules**: use the `db.db()` context manager per operation; never hold a connection across a network call; WAL mode is required.
- **Never block the event loop**: synchronous endpoints run in the thread pool; SSE endpoints pump blocking generators through `iterate_in_threadpool`.
- **No secrets in memory**: `memory.is_sensitive()` guards `remember()`; keep new storage paths behind the same guard.
- **Graceful degradation**: Ollama down, `opencode` missing, no Telegram token — none may crash the service; each degrades to a clear message.
- **Process/orchestrator safety**: `orchestrator.py` process/streaming must not leak processes; `ProcessTracker` finalizes runs correctly.
- **MCP/tool additions**: new tools in `mcp/perun_mcp.py` must resolve projects and document endpoints.
- **Security risks**: credential handling, secret leakage in logs/docs, input sanitization, injection, unsafe deserialization.
- **Error handling & fallbacks**: network failures, exceptions, `None`/empty inputs, missing keys, DB locks.
- **Resource leaks**: threads, locks, DB connections, file handles, network sockets, subscriptions.
- **Test coverage**: are new functions/paths exercised by tests? Should regression tests be added?
- **Documentation/traceability**: does the change require updates to `docs/CHANGELOG.md`, `docs/TECHNICAL_DOCUMENTATION.md`, `docs/CODEMAP.md`, or `AGENTS.md`?

## 4. Run static checks

- `python -c "import py_compile; py_compile.compile('<file>', doraise=True)"` for each changed Python file.
- `python -m pytest tests/ -q` for the full suite or targeted tests for affected modules.

## 5. Report

Return a concise but complete audit report:

- **Changes audited**: list of files and a one-line summary of each change.
- **Critical/high-risk findings**: anything that could cause crashes, data loss, security exposure, or regressions.
- **Edge cases not covered**: missing error paths, races, permissions, or test gaps.
- **Recommendations**: concrete, minimal fixes or tests to add.
- **Production-ready verdict**: whether the code is safe to ship as-is, and what must happen before it is.
- **Checks run**: command outputs or pass/fail status.

Do not modify files unless a clear, serious bug is found and the fix is minimal and safe. Prefer recommendations first; ask the user before applying non-trivial changes.
