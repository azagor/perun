"""Perun release automation.

Steps covered:
1. Validate both repos (public perun + private perun_internal) exist and are clean-ish.
2. Bump version in perun/__init__.py.
3. Move Unreleased -> vX.Y.Z in docs/CHANGELOG.md and update docs/TECHNICAL_DOCUMENTATION.md.
4. Sync internal project content to perun_internal via tools/sync-internal.py --reverse.
5. Commit both repos, tag public, push public master+tag and internal master.

Usage:
    python tools/release.py 0.8.24 "Release summary here"
    python tools/release.py 0.8.24 --dry-run

Requires git, Python, and the perun_internal companion repo at ../perun_internal.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

PUBLIC = Path(__file__).resolve().parent.parent
INTERNAL = PUBLIC.parent / "perun_internal"
SYNC_SCRIPT = PUBLIC / "tools" / "sync-internal.py"

# Files that must stay in sync between the two repos.
INTERNAL_ITEMS = [
    "perun.md",
    "AGENTS.md",
    "README.md",
    "docs",
    ".perun",
    ".opencode",
    ".github/workflows/release.yml",
]


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> str:
    """Run a command and return stdout."""
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        print(f"[ERR] {' '.join(cmd)}\n{result.stderr}", file=sys.stderr)
        raise SystemExit(1)
    return result.stdout.strip()


def repo_is_clean(repo: Path) -> bool:
    return run(["git", "status", "--short"], cwd=repo) == ""


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_file(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def bump_init_version(version: str) -> None:
    path = PUBLIC / "perun" / "__init__.py"
    text = read_file(path)
    new_text = re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{version}"', text)
    if new_text == text:
        raise SystemExit(f"[ERR] Could not find __version__ in {path}")
    write_file(path, new_text)
    print(f"[OK] Bumped perun/__init__.py to {version}")


UNRELEASED_HEADER = "## Unreleased"
UNRELEASED_PLACEHOLDER = "_No unreleased changes yet._"


def changelog_unreleased_to_version(version: str) -> None:
    """Promote the current ``## Unreleased`` body into a new version section.

    The previous implementation replaced only the header, which left the
    unreleased entries below a *new* empty ``## Unreleased`` section and buried
    the actual release notes. This version moves the body into the new version
    section and starts a fresh, empty Unreleased section above it.
    """
    path = PUBLIC / "docs" / "CHANGELOG.md"
    text = read_file(path)
    if f"## v{version}" in text:
        print(f"[WARN] Changelog already has ## v{version}; skipping")
        return
    header_start = text.find(UNRELEASED_HEADER)
    if header_start == -1:
        raise SystemExit("[ERR] docs/CHANGELOG.md has no ## Unreleased section")

    body_start = header_start + len(UNRELEASED_HEADER)
    next_match = re.search(r"^## ", text[body_start:], re.MULTILINE)
    if next_match is None:
        body_end = len(text)
        suffix = ""
    else:
        body_end = body_start + next_match.start()
        suffix = text[body_end:]

    body = text[body_start:body_end].strip()
    if body == UNRELEASED_PLACEHOLDER:
        body = ""
    date = datetime.date.today().strftime("%Y-%m-%d")
    new_text = text[:header_start]
    new_text += f"{UNRELEASED_HEADER}\n\n{UNRELEASED_PLACEHOLDER}\n\n"
    new_text += f"## v{version}\n\n*Released {date}.*\n\n"
    if body:
        new_text += body + "\n\n"
    new_text += suffix.lstrip("\n")
    write_file(path, new_text)
    print(f"[OK] Created docs/CHANGELOG.md section for v{version}")


def update_techdoc_latest_release(version: str, summary: str) -> None:
    path = PUBLIC / "docs" / "TECHNICAL_DOCUMENTATION.md"
    text = read_file(path)
    pattern = r"\*\*Latest release:\*\* v[0-9]+\.[0-9]+\.[0-9]+ \([^)]+\)\..*?(?=\n\n|\n\*\*The Context layer)"
    replacement = f"**Latest release:** v{version} (see §15). {summary}"
    new_text, count = re.subn(pattern, replacement, text, count=1)
    if count == 0:
        # Fallback: match the simpler form.
        new_text, count = re.subn(
            r"\*\*Latest release:\*\* v[0-9]+\.[0-9]+\.[0-9]+[^.]*\.",
            f"**Latest release:** v{version} (see §15). {summary}",
            text,
            count=1,
        )
    if count == 0:
        raise SystemExit("[ERR] Could not find **Latest release** line in docs/TECHNICAL_DOCUMENTATION.md")
    write_file(path, new_text)
    print(f"[OK] Updated docs/TECHNICAL_DOCUMENTATION.md latest release to v{version}")


def add_techdoc_revision_entry(version: str, summary: str) -> None:
    path = PUBLIC / "docs" / "TECHNICAL_DOCUMENTATION.md"
    text = read_file(path)
    section = "## 15. Revision history"
    if section not in text:
        raise SystemExit("[ERR] docs/TECHNICAL_DOCUMENTATION.md missing '## 15. Revision history'")
    date = datetime.date.today().strftime("%Y-%m-%d")
    entry = f"\n### {date} — v{version}: {summary}\n"
    new_text = text.replace(section, section + entry, 1)
    write_file(path, new_text)
    print(f"[OK] Added revision entry for v{version}")


def sync_to_internal() -> None:
    if not SYNC_SCRIPT.exists():
        raise SystemExit(f"[ERR] Sync script not found: {SYNC_SCRIPT}")
    print("[OK] Syncing public -> perun_internal...")
    run([sys.executable, str(SYNC_SCRIPT), "--reverse", "--force"], cwd=PUBLIC)


def git_commit(repo: Path, message: str) -> None:
    run(["git", "add", "-A"], cwd=repo)
    run(["git", "commit", "-m", message], cwd=repo)
    print(f"[OK] Committed in {repo.name}")


def git_tag(repo: Path, version: str, message: str) -> None:
    run(["git", "tag", "-a", f"v{version}", "-m", message], cwd=repo)
    print(f"[OK] Tagged v{version} in {repo.name}")


def git_push(repo: Path, refs: list[str]) -> None:
    run(["git", "push", "origin", *refs], cwd=repo)
    print(f"[OK] Pushed {', '.join(refs)} from {repo.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Perun release automation")
    parser.add_argument("version", help="Version to release, e.g. 0.8.24")
    parser.add_argument("summary", nargs="?", default="", help="Short release summary")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without changing files")
    parser.add_argument("--skip-tests", action="store_true", help="Skip running the test suite")
    args = parser.parse_args()

    version = args.version.strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        print(f"[ERR] Version must be X.Y.Z, got: {version}", file=sys.stderr)
        return 1

    summary = args.summary.strip() or f"Release v{version}"

    # 1. Validate repos.
    for repo in (PUBLIC, INTERNAL):
        if not (repo / ".git").is_dir():
            print(f"[ERR] Not a git repo: {repo}", file=sys.stderr)
            return 1
    if not repo_is_clean(PUBLIC):
        print("[WARN] Public repo has uncommitted changes.")
        if not args.dry_run:
            if sys.stdin.isatty():
                answer = input("Continue anyway? [y/N]: ").strip().lower()
                if answer not in ("y", "yes"):
                    print("Aborted.")
                    return 1
            else:
                print("Non-interactive mode: aborting due to uncommitted changes.", file=sys.stderr)
                return 1

    if not args.skip_tests and not args.dry_run:
        print("[OK] Running test suite...")
        run([sys.executable, "-m", "pytest", "tests/", "-q"], cwd=PUBLIC)

    if args.dry_run:
        print(f"[DRY-RUN] Would release v{version}: {summary}")
        print(f"[DRY-RUN] Would bump perun/__init__.py")
        print(f"[DRY-RUN] Would update docs/CHANGELOG.md and docs/TECHNICAL_DOCUMENTATION.md")
        print(f"[DRY-RUN] Would sync to {INTERNAL}")
        print(f"[DRY-RUN] Would commit+tag public, commit internal, push both")
        return 0

    # 2. Bump version.
    bump_init_version(version)

    # 3. Update docs.
    changelog_unreleased_to_version(version)
    update_techdoc_latest_release(version, summary)
    add_techdoc_revision_entry(version, summary)

    # 4. Sync to internal.
    sync_to_internal()

    # 5. Commit public repo.
    public_message = f"Release v{version}\n\n{summary}"
    git_commit(PUBLIC, public_message)
    git_tag(PUBLIC, version, f"Release v{version}: {summary}")

    # 6. Commit internal repo.
    internal_message = f"Sync public v{version}\n\n{summary}"
    git_commit(INTERNAL, internal_message)

    # 7. Push.
    git_push(PUBLIC, ["master", f"v{version}"])
    git_push(INTERNAL, ["master"])

    print(f"\n[OK] Released v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
