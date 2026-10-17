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
* **Start any agent from anywhere.** Kick off a task in `project-a` while
  you're "in" `project-b`.
* **Phone access.** A single Telegram bot with per-chat project switching. Swap
  the bot for a different one from the dashboard (name, API key, chat ids) — the
  key is verified before it is saved, stays masked, and the bridge restarts in
  one click.
* **An always-available orchestrator.** Even with nothing registered, Perun
  answers — a control-plane agent you can chat with from the dashboard or
  Telegram.
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

## Install on a new machine

### 1. Windows GUI installer (easiest for end users)

Download `PerunSetup.exe` from the latest release and run it. The wizard
installs any missing prerequisites (Python, git, OpenCode CLI, Ollama),
installs Perun, wires the global OpenCode integration, and creates a desktop
shortcut. The Telegram bridge starts with the dashboard; the optional
`PerunBridge` logon task is not registered unless the installer opts in.

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

### 3. Linux / macOS command-line installer

Open a terminal and run:

```bash
curl -fsSL https://raw.githubusercontent.com/azagor/perun/master/setup.sh | bash
```

Then start the dashboard:

```bash
python3 -m perun serve
```

### 4. Manual install (any platform)

```bash
pip install -r requirements.txt
python -m perun install --machine
python -m perun serve
```

`install --machine` is **idempotent** — re-run it any time to repair. Narrow the
scope with `--skip-deps`, `--skip-global`, `--skip-task`, or `--no-index`; pass
`--task` to register the optional `PerunBridge` logon task.
`python -m perun install --machine --uninstall` reverses it (and removes the
`PerunBridge` task if present).

A new machine needs **Python 3.10+**, **git**, and the **opencode** CLI on `PATH`
(Ollama is optional). The GUI installer handles all of these automatically.
The optional `PerunBridge` task is registered with `IgnoreNew`, an unlimited
execution limit, and restart-on-failure, so the Telegram poller stays singular;
it is only *started* once a Telegram token is configured in `~/.perun/config.json`.

### Code signing for releases

The installer can be signed with a trusted certificate. The GitHub Actions
workflow in `.github/workflows/release.yml` supports signing via the
`PERUN_SIGN_PFX_BASE64` and `PERUN_SIGN_PASSWORD` repository secrets. For
details see `docs/TECHNICAL_DOCUMENTATION.md`.

## What you get

| Surface | Where | Notes |
|---|---|---|
| **Dashboard** | `http://127.0.0.1:8760` | Gold/dark SPA styled after peruninthemachine.com |
| **REST API** | `/api/*` | JSON; see `docs/TECHNICAL_DOCUMENTATION.md` |
| **Live streams** | `/api/runs/{id}/stream`, `/api/stream` | Server-Sent Events |
| **MCP server** | stdio, tool namespace `perun` | 17 tools, installed globally |
| **Telegram bot** | long-polling | `/projects`, `/project`, `/orchestrator`, `/recall`, … |
| **CLI** | `python -m perun …` | `serve`, `bridge`, `discover`, `recall`, … |
| **One-click start** | `Perun.cmd` (Windows) | Idempotent dashboard launcher; never starts a second bridge |
| **Installer** | `installer\Output\PerunSetup.exe` (Windows wizard) or `setup.ps1` / `setup.sh` (web) or `bootstrap.ps1` / `perun install --machine` (local) | Prereqs + deps + global install + optional `PerunBridge` task |

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
python -m perun install [--uninstall] [--machine]
python -m perun mcp
```

## Layout

```
perun/                     the Python package
  config.py                paths + persisted Settings (+ is_control_path)
  db.py                    SQLite schema (registry + memory + vectors)
  registry.py              projects / sessions / runs / activity / bridge links
  control.py               the always-present orchestrator (control plane)
  projects.py              shared project creation (wizard + MCP)
  memory.py                unified facts + doc-chunk store + search
  embeddings.py            Ollama transport + vector math
  discovery.py             find projects under configured roots
  orchestrator.py          start/stream/cancel `opencode run`
  telegram.py              shared bridge with per-project routing
  server.py                FastAPI + SSE + static dashboard
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
tools/make_icon.py         regenerates the app icon
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
