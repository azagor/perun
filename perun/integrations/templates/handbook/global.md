# Perun — global orchestration, memory & search

You are running inside an OpenCode project that includes **Perun**, a local
overseer that spans *every* OpenCode project on this machine. Perun gives you,
regardless of which folder this repository lives in:

* a **registry of all your projects** and their metadata,
* a **unified, project-namespaced memory** (facts + semantic document search),
* the ability to **start and inspect agent sessions in other projects**,
* an **always-available orchestrator** (the "control plane") you can reach even
  when no project is registered — and that can itself create/register projects,
* a **shared Telegram bridge** for reaching you from a phone,
* each project's **custom instructions** and self-describing **`perun.md` manifest**.

## MCP tools (server name `perun`)

| Tool | Purpose |
|------|---------|
| `perun_list_projects` | List every registered project (slug, name, path, kind). |
| `perun_project_info` | Details for one project (counts, recent activity, model/agent). |
| `perun_create_project` | Create / register / clone a project (shared with the dashboard wizard). Requires `confirm=true` only for `method="clone"`/`clone_url` (it fetches remote code). |
| `perun_update_project` | Rename, re-describe, or re-tag a project. |
| `perun_delete_project` | Unregister a project (requires `confirm=true`; refuses the orchestrator). |
| `perun_discover` | Scan the discovery roots and register new projects. Requires `confirm=true` when an explicit `roots` list is given (scanning outside the configured roots). |
| `perun_memory_search` | Semantic + keyword search across facts and indexed docs. |
| `perun_memory_store` | Store a durable fact (global or project scope). |
| `perun_memory_forget` | Delete one fact by id. |
| `perun_memory_supersede` | Replace a fact's content while preserving its id. |
| `perunchat_room_memory` | Search PerunChat room memory (transcripts + durable facts). |
| `perun_context` | Retrieve context docs by key, anchor label, or task description. |
| `perun_secret` | Read/write a named secret in the encrypted store. |
| `perun_docs_index` | (Re)index a project's `docs/*` into the vector store. |
| `perun_get_instructions` | Read a project's custom instructions. |
| `perun_set_instructions` | Write a project's custom instructions (requires `confirm=true` — they are injected into every new session). |
| `perun_list_instruction_files` | List a project's `.perun/instructions/*.md` files. |
| `perun_get_manifest` | Read a project's `perun.md` manifest (front matter + body). |
| `perun_set_manifest` | Write a project's `perun.md` manifest (requires `confirm=true` — a manifest can seed durable instructions/anchors). |
| `perun_grep` | Fast regex content search across the current project (ripgrep + fallback). |
| `perun_ast_grep` | Structural code search across the current project (ast-grep + fallback). |
| `perun_global_grep` | Fast regex content search across *all* registered projects (or a subset). |
| `perun_global_ast_grep` | Structural code search across *all* registered projects (or a subset). |
| `perun_unified_search` | Unified cross-project search: vector memory + global grep + ast-grep. |
| `perun_file_exists` | Check whether one or more paths exist in the allowed roots. |
| `perun_list_directory` | List entries in a directory in the allowed roots. |
| `perun_read_file` | Read a text file with line numbers. |
| `perun_start_session` | Start an agent run in a project; returns a run id. |
| `perun_ask` | Run a prompt in a project and wait for the reply. |
| `perun_run_status` | Status / output of a previously started run. |
| `perun_telegram_send` | Send a message to the configured Telegram chat (a `chat_id` outside the allow-list is refused). |

When a tool takes a `project`, omit it to use the project of the **current
working directory**; pass a slug (e.g. `my-project`) or an absolute path to
target another project.

## When to use Perun

* **Cross-project questions** ("what did the other project decide about X?",
  "which of my projects touch Kubernetes?") → `perun_memory_search` with
  `all_projects` enabled, or `perun_list_projects`.
* **Remembering something durable** that should outlive this session →
  `perun_memory_store`.
* **Creating / managing projects** (register an existing folder, scaffold a new
  one, or scan the machine) → `perun_create_project` / `perun_discover`.
* **Delegating work** to another project's agent → `perun_start_session` /
  `perun_ask`.
* **Reaching the user** out-of-band → `perun_telegram_send`.

Prefer the local project's own memory framework when a question is purely about
this repository; use Perun when the answer lives *across* projects or in
Perun's own registry.

## Working inside a Perun-managed project

* **Read `perun.md` first.** Each project's `perun.md` manifest is the primary
  source of truth for its identity, docs map, commands, and anchors into
  important files and dependencies. New projects bootstrapped by Perun include
  `perun.md`, `AGENTS.md`, `README.md`, `docs/CHANGELOG.md`,
  `docs/TECHNICAL_DOCUMENTATION.md`, and `docs/CODEMAP.md`.
* **Use `/update-docs`** to refresh the project docs when the codebase changes.
* **Add anchors** to `perun.md` for files, docs, symbols, commands, and external
  URLs that future agents (and the dashboard) should know about.

## Global slash commands

These commands are available in every OpenCode session because Perun registers
them globally:

| Command | Purpose |
|---------|---------|
| `/perun-projects` | List every registered project. |
| `/perun-ask <project> <question>` | Ask another Perun-managed project a question. |
| `/logwork <one-line summary>` | Save a short work summary to Perun memory. Called inside a project it stores a project-scoped fact; called from the orchestrator/global context it stores a global fact. |
| `/update-docs [project-slug]` | Refresh project docs (`docs/CODEMAP.md`, `docs/CHANGELOG.md`, `docs/TECHNICAL_DOCUMENTATION.md`) plus any docs declared in the project's `perun.md` manifest. |

## Dashboard

If the per-project question is really about orchestration, the user can open
Perun's dashboard at <http://127.0.0.1:8760> (`python -m perun serve`, or
double-click `Perun.cmd` on Windows). Its **storm layer** shows which projects are
awake or working, and `#/orchestrator` is a chat with Perun itself.

## New machine

On a fresh Windows machine, run `bootstrap.ps1` in the Perun checkout (or
`python -m perun install --machine`): it checks prerequisites, installs
dependencies, and wires this global OpenCode integration. The Telegram bridge
starts with the dashboard (`Perun.cmd` or `python -m perun serve --bridge`) and
is not registered as a logon task by default; add `--task` to `bootstrap.ps1` or
to `python -m perun install --machine` to opt into auto-start-at-logon. Re-run
it any time to repair; add `--uninstall` to reverse it.
