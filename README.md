# Perun

**Perun is a local overseer and orchestrator for all your OpenCode projects.**

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
* **Cross-project memory.** Facts you store and docs Perun indexes are
  searchable from any project — "which project handled the OAuth callback?".
* **Start any agent from anywhere.** Kick off a task in `trading_bot` while
  you're "in" `optimus`.
* **Phone access.** A single Telegram bot with per-chat project switching.
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

Optionally run the Telegram bridge too:

```bash
python -m perun serve --bridge   # dashboard + bridge in one process
# or, separately:
python -m perun bridge
```

## What you get

| Surface | Where | Notes |
|---|---|---|
| **Dashboard** | `http://127.0.0.1:8760` | Gold/dark SPA styled after peruninthemachine.com |
| **REST API** | `/api/*` | JSON; see `docs/TECHNICAL_DOCUMENTATION.md` |
| **Live streams** | `/api/runs/{id}/stream`, `/api/stream` | Server-Sent Events |
| **MCP server** | stdio, tool namespace `perun` | 9 tools, installed globally |
| **Telegram bot** | long-polling | `/projects`, `/project`, `/recall`, … |
| **CLI** | `python -m perun …` | `serve`, `bridge`, `discover`, `recall`, … |

## CLI

```
python -m perun serve [--host H] [--port P] [--reload] [--bridge]
python -m perun bridge
python -m perun discover [--roots a;b] [--index]
python -m perun projects
python -m perun add <name> <path> [--model M] [--agent A]
python -m perun status
python -m perun remember <text> [--project SLUG] [--global] [--topic T]
python -m perun recall <query> [--project SLUG] [--k N] [--all]
python -m perun index <slug> | --all
python -m perun import-legacy-memory [--path P]
python -m perun install [--uninstall]
python -m perun mcp
```

## Layout

```
perun/                     the Python package
  config.py                paths + persisted Settings
  db.py                    SQLite schema (registry + memory + vectors)
  registry.py              projects / sessions / runs / activity / bridge links
  memory.py                unified facts + doc-chunk store + search
  embeddings.py            Ollama transport + vector math
  discovery.py             find projects under configured roots
  orchestrator.py          start/stream/cancel `opencode run`
  telegram.py              shared bridge with per-project routing
  server.py                FastAPI + SSE + static dashboard
  __main__.py              CLI
  integrations/            global install into ~/.config/opencode
mcp/perun_mcp.py           the `perun` MCP server (stdio)
web/                       the dashboard (index.html + css + js)
docs/                      CODEMAP · TECHNICAL · CHANGELOG · DESIGN
tests/                     pytest suite
```

Runtime state lives **outside** the repo, in `~/.perun/` (override with
`PERUN_HOME`): `perun.db`, `config.json`, `logs/`, `runs/`.

## Design principles

* **Local-first.** Everything except the OAuth dance and an optional Telegram
  bot runs on your machine. No cloud, no accounts.
* **Additive & reversible.** The global install merges into your OpenCode
  config and backs it up first; `--uninstall` removes exactly what it added.
* **Graceful degradation.** If Ollama is down, memory still stores and searches
  (keyword + recency); semantic ranking just returns when it's back.
* **Bounded resources.** A semaphore caps concurrent agent runs; each run
  streams to disk (`~/.perun/runs/*.log`) and a capped in-memory buffer.

See `docs/DESIGN.md` for the full product design and `docs/TECHNICAL_DOCUMENTATION.md`
for the internals.
