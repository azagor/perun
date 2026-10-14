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

1. **Local-first, single-binary-ish.** One process, one SQLite file, no accounts.
   The only network dependencies are optional (Telegram, Ollama).
2. **Inherited, not installed per project.** A global MCP server + global
   instructions mean *every* project — including ones you create later —
   automatically gets Perun's tools. No per-project setup.
3. **Additive and reversible.** Touching a user's global OpenCode config is
   invasive, so we back up first, merge only our keys, and can remove exactly
   what we added.
4. **Graceful degradation everywhere.** No Ollama → keyword search still works.
   No token → bridge stays dormant. No `opencode` → a clear error, not a crash.
5. **Bounded, observable.** Cap concurrent runs; stream everything; log raw
   output; expose a live activity feed. The overseer must itself be watchable.
6. **Clarity over cleverness.** The UI shows plain labels, real statuses, and
   one obvious action per card. No hidden state.

## 3. Mental model (what the user sees)

```
        ┌──────────────┐        Perun is a "control room".
        │   PROJECTS   │        ─ each project is a card
        │  ▤ ▤ ▤ ▤ ▤   │        ─ each card can Ask / Open / Index
        └──────┬───────┘
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
        TELEGRAM ── the same control room, from your phone
```

Three nouns carry the whole product: **Project**, **Run**, **Memory**. Every
screen maps to one of them.

## 4. Information architecture

| Route | Purpose | Primary question it answers |
|---|---|---|
| `#/dashboard` | Command overview | "What's the state of everything?" |
| `#/projects` | Registry | "What projects do I have?" |
| `#/project/{slug}` | Project detail | "Everything about this one." |
| `#/console[/{slug}]` | Agent launcher | "Run an agent and watch it work." |
| `#/memory` | Memory explorer | "What do we know / have we decided?" |
| `#/activity` | Live feed | "What is happening right now?" |
| `#/telegram` | Bridge control | "Can I reach this from my phone?" |
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
* **Motion:** minimal — hover lift, toast slide-in, spinner. Nothing that hides
  state or delays reading.

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
One bot. `/projects` lists, `/project <n>` switches (per chat, persisted in the
DB), everything else is a prompt. `/recall` shares the same memory as the web UI.

## 7. Why these technical choices

* **SQLite WAL, connection-per-op** — the API, orchestrator threads, bridge, and
  MCP server all write; WAL + short connections avoid lock contention without a
  connection pool.
* **Single DB for registry + memory + vectors + routing** — one file to back up,
  one transaction domain, no cross-store consistency bugs.
* **Thread-per-run + semaphore** — agent runs are long and blocking; threads are
  the simplest correct model, and the semaphore bounds cost. SSE pumps them
  through `iterate_in_threadpool` so the event loop stays free.
* **`opencode run --format json` with `cwd`** — proven in the per-project
  Telegram bridge; JSON frames give us text/tool/session events without screen
  scraping, and setting `cwd` avoids OpenCode's directory-argument ambiguity.
* **Global MCP over per-project scripts** — the *point* is zero per-project
  setup; a stdio server registered once in the user config is inherited
  everywhere.
* **Ollama + `nomic-embed-text`** — reuses the exact model/prefixes of the
  existing per-project memory tooling, so imported vectors stay comparable and
  there's no new dependency to run.

## 8. Deliberately out of scope (v0.1)

* Multi-user / auth — it binds to `127.0.0.1` only.
* Scheduling / cron-style auto-runs.
* A graph of inter-project dependencies.
* Editing a project's files from the dashboard.

These are natural v0.2 candidates and the data model already leaves room
(`projects.enabled`, `runs` history, `activity` meta).
