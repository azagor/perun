"""Stop/restart must not silently kill agent runs."""

from __future__ import annotations

import argparse

from perun import __main__ as cli


class _Settings:
    host = "127.0.0.1"
    port = 8760


def test_stop_refuses_while_runs_are_active(control_repo, monkeypatch):  # noqa: ARG001
    monkeypatch.setattr(cli, "_active_run_count", lambda settings: 2)
    monkeypatch.setattr(cli.daemon, "is_another_perun_running", lambda settings: 4242)
    stopped: list[int] = []
    monkeypatch.setattr(cli.daemon, "stop", lambda pid, timeout=0: stopped.append(pid) or True)

    rc = cli.cmd_stop(argparse.Namespace(timeout=5, force=False))

    assert rc == 1
    assert stopped == []


def test_stop_proceeds_with_force(control_repo, monkeypatch):  # noqa: ARG001
    monkeypatch.setattr(cli, "_active_run_count", lambda settings: 2)
    monkeypatch.setattr(cli.daemon, "is_another_perun_running", lambda settings: 4242)
    stopped: list[int] = []
    monkeypatch.setattr(cli.daemon, "stop", lambda pid, timeout=0: stopped.append(pid) or True)
    monkeypatch.setattr(cli.daemon, "clear_pid", lambda: None)

    rc = cli.cmd_stop(argparse.Namespace(timeout=5, force=True))

    assert rc == 0
    assert stopped == [4242]


def test_restart_refuses_while_runs_are_active(control_repo, monkeypatch):  # noqa: ARG001
    monkeypatch.setattr(cli, "_active_run_count", lambda settings: 1)
    monkeypatch.setattr(cli.daemon, "is_another_perun_running", lambda settings: 4242)
    monkeypatch.setattr(cli.daemon, "stop", lambda pid, timeout=0: False)

    rc = cli.cmd_restart(
        argparse.Namespace(
            timeout=5, force=False, host=None, port=None, reload=False,
            bridge=False, foreground=False,
        )
    )

    assert rc == 1


def test_active_run_count_returns_none_when_the_server_is_unreachable(monkeypatch):
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert cli._active_run_count(_Settings()) is None


def test_active_run_count_prefers_blocking_over_count(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"count": 3, "blocking": 0}'

    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert cli._active_run_count(_Settings()) == 0
