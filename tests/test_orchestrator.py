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


def test_orchestrator_start_run_uses_default_prompt_when_empty(monkeypatch):
    from perun import orchestrator, registry

    project = registry.add_project("P", r"C:\p")
    o = orchestrator.Orchestrator()
    # Do not actually spawn an opencode process.
    monkeypatch.setattr(o, "_worker", lambda *args, **kwargs: None)
    run = o.start_run("p", "   ")
    assert run["project_id"] == project["id"]
    assert run["prompt"] == orch.DEFAULT_SESSION_PROMPT
    assert run["model"] == ""  # no explicit/project/global model was forced
    assert run["model_explicit"] == 0


def test_orchestrator_global_model_not_forced_on_projects(monkeypatch):
    from perun import orchestrator, registry, config

    project = registry.add_project("P2", r"C:\p2")
    settings = config.Settings.load()
    settings.default_model = "provider/global-model"
    o = orchestrator.Orchestrator(settings)
    monkeypatch.setattr(o, "_worker", lambda *args, **kwargs: None)
    run = o.start_run("p2", "hi")
    assert run["model"] == ""  # OpenCode's own default should be used
    assert run["model_explicit"] == 0


def test_orchestrator_explicit_model_is_passed(monkeypatch):
    from perun import orchestrator, registry

    registry.add_project("P3", r"C:\p3")
    o = orchestrator.Orchestrator()
    monkeypatch.setattr(o, "_worker", lambda *args, **kwargs: None)
    run = o.start_run("p3", "hi", model="provider/explicit")
    assert run["model"] == "provider/explicit"
    assert run["model_explicit"] == 1


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
    # Without a session id this is a genuine failed run.
    assert run["status"] == "failed"
    assert "provider boom" in (run.get("error") or "")
    assert run["project_id"] == project["id"]


def test_provider_error_keeps_session_active(monkeypatch, tmp_path):
    """A provider error after session creation marks provider_error, not failed."""
    import sys

    from perun import orchestrator, registry

    project = registry.add_project("ProvErr", str(tmp_path))
    script = (
        "import json,sys;"
        "print(json.dumps({'type':'session','session':{'id':'ses_123'}}));"
        "print(json.dumps({'type':'provider.quota','error':{'message':'provider quota'},'sessionID':'ses_123'}));"
        "sys.exit(1)"
    )
    monkeypatch.setattr(
        orchestrator, "resolve_opencode",
        lambda name: [sys.executable, "-c", script],
    )

    o = orchestrator.Orchestrator()
    res = o.run_and_wait("proverr", "hi", timeout=60)
    run = res["run"]
    assert run["status"] == "provider_error"
    assert run["opencode_session_id"] == "ses_123"
    assert "provider quota" in (run.get("error") or "")
    # The session row should be active so the user can continue it.
    session = registry.get_session(run["session_pk"])
    assert session is not None
    assert session["status"] == "active"


def test_compose_prompt_injects_instructions_on_new_session():
    project = {"instructions": "Always use tabs."}
    out = orch.compose_prompt(project, "Do it", new_session=True)
    assert "[PERUN PROJECT INSTRUCTIONS]" in out
    assert "[END PERUN PROJECT INSTRUCTIONS]" in out
    assert "Always use tabs." in out
    assert out.endswith("Do it")


def test_compose_prompt_skips_when_continuing_or_empty():
    project = {"instructions": "Rules."}
    assert orch.compose_prompt(project, "hi", new_session=False) == "hi"
    assert orch.compose_prompt({"instructions": "  "}, "hi", new_session=True) == "hi"
    assert orch.compose_prompt({}, "hi", new_session=True) == "hi"


def test_execute_injects_project_instructions(monkeypatch, tmp_path):
    """A new session's prompt carries the project instructions (raw prompt is stored)."""
    import sys

    from perun import orchestrator, registry

    registry.add_project("Instr", str(tmp_path), instructions="PROJECT RULE 42")
    # The fake "opencode" echoes its final argv element (the composed prompt).
    script = (
        "import json,sys;"
        "print(json.dumps({'type':'text','sessionID':'ses_x',"
        "'part':{'type':'text','text':sys.argv[-1]}}))"
    )
    monkeypatch.setattr(orchestrator, "resolve_opencode",
                        lambda name: [sys.executable, "-c", script])

    o = orchestrator.Orchestrator()
    res = o.run_and_wait("instr", "the task", timeout=60)
    # Storage keeps the user's raw prompt…
    assert res["run"]["prompt"] == "the task"
    # …but the agent received the project-scoped instructions.
    assert "PROJECT RULE 42" in (res["reply"] or "")
    assert "[PERUN PROJECT INSTRUCTIONS]" in (res["reply"] or "")
