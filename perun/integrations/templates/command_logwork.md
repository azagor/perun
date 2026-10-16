---
description: Save a short work summary to Perun memory.
---

Save a very short, one-sentence summary of the work just done to Perun's durable
memory store.

Use the `perun_memory_store` MCP tool:

- **fact** — a concise sentence describing what was accomplished. If no project
  is active, include the project name in the fact or keep it general.
- **topic** — `work`.
- **project** — **omit this argument** so Perun scopes the fact automatically:
  when called inside a project directory it becomes a project-scoped fact; when
  called from the orchestrator / global context it becomes a global fact.

User summary: $ARGUMENTS

If the summary above is empty, ask the user for a one-line summary before calling
`perun_memory_store`. Do not store an empty fact.
