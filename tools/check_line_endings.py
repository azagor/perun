"""Quick report on line endings in tracked text files."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    res = subprocess.run(
        ["git", "ls-files"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        print(res.stderr, file=sys.stderr)
        return 1

    crlf_files: list[str] = []
    lf_files: list[str] = []
    mixed_files: list[str] = []

    for line in res.stdout.splitlines():
        p = Path(line)
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        # Only consider text files.
        if b"\x00" in data:
            continue
        has_crlf = b"\r\n" in data
        has_lf_only = b"\n" in data.replace(b"\r\n", b"")
        if has_crlf and has_lf_only:
            mixed_files.append(line)
        elif has_crlf:
            crlf_files.append(line)
        elif has_lf_only:
            lf_files.append(line)

    print(f"CRLF:  {len(crlf_files)}")
    print(f"LF:    {len(lf_files)}")
    print(f"Mixed: {len(mixed_files)}")
    if crlf_files:
        print("\nCRLF files:")
        for f in crlf_files[:20]:
            print(f"  {f}")
    if mixed_files:
        print("\nMixed line-ending files (can break exact-string edits):")
        for f in mixed_files[:20]:
            print(f"  {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
