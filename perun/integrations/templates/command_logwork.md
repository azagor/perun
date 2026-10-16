---
description: Save a short work summary to Perun memory.
---

Save a very short, one-sentence summary of the work just done to Perun's durable
memory store.

Use the `perun_memory_store` MCP tool:

- **content** — a concise sentence describing what was accomplished. If no project
  is active, include the project name in the content or keep it general.
- **topic** — `work`.
- **project** — **omit this argument** so Perun scopes the fact automatically:
  when called inside a project directory it becomes a project-scoped fact; when
  called from the orchestrator / global context it becomes a global fact.

User summary: $ARGUMENTS

If the summary above is empty, compose a very short one-sentence summary of the
work done in the current session based on the conversation so far, then call
`perun_memory_store` with that as the `content`. Only ask the user if the
conversation is too ambiguous to summarize. Avoid including secrets or raw
credentials in the stored content.
