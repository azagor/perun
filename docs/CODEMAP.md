# Perun — CODEMAP

Navigation source of truth. **Start every code lookup here.** Each entry lists
the file and the key identifiers you'll jump to.

---

## Package `perun/`

### `perun/__init__.py`
Package docstring + `__version__` (`"0.1.0"`).

### `perun/config.py`
Filesystem layout + persisted settings.
* Constants: `PACKAGE_DIR`, `REPO_ROOT`, `HOME`, `DB_PATH`, `CONFIG_PATH`,
  `LOGS_DIR`, `RUNS_DIR`, `WEB_DIR`, `MCP_SERVER`, `TEMPLATES_DIR`.
* `_resolve_home()`, `_default_roots()`.
* `Settings` (dataclass): `roots host port default_model default_agent
  max_concurrent_runs run_timeout ollama_host embed_model embed_dim
  telegram_token telegram_allowed_chats auto_discover_on_start
  index_docs_on_discover`.
  * `Settings.load()`, `Settings.save()`, `Settings.to_dict()`,
    `Settings.telegram()`.
* `ensure_dirs()`, `settings()`.

### `perun/db.py`
SQLite schema + connection helpers. Tables: `projects`, `sessions`, `runs`,
`activity`, `memory`, `doc_chunks`, `settings`, `bridge_links`.
* `SCHEMA`, `connect()`, `db()` (context manager), `init_db()`, `rows()`, `one()`.

### `perun/registry.py`
Registry CRUD (projects, sessions, runs, activity, bridge links).
* Helpers: `now_iso()`, `slugify()`, `project_dict()`.
* Projects: `add_project()`, `update_project()`, `touch_project()`,
  `get_project()`, `get_project_by_path()`, `list_projects()`, `delete_project()`.
* Sessions: `create_session()`, `get_session()`, `find_session_by_opencode()`,
  `update_session()`, `list_sessions()`, `delete_session()`.
* Runs: `create_run()`, `update_run()`, `get_run()`, `list_runs()`.
* Activity: `log_activity()`, `list_activity()`.
* Bridge: `get_bridge_link()`, `set_bridge_link()`.

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
  `register()`, `discover_and_sync()`.

### `perun/orchestrator.py`
Start / stream / cancel `opencode run`.
* `EventBus` (`subscribe`, `unsubscribe`, `publish`, `subscribe_all`,
  `unsubscribe_all`, `close`).
* Process helpers: `resolve_opencode()`, `terminate_tree()`, `parse_event()`.
* `Orchestrator`: `start_run()`, `run_and_wait()`, `cancel()`, `is_final()`,
  `active()`, `_worker()`, `_execute()`, `_finalize()`, `stream()`.
* Constants: `MAX_STORED_OUTPUT`, `HISTORY_LIMIT`.

### `perun/telegram.py`
Shared Telegram ⇄ Perun bridge.
* Formatting: `format_html()`, `chunk()`, `_esc()`.
* `PidLock` (single-instance; `acquire()`, `release()`, side-effect-free
  `is_held()`, `_default_mutex_name()` namespaces the mutex by `PERUN_HOME`), `_pid_alive()`.
* `PerunTelegramBridge`: `api()`, `send()`, `handle_command()`,
  `run_message()`, `worker()`, `authorised()`, `run()`.
* Entry: `run_bridge()`, `_setup_logging()`.

### `perun/server.py`
FastAPI app + SSE + static dashboard.
* Globals: `SETTINGS`, `ORCH`, `app`, `STARTED_AT`, `_SSE_HEADERS`.
* Helpers: `_project_or_404()`, `_resolve_project()`, `_ok()`, `_sse()`,
  `_max_activity_id()`, `_new_activity()`.
* Endpoint groups: health (`/api/health`, `/api/models`), projects
  (`/api/projects…`), discovery (`/api/discover…`), runs (`/api/runs…`,
  `/api/runs/{id}/stream`, `/api/stream`), memory (`/api/memory/*`), activity
  (`/api/activity`), settings (`/api/settings`), telegram
  (`/api/telegram/*`).
* `serve()` (uvicorn launcher).

### `perun/__main__.py`
CLI. `cmd_serve`, `cmd_bridge`, `cmd_discover`, `cmd_projects`, `cmd_add`,
`cmd_status`, `cmd_remember`, `cmd_recall`, `cmd_index`, `cmd_import_legacy`,
`cmd_install`, `cmd_mcp`, `build_parser()`, `main()`.

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
stdio MCP server (`perun`). Tools: `perun_list_projects`, `perun_project_info`,
`perun_memory_search`, `perun_memory_store`, `perun_docs_index`,
`perun_start_session`, `perun_ask`, `perun_run_status`, `perun_telegram_send`.
* `_resolve_project()`, `_project_line()`, `main()`.

---

## Web dashboard `web/`

* `web/index.html` — app shell (sidebar, topbar, view container, modal/toast roots).
* `web/css/perun.css` — design tokens & components (gold/dark theme).
* `web/js/perun.js` — SPA: `api()`, `toast()`, `modal()`, `router()`, and the
  view renderers (`viewDashboard`, `viewProjects`, `viewProject`, `viewConsole`,
  `viewMemory`, `viewActivity`, `viewTelegram`, `viewSettings`), `mountConsole()`,
  `streamRun()`.
* `web/assets/Cinzel.ttf` — heading font (shared with the company site).

---

## Root files

* `run.py` — launcher (`python run.py serve`).
* `pyproject.toml` — packaging + `perun` console script + pytest config.
* `opencode.json` — repo-local OpenCode config (perun MCP + AGENTS.md).
* `requirements.txt`, `.gitignore`, `README.md`, `AGENTS.md`.

---

## Tests `tests/`

* `test_db_registry.py` — schema, projects/sessions/runs/activity CRUD.
* `test_discovery.py` — detection, classify, register, scan.
* `test_memory.py` — remember/sensitive/dedupe, chunking, index, search,
  unified_search, stats (Ollama-independent).
* `test_install_global.py` — merge/uninstall idempotency against a temp config.
* `test_server.py` — FastAPI TestClient smoke of the main endpoints.
* `test_orchestrator.py` — `parse_event`, `resolve_opencode`, `EventBus`,
  end-to-end stdout-error capture.
* `test_telegram_lock.py` — mutex namespacing + side-effect-free `is_held()`.
* `test_cli.py` — UTF-8 stdio hardening and non-ASCII recall output.
