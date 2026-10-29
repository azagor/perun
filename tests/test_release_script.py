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


def test_bump_version_sources_updates_all_shipped_versions(tmp_path, monkeypatch):
    """The release must keep __init__, pyproject, installer, and CODEMAP in sync."""
    monkeypatch.setattr(release, "PUBLIC", tmp_path)
    (tmp_path / "perun").mkdir()
    (tmp_path / "perun" / "__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "perun"\nversion = "0.1.0"\n', encoding="utf-8")
    (tmp_path / "installer").mkdir()
    (tmp_path / "installer" / "PerunSetup.iss").write_text(
        '#define MyAppVersion "0.1.0"\n', encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CODEMAP.md").write_text(
        'Package docstring + `__version__` (`"0.1.0"`).\n', encoding="utf-8")

    release.bump_version_sources("1.2.3")

    assert '__version__ = "1.2.3"' in (
        tmp_path / "perun" / "__init__.py").read_text(encoding="utf-8")
    assert 'version = "1.2.3"' in (
        tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert '#define MyAppVersion "1.2.3"' in (
        tmp_path / "installer" / "PerunSetup.iss").read_text(encoding="utf-8")
    assert '`"1.2.3"`' in (
        tmp_path / "docs" / "CODEMAP.md").read_text(encoding="utf-8")


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
