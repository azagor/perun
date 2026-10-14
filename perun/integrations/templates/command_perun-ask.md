---
description: Ask another Perun project's agent a question and wait for the reply
---

Ask another OpenCode project (managed by Perun) a question.

Arguments: `$ARGUMENTS` — either `<project-slug>: <question>` or just a question
(which runs in the current project).

Steps:
1. Parse the project slug (everything before the first `:`) and the question.
2. Call the `perun` MCP tool `perun_ask` with `project` = the slug and
   `prompt` = the question. Omit `project` to use the current directory.
3. Report the agent's reply verbatim, prefixed with the project slug.

If the `perun` MCP server is unavailable, use a shell fallback:
`python -m perun recall "<question>" --project <slug>` for memory questions, or
select the project in the Perun dashboard (<http://127.0.0.1:8760>) for agents.
