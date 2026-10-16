#!/usr/bin/env bash
# Quick-setup installer for Perun (Unix).
# Clones (or reuses) the repo and installs Python dependencies + the global
# OpenCode integration.
#
#   curl -fsSL https://raw.githubusercontent.com/azagor/perun/master/setup.sh | bash
#   # or, with options:
#   curl -fsSL ... | bash -s -- --install-dir ~/perun

set -euo pipefail

REPO_URL="https://github.com/azagor/perun.git"
BRANCH="master"
INSTALL_DIR="${HOME}/perun"
PYTHON="${PERUN_PYTHON:-}"
SKIP_DEPS=0
SKIP_GLOBAL=0
NO_INDEX=0

log() { echo "[perun] $*"; }
err() { echo "[perun] ERROR: $*" >&2; }

usage() {
    cat <<EOF
Usage: setup.sh [OPTIONS]

Options:
  --install-dir DIR   Where to clone the repo (default: ~/perun)
  --repo-url URL      Git URL to clone (default: $REPO_URL)
  --branch BRANCH     Branch to checkout (default: $BRANCH)
  --python EXE        Path to python3 executable
  --skip-deps         Skip pip install -r requirements.txt
  --skip-global       Skip the global OpenCode integration
  --no-index          Skip indexing project docs
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --repo-url) REPO_URL="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --python) PYTHON="$2"; shift 2 ;;
        --skip-deps) SKIP_DEPS=1; shift ;;
        --skip-global) SKIP_GLOBAL=1; shift ;;
        --no-index) NO_INDEX=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) err "unknown option: $1"; usage; exit 1 ;;
    esac
done

# --------------------------------------------------------------------------- #
# Resolve Python
# --------------------------------------------------------------------------- #
resolve_python() {
    if [[ -n "$PYTHON" ]]; then
        if command -v "$PYTHON" >/dev/null 2>&1; then
            echo "$PYTHON"
            return 0
        fi
        err "specified Python not found: $PYTHON"
        return 1
    fi

    for cmd in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            local ver
            ver=$("$cmd" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
            if [[ "$ver" =~ ^3\.([1-9][0-9])$ || "$ver" =~ ^3\.([0-9])$ ]]; then
                local minor="${BASH_REMATCH[1]}"
                if [[ "$minor" -ge 10 ]]; then
                    echo "$cmd"
                    return 0
                fi
            fi
        fi
    done

    err "No Python 3.10+ found. Install Python 3.10+ and ensure it is on PATH."
    return 1
}

PY=$(resolve_python)
export PERUN_PYTHON="$PY"
log "using Python: $PY ($($PY --version))"

# --------------------------------------------------------------------------- #
# Clone or reuse repo
# --------------------------------------------------------------------------- #
if [[ -f "$(dirname "$0")/perun/__main__.py" ]]; then
    REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
    log "running from existing checkout: $REPO_ROOT"
elif [[ -d "$INSTALL_DIR/.git" && -f "$INSTALL_DIR/perun/__main__.py" ]]; then
    REPO_ROOT="$INSTALL_DIR"
    log "reusing existing clone: $REPO_ROOT"
else
    if ! command -v git >/dev/null 2>&1; then
        err "git is required but not found. Install git and try again."
        exit 1
    fi
    if [[ -e "$INSTALL_DIR" ]]; then
        err "$INSTALL_DIR already exists and is not a Perun repo. Remove it or choose another --install-dir."
        exit 1
    fi
    log "cloning $REPO_URL into $INSTALL_DIR ..."
    git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$INSTALL_DIR"
    REPO_ROOT="$INSTALL_DIR"
fi

cd "$REPO_ROOT"

# --------------------------------------------------------------------------- #
# Install dependencies
# --------------------------------------------------------------------------- #
if [[ "$SKIP_DEPS" -eq 0 ]]; then
    log "installing Python dependencies ..."
    "$PY" -m pip install -r requirements.txt
fi

# --------------------------------------------------------------------------- #
# Global OpenCode integration
# --------------------------------------------------------------------------- #
if [[ "$SKIP_GLOBAL" -eq 0 ]]; then
    log "installing global OpenCode integration ..."
    if [[ "$NO_INDEX" -eq 1 ]]; then
        "$PY" -m perun install --no-index
    else
        "$PY" -m perun install
    fi
fi

log "Perun is ready at $REPO_ROOT"
log "Start the dashboard with: $PY -m perun serve"
