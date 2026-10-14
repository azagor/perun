"""Regression tests for the Telegram bridge single-instance lock.

The lock previously used the bare Windows mutex name ``PerunTelegramBridge``,
which is the *same* name the unrelated ``trading_bot/scripts/telegram_bridge.py``
uses. Whenever that other bridge was running, ``perun status`` falsely reported
Perun's own bridge as running. The mutex name is now namespaced by
``PERUN_HOME`` and ``PidLock.is_held`` provides a side-effect-free probe.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from perun import config
from perun.telegram import PidLock, _default_mutex_name


def test_mutex_name_is_namespaced_and_not_bare():
    name = _default_mutex_name()
    # Never the bare name shared with trading_bot's bridge.
    assert name != "PerunTelegramBridge"
    assert name.startswith("PerunManagedBridge-")
    # Stable for a given home.
    assert name == _default_mutex_name()


def test_is_held_does_not_write_pid_file(tmp_path: Path):
    pid_path = tmp_path / "telegram.pid"
    assert PidLock.is_held(pid_path) in (True, False)
    # A read-only probe must not create the pid file as a side effect.
    assert not pid_path.exists()


def test_acquire_release_roundtrip(tmp_path: Path):
    pid_path = tmp_path / "telegram.pid"
    lock = PidLock(pid_path)
    assert lock.acquire() is True
    # While held (in this process), a probe reports it held.
    assert PidLock.is_held(pid_path) is True
    lock.release()
    assert PidLock.is_held(pid_path) is False


@pytest.mark.skipif(os.name != "nt", reason="named-mutex behaviour is Windows-only")
def test_status_probe_is_not_fooled_by_bare_name(tmp_path: Path):
    """Holding the *bare* trading_bot name must not make Perun report held."""

    import ctypes

    k32 = ctypes.windll.kernel32
    handle = k32.CreateMutexW(None, False, "PerunTelegramBridge")
    assert handle
    try:
        # The foreign bare mutex exists, but Perun's namespaced probe is free.
        assert PidLock.is_held(tmp_path / "telegram.pid") is False
    finally:
        k32.CloseHandle(handle)


def test_home_is_used_for_namespacing():
    # Sanity: config.HOME is resolved (the conftest points it at a temp dir).
    assert config.HOME is not None


def test_second_lock_cannot_acquire_while_held(tmp_path: Path):
    """The core guarantee: the bridge can never multiply.

    ``getUpdates`` allows a single consumer per bot token, so a second poller
    would fight with HTTP 409s. A distinct ``PidLock`` (a fresh object, standing
    in for a second process: another launcher, ``perun bridge``, or the
    ``PerunBridge`` logon task) must be *refused* while the first holds it, and
    may take over only after the first releases.
    """
    pid_path = tmp_path / "telegram.pid"
    first = PidLock(pid_path)
    assert first.acquire() is True

    second = PidLock(pid_path)
    assert second.acquire() is False          # refused: would be a duplicate poller
    assert second.acquired is False
    assert first.acquired is True             # the original keeps the lock

    first.release()
    assert second.acquire() is True           # now exactly one holder, the second
    second.release()
    assert PidLock.is_held(pid_path) is False
