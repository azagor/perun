"""Pre-change copies of OpenCode's config: location, retention and failure paths."""

from __future__ import annotations

import pytest

from perun import backups, config


@pytest.fixture()
def backup_root(tmp_path, monkeypatch):
    root = tmp_path / "perun-home" / "backups"
    monkeypatch.setattr(config, "BACKUPS_DIR", root)
    return root


def _source(tmp_path, name="opencode.json", text="{}"):
    path = tmp_path / "project" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_backup_dir_keeps_real_labels_and_never_escapes(backup_root):
    assert backups.backup_dir("ai-playground") == backup_root / "ai-playground"
    assert backups.backup_dir("opencode") == backup_root / "opencode"
    # A label that would resolve outside the tree, or name nothing useful.
    for label in ("..", ".", "", "   ", "!!!", "/etc"):
        resolved = backups.backup_dir(label)
        assert resolved.parent == backup_root.resolve(), resolved
        assert resolved.name not in ("..", "."), resolved
    # A drive-like label is one segment, not a path.
    assert backups.backup_dir("C:\\temp").name == "C--temp"


def test_save_copy_stamps_and_never_overwrites(backup_root, tmp_path):
    source = _source(tmp_path, text='{"a": 1}')
    first = backups.save_copy(source, label="demo", now=1_700_000_000)
    second = backups.save_copy(source, label="demo", now=1_700_000_000)

    assert first is not None and second is not None
    assert first != second                     # same second: distinct copies
    assert first.read_text(encoding="utf-8") == '{"a": 1}'


def test_save_copy_does_nothing_for_a_missing_source(backup_root, tmp_path):
    assert backups.save_copy(tmp_path / "nope.json", label="demo") is None


def test_a_failed_copy_is_swallowed(backup_root, tmp_path, monkeypatch):
    """A backup must never break the config write it is protecting."""
    source = _source(tmp_path)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(backups.shutil, "copy2", boom)
    assert backups.save_copy(source, label="demo") is None
    assert source.is_file()                    # the original is untouched


def test_prune_keeps_the_newest_and_is_a_noop_for_zero(backup_root, tmp_path):
    source = _source(tmp_path)
    for index in range(7):
        assert backups.save_copy(source, label="demo", keep=7,
                                 now=1_700_000_000 + index) is not None
    directory = backups.backup_dir("demo")
    assert len(backups._copies(directory, "opencode.json")) == 7

    assert backups.prune(directory, "opencode.json", keep=0) == 0
    assert len(backups._copies(directory, "opencode.json")) == 7

    assert backups.prune(directory, "opencode.json", keep=2) == 5
    remaining = backups._copies(directory, "opencode.json")
    assert len(remaining) == 2
    # The newest stamps (lexically greatest) survived.
    assert remaining == sorted(remaining)


def test_prune_ignores_other_stems(backup_root, tmp_path):
    """AGENTS.md copies must never be pruned by a config cleanup."""
    directory = backups.backup_dir("opencode")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "AGENTS.md-20260101-000000").write_text("a", encoding="utf-8")
    source = _source(tmp_path)
    for index in range(3):
        backups.save_copy(source, label="opencode", keep=1, now=1_700_000_000 + index)
    assert (directory / "AGENTS.md-20260101-000000").is_file()
    assert len(backups._copies(directory, "opencode.json")) == 1
