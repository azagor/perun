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

Each project also exposes a compact ``<slug>.codemap`` outline. Call
``perun_context("<slug>.codemap")`` for a module-level map, then use
``perun_unified_search`` for details.

Always true, never retrieved away:

- Resolve cross-project paths with `perun.project_roots.resolve(name, …)` —
  never hand-craft a relative path.
- Never commit secrets; never paste credentials into a file or a prompt.
- **Never leave a question for the operator unanswered.** Ask with
  `perun_ask_operator` — it reaches them on Telegram and the dashboard and
  returns the reply into *this* session; keep waiting on an `att_<id>` handle
  with `perun_await_operator`, and use `perun_attention_list` to see what is
  pending.
- **Use Perun's bundled MCP tools for discovery, search, and file inspection.**
  Prefer `perun_unified_search`, `perun_grep`, `perun_ast_grep`,
  `perun_global_grep`, `perun_global_ast_grep`, and `perun_memory_search` over
  raw filesystem `grep` or the OpenCode built-in `grep` tool. For confined
  environments without direct filesystem access, use `perun_file_exists`,
  `perun_list_directory`, and `perun_read_file`.
- **Never search by parsing shell output.** Use the `grep` tool (or
  `perun_grep`), never a `Select-String` / `findstr` / shell-`grep` pipeline.
  The Windows console mangles match lines into columns of `1`s or ANSI noise,
  so a search can look empty when it matched — and a misread costs a whole
  retry. Reserve the shell for commands and tests.
- Read the project's `perun.md` manifest before acting on its structure.
