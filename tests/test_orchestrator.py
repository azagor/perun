"""Orchestrator unit tests (no real opencode process)."""

from __future__ import annotations

import json
import os

from perun import orchestrator as orch


def test_parse_event_text():
    line = json.dumps({
        "type": "text",
        "sessionID": "ses_1",
        "part": {"type": "text", "text": "hello world"},
    })
    ev = orch.parse_event(line)
    assert ev["kind"] == "text"
    assert ev["text"] == "hello world"
    assert ev["session_id"] == "ses_1"


def test_parse_event_tool():
    line = json.dumps({
        "type": "tool",
        "part": {"type": "tool", "tool": "bash",
                 "state": {"status": "completed", "input": {"command": "ls"}}},
    })
    ev = orch.parse_event(line)
    assert ev["kind"] == "tool"
    assert ev["tool"] == "bash"
    assert ev["status"] == "completed"


def test_parse_event_ignores_non_json():
    assert orch.parse_event("not json") is None
    assert orch.parse_event("") is None


def test_parse_event_error():
    ev = orch.parse_event(json.dumps({"type": "error", "error": {"x": 1}}))
    assert ev["kind"] == "error"


def test_resolve_opencode_returns_list():
    argv = orch.resolve_opencode("definitely-not-a-real-binary-xyz")
    assert isinstance(argv, list) and argv


def test_event_bus_publish_subscribe_replay():
    bus = orch.EventBus()
    bus.publish("run1", {"type": "text", "text": "a"})
    bus.publish("run1", {"type": "text", "text": "b"})
    q = bus.subscribe("run1")  # replay=True default
    got = [q.get_nowait(), q.get_nowait()]
    assert [e["text"] for e in got] == ["a", "b"]
    # live delivery
    bus.publish("run1", {"type": "text", "text": "c"})
    assert q.get_nowait()["text"] == "c"
    bus.unsubscribe("run1", q)
    bus.close("run1")


def test_event_bus_global_subscribers_receive_non_activity():
    bus = orch.EventBus()
    g = bus.subscribe_all()
    bus.publish("activity", {"type": "activity"})   # excluded from globals
    bus.publish("run2", {"type": "text"})
    first = g.get_nowait()
    assert first["type"] == "text"


def test_orchestrator_start_run_rejects_unknown_project():
    from perun import orchestrator

    o = orchestrator.Orchestrator()
    try:
        o.start_run("nope-nope", "hi")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_orchestrator_start_run_rejects_empty_prompt():
    from perun import orchestrator, registry

    registry.add_project("P", r"C:\p")
    o = orchestrator.Orchestrator()
    try:
        o.start_run("p", "   ")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_execute_persists_stdout_error_event(monkeypatch, tmp_path):
    """A provider error emitted only as a stdout event is captured on the run.

    Mirrors a rejected-model run: ``opencode`` prints an ``error`` JSON line to
    stdout and exits non-zero with no text reply. The error must land in the
    run record so the dashboard can explain the failure after the fact.
    """
    import sys

    from perun import orchestrator, registry

    project = registry.add_project("FakeProj", str(tmp_path))
    script = (
        "import json,sys;"
        "print(json.dumps({'type':'error','error':{'message':'provider boom'}}));"
        "sys.exit(1)"
    )
    monkeypatch.setattr(
        orchestrator, "resolve_opencode",
        lambda name: [sys.executable, "-c", script],
    )

    o = orchestrator.Orchestrator()
    res = o.run_and_wait("fakeproj", "hi", timeout=60)
    run = res["run"]
    assert run["status"] == "failed"
    assert "provider boom" in (run.get("error") or "")
    assert run["project_id"] == project["id"]
