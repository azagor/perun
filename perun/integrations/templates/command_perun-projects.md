---
description: List every OpenCode project Perun knows about
---

Use the `perun` MCP server's `perun_list_projects` tool to list all registered
projects. Then present them as a concise table with columns: **slug**, **name**,
**kind**, **path**, and (if available) **last activity**. Highlight the project
whose path is the current working directory.

If the `perun` MCP server is unavailable, fall back to running
`python -m perun projects` in a shell and report its output.
