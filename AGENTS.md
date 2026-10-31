# Perun — Agent Reference

> This is the repository for **Perun**, a local overseer/orchestrator for all
> OpenCode projects. It is itself an OpenCode project, so agents working *in*
> this repo should follow the conventions below.
>
> **Internal project content** — this `AGENTS.md`, `perun.md`, `README.md`, the
> `docs/` directory, and the `.perun/` local context/instructions — live in the
> private companion repo **<https://github.com/azagor/perun_internal>**. The
> public `azagor/perun` repository ships only the installable runtime source. When
> working on the full project, ensure the internal repo is overlayed/cloned into
> the workspace so these files are present.

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
| `perun.context.contributing` | adding a source file, a REST endpoint, an MCP tool, a Shared Projects change |
| `perun.context.commands` | syntax check, test suite, dev server, UI dump, install |
| `perun.context.credentials` | Git push credentials and the `workflow` scope trap |
| `perun.context.releasing` | the release procedure: version bump, tag, signed installer |
| `docs/SHARED_PROJECTS_PRD.md` | Shared Projects product requirements: agent approval, policy auto-approve, lifecycle notifications |

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
  (and the relevant `docs/TECHNICAL_DOCUMENTATION.md` section) before searching.
  Prefer Perun's bundled MCP search tools (`perun_unified_search`, `perun_grep`,
  `perun_ast_grep`, `perun_global_grep`, `perun_global_ast_grep`,
  `perun_memory_search`) for discovery and structural search. Use `perun_grep` or
  `perun_ast_grep` only to confirm; avoid raw filesystem `grep` or ad-hoc
  directory scanning.
* **Keep the layers separate.** `registry.py`/`db.py` = persistence;
  `memory.py` = facts + docs; `context.py` = keyed reference docs;
  `docaudit.py` = CODEMAP traceability; `secret_store.py` = encrypted named
  secrets; `orchestrator.py` = process/streaming; `server.py` = HTTP only (no
  business logic); `web/` = presentation only.
* **SQLite access is per-operation.** Always use the `db.db()` context manager;
  never hold a connection across a network call. WAL mode is required.
* **Never block the event loop.** Synchronous FastAPI endpoints run in the
  thread pool; SSE endpoints pump blocking generators through
  `iterate_in_threadpool`. Keep it that way.
* **No secrets in memory.** `memory.is_sensitive()` guards `remember()`; keep
  new storage paths behind the same guard. Provider API keys may be exported
  fresh from `opencode auth export` and injected into child `opencode run`
  processes, but they must never be cached, logged, or persisted by Perun.
* **Browser WebSocket auth uses short-lived tickets.** The dashboard obtains a
  30 s single-use ticket from `/api/perunchat/ws-ticket`; never put a long-lived
  API key in a URL. The `?token=` fallback is opt-in
  (`PERUNCHAT_ALLOW_QUERY_TOKEN`) and every log path must redact it.
* **Key rotation is self-service.** Agents rotate via
  `perunchat_rotate_key`/`POST /api/v1/keys/rotate`; the tool must verify
  `PERUNCHAT_KEY_FILE` is writable before the server revokes the old key.
* **Never abandon an approved proposal.** `reject()` only accepts `pending`, so a
  superseded approval must be withdrawn with `cancel()` (terminal `cancelled`
  status); leaving it `approved` keeps it claimable for its whole lifetime.
  `execution_mode` is empty until an execution actually starts — never infer
  ownership from it on an unclaimed row.
* **Approval pins target content.** `approve()` stores a sha256 of every declared
  target, and claim/apply refuse a target that changed since. Never bypass that
  check to "unblock" a handoff: cancel and re-propose so the approver sees the
  current content.
* **Every project room publishes its operator contact.** Derive the human from
  `perun_owner_name` (or `attention_operator_names`); never add per-project
  operator configuration. The notice is idempotent (a `project_room_welcome`
  activity row) and names *this project's* operator, not another agent's.
* **Routing is advisory, never authoritative.** `perun/routing.py` may choose a
  project or a command, but it must never be the only check on a mutating action:
  the guard allowlist, proposal approval and post-run audit stay in force.
* **Runs are durable at the session layer.** A run is an idempotent prompt
  (`msg_<run_uid>`) admitted to an OpenCode session, owned by a lease and observed
  through the event stream; never make "a run" mean "a child process" again, never
  re-admit without checking `admitted_at`, and keep the reply exactly-once
  (`runs.reply_sent_at`).
* **A TUI run is standalone and user-owned.** `POST /api/runs/tui` records an
  external terminal (`runs.standalone=1`, `runs.pid`); never kill it on server
  shutdown (`ProcessTracker.terminate_all()` is explicit-only), never mark it
  interrupted while its pid is alive, never offer a Stop button for it, and sweep
  it when the pid is gone (age window only as a fallback).
* **Notifications carry a typed target.** `attention_requests.target_kind` +
  `target_id` decide where a reply goes; never infer the context from the current
  project or the newest session. Producers only write rows — the process that owns
  the Telegram poller is the only sender, and every reply runs its adapter before
  the item is closed.
* **Detection is event-driven, polling is the net.** The watcher subscribes to the
  shared `EventHub` as a global subscriber and polls once per debounced burst; the
  `attention_poll_seconds` timer only reconciles. Keep both paths — events make a
  card appear in a second, but the poll is what closes a request answered in the
  TUI and what still works when the stream is down. One `poll_once()` at a time.
* **Never restart Perun under an active run.** `perun stop`/`perun restart` now
  refuse when `/api/runs/active` reports a run (pass `--force` to override); a
  forced restart marks runs `interrupted` and the bridge posts a notice on its
  next start. Never `Stop-Process` a live server by hand.
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

## Versioning and releasing

There is a single source of truth for history: `docs/CHANGELOG.md`. When you
make a notable change, append an entry there and update the "Latest release"
line in `docs/TECHNICAL_DOCUMENTATION.md`.

The release pipeline is `tools/release.py`. It bumps `perun/__init__.py`,
updates the changelog/technical docs, syncs internal project content to
`../perun_internal` via `tools/sync-internal.py --reverse --force`, commits both
repos, tags the public repo, and pushes `master` + the tag. Read that script
before a release and prefer it over hand-crafted git commands. Run
`python -m perun docs audit --all` to check CODEMAP traceability before
tagging.
