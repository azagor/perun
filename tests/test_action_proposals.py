import json
import time
import threading
from pathlib import Path

import pytest

from perun import action_proposals, db, manifest, registry, shared_projects


def _make_project(tmp_path, name="test-project"):
    (tmp_path / name).mkdir(parents=True)
    project = registry.add_project(name, str(tmp_path / name))
    project["path"] = Path(project["path"])
    return project


def _enable_shared(project, participants=None, deps=None):
    data = manifest.from_project(project)
    data["shared"] = True
    data["participants"] = participants or [{"agent_id": "frambo", "roles": ["rw"]}]
    data["external_deps"] = deps or []
    manifest.save(project["path"], data, body=f"# {project['name']}")


def test_create_proposal(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")

    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "add validation", "targets": [target], "action_type": "edit"},
    )
    assert proposal["status"] == "pending"
    assert proposal["requester"] == "frambo"
    assert proposal["targets"] == [target]


def test_create_proposal_rejects_outside_allowlist(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    outside = str(tmp_path / "evil.py")

    with pytest.raises(ValueError, match="outside the project allowlist"):
        action_proposals.create(
            project,
            "frambo",
            {"intent": "bad", "targets": [outside], "action_type": "edit"},
        )


def test_create_proposal_rejects_read_only_participant(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project, participants=[{"agent_id": "frambo", "roles": ["ro"]}])
    target = str(project["path"] / "api.py")

    with pytest.raises(ValueError, match="read-only access"):
        action_proposals.create(
            project,
            "frambo",
            {"intent": "add validation", "targets": [target], "action_type": "edit"},
        )


def test_create_proposal_allows_rw_participant(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project, participants=[{"agent_id": "frambo", "roles": ["ro", "rw"]}])
    target = str(project["path"] / "api.py")

    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "add validation", "targets": [target], "action_type": "edit"},
    )
    assert proposal["status"] == "pending"


def test_room_owner_may_propose_without_being_a_participant(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project, participants=[{"agent_id": "frambo", "roles": ["rw"]}])
    target = str(project["path"] / "api.py")

    proposal = action_proposals.create(
        project,
        "peruntheoverseer",
        {"intent": "add validation", "targets": [target], "action_type": "edit"},
        owner="peruntheoverseer",
    )
    assert proposal["status"] == "pending"
    assert proposal["requester"] == "peruntheoverseer"

    with pytest.raises(ValueError, match="read-only access"):
        action_proposals.create(
            project,
            "intruder",
            {"intent": "add validation", "targets": [target], "action_type": "edit"},
            owner="peruntheoverseer",
        )


def test_proposal_bounds_chat_supplied_payloads(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")

    proposal = action_proposals.create(
        project,
        "frambo",
        {
            "intent": "x" * (action_proposals.MAX_INTENT_CHARS + 50),
            "targets": [target],
            "action_type": "edit",
        },
    )
    assert len(proposal["intent"]) == action_proposals.MAX_INTENT_CHARS + 1  # + ellipsis
    assert proposal["intent"].endswith("…")

    with pytest.raises(ValueError, match="more than"):
        action_proposals.create(
            project,
            "frambo",
            {
                "intent": "too many",
                "targets": [target] * (action_proposals.MAX_TARGETS + 1),
                "action_type": "edit",
            },
        )

    with pytest.raises(ValueError, match="details exceed"):
        action_proposals.create(
            project,
            "frambo",
            {
                "intent": "too detailed",
                "targets": [target],
                "action_type": "edit",
                "details": {"blob": "x" * (action_proposals.MAX_DETAILS_CHARS + 1)},
            },
        )


def test_proposal_envelope_rejects_malformed_shapes(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")

    with pytest.raises(ValueError, match="targets must be a list"):
        action_proposals.create(
            project, "frambo",
            {"intent": "x", "targets": target, "action_type": "edit"},
        )
    with pytest.raises(ValueError, match="details must be an object"):
        action_proposals.create(
            project, "frambo",
            {"intent": "x", "targets": [target], "action_type": "edit", "details": []},
        )
    with pytest.raises(ValueError, match="control characters"):
        action_proposals.create(
            project, "frambo",
            {"intent": "x", "targets": [target + "\nsecond"], "action_type": "edit"},
        )

    proposal = action_proposals.create(
        project, "frambo",
        {
            "intent": "x",
            "targets": [target],
            "action_type": "edit",
            "manifest_rev": "not-a-number",
        },
    )
    assert proposal["manifest_rev"] >= 1


def test_create_rejects_untrusted_requester_unless_owner(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")

    with pytest.raises(ValueError, match="not on the PerunChat trust list"):
        action_proposals.create(
            project, "frambo",
            {"intent": "x", "targets": [target], "action_type": "edit"},
            trusted=["someone-else"],
        )

    proposal = action_proposals.create(
        project, "perun-local",
        {"intent": "x", "targets": [target], "action_type": "edit"},
        owner="perun-local",
        trusted=[],
    )
    assert proposal["status"] == "pending"


def test_approve_issues_token(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )

    approved = action_proposals.approve(proposal["id"], "owner")
    assert approved["status"] == "approved"
    assert approved["approval_token"]
    assert approved["approval_expiry"]


def test_reject_proposal(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )

    rejected = action_proposals.reject(proposal["id"], "owner", reason="no time")
    assert rejected["status"] == "rejected"
    assert rejected["result"] == "no time"


def test_sensitive_targets_are_rejected(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    for name in (".env", "perun.md", "opencode.json", ".git/config"):
        with pytest.raises(ValueError, match="protected or sensitive"):
            action_proposals.create(
                project,
                "frambo",
                {
                    "intent": "read a secret",
                    "targets": [str(project["path"] / name)],
                    "action_type": "read",
                },
            )


def test_cas_rejects_duplicate_approve_and_reject(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    # Simulate a concurrent reader that still sees "pending" after another
    # approver has already won the CAS. The SQL update must not succeed twice.
    stale = {**action_proposals.get(proposal["id"]), "status": "pending"}
    monkeypatch.setattr(action_proposals, "get", lambda _pid: stale)
    with pytest.raises(ValueError, match="no longer pending"):
        action_proposals.approve(proposal["id"], "second-approver")


def test_state_digest_humanizes_json_errors():
    from perun import action_proposals

    proposal = {
        "id": "prop_1",
        "action_type": "create",
        "status": "failed",
        "targets": ["README.md"],
        "intent": "add readme",
        "result": json.dumps({
            "type": "provider.invalid-request",
            "message": "Upstream request failed: Endpoint is unavailable.",
            "status": 410,
            "response": {"body": "secret details"},
        }),
    }
    digest = action_proposals.state_digest(proposal, {"slug": "demo"})
    assert digest["outcome"] == "failed"
    assert "provider.invalid-request" in digest["notes"]
    assert "Endpoint is unavailable" in digest["notes"]
    assert "secret details" not in digest["notes"]
    assert "status" not in digest["notes"]


def test_retry_creates_new_pending_from_terminal_proposal(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals._set_status(proposal["id"], "failed", "provider error")

    new = action_proposals.retry(proposal["id"], owner="owner", trusted=["frambo"])

    assert new["id"] != proposal["id"]
    assert new["status"] == "pending"
    assert new["intent"] == "x"
    assert new["targets"] == [target]
    assert new["details"].get("retry_of") == proposal["id"]


def test_retry_refuses_non_terminal_proposal(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )

    with pytest.raises(ValueError, match="only terminal proposals"):
        action_proposals.retry(proposal["id"], owner="owner", trusted=["frambo"])

    action_proposals._set_status(proposal["id"], "executing")
    with pytest.raises(ValueError, match="cannot retry"):
        action_proposals.retry(proposal["id"], owner="owner", trusted=["frambo"])


def test_delete_removes_proposal_and_logs_activity(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )

    assert action_proposals.delete(proposal["id"], deleted_by="owner") is True
    assert action_proposals.get(proposal["id"]) is None
    activity = registry.list_activity(project["id"])
    assert any(a["kind"] == "proposal_deleted" for a in activity)


def test_delete_refuses_executing_proposal(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals._set_status(proposal["id"], "executing")

    with pytest.raises(ValueError, match="cannot delete"):
        action_proposals.delete(proposal["id"])


def test_cas_rejects_duplicate_reject(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.reject(proposal["id"], "owner", reason="first")

    stale = {**action_proposals.get(proposal["id"]), "status": "pending"}
    monkeypatch.setattr(action_proposals, "get", lambda _pid: stale)
    with pytest.raises(ValueError, match="no longer pending"):
        action_proposals.reject(proposal["id"], "second-owner", reason="second")


def test_allowlist_env_includes_resolved_targets(tmp_path):
    import json

    project = _make_project(tmp_path)
    _enable_shared(project, deps=[{"path": "../shared-lib", "access": "ro"}])
    target = str(project["path"] / "api.py")

    cfg = json.loads(action_proposals._build_allowlist_env(project, [target]))

    assert cfg["root"] == str(project["path"].resolve())
    assert cfg["targets"] == [str(Path(target).resolve())]
    assert cfg["deps"][0]["access"] == "ro"


def test_execute_approved_proposal(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    mock_orchestrator = type("MockOrchestrator", (), {})()
    # Real Orchestrator runs finalize as "done", not "completed"; this
    # regression test used to hide that mismatch by faking "completed".
    mock_orchestrator.run_and_wait = lambda *a, **kw: {"run": {"status": "done"}, "reply": "done"}
    executed = action_proposals.execute(proposal["id"], orchestrator_instance=mock_orchestrator)

    assert executed["status"] == "executed"
    assert executed["result"] == "done"


def test_start_execution_is_non_blocking_and_single_shot(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    started = threading.Event()
    release = threading.Event()

    class BlockingOrchestrator:
        def run_and_wait(self, *args, **kwargs):
            started.set()
            assert release.wait(5)
            return {
                "run": {"status": "completed", "run_uid": kwargs.get("run_uid")},
                "reply": "done",
            }

    completed = []
    result = action_proposals.start_execution(
        proposal["id"],
        orchestrator_instance=BlockingOrchestrator(),
        on_complete=completed.append,
    )

    # The HTTP caller gets control back immediately, before the agent finishes.
    assert result["status"] == "executing"
    assert started.wait(5)

    # A second click cannot start a second agent.
    with pytest.raises(ValueError, match="proposal is executing"):
        action_proposals.start_execution(
            proposal["id"], orchestrator_instance=BlockingOrchestrator()
        )

    release.set()
    deadline = time.time() + 5
    current = action_proposals.get(proposal["id"])
    while current["status"] == "executing" and time.time() < deadline:
        time.sleep(0.05)
        current = action_proposals.get(proposal["id"])

    assert current["status"] == "executed"
    assert completed and completed[0]["id"] == proposal["id"]


def test_execution_marks_failed_when_audit_finds_a_violation(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    orch = type("OkOrchestrator", (), {})()
    orch.run_and_wait = lambda *a, **kw: {"run": {"status": "completed"}, "reply": "done"}
    monkeypatch.setattr(
        action_proposals.fs_audit,
        "diff",
        lambda project, before: {"violations": ["C:/outside/secret.txt"], "changes": []},
    )
    monkeypatch.setattr(action_proposals.fs_audit, "log_result", lambda *a, **kw: None)

    executed = action_proposals.execute(proposal["id"], orchestrator_instance=orch)

    assert executed["status"] == "failed"
    assert "POLICY VIOLATION" in executed["result"]


def test_execution_marks_failed_when_audit_detects_an_escape(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    orch = type("OkOrchestrator", (), {})()
    orch.run_and_wait = lambda *a, **kw: {"run": {"status": "done"}, "reply": "done"}
    monkeypatch.setattr(
        action_proposals.fs_audit,
        "diff",
        lambda project, before: {
            "violations": [],
            "escaped": [str(Path(project["path"]).parent / "outside.txt")],
        },
    )
    monkeypatch.setattr(action_proposals.fs_audit, "log_result", lambda *a, **kw: None)

    executed = action_proposals.execute(proposal["id"], orchestrator_instance=orch)

    assert executed["status"] == "failed"
    assert "1 escape(s) outside the project root" in executed["result"]


def test_execution_marks_failed_when_audit_cannot_run(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    orch = type("OkOrchestrator", (), {})()
    orch.run_and_wait = lambda *a, **kw: {"run": {"status": "done"}, "reply": "done"}
    monkeypatch.setattr(
        action_proposals.fs_audit,
        "diff",
        lambda project, before: (_ for _ in ()).throw(OSError("audit unavailable")),
    )

    executed = action_proposals.execute(proposal["id"], orchestrator_instance=orch)

    assert executed["status"] == "failed"
    assert "POLICY AUDIT FAILED" in executed["result"]
    assert "audit unavailable" in executed["result"]


def test_execution_preparation_failure_does_not_leave_executing(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    def boom(*_args):
        raise RuntimeError("manifest vanished")

    monkeypatch.setattr(action_proposals, "_build_allowlist_env", boom)

    with pytest.raises(ValueError, match="could not prepare execution"):
        action_proposals.start_execution(
            proposal["id"], orchestrator_instance=type("Never", (), {})()
        )
    assert action_proposals.get(proposal["id"])["status"] == "failed"


def test_orchestrator_construction_failure_marks_failed(tmp_path, monkeypatch):
    from perun import orchestrator as orchestrator_module

    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    class BrokenOrchestrator:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("engine unavailable")

    monkeypatch.setattr(orchestrator_module, "Orchestrator", BrokenOrchestrator)

    with pytest.raises(ValueError, match="could not start orchestrator"):
        action_proposals.start_execution(proposal["id"])
    assert action_proposals.get(proposal["id"])["status"] == "failed"


def test_missing_shared_agent_fails_preparation(tmp_path, monkeypatch):
    from perun import shared_agent

    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    monkeypatch.setattr(shared_agent, "ensure_shared_agent", lambda: shared_agent.agent_path())
    monkeypatch.setattr(shared_agent, "agent_path", lambda: tmp_path / "nonexistent" / "perun-shared.md")

    with pytest.raises(ValueError, match="restricted shared-project agent is missing"):
        action_proposals.start_execution(proposal["id"])
    assert action_proposals.get(proposal["id"])["status"] == "failed"


def test_settings_load_failure_does_not_leave_executing(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    class Engine:
        def validate(self, **kwargs):
            return True, ""

    def boom():
        raise RuntimeError("config unreadable")

    # The token itself is valid; only the post-claim Settings.load() fails.
    monkeypatch.setattr(action_proposals.hitl, "default_engine", lambda: Engine())
    monkeypatch.setattr(action_proposals.config.Settings, "load", staticmethod(boom))

    with pytest.raises(ValueError, match="could not prepare execution"):
        action_proposals.start_execution(
            proposal["id"], orchestrator_instance=type("Never", (), {})()
        )
    assert action_proposals.get(proposal["id"])["status"] == "failed"


def test_reconcile_interrupted_marks_executing_proposals_failed(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    with db.db() as conn:
        conn.execute(
            "UPDATE action_proposals SET status='executing' WHERE id=?", (proposal["id"],)
        )

    recovered = action_proposals.reconcile_interrupted()

    assert [p["id"] for p in recovered] == [proposal["id"]]
    current = action_proposals.get(proposal["id"])
    assert current["status"] == "failed"
    assert "interrupted by a Perun restart" in current["result"]


def test_list_by_project(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "file.txt")
    p1 = action_proposals.create(
        project, "frambo", {"intent": "a", "targets": [target], "action_type": "edit"}
    )
    p2 = action_proposals.create(
        project, "frambo", {"intent": "b", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(p1["id"], "owner")

    all_proposals = action_proposals.list_by_project(project["id"])
    assert len(all_proposals) == 2
    pending = action_proposals.list_by_project(project["id"], status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] == p2["id"]


def test_pending_proposals_expire(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "file.txt")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    with db.db() as conn:
        conn.execute(
            "UPDATE action_proposals SET expires_at=? WHERE id=?",
            (int(time.time()) - 1, proposal["id"]),
        )
    refreshed = action_proposals.get(proposal["id"])
    assert refreshed["status"] == "expired"


def test_auto_approve_does_nothing_when_disabled(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "file.txt")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    monkeypatch.setattr(action_proposals.shared_projects, "is_auto_approve", lambda _path: False)

    started = []
    monkeypatch.setattr(action_proposals, "start_execution", lambda *a, **kw: started.append((a, kw)) or proposal)

    result = action_proposals.auto_approve_and_execute(project, proposal)
    assert result is None
    assert action_proposals.get(proposal["id"])["status"] == "pending"
    assert not started


def test_auto_approve_uses_peer_agent_and_starts_execution(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(
        project,
        participants=[
            {"agent_id": "frambo", "roles": ["rw"]},
            {"agent_id": "glados", "roles": ["rw"]},
        ],
    )
    target = str(project["path"] / "file.txt")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    monkeypatch.setattr(action_proposals.shared_projects, "is_auto_approve", lambda _path: True)

    started = []

    def fake_start(proposal_id, *, orchestrator_instance=None, on_complete=None):
        started.append({"proposal_id": proposal_id, "on_complete": on_complete})
        return action_proposals.get(proposal_id)

    monkeypatch.setattr(action_proposals, "start_execution", fake_start)

    result = action_proposals.auto_approve_and_execute(project, proposal)

    assert result["status"] == "approved"
    assert started
    assert started[0]["proposal_id"] == proposal["id"]
    approved = action_proposals.get(proposal["id"])
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "glados"


def test_auto_approve_falls_back_to_auto_when_no_peer_agent(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project, participants=[{"agent_id": "frambo", "roles": ["rw"]}])
    target = str(project["path"] / "file.txt")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    monkeypatch.setattr(action_proposals.shared_projects, "is_auto_approve", lambda _path: True)

    def fake_start(proposal_id, *, orchestrator_instance=None, on_complete=None):
        return action_proposals.get(proposal_id)

    monkeypatch.setattr(action_proposals, "start_execution", fake_start)

    action_proposals.auto_approve_and_execute(project, proposal)

    approved = action_proposals.get(proposal["id"])
    assert approved["approved_by"] == action_proposals.AUTO_APPROVER


def test_auto_approve_leaves_proposal_approved_if_execution_fails(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project, participants=[{"agent_id": "frambo", "roles": ["rw"]}])
    target = str(project["path"] / "file.txt")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    monkeypatch.setattr(action_proposals.shared_projects, "is_auto_approve", lambda _path: True)

    def fake_start(proposal_id, *, orchestrator_instance=None, on_complete=None):
        raise RuntimeError("orchestrator unavailable")

    monkeypatch.setattr(action_proposals, "start_execution", fake_start)

    with pytest.raises(RuntimeError, match="orchestrator unavailable"):
        action_proposals.auto_approve_and_execute(project, proposal)

    approved = action_proposals.get(proposal["id"])
    assert approved["status"] == "approved"
    assert approved["approved_by"] == action_proposals.AUTO_APPROVER
