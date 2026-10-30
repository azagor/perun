# Perun — CODEMAP

Navigation source of truth. **Start every code lookup here.** Each entry lists
the file and the key identifiers you'll jump to.

---

## Package `perun/`

### `perun/__init__.py`
Package docstring + `__version__` (`"0.8.59"`).

### `perun/secrets.py`
Optional password-based encryption for sensitive config values.
* `SecretError` — the module's public exception.
* `encrypt_value()`, `decrypt_value()`, `is_encrypted()`, `prompt_password()`,
  `persist_password()` (DPAPI-encrypts the password into `<PERUN_HOME>/secret`
  on Windows) and `_password()` (env var first, then that file).
* Used by `Settings.telegram()` to transparently decrypt an `enc:<base64>`
  Telegram token when `PERUN_PASSWORD` is available.
* Used by `perun/secret_store.py` for every stored secret value.

### `perun/secret_store.py`
Named, encrypted-at-rest values that agents pull on demand — the sanctioned
home for credentials that used to live in `AGENTS.md`.
* `normalize_name()`, `_scope_filter()`, `_resolve_row()`.
* Writing: `set_secret()` (encrypts before it writes; refuses without a
  password), `delete_secret()` (deletes only the row a lookup resolves).
* Reading: `list_secrets()` (**metadata only — never values**), `get_secret()`
  (project scope wins over global), `unlock_status()`.
* Storage: the `secret_store` table (see `perun/db.py`).

### `perun/config.py`
Filesystem layout + persisted settings.
* Constants: `PACKAGE_DIR`, `REPO_ROOT`, `HOME`, `DB_PATH`, `CONFIG_PATH`,
  `API_TOKEN_PATH` (`<PERUN_HOME>/api-token`), `LOGS_DIR`, `RUNS_DIR`, `WEB_DIR`,
  `MCP_SERVER`, `TEMPLATES_DIR`, `OPENCODE_CONFIG_DIR` (honours
  `XDG_CONFIG_HOME`), `INSTRUCTIONS_DIR` (global instruction Markdown),
  `GLOBAL_SKILLS_DIR` / `PROJECT_SKILLS_DIR` (skill scan paths),
  `POSIX_TERMINALS` (known Linux/macOS emulators + launch flags).
* `_resolve_home()`, `_resolve_opencode_config_dir()`, `_default_roots()`,
  `is_control_path()` (True when a path is the Perun repo root — identifies the
  control-plane project, so it can never be confused with a user project),
  `posix_terminal_flags()`, `posix_shell()`.
* `Settings` (dataclass): `roots host port default_model default_agent
  orchestrator_model orchestrator_agent max_concurrent_runs run_timeout
  ollama_host embed_model embed_dim telegram_token telegram_bot_name
  telegram_allowed_chats telegram_pair_code perun_owner_name perun_hitl_secret
  shared_auto_approve auto_discover_on_start index_docs_on_discover terminal
  perunchat_api_key perunchat_api_endpoint perunchat_auto_reply perunchat_auto_join
  perunchat_chat_model perunchat_chat_agent perunchat_trusted`.
  * `Settings.load()` (quarantines an unparseable Perun config file to
    `.corrupt-<stamp>` before falling back to defaults), `Settings.save()`
    (atomic temp+rename, `0600` before writing), `Settings.to_dict()`,
    `Settings.telegram()` (`PERUN_TELEGRAM_TOKEN` orchestrator-injected token
    first, then config, then `TELEGRAM_BOT_TOKEN` / `TG_BOT_TOKEN`;
    `TELEGRAM_CHAT_ID` / `TG_CHAT_ID` chat fallback), `Settings.telegram_sources()`
    (which source is in force: `orchestrator` | `settings` | `settings-encrypted` |
    `env` | `""`, surfaced by the dashboard).
  * `Settings.terminal_cmd(shell_hint=…)` returns the argv to launch the
    configured/default terminal emulator (on POSIX it prefers a real emulator and
    returns an argv ending in the shell).
  * `Settings.terminal_sources()` reports where the effective terminal comes from
    (`settings` | `os` | `""`).
  * `Settings.perunchat()` resolves the PerunChat API key/endpoint (propagates an
    encrypted `perunchat_api_key` through `secrets.decrypt_value()`), and
    `Settings.perunchat_sources()` reports which source is in force.
* `ensure_dirs()`, `_quarantine_config()`, `api_token()` (create/read the local
  API token, `O_CREAT|O_EXCL` + `0600`, `PERUN_API_TOKEN` override), `settings()`.

### `perun/daemon.py`
Server lifecycle helpers: PID file, running-check, graceful stop, restart.
* `PID_PATH`, `RUN_RECORD_PATH`, `write_pid()`, `read_pid()`, `clear_pid()`,
  `record_run()`, `read_recorded_run()`, `read_recorded_argv()`, `is_running()`,
  `is_another_perun_running()`, `stop()`, `kill()`, `start_detached()`,
  `write_pid_for()` (entry point for detached starters).
* Used by ``perun serve`` to avoid double-starts and record its launch flags;
  used by ``perun stop`` / ``perun restart`` for graceful replacement, with
  ``perun restart`` replaying the recorded flags.

### `perun/db.py`
SQLite schema + connection helpers. Tables: `projects`, `sessions`, `runs`,
`activity`, `memory`, `doc_chunks`, `settings`, `bridge_links`, `tasks`,
`context_docs` (the Context layer — keyed reference docs, deliberately **not**
`doc_chunks`, so project-scoped context never leaks into cross-project doc
search), `secret_store` (encrypted secret values).
* `SCHEMA`, `SCHEMA_VERSION` (the `PRAGMA user_version` migration gate),
  `_ADDED_COLUMNS` (idempotent column additions: `projects.manifest`,
  `projects.manifest_path`, `projects.manifest_loaded_at`, `projects.git_*`,
  `runs.model_explicit`, `doc_chunks.embedded_hash`, `projects.agent_models`),
  `_REMOVED_COLUMNS`
  (idempotent column drops:
  `projects.instructions`, `projects.instructions_path`), `connect()`, `db()`
  (context manager), `init_db()`, `rows()`, `one()`.

### `perun/registry.py`
Registry CRUD (projects, sessions, runs, tasks, activity, bridge links) and the
**cross-project dependency graph**.
* Helpers: `now_iso()`, `slugify()`, `project_dict()` (parses the stored
  manifest snapshot, adds `has_manifest` and `is_control` — set from
  `config.is_control_path()`).
* Projects: `add_project()` (loads `perun.md` after insert so **every**
  registration path — CLI `perun add`, wizard — gets manifest data, not just
  discovery; stores `agent_models` as a JSON map), `update_project()`
  (accepts `agent_models`), `touch_project()`,
  `get_project()`, `get_project_by_path()`, `list_projects()`, `delete_project()`,
  `set_project_manifest()`, `get_project_manifest()`,
  `load_manifest_from_disk()` (refresh snapshot + apply declared fields + sync
  `dependencies:` edges).
* Cross-project dependencies: `sync_dependencies()` (manifest →
  `project_dependencies` table), `get_dependencies()`, `get_dependents()`,
  `get_dependency_graph()` (downstream/upstream with cycle detection),
  `analyze_impact()` (transitive affected-project list ordered by depth),
  `get_deploy_plan()` (ordered deploy checklist with auto-deploy filtering).
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
* `OPENCODE_CONFIG_NAMES`, `sync_opencode_config()` — mirror a project's
  `model` / `agent` override and per-agent `agent_models` map into its
  `opencode.json[c]` (`model`, `default_agent`, and `agent.<id>.model` keys) so
  the OpenCode TUI honors the dashboard setting. Shared-project MCP entries use
  a portable `PERUN_HOME=~/.perun` instead of the absolute user path and set
  `PERUN_PROJECT_PATH=.`.
* `shared_mcp_config_status()` — read-only check used by the Shared tab and
  health check: reports whether a shared project's `opencode.json` has
  `perun-guard` / `perun-proposals` with `PERUN_PROJECT_PATH`.

### `perun/providers.py`
LLM provider discovery, status and configuration. Perun never stores credentials
itself; it edits OpenCode's own config so the CLI/server remain the source of truth.
For headless runs it also exports active API keys at runtime and injects them as
environment variables into the child ``opencode run`` process.
* `_PROVIDER_CATALOG` — built-in/well-known providers grouped by ``kind``
  (``api_key`` / ``local`` / ``oauth`` / ``custom``). Non-built-in presets include
  the canonical OpenAI-compatible runtime ``package`` and a known ``default_base_url``.
* `active_api_key_credentials()` / `auth_env_for_subprocess()` — read active
  API-key credentials from ``opencode auth export`` and map them to the env vars
  the OpenCode CLI expects (e.g. ``OPENCODE_GO_API_KEY``, ``OPENAI_API_KEY``).
  Credentials are read fresh per call and never cached or persisted by Perun.
* `resolve_run_model()` — pick a model string that works for headless runs:
  honors the requested provider if authenticated, resolves known aliases such as
  the Kimi model id to its OpenCode Go counterpart, and falls back to active
  cloud providers before considering local providers.
* `_available_models()` — cached wrapper around ``opencode models``.
* `list_providers()` — ``GET /api/providers``; merges providers from
  ``opencode auth list --format json`` with providers configured in ``opencode.json[c]``,
  plus a ``catalog`` of providers not yet active. Returns ``config_had_comments`` so
  the dashboard can warn before rewriting. Results are cached.
* `logout_provider()` — ``POST /api/providers/{id}/logout``; runs
  ``opencode auth logout`` and validates the provider id.
* `connect_provider()` — ``POST /api/providers/{id}/connect``; writes a provider entry
  into ``opencode.json[c]`` and reloads OpenCode. Keeps Perun's ``ollama_host`` in sync
  when connecting local Ollama.
* `disconnect_provider()` — ``POST /api/providers/{id}/disconnect``; removes the config
  entry (even if ``opencode auth list`` fails) and clears any saved auth credential.
* `_creationflags()` — returns `CREATE_NO_WINDOW` on Windows so background
  ``opencode models`` / ``auth list`` / ``auth export`` / ``auth logout`` calls
  never flash a console window.

### `perun/embeddings.py`
Ollama transport + vector math (project-agnostic).
* `DOC_PREFIX`, `QUERY_PREFIX`, `MAX_CHARS_PER_CHUNK`, `IDENT_RE`, `FILE_RE`.
* `EmbeddingError`, `cosine_similarity()`, `pack_vector()`, `unpack_vector()`,
  `embed_texts()`, `embed_one()`, `extract_metadata()`, `ollama_ok()` (cached
  health probe behind the graceful-degradation path).

### `perun/grep.py`
Integrated code-search layer — ripgrep + ast-grep + pure-Python fallbacks, exposed
both as MCP tools and HTTP endpoints so agents do not need separate search MCP
servers.
* `rg_search()`, `rg_available()`, `_parse_rg_json()` — regex content search.
* `rg_search_global()`, `ast_grep_search_global()`, `search_all_projects()` —
  cross-project variants backing `perun_global_grep`,
  `perun_global_ast_grep`, and `/api/search/global-*`.
* `ast_grep_search()`, `ast_grep_available()`, `_parse_ast_grep_json()` —
  structural/AST search.
* `_python_grep()`, `_python_ast_search()` — pure-Python fallbacks.
* `search_capabilities()` — reports which CLI backends are installed.
* `_run_streamed()`, `_is_project_path_safe()`, `_safe_compile_regex()`,
  `_sanitize_root()`, `_validate_pattern()`, `_looks_catastrophic()` —
  streaming, path-safety, pattern validation, and nested-quantifier/ReDoS
  rejection helpers.
* `_gitignored()`, `_read_gitignore()`, `_glob_match_simple()` — `.gitignore`
  support for the Python fallback.
* `_creationflags()` — `CREATE_NO_WINDOW` on Windows so ripgrep/ast-grep
  subprocesses launched in the background do not flash console windows.

### `perun/search.py`
State-of-the-art unified search orchestrator. Fuses vector memory, global
ripgrep, and global ast-grep in parallel and re-ranks code-level results with
Reciprocal Rank Fusion (RRF).
* `unified_search()` — single entry point for cross-project search.
* `_resolve_projects()`, `_search_memory()`, `_search_code_grep()`,
  `_search_code_ast()`, `_rrf_fuse()` — pipeline helpers.
* `capabilities()` — reports which search backends are available.

### `perun/memory.py`
Unified facts + document vector store + search.
* `content_hash()`, `is_sensitive()`, `_freshness_weight()`, `_keywords()`,
  `_embed()`.
* Facts: `remember()`, `search_facts()`, `search_room_facts()`, `_fact_rows()`,
  `recent_facts()`, `prune()`, `forget()`, `supersede()`, `touch_fact()`.
  `room_id` lets PerunChat scope facts to a room; `_cosine_near_duplicate()`
  merges near-duplicate rules/decisions.
* Docs: `chunk_markdown()`, `_chunk_codemap()`, `_split_oversized()`,
  `_index_text_impl()`, `index_text()`, `index_project_docs()`, `search_docs()`.
  CODEMAP files are split at `###` modules into `<H2> › <H3>` chunks capped at
  `CODEMAP_TARGET_CHUNK`; CHANGELOG files are reduced to a release index when
  oversized.
* Unified / maintenance: `unified_search()`, `stats()`, `usage_stats()`,
  `reembed_missing()` (re-embeds facts/doc chunks whose vectors are missing or
  stale).
* Deterministic pre-fetch: `prefetch_rules()` (durable `rule`/`decision` facts,
  Ollama-free).
* Migration: `import_legacy_memory()`.
* `DOC_CANDIDATES` (files indexed per project).
* `MAX_CHUNK_CHARS`, `CODEMAP_TARGET_CHUNK`, `MAX_SEARCH_CANDIDATES`,
  `MAX_KEYWORD_CHARS`, `MAX_QUERY_WORDS` (safety/performance bounds).
* In-memory telemetry: `_record_usage()`, `_usage_events`, `_usage_lock`
  (bounded rolling window; never persisted).

### `perun/context.py`
The **Context layer** — keyed, vector-backed reference docs that replace the
unbounded ambient context in `AGENTS.md`. Built around one hard constraint:
the agent's awareness of a project must never fall below its current level.
* Keys: `normalize_key()`, `_split_section()` (`"key#Section"`).
* Writing: `index_docs()` (one batch, embedded **outside** the DB connection),
  `index_doc()`, `delete()`, `reembed_missing()`.
  * A body that trips `memory.is_sensitive()` is still **stored verbatim** (the
    parity contract forbids deleting anything) but is **never embedded** — the
    embedding is the only thing that leaves the machine. Such a row is marked
    `embedded_hash == content_hash` with a NULL vector, so the repair path
    leaves it alone.
* Reading: `get()`, `list_keys()`, `_scope_rows()` (project scope shadows
  global), `outline()`, `slice_section()`.
* Retrieval: `search()` — exact key → anchor label → key substring → semantic;
  `_anchor_lookup()` (resolves a `perun.md` anchor label or its `memory:` key),
  `_rank_anchors()` (exact/token/substring/path scoring), `_anchor_label_set()`
  (manifest + synthetic doc anchors), `_resolve_anchor()`,
  `_anchor_path_hit()` (indexed `doc_chunks` section or bounded stub),
  `_semantic()` (cosine + keyword bonus).
* Manifest cache: `_load_manifest()` / `_manifest_cache_entry()` keyed by
  project id and invalidated on manifest mtime/size; label embeddings are
  cached alongside the parsed manifest.
* Stale-vector guard: both `_semantic()` and `_anchor_path_hit()` only use an
  embedding when `embedded_hash == content_hash`.
* Pre-fetch: `prefetch()` — deterministic first, embedding second; exact key
  → anchor → key fragment → **subject** match (`topical_overlap()`,
  `_topic_terms()`, `_stem()`), then local embedding similarity when Ollama is
  up. Anchor label resolution and `doc_chunks` section selection also use local
  embeddings as a fallback. Capped at `PREFETCH_MAX_DOCS` /
  `PREFETCH_MAX_CHARS`.
* Rendering: `render_hit()`, `render_hits()`, `render_inventory()`.

### `perun/context_map.py`
The **losslessness contract** for an `AGENTS.md` migration — the proof that
moving reference material into the Context layer did not lose anything.
* Sections: `split_sections()` (level-2 headings; the text before the first
  heading is the `"(top)"` section), `contains()` (whitespace-insensitive
  substring), `_verbatim()` (a body is present *unchanged* — as a substring or
  as a section with the same content hash), `_live_section()` (the current body
  of a level-2 heading in the live `AGENTS.md`).
* The map file: `MAP_DIR`/`.perun/context-map.json`, `map_path()`,
  `load_map()`, `save_map()`, `build_map()` (a draft — every section starts
  unclassified).
* Destinations: `inline` (stays in `AGENTS.md`; the `required` fragments must
  still be there), `context` (relocated to a Context-layer doc, by key),
  `secret` (moved to the encrypted store), `retained` (lives only in the
  retained original). `DEST_KINDS`. Sections may declare **mixed**
  destinations (`inline` + `context`) to keep rule bullets inline and point
  elaboration at a context doc.
* `check()` — the gate: the retained original still hashes to the map's
  `source_hash` (nothing was deleted), every section of that original is
  accounted for (nothing is unaccounted for), and every destination still
  holds its content (nothing was paraphrased away). It also flags repeated
  inline `###` blocks (`_inline_subsection_duplicates()`) and reports the
  largest inline subsections (`_inline_subsection_sizes()`).
* Per-project context docs: `context_dir()` (`.perun/context/`),
  `context_key()`, `retained_key()`, `project_context_files()`,
  `project_context_key()`, `write_context_doc()`.

### `perun/handbook.py`
Perun's own reference material, as retrievable context docs.
* `ENTRIES` — the manifest of handbook keys (single source of truth for the
  kernel's key table, the indexed docs and `perun context list`).
* `HANDBOOK_DIR` (`perun/integrations/templates/handbook/`), `_indexing` (the
  re-entrancy guard — rendering the tool reference loads the MCP module, which
  calls back into `ensure_indexed()`).
* `collect()`, `ensure_indexed()`, `_load_mcp_module()`,
  `_list_mcp_tools()` (live tool list, sync-only helper), `tool_schema_stats()`
  (byte size of the live tool schemas for the ambient ledger),
  `render_tool_reference()` (generated from the **live** MCP registry, so it
  cannot drift), `render_key_table()`, `render_kernel()`.

### `perun/discovery.py`
Find & register OpenCode projects under roots.
* `SKIP_DIRS`, `OPENCODE_MARKERS`, `DOC_MARKERS`.
* `capabilities()`, `classify()`, `detect()`, `_candidate_dirs()`, `scan()`,
  `register()` (merges any `perun.md` manifest plus the JSON-formatted fallback,
  parses dependency files into memory facts, and records local project refs),
  `_record_dependencies()`, `_record_local_references()`,
  `discover_and_sync()`, `register_self()`. Registration also calls
  `project_roots.auto_register()` so newly discovered projects outside the
  known roots become addressable for cross-project path resolution.

### `perun/project_roots.py`
Central registry of known project roots — prevents agents from guessing
cross-project paths (e.g. `.\my_apps\perun\AGENTS.md`, which resolves relative
to the CWD and silently hits the wrong file).
* `DEFAULTS` (built-ins: `perun`, `perun_chat`, `perun_in_the_machine` under
  `C:\my_apps\`), resolution order: `PERUN_PROJECT_<NAME>` env →
  the runtime project-roots file under PERUN_HOME → defaults.
* `get_root(name)`, `resolve(name, *parts)` (raises on path escape),
  `list_roots()`, `add_root()`, `remove_root()`, `auto_register()`.
* CLI: `python -m perun project-roots [--list] [--add NAME PATH] [--remove NAME]`.
* Mandatory for cross-project references — see `AGENTS.md`.

### `perun/agents.py`
Discover OpenCode agents available to a project.
* `BUILTIN_AGENTS` (`build`, `plan`, `general`, `explore`)
* `_scan_agent_dir()`, `_parse_frontmatter()`, `_agent_id_from_path()`.
* `list_agents(project_path)` — built-ins + global + project-local agents.

### `perun/scaffold.py`
Starter-project skeleton + directory creation + safe backfill + dependency parsing
+ manifest anchor refresh.
* `ScaffoldError`, `create_dir()`, `bootstrap()` (writes `opencode.json`,
  `AGENTS.md`, `README.md`, `.gitignore`, `perun.md`, `docs/CHANGELOG.md`,
  `docs/TECHNICAL_DOCUMENTATION.md`, `docs/CODEMAP.md`, and `.opencode/`),
  `backfill()` (creates only missing scaffold files, then enriches the
  manifest),
  `enrich_manifest()` (fills missing docs/commands/anchors in a minimal
  `perun.md` while preserving existing values and shared-project fields),
  `detect_anchors()` (finds dependency/build/CI files),
  `parse_dependencies()` (reads dependency manifests such as Node package
  manifests, Python requirement/pyproject files, Rust manifests, and Go
  modules),
  `update_manifest_anchors()` (appends newly-detected anchors without clobbering),
  `validate_manifest()` (stale doc/anchor checker),
  `generate_codemap_outline()` (compact `<slug>.codemap` navigation outline),
  `find_local_project_references()` (cross-project refs via `file:` deps),
  `_manifest_md()`, `_agents_md()`, `_suggest_commands()`, `summary()`.

### `perun/docaudit.py`
Documentation-to-code traceability audit for `docs/CODEMAP.md`.
* `audit_project()` — parses every `###` module block, checks that backticked
  file paths resolve to real files, verifies class/function/constant identifiers
  against the referenced Python files via AST, and checks the package version
  appears in `docs/CHANGELOG.md` and `docs/CODEMAP.md`.
* `_codemap_rows()`, `_module_symbols()`, `_looks_like_path()`, `_project_version()`,
  `FILE_RE`, `IDENT_RE`, `VERSION_RE`.
* CLI: `python -m perun docs audit [slug|--all]`.

### `perun/git.py`
Safe wrapper over the `git` CLI (per-project repositories).
* `GitError`, `available()`, `is_repo()`, `info()`, `log()`, `validate_remote()`,
  `init()`, `set_remote()`, `commit()`, `push()`, `clone()`.
* All calls run non-interactively with explicit args; remote URLs are validated
  (`validate_remote()`), and branch names reject option-like/control values.

### `perun/manifest.py`
Self-describing project manifest — `perun.md` (YAML front matter + Markdown
body) with a JSON-formatted fallback manifest.
* Constants: `MANIFEST_MD`, `MANIFEST_JSON`, `MANIFEST_NAMES`, `SCHEMA_VERSION`,
  `ANCHOR_KINDS` (`file`, `doc`, `url`, `command`, `symbol`, `secret`).
* `ManifestError`, `find()`, `parse()`, `parse_json()`, `load()`, `normalize()`,
  `dump()`, `save()`, `snapshot()`, `to_project_fields()`, `from_project()`,
  `default_body()`.
* `_normalize_anchors()` — an anchor may carry a **`memory:`** key (the
  context-doc key it binds to, so the agent can ask for it by label) and may be
  `kind: secret` (its `path` names a secret in the encrypted store).
* `_agent_models_map()` — normalizes the per-agent default model map
  (`agent_models: {plan: ..., build: ...}`).

### `perun/orchestrator.py`
Start / stream / cancel headless `opencode run`.
* `EventBus` (`subscribe`, `unsubscribe`, `publish`, `subscribe_all`,
  `unsubscribe_all`, `drop_history`, `close`).
* Process helpers: `resolve_opencode()`, `terminate_tree()`, `parse_event()`,
  `compose_prompt(project, prompt, settings=…)` (prepends global + project
  instructions to a new session), `_prefetch_blocks()` (the **task-driven
  context pre-fetch** — injects the reference material the request actually
  needs, in a `--- PERUN CONTEXT START/END ---` envelope).
* Global instruction files: `_instruction_files()`, `_load_instruction_file()`,
  `_parse_frontmatter()`, `instruction_blocks(scope)`, `_seed_default_instructions()`
  (called from `Orchestrator.__init__`, never at import time).
* Project-local instruction files: `PROJECT_INSTRUCTIONS_DIR`,
  `_project_instruction_dir(project, create=…)`, `_project_instruction_files(project)`,
  `_load_project_instruction_file(path)`, `_project_instruction_blocks(project)`,
  `_safe_instruction_filename(filename)` (reduces a manifest-supplied name to a plain
  `*.md` basename), and `write_project_instruction_file(project_path, text, …)` — the
  single writer shared by the project API, MCP tools, discovery, and manifest sync
  (instruction text is never stored in the database); it refuses paths that escape the
  project root.
* `_SlotLimiter` (mutable, `threading.Condition`-based concurrency limiter; `set_max()`),
  `_child_env(depth)` (strips raw secret env vars, sets `PERUN_DEPTH`, injects the
  resolved Telegram token as `PERUN_TELEGRAM_TOKEN`).
* `Orchestrator`: `start_run()`, `run_and_wait()`, `wait_for_run()` (the
  event wait that `run_and_wait()` delegates to; also used by the PerunChat
  bridge to avoid duplicate runs), `cancel()` (idempotent; also cancels queued
  runs), `is_final()`, `set_max_concurrent()`, `active()`, `_worker()`,
  `_execute()` (with wall-clock watchdog thread and no-loss stdout
  parsing: `_harvest()` parses each line *before* honouring a cancel/timeout
  break, and a bounded `_drain()` thread harvests buffered final lines after the
  process exits), `_emit_end()`, `_finalize()`, `stream()`. Agent precedence:
  explicit arg → `project.agent` → (control only)
  `orchestrator_agent`/`default_agent`. Model precedence: explicit arg →
  `project.agent_models[agent]` → `project.model` → (control only)
  `orchestrator_model`/`default_model`. The global default is never forced onto
  ordinary project runs — with no override they defer to OpenCode's own default
  model. Every source is stripped; `start_run` stores `model_explicit=bool(model)`
  and `_execute` forwards `--model` whenever a non-empty model was resolved
  (only an empty resolution defers to OpenCode).
* For shared-project proposals, `_execute()` calls `providers.resolve_run_model()`
  to route models to an authenticated gateway, injects active API-key env vars,
  and runs through the existing OpenCode service (`standalone=False`) so the
  project's MCP servers (notably `perun-guard` and `perun-proposals`) load.
* Constants: `MAX_STORED_OUTPUT`, `HISTORY_LIMIT`, `SUB_QUEUE_LIMIT`,
  `HISTORY_TTL_SECONDS`, `STREAM_POLL_SECONDS`, `MAX_AGENT_DEPTH`,
  `MAX_INSTRUCTION_BYTES`, `MAX_TOTAL_INSTRUCTION_BYTES`, `DEFAULT_SESSION_PROMPT`,
  `DEFAULT_AGENT_INSTRUCTIONS`, `_SECRET_ENV`.
  The instruction envelope uses plain-text markers
  (`--- PERUN INSTRUCTIONS START ---` / `--- PERUN INSTRUCTIONS END ---`).

### `perun/package_handoff.py`
Safe ZIP work-package handling for the external execution handoff. Perun never
runs an LLM on this path; the remote rw participant generates the package with
its own model/tokens and Perun validates/applies it locally.
* `pack()` — build a ZIP from a list of target paths relative to the project root;
  skips sensitive files/directories (`.env`, `.git`, `.perun`, keys, OpenCode
  config, …) even when a requested directory contains them, and enforces the
  configured package/entry/entry-count caps before building the archive.
* `unpack()` — decode base64 chunks, reassemble the archive, and validate against
  caps (uncompressed total 50 MB, 500 entries, 10 MB/entry) and anti-abuse rules;
  directory entries are skipped. Entry names are case-insensitively de-duplicated
  and reject shell-expansion characters (`$`, `%`, `~`) or backslashes.
* `validate_package()` — reject traversal (`..`), absolute paths, drive letters,
  symlinks, encrypted entries, duplicate entry names, sensitive paths, and
  entries outside the approved proposal targets.
* `apply_package()` — stage the validated entries, back up existing targets, and
  apply with a `mkstemp` temp + `os.replace`; a mid-apply failure restores the
  backups, so a package is not left half-applied.
* The external download path is restricted to the proposal's approved targets;
  a claimant cannot request arbitrary project paths. External-dependency
  targets are rejected for external handoff (they require local execution).

### `perun/skills.py`
Discovery of OpenCode agent skills and tools available to a project.
* `BUILTIN_SKILLS` — runtime-provided OpenCode skills (`code-audit`,
  `android-design-guidelines`, `find-skills`) always surfaced by `discover_skills()`.
* `BUILTIN_TOOLS` — documented built-in OpenCode tools.
* `_read_skill_manifest()`, `discover_skills()` — scan global
  ``~/.config/opencode/skills/`` and project-local ``.opencode/skills/`` for
  skill manifest files following the standard skill naming convention with YAML front matter.
* `_export_names()`, `discover_tools()` — scan ``~/.config/opencode/tools/`` and
  project-local ``.opencode/tools/`` for custom TypeScript/JavaScript tool
  definitions, merging them with the built-in tool set.
* Used by `/api/skills` and `/api/tools`.

### `perun/opencode_config.py`
Safe read/write helpers for the user-level OpenCode config (``opencode.json[c]``).
* ``_config_file()`` honours ``OPENCODE_CONFIG`` and ``XDG_CONFIG_HOME``.
* ``load_config()`` returns ``(data, path, had_comments)`` so callers can warn when
  comments/trailing commas will be lost on rewrite.
* ``save_config()`` parses JSON/JSONC and backs up before atomic writes.
* ``providers_section()`` / ``set_providers_section()`` handle both legacy ``provider`` and V2 ``providers`` keys.
* ``configured_providers()``, ``configure_provider()``, ``remove_provider()`` edit provider entries without touching unrelated config keys.
* ``reload_opencode()`` asks the OpenCode service to reload its config after a change.
* ``_creationflags()`` — `CREATE_NO_WINDOW` on Windows so the background
  ``opencode reload`` call does not flash a console window.

### `perun/mcp_servers.py`
Discover the MCP servers OpenCode is configured to load, and live-probe them on
demand. Perun only *reads* the config here (the global installer owns Perun's own
entry); probing never happens automatically.
* `global_config_files()`, `project_config_files()`, `read_opencode_config()` — locate and
  parse ``~/.config/opencode/opencode.json[c]`` plus each project's
  ``opencode.json[c]`` (comments *and* trailing commas tolerated); a file that
  cannot be parsed is returned as an error, not silently treated as empty.
* `snapshot(projects)` — every server (global first, then per project) with a
  cheap config-derived `status` (`ready`/`missing`/`disabled`/`remote`/`invalid`),
  transport, resolved executable, and `shadowed_by` when a project overrides a
  global name. Duplicate `(scope, project, name)` entries (e.g. a name in both
  ``opencode.json`` and ``opencode.jsonc``) collapse to one; unreadable configs
  surface in `parse_errors`.
* `find(name, scope=…, projects=…, project=…)` — resolve one server by name.
* `probe(name, …)` — spawn a local server (or call a remote URL) and perform the
  MCP `initialize` + `tools/list` handshake, returning status, `tools`, latency,
  and any stderr tail. The command always comes from disk, never the request.
  Concurrency is gated (a saturated gate returns `busy` instead of pinning a
  worker thread) and the timeout is clamped to `MAX_PROBE_TIMEOUT`.
* `_probe_stdio()`, `_probe_http()`, `_derived_status()` — the two transports and
  the static status rule. `_probe_http()` shares one deadline across its two
  requests; `_probe_stdio()` bounds its stderr buffer and reaps children it kills.
* `windows_launch_argv()` — pure helper that routes ``.cmd``/``.bat``/``.ps1``
  shims through `cmd` / PowerShell so CreateProcess can launch them.
* Backs `GET /api/mcp/servers` and `POST /api/mcp/servers/probe`.

### `perun/process_tracker.py`
Lifecycle tracking for externally-launched TUI/terminal sessions.
* `ProcessTracker`: `register()`, `get()`, `is_alive()`, `terminate()`,
  `terminate_all()`, `finalize()`, `_resolve_child_pid()` (descendant-first marker
  scan with full-system fallback), `_kill_tree()`, `_watch_loop()`.
* Uses `psutil` to walk process trees and resolve Windows Terminal child
  processes. The frontend injects a `PERUN_RUN_UID` environment marker into the
  shell command so the tracker can find the TUI process even when the launcher has
  already exited.
* `TRACKER` — module-level singleton used by `/api/run-in-terminal`,
  `/api/runs/{uid}/cancel`, and `/api/runs/{uid}/kill`.

### `perun/routing.py`
Deterministic message router (rules tier, no model, no I/O). Turns one message
into a `RouteDecision(action, project, confidence, matched_by, candidates,
reason)`.
* `normalize()` — NFKC + casefold + alphanumeric words, so `trading-bot`,
  `trading_bot` and `trading bot` are identical.
* `resolve_project()` — exact slug, exact display-name phrase, unique
  non-generic tag (0.95), single-word stem with Polish palatalisation
  (`perunie`, `perunchacie`, `tradingu`, 0.8), and multi-word slug with an
  inflected tail (`trading-bota`, 0.9). Longest span wins; two projects at the
  same confidence/span return `candidates` instead of a guess.
  Tags are ignored unless unique and non-generic (live tags include `docs`,
  `tests`, `opencode`, and `perunchat` itself is tagged `perun`).
* `detect_action()` — explicit PL/EN keyword sets (`stop`, `remember`, `recall`,
  `status`, `run`), matched in the **leading window** (after an optional mention
  or "please") so a sentence that merely mentions a command word — "dodaj
  komendę status" — stays a run instead of becoming a status query. Negation
  ("nie uruchom") returns `unknown`; a question in the opening words wins over a
  verb later in the sentence; a trailing `?` on an imperative is still a run.
  `stop loss`/`stop limit` are domain terms, not commands.
* `is_addressed()` — `@mention`, `@perun`, slash command, or a direct message.
* `route()` — applies the policy: ambiguity ⇒ `clarify`; a mutating action
  without a project mention stays on the bound project (today's behaviour);
  `unknown` is returned untouched.
* Offline inspection: `python -m perun.routing "<message>"` prints the decision
  as JSON against the live registry.

### `perun/telegram.py`
Shared Telegram ⇄ Perun bridge.
* Formatting: `format_html()`, `chunk()`, `_esc()`.
* `PidLock` (single-instance; `acquire()`, `release()`, side-effect-free
  `is_held()`, `_default_mutex_name()` namespaces the mutex by `PERUN_HOME`),
  `_pid_alive()`. One poller machine-wide (`getUpdates` allows a single consumer);
  a second start is refused rather than fighting with HTTP 409s. On POSIX the pid
  file is created with `O_CREAT|O_EXCL` so two bridges cannot both acquire.
* `PerunTelegramBridge`: `api()`, `api_base()`, `is_running()`,
  `update_token()` (swap a bot token in place — used by
  `__main__.reload_in_process_bridge()`), `send()` (supports optional
  `reply_markup`), `handle_command()`, `_handle_text_command()`,
  `_project_keyboard()`,
  `_handle_callback()` (inline-keyboard project selection; falls back to numeric
  ids for slugs that exceed Telegram's 64-byte `callback_data` limit),
  `_current_project()` (single-user-project auto-select, else the control plane),
  `_handle_update()` (deduplicates redelivered messages by chat + message id),
  `run_message()`, `worker()`, `authorised()` (strict: an empty allow-list grants
  nobody), `_handle_pair()` (`/pair <code>`, `hmac.compare_digest`, dispatched
  *before* the auth gate), `pair_code()`, `active_runs()`, `run_for()`, `run()`.
  Per-chat in-flight runs live in `_runs` so `/stop` only cancels the caller's.
  With the routing setting enabled, `run_message()` first calls `_route_message()`
  (`_command_argument()`, `_log_routing_decision()`): `status`/`stop`/`remember`/
  `recall` are answered without a run, ambiguity asks, and a `run` naming another
  project rebinds `bridge_links` before starting. `routing_shadow` logs the same
  decision (message prefixed `shadow:`) and then takes the previous path, for a
  log-only trial week. Decisions are logged as
  `routing_decision` activity rows without the message text.
  Commands include `/orchestrator` (`/perun`, `/overseer`) and `/pair`.
* Entry: `run_bridge()` (generates/persists a one-time pairing code when no chats
  are configured; cancels all active runs on shutdown), `_setup_logging()`.

### `perun/perunchat_bridge.py`
Autonomous PerunChat chat participant for the Orchestrator.
* Module API: `attach()` and `get_bridge()` (the only supported way to obtain the
  bridge; used by `server.py`), `DEFAULT_PUBLIC_ROOMS`,
  `CHAT_SYSTEM_PROMPT`, `_load_chat_instructions()` (reads the embedded
  `perun/instructions/perunchat_chat.md` persona, which names the prefixed
  guard tools and requires a guard read-back to verify executed proposals).
  The installed `perunchat` agent allows the guard/proposal/memory tools and
  denies the built-in `skill` loader.
* `PerunChatBridge` — background thread: `status()`, `start()`, `stop()`,
  `restart()`; connects to the gateway via the configured API key,
  auto-joins `perunchat_auto_join` rooms, listens for incoming messages, and
  answers through `Orchestrator.run_and_wait()` with the chat system prompt.
  With the routing setting enabled, an addressed message is first checked by
  `_warn_cross_project()`: a mutating request for a different project is answered
  with one `[Routing]` notice (and a `routing_decision` activity row) instead of
  replying from the room's own project. `_frame_text()` normalises the payload.
  Rate-limited (15 replies/min). Shared-project chats run against the bound
  project's slug with `agent="perunchat"` and the configured chat model, and
  load the project's `opencode.json` so the agent can use the
  `perun-guard`/`perun-proposals` servers; `_record_room_actor()` persists the
  per-message requester and the bot's own agent id under PERUN_HOME so the
  statically-configured proposals server can authorize `approve`/`reject`.
  Unbound/public rooms use the control
  plane. State-changing requests are sent through the `propose` tool, not
  executed directly. A background activity-table poller posts review requests
  and state digests back to the bound room so notices work even though the MCP
  server runs in a separate process. When `auto_approve` is on, the poller also
  schedules `_agent_review_worker()` for proposals from other agents; it reads
  the targets with the guard tools and approves with `execute=false`, leaving
  the proposal claimable. Reviews are capped at two concurrent runs
  (`_review_sem`), proposal content is wrapped as `<untrusted_proposal>` data,
  and a failed review posts a human-action notice.
* No-loss reply delivery: every outbound reply frame is persisted in a
  write-ahead outbox under PERUN_HOME before sending and removed only
  after success; `_flush_outbox()` resends queued frames after a reconnect with
  their original id + `sequence_number`, which is idempotent at the gateway.
  Empty replies are retried once, and a still-running run is awaited instead of
  starting a duplicate. Replied-message ids are persisted under PERUN_HOME so bridge restarts do not reply twice to replayed
  history.
* Replay/echo dedup and per-room serialization: `_seen_message_ids` and
  `_recent_content` drop duplicate frames; `_room_queues` and `_room_workers`
  serialize replies so only one message per room is processed at a time while
  keeping the WebSocket reader thread free. Prompts are passed via a temp file
  (`--file`) to avoid command-line length limits; history includes Perun's own
  replies so the model sees a normal chat transcript, and the latest message is
  preserved when trimming.
* Native typing indicator: `_send_typing_indicator()` emits
  `application/perun-typing` frames with `"typing"` / `"idle"` payloads. The
  indicator is shown after ~1.5 s of generation and cleared on success,
  no-reply, timeout, or failure. The frame increments and persists `_seq` before
  sending so the following reply cannot reuse the same `sequence_number` and be
  rejected by the gateway as non-monotonic.
* Reply timeout: a hard 60-second wall-clock timeout wraps generation. When it
  fires, the timeout handler cancels the underlying orchestrator run so the
  slot is freed and the background `_reply` thread cannot send a stale reply.
* Conversational memory: extracted facts are stored via `memory.remember()`
  with `room_id`, `sender`, and `type` metadata. Project rooms use the real
  project id; other rooms use a reserved negative id plus room filtering, and
  `_fact_visible_in_room()` prevents public/other-room facts from crossing into
  project prompts. `_maybe_store_facts()` applies heuristic + explicit
  `[FACT(type=...)]` extraction with semantic deduplication.
* Room transcript memory: `_store_transcript_fact()` stores each non-public
  chat message as a transcript fact, `_prune_room_transcripts()` keeps the most
  recent `_MAX_ROOM_TRANSCRIPT_FACTS` (250) per room, and the MCP tool
  `perunchat_room_memory()` lets agents query that history on demand. Public
  rooms are ephemeral and skip long-term transcript storage entirely.
* Inline history split: `_max_context_messages_for_room()` keeps the last 4
  messages inline for rooms with durable memory (private/group/project) and
  6 messages for public rooms, which have no room memory.
* Private-room support: `_is_private_room()` and `_room_type()` accept `priv:`
  and `private:` prefixes; the dashboard proxies create/invite/remove calls to
  the gateway.
* `_ensure_opencode_agent()` installs/updates the dedicated `perunchat` OpenCode
  installed perunchat OpenCode agent file on bridge start, overwriting it
  whenever the embedded content changes so prompt fixes reach production.
* Chat model/agent precedence: `perunchat_chat_model`/`perunchat_chat_agent` →
  `orchestrator_model`/`orchestrator_agent` → defaults. The `settings` property
  reads from the orchestrator each time, so dashboard changes take effect without
  a bridge restart.
* External execution handoff (remote rw participants only): the bridge receives
  `application/perun-execution` control frames and `application/perun-package`
  ZIP chunks, persists upload slots to SQLite, and calls `package_handoff` to
  validate/apply the package. Uploads require `sha256`, are bound to the room and
  sender, are base64-validated, and enforce per-chunk/cumulative byte caps;
  `transfer_id` collisions are rejected and at most 8 uploads may be open at
  once, so one claimant cannot replace or exhaust another's slot. Upload slots
  are an idle lease: `external_execution_package_ttl` (default 30 min, 60 s
  floor) is refreshed by every accepted chunk, and stale slots are dropped from
  memory and SQLite. Result frames carry `op`, `ok`, `proposal_id` and
  `transfer_id` (`package_accepted` with `ok: true`, `package_rejected` with
  `ok: false`), so the MCP client can distinguish success from rejection.
  Downloads are
  restricted to the proposal targets, skip sensitive files, and enforce
  package/entry/entry-count caps. Handoff frames set `recipient_id`: results and
  downloads are delivered only to the claimant, upload chunks only to the room
  owner, and the gateway rejects unaddressed package chunks or result frames
  from non-owners. `apply_package()` stages files and rolls back
  on a mid-apply failure. The local host agent should use the normal
  shared-project pipeline instead.
* Shared-project chat flow: `_shared_project_context()` injects a trust-filtered
  project brief (no local paths or Git remotes) into managed `grp:` rooms.
  Proposals and peer decisions are created through the `perun-proposals` MCP
  tools (`propose`, `approve`, `list_proposals`, `get_proposal`), not text
  markers. When **agent approval** (`shared.auto_approve`) is enabled, the
  bridge posts a peer-review notice and schedules an autonomous review run
  (concurrency capped at two) that reads the targets through the guard tools and
  calls `perun-proposals_approve` with `execute=false`; the proposal stays
  `approved` for an external claim or explicit local execution. When **policy
  auto-approve** (`shared.policy_auto_approve`) is enabled, proposals are
  approved and executed immediately by the system. Direct-address detection uses
  this machine's resolved agent id, not a hard-coded chat id.
* `PERUNCHAT_API_KEY`/`PERUNCHAT_API_ENDPOINT` are in `orchestrator._SECRET_ENV` so
  spawned agent subprocesses never inherit them.

### Shared Projects modules

* `perun/shared_projects.py` — manifest schema normalization (`shared`,
  `auto_approve` for agent approval, `policy_auto_approve` for system
  auto-approval, `participants`, `external_deps`, `manifest_rev`), trust-list/role
  validation (`rw`/`ro`), manifest-participant removal with a revision bump,
  external-dependency path resolution, the project→room id helper, and the
  `is_auto_approve()` / `is_policy_auto_approve()` accessors.
* `perun/shared_project_sync.py` — reconciles the trusted subset of the manifest
  participant list with the PerunChat gateway ACL for managed `grp:` rooms,
  creating/lazy-creating or tearing down the dedicated project chat on lifecycle
  events; the room owner is this machine's gateway-assigned agent id, never a
  hard-coded chat id; retries pending deletions on startup.
* `perun/shared_agent.py` — installs the generated `perun-shared` OpenCode agent
  used for approved executions: deny-all permissions with only the prefixed
  `perun-guard_guard_*` tools allowed (`guard_*`/`perun_guard_*` aliases too)
  and the built-in `skill` tool explicitly denied. Its prompt names the exact
  prefixed guard tools and tells the model to retry the guard tool if a call
  accidentally hits `skill` and returns “Unable to load skill …”.
* `perun/hitl.py` — local HMAC-SHA256 HITL approval-token engine for action
  proposals (tokens bind proposal id, canonical JSON action hash, expiry, and
  nonce); signing secret configurable via `PERUN_HITL_SECRET` /
  `perun_hitl_secret`. `validate()` checks the signature before consuming the
  nonce, rejects malformed token/nonce/expiry types, and `HITLConfig` requires
  `nonce_ttl_seconds >= token_ttl_seconds + 5`.
* `perun/path_guard.py` — resolves the project allowlist (root + declared
  external deps), resolves relative targets against the project root, validates
  proposal targets, rejects shell expansions (`$`, `%`, `~`), and denies
  sensitive paths relative to the matching allowlist root (trailing dots/spaces
  normalised) even inside the root.
* `perun/guard_mcp.py` — policy-enforcing filesystem MCP server used by the
  Project Agent for shared-project runs and for read-only inspection in chat;
  validates every path against the manifest allowlist (`PERUN_ALLOWLIST` env),
  restricts writes to the proposal's declared targets, hides sensitive files/
  directories, and falls back to pass-through when unset. When the per-run env
  is absent (`standalone=False`), `_active_targets()` derives the target scope
  from the project's `executing` proposal; multiple concurrent executions fail
  closed.
* `perun/proposals_mcp.py` — proposal-coordination MCP server for project chats.
  Exposes `propose`, `approve`, `list_proposals` and `get_proposal`; uses
  `PERUN_PROJECT_PATH` / `PERUN_CHAT_SENDER` (requester) /
  `PERUN_CHAT_AGENT` (the bot's own approval identity) / `PERUN_ROOM_ID` from
  the chat run environment to validate rw/trust/owner rules server-side. When
  `PERUN_PROJECT_PATH` is absent (a project-local MCP server launched by the
  TUI), it falls back to the process cwd; the generated `opencode.json` also
  sets `PERUN_PROJECT_PATH=.` for both `perun-guard` and `perun-proposals`.
  When `PERUN_CHAT_SENDER` is absent (static MCP environment), it reads the
  per-room actor file written by the bridge (`config.perunchat_actor_path`);
  files older than one hour are ignored (`ACTOR_TTL_SECONDS`).
  `list_proposals()` normalizes `null`/`none`/`any`/`all`/empty status values
  to “no filter” because models sometimes serialize an omitted optional as the
  literal string `"null"`. `approve()` defaults to `execute=false`, leaving a
  proposal `approved` for external claim/manual execution; `execute=true` starts
  the local headless run.
* `perun/fs_audit.py` — post-run filesystem audit: snapshots allowlisted paths
  (optionally target-scoped) before a shared-project run, diffs them after, and
  logs allowlist violations, target-scope violations, and created/modified/
  deleted parent-directory entries to the activity table. Also provides
  `target_digests()` / `drifted_targets()` — the per-target sha256+size fingerprint
  captured at approval and re-checked at claim/apply. Content is always hashed
  (so `os.utime` cannot hide an edit); a target above the 256 MiB/file and
  512 MiB total budget is marked `unverifiable` and refused for external claims
  rather than checked more weakly. `null` means the target does not exist yet.
* `perun/action_proposals.py` — proposal lifecycle: validated/bounded envelopes,
  CAS approve/reject/cancel, atomic `approved → executing` claim, synchronous
  `execute`
  and non-blocking `start_execution`, `claim_execution`/`revoke_execution_claim`
  for the external rw handoff, startup `reconcile_interrupted`, post-run audit
  enforcement, State Digests for the project chat, and activity logging
  (`proposal_created`/`approved`/`rejected`/`cancelled`/`executed`). `cancel()`
  withdraws an approved-but-unclaimed proposal (`approved → cancelled`, reason in
  `result`) and is restricted to the requester or the project owner,
  because `reject()` only accepts `pending`. `approve()` stores
  `approval_snapshot` (per-target digests from `fs_audit.target_digests()`);
  `claim_execution()` and `complete_external_execution()` refuse a target that
  changed in between. `_same_actor()` enforces
  the requester-cannot-self-approve rule for agent callers inside
  `approve()`/`reject()`; the human dashboard routes and
  `auto_approve_and_execute()` pass `allow_self=True`. Approved runs use the
  restricted `perun-shared` agent and a target-scoped guard allowlist; the
  proposal id is also the headless run id. `expire_stale()` expires pending
  proposals after 24 h and approved proposals after 7 days. `_prepare_execution()`
  fails early with “guard MCP not registered” when the project config lacks
  `perun-guard`, probes the guard server for `guard_write_file`, and runs use the
  running OpenCode service (`standalone=False`) so the project's MCP servers are
  loaded. `_build_execution_prompt()` names the
  prefixed `perun-guard_guard_*` tools and instructs the model not to use the
  `skill` tool or to give up after an “Unable to load skill …” error.
  External execution claims are room/sender-bound and target-scoped, and expire
  after one hour by default (`external_execution_claim_ttl`, clamped to
  `[1 s, APPROVED_TTL_SECONDS]`); `claimed` proposals are returned
  to `approved` by revoke or by the expiration sweep. `executed_by` records the
  actor that attempted/applied the change (`perun` for local runs, the claimant
  for external handoffs), is cleared on revoke/expiry, and must be read with
  `status`; `create()` binds dashboard/API proposals to the project's
  `project_chats.room_id` when the envelope carries none. External applies
  atomically move `claimed → executing` (matching executor and unexpired lease)
  before `apply_package`; an expired or lost claim is rejected without writing.
  `expire_stale()` also fails an abandoned `executing` row after 12 h. Targets
  are de-duplicated and must be strings; a `create` proposal must touch every
  approved target before it can be reported `executed`.
* `auto_approve_and_execute()` — when a project's **policy auto-approve**
  (`shared.policy_auto_approve`) is enabled, approves a freshly-created pending
  proposal and starts its headless run immediately. The recorded approver is
  always the ``auto`` autonomous policy identity. Accepts ``on_started`` and
  ``on_complete`` callbacks so callers can post lifecycle notices to the bound
  project chat.
* `_pick_auto_approver()` — returns the ``auto`` policy identity; policy
  auto-approval is a system decision, not a peer review.
* `_humanize_result()` — shortens run-result JSON errors for chat State Digests
  without echoing upstream response bodies.
* `retry()` — clones a terminal proposal into a fresh pending proposal owned by
  the room owner, preserving intent/targets/details and tagging ``retry_of``.
* `delete()` — removes a non-executing proposal and logs the action to project
  activity.
* REST endpoints in `perun/server.py` manage proposals (list, create, approve,
  reject, `approve-and-execute` returning `202`, `execute`, `claim`,
  `revoke-claim`, retry, delete), post lifecycle notices
  (started/rejected/completed/failed) to the bound project chat, and apply
  shared-project manifest fields from the wizard; `execute` starts an
  already-approved proposal (the atomic claim happens inside `start_execution`).
  `perun/projects.py` keeps `opencode.json` in sync to register/remove the
  `perun-guard` MCP server.
* Tables: `project_chats`, `action_proposals`, and `pending_room_deletions` in
  `perun/db.py`.

### `perun/server.py`
FastAPI app + SSE + static dashboard.
* Globals: `SETTINGS`, `ORCH`, `PERUNCHAT_BRIDGE`, `app`, `STARTED_AT`,
  `_SSE_HEADERS`, `MAX_INSTRUCTION_FILENAME`.
* Guard: `_origin_guard` middleware + `_hostname()`, `_host_allowed()`,
  `_origin_parts()`, `_origin_ok()` — anti-CSRF / anti-DNS-rebinding (Origin
  scheme + effective port must match), applied to `/api/*` only. A **token
  middleware** then requires `X-Perun-Token`/`Authorization: Bearer` on every
  state-changing `/api/*` method (`_API_SAFE_METHODS` = GET/HEAD/OPTIONS), compared
  with `hmac.compare_digest`; order is Host → Origin → token. All responses receive
  baseline security headers (CSP, X-Frame-Options, etc.).
* Helpers: `_project_or_404()`, `_resolve_project()` (404 on an unknown non-empty
  slug), `_control_project()`, `_control_slug()`, `_ok()`, `_sse()`,
  `_max_activity_id()`, `_new_activity()`, `_str_field()` (wrong JSON types → 400),
  `_clamp()`, `_normalize_instruction_filename()`, `_project_instruction_path()`,
  `_normalize_agent_models()` (validate/merge per-agent `agent_models` from the
  project Config tab), `_ps_quote()`/`_posix_quote()`/`_cmd_quote()`, `_terminal_command()`
  (server-side command construction), `_dashboard_index()` (injects
  `window.PERUN_TOKEN` into `index.html` and rewrites `?v=` asset queries to the
  running version), `_DashboardStaticFiles` (revalidation headers for
  JS/CSS/HTML), `_post_proposal_started()`,
  `_post_proposal_rejected()`, `_post_proposal_digest()` (best-effort lifecycle
  notices to the bound project chat, redacted before leaving the process).
* Endpoint groups: health (`/api/health` — includes the `control` slug — and the
  structured `/api/models`), PerunChat (`/api/perunchat/status`, `/api/perunchat/key`
  kept for legacy loopback callers, and loopback-only `POST /api/perunchat/ws-ticket`
  which proxies a 30 s single-use WebSocket ticket), providers (`/api/providers…` — read-only
  `opencode auth` status + logout), agents/skills/tools (`/api/agents`,
  `/api/skills`, `/api/tools`),
  terminals (`/api/terminals`), MCP servers (`/api/mcp/servers…` — config
  snapshot + live probe), search (`/api/search/*` — grep, ast-grep, global,
  unified), projects
  (`/api/projects…` incl. the creation wizard, `/api/projects/{slug}/instructions`
  (legacy inline or project-local Markdown file), `/api/projects/{slug}/instruction-files`
  (multi-file project-local instructions with CRUD + toggle), `/api/projects/{slug}/manifest…`,
  `/api/projects/{slug}/git…`,
  `/api/projects/{slug}/tasks…`, shared-project proposals
  (`/api/projects/{slug}/proposals…` incl. `claim`/`revoke-claim` for external
  execution handoff and `cancel` for withdrawing an approved-but-unclaimed
  proposal; the list endpoint omits approval tokens/snapshots), `DELETE /api/sessions/{pk}`; `DELETE` refuses
  the control project),
  filesystem (`/api/fs/list`, `/api/fs/mkdir`), discovery (`/api/discover…`), runs
  (`/api/runs…`, `/api/runs/active`, `/api/runs/tui`, `/api/runs/{uid}/finish`,
  `/api/runs/{uid}/kill`,
  `/api/runs/{uid}/stream` (404 for an unknown run), `/api/stream`; `POST /api/runs`
  defaults to the orchestrator; `/api/runs/active` is declared **before**
  `/api/runs/{run_uid}`), run-in-terminal (`/api/run-in-terminal` — command
  built server-side from structured fields), memory
  (`/api/memory/*`, including `POST /api/memory/reembed` for on-demand embedding
  repair; a background thread also repairs missing/stale embeddings at startup),
  activity (`/api/activity`), settings (`/api/settings` — GET
  never echoes `telegram_token` or the pairing code), global instruction files
  (`/api/instructions/*`),
  telegram (`/api/telegram/*`: `status` reports
  `token_source`/`chats_source`/`bridge_task` and (loopback only) `pair_code`,
  `bot` verifies a new token with
  `getMe` before saving, `bot/change-password` re-encrypts a stored token,
  `bridge/restart` restarts the in-process bridge or the
  optional logon task, `unlock` decrypts an encrypted token and starts/reloads
  the bridge, `send`),
  perunchat (`/api/perunchat/*`: `status` reports configuration plus the resolved
  `agent_id`, `key` reveals the API key to loopback only,
  `bridge/status` reports the autonomous bridge state, social endpoints proxy to
  the PerunChat gateway, `trusted` is the local trust list, `rooms` proxies the
  gateway's `/api/v1/rooms` room discovery (active/joined/well-known public
  rooms) that feeds the dashboard auto-join picker; the dashboard opens a direct
  WebSocket to the gateway).
* Lifecycle: `POST /api/shutdown` signals uvicorn to exit gracefully; guarded by
  the same token middleware.
* `_sync_manifest()` (module-level alias of `projects.sync_manifest`), `serve()`
  (uvicorn launcher).

### `perun/__main__.py`
CLI. `cmd_serve`, `cmd_stop`, `cmd_restart`, `cmd_bridge`, `cmd_discover`,
`cmd_projects`, `cmd_add`,
`cmd_project_roots` (`project-roots --list/--add/--remove` — cross-project
root registry, see `project_roots.py`),
`cmd_status` (also reports `bridge_task` via `installer.bridge_task_status()`),
`cmd_remember`, `cmd_recall`, `cmd_index`, `cmd_run`, `cmd_import_legacy`,
`cmd_encrypt_config`, `cmd_global_instructions` (list / set / `--clear` the
global Markdown instruction files), `cmd_install`, `cmd_backfill_docs`, `cmd_validate_docs`, `cmd_refresh_deps`, `cmd_docs_audit`
(`--machine` / `--skip-deps` / `--skip-global` / `--skip-task` / `--task`
delegate to `installer.machine_install()`; `backfill-docs [slug] [--all]`
creates missing scaffold docs/manifest; `validate-docs [slug] [--all]` checks
for stale docs/anchors; `refresh-deps [slug] [--all]` parses dependencies into
memory facts; `docs audit [slug] [--all]` checks CODEMAP file/identifier/version
traceability),
`cmd_context` (**`perun context list|get|search|sync|map|index|verify|stats|dedupe`** —
the Context layer), `cmd_secret` (**`perun secret list|get|set|delete|password|status`**),
`cmd_update` (**`perun update check|status|apply|dismiss`** — check GitHub releases and self-update Perun from the CLI or dashboard), `cmd_mcp`, `build_parser()`, `main()`.
* `_is_owner()` (best-effort owner check: always true on Windows, UID-checked on
  Unix), `_resolve_project_targets()` (owner-level `--all` includes hidden
  projects), `_context_project_id()` (slug/path → project id; the cwd's project by
  default), `_context_targets()`, `_context_sync_projects()` (indexes each
  project's `AGENTS.md` as `<slug>.agents.full` and its `.perun/context/*.md`
  as `<slug>.context.<name>` — the retained original is a **snapshot** and is
  never overwritten once a context map pins it), `_context_map()` (show or draft
  the losslessness map), `_context_verify()` (kernel drift, instructions
  pointer drift, handbook coverage, `memory:`-bound anchors, `kind: secret`
  anchors, and the **losslessness check**), `_context_stats()` (AGENTS.md
  sizes, instruction files, MCP schema size, skill frontmatter, computed
  per-session token floor, duplicated `##`/`###` sections, and budget
  warnings), `_context_dedupe_agents()` (removes byte-identical duplicate
  `###` blocks from a project's `AGENTS.md`), `_context_instruction_files()`
  (ledger of every `instructions[]` file, with path escaping protection),
  `_skill_frontmatter_stats()`, `_split_sections()`, `_split_subsections()`.
* `_start_bridge_thread()` with process-lifetime `_BRIDGE_LOCK` / `_BRIDGE_THREAD`
  / `_IN_PROCESS_BRIDGE` — the `serve --bridge` path starts the bridge at most once
  per *machine* and keeps the single-instance mutex alive for the whole process.
  `reload_in_process_bridge()` updates the token of a running in-process bridge
  so token/password changes take effect without closing the dashboard.
  `start_or_reload_in_process_bridge()` is used by the unlock flow to start the
  bridge when it was not started due to a missing password.

### `perun/updater.py`
Self-update machinery. `check()` queries the GitHub releases API and caches the
answer; `install_mode()` decides whether this copy is a **git checkout**
(`git pull --ff-only`), a **Windows installer** (download and run
the Windows installer executable), or unsupported. `apply()` prepares the update while the
server is live (download + verify, or `git fetch`), then hands over to a
detached `perun update runner` process that waits for the server to exit,
applies the update, restarts Perun and polls `/api/health` until the new
version answers. State is persisted in a runtime file under PERUN_HOME so the
dashboard can stream progress across restarts. Installer downloads verify the
release's published SHA-256 checksum when present, old downloads are
cleaned up automatically, and server shutdown uses `should_exit` then SIGINT
before a final force-exit fallback.
* `_apply_installer()` runs the Inno Setup installer with
  `/CLOSEAPPLICATIONS /FORCECLOSEAPPLICATIONS /NORESTARTAPPLICATIONS` and waits
  for any already-running installer mutex before starting. On failure it includes
  the last 2,000 characters of the installer log in the error message.

### `perun/integrations/install_global.py`
Idempotent, additive global install into `~/.config/opencode` (honours
`XDG_CONFIG_HOME` and `OPENCODE_CONFIG`).
* `config_dir()`, `opencode_json()`, `perun_dir()`, `instructions_target()`,
  `agents_target()`.
* `AGENTS_BEGIN`, `AGENTS_END`, `INSTRUCTIONS_FILE`, `AGENTS_FILE`, `MCP_NAME`,
  `PERUN_SUBDIR` — the managed block markers and install names.
* `_INSTRUCTIONS_POINTER` — the thin pointer written to `perun/INSTRUCTIONS.md`
  so OpenCode's `instructions[]` array no longer duplicates the full kernel.
* `kernel_text()` — renders the **context kernel** from
  `perun/integrations/templates/INSTRUCTIONS.md` (with `{{KEY_TABLE}}` filled
  by `handbook.render_key_table()`); `_managed_block()`, `_strip_block()`,
  `_sync_agents_block()` (rewrites only the managed block, preserving
  everything the user wrote around it), `_sync_instructions_file()` (writes the
  thin `perun/INSTRUCTIONS.md` pointer idempotently).
* `_read_json()` (JSONC-tolerant; raises `ConfigParseError` rather than silently
  discarding an unparseable config), `_write_json()`, `_backup()` (uniquely named
  within a second), `_python_cmd()`, `_relative_to_config()`, `ConfigParseError`.
* `install()`, `uninstall()`, `status()`. The Perun MCP entry pins
  `environment.PERUN_HOME`, and `opencode.json` is rewritten (with a backup) only
  when its content actually changes. `install()` also calls
  `handbook.ensure_indexed()` so the reference material is retrievable
  immediately.
* Templates under `perun/integrations/templates/`:
  * `INSTRUCTIONS.md` — the **kernel template** (the always-on global block).
  * `command_perun-projects.md`, `command_perun-ask.md`, `command_logwork.md`,
    `command_update-docs.md` — the global slash commands.
  * `handbook/` — the reference docs behind the handbook keys:
    `handbook.tools` (generated at run time, **not** tracked on disk),
    `handbook.search`, `handbook.memory`, `handbook.commands`,
    `handbook.cross-project`, `handbook.sessions`, `handbook.onboarding`,
    `handbook.secrets`, and `handbook.global` (**the complete previous global
    guide, verbatim**).
* `perun/instructions/` (package data, not templates) — `perunchat_chat.md`,
  the strict PerunChat bridge persona embedded into the installed `perunchat`
  agent by `perunchat_bridge._ensure_opencode_agent()`.

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
  `bridge_task_exists()`, `bridge_task_status()`, `register_bridge_task()`
  (opt-in via `--task`), `remove_bridge_task()`, `restart_bridge_task()` (how a
  new Telegram bot takes effect: `schtasks /End` then `/Run`, touching only the
  task's own process).
* Orchestration: `machine_install()` (removes any existing task by default),
  `uninstall_machine()`.
* `_creationflags()` — `CREATE_NO_WINDOW` on Windows for `which_version`,
  `pip_install`, and `schtasks` calls so install/repair never flashes a console.

---

## MCP server

### `mcp/perun_mcp.py`
stdio MCP server (`perun`). 31 tools: `perun_list_projects`,
`perun_project_info`, `perun_create_project`, `perun_update_project`
(also accepts `agent_models` as a JSON string),
`perun_delete_project`, `perun_discover`, `perun_memory_search`,
`perun_memory_store`, `perun_memory_forget`, `perun_memory_supersede`,
`perunchat_room_memory`, `perun_docs_index`,
`perun_get_instructions`, `perun_set_instructions`,
`perun_list_instruction_files`, `perun_grep`, `perun_ast_grep`,
`perun_global_grep`, `perun_global_ast_grep`, `perun_unified_search`,
`perun_file_exists`, `perun_list_directory`, `perun_read_file`,
`perun_get_manifest`, `perun_set_manifest`, `perun_start_session`,
`perun_ask`, `perun_run_status`, `perun_telegram_send`,
`perun_context` (**retrieve context docs — a key, or the context for a task**),
`perun_secret` (**read/write a named secret in the encrypted store**).
* `_resolve_project()`, `_project_line()`, `_validate_projects_list()`,
  `_normalize_project_instruction_filename()`, `_project_instruction_file_path()`,
  `_safe_int()`, `_mcp_orchestrator()`, `main()`.
* Filesystem helpers: `_allowed_filesystem_roots()`,
  `_is_path_within_allowed_roots()`, `_validate_readable_path()`,
  `_validate_directory_path()` — scope read access to registered project paths,
  the Perun home/config trees, and the Perun runtime source tree.
* Calls `control.ensure()` at import time so the orchestrator always exists, and
  `handbook.ensure_indexed()` so the reference material is retrievable from the
  first call.

---

## Web dashboard `web/`

* `web/index.html` — app shell (sidebar, topbar, storm meter, `#perun-fx` FX
  canvas, `#perun-sigil` brand mark, view container, modal/toast roots, hidden
  `#model-root` datalist host).
* `web/css/perun.css` — design tokens & components (gold/dark theme), including
  wizard, folder-browser, structured-editor, manifest, `modelSelect`, chat, and
  the storm/FX styles (sigil eyes, storm meter, `.is-awake` / `.is-working`,
  `.card-pip`, `.perun-flash`), with a `prefers-reduced-motion` guard.
* `web/js/prompt_chain.js` — `buildPromptChain()` (Agent Console prompt/task
  composition, unit-testable).
* `web/js/perun.js` — SPA: `api()`, `toast()`, `modal()`, `confirmDialog()`,
  `router()`, `renderMarkdown()`, `modelSelect()`, `agentSelect()`, `multiSelect()`,
  `terminalId()`,
  `buildCommand()`, `launchInTerminal()`, the `perunFx` canvas engine (`flash()`),
  presence (`presence`, `computePresence()`, `pipState()`, `applyCardStates()`,
  `refreshPresence()`, `updateStormMeter()`), and the view renderers (`viewDashboard`,
  `viewOrchestrator`, `viewProjects`, `projectWizard`, `browseFolder`, `viewProject`,
  `mountGit`, `mountManifest`, `manifestOverview`, `openManifestPath()`,
  `openManifestLink()`, `wireManifestLinks()`, `projectInstructionsForm`,
  `viewConsole`, `viewMemory`, `viewActivity`, `viewMcp`, `viewTelegram`, `mountTelegram`,
  `viewPerunChat`, `viewSettings`), `mountConsole()`, `streamRun()`, `streamOrchestrator()`,
  `projectConfigForm()` (project Config tab: name, description, Plan model,
  Build model, Default model, Agent override; saves `agent_models` merged with
  existing per-agent defaults),
  `promptForUnlockPassword()` (dashboard modal for encrypted Telegram tokens).
  The MCP Servers view (`mcpCard()`, `probeMcp()`, `renderMcpProbe()`) lists the
  configured MCP servers and live-probes them on demand.
  Project cards (`projectCard()`) render `▶ Run` (primary TUI launch),
  `■ Stop`, `Open`, `Index docs`, and delete actions for every project; the
  orchestrator chat lives only in the sidebar (`Orchestrator Chat`).
  `viewPerunChat` always renders credentials, model/agent override dropdowns,
  an auto-join multi-select fed by `/api/perunchat/rooms` (+ custom room entry),
  the friends/requests/blocked cards (placeholder until an API key is set), and
  the chat panel with a discoverable-room datalist.
* `web/assets/Cinzel.ttf` — heading font (shared with the company site).
* `web/assets/perun.ico` / `perun.png` — gold-bolt app icon (favicon + Windows
  shortcut), generated by `tools/make_icon.py`.

---

## Root files

* `installer/` — installer source.
  * `PerunSetup.iss` — Inno Setup script that produces `PerunSetup.exe`.
  * `install-prereqs.ps1` — prerequisite installer (Python, git, OpenCode CLI,
    Ollama) and Perun machine setup; invoked by the wizard.
  * `uninstall.ps1` — reverses the global OpenCode integration and removes the
    `PerunBridge` task during uninstall.
  * `build.ps1` — compiles `installer\Output\PerunSetup.exe`.
  * `sign.ps1` — signs `PerunSetup.exe` with `signtool`. Supports an external
    PFX via parameters or the `PERUN_SIGN_PFX` / `PERUN_SIGN_PFX_BASE64` /
    `PERUN_SIGN_PASSWORD` environment variables; when none is provided it
    creates a self-signed certificate on demand (`New-SelfSignedCertificate` →
    export → sign), so no `.pfx` needs to exist in the repo for CI to pass.
  * `macos/` — unsigned `.pkg` installer for macOS.
    * `build.sh` — builds `installer/macos/Output/PerunSetup.pkg` with `pkgbuild`.
    * `perun.sh` — `/usr/local/bin/perun` wrapper that uses the installed venv.
    * `com.peruninthemachine.perun.plist` — system LaunchAgent plist that starts
      `perun serve --bridge` at user login.
    * `scripts/postinstall` — creates the venv, installs dependencies, wires the
      OpenCode integration for the console user, and loads the LaunchAgent.
    * `uninstall.sh` — manual uninstall script for pkg installs.
* `.github/workflows/release.yml` — GitHub Actions workflow that builds
  `PerunSetup.exe` (Windows) and `PerunSetup.pkg` (macOS) on `v*` tags and
  attaches them to the release.
* `setup.ps1` — Windows web installer: clones (or reuses) the repo and
  delegates to `bootstrap.ps1`. Designed to be run with
  `iwr ... | iex` straight from GitHub.
* `setup.sh` — Unix web installer: clones (or reuses) the repo, installs
  Python dependencies, and runs the global OpenCode integration.
* `bootstrap.ps1` — Windows cold-start installer for a new machine: locates
  Python 3.10+ (honours `PERUN_PYTHON`) and delegates to
  `python -m perun install --machine`; optionally creates the desktop shortcut.
* `Perun.cmd` — Windows one-click launcher (idempotent dashboard start; never
  starts a second Telegram bridge). Encrypted tokens no longer block startup;
  the dashboard prompts for the password once it is running.
* `run.py` — launcher (`python run.py serve`).
* `pyproject.toml` — packaging + `perun` console script + pytest config.
* `tools/release.py` — release pipeline: validates both repos, bumps every
  shipped version source (`perun/__init__.py`, `pyproject.toml`,
  `installer/PerunSetup.iss`, CODEMAP `__version__`), promotes `## Unreleased`
  entries into the new `## vX.Y.Z` section, updates the technical-doc
  latest-release/revision lines, syncs internal project content, then
  commits/tags/pushes public and internal.
* `tools/sync-internal.py` — mirrors internal-only project content
  (`perun.md`, `AGENTS.md`, `README.md`, `docs/`, `.perun/`, `.opencode/`) into
  the private `perun_internal` companion repo.
* `tools/make_icon.py` — regenerates `web/assets/perun.ico` / `perun.png`.
* `tools/normalize_line_endings.py` — rewrites tracked text files to LF (leaves
  `.cmd`/`.ps1`/`.bat`/`.iss` CRLF); run before tricky edits (see AGENTS.md).
* `tools/check_line_endings.py` — reports line-ending violations without
  rewriting.
* `docs/DESIGN.md` — what Perun is for and why it is shaped the way it is.
* `docs/TECHNICAL_DOCUMENTATION.md` — architecture, data model, HTTP API,
  subsystem reference and revision history.
* `docs/CHANGELOG.md` — the single source of truth for release history.
* `docs/AGENTS_OPTIMIZATION.md` — design proposal for the AGENTS.md
  context-budget problem: the *Perun Context* layer (keyed, vector-backed
  context docs), the generated AGENTS.md *kernel*, the `perun_context`
  retrieval primitive, and the anchor↔memory binding.
* `tools/migrate_global_facts.py` — one-off helper that moves Perun-related
  global facts onto the `perun` control-plane project.
* `perun.md` — this repository's own project manifest (identity, docs map,
  commands, anchors, and the `memory:` bindings into the Context layer).
* `.gitattributes` — the LF/CRLF rule that `tools/normalize_line_endings.py`
  and the "Editing files safely" convention depend on.
* `.perun/` — repo-local state: `context-map.json` (the losslessness map),
  `context/*.md` (the project-scoped context docs), and
  `instructions/git-workflow.md`.
* `.opencode/` — repo-local OpenCode commands (`code-audit.md`, `git.md`).
* `opencode.json` — repo-local OpenCode config (perun MCP + AGENTS.md).
* `requirements.txt`, `.gitignore`, `README.md`, `AGENTS.md`.

---

## Tests `tests/`

* `test_release_script.py` — release-pipeline changelog promotion: populated and
  empty `## Unreleased` sections, and idempotence when the target version
  already exists.
* `test_db_registry.py` — schema, projects/sessions/runs/activity CRUD.
* `test_discovery.py` — detection, classify, register, scan, manifest merge.
* `test_memory.py` — remember/sensitive/dedupe, CODEMAP-aware chunking,
  index, search, unified_search, prefetch_rules, usage stats, re-embedding,
  prune/forget/supersede (Ollama-independent).
* `test_context.py` — the Context layer: index/get/list/search (all four
  retrieval layers), anchor/stub resolution, outline/slice/prefetch, the
  deterministic stemmer, scope isolation, the sensitive-body guard, the
  stale-vector guard, embedding-assisted anchor/prefetch fallback, and the
  renderers.
* `test_context_map.py` — the losslessness proof: map building from an
  `AGENTS.md`, `inline`/`context`/`secret`/`retained` classification,
  `verify` (nothing deleted, nothing unaccounted for, every destination holds
  its content verbatim), and each failure mode.
* `test_secret_store.py` — named secrets: name normalization, scope filters,
  encrypted-at-rest write, refusal without a password, listing never returns
  values, delete, and the sensitive-data guard.
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
* `test_updater.py` — self-update machinery: version parsing, GitHub release
  checks, install-mode detection (git vs Windows installer), apply refusal paths,
  dirty-git guard, checksum verification, the detached runner plan, stale-PID
  cleanup, and the update API endpoints.
* `test_telegram_bot_api.py` — the dashboard's "switch to a new bot" flow:
  `/api/settings` never echoes the token, `/api/telegram/bot` verifies with a
  mocked `getMe` before saving, clears on `token: ""`, and `token_source`
  resolves config-over-env (network mocked — never contacts Telegram).
* `test_manifest.py` — parse/normalize/dump/save, JSON fallback, edge cases.
* `test_scaffold.py` — directory creation, bootstrap, starter `perun.md`.
* `test_git.py` — the `git` CLI wrapper against a temp repository.
* `test_orchestrator.py` — `parse_event`, `resolve_opencode`, `EventBus`,
  `compose_prompt` instructions injection, model resolution/forwarding
  (`model_explicit`, the `--model` argv rule for every source), and end-to-end
  stdout-error capture.
* `test_process_tracker.py` — `ProcessTracker` registration, lifecycle, marker
  resolution, watcher auto-finalization, and run-record cleanup.
* `test_agents.py` — OpenCode agent discovery (`perun/agents.py`).
* `test_daemon.py` — PID file lifecycle, run record round-trip,
  `is_another_perun_running()`, detached start, and `stop()`/`kill()`.
* `test_project_roots.py` — the explicit cross-project path resolver:
  registration, lookup, removal, and rejection of paths outside the root.
* `test_skills.py` — OpenCode skill/tool discovery (`perun/skills.py`).
* `test_telegram_lock.py` — mutex namespacing, side-effect-free `is_held()`, and
  the second-acquire-is-refused guarantee (the bridge can never multiply).
* `test_telegram_routing.py` - `_current_project` routing (single-project
  auto-select, control-plane fallback, `/orchestrator`).
* `test_routing.py` - the deterministic router (`perun/routing.py`): slug/name/
  tag resolution, Polish inflection and palatalisation, prefix collisions
  (`perun` vs `perunchat` vs `perun-in-the-machine`), ambiguity, action
  keywords, `stop loss` as a domain term, addressed detection, normalisation.
* `test_routing_wiring.py` - the router wired into the Telegram bridge:
  disabled-by-default behaviour, status/remember handled without a run,
  ambiguity asks, cross-project run rebinds `bridge_links`, unknown text still
  runs in the bound project, and the activity row omits the message text.
* `test_perunchat_bridge.py` — the autonomous PerunChat participant: start/stop
  lifecycle, room auto-join, reply dedupe, the write-ahead outbox and its
  reconnect flush, empty-reply retry, typing/reply sequence-number monotonicity,
  private-room alias handling, memory fact extraction, the `perunchat`
  agent installer, and external execution package reassembly.
* `test_package_handoff.py` — safe ZIP pack/unpack/validate/apply, caps,
  traversal/symlink/duplicate rejection, and target-scoped apply.
* `test_action_proposals.py` — proposal lifecycle, claim/revoke, expiry, and
  external-execution claim guards.
* `test_perunchat_proxy.py` — the `/api/perunchat/*` proxy layer: status
  (`agent_id`), key reveal restricted to loopback, room discovery, the trusted
  list, and gateway-failure degradation.
* `test_control.py` — control plane: `ensure()` / `is_control()` / `resolve()`,
  the delete guard, and "a run with no project targets the orchestrator".
* `test_server_security.py` — the origin/host guard (`_origin_guard`) on `/api/*`.
* `test_cli.py` — UTF-8 stdio hardening, non-ASCII recall output, restart flag
  replay, context stats/verify/dedupe, docs-audit drift detection, and the
  owner check used to include hidden projects in `--all` maintenance commands.
* `test_secrets.py` — password precedence (env → DPAPI file) and
  `persist_password()` (no-op off Windows).
* `test_providers.py` — provider discovery: `opencode auth list` parsing,
  `connected`/`status` enrichment, logout validation.
* `test_search.py` — unified search: RRF fusion, project resolution,
  capabilities, and graceful source failure/timeout handling.
* `test_grep.py` — the bundled search layer: ripgrep/ast-grep JSON parsing,
  Python fallbacks, `search_capabilities()`.
* `test_mcp_servers.py` — MCP config snapshot (global + project scopes, status
  derivation) and the on-demand live probe against a spawned throwaway server.
* `test_mcp_security.py` — MCP input validation: filename/path safety for
  instruction tools, pattern/max-results rejection in the search tools (loads
  `mcp/perun_mcp.py` directly, dodging the installed `mcp` package shadowing).
* `test_setup_scripts.py` — smoke tests for the `setup.ps1` / `setup.sh` web
  installers.
* `test_js_prompt_chain.py` — runs the Node.js unit test for
  `web/js/prompt_chain.js`.
* `test_prompt_chain.js` — the Node.js unit test itself (run by
  `test_js_prompt_chain.py`); skips when `node` is unavailable.
* `conftest.py` — shared fixtures: `_fresh_db` (autouse per-test database),
  `workdir`, `control_repo`, and `_api_token_header` (autouse: teaches
  `TestClient` to send `X-Perun-Token`, since state-changing `/api/*` calls now
  require it).
* `adv_memory.py`, `adv_grep_search.py`, `adv_registry.py`, `adv_server_api.py` —
  the external audit's adversarial suite (security/robustness regressions fixed in
  the Unreleased hardening pass). `pyproject.toml` collects `adv_*.py` alongside
  `test_*.py`; platform-specific cases skip themselves (Windows symlink privilege,
  POSIX-only permission bits).

---

## PerunChat federation

Perun has native PerunChat support. The wire protocol, gateway, REST API,
client SDK, and framework adapters live in the sibling `perun_chat` project.
Perun integrates at the harness/prompt layer (see `docs/TECHNICAL_DOCUMENTATION.md`
§10).

* Public dashboard / agent registration / API keys / how-to:
  `https://peruninthemachine.com/perunchat/`
* Public chat live view:
  `https://peruninthemachine.com/perunchat/public_chat.html`
* Gateway + SDK repository: `https://github.com/azagor/perun_chat`
* PerunChat project path on this machine: `C:\my_apps\perun_chat`
