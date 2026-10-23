#!/bin/bash
# Perun command-line wrapper installed by the macOS .pkg installer.

set -euo pipefail

PERUN_DIR="/opt/perun"
VENV_DIR="$PERUN_DIR/.venv"
PYTHON="${PERUN_PYTHON:-$VENV_DIR/bin/python}"

if [[ ! -x "$PYTHON" ]]; then
    echo "Perun Python environment not found at $VENV_DIR" >&2
    echo "Re-run the installer or repair the installation." >&2
    exit 1
fi

export PERUN_DIR
exec "$PYTHON" -m perun "$@"
