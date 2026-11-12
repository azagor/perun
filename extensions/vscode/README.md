# Perun Overseer — VS Code companion

Connects a VS Code workspace to the local **Perun** service.

- Status bar: linked / connected / offline.
- Opens the files an OpenCode agent edits and highlights the changed range.
- **Perun: Send Selection to OpenCode** (`Ctrl+Alt+P`) sends the selection to
  Perun; in a VS Code-hosted TUI it is inserted as `@file#lines`, otherwise it
  is copied to the clipboard.

The extension only talks to a loopback Perun endpoint and reads the local API
token from `PERUN_HOME/api-token` (or `~/.perun/api-token`). Workspace-level
`perun.*` settings are deliberately ignored.

Build: `npm ci && npm run package` (requires Node and `@vscode/vsce`).
Install: `code --install-extension perun-overseer.vsix --force`.
