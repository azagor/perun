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
  secrets; `backups.py` = pre-change config copies under `PERUN_HOME/backups/`
  (never beside a config, which may live in a git repo); `orchestrator.py` =
  process/streaming; `server.py` = HTTP only (no business logic); `web/` =
  presentation only.
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
* **A proposal is a changeset.** `operations` is the source of truth: one
  `{path, op}` entry per target (`create`/`edit`/`delete`), and one proposal may
  mix all three atomically. `create` targets must be absent and `edit`/`delete`
  targets must exist as regular files at approval, claim, upload/apply and local
  run. `action_type` is only a derived summary (`mixed` when the operations
  differ). Never split one logical change into a proposal chain just because it
  adds a file and edits another.
* **External packages are staged, then approved as a whole.** An upload is never
  applied directly. Perun derives the typed manifest and pre/post hashes itself
  (never trust an agent-authored manifest), stores the archive digest, and waits
  for a second approval by the operator, another rw trusted peer/owner, or
  `policy_auto_approve`. The requester and executor cannot approve their own
  package. `reject_external_package()` keeps the claim so a new archive can be
  uploaded. Apply is atomic, re-checks every pre/postcondition, rolls back on
  failure, and discards the archive on apply/reject/expiry/revoke/delete.
* **Delete is a typed operation.** `guard_delete_file` works only when the
  executing changeset declares `delete` for that exact target; write/edit tools
  are refused for a delete target, and the chat persona's agent permissions deny
  delete outright. A package whose manifest contains a `delete` is operator-only:
  peer/room-owner MCP approval must be refused, so the human applies it from the
  dashboard (or the explicit `policy_auto_approve` path). Never widen the chat
  agent's guard allow to include delete.
* **Pausing project work stops autonomy, not people.** `projects.work_paused` is
  durable; while set, the bridge must not auto-reply in the project room, must
  not ingest its messages for coroutine triggers, must not start the autonomous
  proposal review, and policy auto-approve must leave new proposals pending.
  In-flight runs finish; human chat, explicit approvals and the dashboard remain.
  Operator-directed mentions stay durable while paused (a notification, not
  agent work). Scheduled/time-based coroutines keep their own Pause control; this
  flag suppresses room-driven autonomy only. A room message can never clear the
  flag.
* **Execution is declared, never improvised.** The only command Perun runs on
  its own is a check the operator declared (`project_checks`, written by
  `perun checks add` / the dashboard, never by an MCP tool and never from the
  project tree). A room may run a declared check **by name**
  (`guard_run_check`) and may verify an **executed** proposal's approved script
  only after the operator confirms the exact derived command and its sha256
  (`perun_verify_proposal`); the run is refused if the file changed. Never add a
  tool that accepts a command string, never let a proposal's free text become
  argv, and never widen the guard allowlist so the room persona gets shell.
  Every run is cross-process single-flight per project (with a machine-wide
  cap), gets a scrubbed environment (a per-check `env` map adds toolchain paths
  only, never credentials), caps timeout/output, strips terminal control bytes,
  prunes old history, and records `check_runs` + activity.
* **Docs are part of the proposal pipeline.** The executor is sandboxed to the
  approved targets, so it cannot write `docs/`; never widen the allowlist to let
  it. `docs_sync.after_execution()` runs the installed `/update-docs` command once
  the proposal reaches `executed` (falling back to one changelog bullet when the
  command is missing). That session runs as the restricted **`perun-docs`** agent
  (read anything, write only `docs/**`, `AGENTS.md`, `perun.md`), and its writes
  are diffed and logged as `docs_audit` / `docs_audit_violation`. Keep it
  fire-and-forget: a docs run must never change or delay an execution result.
* **Agent files are user-owned.** `perun-orchestrator`, `perun-docs` (and any
  agent Perun installs) is written once by `perun/managed_agent.py` and refreshed
  only while its bytes still match the sha256 Perun recorded; a hand-edited file
  is never overwritten. `reset()` is the only way back, and it is always an
  explicit user action. A new managed agent must go through `managed_agent.ensure()`
  and never write the file directly.
* **Every project room publishes its operator contact.** Derive the human from
  `perun_owner_name` (or `attention_operator_names`); never add per-project
  operator configuration. The notice is idempotent (a `project_room_welcome`
  activity row) and names *this project's* operator, not another agent's.
* **The control project is local, and shareable only on purpose.** `perun` is
  registered **hidden** so it stays out of project lists and is never deleted;
  hiding is a listing rule, not an access rule. The dashboard lists it only in
  the owner's private workspace (the `.perun/owner-workspace` marker shipped by
  the internal overlay; `PERUN_SHOW_HIDDEN_PROJECTS` overrides), so an installed
  copy or a plain public clone never shows it while the Orchestrator page still
  reaches it. If you want another Perun
  installation to work on it, share it like any project: `shared: true` plus
  participants on the `perunchat_trusted` list, with `rw`/`ro` roles, HITL
  approval and the guard/audit unchanged. `reconcile_all_project_chats()` includes
  hidden projects so such a room is reconciled after a reconnect.
  Telegram is optional - the CLI, TUI, dashboard (loopback + token) and MCP work
  without it, and `telegram_allowed_chats` gates only the bridge. The MCP layer
  has no user identity, so it scopes by cwd: `perun` resolves only from a session
  inside its checkout; a claimed execution runs in the project directory, so
  sharing still works. Do not "fix" a refused hidden lookup by widening
  `_visible()`.
* **Routing is advisory, never authoritative.** `perun/routing.py` may choose a
  project or a command, but it must never be the only check on a mutating action:
  the guard allowlist, proposal approval and post-run audit stay in force.
* **Runs are durable at the session layer.** A run is an idempotent prompt
  (`msg_<run_uid>`) admitted to an OpenCode session, owned by a lease and observed
  through the event stream; never make "a run" mean "a child process" again, never
  re-admit without checking `admitted_at`, and keep the reply exactly-once
  (`runs.reply_sent_at`).
* **Coroutines are a scheduler, not a second execution engine.** Every fire is
  a normal `Orchestrator.start_run`; do not add a parallel runner. A fire is
  idempotent through the unique `runs.coroutine_fire_key`; completion is
  accounted exactly once through `runs.coroutine_accounted_at`. Never fire a
  slot twice to "catch up" — the default is skip, and `catch_up='once'` is a
  single deliberate replay. **Global coroutines are user-wide and run in the
  control plane as the orchestrator; never bind them to a user project or offer
  a project picker for them. Event triggers are project-scoped — a global job
  must be interval/cron/once/manual, never event — and an event job may never
  run in the orchestrator control plane, because its tool access is the risk,
  not the label on the job. An event job defaults to the read-only `plan`
  agent and may not name `perun-orchestrator`; empty-agent event rows are
  filled at startup.**
* **Pause is not Stop.** Pause lets the active run finish and suppresses future
  cycles/iterations; Stop cancels the run and marks the job stopped. Resume
  recomputes the next slot from now and resets a room job's `event_listen_since`.
* **Catch-up never bursts.** A restart must not start a missed slot storm. The
  scheduler skips past slots (or fires exactly one with `catch_up='once'`), and
  a loop parks its next cycle slot in `next_cycle_epoch` while iterations run.
* **Unlimited loops need hard caps.** `max_iterations=0` is allowed only with a
  daily run cap and a cycle wall-clock cap; the stall guard and the per-job
  timeout watchdog are not optional. A loop must never silently occupy the
  last execution slot.
* **Room results go through `coroutine_room_outbox`.** The scheduler may run in
  a different process from the PerunChat WebSocket, so never call
  `post_to_room` from the scheduler. The bridge drains the outbox (on connect
  and every 10 s), applies the room-aware redactor, and uses `in_reply_to`.
  A job with `suppress_auto_reply` owns the message; the bridge must stay quiet
  so the room does not get two answers.
* **Event payloads are untrusted data.** Webhooks and room messages are
  framed as an explicit, random-boundary untrusted block before they reach a
  prompt. Webhook secrets are stored as SHA-256 only and travel in
  `X-Perun-Webhook-Secret`; never put a secret in the URL, because uvicorn's
  access log records the path. The event inbox is bounded and old events are
  dropped; never replay a whole outage as a burst of agent cycles.
* **The scheduler is a singleton with a visible heartbeat.** `serve` owns the
  one scheduler thread; the dashboard shows a stale-heartbeat warning and a
  global pause. Never add a second scheduler or hide a dead one.
* **A TUI run is standalone and user-owned.** `POST /api/runs/tui` records an
  external terminal Perun launched (`runs.standalone=1`, `runs.pid`); never kill
  it on server shutdown (`ProcessTracker.kill_all_explicit()` is explicit-only) and
  never mark it interrupted while its pid is alive. **Stop is the intended way to
  close it**: `POST /api/runs/{uid}/cancel` cancels the orchestrator run and always
  offers the run to `ProcessTracker.terminate()` — even after a restart, when the
  in-memory entry is gone, because `runs.pid` + the `PERUN_RUN_UID` marker still
  identify the terminal. The kill target is the **shell that owns the terminal**
  (`_terminal_owner()`: the outermost `cmd`/`pwsh`/`sh` carrying the marker in its
  command line), not the agent inside it — `cmd /k` and `pwsh -NoExit` keep the
  window open after the agent exits, so killing only the child leaves a prompt and
  an "active" terminal behind. Attribution still gates it: a pid carrying the
  marker, or the live `Popen` handle Perun launched — never the launch-time Windows
  Terminal heuristic (liveness-only). With nothing attributable, Stop finalizes the
  row. Closing that shell ends everything started in the window (the same as
  closing it by hand), including a nested `opencode serve` shared with other
  sessions. Telegram's `/stop` and `/cancel` take the same path (bridge →
  `ORCH.cancel` + `ProcessTracker.terminate`). Sweep the run when the pid is gone
  (age window only as a fallback). **Run resumes the latest TUI chat by default**
  (`opencode --session <id>`); the card's caret starts a new session or picks
  another, and the Agent Console picker defaults to the latest chat. **A session
  whose first run was headless (a coroutine, a one-off job) is never
  auto-resumed**; it stays explicitly selectable and is labeled `headless run`.
  A repeated Run must not silently create a new session. **Focus resolves the
  window through the run's console handle** (a detached `AttachConsole` helper;
  the pseudoconsole's owner is the exact Windows Terminal window), never by title
  alone. `AttachConsole` is process-wide: it must never run inside the server,
  and the cached window is keyed by `(pid, run_uid)`.
* **A working card never offers Run.** The first card action comes from
  `web/js/card_controls.js` (`runControlSpec`): Run when idle, Stop for *any*
  active run (headless cancel, or closing the TUI terminal Perun opened). A
  dashboard TUI run is `standalone=1` for its whole life, including after a
  restart, so suppressing Stop for standalone runs reverted an active card to Run
  and invited a second session — that was a real regression. Keep the decision in
  that module (it is unit-tested) instead of hardcoding a button.
* **Operator-directed room asks are durable.** A room message that names the
  operator creates a `room` attention item **even when Perun also answers** — a
  task like "register these two checks" must not live only in the chat scroll.
  The PerunChat page shows the open-ask count and links to `#/attention`; the
  operator's own messages, Perun's echo and object payloads are ignored. A reply
  in the inbox is posted back to the exact room.
* **Notifications carry a typed target.** `attention_requests.target_kind` +
  `target_id` decide where a reply goes; never infer the context from the current
  project or the newest session. Producers only write rows — the process that owns
  the Telegram poller is the only sender, and every reply runs its adapter before
  the item is closed.
* **Formatting never eats the keyboard.** All Telegram rendering goes through
  `perun/telegram_format.py` (emoji badge + bold title, rich HTML, fence-aware
  chunking, `<tg-time>`). A rejected message is retried as plain text **with
  `reply_markup` kept**; unsupported `style`/`copy_text` fields are degraded
  first, and the keyboard is dropped only as a last resort. Live progress uses
  `sendMessageDraft` in private chats only, throttled, and must never delay or
  duplicate the final reply; `stopped_message_generation` cancels that exact run.
  Keep button text redacted like the card itself.
* **VS Code is optional and local.** The companion connects only over loopback,
  with a single-use ticket bound to one workspace; never put a long-lived token
  in a URL and never trust workspace-level `perun.*` settings (a cloned repo
  could redirect the endpoint). Paths from the editor are validated against the
  project root. `code` launches use a fixed argv; the extension cannot make Perun
  run arbitrary commands. Mode B's terminal id/pid must stay bound to the run so
  Stop/Focus survive a restart. A missing VS Code or extension degrades to the
  `code --goto` reveal and clipboard selection, never to a broken service.
  **The companion is installed per user, never machine-wide and never by
  installing VS Code itself.** `installer.install_vscode_extension()` is the
  only path that installs it: it skips an elevated process (UAC can resolve to
  another account), skips without a VSIX or a network, validates a downloaded
  VSIX as the real `perun.perun-overseer` archive, and is a no-op when that
  version is already installed. `--skip-vscode` opts out; keep the installer's
  `runasoriginaluser` step and the macOS console-user step in place.
* **Detection is event-driven, polling is the net.** The watcher subscribes to the
  shared `EventHub` as a global subscriber and polls once per debounced burst; the
  `attention_poll_seconds` timer only reconciles. Keep both paths — events make a
  card appear in a second, but the poll is what closes a request answered in the
  TUI and what still works when the stream is down. One `poll_once()` at a time.
* **Never restart Perun under an active run.** `perun stop`/`perun restart` now
  refuse when `/api/runs/active` reports a run (pass `--force` to override); a
  forced restart marks runs `interrupted` and the bridge posts a notice on its
  next start. Never `Stop-Process` a live server by hand.
* **Prompt caching is the cost model.** 98% of measured input is prompt-cache
  reads, so never rewrite a session prefix per turn: instruction layers are
  injected only when a session starts (`compose_prompt(new_session=True)`), and
  compaction is a deliberate boundary event (`perun/context_governor.py`) — never
  on an active session, never more often than its cooldown, and never automatic
  for every turn.
* **Local AI is optional and bounded.** `perun/local_llm.py` is the only client;
  every consumer keeps its previous behaviour when the model or runtime is
  missing. Downloads are explicit (`auto` on Prepare or `manual` commands), never
  on import, and a pulled model is verified against `/api/tags`. The local model
  never sits on a run's critical path, never sees a provider credential, and tool
  budgets must never truncate exact file reads (`perun_read_file`, manifests,
  instructions).
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
repos, tags the public repo, and pushes `master` + the tag. It also refuses to
run without the private `.perun/owner-workspace` marker and re-verifies it after
the sync: that marker keeps the control project listed on the owner's dashboard,
and the reverse sync would otherwise delete it. Read that script
before a release and prefer it over hand-crafted git commands. Run
`python -m perun docs audit --all` to check CODEMAP traceability before
tagging.
