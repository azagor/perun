"""MCP server discovery and the on-demand live probe.

These exercise :mod:`perun.mcp_servers` directly (no HTTP): config parsing across
global + project scopes, status derivation, and the stdio handshake against a
throwaway MCP server we spawn from a temp script.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.fixture()
def opencode_dir(monkeypatch, tmp_path):
    """Redirect the global OpenCode config dir into a temp folder."""
    from perun import opencode_config

    cfg = tmp_path / "opencode"
    cfg.mkdir()
    monkeypatch.setattr(opencode_config, "_config_file", lambda: cfg / "opencode.json")
    return cfg
def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_snapshot_reads_global_and_project_scopes(opencode_dir, tmp_path):
    from perun import mcp_servers, opencode_config

    _write(opencode_dir / "opencode.json", {"mcp": {
        "perun": {"type": "local", "command": [sys.executable, "server.py"], "enabled": True},
        "docs": {"type": "remote", "url": "https://example.test/mcp"},
        "off": {"type": "local", "command": [sys.executable], "enabled": False},
    }})
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(proj / "opencode.json", {"mcp": {
        "perun": {"type": "local", "command": [sys.executable, "other.py"], "enabled": True},
        "extra": {"type": "local", "command": [sys.executable, "-c", "print()"]},
    }})

    snap = mcp_servers.snapshot([{"slug": "proj", "name": "Proj", "path": str(proj)}])
    by = {(s["name"], s["scope"]): s for s in snap["servers"]}

    assert set(by) == {("perun", "global"), ("docs", "global"), ("off", "global"),
                       ("perun", "project"), ("extra", "project")}
    assert by[("docs", "global")]["status"] == "remote"
    assert by[("off", "global")]["status"] == "disabled"
    assert by[("perun", "global")]["status"] == "ready"
    assert by[("perun", "global")]["resolved"]          # sys.executable is on PATH
    assert by[("perun", "global")]["shadowed_by"] == ["proj"]
    assert by[("extra", "project")]["project"] == "proj"
    assert snap["count"] == 5
    assert snap["counts"]["ready"] == 3  # global perun + project perun + extra


def test_snapshot_reads_mcp_servers_from_windows_appdata_dir(monkeypatch, tmp_path):
    """On Windows, global MCP servers in APPDATA\\opencode are discovered too."""
    from perun import mcp_servers, opencode_config

    primary = tmp_path / "primary" / "opencode"
    primary.mkdir(parents=True)
    appdata = tmp_path / "appdata" / "opencode"
    appdata.mkdir(parents=True)

    _write(primary / "opencode.json", {"mcp": {"primary-server": {"type": "local", "command": [sys.executable, "-c", "print()"]}}})
    _write(appdata / "opencode.json", {"mcp": {"tui-server": {"type": "local", "command": [sys.executable, "-c", "print()"]}}})

    monkeypatch.setattr(opencode_config, "_config_file", lambda: primary / "opencode.json")
    monkeypatch.setattr(opencode_config.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    snap = mcp_servers.snapshot([])
    names = {s["name"] for s in snap["servers"]}
    assert "primary-server" in names
    assert "tui-server" in names


def test_snapshot_marks_missing_executable_and_parses_jsonc(opencode_dir):
    from perun import mcp_servers

    (opencode_dir / "opencode.jsonc").write_text(
        '{\n  // jsonc comment\n  "mcp": { "x": { "type": "local", '
        '"command": ["definitely-missing-bin-xyz"] } }\n}\n',
        encoding="utf-8",
    )
    snap = mcp_servers.snapshot([])
    entry = snap["servers"][0]
    assert entry["name"] == "x"
    assert entry["status"] == "missing"
    assert entry["resolved"] is None


def test_snapshot_empty_config(opencode_dir):
    from perun import mcp_servers

    snap = mcp_servers.snapshot([])
    assert snap["servers"] == []
    assert snap["count"] == 0


FAKE_SERVER = textwrap.dedent(
    """
    import json, sys
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        if method == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "9.9"}}}), flush=True)
        elif method == "tools/list":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "tools": [{"name": "alpha"}, {"name": "beta"}]}}), flush=True)
    """
).strip()


def test_probe_local_stdio_handshake(opencode_dir, tmp_path):
    from perun import mcp_servers

    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    _write(opencode_dir / "opencode.json", {"mcp": {"fake": {
        "type": "local", "command": [sys.executable, str(script)], "enabled": True}}})

    res = mcp_servers.probe("fake", timeout=15)
    assert res["ok"] is True
    assert res["status"] == "ok"
    assert res["tools"] == ["alpha", "beta"]
    assert res["tool_count"] == 2
    assert res["server_info"] == {"name": "fake", "version": "9.9"}
    assert res["latency_ms"] >= 0


def test_probe_missing_binary(opencode_dir):
    from perun import mcp_servers

    _write(opencode_dir / "opencode.json", {"mcp": {"gone": {
        "type": "local", "command": ["no-such-binary-zzz"], "enabled": True}}})
    res = mcp_servers.probe("gone", timeout=5)
    assert res["ok"] is False
    assert res["status"] == "missing"
    assert "not found on PATH" in res["error"]


def test_probe_times_out_on_silent_server(opencode_dir, tmp_path):
    from perun import mcp_servers

    script = tmp_path / "sleeper.py"
    script.write_text("import time\nwhile True: time.sleep(1)\n", encoding="utf-8")
    _write(opencode_dir / "opencode.json", {"mcp": {"sleepy": {
        "type": "local", "command": [sys.executable, str(script)], "enabled": True}}})

    res = mcp_servers.probe("sleepy", timeout=2)
    assert res["ok"] is False
    assert res["status"] == "unreachable"


def test_probe_unknown_server_raises(opencode_dir):
    from perun import mcp_servers

    _write(opencode_dir / "opencode.json", {"mcp": {}})
    with pytest.raises(LookupError):
        mcp_servers.probe("nope")


def test_perun_mcp_server_suppresses_startup_banner():
    """The real perun MCP server must not print a banner before JSON-RPC."""
    import subprocess

    server_path = Path(__file__).resolve().parent.parent / "mcp" / "perun_mcp.py"
    init = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    })
    proc = subprocess.Popen(
        [sys.executable, str(server_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(input=init + "\n", timeout=15)
        first_line = out.splitlines()[0] if out.strip() else ""
        msg = json.loads(first_line)
        assert msg["jsonrpc"] == "2.0"
        assert msg["id"] == 1
        assert msg["result"]["serverInfo"]["name"] == "Perun"
    finally:
        if proc.poll() is None:
            proc.kill()


# --------------------------------------------------------------------------- #
# HTTP endpoints
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as c:
        yield c


def test_mcp_servers_endpoint_lists_config(client, opencode_dir):
    _write(opencode_dir / "opencode.json", {"mcp": {
        "perun": {"type": "local", "command": [sys.executable], "enabled": True}}})

    r = client.get("/api/mcp/servers")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert any(s["name"] == "perun" and s["scope"] == "global" for s in body["servers"])


def test_mcp_probe_endpoint_runs_handshake(client, opencode_dir, tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    _write(opencode_dir / "opencode.json", {"mcp": {"fake": {
        "type": "local", "command": [sys.executable, str(script)], "enabled": True}}})

    r = client.post("/api/mcp/servers/probe", json={"name": "fake", "scope": "global"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["tools"] == ["alpha", "beta"]


def test_mcp_probe_endpoint_validates_and_404s(client, opencode_dir):
    _write(opencode_dir / "opencode.json", {"mcp": {}})
    assert client.post("/api/mcp/servers/probe", json={}).status_code == 400
    assert client.post("/api/mcp/servers/probe",
                       json={"name": "nope", "scope": "global"}).status_code == 404
    assert client.post("/api/mcp/servers/probe",
                       json={"name": "x", "scope": "project"}).status_code == 400


# --------------------------------------------------------------------------- #
# Hardening: dedupe, tolerant parsing, gate, platform shims
# --------------------------------------------------------------------------- #
def test_snapshot_dedupes_across_config_file_variants(opencode_dir):
    """A name defined in both .json and .jsonc must list once, not twice."""
    from perun import mcp_servers

    body = {"mcp": {"perun": {"type": "local", "command": [sys.executable], "enabled": True}}}
    _write(opencode_dir / "opencode.json", body)
    _write(opencode_dir / "opencode.jsonc", body)

    snap = mcp_servers.snapshot([])
    assert [s["name"] for s in snap["servers"]] == ["perun"]
    assert snap["count"] == 1


def test_snapshot_dedupes_duplicate_project_paths(opencode_dir, tmp_path):
    from perun import mcp_servers

    proj = tmp_path / "proj"
    proj.mkdir()
    _write(proj / "opencode.json", {"mcp": {"x": {"type": "local", "command": [sys.executable]}}})

    snap = mcp_servers.snapshot([
        {"slug": "a", "name": "A", "path": str(proj)},
        {"slug": "a", "name": "A", "path": str(proj)},
    ])
    assert snap["count"] == 1


def test_snapshot_surfaces_config_parse_errors(opencode_dir):
    from perun import mcp_servers

    (opencode_dir / "opencode.json").write_text("{ definitely not json", encoding="utf-8")
    snap = mcp_servers.snapshot([])
    assert snap["servers"] == []
    assert len(snap["parse_errors"]) == 1
    assert "invalid JSON" in snap["parse_errors"][0]["error"]
    assert snap["parse_errors"][0]["source"].endswith("opencode.json")


def test_snapshot_accepts_jsonc_trailing_commas(opencode_dir):
    from perun import mcp_servers

    (opencode_dir / "opencode.jsonc").write_text(
        '{\n  // comment with a comma,\n  "mcp": {\n'
        '    "x": { "type": "remote", "url": "https://example.test/mcp", },\n  },\n}\n',
        encoding="utf-8",
    )
    snap = mcp_servers.snapshot([])
    assert snap["parse_errors"] == []
    assert [s["name"] for s in snap["servers"]] == ["x"]


def test_probe_reports_busy_when_gate_saturated(opencode_dir, monkeypatch):
    import threading

    from perun import mcp_servers

    _write(opencode_dir / "opencode.json", {"mcp": {"one": {
        "type": "local", "command": [sys.executable], "enabled": True}}})
    gate = threading.BoundedSemaphore(1)
    assert gate.acquire(blocking=False)
    monkeypatch.setattr(mcp_servers, "_PROBE_GATE", gate)
    monkeypatch.setattr(mcp_servers, "_GATE_WAIT", 0.0)
    try:
        res = mcp_servers.probe("one", timeout=5)
    finally:
        gate.release()
    assert res["ok"] is False
    assert res["status"] == "busy"


def test_probe_timeout_is_capped(opencode_dir, monkeypatch):
    from perun import mcp_servers

    _write(opencode_dir / "opencode.json", {"mcp": {"x": {
        "type": "local", "command": [sys.executable], "enabled": True}}})
    seen: dict = {}
    monkeypatch.setattr(
        mcp_servers, "_probe_stdio",
        lambda entry, timeout: (seen.update(t=timeout) or {"status": "ok", "ok": True, "tools": []}),
    )
    res = mcp_servers.probe("x", timeout=99999)
    assert seen["t"] == mcp_servers.MAX_PROBE_TIMEOUT
    assert res["ok"] is True


def test_windows_launch_argv_wraps_shims():
    from perun import mcp_servers

    assert mcp_servers.windows_launch_argv(
        ["npm.cmd", "-y", "x"], r"C:\n\npm.cmd", os_name="nt",
    ) == ["cmd", "/c", "npm.cmd", "-y", "x"]
    assert mcp_servers.windows_launch_argv(
        ["dep.ps1"], r"C:\d\dep.ps1", os_name="nt",
    ) == ["powershell", "-NoProfile", "-File", r"C:\d\dep.ps1"]
    assert mcp_servers.windows_launch_argv(
        ["tool.exe"], r"C:\t\tool.exe", os_name="nt",
    ) == ["tool.exe"]
    assert mcp_servers.windows_launch_argv(
        ["npm.cmd"], r"/usr/bin/npm.cmd", os_name="posix",
    ) == ["npm.cmd"]
