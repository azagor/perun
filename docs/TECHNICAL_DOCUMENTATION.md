# Perun — Technical Documentation

**Latest release:** v0.7.0 (see §14). This release adds a Windows GUI installer
(`PerunSetup.exe`), web installers (`setup.ps1` / `setup.sh`), a CI release
workflow, model/agent overrides mirrored into project `opencode.json`, a new
**Providers** tab with OpenCode auth status, slash-command skill tasks in the
Agent Console, and skill-name validation. `perun.__version__` reports `0.7.0`.

**Latest release details (v0.6.4):** instruction text lives *only* on disk. Projects keep custom
instructions as Markdown files under `.perun/instructions/`, each with YAML front matter
(`name`, `enabled`); enabled files are injected into new agent sessions in sorted filename
order. The legacy `projects.instructions` / `projects.instructions_path` columns and both
one-off migrations (project and global) have been removed, along with the
`perun_migrate_project_instructions` MCP tool. `db._migrate()` drops the retired columns from
older databases on next start. A single shared writer
(`orchestrator.write_project_instruction_file()`) is used by the project API, MCP tools,
discovery, and manifest sync. The MCP tools `perun_get_instructions`, `perun_set_instructions`,
and `perun_list_instruction_files` operate only on the multi-file system.
v0.6.4 also fixes `/api/run-in-terminal`: its `cmd`/`wt` branches hand the frontend-built
command text to `cmd /k` as raw text, because cmd.exe cannot parse the C-runtime `\"` quoting
that `subprocess.list2cmdline()` produces.

The obsolete inline global instruction fields `orchestrator_instructions` and
`global_agent_instructions` are not part of `Settings` and are never read. The dashboard
settings UI manages the instruction file list only. Manifest `instructions` from the HTTP PUT
and the `perun_set_manifest` MCP tool are routed through `projects.sync_manifest()` to
`.perun/instructions/default.md`; `db._migrate()` tolerates a failed `DROP COLUMN` (SQLite
< 3.35); and the persisted password is now Windows **DPAPI**-encrypted in `<PERUN_HOME>/secret`
instead of plaintext in the user environment. Builds on v0.6.2 (bundled `perun_grep` /
`perun_ast_grep` code-search tools and matching HTTP endpoints, TUI runs, reusable tasks) and
v0.6.3 (multi-file instruction system). v0.6.4 also hardens the bundled code-search layer: a
ripgrep/ast-grep "no matches" exit code (`1`) is now a successful empty result instead of
triggering the pure-Python fallback, and the unsupported `--files-from` flag is gone (global
searches pass project paths positionally). `perun.__version__` reports `0.7.0`.

---

## 1. Overview

Perun is a single local service (Python 3.10+) that sits *on top of* OpenCode.
It has no cloud dependency except an optional Telegram bot and an optional local
Ollama instance for embeddings.

### Process model

| Concern | Implementation |
|---|---|
| HTTP API + dashboard | FastAPI/Starlette app in `perun/server.py`, served by uvicorn |
| Live run output | Server-Sent Events (`/api/runs/{id}/stream`, `/api/stream`) |
| Persistence | one WAL-mode SQLite DB at `~/.perun/perun.db` |
| Agent execution | `subprocess` of `opencode run …`, one thread per run, semaphore-capped |
| Embeddings | local Ollama `nomic-embed-text` via `/api/embed` |
| LLM providers | read-only view of `opencode auth list` (`/api/providers`); Perun does not store credentials |
| Telegram | a long-polling thread (`PerunTelegramBridge`) |
| Global integration | stdio MCP server `mcp/perun_mcp.py` + `~/.config/opencode` merge |
| One-click startup (Windows) | `Perun.cmd` — idempotent dashboard launcher; never starts a second Telegram bridge |
| Machine install (Windows) | `PerunSetup.exe` (wizard), `setup.ps1` (web), or `bootstrap.ps1` (local) → `python -m perun install --machine` (`perun/installer.py`): prerequisites, deps, global install, and (opt-in) the `PerunBridge` task |

### Runtime layout (`~/.perun`, override with `PERUN_HOME`)

```
~/.perun/
  perun.db        registry + facts + doc_chunks + vectors
  config.json     Settings (Telegram token may be stored as `enc:<base64>`)
  secret          DPAPI-encrypted password (Windows; written by "Remember on this PC")
  logs/           telegram.log, service logs
  runs/           <run_uid>.log — raw opencode stdout per run
```

### One-click launcher (Windows)

`Perun.cmd` at the repo root is the double-click entry point. It finds a Python
interpreter (`PERUN_PYTHON` → `C:\Python314\python.exe` → `py -3` → `python`),
resolves the port from `~/.perun/config.json` (`PERUN_PORT` overrides it), then:

* if something is already listening on the port, opens the dashboard and exits —
  so repeated double-clicks never spawn a second server;
* otherwise prints a banner, opens the browser, and runs
  `python -m perun serve --port <N>` in the foreground (close the window or
  Ctrl+C to stop).

`Perun.cmd` starts the dashboard and the Telegram bridge **in-process**
(`python -m perun serve --bridge`). It first checks `perun status --json` to see
whether the bridge mutex is already held by a live process; only then does it
skip the in-process bridge. This makes the launcher resilient to a crashed or
stale `PerunBridge` scheduled task. By default there is no logon task; the
bridge runs only while the dashboard is open. The optional `PerunBridge` logon
task can be registered with `--task` (§10).

The desktop shortcut's `.ico` is generated deterministically by
`tools/make_icon.py`.

---

## 2. Data model

Single SQLite DB, WAL mode, one short-lived connection per operation
(`db.db()`), so the API threads, orchestrator threads, bridge, and MCP server
can all write concurrently.

| Table | Purpose | Notes |
|---|---|---|
| `projects` | registry | `slug` unique, `path` unique, `kind` ∈ {opencode,git,dir}, `favorite`, `enabled`, `model`, `agent`, `manifest` (JSON snapshot), `manifest_path`, `manifest_loaded_at`, `git_remote`, `git_branch` |
| `sessions` | Perun's record of OpenCode sessions | `opencode_session_id`, `message_count`, `status` |
| `runs` | one agent invocation | `run_uid` unique, `status` ∈ {queued,running,done,failed,cancelled,timeout,provider_error}, `model` (resolved), `model_explicit` (1 when `--model` was explicitly requested), `agent`, `output`, `error` |
| `activity` | event feed | `kind`, `message`, `meta` JSON |
| `memory` | facts | `scope` ∈ {global,project}; **`scope='global'` rows use `project_id=0`**; `UNIQUE(scope,project_id,content_hash)` |
| `doc_chunks` | document vectors | `UNIQUE(project_id,source_file,section)` |
| `settings` | key/value scratch | |
| `bridge_links` | Telegram routing | `chat_id` → `project_id` + `session_pk` |

Column additions after v0.1.0 are applied idempotently by `db._ADDED_COLUMNS`
(`manifest`, `manifest_path`, `manifest_loaded_at`, `git_*`, `runs.model_explicit`), so existing
databases migrate in place. Columns retired in v0.6.4 (`projects.instructions`,
`projects.instructions_path`) are dropped idempotently by `db._REMOVED_COLUMNS` on next start, so
fresh and upgraded schemas converge. Instruction text is never stored in the database.

**Why `project_id=0` for global facts:** SQLite treats `NULL`s as distinct, so a
`UNIQUE(scope, project_id, content_hash)` constraint would not dedupe global
rows if the id were `NULL`. Using `0` keeps the constraint meaningful.

---

## 3. Memory subsystem

Two stores, one search surface (`memory.unified_search`):

1. **Facts** — agent/CLI-authored atomic memories (`remember`). Scoped
   `global` (visible everywhere) or `project`. Guarded by `is_sensitive()`,
   deduped by `content_hash()`, and freshness-weighted (`_freshness_weight`,
   30-day half-life) at query time.
2. **Doc chunks** — Markdown from a project's `DOC_CANDIDATES`
   (`docs/CODEMAP.md`, `docs/TECHNICAL_DOCUMENTATION.md`, `docs/CHANGELOG.md`,
   `docs/README.md`, `AGENTS.md`, `README.md`). `chunk_markdown()` splits at
   level-2 headings; `index_text()` upserts changed sections only;
   `extract_metadata()` tags each chunk with CODEMAP-style file paths and
   identifiers so search results navigate straight into code.

**Ranking.** Cosine similarity over Ollama embeddings, plus keyword bonus, times
freshness. Chunks also pick up a small keyword bonus. Results with score ≤ 0.15
are dropped.

**Degradation.** If Ollama is unreachable, `_embed()` returns `None`, rows store
without an embedding, and search falls back to keyword + recency. `stats()`
reports `ollama_ok`.

**Legacy import.** `import_legacy_memory()` copies facts from the old shared
`~/.config/opencode/memory.db` into global Perun scope (skipping stale CODEMAP
"Subsystem:" rows, which are re-indexed per project instead).

---

## 4. Discovery

`discovery.detect()` classifies a directory via cheap marker stats:

* **opencode** — has `.opencode/`, `opencode.json(c)`, `AGENTS.md`, or `docs/CODEMAP.md`
* **git** — has `.git/`
* **dir** — anything else (listed, not auto-registered)

`scan()` walks at most `max_depth` under each root, skipping `SKIP_DIRS`
(`node_modules`, `.venv`, `dist`, …) and dotted dirs. `discover_and_sync()`
registers only `opencode`/`git` projects, updating existing ones by path
(`register()`). `register_self()` always registers the Perun repository itself.

`register()` also reads a project **manifest** (see §6). Fields a manifest
*declares* (`name`, `kind`, `description`, `tags`, `model`, `agent`,
`instructions`, `git`) override the heuristics; undeclared fields keep their
discovered/registry value. The parsed manifest is snapshotted into the
`projects.manifest` column.

---

## 5. Project management

**Creation wizard** (`POST /api/projects`, `web/js/perun.js: projectWizard`). A
single endpoint handles three methods (the reusable create/register/sync logic
lives in `perun/projects.py` and is shared with the MCP tools):

* `existing` — register a path (may not exist yet; created lazily on first use);
* `new` — create the directory and optionally **bootstrap** a starter project
  (`scaffold.bootstrap`: `opencode.json`, `AGENTS.md`, `README.md`, `.gitignore`,
  `perun.md`, `docs/CHANGELOG.md`, `docs/TECHNICAL_DOCUMENTATION.md`,
  `docs/CODEMAP.md`, and `.opencode/`). The generated `AGENTS.md` points to
  `perun.md` as the primary source of truth; `perun.md` includes a `docs` map,
  suggested commands, and anchors to dependency/build/CI files discovered on disk.
* `clone` — `git.clone(url, path)` into the target.

Optional steps: `git_init` (`scaffold`/`git.init`), `git_remote` + `git_branch`
(`git.set_remote`), and `index_docs` (`memory.index_project_docs`). After
registration the manifest is merged (`_sync_manifest`) so the dashboard reflects
it immediately.

**Backfill docs for existing projects**
(`python -m perun backfill-docs [slug] [--all]`). The `scaffold.backfill()`
helper writes the same scaffold files but only where they are missing, so it is
safe to run on existing projects. It is useful for bringing older projects up
to the current documentation standard. A server-backed **folder browser** powers the path pickers:
`GET /api/fs/list?path=&show_hidden=` returns `roots`, `home`, `parent`, `dirs`,
`files`; `POST /api/fs/mkdir {path}` creates a directory.

**Per-project Git** (`perun/git.py`). Wraps the `git` CLI with explicit,
non-interactive arguments (`GIT_TERMINAL_PROMPT=0`), validated remote URLs, and a
structured `info()` (`repo`, `branch`, `dirty`, `change_count`, `ahead`,
`behind`, `remote_url`). It **never** rewrites a working tree implicitly —
`init`/`remote`/`commit`/`push` are explicit user actions.

**Custom project instructions** (`.perun/instructions/*.md`). Project-scoped rules
injected only when a **new** session starts: `orchestrator.compose_prompt()`
prepends

```
[PERUN PROJECT INSTRUCTIONS]
…rules…
[END PERUN PROJECT INSTRUCTIONS]

<user prompt>
```

(The previous XML-style delimiters were dropped because models interpreted them
as markup and sometimes discarded the prompt that followed.)

Project instructions are stored as Markdown files under `.perun/instructions/`
with YAML front matter (`name`, `enabled`). Enabled files are included in sorted
filename order. HTTP endpoints: `GET /api/projects/{slug}/instruction-files`,
`GET /api/projects/{slug}/instruction-files/{filename}`,
`POST /api/projects/{slug}/instruction-files/{filename}`,
`PATCH /api/projects/{slug}/instruction-files/{filename}/toggle`,
`DELETE /api/projects/{slug}/instruction-files/{filename}`. Filenames must be safe
(no `/`, `\`, `..`) and are normalized to `.md`.

**Single writer.** All project instruction files are written by
`orchestrator.write_project_instruction_file(project_path, text, *, name, filename, overwrite)`.
It is used by the project API, the MCP tools, discovery, and manifest sync.
Manifest-declared instructions use `overwrite=False` (seed once), so a later dashboard edit is
never clobbered; explicit user writes use `overwrite=True` (default). Instruction text is never
stored in the database — there is no legacy column and no migration.

Continuations of an existing session inherit the rules from history, so they are
not re-injected. The raw user prompt is what gets stored on the run row.

**Model/agent overrides are mirrored into the project's OpenCode config.**
Saving a model or agent override on a project (dashboard config tab, wizard, or
`perun_update_project` MCP tool) now writes the values into the project's own
`opencode.json[c]` under the keys `model` and `default_agent`. This lets the
OpenCode **TUI** honor the override, because the TUI reads project defaults from
that file rather than command-line flags. Existing keys (`mcp`, `instructions`,
…) are preserved; clearing an override removes the key. Writes are atomic
(temp file + `os.replace`) and commented `.jsonc` files are left untouched so
user comments are not destroyed. `projects.sync_opencode_config()` returns an
`{action, path, warning}` status that the dashboard surfaces in a toast.

**Model/agent overrides are explicit only.** Perun resolves model/agent as
`explicit call/project field → control-plane settings`, but the **global**
`default_model` / `default_agent` are no longer forced onto ordinary project runs.
That lets OpenCode use its own project defaults when the Model/Agent fields are
left empty, avoiding surprising provider errors from a machine-wide default. The
run row records whether the model was explicit (`model_explicit=1`) so `_execute`
knows whether to pass `--model` to `opencode run`.

### The control plane (always-available orchestrator)

### The control plane (always-available orchestrator)

A machine with **nothing registered** must still be reachable, so Perun registers
*itself* as a protected, always-present project — the **control plane**
(`perun/control.py`). It is identified by its path (`config.is_control_path()` ==
the Perun repo root, `config.REPO_ROOT`), not by a reserved slug, so it can never
be confused with — or deleted as — a user project.

* `control.ensure()` is idempotent and called on server startup (lifespan), on
  every Telegram `/projects`, and at MCP-server import; `control.resolve()`
  returns the control project dict. It re-creates the row if the registry is
  wiped, so the orchestrator survives.
* `registry.project_dict()` sets `is_control` on the row; the dashboard badges it
  `orchestrator`, links its card to `#/orchestrator`, and hides its delete
  button. `DELETE /api/projects/{slug}` and the MCP `perun_delete_project` tool
  refuse it.
* `POST /api/runs` with no `project` targets the control slug, and `/api/health`
  reports it as `control`.

**Orchestrator instructions** are scoped Markdown files under
`~/.config/opencode/perun/instructions/` with `scope: orchestrator`. They are
the control-plane analogue of project instructions: a machine-wide persona/rules
block injected ahead of every orchestrator prompt. `compose_prompt(project,
prompt, settings=…)` loads them only when the target project `is_control`.

**Managing global instruction files from the CLI.** `python -m perun
global-instructions` lists the global instruction files (name, scope, enabled,
body); `python -m perun global-instructions "<text>"` writes `custom.md` with
`scope: all_projects`; `python -m perun global-instructions --clear` deletes every
custom file and re-seeds the default guidance block.

---

## 6. Manifest (`perun.md`)

A project can describe itself with a **`perun.md`**: a Markdown file whose
**YAML front-matter** (parsed by the already-required PyYAML) holds structured
data, followed by a free-form Markdown body shown as the project overview.

```markdown
---
version: 1
name: Trading Bot
kind: opencode
description: Android crypto trading bot.
tags: [python, kivy, trading]
model: opencode/big-pickle
docs: { readme: README.md, changelog: docs/CHANGELOG.md }
commands: { test: "python -m pytest tests/ -q" }
links: { repository: "https://github.com/you/repo" }
anchors:
  - { label: Entry point, kind: file, path: main.py }
  - { label: Pipeline, kind: symbol, path: services.py, symbol: TradingEngine }
  - { label: Site, kind: url, url: "https://example.com" }
  - { label: Run tests, kind: command, command: test }
---

# Trading Bot

Free-form notes rendered on the project page.
```

* **Anchor kinds:** `file`, `doc`, `symbol`, `url`, `command`. Each anchor needs
  a `label` and one locator (`path` / `url` / `command`). Shorthand: a string
  anchor becomes a `file` anchor; `kind` is inferred when omitted.
* **JSON fallback:** a plain `perun.json` is accepted; if a project uses JSON,
  `save()` writes JSON back (the Markdown body is stored under a `body` key) and
  `perun.md` is never created alongside it. `perun.md` wins if both exist.
* **Declarative input, explicit writes.** The server only *reads* the manifest
  (on discovery, index, wizard, or refresh). Disk writes happen only through
  explicit `POST …/manifest/export` (generate from registry; 409 if one already
  exists) or `PUT …/manifest` (from the editor / MCP).
* **Tolerant & forward-compatible.** `normalize()` validates and repairs, and
  **preserves unknown keys**; malformed YAML/JSON degrades to defaults with
  warnings instead of failing. Anchors missing a locator are dropped with a
  warning. Paths are expected relative to the project root.
* `dump()` omits empty defaults so generated files stay readable.

---

## 7. Orchestrator

`Orchestrator.start_run(project, prompt, …)`:

1. resolves model/agent: explicit call arg → project override → (when the target
   `is_control`) `Settings.orchestrator_model` / `orchestrator_agent`. The global
   `default_model` / `default_agent` are used **only for the control plane**, not
   for ordinary projects, so an empty Model/Agent field lets OpenCode use its own
   project defaults. The run records `model_explicit=1` when a model was
   explicitly chosen.
2. writes a `runs` row (`status=queued`) and an activity entry,
3. spawns a daemon thread running `_execute`, guarded by
   `threading.Semaphore(max_concurrent_runs)`.

`_execute` runs (Windows-aware via `resolve_opencode`):

```
opencode run --format json --auto [--model M only if model_explicit] [--agent A only if explicit]
             [--session <oc_id>] --title "Perun · …" -- <prompt>
```

For **TUI** launches (`POST /api/run-in-terminal`, project-card "Run" button,
Agent Console "Run"), the dashboard builds a base `opencode` command and
passes only flags accepted by the TUI (`--prompt` and `--session`); `--model` and
`--agent` are omitted because they belong to `opencode run`, not the base
`opencode` command. Model and agent resolution for TUI sessions is delegated to
OpenCode's own project defaults (including the values mirrored into
`opencode.json` by `sync_opencode_config()`).

`--model` is passed only when `model_explicit=1`; `--agent` is passed only when
an agent value is set. With `cwd` set to the **target project path** — no
positional directory argument. stdout is streamed line-by-line to
`~/.perun/runs/<uid>.log`, parsed by `parse_event()`, and fanned out through
`EventBus` as normalized events:

| event | meaning |
|---|---|
| `queued` / `start` | accepted / process launched |
| `session` | learned the OpenCode session id |
| `text` | assistant text delta |
| `tool` | tool invocation + status |
| `error` / `provider.*` | error frame; `provider.*` events are treated as provider-side errors |
| `end` | terminal: `status`, `exit_code`, `reply`, `error` |

Error frames observed on stdout are also collected and, when a run ends with no
reply, joined into the run's persisted `error` field so a finished failure stays
explainable in the dashboard. Provider-side errors that occur **after** a session
id has been learned (`provider.quota`, `provider.invalid-request`, etc.) set the
run status to `provider_error` rather than `failed`, and the `sessions` row stays
`active` so the user can continue the session once the provider issue is resolved.

`_finalize()` writes the terminal status, keeps the `sessions` row in sync
(creating one on first sight of a new OpenCode session id), and publishes `end`.
`cancel()` kills the process tree (`terminate_tree` → `taskkill /T` on Windows).

`run_and_wait()` blocks on a per-run `threading.Event` and returns
`{run, reply}` — used by the MCP `perun_ask` tool and the Telegram bridge.

---

## 8. HTTP API

All responses are `{"ok": true, …}`; errors are `HTTPException` JSON.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | version, uptime, project count, active runs, `control` slug |
| GET | `/api/models` | structured `{id,provider,name,label}` entries from `opencode models` (+ `providers`, `count`) |
| GET | `/api/providers` | LLM providers from `opencode auth list --format json` with `connected`/`status` (+ `count`, `error`); Perun does not store credentials |
| POST | `/api/providers/{provider_id}/logout` | disconnect a provider via `opencode auth logout`; returns `{ok, error}` |
| GET | `/api/agents` | OpenCode agents: built-ins (`build`,`plan`,`general`,`explore`) + user-defined agents on disk (+ `count`) |
| GET | `/api/skills` | OpenCode skills: global `~/.config/opencode/skills/` + project-local `.opencode/skills/` + runtime built-ins (`code-audit`, `android-design-guidelines`, `find-skills`) (+ `count`) |
| GET | `/api/tools` | OpenCode tools: built-ins + custom `.ts/.js/.mjs` under `~/.config/opencode/tools/` and project-local `.opencode/tools/` (+ `count`) |
| GET | `/api/mcp/servers` | every MCP server OpenCode loads (global `~/.config/opencode/opencode.json[c]` + per-project `opencode.json[c]`) with a config-derived `status`, transport, resolved executable, and `shadowed_by` (+ `count`, `counts`); duplicate names across config-file variants collapse to one and unparseable files appear in `parse_errors`; config only, nothing is spawned |
| POST | `/api/mcp/servers/probe` | live-probe one configured MCP server `{name,scope:"global"\|"project",project?}` — spawns a local server (or calls a remote URL) and runs the MCP `initialize` + `tools/list` handshake, returning `status`, `tools`, `latency_ms`, and any `stderr`; the command is read from disk, never the request. Gated and timeout-capped, so a probe may return `status:"busy"` when too many run at once |
| GET | `/api/projects` | list projects |
| POST | `/api/projects` | wizard: register / create / clone `{method,path,…}` |
| GET | `/api/projects/{slug}` | detail + sessions + runs + activity + memory + manifest body |
| PATCH | `/api/projects/{slug}` | update fields; when `model`/`agent` change, also mirrors them into the project's `opencode.json[c]` and returns the result in `opencode_config` |
| DELETE | `/api/projects/{slug}` | unregister (refuses the control project) |
| POST | `/api/projects/{slug}/index` | sync manifest, then index docs |
| GET | `/api/projects/{slug}/sessions` | sessions |
| POST | `/api/projects/{slug}/session` | start an OpenCode session for this project; `prompt` is optional |
| DELETE | `/api/sessions/{pk}` | delete a session |
| GET | `/api/projects/{slug}/activity` | project activity |
| GET | `/api/projects/{slug}/memory` | project facts + stats |
| GET/PUT | `/api/projects/{slug}/instructions` | project-scoped instructions; GET returns the effective combined text; PUT writes `{instructions}` to `.perun/instructions/default.md` (or `{filename}`) with YAML front matter |
| GET | `/api/instructions` | list global instruction Markdown files (name, scope, enabled) |
| GET | `/api/instructions/dir` | absolute path to the global instructions directory |
| GET | `/api/instructions/{filename}` | read one global instruction file body + metadata |
| POST | `/api/instructions` | create/update a global instruction Markdown file |
| PATCH | `/api/instructions/{filename}/toggle` | enable/disable a global instruction file |
| DELETE | `/api/instructions/{filename}` | delete a global instruction file |
| POST | `/api/search/grep` | regex content search across a project (`pattern`, optional `case_sensitive`, `whole_word`, `include`, `max_results`) — uses ripgrep with Python fallback |
| POST | `/api/search/ast-grep` | structural code search across a project (`rule`, optional `max_results`) — uses ast-grep with Python-AST fallback |
| POST | `/api/search/global-grep` | regex content search across all registered projects (`pattern`, optional `projects`, `case_sensitive`, `whole_word`, `include`, `max_results`) — uses ripgrep with Python fallback |
| POST | `/api/search/global/ast-grep` | structural code search across all registered projects (`rule`, optional `projects`, `max_results`) — uses ast-grep with Python-AST fallback |
| POST | `/api/search/unified` | unified cross-project search (`query` for memory, `pattern` for grep, `rule` for ast-grep); fuses and re-ranks results |
| GET | `/api/projects/{slug}/manifest` | manifest (`?live=true` reads disk) |
| POST | `/api/projects/{slug}/manifest/refresh` | re-read disk, merge to registry |
| POST | `/api/projects/{slug}/manifest/export` | generate manifest from registry (409 if exists) |
| PUT | `/api/projects/{slug}/manifest` | write manifest `{content}` or `{data,body}` |
| GET | `/api/projects/{slug}/git` | git status `info()` |
| GET | `/api/projects/{slug}/git/log` | recent commits |
| POST | `/api/projects/{slug}/git/init` | initialise repository |
| POST | `/api/projects/{slug}/git/remote` | set/validate remote |
| POST | `/api/projects/{slug}/git/commit` | commit all changes |
| POST | `/api/projects/{slug}/git/push` | push branch |
| GET | `/api/fs/list` | folder browser listing |
| POST | `/api/fs/mkdir` | create a directory |
| POST | `/api/run-in-terminal` | open the configured terminal/shell with a command `{cwd,command,shell?,run_uid?}` (or pass `project` instead of `cwd`); `run_uid` links the terminal to a TUI run record for lifecycle tracking |
| GET | `/api/projects/{slug}/tasks` | list reusable project tasks |
| POST | `/api/projects/{slug}/tasks` | create a task `{name,kind,prompt,tools[],skills[],enabled}`. `kind` is one of `prompt`, `skill`, `tool`, `combo`. Skill tasks are emitted as `/<skillname>` slash commands by `web/js/prompt_chain.js` |

| PATCH | `/api/projects/{slug}/tasks/{task_id}` | update a task |
| DELETE | `/api/projects/{slug}/tasks/{task_id}` | delete a task |
| POST | `/api/discover` | scan roots & register |
| GET | `/api/discover/scan` | dry-run scan |
| GET | `/api/runs` | recent runs (`?project=`) |
| GET | `/api/runs/active` | queued/running runs joined with their project (the live storm layer) |
| POST | `/api/runs` | start a headless run `{project,prompt?,model?,agent?,session_pk?}`; `project` is optional and defaults to the orchestrator; an omitted `prompt` uses a default starter prompt |
| POST | `/api/runs/tui` | create a lightweight `running` run record for an externally-launched TUI session `{project,prompt?,model?,agent?}` |
| GET | `/api/runs/{run_uid}` | run status/output |
| POST | `/api/runs/{run_uid}/cancel` | cancel a headless run **and** terminate a tracked TUI/terminal session |
| POST | `/api/runs/{run_uid}/finish` | finalize a TUI run record (e.g. user closed the window manually) |
| POST | `/api/runs/{run_uid}/kill` | terminate the tracked terminal/TUI process for a run and finalize it |
| GET | `/api/runs/{run_uid}/stream` | **SSE** run events |
| GET | `/api/stream` | **SSE** global run events + activity |
| POST | `/api/memory/remember` | store a fact |
| POST | `/api/memory/search` | unified search |
| GET | `/api/memory/stats` | counts + `ollama_ok` |
| GET | `/api/memory/recent` | recent facts |
| POST | `/api/memory/import-legacy` | migrate legacy facts |
| POST | `/api/memory/prune` | drop old non-global facts |
| GET | `/api/activity` | global feed |
| GET/PATCH | `/api/settings` | read/update `Settings` (GET returns `telegram_token: ""` — never the value — plus `telegram_token_set`) |
| GET | `/api/telegram/status` | bot + allowed chats + `token_source`/`chats_source` + `bridge_running` + `bridge_task` + `platform` |
| POST | `/api/telegram/bot` | `{name?,token?,chats?,password?}` — verifies a new token with `getMe` **before** saving; `password` encrypts the token; `token: ""` clears; `{restart_required}` |
| POST | `/api/telegram/bot/change-password` | `{old_password,new_password}` — decrypt and re-encrypt the stored token with a new password; you still need to restart the bridge so it loads the re-encrypted token |
| POST | `/api/telegram/bridge/restart` | Reloads the in-process bridge token if the dashboard owns it; otherwise `schtasks /End` + `/Run` so the optional `PerunBridge` task re-reads the bot |
| POST | `/api/telegram/unlock` | `{password,remember?}` — decrypt and unlock an encrypted stored token, set `PERUN_PASSWORD` for the running process, optionally persist it (Windows DPAPI-encrypted in `<PERUN_HOME>/secret`), then start/reload the bridge; if another process owns the bridge lock and `remember` is false, `bridge.reason` explains why |
| POST | `/api/telegram/send` | send a message |
| GET | `/` etc. | static dashboard from `web/` |

`GET /api/runs/active` is registered **before** `GET /api/runs/{run_uid}` so the
literal path is not swallowed by the parameterised route.

### Integrated code search

The `/api/search/*` endpoints and their MCP equivalents are backed by a bundled
search layer (`perun/grep.py` for a single backend, `perun/search.py` for fusion),
so projects get code search without installing extra MCP servers. Every backend
degrades to a pure-Python implementation when its CLI is missing.

| Backend | Fast path | Fallback | Match shape |
|---|---|---|---|
| Regex content | `ripgrep` (`rg --json`) | `grep._python_grep` | `{file,line,column,text,match}` |
| Structural/AST | `ast-grep` (`sg run --lang py`, or `sg scan` for YAML rules) | `grep._python_ast_search` (Python) | same |
| Semantic | Ollama embeddings via `memory` | keyword + recency | `{project,file,section,text,score}` |

* **Result envelope.** Every match is normalized to `{ok, matches:[…], tool}` (plus
  `projects_searched` for the global variants); `tool` names the backend actually
  used (`rg` / `rg-global` / `python-grep` / `ast-grep` / `ast-grep-global` /
  `python-ast` / `python-ast-global`).
* **Exit-code semantics.** `rg` and `sg` exit `0` with matches and `1` with none;
  an exit of `1` is a **successful empty result**, not an error. Any other non-zero
  code (e.g. `2` for a bad flag, `8` for an invalid ast-grep rule) falls through to
  the Python fallback, and an invalid rule surfaces as `error_kind: "invalid_input"`.
* **Positional paths only.** Both tools receive project directories as positional
  arguments — neither supports a `--files-from` path-list flag. A command line too
  long for the OS simply fails and is served by the Python fallback.
* **Caps & token frugality.** `MAX_RESULTS = 200`, lines truncated to 160 chars, and
  subprocess output capped at 16 MiB; matches are de-duplicated before returning.
* **Unified fusion.** `search.unified_search()` runs the active backends in parallel
  (a 3-worker `ThreadPoolExecutor`, 60 s per backend) and fuses the ripgrep and
  ast-grep rankings with **Reciprocal Rank Fusion** (`_rrf_fuse`, `k = 60`),
  deduplicating by `(project, file, line[, column])`. Memory results are returned
  separately. The executor is shut down **without waiting**, so a hung backend
  cannot block the response past its per-future timeout.

### SSE notes

SSE endpoints are `async` and wrap the blocking `EventBus`/`Orchestrator.stream`
with `starlette.concurrency.iterate_in_threadpool`. The global stream merges run
events with polled `activity` rows. `X-Accel-Buffering: no` disables proxy
buffering.

### Access control

The API is **local and unauthenticated by design**. Because it can start agent
runs (`POST /api/runs`, executed with `--auto`) and browse the filesystem, an
`_origin_guard` middleware (added in v0.2.1) rejects any `/api/*` request whose
`Host` header does not address this machine (loopback, or the configured bind
address) — defeating DNS rebinding — and rejects a request whose browser-supplied
`Origin` does not resolve to the same host. Static dashboard assets are not
guarded. Binding to a non-loopback address still works but logs a warning; expose
it only on a trusted network.

---

## 9. Telegram bridge

`PerunTelegramBridge` long-polls `getUpdates`. Routing state lives in
`bridge_links` (chat → project → session), not a side file. Chat→project
resolution (`_current_project`) keeps the single-user-project auto-select (the
control plane is excluded from it) and otherwise falls back to the
**orchestrator** (`control.ensure()`), so a chat can always be answered even with
nothing registered.

Commands: `/start` `/help`, `/projects` (lists the orchestrator first),
`/project <name|#>`, `/orchestrator` (`/perun`, `/overseer`) to switch to the
control plane in place, `/new` `/reset`, `/status`, `/stop` `/cancel`,
`/remember <fact>`, `/recall <query>`, `/index`. Anything else is treated as a
prompt and answered by a Perun-orchestrated run, with a typing indicator and
reply threading. `format_html()` renders a safe subset of Markdown into Telegram
HTML; `chunk()` splits at 3900 chars.

**Exactly one poller.** `getUpdates` allows a single consumer per bot token, so
two bridges would fight with HTTP 409s. Single-instance is enforced machine-wide
by `telegram.PidLock` (a named Windows mutex namespaced by `PERUN_HOME`; a POSIX
pid-file) and honoured by both `run_bridge()` (the `bridge` CLI, and the optional
`PerunBridge` logon task) and `_start_bridge_thread()` (`serve --bridge`, which now
keeps the lock referenced for the process lifetime). A second attempt refuses
instead of poller-fighting, and the `PerunBridge` task is registered with
`IgnoreNew`.

### Switching to a different bot

The bridge resolves its token **once, at startup** (`run_bridge()` →
`Settings.telegram()`), so a token change only takes effect on a restart.

| Concern | How it is handled |
|---------|-------------------|
| **Edit name / API key / chat ids** | `POST /api/telegram/bot` — the Telegram page's "Bot settings" card. The name is a **local label** (`Settings.telegram_bot_name`) used to tell bots apart; it is not pushed to Telegram. |
| **Verify before trusting** | A non-empty token is checked with Telegram's `getMe` **before** `Settings.save()`, so a typo can never silently replace a working bot. `token: ""` clears it (Disconnect). |
| **Token never leaks** | `GET /api/settings` returns `telegram_token: ""` plus `telegram_token_set`; the UI's password field is only ever a placeholder. The bridge side keeps the token out of logs. |
| **Env fallback** | `Settings.telegram()` prefers `config.json`, then `TELEGRAM_BOT_TOKEN` / `TG_BOT_TOKEN` (chats: `TELEGRAM_CHAT_ID` / `TG_CHAT_ID`). Because of that, `Settings.telegram_sources()` reports which source is in force, and the dashboard warns + disables **Disconnect** when an env token would silently reappear. |
| **Apply the change** | `POST /api/telegram/bridge/restart` reloads the in-process bridge token (no window close needed). If the optional `PerunBridge` task owns the lock instead, the same endpoint falls back to `schtasks /End` + `/Run`. |

---

## 10. Global installation

`python -m perun install` (`integrations/install_global.py`):

1. copies `templates/INSTRUCTIONS.md` → `~/.config/opencode/perun/INSTRUCTIONS.md`
   and `templates/command_*.md` → `~/.config/opencode/commands/`;
2. merges into `~/.config/opencode/opencode.json`:
   * `mcp.perun` = `{type: local, command: [python, <repo>/mcp/perun_mcp.py], enabled: true}`,
   * appends the instructions path to `instructions[]`,
   * adds `command.perun-projects` / `command.perun-ask` / `command.logwork` / `command.update-docs`;
3. **backs up** the prior config to `opencode.json.perun-backup-<ts>`;
4. seeds the registry (this repo + a discovery pass) and optionally indexes docs.

Unrelated keys are preserved (JSON load → edit → dump). `uninstall()` removes
exactly the Perun keys/files; runtime data in `~/.perun` is kept.

### Web installer (`setup.ps1` / `setup.sh`)

For a fresh machine, the repo also ships one-liner installers that clone the
checkout and then run the local machine installer:

* **Windows:** `powershell -c "iwr -useb https://raw.githubusercontent.com/azagor/perun/master/setup.ps1 | iex"`
  — `setup.ps1` checks for Python 3.10+ and git, clones the repo to
  `%USERPROFILE%\perun` by default, and delegates to `bootstrap.ps1`.
* **Unix:** `curl -fsSL https://raw.githubusercontent.com/azagor/perun/master/setup.sh | bash`
  — `setup.sh` checks for Python 3.10+ and git, clones to `~/perun`, installs
  `requirements.txt`, and runs `python -m perun install`.

Both scripts are idempotent: re-running them updates or reuses an existing clone
and repairs the installation.

### Windows installer (`installer\PerunSetup.iss`)

For end-user distribution, the repo ships an **Inno Setup** project that builds
`PerunSetup.exe`. The wizard:

1. Installs Perun to `{autopf}\Perun` (administrative install).
2. Optionally installs missing prerequisites:
   * **Python 3.12.10** — downloaded from python.org, installed to
     `{app}\Python`, and `PERUN_PYTHON` is set.
   * **Git for Windows** — downloaded from the Git for Windows releases page.
   * **OpenCode CLI** — installed with `npm install -g opencode-ai` if npm is
     available, otherwise the standalone `opencode-windows-x64.zip` binary is
     downloaded and extracted.
   * **Ollama** - installed with `winget install Ollama.Ollama` if winget is
     available, otherwise a pinned `OllamaSetup.exe` is downloaded.

   Downloaded installers are verified against a built-in SHA256 table before
   execution.
3. Runs `python -m pip install -r requirements.txt`.
4. Runs `python -m perun install --machine` to wire the global OpenCode
   integration. The `PerunBridge` task is not registered unless the installer
   passes `--task`.
5. Creates Start Menu and optional desktop shortcuts for `Perun.cmd`.

Uninstall runs `python -m perun install --uninstall` and
`python -m perun install --machine --uninstall` to remove the global integration
and the bridge task, then removes `PERUN_PYTHON`.

Build it with:

```powershell
.\installer\build.ps1
```

The resulting installer is written to `installer\Output\PerunSetup.exe`.

Self-sign the installer (optional, dev/enterprise use only):

```powershell
.\installer\sign.ps1 -Password 'your-pfx-password'
```

`sign.ps1` creates a self-signed code-signing certificate, exports it to
`installer\certs\PerunSelfSign.pfx`, and signs `PerunSetup.exe`. Because the
certificate is self-signed, it will not be trusted by default on other
machines; for public distribution, use a publicly trusted code-signing
certificate.

For open-source releases, a free trusted signing option is available from
[SignPath Foundation](https://signpath.org). A low-cost paid option is
[Certum Open Source Code Signing in the Cloud](https://certum.store/open-source-code-signing-on-simplysign.html).
The repository includes a GitHub Actions workflow
(`.github/workflows/release.yml`) that builds `PerunSetup.exe` and signs it
using `PERUN_SIGN_PFX_BASE64` and `PERUN_SIGN_PASSWORD` repository secrets,
then attaches the signed installer to the GitHub release.

### Machine install (`perun install --machine`, `perun/installer.py`)

`install_global.py` covers the *user-level OpenCode* integration. Everything that
is specific to a **machine** lives in `perun/installer.py`, driven from the CLI
and from `bootstrap.ps1`:

1. **Prerequisites** — `check_prerequisites()` returns
   `{name, ok, required, detail}` rows. Python ≥ 3.10 (`MIN_PYTHON`) and `pip` are
   required and abort the install on failure; `git`, the `opencode` CLI, and a
   reachable Ollama (`ollama_status()`) are advisory warnings.
2. **Dependencies** — `pip_install()` runs
   `<python> -m pip install -r requirements.txt` (falling back to installing the
   project itself when there is no requirements file) and returns
   `{ok, command, summary, output_tail}`. This is safe to run before any
   dependency exists, because nothing imports `yaml`/`fastapi`/`httpx` at module
   load.
3. **Global install** — delegates to `install_global.install()` above.
4. **`PerunBridge` task (opt-in)** — `register_bridge_task()` when `--task` is
   passed; otherwise `remove_bridge_task()` cleans up any previously registered
   task. The bridge is started by the dashboard by default.

#### The `PerunBridge` task contract

`bridge_task_xml()` builds the task from a template (UTF-16, as
`schtasks /Create /XML` expects) and `register_bridge_task()` writes it to a temp
file and runs `schtasks /Create /TN PerunBridge /XML <file> /F`. The `/F` makes it
idempotent — re-running **repairs** rather than duplicates. The task is only
registered when explicitly requested (`--task`); by default the bridge starts
with the dashboard and stops when the dashboard closes.

| XML node | Value | Why |
|---|---|---|
| `Settings/MultipleInstancesPolicy` | `IgnoreNew` | a second trigger is refused, so the poller stays singular |
| `Settings/ExecutionTimeLimit` | `PT0S` | unlimited; the bridge is meant to run forever |
| `Settings/RestartOnFailure` | `999` × `PT1M` | survive crashes and network loss |
| `Settings/StartWhenAvailable` | `true` | catch up if the logon trigger was missed |
| `Triggers/LogonTrigger/UserId` | `DOMAIN\user` | start at logon for the installing user |
| `Actions/Exec` | `<python> -m perun bridge`, workdir = repo root | the single-instance IPC path (§9) |

The interpreter comes from `_python_exe()`, which honours `PERUN_PYTHON` — the
same override `Perun.cmd` uses — so the launcher and the task can never disagree.
The task is only **started** (`schtasks /Run`) when a Telegram token is
configured and `--task` was passed; otherwise no task is registered.
`bridge_task_status()` reads the task back with `schtasks /Query /XML` and parses
it (never locale-dependent `/V` text); `perun status` surfaces it as
`bridge_task`.

`uninstall_machine()` removes the task, so `python -m perun install --machine
--uninstall` reverses the whole machine-level setup.

---

## 11. MCP server

`mcp/perun_mcp.py` (FastMCP, stdio). It inserts the repo root on `sys.path`,
`db.init_db()`s, calls `control.ensure()` at import, and exposes **twenty-three**
tools:

* **Registry:** `perun_list_projects`, `perun_project_info`,
  `perun_create_project`, `perun_update_project` (also mirrors `model`/`agent`
  into the project's `opencode.json[c]` and reports the result), `perun_delete_project`
  (requires `confirm=True`; refuses the control plane), `perun_discover`.
* **Memory & docs:** `perun_memory_search`, `perun_memory_store`,
  `perun_docs_index`.
* **Instructions & manifest:** `perun_get_instructions`,
  `perun_set_instructions`, `perun_list_instruction_files`,
  `perun_get_manifest`, `perun_set_manifest`.
* **Runs & messaging:** `perun_start_session` (returns a run id immediately),
  `perun_ask` (blocking), `perun_run_status`, `perun_telegram_send`.
* **Code search:** `perun_grep` (regex content search) and `perun_ast_grep`
  (structural/AST search), plus the cross-project `perun_global_grep` and
  `perun_global_ast_grep`, so projects get code search without extra MCP
  configuration. `perun_unified_search` is the preferred bundled search tool,
  fusing memory, ripgrep, and ast-grep behind a single MCP call; code results from
  ripgrep and ast-grep are combined with Reciprocal Rank Fusion (see §8).

`_resolve_project()` accepts a slug, a path, or `None` — for `None` it walks up
from the current working directory to the nearest registered project. Project
creation is shared with the HTTP wizard (`perun/projects.py`).

---

## 12. Web dashboard

`web/` is a dependency-free SPA (vanilla JS, hash routing).

* **Views:** Dashboard, Orchestrator chat (`#/orchestrator`), Projects (+ detail
  with tabs Overview/Console/Sessions/Memory/Activity/Git/Manifest/Instructions/
  Config), Agent Console (live SSE), Memory Explorer, Activity (live
  `/api/stream`), MCP Servers (`#/mcp`), **Providers**, Telegram, Settings.
* **Providers view (`#/providers`).** Shows LLM providers reported by
  `opencode auth list --format json` with their credential status ("Saved
  credential" / "No credential"). The Connect button opens the configured
  terminal running `opencode auth login <id>` so Perun never handles API keys.
  Disconnect invokes `POST /api/providers/{id}/logout`. Results are cached for
  30 seconds; a Refresh button fetches with `?refresh=true`.
* **Dashboard provider preview.** A stat card on the Dashboard shows the count of
  connected/total providers, complementing the existing Ollama embeddings status
  card.
* **MCP Servers view.** Lists every MCP server OpenCode is configured to load
  (global + per project) with a config-derived status badge, transport, target,
  and resolved executable. A `Test` button (and `Test all enabled`) calls
  `POST /api/mcp/servers/probe` to spawn the server and run the MCP handshake,
  showing the live tool list, latency, and any stderr. A config file that fails
  to parse is called out in a warning card (rather than looking like "no
  servers"), and if the endpoint itself 404s — the running service is older than
  the static JS the browser loaded from disk — the view names the fix (restart
  Perun) instead of showing an empty list.
* **Memory Explorer project filter.** Selecting a project in the Memory Explorer
  now reloads recent facts and stats for that project even when the query field
  is empty; semantic search via `/api/memory/search` is still used when a query
  is entered.
* **Agent Console.** A three-pane layout: project/session/terminal selectors at the
  top, a live output/chat log in the middle, and a prompt composer + task queue at
  the bottom. The output pane shows a centered placeholder (`Agent output will
  appear here…`) when no run is active and clears it as soon as the first event
  arrives. The primary action button is **Run** (labelled `Run N tasks` when
  tasks are queued). Skill tasks (`kind: "skill"`) are emitted as `/<skillname>`
  slash commands by `web/js/prompt_chain.js`; other task kinds are appended as
  structured instructions. The task editor hides the **Tools** selector for skill
  tasks because tools are not used by slash commands.
* **Uniform project cards.** `projectCard()` renders the *same* action set for
  every project, including the orchestrator control-plane card: `Open` (project
  page) and `Run` (Agent Console with that project preselected). The old
  card-level special case that routed all control-card clicks to
  `#/orchestrator` — which made its `Ask` button do something different from
  every other card's — is gone. The orchestrator **chat** is reachable only from
  the left toolbar (nav item `Orchestrator Chat`); the topbar's quick action is
  `✦ Run`.
* **Orchestrator chat** targets the control plane; it streams in place and works
  with zero other projects registered. Its Settings card edits the orchestrator's
  **model**, **agent**, and **custom instructions**.
* **Telegram page** (`viewTelegram`/`mountTelegram`) owns the bot configuration:
  a "Bot settings" card (name / API key / chat ids), a bridge-process card with
  **⟳ Restart bridge**, and the send-a-message form. Settings keeps only a
  pointer to it, so there is one source of truth.
* **Searchable model picker** (`modelSelect`): a reusable combobox that filters
  the structured `/api/models` catalogue with a live search bar.
* **Searchable agent picker** (`agentSelect`): a similar combobox populated from
  `GET /api/agents`, which returns OpenCode's built-in agents (`build`, `plan`,
  `general`, `explore`) plus any user-defined agents found under
  `~/.config/opencode/agents/` or `.opencode/agents/` in the project. If the
  endpoint is empty or unavailable the UI falls back to the built-in list so the
  dropdown is never empty.
* The native `<datalist>` (`model-root`) was removed; the custom `mselect`
  components provide their own search panels.
* **Storm layer** (`perunFx`): a full-viewport `<canvas>` lightning engine plus an
  animated brand sigil whose **eyes glow**. Three escalating states — **idle**,
  **awake** (a project touched in the last 15 min), **storm** (≥1 run running) —
  are published on `<body data-storm>` and drive a topbar storm meter, a
  travelling energy rim + eye pips on working cards, and a one-shot screen flash
  when a run starts. All motion is disabled under `prefers-reduced-motion`.
* **Project wizard & folder browser** are modal flows over `/api/projects` and
  `/api/fs/*`; **Git** and **Manifest** tabs load their own data lazily.
* **Manifest body** is rendered by `renderMarkdown()` — a small, escaped-by-default
  Markdown subset (headings, lists, emphasis, code, links); link targets are
  restricted to `http(s)`, `mailto`, or relative URLs.
* **Design tokens** mirror peruninthemachine.com: `#050505` background,
  `#c9a227` gold, `Cinzel` headings (font copied to `web/assets/Cinzel.ttf`), and
  the gold-bolt favicon `web/assets/perun.ico`.
* **Live output** uses `EventSource`; streams are closed on navigation.

---

## 13. Configuration reference (`Settings`)

| Field | Default | Meaning |
|---|---|---|
| `roots` | `C:/my_apps` + `~/projects`/`~/src`/`~/code` that exist | discovery roots |
| `host` / `port` | `127.0.0.1` / `8760` | dashboard bind |
| `default_model` / `default_agent` | `""` | fallbacks **only for the control plane**; ordinary projects fall back to OpenCode's own defaults when these are empty |
| `orchestrator_model` / `orchestrator_agent` | `""` | control-plane (orchestrator) model/agent, outranking the global default |
| `max_concurrent_runs` | `3` | orchestrator semaphore |
| `run_timeout` | `1800` | seconds per run |
| `ollama_host` | `http://127.0.0.1:11434` | embeddings provider |
| `embed_model` | `nomic-embed-text` | 768-dim |
| `telegram_token` | `""` | never echoed by the API; falls back to `TELEGRAM_BOT_TOKEN` |
| `telegram_bot_name` | `""` | local label to tell bots apart (dashboard only; not pushed to Telegram) |
| `telegram_allowed_chats` | `[]` | falls back to `TELEGRAM_CHAT_ID`; empty = pair-on-first-message |
| `auto_discover_on_start` | `true` | discovery on service startup |
| `index_docs_on_discover` | `false` | index new projects' docs on discover |
| `terminal` | `""` | preferred terminal emulator for `Run in Terminal` / TUI launches. Empty = OS default (`pwsh`/`powershell` on Windows, `$SHELL` elsewhere). Accepts an alias (`cmd`, `pwsh`, `wt`) or a full path. The dashboard Settings page edits this value; it only affects future launches. |

Environment overrides: `PERUN_HOME`, `PERUN_ROOTS`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`. A config.json value wins over the environment;
`Settings.telegram_sources()` reports which is actually in force (surfaced as
`token_source` / `chats_source` so the dashboard can explain it).

### Terminal selection and duplicate TUI windows

Perun does not control OpenCode's TUI tab/session restore behavior; the TUI
itself reopens previous sessions as tabs. Perun **does** control which terminal
emulator is used when the dashboard launches a TUI via `POST /api/run-in-terminal`
(or the dashboard "Run" / "OpenCode" buttons). If you see two identical session
windows, you probably launched the TUI from two different terminals (e.g.,
classic `cmd.exe` and PowerShell). Set `Settings.terminal` to a single value
(`pwsh`, `wt`, `cmd`, or a full path) in the dashboard Settings page so every
future launch uses the same terminal. Existing windows are not closed by this
setting.

---

## 14. Revision history

### 2026-10-16 — v0.7.0 released: installer, providers, model/agent sync, slash-command tasks

* **Windows GUI installer + web installers + CI release.** Added `installer/`
  (Inno Setup `PerunSetup.iss`, `install-prereqs.ps1`, `uninstall.ps1`,
  `build.ps1`, `sign.ps1`), repo-root `setup.ps1` / `setup.sh`, and
  `.github/workflows/release.yml`. The wizard installs Python, git, OpenCode CLI,
  and Ollama as needed, verifies downloaded binaries against built-in SHA256
  hashes, installs Perun, registers the `PerunBridge` task, and creates shortcuts.
  The release workflow builds and signs `PerunSetup.exe` on `v*` tags and attaches
  it to the GitHub release.
* **Dashboard model/agent overrides are mirrored into project `opencode.json`.**
  Added `projects.sync_opencode_config()` which writes `model` / `default_agent`
  into a project's `opencode.json[c]` whenever a dashboard config save, wizard
  creation, or MCP `perun_update_project` changes those fields. Existing keys are
  preserved, clearing the override removes the keys, commented `.jsonc` files are
  left untouched, and writes are atomic (temp file + `os.replace`). The PATCH
  endpoint returns the mirror status in `opencode_config` so the UI can toast
  success or warn about an unparseable config.
* **TUI launch no longer passes `run`-only flags.** The project-card and Agent
  Console TUI command builders removed `--model` / `--agent`; the base `opencode`
  TUI command only accepts `--prompt` and `--session`, so passing model/agent
  caused `Unrecognized flag: --model in command opencode`. TUI sessions now rely
  on OpenCode's project defaults, including the newly-mirrored `opencode.json`
  values.
* **Providers tab + dashboard preview.** New `perun/providers.py` module and
  endpoints `GET /api/providers` and `POST /api/providers/{id}/logout`. The list
  is sourced from `opencode auth list --format json`, enriched with credential
  status, and cached for 30 seconds (bypassed with `?refresh=true`, cache guarded
  by `threading.Lock`). A dedicated `#/providers` view and a Dashboard stat card
  show connected/total providers; the Connect button opens the configured terminal
  with `opencode auth login <id>` so Perun never sees API keys. Provider IDs are
  validated before logout; invalid IDs return HTTP 400, and the cache is evicted
  on disconnect.
* **Skill tasks run as TUI slash commands.** The Agent Console prompt chain now
  emits `/<skillname>` for queued `kind: "skill"` tasks. The logic was extracted
  into `web/js/prompt_chain.js` for testability and is covered by
  `tests/test_prompt_chain.js` (run from `tests/test_js_prompt_chain.py`). The
  task editor hides the **Tools** selector for skill tasks because tools are not
  applicable to slash commands.
* **Skill name validation.** `perun/skills.py` rejects skill manifests whose
  `name` contains characters outside `[a-z0-9_-]`, preventing malformed names from
  reaching the TUI as slash commands.
* **Consistent "Run" labelling.** The project-card primary action and the Agent
  Console action button are now labelled **Run** (previously `🖵 OpenCode`), and
  the Agent Console button updates to "Run N tasks" when tasks are queued.
* **Memory Explorer filters by project without a query.** Selecting a project in
  the Memory view now reloads recent facts (`GET /api/memory/recent?project=`)
  and stats (`GET /api/memory/stats?project=`) for that project even when the
  query field is empty; semantic search remains available when a query is entered.
* **Agent Console placeholder.** The empty output pane now shows a centered
  "Agent output will appear here…" message before the first run starts.
* **Shared JSONC reader.** `mcp_servers._read_config()` was promoted to public
  `read_opencode_config()` and now returns a `lossy` flag so callers can detect
  when comments/trailing commas had to be stripped and avoid clobbering them on
  rewrite.
* **Git workflow instruction.** Added a project-local instruction file
  `.perun/instructions/git-workflow.md` covering credential setup and the release
  checklist; `AGENTS.md` documents the same for agents.
* **Docs/tests/versions.** `docs/CODEMAP.md`, `docs/TECHNICAL_DOCUMENTATION.md`,
  `docs/CHANGELOG.md`, `AGENTS.md`, `README.md`, and
  `perun/integrations/templates/INSTRUCTIONS.md` updated. Version bumped to
  `0.7.0` in `perun/__init__.py`, `pyproject.toml`, and `installer/PerunSetup.iss`.
  New tests: `tests/test_providers.py`, `tests/test_setup_scripts.py`,
  `tests/test_js_prompt_chain.py`; expanded `tests/test_server.py`. Full suite
  passes.

### 2026-10-16 — v0.6.4: on-disk instructions, DPAPI secrets & bundled search

* **Instruction text lives only on disk.** Project instructions are Markdown files
  under `<project>/.perun/instructions/` (global under
  `~/.config/opencode/perun/instructions/`), each with `name`/`scope`/`enabled`
  front matter; enabled files are injected into new sessions in sorted filename
  order. Removed the legacy `projects.instructions` / `projects.instructions_path`
  columns, both one-off migrations, the `.migrated` marker, and the
  `perun_migrate_project_instructions` MCP tool. `db._migrate()` drops the retired
  columns from older databases. `orchestrator.write_project_instruction_file()` is
  the single writer; `projects.sync_manifest()` is the single manifest-merge path,
  and manifest-declared `instructions` are seeded once to `default.md` — now also
  from the HTTP `PUT …/manifest` and the `perun_set_manifest` MCP tool.
* **DPAPI-encrypted remembered password.** `secrets.persist_password()` (the
  dashboard's "Remember on this PC") now DPAPI-encrypts the password into
  `<PERUN_HOME>/secret` (bound to the current Windows user) instead of writing a
  plaintext `PERUN_PASSWORD` to the user environment.
* **Robustness fixes.** `db._migrate()` tolerates a failed `DROP COLUMN` (SQLite
  < 3.35); `search.unified_search()` no longer blocks on a hung backend (the
  executor is shut down without waiting); `perun global-instructions --clear`
  deletes custom files before re-seeding the default guidance block.
* **Bundled code search (landed in v0.6.2).** `perun/grep.py` + `perun/search.py`
  add ripgrep/ast-grep search with pure-Python fallbacks; new `/api/search/*`
  endpoints and the MCP tools `perun_grep`, `perun_ast_grep`, `perun_global_grep`,
  `perun_global_ast_grep`, and `perun_unified_search` (see §8 and §11).
* **Search correctness hardening.** Dropped the unsupported `--files-from` flag
  (neither ripgrep nor ast-grep supports it) so global searches pass project paths
  positionally; a ripgrep/ast-grep "no matches" exit code (`1`) is now a successful
  empty result instead of re-running the entire search with the Python fallback;
  removed the dead `_path_list_file` helper. Added regression tests for both.
* **Version.** Bumped `perun.__version__` to `0.6.4` to match the changelog;
  `/api/health` and the OpenAPI title report it.
* **Docs.** Fixed a stray `)` before a `### Added` heading in `docs/CHANGELOG.md`
  and documented the search layer here (§8).
* **Tests:** full suite at **377 passed** (+6 regression tests).

### 2026-10-16 — Terminal setting documented and configured

* **Configuration reference updated.** Added `Settings.terminal` to the settings
  table (§13) and documented how Perun resolves the terminal emulator for TUI
  launches (`terminal_cmd()`). The dashboard Settings page is the recommended
  place to change it.
* **Duplicate-window guidance.** Added a note (§13) explaining that multiple
  identical session windows come from launching the TUI in different terminals
  (e.g., `cmd.exe` vs. PowerShell), while TUI tabs are controlled by OpenCode's
  own session restore, not Perun. Set `Settings.terminal` to a single value to
  keep future launches consistent.
* **Runtime configuration.** The local Perun instance was configured to use
  `pwsh` (PowerShell 7) as its explicit terminal, replacing the previous OS
  default resolution.

### 2026-10-16 — `/logwork` global slash command

* **New global command `/logwork`.** Added `perun/integrations/templates/command_logwork.md`
  and registered it in `install_global.py` (and the live `~/.config/opencode/opencode.json`).
  The command stores a one-sentence work summary via `perun_memory_store`; when
  invoked inside a project it becomes a project-scoped fact, and when invoked from
  the orchestrator / global context it becomes a global fact.
* **Global instructions updated.** Both `perun/integrations/templates/INSTRUCTIONS.md`
  and the managed `~/.config/opencode/AGENTS.md` now list the available global
  slash commands (`/perun-projects`, `/perun-ask`, `/logwork`).
* **Documentation updated.** The Global installation section (§10) now references
  the `command.logwork` registry entry.

### 2026-10-14 — Agent Console & session-start improvements

* **Empty-prompt session start.** The Agent Console `Run` button and
  `POST /api/runs` no longer require a prompt; an omitted prompt uses the default
  starter prompt `"Started a working session for this project."`.
* **New CLI command:** `python -m perun run <slug> [prompt] [--model M]
  [--agent A] [--title T] [--wait]`.
* **New endpoint:** `POST /api/projects/{slug}/session` starts a session for a
  specific project with an optional prompt.
* **Agent dropdown.** The Agent Console's free-text Agent input is now a
  searchable `agentSelect` populated from `GET /api/agents` (built-ins + agents on
  disk); a built-in fallback prevents an empty list.
* **Model/agent overrides are explicit only.** The global `default_model` /
  `default_agent` are no longer forced onto ordinary project runs; Perun only
  passes `--model` / `--agent` to OpenCode when explicitly requested. Added
  `runs.model_explicit` column to track this.
* **Provider errors keep sessions active.** If a session id is learned but the
  provider returns an error (`provider.quota`, `provider.invalid-request`, …), the
  run status is `provider_error` (not `failed`) and the session row stays active.
* **Instruction delimiter fix.** Project instructions now use plain-text
  `[PERUN PROJECT INSTRUCTIONS]` / `[END PERUN PROJECT INSTRUCTIONS]` instead of
  XML-style tags that models were interpreting as markup.
* **UI cleanup.** Removed the native `<datalist>` / `#model-root` machinery and
  tightened `.mselect` styling so dropdowns fit the Agent Console layout.
* **Tests:** full suite updated to **195 passed**.

### 2026-10-14 — v0.5.0 UI-consistency + Telegram bot switch

* Uniform project cards (`Open` / `Run`), `Run` replaces ambiguous `Ask`.
* Orchestrator chat reachable only from the left toolbar.
* Telegram page "Bot settings" card with masked, `getMe`-verified API key and
  one-click bridge restart.

### Earlier releases

* v0.4.0 — Machine install (`bootstrap.ps1` / `python -m perun install --machine`)
  with prerequisites, deps, global OpenCode integration, and the `PerunBridge`
  logon task.
* v0.3.0 — Control plane (always-available orchestrator), orchestrator chat,
  project-management MCP tools, storm layer, `Perun.cmd` launcher.
* v0.2.1 — Anti-CSRF / anti-DNS-rebinding guard on `/api/*`, stored-XSS fixes.
* v0.2.0 — Project creation wizard, per-project Git, custom instructions,
  `perun.md` manifest, model picker.
* v0.1.0 — Machine-wide registry + unified memory, agent orchestrator, web
  dashboard, shared Telegram bridge, globally-installed MCP server.
