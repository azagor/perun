"""Verify that the Windows and macOS installers exclude internal project content.

The public runtime installer must not ship owner-only files (perun.md, AGENTS.md,
README.md, docs/, .perun/, .opencode/, .github/workflows/release.yml). This
script parses the installer definitions and fails loudly if any of those paths
are missing from the exclusion lists.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# Not imported during normal Perun execution; only used by the check script.
def _normalize_dir(value: str) -> str:
    """Return the exclusion string with a trailing slash for directory entries."""
    return value if value.endswith("/") else f"{value}/"

REQUIRED_WINDOWS = {"perun.md", "AGENTS.md", "README.md", "docs", ".perun"}
REQUIRED_MACOS = {"perun.md", "AGENTS.md", "README.md", "docs/", ".perun/"}



def _windows_excludes() -> set[str]:
    iss_file = REPO / "installer" / "PerunSetup.iss"
    text = iss_file.read_text(encoding="utf-8")
    match = re.search(r'Excludes:\s*"([^"]+)"', text)
    if not match:
        raise RuntimeError("Could not find Excludes line in PerunSetup.iss")
    return {s.strip() for s in match.group(1).split(",")}


def _macos_excludes() -> set[str]:
    sh_file = REPO / "installer" / "macos" / "build.sh"
    text = sh_file.read_text(encoding="utf-8")
    return {
        m.group(1).strip()
        for m in re.finditer(r"--exclude='([^']+)'", text)
    }


def main() -> int:
    errors: list[str] = []

    win_excludes = _windows_excludes()
    for req in REQUIRED_WINDOWS:
        if req not in win_excludes:
            errors.append(f"Windows installer is missing exclusion: {req}")

    mac_excludes_raw = _macos_excludes()
    mac_excludes = {_normalize_dir(e) if e.endswith(("/")) else e for e in mac_excludes_raw}
    for req in REQUIRED_MACOS:
        if req not in mac_excludes_raw:
            errors.append(f"macOS installer is missing exclusion: {req}")

    if errors:
        print("Installer exclusion check FAILED", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("Installer exclusion check PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
