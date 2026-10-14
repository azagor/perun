# Perun — Technical Documentation

**Latest release:** v0.2.1 — security hardening of the dashboard API
(anti-CSRF / anti-DNS-rebinding guard on `/api/*`, plus stored-XSS fixes) on top
of v0.2.0 (project management: creation wizard, per-project Git, custom project
instructions, self-describing `perun.md` manifest, model picker) and the v0.1.0
core (machine-wide registry + unified memory, agent orchestrator, web dashboard,
shared Telegram bridge, and a globally-installed MCP server).

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

### Runtime layout (`~/.perun`, override with `PERUN_HOME`)

```
~/.perun/
  perun.db        registry + facts + doc_chunks + vectors
  config.json     Settings
  logs/           telegram.log, service logs
  runs/           <run_uid>.log — raw opencode stdout per run
```

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
single endpoint handles three methods:

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

1. resolves model/agent (arg → project → global default),
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
| GET | `/api/health` | version, uptime, project count, active runs |
| GET | `/api/models` | `opencode models` listing |
| GET | `/api/projects` | list projects |
| POST | `/api/projects` | wizard: register / create / clone `{method,path,…}` |
| GET | `/api/projects/{slug}` | detail + sessions + runs + activity + memory + manifest body |
| PATCH | `/api/projects/{slug}` | update fields |
| DELETE | `/api/projects/{slug}` | unregister |
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
| POST | `/api/runs` | start a run `{project,prompt,model?,agent?,session_pk?}` |
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
resolution (`_current_project`) defaults to the sole project when only one is
registered.

Commands: `/start` `/help`, `/projects`, `/project <name|#>`, `/new` `/reset`,
`/status`, `/stop` `/cancel`, `/remember <fact>`, `/recall <query>`, `/index`.
Anything else is treated as a prompt and answered by a Perun-orchestrated run,
with a typing indicator and reply threading. `format_html()` renders a safe
subset of Markdown into Telegram HTML; `chunk()` splits at 3900 chars.
`PidLock` (named Windows mutex / POSIX pid-file) enforces one instance.

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

---

## 11. MCP server

`mcp/perun_mcp.py` (FastMCP, stdio). It inserts the repo root on `sys.path`,
`db.init_db()`s, and exposes thirteen tools. `_resolve_project()` accepts a slug,
a path, or `None` — for `None` it walks up from the current working directory to
the nearest registered project. `perun_ask` is blocking; `perun_start_session`
returns immediately with a run id. `perun_get_instructions` /
`perun_set_instructions` read/write the project-scoped instructions;
`perun_get_manifest` / `perun_set_manifest` read/write `perun.md`.

---

## 12. Web dashboard

`web/` is a dependency-free SPA (vanilla JS, hash routing).

* **Views:** Dashboard, Projects (+ detail with tabs Overview/Console/Sessions/
  Memory/Activity/Git/Manifest/Instructions/Config), Agent Console (live SSE),
  Memory Explorer, Activity (live `/api/stream`), Telegram, Settings.
* **Project wizard & folder browser** are modal flows over `/api/projects` and
  `/api/fs/*`; **Git** and **Manifest** tabs load their own data lazily.
* **Manifest body** is rendered by `renderMarkdown()` — a small, escaped-by-default
  Markdown subset (headings, lists, emphasis, code, links); link targets are
  restricted to `http(s)`, `mailto`, or relative URLs.
* **Design tokens** mirror peruninthemachine.com: `#050505` background,
  `#c9a227` gold, `Cinzel` headings (font copied to `web/assets/Cinzel.ttf`).
* **Live output** uses `EventSource`; streams are closed on navigation.

---

## 13. Configuration reference (`Settings`)

| Field | Default | Meaning |
|---|---|---|
| `roots` | `C:/my_apps` + `~/projects`/`~/src`/`~/code` that exist | discovery roots |
| `host` / `port` | `127.0.0.1` / `8760` | dashboard bind |
| `default_model` / `default_agent` | `""` | fallbacks when a project has none |
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
