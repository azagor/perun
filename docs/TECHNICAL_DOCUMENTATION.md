# Perun — Technical Documentation

**Latest release:** v0.4.0 — Perun is now **installable from one command**.
`bootstrap.ps1` (or `python -m perun install --machine`) checks prerequisites,
installs dependencies, wires the global OpenCode integration, and registers the
single-instance **`PerunBridge`** logon task — so a fresh machine can be brought
up without hand-editing Task Scheduler, which until now was the only way the task
ever came into existence. It builds on v0.3.0 (the **control plane** — an
always-present orchestrator reachable with zero projects registered, a
**dashboard chat for the orchestrator** with its own custom instructions and a
searchable model picker, project-management MCP tools, the **storm layer** (Perun's
lightning + glowing eyes as live idle/awake/working feedback), and the
**one-click Windows launcher** `Perun.cmd`), v0.2.1 (security
hardening: anti-CSRF / anti-DNS-rebinding guard on `/api/*`, plus stored-XSS
fixes), v0.2.0 (project management: creation wizard, per-project Git, custom
project instructions, self-describing `perun.md` manifest, model picker), and the
v0.1.0 core (machine-wide registry + unified memory, agent orchestrator, web
dashboard, shared Telegram bridge, and a globally-installed MCP server).

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
| Telegram | a long-polling thread (`PerunTelegramBridge`) |
| Global integration | stdio MCP server `mcp/perun_mcp.py` + `~/.config/opencode` merge |
| One-click startup (Windows) | `Perun.cmd` — idempotent dashboard launcher; never starts a second Telegram bridge |
| Machine install (Windows) | `bootstrap.ps1` → `python -m perun install --machine` (`perun/installer.py`): prerequisites, deps, global install, and the `PerunBridge` task |

### Runtime layout (`~/.perun`, override with `PERUN_HOME`)

```
~/.perun/
  perun.db        registry + facts + doc_chunks + vectors
  config.json     Settings
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

It **never** runs `perun bridge` or `serve --bridge`. Telegram is owned solely by
the `PerunBridge` logon task, which the launcher only `schtasks /run`s; together
with the named-mutex singleton (§9) that guarantees exactly one `getUpdates`
poller. The desktop shortcut's `.ico` is generated deterministically by
`tools/make_icon.py`.

The task itself is **created and repaired by the installer** (§10), not by the
launcher — `Perun.cmd` only triggers it. On a brand-new machine, run
`.\bootstrap.ps1` once to register it.

---

## 2. Data model

Single SQLite DB, WAL mode, one short-lived connection per operation
(`db.db()`), so the API threads, orchestrator threads, bridge, and MCP server
can all write concurrently.

| Table | Purpose | Notes |
|---|---|---|
| `projects` | registry | `slug` unique, `path` unique, `kind` ∈ {opencode,git,dir}, `favorite`, `enabled`, `model`, `agent`, `instructions`, `manifest` (JSON snapshot), `manifest_path`, `manifest_loaded_at`, `git_remote`, `git_branch` |
| `sessions` | Perun's record of OpenCode sessions | `opencode_session_id`, `message_count`, `status` |
| `runs` | one agent invocation | `run_uid` unique, `status` ∈ {queued,running,done,failed,cancelled,timeout}, `output`, `error` |
| `activity` | event feed | `kind`, `message`, `meta` JSON |
| `memory` | facts | `scope` ∈ {global,project}; **`scope='global'` rows use `project_id=0`**; `UNIQUE(scope,project_id,content_hash)` |
| `doc_chunks` | document vectors | `UNIQUE(project_id,source_file,section)` |
| `settings` | key/value scratch | |
| `bridge_links` | Telegram routing | `chat_id` → `project_id` + `session_pk` |

Column additions after v0.1.0 are applied idempotently by `db._ADDED_COLUMNS`
(`instructions`, `manifest`, `manifest_path`, `manifest_loaded_at`, `git_*`), so
existing databases migrate in place.

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
  `perun.md`);
* `clone` — `git.clone(url, path)` into the target.

Optional steps: `git_init` (`scaffold`/`git.init`), `git_remote` + `git_branch`
(`git.set_remote`), and `index_docs` (`memory.index_project_docs`). After
registration the manifest is merged (`_sync_manifest`) so the dashboard reflects
it immediately. A server-backed **folder browser** powers the path pickers:
`GET /api/fs/list?path=&show_hidden=` returns `roots`, `home`, `parent`, `dirs`,
`files`; `POST /api/fs/mkdir {path}` creates a directory.

**Per-project Git** (`perun/git.py`). Wraps the `git` CLI with explicit,
non-interactive arguments (`GIT_TERMINAL_PROMPT=0`), validated remote URLs, and a
structured `info()` (`repo`, `branch`, `dirty`, `change_count`, `ahead`,
`behind`, `remote_url`). It **never** rewrites a working tree implicitly —
`init`/`remote`/`commit`/`push` are explicit user actions.

**Custom project instructions** (`projects.instructions`). Project-scoped rules
injected only when a **new** session starts: `orchestrator.compose_prompt()`
prepends

```
<perun:project_instructions>
…rules…
</perun:project_instructions>

<user prompt>
```

Continuations of an existing session inherit the rules from history, so they are
not re-injected. The raw user prompt is what gets stored on the run row.

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

**Orchestrator instructions** (`Settings.orchestrator_instructions`) are the
control-plane analogue of project instructions: a machine-wide persona/rules
block injected ahead of every orchestrator prompt. They live in **Settings**, not
on the project row, because the orchestrator is a singleton and its config must
survive a registry wipe + `control.ensure()` re-creation. `compose_prompt(
project, prompt, settings=…)` injects them only when the target project
`is_control`.

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

1. resolves model/agent with a control-only tier: explicit call arg → project
   override → (when the target `is_control`) `Settings.orchestrator_model` /
   `orchestrator_agent` → global `default_model` / `default_agent`,
2. writes a `runs` row (`status=queued`) and an activity entry,
3. spawns a daemon thread running `_execute`, guarded by
   `threading.Semaphore(max_concurrent_runs)`.

`_execute` runs (Windows-aware via `resolve_opencode`):

```
opencode run --format json --auto [--model M] [--agent A]
             [--session <oc_id>] --title "Perun · …" -- <prompt>
```

with `cwd` set to the **target project path** — no positional directory
argument. stdout is streamed line-by-line to `~/.perun/runs/<uid>.log`, parsed
by `parse_event()`, and fanned out through `EventBus` as normalized events:

| event | meaning |
|---|---|
| `queued` / `start` | accepted / process launched |
| `session` | learned the OpenCode session id |
| `text` | assistant text delta |
| `tool` | tool invocation + status |
| `error` | error frame |
| `end` | terminal: `status`, `exit_code`, `reply`, `error` |

Error frames observed on stdout are also collected and, when a run ends with no
reply, joined into the run's persisted `error` field so a finished failure stays
explainable in the dashboard.

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
| GET | `/api/projects` | list projects |
| POST | `/api/projects` | wizard: register / create / clone `{method,path,…}` |
| GET | `/api/projects/{slug}` | detail + sessions + runs + activity + memory + manifest body |
| PATCH | `/api/projects/{slug}` | update fields |
| DELETE | `/api/projects/{slug}` | unregister (refuses the control project) |
| POST | `/api/projects/{slug}/index` | sync manifest, then index docs |
| GET | `/api/projects/{slug}/sessions` | sessions |
| DELETE | `/api/sessions/{pk}` | delete a session |
| GET | `/api/projects/{slug}/activity` | project activity |
| GET | `/api/projects/{slug}/memory` | project facts + stats |
| GET/PUT | `/api/projects/{slug}/instructions` | project-scoped instructions |
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
| POST | `/api/discover` | scan roots & register |
| GET | `/api/discover/scan` | dry-run scan |
| GET | `/api/runs` | recent runs (`?project=`) |
| GET | `/api/runs/active` | queued/running runs joined with their project (the live storm layer) |
| POST | `/api/runs` | start a run `{project,prompt,model?,agent?,session_pk?}`; `project` is optional and defaults to the orchestrator |
| GET | `/api/runs/{run_uid}` | run status/output |
| POST | `/api/runs/{run_uid}/cancel` | cancel |
| GET | `/api/runs/{run_uid}/stream` | **SSE** run events |
| GET | `/api/stream` | **SSE** global run events + activity |
| POST | `/api/memory/remember` | store a fact |
| POST | `/api/memory/search` | unified search |
| GET | `/api/memory/stats` | counts + `ollama_ok` |
| GET | `/api/memory/recent` | recent facts |
| POST | `/api/memory/import-legacy` | migrate legacy facts |
| POST | `/api/memory/prune` | drop old non-global facts |
| GET | `/api/activity` | global feed |
| GET/PATCH | `/api/settings` | read/update `Settings` |
| GET | `/api/telegram/status` | bot + allowed chats + `bridge_running` |
| POST | `/api/telegram/send` | send a message |
| GET | `/` etc. | static dashboard from `web/` |

`GET /api/runs/active` is registered **before** `GET /api/runs/{run_uid}` so the
literal path is not swallowed by the parameterised route.

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
pid-file) and honoured by both `run_bridge()` (the `bridge` CLI, and the
`PerunBridge` logon task) and `_start_bridge_thread()` (`serve --bridge`, which now
keeps the lock referenced for the process lifetime). A second attempt refuses
instead of poller-fighting, and the `PerunBridge` task is registered with
`IgnoreNew`.

---

## 10. Global installation

`python -m perun install` (`integrations/install_global.py`):

1. copies `templates/INSTRUCTIONS.md` → `~/.config/opencode/perun/INSTRUCTIONS.md`
   and `templates/command_*.md` → `~/.config/opencode/commands/`;
2. merges into `~/.config/opencode/opencode.json`:
   * `mcp.perun` = `{type: local, command: [python, <repo>/mcp/perun_mcp.py], enabled: true}`,
   * appends the instructions path to `instructions[]`,
   * adds `command.perun-projects` / `command.perun-ask`;
3. **backs up** the prior config to `opencode.json.perun-backup-<ts>`;
4. seeds the registry (this repo + a discovery pass) and optionally indexes docs.

Unrelated keys are preserved (JSON load → edit → dump). `uninstall()` removes
exactly the Perun keys/files; runtime data in `~/.perun` is kept.

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
4. **`PerunBridge` task** — `register_bridge_task()`.

#### The `PerunBridge` task contract

`bridge_task_xml()` builds the task from a template (UTF-16, as
`schtasks /Create /XML` expects) and `register_bridge_task()` writes it to a temp
file and runs `schtasks /Create /TN PerunBridge /XML <file> /F`. The `/F` makes it
idempotent — re-running **repairs** rather than duplicates.

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
configured; otherwise it is registered and idles harmlessly until a token exists.
`bridge_task_status()` reads the task back with `schtasks /Query /XML` and parses
it (never locale-dependent `/V` text); `perun status` surfaces it as
`bridge_task`.

`uninstall_machine()` removes the task, so `python -m perun install --machine
--uninstall` reverses the whole machine-level setup.

---

## 11. MCP server

`mcp/perun_mcp.py` (FastMCP, stdio). It inserts the repo root on `sys.path`,
`db.init_db()`s, calls `control.ensure()` at import, and exposes **seventeen**
tools:

* **Registry:** `perun_list_projects`, `perun_project_info`,
  `perun_create_project`, `perun_update_project`, `perun_delete_project`
  (requires `confirm=True`; refuses the control plane), `perun_discover`.
* **Memory & docs:** `perun_memory_search`, `perun_memory_store`,
  `perun_docs_index`.
* **Instructions & manifest:** `perun_get_instructions`,
  `perun_set_instructions`, `perun_get_manifest`, `perun_set_manifest`.
* **Runs & messaging:** `perun_start_session` (returns a run id immediately),
  `perun_ask` (blocking), `perun_run_status`, `perun_telegram_send`.

`_resolve_project()` accepts a slug, a path, or `None` — for `None` it walks up
from the current working directory to the nearest registered project. Project
creation is shared with the HTTP wizard (`perun/projects.py`).

---

## 12. Web dashboard

`web/` is a dependency-free SPA (vanilla JS, hash routing).

* **Views:** Dashboard, Orchestrator chat (`#/orchestrator`), Projects (+ detail
  with tabs Overview/Console/Sessions/Memory/Activity/Git/Manifest/Instructions/
  Config), Agent Console (live SSE), Memory Explorer, Activity (live
  `/api/stream`), Telegram, Settings.
* **Orchestrator chat** targets the control plane; it streams in place and works
  with zero other projects registered. Its Settings card edits the orchestrator's
  **model**, **agent**, and **custom instructions**.
* **Searchable model picker** (`modelSelect`): a reusable combobox that filters
  the structured `/api/models` catalogue with a live search bar; the native
  `<datalist>` is kept for wizard/manifest inputs.
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
| `default_model` / `default_agent` | `""` | fallbacks when a project has none |
| `orchestrator_model` / `orchestrator_agent` | `""` | control-plane (orchestrator) model/agent, outranking the global default |
| `orchestrator_instructions` | `""` | machine-wide persona/rules injected into every orchestrator prompt |
| `max_concurrent_runs` | `3` | orchestrator semaphore |
| `run_timeout` | `1800` | seconds per run |
| `ollama_host` | `http://127.0.0.1:11434` | embeddings provider |
| `embed_model` | `nomic-embed-text` | 768-dim |
| `telegram_token` | `""` | falls back to `TELEGRAM_BOT_TOKEN` |
| `telegram_allowed_chats` | `[]` | falls back to `TELEGRAM_CHAT_ID`; empty = pair-on-first-message |
| `auto_discover_on_start` | `true` | discovery on service startup |
| `index_docs_on_discover` | `false` | index new projects' docs on discover |

Environment overrides: `PERUN_HOME`, `PERUN_ROOTS`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`.
