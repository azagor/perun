# Perun — Agent Reference

> This is the repository for **Perun**, a local overseer/orchestrator for all
> OpenCode projects. It is itself an OpenCode project, so agents working *in*
> this repo should follow the conventions below.

A single machine-wide Python service (FastAPI + SQLite + a static SPA) that
registers every OpenCode project it can find, stores unified
project-namespaced memory, orchestrates `opencode run` sessions, serves a web
dashboard and a REST/SSE API, and runs one shared Telegram bridge. The
module-by-module breakdown is retrievable as `perun.context.overview`.

## Read first

| What | Where |
|---|---|
| Navigation source of truth | `docs/CODEMAP.md` |
| Architecture, API and data model | `docs/TECHNICAL_DOCUMENTATION.md` |
| Product design and principles | `docs/DESIGN.md` |
| Project identity, anchors, commands | `perun.md` |
| Migration losslessness map | `.perun/context-map.json` |
| Release history | `docs/CHANGELOG.md` |

## Context (retrievable, not ambient)

Reference material was moved out of this file into the Context layer, so a
session only pays for what it actually uses. Ask for it by key:

| Key | Covers |
|---|---|
| `perun.agents.full` | this file in full, as it was before the distillation |
| `perun.context.overview` | what the project is, module by module |
| `perun.context.contributing` | adding a source file, a REST endpoint, an MCP tool |
| `perun.context.commands` | syntax check, test suite, dev server, UI dump, install |
| `perun.context.credentials` | Git push credentials and the `workflow` scope trap |
| `perun.context.releasing` | the release procedure: version bump, tag, signed installer |

In an OpenCode TUI session, call `perun_context("<what you are about to do>")`
before you start. An orchestrated Perun run pre-fetches the matching context
automatically. The nine machine-wide `handbook.*` keys (`tools`, `search`,
`memory`, `commands`, `cross-project`, `sessions`, `onboarding`, `secrets`,
`global`) are visible from every project; `perun context list` prints the full
inventory.

## Cross-project references

Always use the explicit `perun.project_roots` resolver when reading or writing
files in another project. Never guess or hand-craft paths like
`..\perun_chat\perunchat\api.py` or `.\my_apps\perun\AGENTS.md` — those
resolve relative to the current working directory and will silently target the
wrong file.

```python
from perun.project_roots import resolve
api_path = resolve("perun_chat", "perunchat/api.py")  # absolute, validated
```

Roots are auto-populated when projects are discovered. Override or add roots
with `python -m perun project-roots --add <name> <path>`.

## Core rules for working here

* **CODEMAP-first.** `docs/CODEMAP.md` is the navigation source of truth. Read it
  (and the relevant `docs/TECHNICAL_DOCUMENTATION.md` section) before grepping.
  Use `perun_ast_grep` for structural code search and `perun_grep` (ripgrep) only
  to confirm.
* **Keep the layers separate.** `registry.py`/`db.py` = persistence;
  `memory.py` = facts + docs; `context.py` = keyed reference docs;
  `secret_store.py` = encrypted named secrets; `orchestrator.py` =
  process/streaming; `server.py` = HTTP only (no business logic); `web/` =
  presentation only.
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
  and never start a second poller. `serve --bridge`, `perun bridge`, the optional
  `PerunBridge` logon task, and `Perun.cmd` must all defer to that lock.
* **The bridge task is opt-in and installer-owned.** `PerunBridge` is created
  and repaired by `perun/installer.py` (`bridge_task_xml()` →
  `schtasks /Create /XML /F`) only when `--task` is passed, never by hand and
  never by `Perun.cmd`. Keep the `/F` idempotency and the `IgnoreNew` +
  `ExecutionTimeLimit=PT0S` settings — they are what keep the poller singular
  and long-lived.

## Editing files safely

All text files in this repo are normalized to **LF** line endings (see
`.gitattributes`). The OpenCode `edit` tool requires an exact byte match; edits
against a CRLF working tree often fail with:

    Could not find oldString in <file>. It must match exactly...

Before a tricky edit, re-read the exact lines. If you suspect CRLF in the
working tree, run:

```bash
python tools/normalize_line_endings.py
```

It rewrites tracked text files to LF while leaving Windows scripts (`.cmd`,
`.ps1`, `.bat`, `.iss`) as CRLF.

## Versioning

There is a single source of truth for history: `docs/CHANGELOG.md`. When you
make a notable change, append an entry there and update the "Latest release"
line in `docs/TECHNICAL_DOCUMENTATION.md`.
