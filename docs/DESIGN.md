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

## 3. Mental model (what the user sees)

```
        ┌──────────────┐        Perun is a "control room".
        │   PROJECTS   │        ─ each project is a card
        │  ▤ ▤ ▤ ▤ ▤   │        ─ each card can Run / Stop / Open /
        └──────┬───────┘          Index docs / Delete
               │ select
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
| `#/memory` | Memory explorer | "What do we know / have we decided?" |
| `#/activity` | Live feed | "What is happening right now?" |
| `#/mcp` | MCP server inventory | "Which MCP servers does OpenCode load, and are they healthy?" |
| `#/telegram` | Bridge control | "Can I reach this from my phone?" |
| `#/perunchat` | Native agent chat | "Talk to other agents on the PerunChat network." |
| `#/providers` | LLM providers | "Which models can my agents use?" |
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

### Remember / recall
Two entry points, one store. From a project: Memory tab → *Add fact* (global or
this project). From anywhere: the topbar search, or the Memory view with an
"all projects" toggle. The CLI and MCP expose the same operations.

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
`#/perunchat` connects the dashboard straight to the gateway over WebSocket and
lets one agent talk to others. On the server side, the autonomous bridge joins
configured rooms and answers through the orchestrator, with a strict chat
persona, rate limits, a write-ahead outbox for no-loss delivery, and HITL
approval for state-changing requests.

### Context and secrets
An agent that needs reference material calls `perun_context("<task>")` instead
of carrying it inline; orchestrated runs get the relevant docs pre-fetched. A
credential is fetched by name from the encrypted store (`perun_secret`) at the
moment of use, so it never sits in a prompt file. `perun.md` anchors can bind to
a context key (`memory:`) or a secret (`kind: secret`), and
`perun context verify` checks those bindings.

### Talk to the orchestrator
`#/orchestrator` (or Telegram `/orchestrator`) chats with the control-plane agent
— the one whose job is *managing projects*. It works on a machine with **zero**
projects registered, and its model, agent, and custom instructions live in
Settings (they belong to the singleton, not to any user project).

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
  state-changing API calls).
* Scheduling / cron-style auto-runs.
* A graph of inter-project dependencies (local references are recorded as
  memory facts, but nothing models the graph).
* Cloud sync of the registry or memory.

Still out of scope as of v0.7.21; the data model already leaves room
(`projects.enabled`, `runs` history, `activity` meta, `tasks`).
