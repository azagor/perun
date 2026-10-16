"""Secret storage: password precedence and DPAPI-backed persistence.

The DPAPI-dependent paths are Windows-only; on other platforms
``persist_password()`` degrades to a no-op returning ``False``.
"""

from __future__ import annotations

import os

import pytest

from perun import config, secrets


def test_password_prefers_env(monkeypatch):
    monkeypatch.setenv("PERUN_PASSWORD", "env-pw")
    assert secrets._password() == "env-pw"


def test_password_none_without_env_or_file(monkeypatch, tmp_path):
    monkeypatch.delenv("PERUN_PASSWORD", raising=False)
    monkeypatch.setattr(config, "HOME", tmp_path)
    assert secrets._password() is None


def test_persist_password_empty_is_false(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    assert secrets.persist_password("") is False


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_persist_password_roundtrip_is_encrypted(monkeypatch, tmp_path):
    monkeypatch.delenv("PERUN_PASSWORD", raising=False)
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)

    assert secrets.persist_password("s3cret-pw") is True

    stored = tmp_path / secrets._PASSWORD_FILE
    assert stored.is_file()
    # The stored blob must be DPAPI-encrypted, never the raw password.
    assert b"s3cret-pw" not in stored.read_bytes()

    # A fresh lookup (no env var) decrypts the stored password.
    assert secrets._password() == "s3cret-pw"


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_password_env_wins_over_stored(monkeypatch, tmp_path):
    monkeypatch.delenv("PERUN_PASSWORD", raising=False)
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)

    assert secrets.persist_password("stored-pw") is True
    monkeypatch.setenv("PERUN_PASSWORD", "env-pw")
    assert secrets._password() == "env-pw"
