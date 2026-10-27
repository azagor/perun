# Perun macOS installer

Unsigned `.pkg` installer for macOS. It installs Perun to `/opt/perun`, creates
a dedicated Python virtual environment, wires the global OpenCode integration
for the logged-in console user, and registers a LaunchAgent that starts
`perun serve --bridge` at login.

## Build

Requires macOS with `pkgbuild`, `rsync`, and `shasum` (all present by default).

```bash
./installer/macos/build.sh
```

The installer and its SHA256 checksum are written to
`installer/macos/Output/`:

```
installer/macos/Output/PerunSetup.pkg
installer/macos/Output/PerunSetup.pkg.sha256
```

## Install

Double-click `PerunSetup.pkg`. Because the package is **not signed**, macOS
Gatekeeper blocks it by default on the first run. Right-click the `.pkg` and
choose **Open**, then confirm.

Prerequisites:

- macOS 12 or later
- Python 3.10+ installed at the system level (e.g. from python.org or Homebrew)
- Node.js 18+ and npm (required by the OpenCode CLI)
- Xcode Command Line Tools if any Python dependency needs compilation:
  ```bash
  xcode-select --install
  ```

The installer:

1. Creates `/opt/perun/.venv` and installs `requirements.txt`.
2. Runs `python -m perun install --no-index` for the console user.
3. Installs `/usr/local/bin/perun`.
4. Installs a LaunchAgent and starts Perun.

After install, the dashboard is available at `http://127.0.0.1:8760` and the
`perun` command is on `PATH`.

## Uninstall

```bash
sudo ./installer/macos/uninstall.sh
```

This removes `/opt/perun`, `/usr/local/bin/perun`, and the LaunchAgent. It does
not remove user data in `~/.perun` or the OpenCode global integration; run
`perun install --uninstall` first if you want those removed.

## Known limitations

- **Unsigned.** An Apple Developer ID Installer certificate is required for a
  fully Gatekeeper-friendly install. The release workflow builds an unsigned
  package until that certificate is available.
- **Console user required.** OpenCode integration and LaunchAgent loading run
  for the user currently logged in at `/dev/console`. If the installer runs
  during an MDM deployment with no interactive user, those steps are skipped
  and must be performed manually after first login.
- **No automated uninstall hook.** macOS `.pkg` installers do not provide an
  uninstall callback; use `uninstall.sh`.
