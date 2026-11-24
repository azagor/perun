"""MCP startup must not block the stdio handshake.

OpenCode times out an MCP connect, and the handbook re-index embeds changed docs
with a 180 s request timeout — the log showed a connect failing after ~184 s,
which is exactly that. Indexing therefore runs off the startup path, and only
for the real server (``main()``), never for a module imported by a test.
"""

from __future__ import annotations

import importlib.util
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
    monkeypatch.setattr(module.mcp, "run", lambda: calls.append("run"))
    began = time.time()
    module.main()
    elapsed = time.time() - began

    assert calls == ["run"]
    assert elapsed < 1.0
    assert started.wait(5) is True
