# Perun on this machine

## The dashboard

<http://127.0.0.1:8760>

Start it with:

```bash
python -m perun serve          # from the Perun checkout
.\Perun.cmd                    # Windows one-click launcher (idempotent)
```

`Perun.cmd` never starts a second Telegram bridge, and it is safe to run
repeatedly — it reuses a running instance and just opens the browser.

Views: `#/dashboard` (overview), `#/orchestrator` (chat with Perun itself),
`#/projects`, `#/project/{slug}`, `#/console`, `#/memory`, `#/activity`,
`#/mcp`, `#/telegram`, `#/perunchat`, `#/settings`.

The **storm layer** shows which projects are awake or working: a project
touched in the last 15 minutes is *awake*; a project with a running agent is
*in the storm*.

## The control plane (orchestrator)

Perun itself is a first-class agent: the **orchestrator**. It is reachable
even with **zero** projects registered, through the dashboard chat
(`#/orchestrator`), from Telegram (`/orchestrator`), or through the MCP
tools. Its job is orchestration — listing, describing, creating, launching
and deleting projects — and it is never confused with one of your projects
(it is identified by its path being the Perun repository itself).

## The Telegram bridge

**One** poller, machine-wide. The Telegram Bot API allows a single consumer
per bot token, so `telegram.PidLock` (a machine-global named mutex) refuses a
second start rather than fighting over HTTP 409s. `serve --bridge`,
`perun bridge`, the optional `PerunBridge` logon task and `Perun.cmd` all
defer to that lock.

* `/projects` lists projects, `/project <n>` switches the active one, and
  everything else is a prompt sent to the active project.
* `/recall` searches the same memory the web UI shows.
* A pairing code (`/pair <code>`) authorises a chat. The allow-list is
  strict — an empty allow-list grants nobody.

The bridge is **not** registered as a logon task by default. Add `--task` to
`bootstrap.ps1` or to `python -m perun install --machine` to opt into
auto-start-at-logon.

## Fresh-machine setup

```bash
.\bootstrap.ps1                       # Windows: prerequisites + deps + global install
python -m perun install --machine    # the same, from a Python that can import perun
```

This checks the prerequisites (Python 3.10+, pip, the OpenCode CLI, Ollama),
installs the dependencies, wires the global OpenCode integration (the MCP
server, the AGENTS.md kernel, the slash commands) and registers Perun plus
its discovered siblings. Add `--task` to also create the `PerunBridge` logon
task. `--uninstall` reverses it.

## Repairing an existing install

```bash
python -m perun install --machine    # idempotent; re-run to repair
python -m perun discover --index     # rescan roots and re-index every project's docs
python -m perun validate-docs --all  # report manifest docs/anchors pointing at missing files
python -m perun context sync         # re-index the handbook and refresh the AGENTS.md kernel
python -m perun status               # what Perun sees, and where the bridge stands
```

## Provider and model configuration

Perun never stores provider credentials itself. It reads OpenCode's own
config (`opencode auth list`) and edits that config when you connect a
provider, so the CLI and the service never disagree about who is signed in.
`GET /api/providers` shows the status; `POST /api/providers/{id}/connect`
and `/logout` change it. A backup is written before any rewrite, and a config
with comments or trailing commas is reported as such rather than silently
reformatted.
