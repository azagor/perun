#!/bin/bash
# Uninstall Perun from a macOS .pkg installation.
# Must be run as root (uses sudo if not root).

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "[perun] re-running with sudo ..." >&2
    exec sudo "$0" "$@"
fi

LABEL="com.peruninthemachine.perun"
USER_UID=$(stat -f '%Du' /dev/console 2>/dev/null || echo "")

if [[ -n "$USER_UID" ]]; then
    echo "[perun] stopping LaunchAgent ..."
    launchctl bootout "gui/$USER_UID/$LABEL" 2>/dev/null || true
fi

rm -f "/Library/LaunchAgents/$LABEL.plist"
rm -f "/usr/local/bin/perun"
rm -rf "/opt/perun"

echo "Perun has been uninstalled."
