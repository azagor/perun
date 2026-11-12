# Perun Overseer — VS Code companion

Connects a VS Code workspace to the local **Perun** service.

- Status bar: linked / connected / offline.
- Opens the files an OpenCode agent edits and highlights the changed range.
- **Perun: Send Selection to OpenCode** (`Ctrl+Alt+P`) sends the selection to
  Perun; in a VS Code-hosted TUI it is inserted as `@file#lines`, otherwise it
  is copied to the clipboard.
- **Perun: Open TUI (tracked)** from the Command Palette (or the **Perun TUI**
  status-bar item) opens a session picker: resume the latest chat, choose any
  recent session, or start a new one. The chosen session opens in the tracked
  OpenCode terminal beside the editor.
- **Perun Dashboard** opens the dashboard directly in VS Code's **Simple
  Browser** tab; there is no wrapper page or second click.

The extension only talks to a loopback Perun endpoint and reads the local API
token from `PERUN_HOME/api-token` (or `~/.perun/api-token`). Workspace-level
`perun.*` settings are deliberately ignored.

Build: `npm ci && npm run package` (requires Node and `@vscode/vsce`).
Install: `code --install-extension perun-overseer.vsix --force`.
