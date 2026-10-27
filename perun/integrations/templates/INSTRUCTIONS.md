# Perun — context kernel

Perun is the machine-wide overseer for every OpenCode project: a registry of
all local projects, project-namespaced vector memory, cross-project code
search, and agent-run orchestration.

**First action in any Perun-managed project:** before reading or editing code,
call `perun_context("<what you are about to do>")`. It resolves your intent
into the reference material you need — build commands, server access, key
files, conventions, the tool surface. Do not guess at these; they are
retrievable, and a guess costs a wrong turn.

Perun's reference material lives in *context docs* with stable keys:

{{KEY_TABLE}}

Always true, never retrieved away:

- Resolve cross-project paths with `perun.project_roots.resolve(name, …)` —
  never hand-craft a relative path.
- Never commit secrets; never paste credentials into a file or a prompt.
- **Use Perun's bundled MCP tools for discovery, search, and file inspection.**
  Prefer `perun_unified_search`, `perun_grep`, `perun_ast_grep`,
  `perun_global_grep`, `perun_global_ast_grep`, and `perun_memory_search` over
  raw filesystem `grep` or the OpenCode built-in `grep` tool. For confined
  environments without direct filesystem access, use `perun_file_exists`,
  `perun_list_directory`, and `perun_read_file`.
- Read the project's `perun.md` manifest before acting on its structure.
