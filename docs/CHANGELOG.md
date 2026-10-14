# Perun — Changelog

All notable changes are recorded here. Single source of truth for release notes.

## v0.1.0 — 2026-10-14

Initial release. Perun becomes a machine-wide overseer and orchestrator for all
OpenCode projects.

### Added

- **Project registry & discovery** (`perun/registry.py`, `perun/discovery.py`).
  Cheap marker-based classification (`.opencode/`, `opencode.json`, `AGENTS.md`,
  `docs/CODEMAP.md`, `.git/`), depth-bounded scans under configured roots, and
  path-keyed upsert so re-discovery updates rather than duplicates.
- **Unified memory** (`perun/memory.py`, `perun/embeddings.py`). Facts
  (global/project scoped, sensitive-content guarded, deduped, freshness
  weighted) plus a Markdown doc-chunk vector store with CODEMAP-style path and
  identifier extraction. Ollama `nomic-embed-text` embeddings with graceful
  keyword fallback when Ollama is down. Legacy `~/.config/opencode/memory.db`
  import.
- **Agent orchestrator** (`perun/orchestrator.py`). Starts
  `opencode run --format json --auto` with `cwd` set to the target project,
  streams normalized events (text/tool/session/error/end) over an in-process
  `EventBus`, persists runs, logs raw output to `~/.perun/runs/`, and cancels by
  killing the process tree. Semaphore-bounded concurrency.
- **HTTP service + dashboard** (`perun/server.py`, `web/`). REST API, SSE run
  streaming and a global activity stream, and a gold/dark single-page dashboard
  styled after peruninthemachine.com (Dashboard, Projects + detail tabs, Agent
  Console with live output, Memory Explorer, Activity, Telegram, Settings).
- **Shared Telegram bridge** (`perun/telegram.py`). One long-polling process,
  per-chat project/session routing persisted in the DB, Markdown→Telegram-HTML
  formatting, chunking, typing indicator, `/projects /project /new /status
  /stop /remember /recall /index`, single-instance `PidLock`.
- **Global MCP server** (`mcp/perun_mcp.py`) exposing nine `perun_*` tools, and
  an idempotent, reversible global installer
  (`perun/integrations/install_global.py`) that merges `mcp.perun`, global
  instructions, and helper commands into `~/.config/opencode` with a backup.
- **CLI** (`perun/__main__.py`): `serve`, `bridge`, `discover`, `projects`,
  `add`, `status`, `remember`, `recall`, `index`, `import-legacy-memory`,
  `install`/`--uninstall`, `mcp`.
- Documentation (`README.md`, `AGENTS.md`, `docs/CODEMAP.md`,
  `docs/TECHNICAL_DOCUMENTATION.md`, `docs/DESIGN.md`) and a pytest suite.

### Fixed

- **Bridge status no longer collides with the trading_bot bridge.** The
  Telegram single-instance lock used the bare Windows mutex name
  `PerunTelegramBridge`, which is the *same* global name used by
  `trading_bot/scripts/telegram_bridge.py`. Whenever that unrelated bridge was
  running, `perun status` falsely reported `bridge_running: true` (and the
  `~/.perun/telegram.pid` file was absent). The mutex name is now namespaced by
  `PERUN_HOME` (`PerunManagedBridge-<hash>`), and a new side-effect-free
  `PidLock.is_held()` probe drives `perun status` instead of acquire/release
  (which briefly created and deleted the pid file). Regression tests in
  `tests/test_telegram_lock.py`.
- **Provider errors are persisted on the run record.** When `opencode` reports a
  failure only as a stdout `error` event (e.g. a model rejected for the
  account's region), that message was streamed live but never saved, so a
  finished run showed an empty error. `Orchestrator._execute` now collects error
  events and stores the joined text when there is no reply, so the dashboard can
  explain a past failure. Regression test in `tests/test_orchestrator.py`.
