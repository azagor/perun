# Perun — Agent Reference

> This is the repository for **Perun**, a local overseer/orchestrator for all
> OpenCode projects. It is itself an OpenCode project, so agents working *in*
> this repo should follow the conventions below.

## What Perun is

A single machine-wide Python service (FastAPI + SQLite + a static SPA) that:

1. **Registers** every OpenCode project it can find (`discovery.py`), including
   itself (`register_self()`).
2. Stores **unified, project-namespaced memory** — facts + indexed docs with
   Ollama embeddings (`memory.py`, `embeddings.py`).
3. **Orchestrates** `opencode run` sessions in any project (`orchestrator.py`),
   injecting per-project **custom instructions** on new sessions.
4. Serves a **web dashboard** and a **REST/SSE API** (`server.py`, `web/`) with a
   **project creation wizard**, a **folder browser**, and **per-project Git**.
5. Runs **one shared Telegram bridge** with per-chat project routing
   (`telegram.py`).
6. Reads a self-describing **project manifest** — `perun.md` (YAML front matter +
   Markdown body) with a `perun.json` fallback (`manifest.py`).
7. Ships a global **MCP server** (`mcp/perun_mcp.py`) so every project inherits
   Perun's tools.
8. Provides an **always-available orchestrator** — the "control plane"
   (`control.py`), reachable with **zero** projects registered, with a dashboard
   chat (`#/orchestrator`) and its own Settings.

## Core rules for working here

* **CODEMAP-first.** `docs/CODEMAP.md` is the navigation source of truth. Read it
  (and the relevant `docs/TECHNICAL_DOCUMENTATION.md` section) before grepping.
  Use `ast-grep` for structural code search and `mcp-grep`/`rg` only to confirm.
* **Keep the layers separate.** `registry.py`/`db.py` = persistence;
  `memory.py` = facts + docs; `orchestrator.py` = process/streaming;
  `server.py` = HTTP only (no business logic); `web/` = presentation only.
* **SQLite access is per-operation.** Always use the `db.db()` context manager;
  never hold a connection across a network call. WAL mode is required.
* **Never block the event loop.** Synchronous FastAPI endpoints run in the
  thread pool; SSE endpoints pump blocking generators through
  `iterate_in_threadpool`. Keep it that way.
* **No secrets in memory.** `memory.is_sensitive()` guards `remember()`; keep
  new storage paths behind the same guard.
* **Graceful degradation.** Ollama down, `opencode` missing, no Telegram token
  — none may crash the service; each degrades to a clear message.
* **Additive global install.** `integrations/install_global.py` must preserve
  unrelated config keys, back up before writing, and be fully reversible.
* **The Telegram bridge is a singleton.** `getUpdates` allows one consumer per
  bot token; keep `telegram.PidLock` (a machine-global named mutex) as the guard
  and never start a second poller. `serve --bridge`, `perun bridge`, the
  `PerunBridge` logon task, and `Perun.cmd` must all defer to that lock.

## Adding a new source file

Any new module under `perun/` is picked up automatically (the package is
imported, not copied), but two things must stay in sync:

1. `docs/CODEMAP.md` — add the file + its key identifiers.
2. `pyproject.toml` `[tool.setuptools] packages` if you add a new subpackage.

New web assets just live under `web/` and are served statically.

## Adding a new REST endpoint

1. Implement it in `perun/server.py` as a `def` (sync) or `async def` (SSE)
   function with a `@app.get`/`@app.post`/… decorator.
2. Return `_ok(...)`-shaped JSON (`{"ok": true, ...}`); raise `HTTPException`
   for errors.
3. Add a row to the endpoint table in `docs/TECHNICAL_DOCUMENTATION.md`.

## Adding a new MCP tool

1. Add a `@mcp.tool`-decorated function in `mcp/perun_mcp.py`.
2. Resolve the project with `_resolve_project(project)` (cwd-aware).
3. Document it in the tools table in `mcp/perun_mcp.py`'s docstring *and* in
   `perun/integrations/templates/INSTRUCTIONS.md`.

## Commands

```bash
# syntax check
python -c "import py_compile,glob; [py_compile.compile(f,doraise=True) for f in glob.glob('perun/**/*.py',recursive=True)+glob.glob('mcp/*.py')]"

# run the test suite
python -m pytest tests/ -q

# boot the dashboard (temp state dir)
$env:PERUN_HOME="$env:TEMP\perun_dev"; python -m perun serve

# one-click launcher (Windows; idempotent, opens the browser)
.\Perun.cmd

# regenerate the app icon (web/assets/perun.ico + .png)
python tools/make_icon.py

# discover + index everything
python -m perun discover --index
```

## Versioning

There is a single source of truth for history: `docs/CHANGELOG.md`. When you
make a notable change, append an entry there and update the "Latest release"
line in `docs/TECHNICAL_DOCUMENTATION.md`.
