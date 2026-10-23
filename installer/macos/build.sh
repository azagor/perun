#!/bin/bash
# Build the unsigned PerunSetup.pkg for macOS.
# Run on macOS with pkgbuild available.

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
VERSION=$(grep '__version__' "$REPO_ROOT/perun/__init__.py" | sed -E 's/.*"([^"]+)".*/\1/')
OUTPUT_DIR="$REPO_ROOT/installer/macos/Output"
PKGROOT=$(mktemp -d)
SCRIPTS_DIR="$REPO_ROOT/installer/macos/scripts"

echo "[perun-macos] building PerunSetup.pkg version $VERSION"

mkdir -p "$PKGROOT/opt/perun"
mkdir -p "$PKGROOT/usr/local/bin"
mkdir -p "$PKGROOT/Library/LaunchAgents"
mkdir -p "$OUTPUT_DIR"

# Copy the repo into the package root, excluding build artifacts and the
# Windows installer files.
rsync -a \
    --exclude='.git' \
    --exclude='.gitignore' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='node_modules' \
    --exclude='installer/Output' \
    --exclude='installer/macos/Output' \
    --exclude='installer/*.exe' \
    --exclude='installer/*.iss' \
    --exclude='installer/certs' \
    "$REPO_ROOT/" "$PKGROOT/opt/perun/"

# Install the command-line wrapper.
cp "$REPO_ROOT/installer/macos/perun.sh" "$PKGROOT/usr/local/bin/perun"
chmod 755 "$PKGROOT/usr/local/bin/perun"

# Install the LaunchAgent.
cp "$REPO_ROOT/installer/macos/com.peruninthemachine.perun.plist" \
    "$PKGROOT/Library/LaunchAgents/com.peruninthemachine.perun.plist"
chmod 644 "$PKGROOT/Library/LaunchAgents/com.peruninthemachine.perun.plist"

# Ensure postinstall is executable.
chmod 755 "$SCRIPTS_DIR/postinstall"

pkgbuild \
    --root "$PKGROOT" \
    --identifier com.peruninthemachine.perun \
    --version "$VERSION" \
    --scripts "$SCRIPTS_DIR" \
    --install-location / \
    "$OUTPUT_DIR/PerunSetup.pkg"

# Create checksum.
(
    cd "$OUTPUT_DIR"
    shasum -a 256 PerunSetup.pkg | awk '{print $1 "  PerunSetup.pkg"}' > PerunSetup.pkg.sha256
)

echo "[perun-macos] built $OUTPUT_DIR/PerunSetup.pkg"
echo "[perun-macos] checksum  $(cat "$OUTPUT_DIR/PerunSetup.pkg.sha256")"

rm -rf "$PKGROOT"
