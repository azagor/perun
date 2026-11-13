# Perun Overseer — VS Code companion

Connects a VS Code workspace to the local **Perun** service.

- Status bar: linked / connected / offline.
- Opens the files an OpenCode agent edits and highlights the changed range —
  **without stealing focus**: a visible file is highlighted in place, a new file
  opens beside the active editor (`preserveFocus`), and an unfocused window only
  gets a clickable `$(edit) file:line` status hint. **Perun: Reveal Last Agent
  Edit** opens it deliberately.
- **Perun: Send Selection to OpenCode** (`Ctrl+Alt+P`) sends the selection to
  Perun; in a VS Code-hosted TUI it is inserted as `@file#lines`, otherwise it
  is copied to the clipboard.
- **Perun TUI** (status bar) resumes the newest TUI chat directly — the
  dashboard's **Run**. **Perun: Choose TUI session…** opens the picker:
  **Resume latest** is that same newest TUI chat (a headless job session is
  labelled `headless run` and never auto-selected), then any of the last 50
  sessions, or **Start a new session**. The chosen session opens in the tracked
  OpenCode terminal beside the editor.
- **Perun Dashboard** opens the dashboard directly in VS Code's **Simple
  Browser** tab; there is no wrapper page or second click.
- **Restricted Mode is respected.** The companion reports `trusted: false`,
  refuses to connect or start a TUI, and reconnects as soon as you trust the
  workspace. Use **Perun: Manage Workspace Trust** (or the banner's *Yes, I
  trust the authors*) — Perun cannot grant trust itself.

The extension only talks to a loopback Perun endpoint and reads the local API
token from `PERUN_HOME/api-token` (or `~/.perun/api-token`). Workspace-level
`perun.*` settings are deliberately ignored.

## Install

The Perun installers do this for you when VS Code and its `code` CLI are
present: the Windows wizard offers **Install the Perun companion for Visual
Studio Code** (checked by default) and the macOS `.pkg` postinstall installs it
for the console user.

To install it by hand, download `perun-overseer.vsix` from the latest Perun
GitHub release (every release attaches it) and run:

```bash
code --install-extension perun-overseer.vsix --force
```

Build from a checkout: `npm ci && npm run package` (requires Node and
`@vscode/vsce`); the VSIX is written to
`../../installer/vscode/perun-overseer.vsix`.

