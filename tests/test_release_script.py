"""Regression tests for the release-script changelog promotion."""

from __future__ import annotations

import datetime

import tools.release as release


def _write_changelog(tmp_path, text: str):
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / "CHANGELOG.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_changelog_moves_unreleased_body_into_new_version(tmp_path, monkeypatch):
    """Unreleased entries must end up under the new version, not a new Unreleased."""
    monkeypatch.setattr(release, "PUBLIC", tmp_path)
    path = _write_changelog(tmp_path, """# Perun — Changelog

## Unreleased

### Added

- New thing

## v0.8.34

*Released 2026-10-29.*

### Fixed

- Old thing
""")
    release.changelog_unreleased_to_version("0.9.0")
    text = path.read_text(encoding="utf-8")
    today = datetime.date.today().strftime("%Y-%m-%d")
    assert f"## v0.9.0\n\n*Released {today}.*" in text
    unreleased, version = text.split("## v0.9.0", 1)
    assert "New thing" not in unreleased
    assert "New thing" in version
    assert "Old thing" in version
    assert release.UNRELEASED_PLACEHOLDER in unreleased


def test_changelog_empty_unreleased_creates_version_section(tmp_path, monkeypatch):
    """An empty Unreleased section still creates a clean version section."""
    monkeypatch.setattr(release, "PUBLIC", tmp_path)
    path = _write_changelog(tmp_path, """# Perun — Changelog

## Unreleased

_No unreleased changes yet._

## v0.8.34

*Released 2026-10-29.*
""")
    release.changelog_unreleased_to_version("0.9.0")
    text = path.read_text(encoding="utf-8")
    assert release.UNRELEASED_PLACEHOLDER in text
    assert "## v0.9.0" in text
    version_block = text.split("## v0.9.0", 1)[1].split("## v0.8.34", 1)[0]
    assert release.UNRELEASED_PLACEHOLDER not in version_block


def test_changelog_is_idempotent_for_existing_version(tmp_path, monkeypatch):
    """Re-running the same release must not rewrite the file."""
    monkeypatch.setattr(release, "PUBLIC", tmp_path)
    original = """# Perun — Changelog

## Unreleased

_No unreleased changes yet._

## v0.9.0

*Released 2026-10-29.*
"""
    path = _write_changelog(tmp_path, original)
    release.changelog_unreleased_to_version("0.9.0")
    assert path.read_text(encoding="utf-8") == original
