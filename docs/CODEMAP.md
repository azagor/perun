# Perun — CODEMAP

Navigation source of truth. **Start every code lookup here.** Each entry lists
the file and the key identifiers you'll jump to.

---

## Package `perun/`

### `perun/__init__.py`
Package docstring + `__version__` (`"0.3.0"`).

### `perun/config.py`
Filesystem layout + persisted settings.
* Constants: `PACKAGE_DIR`, `REPO_ROOT`, `HOME`, `DB_PATH`, `CONFIG_PATH`,
  `LOGS_DIR`, `RUNS_DIR`, `WEB_DIR`, `MCP_SERVER`, `TEMPLATES_DIR`.
* `_resolve_home()`, `_default_roots()`, `is_control_path()` (True when a path is
  the Perun repo root — identifies the control-plane project, so it can never be
  confused with a user project).
* `Settings` (dataclass): `roots host port default_model default_agent
  orchestrator_model orchestrator_agent orchestrator_instructions
  max_concurrent_runs run_timeout ollama_host embed_model embed_dim
  telegram_token telegram_allowed_chats auto_discover_on_start
  index_docs_on_discover`.
  * `Settings.load()`, `Settings.save()`, `Settings.to_dict()`,
    `Settings.telegram()`.
* `ensure_dirs()`, `settings()`.

### `perun/db.py`
SQLite schema + connection helpers. Tables: `projects`, `sessions`, `runs`,
`activity`, `memory`, `doc_chunks`, `settings`, `bridge_links`.
* `SCHEMA`, `_ADDED_COLUMNS` (idempotent column migrations:
  `projects.instructions`, `projects.manifest`, `projects.manifest_path`,
  `projects.manifest_loaded_at`, `projects.git_*`), `connect()`, `db()` (context
  manager), `init_db()`, `rows()`, `one()`.

### `perun/registry.py`
Registry CRUD (projects, sessions, runs, activity, bridge links).
* Helpers: `now_iso()`, `slugify()`, `project_dict()` (parses the stored
  manifest snapshot, adds `has_manifest` and `is_control` — set from
  `config.is_control_path()`).
* Projects: `add_project()`, `update_project()`, `touch_project()`,
  `get_project()`, `get_project_by_path()`, `list_projects()`, `delete_project()`,
  `set_project_manifest()`, `get_project_manifest()`.
* Sessions: `create_session()`, `get_session()`, `find_session_by_opencode()`,
  `update_session()`, `list_sessions()`, `delete_session()`.
* Runs: `create_run()`, `update_run()`, `get_run()`, `list_runs()`,
  `list_active_runs()` (queued/running runs joined with their project — the data
  behind the dashboard storm layer).
* Activity: `log_activity()`, `list_activity()`.
* Bridge: `get_bridge_link()`, `set_bridge_link()`.

### `perun/control.py`
The **control plane** — Perun as an always-present, undeletable orchestrator
project, identified by `config.is_control_path()` rather than a reserved slug.
* `ORCHESTRATOR_INSTRUCTIONS` (the default orchestrator persona), `is_control()`,
  `ensure()` (idempotent; called on server lifespan, Telegram `/projects`, and
  MCP import — re-creates the row if the registry is wiped), `resolve()`.

### `perun/projects.py`
Shared project creation/registration, used by **both** the HTTP wizard
(`POST /api/projects`) and the MCP tools.
* `ProjectError`, `name_from_repo()`, `sync_manifest()`, `register_or_update()`,
  `create()` (the `existing` / `new` / `clone` methods + optional git/index steps).

### `perun/embeddings.py`
Ollama transport + vector math (project-agnostic).
* `DOC_PREFIX`, `QUERY_PREFIX`, `MAX_CHARS_PER_CHUNK`, `IDENT_RE`, `FILE_RE`.
* `EmbeddingError`, `cosine_similarity()`, `pack_vector()`, `unpack_vector()`,
  `embed_texts()`, `embed_one()`, `extract_metadata()`.

### `perun/memory.py`
Unified facts + document vector store + search.
* `content_hash()`, `is_sensitive()`, `_freshness_weight()`, `_keywords()`,
  `_embed()`.
* Facts: `remember()`, `search_facts()`, `_fact_rows()`, `recent_facts()`,
  `prune()`.
* Docs: `chunk_markdown()`, `index_text()`, `index_project_docs()`,
  `search_docs()`.
* Unified: `unified_search()`, `stats()`.
* Migration: `import_legacy_memory()`.
* `DOC_CANDIDATES` (files indexed per project).

### `perun/discovery.py`
Find & register OpenCode projects under roots.
* `SKIP_DIRS`, `OPENCODE_MARKERS`, `DOC_MARKERS`.
* `capabilities()`, `classify()`, `detect()`, `_candidate_dirs()`, `scan()`,
  `register()` (merges any `perun.md`/`perun.json` manifest),
  `discover_and_sync()`, `register_self()`.

### `perun/scaffold.py`
Starter-project skeleton + directory creation.
* `ScaffoldError`, `create_dir()`, `bootstrap()` (writes `opencode.json`,
  `AGENTS.md`, `README.md`, `.gitignore`, and a starter `perun.md`),
  `_manifest_md()`, `summary()`.

### `perun/git.py`
Safe wrapper over the `git` CLI (per-project repositories).
* `GitError`, `available()`, `is_repo()`, `status()`, `log()`, `info()`,
  `init()`, `set_remote()`, `commit()`, `push()`, `clone()`.
* All calls run non-interactively with explicit args; remote URLs are validated.

### `perun/manifest.py`
Self-describing project manifest — `perun.md` (YAML front matter + Markdown
body) with a `perun.json` fallback.
* Constants: `MANIFEST_MD`, `MANIFEST_JSON`, `MANIFEST_NAMES`, `SCHEMA_VERSION`,
  `ANCHOR_KINDS`.
* `ManifestError`, `find()`, `parse()`, `parse_json()`, `load()`, `normalize()`,
  `dump()`, `save()`, `snapshot()`, `to_project_fields()`, `from_project()`,
  `default_body()`.

### `perun/orchestrator.py`
Start / stream / cancel `opencode run`.
* `EventBus` (`subscribe`, `unsubscribe`, `publish`, `subscribe_all`,
  `unsubscribe_all`, `close`).
* Process helpers: `resolve_opencode()`, `terminate_tree()`, `parse_event()`,
  `compose_prompt(project, prompt, settings=…)` (prepends project instructions to a
  new session; for the control project it injects
  `Settings.orchestrator_instructions` instead).
* `Orchestrator`: `start_run()`, `run_and_wait()`, `cancel()`, `is_final()`,
  `active()`, `_worker()`, `_execute()`, `_finalize()`, `stream()`. Model/agent
  precedence: explicit arg → project override → (control only)
  `orchestrator_model`/`orchestrator_agent` → global `default_model`/`default_agent`.
* Constants: `MAX_STORED_OUTPUT`, `HISTORY_LIMIT`, `INSTRUCTION_OPEN`,
  `INSTRUCTION_CLOSE`.

### `perun/telegram.py`
Shared Telegram ⇄ Perun bridge.
* Formatting: `format_html()`, `chunk()`, `_esc()`.
* `PidLock` (single-instance; `acquire()`, `release()`, side-effect-free
  `is_held()`, `_default_mutex_name()` namespaces the mutex by `PERUN_HOME`),
  `_pid_alive()`. One poller machine-wide (`getUpdates` allows a single consumer);
  a second start is refused rather than fighting with HTTP 409s.
* `PerunTelegramBridge`: `api()`, `send()`, `handle_command()`,
  `_current_project()` (single-user-project auto-select, else the control plane),
  `run_message()`, `worker()`, `authorised()`, `run()`. Commands include
  `/orchestrator` (`/perun`, `/overseer`).
* Entry: `run_bridge()`, `_setup_logging()`.

### `perun/server.py`
FastAPI app + SSE + static dashboard.
* Globals: `SETTINGS`, `ORCH`, `app`, `STARTED_AT`, `_SSE_HEADERS`.
* Guard: `_origin_guard` middleware + `_hostname()`, `_host_allowed()`,
  `_origin_ok()` — anti-CSRF / anti-DNS-rebinding, applied to `/api/*` only.
* Helpers: `_project_or_404()`, `_resolve_project()`, `_control_project()`,
  `_control_slug()`, `_ok()`, `_sse()`, `_max_activity_id()`, `_new_activity()`.
* Endpoint groups: health (`/api/health` — includes the `control` slug — and the
  structured `/api/models`), projects (`/api/projects…` incl. the creation wizard,
  `/api/projects/{slug}/instructions`, `/api/projects/{slug}/manifest…`,
  `/api/projects/{slug}/git…`; `DELETE` refuses the control project), filesystem
  (`/api/fs/list`, `/api/fs/mkdir`), discovery (`/api/discover…`), runs
  (`/api/runs…`, `/api/runs/active` — declared before `/api/runs/{id}` —
  `/api/runs/{id}/stream`, `/api/stream`; `POST /api/runs` defaults to the
  orchestrator), memory (`/api/memory/*`), activity (`/api/activity`), settings
  (`/api/settings`), telegram (`/api/telegram/*`).
* `_register_project()`, `_sync_manifest()`, `serve()` (uvicorn launcher).

### `perun/__main__.py`
CLI. `cmd_serve`, `cmd_bridge`, `cmd_discover`, `cmd_projects`, `cmd_add`,
`cmd_status`, `cmd_remember`, `cmd_recall`, `cmd_index`, `cmd_import_legacy`,
`cmd_install`, `cmd_mcp`, `build_parser()`, `main()`.
* `_start_bridge_thread()` with process-lifetime `_BRIDGE_LOCK` / `_BRIDGE_THREAD`
  — the `serve --bridge` path starts the bridge at most once per process and keeps
  the single-instance mutex alive for the whole process.

### `perun/integrations/install_global.py`
Idempotent global install into `~/.config/opencode`.
* `config_dir()`, `opencode_json()`, `perun_dir()`, `instructions_target()`.
* `_read_json()`, `_write_json()`, `_backup()`, `_python_cmd()`,
  `_relative_to_config()`.
* `install()`, `uninstall()`, `status()`.
* Templates under `perun/integrations/templates/`.

---

## MCP server

### `mcp/perun_mcp.py`
stdio MCP server (`perun`). 17 tools: `perun_list_projects`,
`perun_project_info`, `perun_create_project`, `perun_update_project`,
`perun_delete_project`, `perun_discover`, `perun_memory_search`,
`perun_memory_store`, `perun_docs_index`, `perun_get_instructions`,
`perun_set_instructions`, `perun_get_manifest`, `perun_set_manifest`,
`perun_start_session`, `perun_ask`, `perun_run_status`, `perun_telegram_send`.
* `_resolve_project()`, `_project_line()`, `main()`; calls `control.ensure()` at
  import time so the orchestrator always exists.

---

## Web dashboard `web/`

* `web/index.html` — app shell (sidebar, topbar, storm meter, `#perun-fx` FX
  canvas, `#perun-sigil` brand mark, view container, modal/toast roots, hidden
  `#model-root` datalist host).
* `web/css/perun.css` — design tokens & components (gold/dark theme), including
  wizard, folder-browser, structured-editor, manifest, `modelSelect`, chat, and
  the storm/FX styles (sigil eyes, storm meter, `.is-awake` / `.is-working`,
  `.card-pip`, `.perun-flash`), with a `prefers-reduced-motion` guard.
* `web/js/perun.js` — SPA: `api()`, `toast()`, `modal()`, `confirmDialog()`,
  `router()`, `renderMarkdown()`, `modelSelect()`, the `perunFx` canvas engine
  (`flash()`), presence (`presence`, `computePresence()`, `pipState()`,
  `applyCardStates()`, `refreshPresence()`, `updateStormMeter()`), and the view
  renderers (`viewDashboard`, `viewOrchestrator`, `viewProjects`, `projectWizard`,
  `browseFolder`, `viewProject`, `mountGit`, `mountManifest`, `manifestOverview`,
  `projectInstructionsForm`, `viewConsole`, `viewMemory`, `viewActivity`,
  `viewTelegram`, `viewSettings`), `mountConsole()`, `streamRun()`,
  `streamOrchestrator()`.
* `web/assets/Cinzel.ttf` — heading font (shared with the company site).
* `web/assets/perun.ico` / `perun.png` — gold-bolt app icon (favicon + Windows
  shortcut), generated by `tools/make_icon.py`.

---

## Root files

* `Perun.cmd` — Windows one-click launcher (idempotent dashboard start; never
  starts a second Telegram bridge).
* `run.py` — launcher (`python run.py serve`).
* `pyproject.toml` — packaging + `perun` console script + pytest config.
* `tools/make_icon.py` — regenerates `web/assets/perun.ico` / `perun.png`.
* `opencode.json` — repo-local OpenCode config (perun MCP + AGENTS.md).
* `requirements.txt`, `.gitignore`, `README.md`, `AGENTS.md`.

---

## Tests `tests/`

* `test_db_registry.py` — schema, projects/sessions/runs/activity CRUD.
* `test_discovery.py` — detection, classify, register, scan, manifest merge.
* `test_memory.py` — remember/sensitive/dedupe, chunking, index, search,
  unified_search, stats (Ollama-independent).
* `test_install_global.py` — merge/uninstall idempotency against a temp config.
* `test_server.py` — FastAPI TestClient smoke of the main endpoints (incl.
  instructions, manifest, git, wizard, models).
* `test_manifest.py` — parse/normalize/dump/save, JSON fallback, edge cases.
* `test_scaffold.py` — directory creation, bootstrap, starter `perun.md`.
* `test_git.py` — the `git` CLI wrapper against a temp repository.
* `test_orchestrator.py` — `parse_event`, `resolve_opencode`, `EventBus`,
  `compose_prompt` instructions injection, end-to-end stdout-error capture.
* `test_telegram_lock.py` — mutex namespacing, side-effect-free `is_held()`, and
  the second-acquire-is-refused guarantee (the bridge can never multiply).
* `test_telegram_routing.py` — `_current_project` routing (single-project
  auto-select, control-plane fallback, `/orchestrator`).
* `test_control.py` — control plane: `ensure()` / `is_control()` / `resolve()`,
  the delete guard, and "a run with no project targets the orchestrator".
* `test_server_security.py` — the origin/host guard (`_origin_guard`) on `/api/*`.
* `test_cli.py` — UTF-8 stdio hardening and non-ASCII recall output.
