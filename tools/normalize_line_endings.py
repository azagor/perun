"""Normalize tracked text files in the working tree to the line ending declared
by .gitattributes (usually LF).

This fixes the common OpenCode ``edit`` failure:
    Could not find oldString in <file>. It must match exactly...
which happens when the working tree contains CRLF but the agent supplies LF.

Usage:
    python tools/normalize_line_endings.py --dry-run   # preview only
    python tools/normalize_line_endings.py             # rewrite files
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def tracked_files() -> list[str]:
    res = subprocess.run(
        ["git", "ls-files"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        print(res.stderr, file=sys.stderr)
        sys.exit(1)
    return [line for line in res.stdout.splitlines() if line]


def is_text(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return b"\x00" not in data


def expected_eol(path: str) -> str | None:
    """Ask git what line ending the working tree should have for *path*."""
    res = subprocess.run(
        ["git", "check-attr", "eol", "--", path],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        return None
    # Output format: "path: eol: value"
    line = res.stdout.strip()
    if ": eol: " in line:
        return line.rsplit(": eol: ", 1)[-1].strip()
    return None


def normalize(path: Path, dry_run: bool) -> bool:
    data = path.read_bytes()
    if b"\r\n" not in data:
        return False
    fixed = data.replace(b"\r\n", b"\n")
    if dry_run:
        print(f"would fix  {path}")
    else:
        path.write_bytes(fixed)
        print(f"fixed      {path}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="preview only")
    args = parser.parse_args(argv)

    fixed = 0
    skipped_crlf = 0
    for rel in tracked_files():
        p = Path(rel)
        if not p.is_file() or not is_text(p):
            continue
        eol = expected_eol(rel)
        if eol == "crlf":
            skipped_crlf += 1
            continue
        if normalize(p, args.dry_run):
            fixed += 1

    print(f"\n{'would fix' if args.dry_run else 'fixed'} {fixed} file(s)")
    if skipped_crlf:
        print(f"skipped {skipped_crlf} file(s) that .gitattributes keeps CRLF")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
