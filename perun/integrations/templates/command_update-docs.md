---
description: Update project documentation from the current codebase
---

Update the project's documentation files so they accurately reflect the current
codebase. This command is available in every OpenCode project because Perun
registers it globally.

Arguments: `$ARGUMENTS` — optional project slug (defaults to the current project).

Steps:

1. **Resolve the project.**
   - If `$ARGUMENTS` contains a project slug, use it.
   - Otherwise use the current working directory's project (the `perun` MCP
     server scopes to cwd when `project` is omitted).
   - If no project is active, ask the user to specify one.

2. **Read the project manifest** with `perun_get_manifest`.

3. **Build the list of docs to update.**
   - Always include these defaults if they exist (or need to be created):
     - `docs/CODEMAP.md`
     - `docs/CHANGELOG.md`
     - `docs/TECHNICAL_DOCUMENTATION.md`
   - Add every value from `manifest.data.docs` (a map of label → relative path).
   - Add every `path` from `manifest.data.anchors` where `kind == "doc"`.
   - Deduplicate paths while preserving order.

4. **Update each doc in the list.**
   - Read the existing file if it exists; if it does not exist, create it.
   - For `docs/CODEMAP.md`: produce a concise map of the codebase — list source
     files/modules with their responsibilities and key identifiers.
   - For `docs/CHANGELOG.md`: add an "Unreleased" section summarizing the recent
     changes if one does not exist; otherwise update it.
   - For `docs/TECHNICAL_DOCUMENTATION.md`: update architecture, data model, API
     endpoints, and any other technical sections to match the current code.
   - For other docs from the manifest: update them according to their purpose,
     inferred from the file name and any existing content.
   - Write the updated content back to the file.
   - Report which files were created, updated, or skipped.

5. **Re-index the project's docs** with `perun_docs_index` so the updated files
   are searchable in Perun memory.

Be concise but thorough. Do not modify files outside the doc list. If the
manifest cannot be read, fall back to updating the three default docs only.
