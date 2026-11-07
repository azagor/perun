# Perun — Design

This document explains *what Perun is for* and *why it is shaped the way it is*.
It is the reference for anyone extending the product or the UI.

---

## 1. The problem

OpenCode is project-scoped. You open a folder, you get an agent that knows about
that folder. As soon as you run more than one project — which is the norm —
three things break down:

1. **You lose the map.** There's no place that lists all your projects, their
   state, or what their agents have been doing.
2. **Knowledge is siloed.** A decision recorded in one project's docs or memory
   is invisible from another. You re-explain context constantly.
3. **You are the router.** To start work in project B while "in" project A, you
   open another terminal, `cd`, and lose the thread — and you can't reach any of
   it from your phone.

Perun's job is to be the **missing top layer**: one local service that holds the
map, the memory, the orchestration, and a remote control.

## 2. Design principles

1. **Local-first.** One local service, one SQLite file, no Perun account. The
   only network dependencies are optional (Telegram, Ollama, PerunChat), and each
   degrades to a clear message when unavailable. Perun itself is one process, but
   it deliberately spawns short-lived children: `opencode run` per agent run, a
   stdio MCP server per OpenCode session, and a detached `serve` after `restart`.
2. **Inherited, not installed per project.** A global MCP server + global
   instructions mean *every* project — including ones you create later —
   automatically gets Perun's tools. No per-project setup.
3. **Bounded ambient context.** `AGENTS.md` is paid for on every request, so it
   is a generated **kernel** (1,832 B) that points at a keyed **Context layer**
   retrieved on demand. Nothing is deleted: the original text is retained
   verbatim, and `perun context verify` proves the migration was lossless.
4. **Additive and reversible.** Touching a user's global OpenCode config is
   invasive, so we back up first, merge only our keys, and can remove exactly
   what we added.
5. **Graceful degradation everywhere.** No Ollama → keyword search still works.
   No token → bridge stays dormant. No `opencode` → a clear error, not a crash.
6. **Bounded, observable.** Cap concurrent runs; stream everything; log raw
   output; expose a live activity feed. The overseer must itself be watchable.
7. **Clarity over cleverness.** The UI shows plain labels, real statuses, and
   one obvious action per card. No hidden state.
8. **Execution is declared, never improvised.** A chat persona has no shell.
   The only command it may run is a **check the operator declared** (by name,
   never by command text) or an **executed proposal's approved script**, after
   the operator sees the exact command and its sha256 and confirms. Declarations
   live in Perun's own store, not in the project, so a room message or an
   approved proposal can never widen what runs.

## 3. Mental model (what the user sees)

```
        ┌──────────────┐        Perun is a "control room".
        │   PROJECTS   │        ─ each project is a card
        │  ▤ ▤ ▤ ▤ ▤   │        ─ each card can Run, or Stop while
        └──────┬───────┘          the project is working / Open /
               │ select             Index docs / Delete
        ┌──────▼───────┐        A project has:
        │   PROJECT    │          Overview · Console · Sessions ·
        │  Overview    │          Memory · Activity · Config
        │  Console     │
        └──────┬───────┘
               │ run
        ┌──────▼───────┐        An agent run streams live, and its
        │ LIVE OUTPUT  │        transcript is kept on disk.
        └──────────────┘

        MEMORY  ── searchable across ALL projects at once
        ACTIVITY ── one live firehose of everything
        ORCHESTRATOR ── talk to Perun itself (works with zero projects)
        TELEGRAM ── the same control room, from your phone
```

A working card also offers **Focus**, which raises the TUI terminal Perun
launched for that run (Windows; a taskbar flash when the OS refuses the
foreground switch), so several open terminals never become a guessing game.

Three nouns carry the whole product: **Project**, **Run**, **Memory**. Every
screen maps to one of them.

## 4. Information architecture

| Route | Purpose | Primary question it answers |
|---|---|---|
| `#/dashboard` | Command overview | "What's the state of everything?" |
| `#/orchestrator` | Orchestrator chat | "Talk to Perun itself, with nothing set up." |
| `#/projects` | Registry | "What projects do I have?" |
| `#/project/{slug}` | Project detail | "Everything about this one." |
| `#/console[/{slug}]` | Agent launcher | "Run an agent and watch it work." |
| `#/coroutines` | Scheduled jobs | "What runs on its own, and when?" |
| `#/memory` | Memory explorer | "What do we know / have we decided?" |
| `#/activity` | Live feed | "What is happening right now?" |
| `#/mcp` | MCP server inventory | "Which MCP servers does OpenCode load, and are they healthy?" |
| `#/telegram` | Bridge control | "Can I reach this from my phone?" |
| `#/perunchat` | Native agent chat | "Talk to other agents on the PerunChat network." |
| `#/providers` | LLM providers | "Which models can my agents use?" |
| `#/updates` | Self-update | "Is there a newer Perun, and is it safe to apply?" |
| `#/settings` | Configuration | "How is Perun wired up?" |

The sidebar is the map; the topbar is context (`title`, cross-project search,
refresh, and a global **Ask** button that always lands on the console).

## 5. Visual language

Mirrors the company site (`peruninthemachine.com`) so the tools feel like one
family:

* **Palette:** `#050505` background, layered `#0c0c0e`/`#101014` surfaces,
  `#c9a227` gold accent, restrained semantic colors (green/red/amber/blue) used
  *only* for status.
* **Type:** `Cinzel` for headings/brand (heraldic, "Perun" = Slavic thunder god),
  the system UI sans for everything else, monospace for ids/paths/output.
* **Surfaces:** one card component everywhere — border `rgba(gold,.15)`,
  14px radius, subtle lift on hover. Status is always a small pill badge.
* **Motion — the storm layer.** Perun is a thunder god, so the interface has
  weather. Three escalating states — **idle** (calm), **awake** (a project was
  touched in the last 15 min), **storm** (an agent run is executing) — drive a
  full-viewport `<canvas>` lightning engine, a brand sigil whose **eyes glow**, a
  topbar storm meter, and a travelling energy rim + eye pips on working cards.
  It is *ambient status*: it never hides state or delays reading a label, and it
  switches entirely off under `prefers-reduced-motion`.

## 6. Key interaction flows

### Start an agent (the core loop)
Console → pick project → (optional) pick session / model / agent → type prompt
→ **Run**. Output streams line-by-line; tool calls and errors are color-coded;
the run ends with a `[status]` line and the button re-enables. The project card
and session list refresh automatically.

### Coroutines (work that runs itself)
`#/coroutines` lists durable jobs and their next fire, health and history. A job
has exactly one trigger — interval, five-field cron, one-shot, manual, or a
domain event (webhook, finished run, approved/executed proposal, operator-inbox
item, git commit/push, PerunChat room message) — and either one run or a loop.
**Pause** lets the active run finish and suppresses future cycles; **Stop**
cancels it. Project jobs name their execution project; global jobs (reminders,
digests, machine health) run as the orchestrator in the control plane. Event
content is untrusted: event triggers are project-scoped, never run in the control
plane, and default to the read-only `plan` agent. Quick create covers a daily or
one-shot reminder; the five-step wizard exposes the full schedule, loop caps, and
room-result options. Every fire is a normal Perun run, so streaming,
cancellation, history and restart recovery behave exactly like every other run.

### Remember / recall
Two entry points, one store. From a project: Memory tab → *Add fact* (global or
this project). From anywhere: the topbar search, or the Memory view with an
"all projects" toggle. The CLI and MCP expose the same operations.

### Verify a change (bounded execution)
The room agent has no shell, so "did the delivered script pass?" needs a
declared path. The operator declares a check once (`perun checks add <project>
<name> "<command>"`, optionally `--pin`ned to the script's sha256); from then on
the room can run it **by name** and report the exit code and output. For an
ad-hoc delivered script, `perun_verify_proposal` derives the command from an
approved target, shows the operator the exact command and sha256, and runs only
after they confirm. A check that is already running, a changed pinned file, or
a changed confirmed file is refused rather than guessed at.

### Global install
Settings → *Install globally* explains the exact command; the CLI does the merge
and reports the changed paths and the backup file. This is deliberately a
deliberate, visible action, never a silent side effect of first run.

### Telegram
One bot for the whole machine — a **singleton** (the `getUpdates` API allows one
consumer per token), enforced by a machine-global named mutex. `/start` `/help`,
`/projects` lists, `/project <name|#>` switches (per chat, persisted in the DB),
`/orchestrator` (`/perun`, `/overseer`) jumps to the control plane, `/new`
`/reset`, `/status`, `/stop` `/cancel`, `/remember`, `/recall`, `/index`, and
`/pair <code>` to authorise a chat. Anything else is a prompt, answered by a
Perun-orchestrated run. An empty allow-list grants **nobody** access until a chat
pairs.

### Manage models and MCP servers
`#/providers` lists the provider catalog and the credentials OpenCode already
has; adding or connecting one writes `opencode.json[c]` (Perun never stores the
key) and asks OpenCode to reload. `#/mcp` snapshots every MCP server OpenCode
would load, derives a config status, and can live-probe one server with a real
`initialize` + `tools/list` handshake — from disk, never from request input.

### PerunChat
`#/perunchat` lets one agent talk to others. The dashboard asks the local Perun
service for a 30-second single-use WebSocket ticket
(`POST /api/perunchat/ws-ticket`, loopback only) and connects to the gateway with
`?ticket=`; the long-lived API key never appears in a URL. `?token=` survives
only as an opt-in legacy fallback (`PERUNCHAT_ALLOW_QUERY_TOKEN`). On the server
side, the autonomous bridge joins
configured rooms and answers through the orchestrator, with a strict chat
persona, rate limits, a write-ahead outbox for no-loss delivery, and HITL
approval for state-changing requests.

**Shared Projects.** A project can be bound to a dedicated `grp:` PerunChat room
with trusted participants (`rw`/`ro`). Chat-originated action proposals go
through local HMAC-SHA256 HITL approval. One click approves and starts the
Project Agent headless in the background (no TUI, no interactive prompt); the
run uses the running OpenCode service (`standalone=False`), the policy-enforcing
`perun-guard` filesystem MCP server, and post-run audits. When **Agent approval**
is enabled, a peer `rw` participant or the bridge's bounded autonomous reviewer
can approve the proposal with `execute=false`, leaving it `approved` for a
remote claim or an explicit local run; the HITL signing secret / approver
identity are configurable. A remote rw claimant can download a target-scoped
ZIP of the approved paths and upload a package, which Perun validates, applies
with rollback on failure, and audits. A proposal is a changeset of `{path, op}`
operations; one proposal can create, edit and delete atomically. `edit`/`delete`
targets must exist and `create` targets must be absent, Perun derives the
pre/post manifest itself, and the upload is staged for a **second package-level
approval** of the exact archive digest. A package containing a `delete` is
operator-only, and the local executor's `guard_delete_file` only works during an
approved delete run. Approval is pinned to the content
it saw: Perun fingerprints every declared target, and a claim or apply is refused
if a target changed, appeared or disappeared in the meantime, so a stale package
can never overwrite newer work. An approver can also **withdraw** an unused
approval, which lands the proposal in a distinct `cancelled` state instead of
leaving it claimable — or mislabelling it as a refusal. In the dashboard chat,
every `prop_…` named by a participant is a status-coloured chip: clicking it
shows the project, status, intent and targets, and **Open in project** takes the
operator straight to that row in the Shared tab. The operator never has to hunt
for which project a chat mention belongs to.

The operator can **pause and resume the project's autonomous work** from the
Shared tab. The durable flag stops room replies, room-event coroutine triggers,
the autonomous reviewer and policy auto-approve; in-flight runs finish, chat
history is retained, operator-directed mentions stay durable, and explicit human
approvals remain available. Scheduled/time-based coroutines keep their own Pause
control, so the project flag scopes autonomy to the room.

### Context and secrets
An agent that needs reference material calls `perun_context("<task>")` instead
of carrying it inline; orchestrated runs get the relevant docs pre-fetched.
Resolution is deterministic first (exact key, anchor label, key fragment,
subject match) and uses local embeddings only as a fallback when Ollama is up.
Anchors resolve by label, stemmed tokens and path, and a file/doc anchor returns
the best indexed section or a bounded stub. CODEMAP is chunked at `###` modules
so retrieval names the module it describes, and a compact `<slug>.codemap`
outline is indexed as a context doc.

A credential is fetched by name from the encrypted store (`perun_secret`) at the
moment of use, so it never sits in a prompt file. `perun.md` anchors can bind to
a context key (`memory:`) or a secret (`kind: secret`), and
`perun context verify` checks those bindings.

### Talk to the orchestrator
`#/orchestrator` (or Telegram `/orchestrator`) chats with the control-plane agent
- the one whose job is *managing projects*. It works on a machine with **zero**
projects registered, and its model, agent, and custom instructions live in
Settings (they belong to the singleton, not to any user project). The control
project is registered hidden and listed only in the owner's private workspace
(the `.perun/owner-workspace` marker from the internal overlay;
`PERUN_SHOW_HIDDEN_PROJECTS` overrides). An installed copy lists only the user's
projects, and this page still works with none of them.

### Start Perun (one click)
On Windows, double-clicking **`Perun.cmd`** brings the whole thing up: it finds
Python, resolves the port, starts the dashboard **idempotently** (a second click
just opens the browser), and starts the single Telegram bridge in-process while
the dashboard runs. It never starts a second poller.

## 7. Why these technical choices

* **SQLite WAL, connection-per-op** — the API, orchestrator threads, bridge, and
  MCP server all write; WAL + short connections avoid lock contention without a
  connection pool.
* **Single DB for registry + memory + vectors + routing** — one file to back up,
  one transaction domain, no cross-store consistency bugs.
* **Thread-per-run + semaphore** — agent runs are long and blocking; threads are
  the simplest correct model, and the semaphore bounds cost. SSE pumps them
  through `iterate_in_threadpool` so the event loop stays free.
* **`opencode run --format json` with `cwd`** — JSON frames give us
  text/tool/session events without screen scraping, and setting `cwd` avoids
  OpenCode's directory-argument ambiguity.
* **One shared Telegram bridge, not one per project** — the bot API permits a
  single `getUpdates` consumer per token; routing chat → project → session lives
  in `bridge_links`, and a named mutex keeps the poller singular across the
  in-process bridge, the optional logon task, and `Perun.cmd`.
* **Explicit cross-project paths** — `perun.project_roots.resolve(name, *parts)`
  validates against a registered root, so an agent never hand-crafts a relative
  path that silently targets the wrong file.
* **Global MCP over per-project scripts** — the *point* is zero per-project
  setup; a stdio server registered once in the user config is inherited
  everywhere.
* **Ollama + `nomic-embed-text`** — reuses the exact model/prefixes of the
  existing per-project memory tooling, so imported vectors stay comparable and
  there's no new dependency to run.

## 8. Deliberately out of scope

* Multi-user / auth — it binds to `127.0.0.1` only (a loopback token still gates
  state-changing API calls); remote participants reach it only through the
  PerunChat gateway's own authentication.
* **Running** deploy commands — the manifest-driven dependency graph and
  `deploy-plan` checklist are modelled, but Perun never executes a deploy
  command itself.
* Cloud sync of the registry or memory.

Still out of scope as of v0.12.4; the data model already leaves room
(`projects.enabled`, `runs` history, `activity` meta, `tasks`). Scheduling and
event-triggered agent jobs moved **into** scope with v0.10.51
(`coroutines.py`, `#/coroutines`), as did operator-declared checks
(`checks.py`).
