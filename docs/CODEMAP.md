# Perun — CODEMAP

Navigation source of truth. **Start every code lookup here.** Each entry lists
the file and the key identifiers you'll jump to.

---

## Package `perun/`

### `perun/__init__.py`
Package docstring + `__version__` (`"0.6.0"`).

### `perun/secrets.py`
Optional password-based encryption for sensitive config values.
* `encrypt_value()`, `decrypt_value()`, `is_encrypted()`, `prompt_password()`.
* Used by `Settings.telegram()` to transparently decrypt an `enc:<base64>`
  Telegram token when `PERUN_PASSWORD` is available.

### `perun/config.py`
Filesystem layout + persisted settings.
* Constants: `PACKAGE_DIR`, `REPO_ROOT`, `HOME`, `DB_PATH`, `CONFIG_PATH`,
  `LOGS_DIR`, `RUNS_DIR`, `WEB_DIR`, `MCP_SERVER`, `TEMPLATES_DIR`,
  `OPENCODE_CONFIG_DIR`.
* `_resolve_home()`, `_default_roots()`, `is_control_path()` (True when a path is
  the Perun repo root — identifies the control-plane project, so it can never be
  confused with a user project).
* `Settings` (dataclass): `roots host port default_model default_agent
  orchestrator_model orchestrator_agent orchestrator_instructions
  max_concurrent_runs run_timeout ollama_host embed_model embed_dim
  telegram_token telegram_bot_name telegram_allowed_chats auto_discover_on_start
  index_docs_on_discover terminal`.
  * `Settings.load()`, `Settings.save()`, `Settings.to_dict()`,
    `Settings.telegram()` (config first, then `TELEGRAM_BOT_TOKEN` /
    `TELEGRAM_CHAT_ID`), `Settings.telegram_sources()` (which of the two is in
    force: `settings` | `env` | `""`, surfaced by the dashboard).
  * `Settings.terminal_cmd(shell_hint=…)` returns the argv to launch the
    configured/default terminal emulator.
  * `Settings.terminal_sources()` reports where the effective terminal comes from
    (`settings` | `os` | `""`).
* `ensure_dirs()`, `settings()`.

### `perun/db.py`
SQLite schema + connection helpers. Tables: `projects`, `sessions`, `runs`,
`activity`, `memory`, `doc_chunks`, `settings`, `bridge_links`, `tasks`.
* `SCHEMA`, `_ADDED_COLUMNS` (idempotent column migrations:
  `projects.instructions`, `projects.manifest`, `projects.manifest_path`,
  `projects.manifest_loaded_at`, `projects.git_*`), `connect()`, `db()` (context
  manager), `init_db()`, `rows()`, `one()`.

### `perun/registry.py`
Registry CRUD (projects, sessions, runs, tasks, activity, bridge links).
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
* Tasks: `create_task()`, `update_task()`, `get_task()`, `list_tasks()`,
  `delete_task()` (reusable project tasks queued into the TUI prompt chain).
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

### `perun/agents.py`
Discover OpenCode agents available to a project.
* `BUILTIN_AGENTS` (`build`, `plan`, `general`, `explore`)
* `_scan_agent_dir()`, `_parse_frontmatter()`, `_agent_id_from_path()`.
* `list_agents(project_path)` — built-ins + global + project-local agents.

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
Start / stream / cancel headless `opencode run`.
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

### `perun/skills.py`
Discovery of OpenCode agent skills and tools available to a project.
* `BUILTIN_SKILLS` — runtime-provided OpenCode skills (`code-audit`,
  `android-design-guidelines`, `find-skills`) always surfaced by `discover_skills()`.
* `BUILTIN_TOOLS` — documented built-in OpenCode tools.
* `_read_skill_manifest()`, `discover_skills()` — scan global
  ``~/.config/opencode/skills/`` and project-local ``.opencode/skills/`` for
  ``SKILL.md`` files with YAML front matter.
* `_export_names()`, `discover_tools()` — scan ``~/.config/opencode/tools/`` and
  project-local ``.opencode/tools/`` for custom TypeScript/JavaScript tool
  definitions, merging them with the built-in tool set.
* Used by `/api/skills` and `/api/tools`.

### `perun/process_tracker.py`
Lifecycle tracking for externally-launched TUI/terminal sessions.
* `ProcessTracker`: `register()`, `get()`, `is_alive()`, `terminate()`,
  `finalize()`, `_resolve_child_pid()`, `_kill_tree()`, `_watch_loop()`.
* Uses `psutil` to walk process trees and resolve Windows Terminal child
  processes. The frontend injects a `PERUN_RUN_UID` environment marker into the
  shell command so the tracker can find the TUI process even when the launcher has
  already exited.
* `TRACKER` — module-level singleton used by `/api/run-in-terminal`,
  `/api/runs/{uid}/cancel`, and `/api/runs/{uid}/kill`.

### `perun/telegram.py`
Shared Telegram ⇄ Perun bridge.
* Formatting: `format_html()`, `chunk()`, `_esc()`.
* `PidLock` (single-instance; `acquire()`, `release()`, side-effect-free
  `is_held()`, `_default_mutex_name()` namespaces the mutex by `PERUN_HOME`),
  `_pid_alive()`. One poller machine-wide (`getUpdates` allows a single consumer);
  a second start is refused rather than fighting with HTTP 409s.
* `PerunTelegramBridge`: `api()`, `send()` (supports optional `reply_markup`),
  `handle_command()`, `_handle_text_command()`, `_project_keyboard()`,
  `_handle_callback()` (inline-keyboard project selection; falls back to numeric
  ids for slugs that exceed Telegram's 64-byte `callback_data` limit),
  `_current_project()` (single-user-project auto-select, else the control plane),
  `run_message()`, `worker()`, `authorised()`, `run()`. Commands include
  `/orchestrator` (`/perun`, `/overseer`); `/projects` sends an inline keyboard.
* Entry: `run_bridge()`, `_setup_logging()`.

### `perun/server.py`
FastAPI app + SSE + static dashboard.
* Globals: `SETTINGS`, `ORCH`, `app`, `STARTED_AT`, `_SSE_HEADERS`.
* Guard: `_origin_guard` middleware + `_hostname()`, `_host_allowed()`,
  `_origin_ok()` — anti-CSRF / anti-DNS-rebinding, applied to `/api/*` only.
* Helpers: `_project_or_404()`, `_resolve_project()`, `_control_project()`,
  `_control_slug()`, `_ok()`, `_sse()`, `_max_activity_id()`, `_new_activity()`.
* Endpoint groups: health (`/api/health` — includes the `control` slug — and the
  structured `/api/models`), terminals (`/api/terminals`), projects
  (`/api/projects…` incl. the creation wizard, `/api/projects/{slug}/instructions`,
  `/api/projects/{slug}/manifest…`, `/api/projects/{slug}/git…`,
  `/api/projects/{slug}/tasks…`; `DELETE` refuses the control project),
  filesystem (`/api/fs/list`, `/api/fs/mkdir`), discovery (`/api/discover…`), runs
  (`/api/runs…`, `/api/runs/active`, `/api/runs/tui`, `/api/runs/{uid}/finish`,
  `/api/runs/{uid}/kill` — declared before `/api/runs/{uid}` —
  `/api/runs/{uid}/stream`, `/api/stream`; `POST /api/runs` defaults to the
  orchestrator), run-in-terminal (`/api/run-in-terminal`), memory
  (`/api/memory/*`), activity (`/api/activity`), settings (`/api/settings` — GET
  never echoes `telegram_token`), telegram (`/api/telegram/*`: `status` reports
  `token_source`/`chats_source`/`bridge_task`, `bot` verifies a new token with
  `getMe` before saving, `bridge/restart` restarts the logon task, `send`).
* `_register_project()`, `_sync_manifest()`, `serve()` (uvicorn launcher).

### `perun/__main__.py`
CLI. `cmd_serve`, `cmd_bridge`, `cmd_discover`, `cmd_projects`, `cmd_add`,
`cmd_status` (also reports `bridge_task` via `installer.bridge_task_status()`),
`cmd_remember`, `cmd_recall`, `cmd_index`, `cmd_import_legacy`, `cmd_install`
(`--machine` / `--skip-deps` / `--skip-global` / `--skip-task` delegate to
`installer.machine_install()`, `--machine --uninstall` to `installer`),
`cmd_mcp`, `build_parser()`, `main()`.
* `_start_bridge_thread()` with process-lifetime `_BRIDGE_LOCK` / `_BRIDGE_THREAD`
  / `_IN_PROCESS_BRIDGE` — the `serve --bridge` path starts the bridge at most once
  per *machine* and keeps the single-instance mutex alive for the whole process.
  `reload_in_process_bridge()` updates the token of a running in-process bridge
  so token/password changes take effect without closing the dashboard.

### `perun/integrations/install_global.py`
Idempotent global install into `~/.config/opencode`.
* `config_dir()`, `opencode_json()`, `perun_dir()`, `instructions_target()`.
* `_read_json()`, `_write_json()`, `_backup()`, `_python_cmd()`,
  `_relative_to_config()`.
* `install()`, `uninstall()`, `status()`.
* Templates under `perun/integrations/templates/`.

### `perun/installer.py`
Machine-level installation (`python -m perun install --machine`) — the part of
setup that belongs to the *machine* rather than to a user's OpenCode config.
* Constants: `TASK_NAME` (`"PerunBridge"`), `MIN_PYTHON`, `TASK_DESCRIPTION`,
  `is_windows()` (patchable, so the Windows-only paths stay testable anywhere).
* Prerequisites: `python_version_ok()`, `pip_available()`, `which_version()`,
  `ollama_status()`, `check_prerequisites()`.
* Dependencies: `requirements_file()`, `pip_install()`.
* Bridge task: `_python_exe()` (honours `PERUN_PYTHON`), `_task_user()`,
  `bridge_task_xml()`, `_run_schtasks()`, `_decode_console()`,
  `bridge_task_exists()`, `bridge_task_status()`, `register_bridge_task()`,
  `remove_bridge_task()`, `restart_bridge_task()` (how a new Telegram bot takes
  effect: `schtasks /End` then `/Run`, touching only the task's own process).
* Orchestration: `machine_install()`, `uninstall_machine()`.

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
  `router()`, `renderMarkdown()`, `modelSelect()`, `agentSelect()`, `terminalId()`,
  `buildCommand()`, `launchInTerminal()`, the `perunFx` canvas engine (`flash()`),
  presence (`presence`, `computePresence()`, `pipState()`, `applyCardStates()`,
  `refreshPresence()`, `updateStormMeter()`), and the view renderers (`viewDashboard`,
  `viewOrchestrator`, `viewProjects`, `projectWizard`, `browseFolder`, `viewProject`,
  `mountGit`, `mountManifest`, `manifestOverview`, `projectInstructionsForm`,
  `viewConsole`, `viewMemory`, `viewActivity`, `viewTelegram`, `mountTelegram`,
  `viewSettings`), `mountConsole()`, `streamRun()`, `streamOrchestrator()`.
  Project cards (`projectCard()`) render `🖵 OpenCode` (primary TUI launch),
  `■ Stop`, `Open`, `Index docs`, and delete actions for every project; the
  orchestrator chat lives only in the sidebar (`Orchestrator Chat`).
* `web/assets/Cinzel.ttf` — heading font (shared with the company site).
* `web/assets/perun.ico` / `perun.png` — gold-bolt app icon (favicon + Windows
  shortcut), generated by `tools/make_icon.py`.

---

## Root files

* `bootstrap.ps1` — Windows cold-start installer for a new machine: locates
  Python 3.10+ (honours `PERUN_PYTHON`) and delegates to
  `python -m perun install --machine`; optionally creates the desktop shortcut.
* `Perun.cmd` — Windows one-click launcher (idempotent dashboard start; never
  starts a second Telegram bridge).
* `run.py` — launcher (`python run.py serve`).
* `pyproject.toml` — packaging + `perun` console script + pytest config.
* `tools/make_icon.py` — regenerates `web/assets/perun.ico` / `perun.png`.
* `tools/migrate_global_facts.py` — one-off helper that moves Perun-related
  global facts onto the `perun` control-plane project.
* `opencode.json` — repo-local OpenCode config (perun MCP + AGENTS.md).
* `requirements.txt`, `.gitignore`, `README.md`, `AGENTS.md`.

---

## Tests `tests/`

* `test_db_registry.py` — schema, projects/sessions/runs/activity CRUD.
* `test_discovery.py` — detection, classify, register, scan, manifest merge.
* `test_memory.py` — remember/sensitive/dedupe, chunking, index, search,
  unified_search, stats (Ollama-independent).
* `test_install_global.py` — merge/uninstall idempotency against a temp config.
* `test_mcp_memory.py` — MCP `perun_memory_store` resolves to project scope when
  called inside a project and to global scope when no project context is given.
* `test_migrate_global_facts.py` — the one-off `tools/migrate_global_facts.py`
  migration is non-destructive by default and only deletes duplicates when
  explicitly requested.
* `test_installer.py` — prerequisites, `pip`, the `PerunBridge` task XML and the
  `schtasks` invocation (faked), `machine_install` orchestration, the CLI flags,
  the `bootstrap.ps1` delegation contract, and `restart_bridge_task()`.
* `test_server.py` — FastAPI TestClient smoke of the main endpoints (incl.
  instructions, manifest, git, wizard, models).
* `test_telegram_bot_api.py` — the dashboard's "switch to a new bot" flow:
  `/api/settings` never echoes the token, `/api/telegram/bot` verifies with a
  mocked `getMe` before saving, clears on `token: ""`, and `token_source`
  resolves config-over-env (network mocked — never contacts Telegram).
* `test_manifest.py` — parse/normalize/dump/save, JSON fallback, edge cases.
* `test_scaffold.py` — directory creation, bootstrap, starter `perun.md`.
* `test_git.py` — the `git` CLI wrapper against a temp repository.
* `test_orchestrator.py` — `parse_event`, `resolve_opencode`, `EventBus`,
  `compose_prompt` instructions injection, end-to-end stdout-error capture.
* `test_process_tracker.py` — `ProcessTracker` registration, lifecycle, marker
  resolution, watcher auto-finalization, and run-record cleanup.
* `test_agents.py` — OpenCode agent discovery (`perun/agents.py`).
* `test_skills.py` — OpenCode skill/tool discovery (`perun/skills.py`).
* `test_telegram_lock.py` — mutex namespacing, side-effect-free `is_held()`, and
  the second-acquire-is-refused guarantee (the bridge can never multiply).
* `test_telegram_routing.py` — `_current_project` routing (single-project
  auto-select, control-plane fallback, `/orchestrator`).
* `test_control.py` — control plane: `ensure()` / `is_control()` / `resolve()`,
  the delete guard, and "a run with no project targets the orchestrator".
* `test_server_security.py` — the origin/host guard (`_origin_guard`) on `/api/*`.
* `test_cli.py` — UTF-8 stdio hardening and non-ASCII recall output.
