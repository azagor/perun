# Perun — Changelog

All notable changes are recorded here. Single source of truth for release notes.

## v0.2.1 — 2026-10-14

Security hardening for the dashboard API (from a post-0.2.0 code/UI audit) plus a
version-source cleanup.

### Security

- **Cross-site request forgery + DNS rebinding guard** (`perun/server.py`
  `_origin_guard` middleware, `_hostname()`, `_host_allowed()`, `_origin_ok()`).
  The dashboard is a local, unauthenticated API that can start agent runs
  (`POST /api/runs`, executed with `--auto`), so a remote web page must not be
  able to drive it:
  - every `/api/*` request must carry a `Host` header that addresses this machine
    (loopback always; the configured bind address when set) — this blocks **DNS
    rebinding**, where an attacker domain resolves to `127.0.0.1` and the browser
    then treats its page as same-origin;
  - when a browser sends an `Origin`, it must resolve to the same host (or
    loopback) — this blocks **classic CSRF**, including the CORS-safelisted
    `text/plain` request shape.
  A deliberately exposed bind (a concrete LAN address or `0.0.0.0`) still works,
  but `serve()` now prints a prominent warning that the dashboard has no
  authentication.
- **Stored-XSS fixes in the dashboard** (`web/js/perun.js`). Manifest
  **warnings** (derived from repository-controlled manifest content) and
  **session titles** are now HTML-escaped before rendering, and manifest
  **links** pass through a new `safeUrl()` scheme allow-list
  (`http(s)`/`mailto`/relative only). A crafted `perun.md` (`javascript:` link, or
  HTML in an anchor label) can no longer execute script in the dashboard origin —
  which, on a localhost server, would otherwise chain into the fs/git/run APIs.

### Changed

- The reported version now comes from `perun.__version__` (the `/api/health`
  endpoint and the FastAPI app metadata) instead of two hardcoded strings;
  `pyproject.toml` was corrected from a stale `0.1.0` to `0.2.1`.

### Tests

- `tests/test_server_security.py` (10 tests): loopback/`localhost` hosts allowed;
  foreign `Host` and DNS-rebinding `POST` blocked; cross-origin and `null`
  `Origin` blocked; same-origin `POST` allowed; static routes unguarded; wildcard
  bind allows a LAN host but still blocks cross-origin. Suite: **118 passed**.

## v0.2.0 — 2026-10-14

Project management becomes first-class: a creation wizard, per-project Git, custom
project instructions, a self-describing project manifest, and a model picker.

### Added

- **Project creation wizard** (`web/js/perun.js: projectWizard`, `perun/server.py`
  `POST /api/projects`). A four-step flow (Method → Location → Details → Review)
  that can **register an existing folder**, **create a new one** (optionally
  bootstrapping starter files), or **clone a Git remote** into a target path. It
  captures name, kind, description, tags, a per-project default model/agent, and
  custom instructions, can initialise a repository and configure a remote/branch,
  and can index docs immediately. Includes a server-backed **folder browser**
  (`GET /api/fs/list`, `POST /api/fs/mkdir`) and a delete action on every project
  card/panel, gated by a confirmation dialog.
- **Per-project Git integration** (`perun/git.py`, `perun/server.py`). A safe
  wrapper over the `git` CLI (no interactive prompts, explicit args, validated
  remote URLs) exposing `status`, `log`, `init`, `set_remote`, `commit`, `push`,
  and `clone`. Endpoints: `GET /api/projects/{slug}/git`,
  `GET /api/projects/{slug}/git/log`, `POST …/git/init|remote|commit|push`; a
  **Git tab** in the project detail view drives them. Remote URLs are validated
  and cloning/initialisation are explicit.
- **Custom project instructions** (`projects.instructions`, migration in
  `perun/db.py`). Project-scoped rules stored per project and injected by
  `orchestrator.compose_prompt()` into **new** sessions only (continuations
  inherit them from session history), wrapped in
  `<perun:project_instructions>…</perun:project_instructions>`. Editable via
  `GET/PUT /api/projects/{slug}/instructions`, the project **Instructions** tab,
  the wizard, and the MCP tools `perun_get_instructions` / `perun_set_instructions`.
- **Project manifest** (`perun/manifest.py`) — `perun.md`, a Markdown file whose
  **YAML front-matter** describes the project and whose body is free-form notes.
  Structured fields: `version`, `name`, `kind`, `description`, `tags`, `model`,
  `agent`, `instructions`, `docs`, `commands`, `links`, `git`, and typed
  **`anchors`** (`file`, `doc`, `symbol`, `url`, `command`). A plain `perun.json`
  is also accepted (and preserved on write-back). Design rules: the manifest is
  **declarative input** (read on discovery/index/wizard) and never silently
  rewritten by the server — disk writes happen only through explicit
  export/save; unknown keys are preserved (forward-compatible); malformed or
  missing manifests degrade gracefully to discovery heuristics with warnings;
  paths are expected relative to the project root. Merged into the registry
  (`manifest`, `manifest_path`, `manifest_loaded_at` columns) with endpoints
  `GET /api/projects/{slug}/manifest`, `POST …/manifest/refresh|export`,
  `PUT …/manifest`, and MCP tools `perun_get_manifest` / `perun_set_manifest`.
  Scaffolded projects ship a starter `perun.md`.
- **Manifest UI** (`web/js/perun.js`). A **Manifest** tab with a structured
  editor (identity, docs/commands/links key-value rows, anchors, Markdown body),
  "Reload from disk" and "Generate from registry" actions, and warnings display;
  a **manifest card on the Overview tab** renders anchors, links, docs, commands,
  and a safe Markdown rendering (`renderMarkdown`, escaped-by-default, link
  targets restricted to `http(s)`/`mailto`/relative).
- **Model picker.** `GET /api/models` (TTL-cached `opencode models` listing) feeds
  a `<datalist>` wired into the wizard, project config, console, and settings
  model inputs, so a **different default model per project** is easy to choose.
- **Self-registration.** The Perun repository registers itself on service start
  (`discovery.register_self()`), so Perun is one of its own managed projects.
- **Telegram view** reframed as a single **project-agnostic** bot for every
  project (with `/projects`, `/project <slug>` routing) and a live
  `bridge_running` badge from `/api/telegram/status`.

### Changed

- `GET /api/projects/{slug}` now also returns `manifest_body` and
  `manifest_source` so the Overview tab can render the manifest without a second
  request.
- `perun.manifest.dump()` omits empty defaults (`{}`/`[]`/`""`) so generated
  manifests stay readable.

### Tests

- New/extended suites: `tests/test_manifest.py` (14), `tests/test_git.py`,
  `tests/test_scaffold.py`, plus `tests/test_server.py` instructions/manifest
  endpoint coverage, `tests/test_discovery.py` manifest-merge coverage, and
  `tests/test_orchestrator.py` instructions-injection coverage.

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
- **CLI output is UTF-8 safe on legacy Windows consoles.** `perun recall` (and
  any command that prints retrieved text) crashed with `UnicodeEncodeError` when
  an excerpt contained a character the console code page could not encode — e.g.
  the `→` arrow in Perun's own docs. `main()` now reconfigures stdout/stderr to
  UTF-8 (`errors="replace"`). Regression tests in `tests/test_cli.py`.
