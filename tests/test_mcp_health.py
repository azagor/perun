"""The MCP health surface: reported state, measured startup, and repair.

OpenCode keeps the MCP connection state per location and caches a failure for
the whole session; a connect that wedges in ``pending`` stays wedged until the
client is explicitly disconnected. These tests pin the reads and the repair
sequence, and the fact that the measured startup is what gets reported.
"""

from __future__ import annotations

import argparse
import json

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


def test_status_reads_the_state_the_service_reports():
    status = mcp_health.server_status("perun", r"C:\p", FakeAPI(state="failed"))

    assert status["state"] == "failed"
    assert status["error"] == "Request timed out"
    assert status["configured"] is True


def test_status_reports_an_unconfigured_server():
    class Empty(FakeAPI):
        def mcp_servers(self, directory: str = ""):
            return []

    status = mcp_health.server_status("perun", r"C:\p", Empty())

    assert status["state"] == ""
    assert status["configured"] is False


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


def test_project_directories_merges_locations_and_registry(monkeypatch):
    """One connection per location, so one repair per location."""
    from perun import registry

    class Api(FakeAPI):
        def locations(self):
            return [r"C:\a", r"C:\b", r"C:\A"]

    monkeypatch.setattr(registry, "list_projects", lambda: [
        {"path": r"C:\b"}, {"path": r"C:\c"}, {"path": ""},
    ])

    assert mcp_health.project_directories(Api()) == [r"C:\a", r"C:\b", r"C:\c"]


def test_project_directories_survives_an_unreachable_service(monkeypatch):
    from perun import registry

    class Api(FakeAPI):
        def locations(self):
            raise RuntimeError("service down")

    monkeypatch.setattr(registry, "list_projects", lambda: [{"path": r"C:\c"}])

    assert mcp_health.project_directories(Api()) == [r"C:\c"]


def _healthy(server: str, directory: str, state: str = "connected") -> dict:
    return {"server": server, "directory": directory, "state": state,
            "configured": state != "", "verdict": "ok", "remedy": "",
            "last_startup": {}, "error": ""}


def test_cmd_mcp_health_exits_zero_when_every_location_is_connected(monkeypatch):
    from perun import __main__ as cli

    calls = []
    monkeypatch.setattr(mcp_health, "project_directories", lambda: [r"C:\a", r"C:\b"])
    monkeypatch.setattr(mcp_health, "diagnose",
                        lambda server, directory: calls.append(directory)
                        or _healthy(server, directory))
    args = argparse.Namespace(all=True, fix=False, json=True, selftest=False,
                              directory="", server="perun")

    assert cli.cmd_mcp_health(args) == 0
    assert calls == [r"C:\a", r"C:\b"]


def test_cmd_mcp_health_repairs_every_broken_location(monkeypatch):
    from perun import __main__ as cli

    states = {r"C:\a": "connected", r"C:\b": "failed"}
    revived = []

    def fake_diagnose(server, directory):
        return _healthy(server, directory, states[directory])

    def fake_revive(server, directory, **kwargs):
        revived.append(directory)
        states[directory] = "connected"
        return {"ok": True, "state": "connected", "already": False}

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [r"C:\a", r"C:\b"])
    monkeypatch.setattr(mcp_health, "diagnose", fake_diagnose)
    monkeypatch.setattr(mcp_health, "revive", fake_revive)
    args = argparse.Namespace(all=True, fix=True, json=True, selftest=False,
                              directory="", server="perun")

    assert cli.cmd_mcp_health(args) == 0
    assert revived == [r"C:\b"]
