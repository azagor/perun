"""Session-centric runs: admission, observation, recovery, leases, delivery."""

from __future__ import annotations

import threading
import time

import pytest

from perun import opencode_api, registry
from perun.orchestrator import Orchestrator
from perun.session_runner import SessionRunner


class FakeAPI:
    """Stands in for the OpenCode service; no HTTP, no discovery."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.prompts: list[dict] = []
        self.interrupts: list[str] = []
        self.active: set[str] = set()
        self.messages_by_session: dict[str, list] = {}
        self.unavailable = False

    def create_session(self, directory, *, model="", agent="", title=""):
        if self.unavailable:
            raise opencode_api.OpenCodeUnavailable("down")
        session = {"id": f"ses_new_{len(self.created) + 1}", "directory": directory}
        self.created.append(session)
        self.active.add(session["id"])
        return session

    def prompt(self, session_id, text, *, delivery="queue", message_id="", resume=True):
        self.prompts.append({"session_id": session_id, "text": text,
                             "delivery": delivery, "message_id": message_id,
                             "resume": resume})
        self.active.add(session_id)
        return {"id": message_id, "delivery": delivery}

    def session_active(self, session_id):
        return session_id in self.active

    def messages(self, session_id, limit=50):
        return list(self.messages_by_session.get(session_id, []))

    def interrupt(self, session_id):
        self.interrupts.append(session_id)
        self.active.discard(session_id)


class FakeHub:
    def __init__(self) -> None:
        self.handlers: dict[str, tuple[str, object]] = {}

    def start(self):
        pass

    def subscribe(self, session_id, handler):
        token = f"tok{len(self.handlers)}"
        self.handlers[token] = (session_id, handler)
        return token

    def unsubscribe(self, token):
        self.handlers.pop(token, None)

    def emit(self, event):
        session_id = (event.get("data") or {}).get("sessionID")
        for sid, handler in list(self.handlers.values()):
            if sid == session_id:
                handler(event)


def _event(kind, session_id, **data):
    return {"type": kind, "data": {"sessionID": session_id, **data}}


@pytest.fixture()
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(opencode_api, "default_api", lambda: fake)
    yield fake


@pytest.fixture()
def hub():
    return FakeHub()


@pytest.fixture()
def orch(control_repo):  # noqa: ARG001 - patches config.REPO_ROOT
    from perun import config

    settings = config.Settings()
    settings.run_timeout = 30
    settings.session_runner_enabled = True
    settings.run_resume_window_minutes = 30
    return Orchestrator(settings)


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "run-proj").mkdir()
    return registry.add_project("run-proj", str(tmp_path / "run-proj"), kind="dir")


def _run(project, prompt="do the thing", uid="run_test"):
    registry.create_run(uid, project["id"], prompt)
    return registry.get_run(uid)


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_execute_admits_once_and_finalizes_from_events(orch, hub, api, project):
    run = _run(project)

    runner = SessionRunner(orch, api=api, hub=hub, owner="test")
    thread = threading.Thread(target=runner.execute,
                              args=(run["run_uid"], project, run["prompt"], run))
    thread.start()

    assert _wait(lambda: bool(api.prompts)), "the prompt was never admitted"
    sid = api.prompts[0]["session_id"]
    assert api.prompts[0]["message_id"] == f"msg_{run['run_uid']}"
    assert api.prompts[0]["delivery"] == "queue"
    assert registry.get_run(run["run_uid"])["admitted_at"]

    hub.emit(_event("session.text.delta", sid, assistantMessageID="m1", delta="OK"))
    hub.emit(_event("session.text.ended", sid, assistantMessageID="m1", text="OK"))
    hub.emit(_event("session.execution.succeeded", sid))
    thread.join(timeout=5)

    final = registry.get_run(run["run_uid"])
    assert final["status"] == "done"
    assert final["output"] == "OK"
    assert final["opencode_session_id"] == sid
    assert registry.run_lease_owned(run["run_uid"], "test", 1) is False


def test_execute_falls_back_before_admission_when_the_service_is_down(orch, hub, api, project):
    api.unavailable = True
    run = _run(project)

    with pytest.raises(opencode_api.OpenCodeUnavailable):
        SessionRunner(orch, api=api, hub=hub, owner="test").execute(
            run["run_uid"], project, run["prompt"], run)

    assert api.prompts == []
    assert registry.get_run(run["run_uid"])["status"] == "queued"


def test_cancel_interrupts_the_session_and_finalizes_cancelled(orch, hub, api, project):
    run = _run(project)
    runner = SessionRunner(orch, api=api, hub=hub, owner="test")
    thread = threading.Thread(target=runner.execute,
                              args=(run["run_uid"], project, run["prompt"], run))
    thread.start()
    assert _wait(lambda: bool(api.prompts))
    sid = api.prompts[0]["session_id"]

    orch.cancel(run["run_uid"])
    hub.emit(_event("session.execution.interrupted", sid))
    thread.join(timeout=5)

    assert sid in api.interrupts
    assert registry.get_run(run["run_uid"])["status"] == "cancelled"


def test_a_silent_session_times_out_and_is_interrupted(orch, hub, api, project):
    orch.settings.run_timeout = 1
    run = _run(project)

    SessionRunner(orch, api=api, hub=hub, owner="test").execute(
        run["run_uid"], project, run["prompt"], run)

    assert api.interrupts
    final = registry.get_run(run["run_uid"])
    assert final["status"] == "timeout"
    assert "exceeded" in (final["error"] or "")


def test_attach_observes_an_already_running_turn(orch, hub, api, project):
    run = _run(project)
    sid = "ses_live"
    registry.update_run(run["run_uid"], status="running",
                        admitted_at=registry.now_iso(), opencode_session_id=sid)
    api.active.add(sid)
    api.messages_by_session[sid] = [
        {"type": "assistant", "parts": [{"type": "text", "text": "partial"}]}
    ]

    run = registry.get_run(run["run_uid"]) or run
    runner = SessionRunner(orch, api=api, hub=hub, owner="test")
    thread = threading.Thread(target=runner.attach, args=(run["run_uid"], project, run))
    thread.start()
    assert _wait(lambda: bool(hub.handlers))
    hub.emit(_event("session.execution.succeeded", sid))
    thread.join(timeout=5)

    final = registry.get_run(run["run_uid"])
    assert final["status"] == "done"
    assert "partial" in final["output"]


def test_reconcile_reattaches_active_and_finalizes_finished(orch, hub, api, project):
    live = _run(project, uid="run_live")
    registry.update_run(live["run_uid"], status="running",
                        admitted_at=registry.now_iso(), opencode_session_id="ses_live")
    api.active.add("ses_live")

    done = _run(project, uid="run_done")
    registry.update_run(done["run_uid"], status="running",
                        admitted_at=registry.now_iso(), opencode_session_id="ses_done")
    api.messages_by_session["ses_done"] = [
        {"type": "assistant", "parts": [{"type": "text", "text": "finished while away"}]}
    ]

    attached: list[str] = []
    orch.attach_session_run = lambda run: attached.append(run["run_uid"]) or True

    result = orch.reconcile_runs()

    assert attached == ["run_live"]
    assert "run_done" in result["finalized"]
    kinds = {row["kind"] for row in registry.list_activity(limit=20)}
    assert "run_reconciled" in kinds
    final = registry.get_run("run_done")
    assert final["status"] == "done"
    assert final["output"] == "finished while away"


def test_reconcile_interrupts_an_admitted_run_without_a_session(orch, api, project):
    run = _run(project)
    registry.update_run(run["run_uid"], status="running", admitted_at=registry.now_iso())

    result = orch.reconcile_runs()

    assert "run_nowhere" not in result["interrupted"]
    final = registry.get_run(run["run_uid"])
    assert final["status"] == "interrupted"


def test_startup_resumes_young_queued_and_interrupts_old(orch, api, project):
    young = _run(project, uid="run_young")
    old = _run(project, uid="run_old")
    with registry.db.db() as conn:
        conn.execute("UPDATE runs SET created_at='2020-01-01T00:00:00+00:00' "
                     "WHERE run_uid='run_old'")

    resumed: list[str] = []
    orch._resume_run = lambda run: resumed.append(run["run_uid"])

    result = orch.reconcile_startup()

    assert resumed == ["run_young"]
    assert registry.get_run("run_old")["status"] == "interrupted"
    assert young["run_uid"] in result["resumed"]


def test_lease_takeover_and_fencing():
    assert registry.claim_run_lease("r1", "a") == 1
    assert registry.claim_run_lease("r1", "a") == 1
    assert registry.claim_run_lease("r1", "b") is None          # live owner
    with registry.db.db() as conn:
        conn.execute("UPDATE run_leases SET beat_epoch=? WHERE run_uid='r1'",
                     (time.time() - 120,))
    assert registry.claim_run_lease("r1", "b") == 2             # stale → takeover
    assert registry.run_lease_owned("r1", "a", 1) is False       # fenced out
    assert registry.run_lease_owned("r1", "b", 2) is True


def test_worker_falls_back_to_the_cli_when_the_service_is_down(orch, api, project, monkeypatch):
    monkeypatch.setenv("PERUN_SESSION_RUNNER", "1")
    api.unavailable = True
    run = _run(project, uid="run_fallback")
    calls: list[str] = []
    monkeypatch.setattr(orch, "_execute", lambda uid, *a, **k: calls.append(uid))

    orch._worker(run["run_uid"], project, run["prompt"], run)

    assert calls == [run["run_uid"]]
    assert registry.get_run(run["run_uid"])["admitted_at"] == ""


def test_a_dead_owner_lease_is_taken_over_immediately():
    import os
    import socket

    host = socket.gethostname()
    assert registry.claim_run_lease("r_dead", f"{host}:999999") == 1
    # The heartbeat is fresh, but that pid is gone: a restart takes over now.
    assert registry.claim_run_lease("r_dead", "newcomer") == 2

    assert registry.claim_run_lease("r_live", f"{host}:{os.getpid()}") == 1
    assert registry.claim_run_lease("r_live", "newcomer") is None


def test_a_lost_lease_never_finalizes_the_run(orch, hub, api, project):
    """Fencing: the old observer must not overwrite the new owner's run."""
    run = _run(project, uid="run_fenced")
    registry.update_run(run["run_uid"], status="running",
                        admitted_at=registry.now_iso(), opencode_session_id="ses_x")
    assert registry.claim_run_lease(run["run_uid"], "old-owner") == 1
    # A restart takes the lease over (its pid is dead).
    with registry.db.db() as conn:
        conn.execute("UPDATE run_leases SET beat_epoch=? WHERE run_uid='run_fenced'",
                     (time.time() - 120,))
    assert registry.claim_run_lease(run["run_uid"], "new-owner") == 2

    SessionRunner(orch, api=api, hub=hub, owner="old-owner")._finish(
        run["run_uid"], "interrupted", "stale", "", lease=1, sid="ses_x")

    assert registry.get_run(run["run_uid"])["status"] == "running"
    assert registry.run_lease_owned(run["run_uid"], "new-owner", 2) is True


def test_an_older_service_without_the_session_api_falls_back(orch, hub, api, project, monkeypatch):
    from perun import opencode_api as real_api

    run = _run(project, uid="run_old_service")

    def no_route(*_a, **_k):
        raise real_api.OpenCodeHTTPError(404, "not found", "/api/session")

    monkeypatch.setattr(api, "create_session", no_route)
    monkeypatch.setenv("PERUN_SESSION_RUNNER", "1")
    calls: list[str] = []
    monkeypatch.setattr(orch, "_execute", lambda uid, *a, **k: calls.append(uid))

    orch._worker(run["run_uid"], project, run["prompt"], run)

    assert calls == [run["run_uid"]]


def test_idle_while_the_session_is_active_does_not_end_the_turn(orch, hub, api, project):
    run = _run(project, uid="run_idle")
    runner = SessionRunner(orch, api=api, hub=hub, owner="test")
    thread = threading.Thread(target=runner.execute,
                              args=(run["run_uid"], project, run["prompt"], run))
    thread.start()
    assert _wait(lambda: bool(api.prompts))
    sid = api.prompts[0]["session_id"]

    hub.emit(_event("session.idle", sid))          # between steps: ignored
    hub.emit(_event("session.text.delta", sid, assistantMessageID="m1", delta="done"))
    hub.emit(_event("session.execution.succeeded", sid))
    thread.join(timeout=5)

    final = registry.get_run(run["run_uid"])
    assert final["status"] == "done"
    assert final["output"] == "done"
