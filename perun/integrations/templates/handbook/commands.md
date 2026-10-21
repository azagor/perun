# Global slash commands

Perun registers these commands **globally**, so they are available in every
OpenCode session on this machine — in any project, with no per-project setup.

| Command | Purpose |
|---------|---------|
| `/perun-projects` | List every registered project (slug, name, path, kind). |
| `/perun-ask <project> <question>` | Ask another Perun-managed project a question and wait for the reply. |
| `/logwork <one-line summary>` | Save a short work summary to Perun memory. |
| `/update-docs [project-slug]` | Refresh the project's docs (see below). |

## `/perun-projects`

Lists every project Perun knows about. Use it when you need a project's
**slug** — that slug is the identifier the other tools take.

## `/perun-ask <project> <question>`

Delegates a question to another project's own agent and waits for the answer.

```
/perun-ask trading-bot "how does the build pipeline stage 3 work?"
```

The answer comes from the agent that actually lives in that project, with
that project's instructions, manifest and code in front of it. This is the
right way to ask a question about project B while you are working in project
A — it avoids re-explaining context and it keeps the answer grounded in the
right repository.

Use `perun_start_session` instead when you want the run to continue
asynchronously and be observed later (`perun_run_status`).

## `/logwork <one-line summary>`

Stores one sentence in Perun memory.

* Called **inside a project** → stored as a project-scoped fact.
* Called from the **orchestrator** (no project selected) → stored as a global
  fact, visible everywhere.

Good summaries are atomic and decision-shaped: *"the Telegram bridge is
single-instance, guarded by telegram.PidLock"*. Bad summaries are diffs:
*"fixed three bugs"* — those belong in the changelog, not memory.

## `/update-docs [project-slug]`

Regenerates the project's documentation from the current codebase:

* `docs/CODEMAP.md` — the navigation map (file + key identifier per module)
* `docs/CHANGELOG.md` — the release history
* `docs/TECHNICAL_DOCUMENTATION.md` — the deep technical reference

It also refreshes any docs declared in the project's `perun.md` manifest. With
no argument it targets the project of the current working directory; pass a
slug to target another one.

Run it after a change that alters the shape of the codebase — a new module, a
new endpoint, a changed data model. The CODEMAP is what future agents read
first, so a stale CODEMAP is a real cost.
