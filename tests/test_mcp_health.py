"""The MCP health surface: reported state, measured startup, and repair.

OpenCode keeps the MCP connection state per location and caches a failure for
the whole session; a connect that wedges in ``pending`` stays wedged until the
client is explicitly disconnected. These tests pin the reads, the repair
sequence, and one rule that matters more than any other: "I could not ask the
service" is never reported as success.
"""

from __future__ import annotations

import argparse
import json
import os
import time

from perun import mcp_health


class FakeAPI:
    """Stands in for OpenCodeAPI: state machine + call log."""

    def __init__(self, state: str = "connected", connect_state: str = "connected"):
        self.state = state
        self.connect_state = connect_state
        self.calls: list[str] = []

    def mcp_servers(self, directory: str = ""):
        return [{
            "name": "perun",
            "status": {
                "status": self.state,
                "error": "Request timed out" if self.state == "failed" else "",
            },
        }]

    def mcp_disconnect(self, server: str, directory: str = "",
                       timeout: float | None = None) -> None:
        self.calls.append("disconnect")
        self.state = "disabled"

    def mcp_connect(self, server: str, directory: str = "",
                    timeout: float | None = None) -> None:
        self.calls.append(f"connect:{timeout}")
        self.state = self.connect_state


class Unreachable:
    """The service is down: every call fails, and nothing may be attempted."""

    def mcp_servers(self, directory: str = ""):
        raise RuntimeError("opencode service unreachable: timed out")

    def mcp_disconnect(self, *args, **kwargs):
        raise AssertionError("a dead service must not be told to disconnect")

    def mcp_connect(self, *args, **kwargs):
        raise AssertionError("a dead service must not be told to connect")


# --------------------------------------------------------------- reading ---- #

def test_status_reads_the_state_the_service_reports():
    status = mcp_health.server_status("perun", r"C:\p", FakeAPI(state="failed"))

    assert status["state"] == "failed"
    assert status["error"] == "Request timed out"
    assert status["configured"] is True
    assert status["reachable"] is True


def test_status_reports_an_unconfigured_server():
    class Empty(FakeAPI):
        def mcp_servers(self, directory: str = ""):
            return []

    status = mcp_health.server_status("perun", r"C:\p", Empty())

    assert status["state"] == ""
    assert status["configured"] is False
    # The service answered; it simply lists no such server here.
    assert status["reachable"] is True


def test_status_marks_an_unreachable_service():
    status = mcp_health.server_status("perun", r"C:\p", Unreachable())

    assert status["reachable"] is False
    assert status["configured"] is False
    assert "unreachable" in status["error"]


def test_diagnose_calls_an_unreachable_service_unreachable():
    report = mcp_health.diagnose("perun", r"C:\p", Unreachable())

    assert report["verdict"] == "service_unreachable"
    assert report["reachable"] is False
    # The wrong advice is worse than none: this is not a config problem.
    assert "add the `perun` entry" not in report["remedy"]
    assert "service" in report["remedy"]


def test_diagnose_names_the_wedge_and_the_repair(monkeypatch):
    monkeypatch.setattr(mcp_health, "read_startup", lambda: {})

    report = mcp_health.diagnose("perun", r"C:\p", FakeAPI(state="pending"))

    assert report["verdict"] == "stuck_pending"
    assert "--fix" in report["remedy"]


def test_diagnose_flags_a_slow_startup(monkeypatch):
    monkeypatch.setattr(mcp_health, "read_startup",
                        lambda: {"total_seconds": 58.0, "recorded_at": "now"})

    report = mcp_health.diagnose("perun", r"C:\p", FakeAPI(state="connected"))

    assert report["slow_startup"] is True
    assert "slow_startup" in report["verdict"]
    assert "60s" in report["remedy"]


def test_diagnose_surfaces_a_failed_schema_reconcile(monkeypatch):
    monkeypatch.setattr(mcp_health, "read_startup",
                        lambda: {"total_seconds": 3.0,
                                 "db_init_error": "OperationalError: locked"})

    report = mcp_health.diagnose("perun", r"C:\p", FakeAPI(state="connected"))

    assert "OperationalError: locked" in report["remedy"]


# ---------------------------------------------------------------- repair ---- #

def test_revive_breaks_a_connection_wedged_in_pending():
    """The exact failure observed: the client never leaves ``pending``."""
    api = FakeAPI(state="pending", connect_state="connected")

    result = mcp_health.revive("perun", r"C:\p", api, wait=0)

    assert api.calls == ["disconnect", f"connect:{mcp_health.CONNECT_TIMEOUT_SECONDS}"]
    assert result["ok"] is True
    assert result["state"] == "connected"
    assert result["already"] is False


def test_revive_leaves_a_connected_server_alone():
    api = FakeAPI(state="connected")

    result = mcp_health.revive("perun", r"C:\p", api, wait=0)

    assert result == {"ok": True, "state": "connected", "already": True,
                      "directory": r"C:\p", "server": "perun"}
    assert api.calls == []


def test_revive_reports_a_failed_reconnect():
    api = FakeAPI(state="failed", connect_state="failed")

    result = mcp_health.revive("perun", r"C:\p", api, wait=0)

    assert result["ok"] is False
    assert result["state"] == "failed"
    assert "disconnect" in api.calls and any(c.startswith("connect") for c in api.calls)


def test_revive_refuses_to_touch_a_dead_service():
    result = mcp_health.revive("perun", r"C:\p", Unreachable(), wait=30)

    assert result["ok"] is False
    assert result["state"] == ""
    assert "unreachable" in result["error"]


def test_settle_does_not_wait_on_a_state_that_cannot_change():
    """``""`` means "no such server" or "service down" — neither is transient."""

    class Absent(FakeAPI):
        def mcp_servers(self, directory: str = ""):
            return []

        def mcp_connect(self, *args, **kwargs):
            pass

        def mcp_disconnect(self, *args, **kwargs):
            pass

    began = time.time()
    status = mcp_health.settle("perun", r"C:\p", Absent(), wait=30)

    assert time.time() - began < 1.0, "settle burned the whole window on a stuck state"
    assert status["state"] == ""


def test_settle_waits_only_until_pending_resolves(monkeypatch):
    api = FakeAPI(state="pending")
    monkeypatch.setattr(mcp_health, "SETTLE_POLL_SECONDS", 0.01)
    reads: list[int] = []
    original = api.mcp_servers

    def counting(directory: str = ""):
        reads.append(1)
        if len(reads) >= 3:
            api.state = "connected"
        return original(directory)

    api.mcp_servers = counting

    status = mcp_health.settle("perun", r"C:\p", api, wait=5)

    assert status["state"] == "connected"
    assert len(reads) == 3


# --------------------------------------------------------------- records ---- #

def test_startup_record_round_trips_and_merges(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_health, "STARTUP_FILE", tmp_path / "mcp-startup.json")

    mcp_health.record_startup({"total_seconds": 3.1, "pid": 42})
    mcp_health.record_startup({"db_init_seconds": 0.02})

    record = mcp_health.read_startup()
    assert record["total_seconds"] == 3.1
    assert record["db_init_seconds"] == 0.02
    assert record["pid"] == 42
    assert record["recorded_at"]
    assert json.loads((tmp_path / "mcp-startup.json").read_text())["pid"] == 42


def test_recording_a_startup_never_raises(tmp_path, monkeypatch):
    # A file where the directory should be: the write must fail silently.
    blocker = tmp_path / "blocked"
    blocker.write_text("x")
    monkeypatch.setattr(mcp_health, "STARTUP_FILE", blocker / "mcp-startup.json")

    mcp_health.record_startup({"total_seconds": 1.0})

    assert mcp_health.read_startup() == {}


# ------------------------------------------------------------ directory ---- #

def test_project_directories_merges_locations_and_registry(tmp_path, monkeypatch):
    """One connection per location, so one repair per location."""
    from perun import registry

    dirs = {name: tmp_path / name for name in ("a", "b", "c")}
    for directory in dirs.values():
        directory.mkdir()

    class Api(FakeAPI):
        def locations(self):
            return [str(dirs["a"]), str(dirs["b"]),
                    os.path.join(str(dirs["a"]), ".")]

    monkeypatch.setattr(registry, "list_projects", lambda: [
        {"path": str(dirs["b"])}, {"path": str(dirs["c"])}, {"path": ""},
    ])

    assert mcp_health.project_directories(Api()) == [
        str(dirs["a"]), str(dirs["b"]), str(dirs["c"])]


def test_project_directories_skips_paths_that_no_longer_exist(tmp_path, monkeypatch):
    """A stale registry entry must not materialize an OpenCode location."""
    from perun import registry

    existing = tmp_path / "a"
    existing.mkdir()

    class Api(FakeAPI):
        def locations(self):
            return []

    monkeypatch.setattr(registry, "list_projects", lambda: [
        {"path": str(existing)}, {"path": str(tmp_path / "gone")},
    ])

    assert mcp_health.project_directories(Api()) == [str(existing)]


def test_project_directories_survives_an_unreachable_service(tmp_path, monkeypatch):
    from perun import registry

    directory = tmp_path / "c"
    directory.mkdir()

    class Api(FakeAPI):
        def locations(self):
            raise RuntimeError("service down")

    monkeypatch.setattr(registry, "list_projects", lambda: [{"path": str(directory)}])

    assert mcp_health.project_directories(Api()) == [str(directory)]


# ------------------------------------------------------------------ CLI ---- #

def _report(server: str, directory: str, state: str = "connected",
            *, configured: bool | None = None,
            reachable: bool = True) -> dict:
    return {"server": server, "directory": directory, "state": state,
            "configured": (state != "") if configured is None else configured,
            "reachable": reachable, "verdict": "ok", "remedy": "",
            "last_startup": {}, "error": ""}


def _args(**overrides) -> argparse.Namespace:
    values = dict(all=True, fix=False, json=True, selftest=False,
                  directory="", server="perun")
    values.update(overrides)
    return argparse.Namespace(**values)


def test_cmd_mcp_health_exits_zero_when_every_location_is_connected(monkeypatch):
    from perun import __main__ as cli

    calls = []
    monkeypatch.setattr(mcp_health, "project_directories", lambda: [r"C:\a", r"C:\b"])
    monkeypatch.setattr(mcp_health, "diagnose",
                        lambda server, directory: calls.append(directory)
                        or _report(server, directory))

    assert cli.cmd_mcp_health(_args()) == 0
    assert calls == [r"C:\a", r"C:\b"]


def test_cmd_mcp_health_repairs_every_broken_location(monkeypatch):
    from perun import __main__ as cli

    states = {r"C:\a": "connected", r"C:\b": "failed"}
    revived = []

    def fake_diagnose(server, directory):
        return _report(server, directory, states[directory])

    def fake_revive(server, directory, **kwargs):
        revived.append(directory)
        states[directory] = "connected"
        return {"ok": True, "state": "connected", "already": False}

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [r"C:\a", r"C:\b"])
    monkeypatch.setattr(mcp_health, "diagnose", fake_diagnose)
    monkeypatch.setattr(mcp_health, "revive", fake_revive)

    assert cli.cmd_mcp_health(_args(fix=True)) == 0
    assert revived == [r"C:\b"]


def test_cmd_mcp_health_fails_when_the_service_is_unreachable(monkeypatch):
    """A service that cannot be asked is not a healthy location."""
    from perun import __main__ as cli

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [r"C:\a"])
    monkeypatch.setattr(mcp_health, "diagnose",
                        lambda server, directory: _report(
                            server, directory, "", configured=False,
                            reachable=False))
    monkeypatch.setattr(mcp_health, "revive",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not revive through a dead service")))

    assert cli.cmd_mcp_health(_args(fix=True)) == 1


def test_cmd_mcp_health_fails_when_there_is_nothing_to_check(monkeypatch):
    from perun import __main__ as cli

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [])

    assert cli.cmd_mcp_health(_args()) == 1


def test_cmd_mcp_health_rechecks_a_location_that_looked_unconfigured(monkeypatch):
    """The first read materializes the location; the second one tells the truth."""
    from perun import __main__ as cli

    reads = []

    def fake_diagnose(server, directory):
        reads.append(directory)
        first = len(reads) == 1
        return _report(server, directory, "" if first else "connected",
                       configured=not first)

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [r"C:\a"])
    monkeypatch.setattr(mcp_health, "diagnose", fake_diagnose)

    assert cli.cmd_mcp_health(_args()) == 0
    assert reads == [r"C:\a", r"C:\a"]


def test_cmd_mcp_health_settles_a_pending_location(monkeypatch):
    from perun import __main__ as cli

    states = iter(["pending", "connected"])
    settled = []

    def fake_diagnose(server, directory):
        return _report(server, directory, next(states, "connected"))

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [r"C:\a"])
    monkeypatch.setattr(mcp_health, "diagnose", fake_diagnose)
    monkeypatch.setattr(mcp_health, "settle",
                        lambda server, directory, wait=0: settled.append(
                            (directory, wait)))

    assert cli.cmd_mcp_health(_args()) == 0
    assert settled == [(r"C:\a", mcp_health.PENDING_SETTLE_SECONDS)]
