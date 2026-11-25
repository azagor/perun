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
    assert status["answered"] is False
    assert "unreachable" in status["error"]


def test_status_separates_an_http_error_from_silence():
    """A 500 is an answer. Only silence means the service is not there."""
    from perun import opencode_api

    class Broken:
        def mcp_servers(self, directory: str = ""):
            raise opencode_api.OpenCodeHTTPError(500, "boom", "/api/mcp")

    status = mcp_health.server_status("perun", r"C:\p", Broken())

    assert status["reachable"] is False   # the connection state is still unknown
    assert status["answered"] is True     # but the service did answer
    assert status["http_status"] == 500
    assert "HTTP 500" in status["error"]


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


def test_diagnose_names_an_error_status_as_an_answer(monkeypatch):
    """It answered with an error: "start the service" would be wrong advice."""
    from perun import opencode_api

    class Broken:
        def mcp_servers(self, directory: str = ""):
            raise opencode_api.OpenCodeHTTPError(503, "unavailable", "/api/mcp")

    monkeypatch.setattr(mcp_health, "read_startup", lambda: {})
    report = mcp_health.diagnose("perun", r"C:\p", Broken())

    assert report["verdict"] == "service_error"
    assert report["answered"] is True
    assert "503" in report["remedy"]
    assert "did not answer" not in report["remedy"]


def test_diagnose_calls_a_configured_row_without_a_state_unknown(monkeypatch):
    """The entry is there; offering to add it again would contradict the read."""
    class Statusless(FakeAPI):
        def mcp_servers(self, directory: str = ""):
            return [{"name": "perun"}]

    monkeypatch.setattr(mcp_health, "read_startup", lambda: {})
    report = mcp_health.diagnose("perun", r"C:\p", Statusless())

    assert report["configured"] is True
    assert report["verdict"] == "unknown_state"
    assert "--fix" in report["remedy"]
    assert "add the `perun` entry" not in report["remedy"]


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


def test_startup_record_does_not_inherit_a_concurrent_process(tmp_path, monkeypatch):
    """Every location starts its own server, and they share this one file."""
    monkeypatch.setattr(mcp_health, "STARTUP_FILE", tmp_path / "mcp-startup.json")

    # Two servers start together; B's import record lands second.
    mcp_health.record_startup({"pid": 111, "total_seconds": 9.9,
                               "db_init_seconds": None, "prepared_at": ""})
    mcp_health.record_startup({"pid": 222, "total_seconds": 1.0,
                               "db_init_seconds": None, "prepared_at": ""})
    # A finishes its schema step and writes again with its own identity.
    mcp_health.record_startup({"pid": 111, "total_seconds": 9.9,
                               "db_init_seconds": 0.4,
                               "db_init_error": "OperationalError: locked",
                               "prepared_at": "2026-11-24T21:00:00"})

    record = mcp_health.read_startup()
    # One run's numbers, not a mixture of both.
    assert record["pid"] == 111
    assert record["total_seconds"] == 9.9
    assert record["db_init_seconds"] == 0.4
    assert record["db_init_error"] == "OperationalError: locked"
    assert record["prepared_at"] == "2026-11-24T21:00:00"


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
            reachable: bool = True, answered: bool | None = None,
            verdict: str = "ok", remedy: str = "", error: str = "") -> dict:
    return {"server": server, "directory": directory, "state": state,
            "configured": (state != "") if configured is None else configured,
            "reachable": reachable,
            "answered": reachable if answered is None else answered,
            "verdict": verdict, "remedy": remedy,
            "last_startup": {}, "error": error}


def _service_knows(monkeypatch, *directories):
    """The pre-read: these are the locations the service already had."""
    monkeypatch.setattr(mcp_health, "service_locations",
                        lambda api=None: list(directories))


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
    _service_knows(monkeypatch, r"C:\a", r"C:\b")

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
    _service_knows(monkeypatch, r"C:\a", r"C:\b")

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
    monkeypatch.setattr(mcp_health, "location_known",
                        lambda directory, api=None: (_ for _ in ()).throw(
                            AssertionError("an unreachable report must not be waited for")))
    _service_knows(monkeypatch)  # unknown, so only the caller's guard can skip it

    assert cli.cmd_mcp_health(_args(fix=True)) == 1


def test_cmd_mcp_health_fails_when_there_is_nothing_to_check(monkeypatch):
    from perun import __main__ as cli

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [])

    assert cli.cmd_mcp_health(_args()) == 1


def test_cmd_mcp_health_rechecks_a_location_that_looked_unconfigured(monkeypatch):
    """The first read materializes the location; a later one tells the truth."""
    from perun import __main__ as cli

    reads = []

    def fake_diagnose(server, directory):
        reads.append(directory)
        first = len(reads) == 1
        return _report(server, directory, "" if first else "connected",
                       configured=not first)

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [r"C:\a"])
    monkeypatch.setattr(mcp_health, "diagnose", fake_diagnose)
    monkeypatch.setattr(mcp_health, "MATERIALIZE_POLL_SECONDS", 0)
    monkeypatch.setattr(mcp_health, "location_known", lambda directory, api=None: False)
    _service_knows(monkeypatch)  # the service had never seen it

    assert cli.cmd_mcp_health(_args()) == 0
    assert reads[0] == r"C:\a"
    assert len(reads) >= 2


def test_cmd_mcp_health_waits_for_a_location_it_just_materialized(monkeypatch, capsys):
    """Measured: absent at t+0, pending at t+2, connected at t+4."""
    from perun import __main__ as cli

    states = [("", False), ("", False), ("pending", True), ("connected", True)]

    def fake_diagnose(server, directory):
        state, configured = states.pop(0) if states else ("connected", True)
        return _report(server, directory, state, configured=configured)

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [r"C:\a"])
    monkeypatch.setattr(mcp_health, "diagnose", fake_diagnose)
    monkeypatch.setattr(mcp_health, "MATERIALIZE_POLL_SECONDS", 0)
    monkeypatch.setattr(mcp_health, "PENDING_SETTLE_SECONDS", 0)
    monkeypatch.setattr(mcp_health, "settle",
                        lambda server, directory, wait=0: None)
    monkeypatch.setattr(mcp_health, "location_known", lambda directory, api=None: False)
    _service_knows(monkeypatch)  # the service had never seen it

    assert cli.cmd_mcp_health(_args()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["state"] == "connected"


def test_cmd_mcp_health_does_not_wait_for_a_location_the_service_already_knew(
        monkeypatch, capsys):
    """The read cannot have created it, so "no server" is the final answer."""
    from perun import __main__ as cli

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [r"C:\a"])
    monkeypatch.setattr(mcp_health, "diagnose",
                        lambda server, directory: _report(
                            server, directory, "", configured=False,
                            verdict="not_configured", remedy="add the entry"))
    monkeypatch.setattr(mcp_health, "location_known",
                        lambda directory, api=None: (_ for _ in ()).throw(
                            AssertionError("a known location must not be waited for")))
    monkeypatch.setattr(mcp_health, "MATERIALIZE_POLL_SECONDS", 0)
    _service_knows(monkeypatch, r"C:\a")

    assert cli.cmd_mcp_health(_args()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["configured"] is False
    assert payload["results"][0]["verdict"] == "not_configured"


def test_cmd_mcp_health_does_not_call_an_uncreated_location_unconfigured(
        monkeypatch, capsys):
    """The service never took the directory: nothing was verified, so it fails."""
    from perun import __main__ as cli

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [r"C:\a"])
    monkeypatch.setattr(mcp_health, "diagnose",
                        lambda server, directory: _report(
                            server, directory, "", configured=False))
    monkeypatch.setattr(mcp_health, "MATERIALIZE_SETTLE_SECONDS", 0)
    monkeypatch.setattr(mcp_health, "location_known", lambda directory, api=None: False)
    _service_knows(monkeypatch)  # the service had never seen it

    assert cli.cmd_mcp_health(_args()) == 1
    payload = json.loads(capsys.readouterr().out)
    report = payload["results"][0]
    assert report["configured"] is False
    assert report["verdict"] == "not_confirmed"
    # The advice must not be "add the entry": the location was never created.
    assert "add the `perun` entry" not in report["remedy"]
    assert "unknown" in report["remedy"]


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
    _service_knows(monkeypatch, r"C:\a")

    assert cli.cmd_mcp_health(_args()) == 0
    assert settled == [(r"C:\a", mcp_health.PENDING_SETTLE_SECONDS)]


def test_cmd_mcp_health_says_why_a_repair_failed(monkeypatch, capsys):
    """A machine-wide repair is useless if it will not say what went wrong."""
    from perun import __main__ as cli

    monkeypatch.setattr(mcp_health, "project_directories",
                        lambda: [r"C:\a", r"C:\b"])
    monkeypatch.setattr(mcp_health, "diagnose",
                        lambda server, directory: _report(server, directory, "failed"))
    monkeypatch.setattr(mcp_health, "revive", lambda server, directory, **kw: {
        "ok": False, "state": "failed", "already": False,
        "error": "HTTP 500 for /api/experimental/mcp/perun/connect"})
    _service_knows(monkeypatch, r"C:\a", r"C:\b")

    assert cli.cmd_mcp_health(_args(fix=True, json=False)) == 1
    out = capsys.readouterr().out
    assert "still down" in out
    assert "HTTP 500" in out


def test_cmd_mcp_health_emits_json_when_there_is_nothing_to_check(monkeypatch, capsys):
    """--json must stay parseable even on the empty answer."""
    from perun import __main__ as cli

    monkeypatch.setattr(mcp_health, "project_directories", lambda: [])

    assert cli.cmd_mcp_health(_args(json=True)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"] == []
    assert "no OpenCode location" in payload["error"]


def test_state_label_distinguishes_an_error_from_silence():
    from perun import __main__ as cli

    silent = _report("perun", r"C:\a", "", configured=False, reachable=False,
                     answered=False)
    answered = {**silent, "answered": True}

    assert cli._mcp_state_label(silent) == "service unreachable"
    assert cli._mcp_state_label(answered) == "service error"


def test_state_label_names_a_refused_location_and_an_unconfirmed_one():
    from perun import __main__ as cli

    refused = _report("perun", r"C:\a", "", configured=False, reachable=False,
                      answered=True, verdict="location_refused")
    unconfirmed = _report("perun", r"C:\a", "", configured=False,
                          verdict="not_confirmed")

    assert cli._mcp_state_label(refused) == "location refused"
    assert cli._mcp_state_label(unconfirmed) == "not confirmed"


def test_cmd_mcp_health_prints_a_remedy_for_a_reachable_failure(
        monkeypatch, capsys):
    """A wedged connect is reachable; "no remedy" left the operator stuck."""
    from perun import __main__ as cli

    failed = "run `perun mcp-health --fix`; if it still fails, restart the editor"
    monkeypatch.setattr(mcp_health, "project_directories",
                        lambda: [r"C:\a", r"C:\b"])
    monkeypatch.setattr(
        mcp_health, "diagnose",
        lambda server, directory: _report(
            server, directory, "connected" if directory == r"C:\a" else "failed",
            verdict="ok" if directory == r"C:\a" else "connect_failed",
            remedy="" if directory == r"C:\a" else failed))
    _service_knows(monkeypatch, r"C:\a", r"C:\b")

    assert cli.cmd_mcp_health(_args(json=False)) == 1
    out = capsys.readouterr().out
    assert "remedy:     " + failed in out


def test_cmd_mcp_health_prints_no_remedy_when_every_location_is_ok(
        monkeypatch, capsys):
    from perun import __main__ as cli

    monkeypatch.setattr(mcp_health, "project_directories",
                        lambda: [r"C:\a", r"C:\b"])
    monkeypatch.setattr(mcp_health, "diagnose",
                        lambda server, directory: _report(server, directory))
    _service_knows(monkeypatch, r"C:\a", r"C:\b")

    assert cli.cmd_mcp_health(_args(json=False)) == 0
    assert "remedy:" not in capsys.readouterr().out


def test_cmd_mcp_health_rejects_a_directory_that_does_not_exist(
        monkeypatch, capsys, tmp_path):
    """A path the service cannot open answers 404; say so before asking."""
    from perun import __main__ as cli

    missing = str(tmp_path / "gone")
    monkeypatch.setattr(mcp_health, "project_directories",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("--directory must not scan every location")))

    assert cli.cmd_mcp_health(_args(all=False, directory=missing, json=True)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"] == []
    assert "no such directory" in payload["error"]
    assert missing in payload["error"]


def test_await_materialized_accepts_an_entry_that_appears_on_the_last_poll(
        monkeypatch):
    """The boundary is the deadline, not the poll count."""
    from perun import __main__ as cli

    clock = iter([0.0, 0.9])  # deadline, then a check still inside the window
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock, 99.0))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(mcp_health, "MATERIALIZE_SETTLE_SECONDS", 1.0)
    monkeypatch.setattr(mcp_health, "MATERIALIZE_POLL_SECONDS", 0)
    monkeypatch.setattr(
        mcp_health, "diagnose",
        lambda server, directory: _report(server, directory, "connected",
                                          configured=True))

    report = cli._await_materialized(
        mcp_health, "perun", _report("perun", r"C:\a", "", configured=False),
        False, known=False)

    assert report["state"] == "connected"
    assert report["verdict"] == "ok"


def test_await_materialized_reports_a_service_that_dies_mid_wait(monkeypatch):
    """A dead service is not an unconfirmed location."""
    from perun import __main__ as cli

    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(mcp_health, "MATERIALIZE_SETTLE_SECONDS", 60.0)
    monkeypatch.setattr(mcp_health, "MATERIALIZE_POLL_SECONDS", 0)
    monkeypatch.setattr(
        mcp_health, "diagnose",
        lambda server, directory: _report(
            server, directory, "", configured=False, reachable=False,
            verdict="service_unreachable", remedy="start the service"))

    report = cli._await_materialized(
        mcp_health, "perun", _report("perun", r"C:\a", "", configured=False),
        False, known=False)

    assert report["verdict"] == "service_unreachable"
    assert report["reachable"] is False


def test_startup_record_retries_a_locked_replacement(tmp_path, monkeypatch):
    """Windows refuses ``os.replace`` while another process reads the file."""
    monkeypatch.setattr(mcp_health, "STARTUP_FILE", tmp_path / "mcp-startup.json")
    monkeypatch.setattr(mcp_health, "REPLACE_RETRY_SECONDS", 0)
    real_replace = os.replace
    attempts = []

    def flaky(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(13, "being used by another process", str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(mcp_health.os, "replace", flaky)

    mcp_health.record_startup({"total_seconds": 2.0, "pid": 7})

    assert len(attempts) == 3
    assert mcp_health.read_startup()["total_seconds"] == 2.0
    assert not list(tmp_path.glob("*.tmp"))


def test_startup_record_leaves_no_temp_file_when_every_replace_fails(
        tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_health, "STARTUP_FILE", tmp_path / "mcp-startup.json")
    monkeypatch.setattr(mcp_health, "REPLACE_RETRY_SECONDS", 0)
    monkeypatch.setattr(mcp_health.os, "replace",
                        lambda src, dst: (_ for _ in ()).throw(
                            PermissionError(13, "locked", str(dst))))

    mcp_health.record_startup({"total_seconds": 2.0})

    assert mcp_health.read_startup() == {}
    assert not list(tmp_path.glob("*.tmp"))


def test_project_directories_dedupes_a_short_and_a_long_path(tmp_path, monkeypatch):
    """The service can list one directory under its 8.3 name and its long name."""
    from perun import registry

    directory = tmp_path / "long_directory_name"
    directory.mkdir()
    short = str(tmp_path / "LONG_DI~1")

    class Api(FakeAPI):
        def locations(self):
            return [str(directory), short]

    real_realpath = os.path.realpath
    monkeypatch.setattr(
        mcp_health.os.path, "realpath",
        lambda path: str(directory) if str(path) == short else real_realpath(path))
    real_isdir = os.path.isdir
    monkeypatch.setattr(
        mcp_health.os.path, "isdir",
        lambda path: True if str(path) == short else real_isdir(path))
    monkeypatch.setattr(registry, "list_projects", lambda: [])

    assert mcp_health.project_directories(Api()) == [str(directory)]


def test_location_known_matches_the_short_and_the_long_form(monkeypatch):
    monkeypatch.setattr(mcp_health, "service_locations",
                        lambda api=None: [r"C:\Users\LSZCZE~1\proj"])

    real_realpath = os.path.realpath
    monkeypatch.setattr(
        mcp_health.os.path, "realpath",
        lambda path: r"C:\Users\lszczepankowski\proj"
        if str(path).lower() == r"c:\users\lszczce~1\proj".lower()
        else real_realpath(path))

    assert mcp_health.location_known(r"C:\Users\lszczepankowski\proj") is True


def test_location_known_answers_false_when_the_list_is_unreadable(monkeypatch):
    def broken(api=None):
        raise RuntimeError("service down")

    monkeypatch.setattr(mcp_health, "service_locations", broken)

    assert mcp_health.location_known(r"C:\p") is False
    assert mcp_health.location_known("") is False


def test_diagnose_calls_a_404_a_refused_location(monkeypatch):
    """A 404 is not "no server configured": the service refused the directory."""
    from perun import opencode_api

    class Refusing:
        def mcp_servers(self, directory: str = ""):
            raise opencode_api.OpenCodeHTTPError(404, "not found", "/api/mcp")

    monkeypatch.setattr(mcp_health, "read_startup", lambda: {})
    report = mcp_health.diagnose("perun", r"C:\gone", Refusing())

    assert report["verdict"] == "location_refused"
    assert report["answered"] is True
    assert "404" in report["remedy"]
    assert "add the `perun` entry" not in report["remedy"]
    assert "did not answer" not in report["remedy"]
