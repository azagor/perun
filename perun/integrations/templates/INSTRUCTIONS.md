# Perun — global orchestration, memory & search

You are running inside an OpenCode project that includes **Perun**, a local
overseer that spans *every* OpenCode project on this machine. Perun gives you,
regardless of which folder this repository lives in:

* a **registry of all your projects** and their metadata,
* a **unified, project-namespaced memory** (facts + semantic document search),
* the ability to **start and inspect agent sessions in other projects**,
* a **shared Telegram bridge** for reaching you from a phone.

## MCP tools (server name `perun`)

| Tool | Purpose |
|------|---------|
| `perun_list_projects` | List every registered project (slug, name, path, kind). |
| `perun_project_info` | Details for one project (counts, recent activity, model/agent). |
| `perun_memory_search` | Semantic + keyword search across facts and indexed docs. |
| `perun_memory_store` | Store a durable fact (global or project scope). |
| `perun_docs_index` | (Re)index a project's `docs/*` into the vector store. |
| `perun_start_session` | Start an agent run in a project; returns a run id. |
| `perun_ask` | Run a prompt in a project and wait for the reply. |
| `perun_run_status` | Status / output of a previously started run. |
| `perun_telegram_send` | Send a message to the configured Telegram chat. |

When a tool takes a `project`, omit it to use the project of the **current
working directory**; pass a slug (e.g. `trading_bot`) or an absolute path to
target another project.

## When to use Perun

* **Cross-project questions** ("what did the optimus project decide about X?",
  "which of my projects touch Kubernetes?") → `perun_memory_search` with
  `all_projects` enabled, or `perun_list_projects`.
* **Remembering something durable** that should outlive this session →
  `perun_memory_store`.
* **Delegating work** to another project's agent → `perun_start_session` /
  `perun_ask`.
* **Reaching the user** out-of-band → `perun_telegram_send`.

Prefer the local project's own memory framework when a question is purely about
this repository; use Perun when the answer lives *across* projects or in
Perun's own registry.

## Dashboard

If the per-project question is really about orchestration, the user can open
Perun's dashboard at <http://127.0.0.1:8760> (`python -m perun serve`).
