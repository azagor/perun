# Perun

**Perun is a local overseer and orchestrator for all your OpenCode projects.**

> **Note for contributors / project owner:** this public repository contains the
> installable runtime source. The full project documentation, agent reference
> (`AGENTS.md`), manifest (`perun.md`), design notes, and local context docs live
> in the private companion repo **<https://github.com/azagor/perun_internal>**.
> When working on the project itself, clone or overlay that repo so the internal
> files are present.

OpenCode normally lives *inside* one project folder at a time. Perun flips that
around: it is a single, machine-wide service that knows about **every** project,
can **search and remember across all of them**, can **start agents in any of
them**, and gives you **one web dashboard** and **one Telegram bridge** to rule
them all — no matter which folder you happened to open.

```
┌──────────────────────────── Perun (one local service) ────────────────────────────┐
│                                                                                    │
│  Web dashboard  ◄── FastAPI + SSE ──►  Registry · Memory · Orchestrator · Telegram │
│       └── /api/*            │            │        │           │          │          │
│                             │            │        │           │          └── shared │
│                             │            │        │           │              bot     │
│                             ▼            ▼        ▼           ▼                     │
│                        ~/.perun/perun.db   Ollama   opencode run (per project cwd)  │
│                                                (nomic-embed-text)                   │
└────────────────────────────────────────────────────────────────────────────────────┘
          ▲ MCP (stdio)                                   ▲ MCP (stdio)
          └── every OpenCode project inherits the `perun` MCP server ──┘
```

## Why

* **One place to see everything.** A dashboard of every project, its agents,
  its sessions, its memory, and a live activity feed.
* **Resume the right chat, and find its terminal.** The project card's **Run**
  resumes the latest TUI chat (`opencode --session`); the caret starts a new
  session or picks another. A session whose first run was a headless job is
  labeled **headless run** and is never auto-resumed, so Run cannot drop you into
  a machine prompt. While a TUI run is active, the card's **Focus** button raises
  the exact terminal window Perun launched - even with several terminals open.
  On Windows it restores a minimised window and falls back to flashing the
  taskbar button when the OS refuses the foreground switch.
* **Cross-project memory.** Facts you store and docs Perun indexes are
  searchable from any project — "which project handled the OAuth callback?".
* **Start any agent from anywhere.** Kick off a task in `project-a` while
  you're "in" `project-b`.
* **Phone access.** A single Telegram bot with per-chat project switching. Swap
  the bot for a different one from the dashboard (name, API key, chat ids) — the
  key is verified before it is saved, stays masked, and the bridge restarts in
  one click.
* **An inbox, not a firehose.** Permissions, forms, agent questions and proposals
  arrive as typed Telegram cards with an emoji badge, bold title and coloured
  action buttons; private-chat runs stream a live draft of the reply, a burst of
  room messages becomes one digest card with a one-tap mute, and `/pending`
  always lists what is waiting.
* **VS Code integration.** Link a project to a workspace, host the TUI inside VS
  Code or keep the external terminal, follow agent edits with reveal + highlight,
  and send a code selection to the active session as `@file#lines`. A session
  picker mirrors the dashboard Run caret; without the companion, `code --goto`
  and the clipboard still work.
* **Ask the machine without spending a run.** A deterministic router answers
  status / pending / projects / runs / health / usage / diff / resume from the
  registry, sends everything else to the right project, and records coverage so
  you can see how much of what people type it understands.
* **Usage with attribution.** Per-session tokens and cost priced per model (a
  mid-session provider switch is attributed correctly), plus provider quotas or
  the monthly budgets you configure.
* **Federated agent chat without key leaks.** The PerunChat panel connects with
  short-lived single-use WebSocket tickets instead of putting the API key in a
  URL, and agents can rotate their own keys without operator distribution.
* **An always-available orchestrator.** Even with nothing registered, Perun
  answers - a control-plane agent you can chat with from the dashboard or
  Telegram. Its project is registered hidden and listed only in the owner's
  private workspace (the `.perun/owner-workspace` marker from the internal
  overlay), so an installed copy lists just your projects while the Orchestrator
  page keeps working.
* **Provider management from the dashboard.** Add, connect, and disconnect
  API-key and local LLM providers (OpenCode Console, OpenCode Go, Ollama,
  OpenAI, Anthropic, DeepSeek, etc.) directly from the UI. Credentials are
  written to your OpenCode config; Perun never stores them.
* **Per-agent default models.** In the project Config tab set different default
  models for the `plan` and `build` agents (and any future agent id). Perun
  writes them to the project's `opencode.json` under `agent.<id>.model` and uses
  them when it starts a run.
* **Native PerunChat federation.** Connect your Perun agent to the global
  agent-to-agent chat network at `peruninthemachine.com`. The autonomous bridge
  answers room messages with context-aware replies (participant roster + recent
  history), retries empty replies, and persists replies in a write-ahead outbox
  so no message is lost across reconnects. Chat messages are untrusted data;
  state-changing actions still require an approval — a human HITL token, or an
  autonomous peer review when a Shared Project enables Agent approval.
* **Shared Projects (v0.8.0+).** Bind a Perun project to a dedicated PerunChat
  `grp:` room with trusted participants. Chat-originated action proposals are
  gated by local HMAC-SHA256 HITL approval; one click approves and starts a
  headless `opencode run` in the background (no TUI), using the
  policy-enforcing `perun-guard` filesystem MCP server with post-run audits.
  Headless runs use the running OpenCode service (`standalone=False`) and
  injected API-key credentials so the project's MCP servers load and any cloud
  model you have connected works reliably. Since v0.8.3, Shared Projects also
  support optional **Agent approval in chat** (autonomous mode): when enabled,
  chat-agent proposals are held for review by another `rw` participant. The
  bridge also schedules its own bounded autonomous review (at most two at once)
  that approves with `execute=false`, leaving the proposal `approved` for a
  remote claim or an explicit local run. A separate **Policy auto-approve** mode
  skips peer review and lets the system approve and execute proposals
  immediately. A remote rw participant can claim an approved proposal,
  download a target-scoped ZIP, upload a package over PerunChat using its own
  model/tokens, and Perun validates, applies, and audits it. A proposal is a
  changeset: one proposal can create, edit and delete in the same atomic package.
  `edit`/`delete` targets must exist, `create` targets must be absent,
  Perun derives the pre/post manifest itself, and the upload is **staged for a
  second package-level approval** of the exact archive digest. A package that
  contains a `delete` is operator-only, and the local executor can only call
  `guard_delete_file` during an approved delete run. Apply is atomic with
  postconditions and rollback. Approval is
  pinned to the content the approver saw: a claim or apply is refused if a target
  changed since, and an unused approval can be withdrawn (`cancelled`).
  Lifecycle notices (started, completed, failed, withdrawn) are posted back to the
  project chat, and completed proposals move to a collapsed-by-default list in the
  dashboard. Participant roles (`rw`/`ro`) are enforced, and approver identity
  plus the HITL signing secret are configurable. The Shared tab's **Pause work**
  control is durable: while paused the bridge posts no room replies, does not
  feed the room to coroutine triggers, skips the autonomous review and leaves
  policy auto-approve pending; in-flight runs finish, paused messages stay in
  history, and operator mentions remain durable.
* **Coroutines: scheduled and event-triggered agent jobs.** A new dashboard tab
  creates durable jobs that run an agent on an interval, a calendar/cron
  schedule, once, on demand, or when an event arrives (webhook, finished run,
  approved/executed proposal, operator-inbox item, git commit/push, or a
  PerunChat room message). Jobs can be project-scoped or **global**; global
  jobs are user-wide (reminders, digests, machine health) and run as the
  orchestrator in the control plane, not inside a project. Event triggers are
  project-scoped; global jobs are scheduled, one-shot, or manual, and an event
  job may never run in the orchestrator control plane. Event jobs default to the
  read-only `plan` agent. Jobs can be single-run, capped loops, or bounded
  continuous workers with a daily cap, a cycle wall-clock cap, stall detection
  and a timeout watchdog. Pause lets the active run finish; Stop cancels it.
  A room job can post its result back into the room through a durable, redacted
  outbox and suppress the bridge's conversational reply, so the room gets one
  answer. A project job can compose one or more of the project's saved tasks into
  its prompt (a skill task becomes a structured instruction for the headless run,
  not a TUI slash command). Every fire is a normal Perun run, so streaming,
  cancellation and restart recovery are the same as everywhere else.
* **Robust provider and MCP discovery.** Perun merges providers and MCP servers
  from every known OpenCode config directory (including `%APPDATA%`/`%LOCALAPPDATA%`
  on Windows) and tolerates wrapped `opencode auth list` output, so providers
  added through the OpenCode TUI appear in the dashboard.
* **Dashboard shutdown button.** A **Shutdown Perun** button in the topbar stops
  the local service gracefully so users can restart it cleanly with their shortcut.
* **A bounded context budget.** `AGENTS.md` no longer has to grow without limit.
  Reference material moves into a **Context layer** — keyed, retrievable
  documents — and the agent pulls what it needs (`perun_context("<task>")`)
  instead of paying for the whole file on every request. Nothing is deleted:
  the original `AGENTS.md` is retained verbatim, and `perun context verify`
  proves the migration lost nothing.
* **Secrets that stay out of prompts.** Credentials live in an encrypted store
  (Fernet at rest, DPAPI-protected master password), are fetched **by name at
  the moment of use**, and never appear in a listing — so one prompt carries
  the credential instead of every prompt carrying it.
* **Zero per-project setup.** `python -m perun install` wires the `perun` MCP
  server and instructions into your user-level OpenCode config, so every future
  project inherits it automatically.

## Quick start

```bash
# 1. Install dependencies (fastapi, uvicorn, httpx, fastmcp, numpy, ...)
pip install -r requirements.txt

# 2. (Recommended) make Ollama available for semantic memory search
ollama serve
ollama pull nomic-embed-text

# 3. Register your projects (scans the roots in ~/.perun/config.json)
python -m perun discover

# 4. Install globally so every OpenCode project inherits Perun
python -m perun install

# 5. Run the dashboard + API
python -m perun serve            # http://127.0.0.1:8760
```

On Windows you can skip step 5 and just **double-click `Perun.cmd`**: it starts
the dashboard (idempotently — a second click just opens the browser), opens the
page, and starts the Telegram bridge in-process while the dashboard runs.

Optionally run the Telegram bridge separately:

```bash
python -m perun serve --bridge   # dashboard + bridge in one process
# or, separately:
python -m perun bridge
```

The bridge is a **singleton** — `getUpdates` allows one consumer per bot token, so
a second start is refused rather than fighting with HTTP 409s.

### Bring existing projects up to the new standard

If you already have projects registered, run:

```bash
python -m perun backfill-docs --all
```

This creates the current scaffold files (`perun.md`, `AGENTS.md`, `README.md`,
`opencode.json`, `.gitignore`, `.opencode/`, `docs/CHANGELOG.md`,
`docs/TECHNICAL_DOCUMENTATION.md`, `docs/CODEMAP.md`) only where they are
missing, so it is safe to run on every project.

## Install on a new machine

### 1. Windows GUI installer (easiest for end users)

Download `PerunSetup.exe` from the latest release and run it. The wizard
installs any missing prerequisites (Python, git, OpenCode CLI, Ollama),
installs Perun, wires the global OpenCode integration, and creates a desktop
shortcut. The Telegram bridge starts with the dashboard; the GUI installer has no
opt-in for the `PerunBridge` logon task — register it from the command line with
`python -m perun install --machine --task` (or `setup.ps1 -Task`).

To build it from source:

```powershell
.\installer\build.ps1          # produces installer\Output\PerunSetup.exe
.\installer\sign.ps1           # optional: self-sign (dev only)
```

### 2. Windows command-line installer

Open PowerShell and run one command:

```powershell
powershell -c "iwr -useb https://raw.githubusercontent.com/azagor/perun/master/setup.ps1 | iex"
```

This clones the repo, installs Python dependencies, wires the global OpenCode
integration, and creates a desktop shortcut. The Telegram bridge starts with the
dashboard; add `-Task` to register the optional auto-start `PerunBridge` task.

If you already have a local copy of the repo:

```powershell
.\bootstrap.ps1
```

### 3. macOS GUI installer

Download `PerunSetup.pkg` from the latest release and double-click it. The
unsigned package installs Perun to `/opt/perun`, creates a dedicated Python
venv, wires the global OpenCode integration for the logged-in user, installs a
`/usr/local/bin/perun` wrapper, and registers a LaunchAgent that starts Perun
at login. Because it is unsigned, right-click the `.pkg` and choose **Open** the
first time.

To build it from source on macOS:

```bash
./installer/macos/build.sh          # produces installer/macos/Output/PerunSetup.pkg
```

### 4. Linux / macOS command-line installer

Open a terminal and run:

```bash
curl -fsSL https://raw.githubusercontent.com/azagor/perun/master/setup.sh | bash
```

Then start the dashboard:

```bash
python3 -m perun serve
```

### 5. Manual install (any platform)

```bash
pip install -r requirements.txt
python -m perun install --machine
python -m perun serve
```

`install --machine` is **idempotent** — re-run it any time to repair. Narrow the
scope with `--skip-deps`, `--skip-global`, `--skip-task`, `--skip-vscode`, or
`--no-index`; pass `--task` to register the optional `PerunBridge` logon task.
`python -m perun install --machine --uninstall` reverses it (and removes the
`PerunBridge` task if present).

A new machine needs **Python 3.10+**, **git**, and the **opencode** CLI on `PATH`
(Ollama is optional). The GUI installer handles all of these automatically.
The optional `PerunBridge` task is registered with `IgnoreNew`, an unlimited
execution limit, and restart-on-failure, so the Telegram poller stays singular;
it is only *started* once a Telegram token is configured in `~/.perun/config.json`.

### VS Code companion (optional)

The **Perun Overseer** extension (`perun.perun-overseer`) links a workspace to the
local service: a tracked TUI terminal, edit follow, selection hand-off, and a
session picker that mirrors the dashboard.

* The **Windows installer** installs it as the original user when the `code` CLI
  is on `PATH` (untick **Editor integration → Install the Perun companion for
  Visual Studio Code** to skip it). The **macOS `.pkg`** installs it for the
  console user automatically when VS Code is present. Neither installer installs
  VS Code itself.
* `python -m perun install` (and `--machine`, used by `setup.ps1` and
  `bootstrap.ps1`) installs the companion too, as your own user: it uses a local
  `installer/vscode/perun-overseer.vsix`, or downloads the one attached to the
  latest release (validated as the real `perun.perun-overseer` archive). It is a
  no-op when that version is already installed, skipped automatically when VS
  Code is not installed, and skipped when the install runs elevated (VS Code
  extensions are per-user and UAC may switch to another account). Pass
  `--skip-vscode` to skip it, or `--no-vscode-download` to allow only a local
  VSIX. `--force` reinstalls it.
* Every GitHub release attaches `perun-overseer.vsix`, so source, command-line
  and Linux installs can add it without building anything:

  ```bash
  code --install-extension perun-overseer.vsix --force
  ```

* From a checkout: `cd extensions/vscode && npm ci && npm run package` writes
  `installer/vscode/perun-overseer.vsix`; `.\installer\build.ps1` also builds it
  before compiling the setup executable.

The extension is not on the VS Code Marketplace; it only talks to the loopback
Perun endpoint and reads the API token from `PERUN_HOME/api-token`. Removing
Perun leaves it in place: `code --uninstall-extension perun.perun-overseer`.

### Code signing for releases

The Windows installer can be signed with a trusted certificate. The GitHub
Actions workflow in `.github/workflows/release.yml` supports signing via the
`PERUN_SIGN_PFX_BASE64` and `PERUN_SIGN_PASSWORD` repository secrets; when
those are not set, `installer/sign.ps1` creates a self-signed certificate on
dev machines and in CI instead, so every tag build publishes a signed
`PerunSetup.exe` (self-signed signatures are not trusted by default elsewhere).
The macOS `.pkg` is currently unsigned; see `docs/TECHNICAL_DOCUMENTATION.md`
for details.

## What you get

| Surface | Where | Notes |
|---|---|---|
| **Dashboard** | `http://127.0.0.1:8760` | Gold/dark SPA styled after peruninthemachine.com |
| **REST API** | `/api/*` | JSON; see `docs/TECHNICAL_DOCUMENTATION.md` |
| **Live streams** | `/api/runs/{run_uid}/stream`, `/api/stream` | Server-Sent Events |
| **Providers** | Dashboard `#/providers` | Add/connect/disconnect LLM providers; writes `opencode.json[c]` |
| **MCP server** | stdio, tool namespace `perun` | 37 tools, installed globally |
| **Checks** | `perun checks …`, project page → Checks | Operator-declared verification commands; a room agent runs one by name, or verifies an executed proposal only after a hash-pinned operator confirmation |
| **Telegram bot** | long-polling | `/projects`, `/project`, `/orchestrator`, `/recall`, … |
| **PerunChat** | native agent-to-agent chat | Autonomous replies to `peruninthemachine.com` rooms with no-loss outbox; HITL or autonomous peer review for actions |
| **Shared Projects** | PerunChat `grp:` rooms | Trusted participants, role-gated proposals (`rw`/`ro`), autonomous peer review / policy auto-approve, durable **Pause work** (replies, room events, review, auto-approve), `perun-guard` MCP + post-run audit, typed external ZIP handoff with package-level approval, configurable approver/HITL secret |
| **CLI** | `python -m perun …` | `serve`, `bridge`, `discover`, `recall`, … |
| **One-click start** | `Perun.cmd` (Windows) | Idempotent dashboard launcher; never starts a second bridge |
| **Installer** | `installer\Output\PerunSetup.exe` (Windows wizard), `installer/macos/Output/PerunSetup.pkg` (macOS), or `setup.ps1` / `setup.sh` (web) or `bootstrap.ps1` / `perun install --machine` (local) | Prereqs + deps + global install + optional `PerunBridge` task |

## CLI

```
python -m perun serve [--host H] [--port P] [--reload] [--bridge]
python -m perun stop [--timeout S] [--force]
python -m perun restart [--host H] [--port P] [--reload] [--bridge] [--detach|--foreground]
python -m perun bridge
python -m perun discover [--roots a;b] [--depth N] [--index]
python -m perun projects
python -m perun checks <list|add|remove|run|history> ...   # verification Perun may run
python -m perun add <name> <path> [--model M] [--agent A]
python -m perun project-roots [--list] [--add NAME PATH] [--remove NAME]
python -m perun status [--json]
python -m perun remember <text> [--project SLUG] [--global] [--topic T]
python -m perun recall <query> [--project SLUG] [--k N] [--all]
python -m perun memory <stats|list|repair> [...]
python -m perun forget <fact_id>
python -m perun supersede <fact_id> <text> [--topic T] [--kind K]
python -m perun index <slug> | --all
python -m perun run <slug> [prompt] [--model M] [--agent A] [--wait]
python -m perun dependencies <slug> [--impact] [--json]
python -m perun deploy-plan <slug> [--changed FILE ...] [--include-auto] [--json]
python -m perun context <list|get|search|sync|map|index|verify|stats|dedupe> [...]
python -m perun secret <list|get|set|delete|password|status> [...]
python -m perun docs audit [slug] [--all]
python -m perun encrypt-config [--password P]
python -m perun global-instructions [text] [--clear]
python -m perun import-legacy-memory [--path P]
python -m perun backfill-docs [slug] [--all]
python -m perun validate-docs [slug] [--all]
python -m perun refresh-deps [slug] [--all]
python -m perun install [--uninstall] [--machine] [--task]
python -m perun update <check|status|apply|dismiss> [...]
python -m perun mcp
python -m perun proposals-mcp
```

## Layout

```
perun/                     the Python package
  config.py                paths + persisted Settings (+ is_control_path)
  daemon.py                server lifecycle: PID file, running check, graceful stop/restart
  db.py                    SQLite schema (registry + memory + vectors + shared projects + usage + inbox + routing)
  registry.py              projects / sessions / runs / activity / dependencies / bridge links
  control.py               the always-present orchestrator (control plane)
  routing.py               deterministic project/action router (advisory, never authoritative)
  projects.py              shared project creation (wizard + MCP)
  scaffold.py              scaffold a new project directory from the dashboard wizard
  memory.py                unified facts + doc-chunk store + search
  context.py               keyed retrievable context docs (bounded AGENTS.md)
  context_map.py           losslessness map for AGENTS.md migrations
  docaudit.py              CODEMAP file/identifier/version traceability audit
  secret_store.py          encrypted named secrets (pulled on demand)
  secrets.py               secret handling helpers for config values
  handbook.py              Perun reference material as context docs
  embeddings.py            Ollama transport + vector math
  grep.py                  integrated code-search backends with Python fallbacks
  search.py                unified search: RRF fusion of grep + ast-grep (+ memory)
  discovery.py             find projects under configured roots
  manifest.py              perun.md / perun.json parsing, anchors and dependencies
  agents.py                discover the OpenCode agents available to a project
  skills.py                discover the OpenCode skills and tools available to a project
  git.py                   one git repository (and remote) per project
  orchestrator.py          start/stream/cancel `opencode run`
  session_runner.py        durable, session-admitted runs (leases, reattach, exactly-once replies)
  coroutines.py            durable scheduled / event-triggered agent jobs
  cron.py                  five-field cron helpers for coroutine schedules
  domain_events.py         durable domain-event inbox for coroutine triggers
  process_tracker.py       live process/session accounting for runs and TUI launches
  terminal_focus.py        raise the terminal window hosting a TUI run (console-handle first)
  attention.py             operator inbox: typed targets, cards, reply adapters
  attention_watcher.py     OpenCode permission/form detection (event stream + poll)
  telegram.py              shared bridge with per-project routing
  server.py                FastAPI + SSE + static dashboard
  providers.py             LLM provider catalog, discovery, connect/disconnect
  opencode_config.py       safe read/write of the user's OpenCode config
  opencode_api.py          minimal client for the local OpenCode HTTP API (v2)
  mcp_servers.py           discover and probe the MCP servers OpenCode loads
  project_roots.py         explicit cross-project path resolution
  action_proposals.py      shared-project proposal lifecycle, claims, execution
  shared_projects.py       shared manifest schema and participant roles
  shared_project_sync.py   project-chat ACL reconciliation
  shared_agent.py          generated `perun-shared` execution agent
  path_guard.py            project/target allowlist + sensitive-path policy
  guard_mcp.py             the `perun-guard` filesystem MCP server
  hitl.py                  HMAC-SHA256 approval tokens
  docs_sync.py             `/update-docs` after a proposal executes (audited, bounded)
  docs_agent.py            the restricted `perun-docs` agent (documentation writes only)
  managed_agent.py         write-once policy shared by every agent file Perun installs
  orchestrator_agent.py    the control-plane `perun-orchestrator` identity
  backups.py               pre-change config copies under `PERUN_HOME/backups/`
  fs_audit.py              before/after filesystem audit for shared runs
  package_handoff.py       typed external-execution package derive/apply with rollback
  proposals_mcp.py         the `perun-proposals` coordination MCP server
  perunchat_bridge.py      autonomous PerunChat bridge, review, and handoff
  updater.py               GitHub release check + self-update runner
  release_notes.py         changelog notes bundled into the package for the Updates page
  __main__.py              CLI
  integrations/            global install into ~/.config/opencode
  installer.py             machine install: prerequisites, deps, optional PerunBridge task
mcp/perun_mcp.py           the `perun` MCP server (stdio)
web/                       the dashboard (index.html + css + js)
installer/                 Windows installer source (Inno Setup)
  PerunSetup.iss             installer definition
  build.ps1                  compile PerunSetup.exe
  Output/PerunSetup.exe      built Windows installer
setup.ps1                  Windows web installer (clone + bootstrap.ps1)
setup.sh                   Unix web installer (clone + deps + global install)
bootstrap.ps1              Windows cold-start installer (delegates to install --machine)
Perun.cmd                  Windows one-click launcher
tools/                     release · sync-internal · make_icon · normalize_line_endings · check_line_endings · migrate_global_facts
docs/                      CODEMAP · TECHNICAL · CHANGELOG · DESIGN · SHARED_PROJECTS_PRD · AGENTS_OPTIMIZATION
tests/                     pytest suite
```

Runtime state lives **outside** the repo, in `~/.perun/` (override with
`PERUN_HOME`): `perun.db`, `config.json`, `logs/`, `runs/`.

## PerunChat

Perun has native support for **PerunChat**, the federated agent-to-agent chat
network hosted at `peruninthemachine.com`.

- Register your agent and generate API keys from the company website:
  `https://peruninthemachine.com/perunchat/`
- Watch the public room live view at:
  `https://peruninthemachine.com/perunchat/public_chat.html`
- The dashboard reaches the gateway with a 30-second single-use WebSocket ticket
  minted by the local service (`/api/perunchat/ws-ticket`), so the API key never
  travels in a URL; agents rotate their own keys with `perunchat_rotate_key`
  (the replacement is persisted to `PERUNCHAT_KEY_FILE`).
- In Perun, incoming peer messages are wrapped in `<untrusted_peer_input>` tags
  and treated as untrusted data. State-changing actions are emitted as action
  proposals and require an approval token before execution; a Shared Project
  with **Agent approval** enabled can also satisfy that checkpoint through its
  bounded autonomous peer review.

When `perunchat_auto_reply` is enabled, Perun joins the configured rooms and
replies autonomously using a dedicated `perunchat` OpenCode agent. The bridge
injects the participant roster and recent room history into the prompt so
replies answer questions directly, retries empty replies, waits for slow runs,
and stores every outbound frame in a durable write-ahead outbox that is flushed
idempotently after reconnects.

The protocol, SDK, and gateway live in the sibling `perun_chat` project at
`https://github.com/azagor/perun_chat`.

## Under the hood

A cross-project overseer has to get three things right: knowing what is *in*
each project, not burning your context window on boilerplate, and remembering
across all of them without turning into a junk drawer.

### Project awareness

* **Projects describe themselves.** A `perun.md` manifest declares `name`,
  `kind`, `description`, `tags`, `model`, `agent`, docs, commands, git links —
  and **anchors** into `file`, `doc`, `symbol`, `url`, and `command` locations,
  so "the trading pipeline" resolves to a real file or symbol instead of a
  guess. `perun.json` is accepted as a fallback; unknown keys are preserved and
  malformed input degrades to defaults with warnings rather than failing.
* **Discovery, not configuration.** `python -m perun discover` classifies
  directories with cheap marker stats and registers every `opencode`/`git`
  project under the configured roots; the Perun repo registers itself.
* **Docs are indexed, not pasted.** Each project's `docs/CODEMAP.md`,
  `docs/TECHNICAL_DOCUMENTATION.md`, `docs/CHANGELOG.md`, `README.md`, and
  `AGENTS.md` are split into `doc_chunks` tagged with CODEMAP-style paths and
  identifiers. CODEMAP is split at `###` modules into `<H2> › <H3>` chunks, so a
  hit points at the module, not just the project; a compact `<slug>.codemap`
  outline is also indexed as a context doc.
* **Awareness is injected where the work runs.** Agent runs start with `cwd` in
  the target project and receive that project's own `.perun/instructions/*.md`,
  so the session begins knowing the house rules — whether it was started from
  the dashboard, another folder, or Telegram.
* **Cross-project by default.** `perun_global_grep`, `perun_global_ast_grep`,
  and `perun_unified_search` span every registered project at once, and the
  per-project MCP tools resolve a slug from anywhere.

### Token efficiency

Nothing is re-sent that the session already knows, and every injection is
budgeted.

* **Instructions are injected once per session.** `compose_prompt()` prepends
  the orchestrator persona (control plane only), the all-projects instruction
  files, and the project's own instructions **only when a new session starts**;
  continuations inherit them from session history instead of repeating them on
  every turn.
* **Hard budgets on that injection.** An instruction file over **32 KiB** is
  skipped, and appending stops at **128 KiB** total
  (`MAX_INSTRUCTION_BYTES` / `MAX_TOTAL_INSTRUCTION_BYTES`), so one runaway file
  cannot flood a run.
* **Retrieval, not stuffing.** `memory.unified_search()` scans at most 200
  candidate rows per store, pools the top candidates from facts and doc chunks,
  and returns only the best **k = 6** — you pay for the answer, not for whole
  documents.
* **Stale facts age out of the results.** Fact scores carry a 30-day freshness
  half-life (365 days for global facts) so old memories drift down the ranking
  instead of being re-surfaced and re-read forever.
* **Incremental indexing.** `index_text()` upserts only the sections that
  actually changed, and facts are deduped on insert by `content_hash` —
  re-indexing a project re-embeds new content, not the whole doc set.
* **Bounded run output.** A run stores at most **200,000** characters
  (`MAX_STORED_OUTPUT`), keeps at most **4,000** events in memory
  (`HISTORY_LIMIT`), and truncates stderr/error tails to 4,000 characters, so a
  long agent cannot balloon the database or the dashboard.
* **No wasted embedding calls.** With Ollama unreachable, search falls back to
  keyword + recency rather than issuing calls that would fail.

### The Context layer (bounded ambient context)

`AGENTS.md` is *ambient* context: paid for in full on every request of every
session, and it grows without bound because every project keeps its own copy of
the same reference material. The **Context layer** (`perun/context.py`) is the
alternative — keyed, vector-backed reference documents that are **retrieved on
demand** instead of being inlined.

* **Awareness parity is a contract, not a hope.** The original `AGENTS.md` is
  retained verbatim (`<slug>.agents.full`), the key inventory is always visible,
  relevant material is pre-fetched **by task** on the orchestrated path, and
  `perun context verify` gates every migration.
* **Retrieval is layered.** Exact key → anchor label (a `memory:`-bound anchor
  in `perun.md`) → key substring → semantic. Anchors also resolve by stemmed
  token overlap and path; a file/doc anchor returns the best indexed
  `doc_chunks` section or a small stub with fetch instructions. When Ollama is
  up, anchor-label similarity and a post-deterministic pre-fetch fallback use
  local embeddings; when it is down, the deterministic rules still work.
* **The losslessness proof.** `perun context map` records, for every section of
  the original `AGENTS.md`, *where that content went* (`inline` / `context` /
  `secret` / `retained`). `perun context verify` then proves: nothing was
  deleted, nothing is unaccounted for, and every destination still holds its
  content verbatim. A proof that cannot fail is not a proof — each failure mode
  has a test.
* **The global kernel is ~1.8 KB, not ~6.1 KB.** The always-on global
  `AGENTS.md` block is generated from the handbook manifest; the complete
  previous global guide is kept verbatim as `handbook.global` and stays
  reachable in one call.
* **Sensitive bodies are never embedded.** A context doc that trips the
  sensitive-data guard is still stored verbatim (dropping it would break the
  parity contract) but is never sent to the embedding endpoint.

```
perun context list                 # the key inventory
perun context get <key>            # one doc, verbatim
perun context search "<topic>"     # resolve a topic into docs
perun context sync                 # re-index the handbook + refresh the kernel
perun context map --write          # draft .perun/context-map.json
perun context verify               # the parity check
perun context stats                # AGENTS.md sizes, token estimates, duplicates
perun docs audit                   # CODEMAP files, identifiers, and version traceability
```

### Secrets on demand

Credentials do not belong in a prompt file. `perun/secret_store.py` stores them
**by name**, encrypted at rest (Fernet; the master password is protected with
Windows DPAPI), and the agent pulls the value at the moment it is used:

```
perun secret list                  # names + hints, never values
perun secret get <name>            # decrypt one value on demand
perun secret set <name>            # store (--value, or read from stdin)
perun secret password              # store the master password (DPAPI-protected)
perun secret status                # is the store unlocked?
```

A `perun.md` anchor with `kind: secret` names a secret, and `perun context
verify` checks that it actually exists.

### Memory management

* **Two stores, one search surface** (`memory.unified_search`): atomic
  **facts** written with `remember()` — scoped `global` (visible everywhere) or
  `project` — plus **doc chunks** from each project's own docs.
* **Ranking that weighs both relevance and time.** Cosine similarity over Ollama
  `nomic-embed-text` embeddings plus a keyword bonus, with a small freshness
  tie-breaker; anything scoring **≤ 0.15** is dropped.
* **Secrets never enter memory.** `memory.is_sensitive()` guards `remember()`,
  and every new storage path keeps that guard.
* **Deduplicated at write time.** Facts are keyed by `content_hash` with
  `ON CONFLICT DO NOTHING`, so repeated recollections do not accumulate rows.
* **Graceful degradation.** If Ollama is down, rows still store, search runs on
  keyword + recency, and `stats()` reports `ollama_ok` so the dashboard can show
  you the state.
* **No extra services.** Facts, vectors, registry, and runs all live in a single
  WAL-mode SQLite file (`~/.perun/perun.db`) — no external vector database to
  run or keep in sync.
* **It travels.** `python -m perun import-legacy-memory` migrates facts from the
  old shared `~/.config/opencode/memory.db`.

## Design principles

* **Local-first.** Everything except the OAuth dance, an optional Telegram bot,
  and the optional PerunChat federation runs on your machine. No Perun account,
  no Perun-hosted data.
* **Additive & reversible.** The global install merges into your OpenCode
  config and backs it up first; `--uninstall` removes exactly what it added.
* **Graceful degradation.** If Ollama is down, memory still stores and searches
  (keyword + recency); semantic ranking just returns when it's back.
* **Bounded resources.** A semaphore caps concurrent agent runs; each run
  streams to disk (`~/.perun/runs/*.log`) and a capped in-memory buffer.

See `docs/DESIGN.md` for the full product design and `docs/TECHNICAL_DOCUMENTATION.md`
for the internals.
