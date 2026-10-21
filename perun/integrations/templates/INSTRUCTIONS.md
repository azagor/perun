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
- Prefer `perun_unified_search` over ad-hoc grep for discovery.
- Read the project's `perun.md` manifest before acting on its structure.
