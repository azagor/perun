"""MCP startup must not block the stdio handshake.

OpenCode times out an MCP connect, and the handbook re-index embeds changed docs
with a 180 s request timeout — the log showed a connect failing after ~184 s,
which is exactly that. Indexing therefore runs off the startup path, and only
for the real server (``main()``), never for a module imported by a test.
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
from pathlib import Path

from perun import config, control, handbook

MCP_PATH = Path(config.REPO_ROOT) / "mcp" / "perun_mcp.py"


def _load(monkeypatch):
    started = threading.Event()

    def slow_index():
        started.set()
        time.sleep(6)
        return {}

    monkeypatch.setattr(control, "ensure", lambda: {"ok": True})
    monkeypatch.setattr(handbook, "ensure_indexed", slow_index)
    spec = importlib.util.spec_from_file_location("perun_mcp_startup_probe", MCP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    return spec, module, started


def test_importing_the_server_does_not_index(monkeypatch):
    spec, module, started = _load(monkeypatch)
    began = time.time()
    spec.loader.exec_module(module)
    elapsed = time.time() - began

    assert elapsed < 3.0, f"MCP import blocked for {elapsed:.1f}s"
    # Nothing ran on the import path: tests and the handbook's tool reference
    # import this module and must not leave a thread behind.
    assert not started.is_set()


def test_importing_the_server_leaves_the_tools_callable(monkeypatch):
    """Direct callers import the tool functions; FastMCP wraps them in 2.x."""
    spec, module, _started = _load(monkeypatch)
    spec.loader.exec_module(module)

    assert callable(module.perun_grep)
    assert not hasattr(module.perun_grep, "fn")


def test_main_starts_the_preparation_off_the_critical_path(monkeypatch):
    spec, module, started = _load(monkeypatch)
    spec.loader.exec_module(module)

    # `main()` is the production entry point; it must not wait for the work.
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(module.mcp, "run", fake_run)
    began = time.time()
    module.main()
    elapsed = time.time() - began

    assert calls == [{"show_banner": False}]
    assert elapsed < 1.0
    assert started.wait(5) is True


def test_startup_never_waits_for_the_database(monkeypatch):
    """The schema work is the only step that can block on another process."""
    spec, module, _started = _load(monkeypatch)
    spec.loader.exec_module(module)

    release = threading.Event()
    calls = []
    monkeypatch.setattr(module.db, "init_db", lambda: release.wait(10))
    monkeypatch.setattr(module.mcp, "run", lambda **kw: calls.append("run"))
    module._DB_READY.clear()
    began = time.time()
    module.main()
    elapsed = time.time() - began

    # `run` (the handshake) is entered while the schema work is still blocked.
    assert calls == ["run"]
    assert elapsed < 1.0, f"MCP start waited {elapsed:.1f}s for the database"
    release.set()


def test_the_handshake_path_has_no_network_version_check(monkeypatch):
    """FastMCP's banner queries PyPI; that call has no business in a start."""
    spec, module, _started = _load(monkeypatch)
    spec.loader.exec_module(module)

    assert os.environ.get("FASTMCP_CHECK_FOR_UPDATES") == "off"
    assert os.environ.get("FASTMCP_SHOW_CLI_BANNER") == "false"
    assert module._STARTUP_BEGAN > 0


def test_the_startup_record_is_written_off_the_handshake_path(monkeypatch):
    """Recording costs disk I/O; the handshake must not wait for it."""
    spec, module, _started = _load(monkeypatch)
    spec.loader.exec_module(module)

    written = threading.Event()
    threads = []

    def fake_record(payload):
        threads.append(threading.current_thread())
        written.set()

    monkeypatch.setattr(module.mcp_health, "record_startup", fake_record)
    monkeypatch.setattr(module.db, "init_db", lambda: None)
    monkeypatch.setattr(module.mcp, "run", lambda **kw: None)
    module._DB_READY.clear()

    module.main()

    assert written.wait(5) is True
    assert threads, "the start recorded nothing"
    assert all(t is not threading.main_thread() for t in threads), (
        "the startup record was written before the handshake")


def _fast_prepare(module, monkeypatch):
    """Keep the preparation thread's unrelated work out of these tests."""
    monkeypatch.setattr(module.handbook, "ensure_indexed", lambda: {})
    monkeypatch.setattr(module.control, "ensure", lambda: {"ok": True})


def test_a_transient_schema_failure_is_retried(monkeypatch):
    """A lock holder at start must not leave this process on an old schema."""
    spec, module, _started = _load(monkeypatch)
    spec.loader.exec_module(module)
    _fast_prepare(module, monkeypatch)

    attempts = []
    records = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("database is locked")

    monkeypatch.setattr(module.db, "init_db", flaky)
    monkeypatch.setattr(module.mcp_health, "record_startup", records.append)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    module._DB_READY.clear()

    module._prepare_in_background(module._STARTUP_BEGAN + 3.0)

    assert len(attempts) == 3
    assert records[-1]["db_init_error"] == ""
    assert module._DB_READY.is_set()


def test_a_persistent_schema_failure_is_recorded(monkeypatch):
    spec, module, _started = _load(monkeypatch)
    spec.loader.exec_module(module)
    _fast_prepare(module, monkeypatch)

    attempts = []
    records = []

    def locked():
        attempts.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(module.db, "init_db", locked)
    monkeypatch.setattr(module.mcp_health, "record_startup", records.append)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    module._DB_READY.clear()

    module._prepare_in_background(module._STARTUP_BEGAN + 3.0)

    assert len(attempts) == module._SCHEMA_ATTEMPTS
    assert "database is locked" in records[-1]["db_init_error"]
    # The import cost is recorded before the schema work, so it survives.
    assert records[0]["total_seconds"] == 3.0


def test_both_startup_writes_carry_the_run_identity(monkeypatch):
    """The record is shared by every location's server; each write must say whose."""
    spec, module, _started = _load(monkeypatch)
    spec.loader.exec_module(module)
    _fast_prepare(module, monkeypatch)

    records = []
    monkeypatch.setattr(module.db, "init_db", lambda: None)
    monkeypatch.setattr(module.mcp_health, "record_startup", records.append)
    module._DB_READY.clear()

    module._prepare_in_background(module._STARTUP_BEGAN + 2.0)

    assert len(records) == 2
    for payload in records:
        # Without the pid the second write merges into another server's record.
        assert payload["pid"] == os.getpid()
        assert payload["total_seconds"] == 2.0
