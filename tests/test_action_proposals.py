import json
import time
import threading
from pathlib import Path

import pytest

from perun import action_proposals, db, manifest, projects, registry, shared_projects


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
    # No bound chat row: the record stays empty rather than failing the write.
    assert proposal["room_id"] == ""


def test_create_proposal_binds_bound_room_when_envelope_omits_it(tmp_path):
    """Dashboard/API proposals must still record the project chat room."""
    project = _make_project(tmp_path)
    _enable_shared(project)
    room_id = "grp:perun:test-project:chat"
    registry.set_project_chat(project["id"], room_id, "owner", "active", 1)
    target = str(project["path"] / "api.py")

    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "add validation", "targets": [target], "action_type": "edit"},
    )

    assert proposal["room_id"] == room_id
    assert action_proposals.get(proposal["id"])["room_id"] == room_id


def test_create_proposal_prefers_envelope_room_over_bound_room(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    registry.set_project_chat(
        project["id"], "grp:perun:test-project:chat", "owner", "active", 1
    )
    target = str(project["path"] / "api.py")

    proposal = action_proposals.create(
        project,
        "frambo",
        {
            "intent": "add validation",
            "targets": [target],
            "action_type": "edit",
            "room_id": "grp:perun:explicit:chat",
        },
    )

    assert proposal["room_id"] == "grp:perun:explicit:chat"


def test_self_approval_is_blocked_in_core(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo",
        {"intent": "x", "targets": [target], "action_type": "edit"},
    )

    with pytest.raises(ValueError, match="cannot be approved by its requester"):
        action_proposals.approve(proposal["id"], "  Frambo  ")
    with pytest.raises(ValueError, match="cannot be rejected by its requester"):
        action_proposals.reject(proposal["id"], "frambo")

    approved = action_proposals.approve(proposal["id"], "szczepan")
    assert approved["approved_by"] == "szczepan"


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


def test_retry_binds_legacy_proposal_to_current_room(tmp_path):
    """A retried record with an empty room_id inherits the bound chat room."""
    project = _make_project(tmp_path)
    _enable_shared(project)
    room_id = "grp:perun:test-project:chat"
    registry.set_project_chat(project["id"], room_id, "owner", "active", 1)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    # Simulate a record created before the room fallback existed.
    with db.db() as conn:
        conn.execute(
            "UPDATE action_proposals SET room_id='' WHERE id=?", (proposal["id"],)
        )
    action_proposals._set_status(proposal["id"], "failed", "provider error")

    new = action_proposals.retry(proposal["id"], owner="owner", trusted=["frambo"])

    assert new["room_id"] == room_id


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
    target = project["path"] / "api.py"
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [str(target)], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    mock_orchestrator = type("MockOrchestrator", (), {})()
    # Real Orchestrator runs finalize as "done", not "completed"; this
    # regression test used to hide that mismatch by faking "completed".
    def run_and_wait(*a, **kw):
        # A real run changes the approved target; the post-run outcome check
        # requires it before reporting "executed".
        target.write_text("x = 1\n", encoding="utf-8")
        return {"run": {"status": "done"}, "reply": "done"}

    mock_orchestrator.run_and_wait = run_and_wait
    executed = action_proposals.execute(proposal["id"], orchestrator_instance=mock_orchestrator)

    assert executed["status"] == "executed"
    assert executed["result"] == "done"
    assert executed["execution_mode"] == "perun"
    assert executed["executed_by"] == action_proposals.LOCAL_EXECUTOR


def test_execute_marks_failed_when_no_target_changed(tmp_path):
    """A no-op run must never be recorded as executed (prop_bd0bf5002f60)."""
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = project["path"] / "api.py"
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [str(target)], "action_type": "create"}
    )
    action_proposals.approve(proposal["id"], "owner")

    orch = type("NoopOrchestrator", (), {})()
    orch.run_and_wait = lambda *a, **kw: {"run": {"status": "done"}, "reply": "done"}

    executed = action_proposals.execute(proposal["id"], orchestrator_instance=orch)

    assert executed["status"] == "failed"
    assert "NO TARGET CHANGES" in executed["result"]
    assert not target.exists()


def test_execution_passes_prompt_as_file(tmp_path):
    """The proposal prompt can exceed the Windows command-line limit."""
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = project["path"] / "api.py"
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [str(target)], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    seen: dict = {}

    class RecordingOrchestrator:
        def run_and_wait(self, *args, **kwargs):
            seen.update(kwargs)
            target.write_text("x = 1\n", encoding="utf-8")
            return {"run": {"status": "done"}, "reply": "done"}

    action_proposals.execute(proposal["id"], orchestrator_instance=RecordingOrchestrator())

    assert seen.get("use_prompt_file") is True


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
            target_path = Path(target)
            target_path.write_text("x = 1\n", encoding="utf-8")
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
    # The status is persisted before the completion callback runs, so wait for
    # both to avoid a race between the final status and the callback.
    while (current["status"] == "executing" or not completed) and time.time() < deadline:
        time.sleep(0.05)
        current = action_proposals.get(proposal["id"])

    assert current["status"] == "executed"
    assert completed and completed[0]["id"] == proposal["id"]


def test_prepare_execution_fails_when_guard_mcp_missing(tmp_path, monkeypatch):
    """An approved run must not burn tokens without the guard server."""
    project = _make_project(tmp_path)
    _enable_shared(project)
    (project["path"] / "opencode.json").write_text('{"mcp": {}}', encoding="utf-8")
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo",
        {"intent": "x", "targets": [target], "action_type": "edit"},
    )
    action_proposals.approve(proposal["id"], "reviewer")
    monkeypatch.setattr(
        projects, "sync_opencode_config",
        lambda _project: {"action": "error", "warning": "boom"},
    )
    with pytest.raises(ValueError, match="guard MCP not registered"):
        action_proposals._prepare_execution(proposal["id"])
    assert action_proposals.get(proposal["id"])["status"] == "failed"


def test_prepare_execution_fails_when_guard_probe_fails(tmp_path, monkeypatch):
    from perun import mcp_servers

    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo",
        {"intent": "x", "targets": [target], "action_type": "edit"},
    )
    action_proposals.approve(proposal["id"], "reviewer")
    monkeypatch.setattr(
        mcp_servers, "probe",
        lambda *a, **k: {"ok": False, "status": "error", "error": "boom"},
    )
    with pytest.raises(ValueError, match="guard MCP probe failed"):
        action_proposals._prepare_execution(proposal["id"])
    assert action_proposals.get(proposal["id"])["status"] == "failed"


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


def test_policy_auto_approve_allows_sole_rw_requester(tmp_path, monkeypatch):
    """A sole rw participant does not block the explicit policy route."""
    project = _make_project(tmp_path)
    _enable_shared(project, participants=[{"agent_id": "frambo", "roles": ["rw"]}])
    target = str(project["path"] / "file.txt")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    monkeypatch.setattr(
        action_proposals.shared_projects, "is_policy_auto_approve", lambda _path: True
    )
    started = []
    monkeypatch.setattr(
        action_proposals,
        "start_execution",
        lambda proposal_id, **kw: started.append(proposal_id)
        or action_proposals.get(proposal_id),
    )

    result = action_proposals.auto_approve_and_execute(project, proposal)

    assert result is not None
    assert started == [proposal["id"]]
    approved = action_proposals.get(proposal["id"])
    assert approved["approved_by"] == action_proposals.AUTO_APPROVER


def test_policy_auto_approve_ignores_non_pending_proposal(tmp_path, monkeypatch):
    """An already-approved proposal is not re-approved or executed."""
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "file.txt")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    approved = action_proposals.approve(proposal["id"], "owner")
    monkeypatch.setattr(
        action_proposals.shared_projects, "is_policy_auto_approve", lambda _path: True
    )
    started = []
    monkeypatch.setattr(
        action_proposals,
        "start_execution",
        lambda *a, **kw: started.append(True) or approved,
    )

    result = action_proposals.auto_approve_and_execute(project, approved)

    assert result is None
    assert not started
    assert action_proposals.get(proposal["id"])["approved_by"] == "owner"


def test_policy_auto_approve_uses_autonomous_policy_identity(tmp_path, monkeypatch):
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
    monkeypatch.setattr(action_proposals.shared_projects, "is_policy_auto_approve", lambda _path: True)

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
    assert approved["approved_by"] == action_proposals.AUTO_APPROVER


def test_policy_auto_approve_leaves_proposal_approved_if_execution_fails(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project, participants=[{"agent_id": "frambo", "roles": ["rw"]}])
    target = str(project["path"] / "file.txt")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    monkeypatch.setattr(action_proposals.shared_projects, "is_policy_auto_approve", lambda _path: True)

    def fake_start(proposal_id, *, orchestrator_instance=None, on_complete=None):
        raise RuntimeError("orchestrator unavailable")

    monkeypatch.setattr(action_proposals, "start_execution", fake_start)

    with pytest.raises(RuntimeError, match="orchestrator unavailable"):
        action_proposals.auto_approve_and_execute(project, proposal)

    approved = action_proposals.get(proposal["id"])
    assert approved["status"] == "approved"
    assert approved["approved_by"] == action_proposals.AUTO_APPROVER


def test_claim_execution_reserves_approved_proposal(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    claimed = action_proposals.claim_execution(proposal["id"], "frambo")
    assert claimed["status"] == "claimed"
    assert claimed["executor_id"] == "frambo"
    assert claimed["execution_mode"] == "external"
    assert claimed["claim_snapshot"]


def test_claim_execution_honours_and_clamps_ttl(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    now = int(time.time())
    short = action_proposals.claim_execution(proposal["id"], "frambo", ttl=1)
    assert 0 <= short["claim_expires_at"] - now <= 2

    action_proposals.revoke_execution_claim(proposal["id"], "owner", owner="owner")
    huge = action_proposals.claim_execution(
        proposal["id"], "frambo", ttl=action_proposals.APPROVED_TTL_SECONDS * 10
    )
    assert (
        huge["claim_expires_at"]
        <= int(time.time()) + action_proposals.APPROVED_TTL_SECONDS + 1
    )


def test_claim_execution_rejects_ro_participant(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(
        project,
        participants=[{"agent_id": "frambo", "roles": ["ro"]}],
    )
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project,
        "owner",
        {"intent": "x", "targets": [target], "action_type": "edit"},
        owner="owner",
    )
    action_proposals.approve(proposal["id"], "reviewer")
    with pytest.raises(ValueError, match="read-only"):
        action_proposals.claim_execution(proposal["id"], "frambo")


def test_claim_execution_rejects_double_claim(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    action_proposals.claim_execution(proposal["id"], "frambo")
    with pytest.raises(ValueError, match="claimed"):
        action_proposals.claim_execution(proposal["id"], "glados")


def test_revoke_execution_claim_returns_to_approved(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    action_proposals.claim_execution(proposal["id"], "frambo")
    revoked = action_proposals.revoke_execution_claim(
        proposal["id"], "owner", owner="owner"
    )
    assert revoked["status"] == "approved"
    assert revoked["executor_id"] == ""
    assert revoked["executed_by"] == ""
    assert revoked["execution_mode"] == "perun"


def test_claim_expiry_sweep_returns_to_approved(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    action_proposals.claim_execution(proposal["id"], "frambo")
    with db.db() as conn:
        conn.execute(
            "UPDATE action_proposals SET claim_expires_at=? WHERE id=?",
            (int(time.time()) - 1, proposal["id"]),
        )
    action_proposals.expire_stale()
    restored = action_proposals.get(proposal["id"])
    assert restored["status"] == "approved"
    assert restored["executed_by"] == ""


def test_approved_proposal_expires_after_ttl(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    with db.db() as conn:
        conn.execute(
            "UPDATE action_proposals SET updated_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", proposal["id"]),
        )
    expired = action_proposals.expire_stale()
    assert proposal["id"] in expired
    assert action_proposals.get(proposal["id"])["status"] == "expired"


def test_concurrent_claim_race_leaves_only_one_winner(tmp_path, monkeypatch):
    """Two simultaneous claim_execution calls must not both succeed."""
    import threading

    project = _make_project(tmp_path)
    _enable_shared(
        project,
        participants=[
            {"agent_id": "frambo", "roles": ["rw"]},
            {"agent_id": "glados", "roles": ["rw"]},
        ],
    )
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    results: list = []
    errors: list = []

    def claim(agent):
        try:
            results.append(action_proposals.claim_execution(proposal["id"], agent))
        except Exception as exc:
            errors.append(str(exc))

    t1 = threading.Thread(target=claim, args=("frambo",))
    t2 = threading.Thread(target=claim, args=("glados",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 1
    assert len(errors) == 1
    winner = results[0]
    assert winner["status"] == "claimed"
    # The loser got a ValueError because the CAS update affected 0 rows.
    assert "approved" in errors[0] or "claimed" in errors[0]


def test_start_execution_refuses_claimed_proposal(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    action_proposals.claim_execution(proposal["id"], "frambo")
    with pytest.raises(ValueError, match="claimed"):
        action_proposals.start_execution(proposal["id"])


def test_delete_refuses_claimed_proposal(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    action_proposals.claim_execution(proposal["id"], "frambo")
    with pytest.raises(ValueError, match="claimed"):
        action_proposals.delete(proposal["id"])


def test_complete_external_execution_marks_executed(tmp_path):
    import io, zipfile
    project = _make_project(tmp_path)
    _enable_shared(project)
    root = project["path"]
    target = str(root / "api.py")
    (root / "api.py").write_text("old")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    action_proposals.claim_execution(proposal["id"], "frambo")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("api.py", "new")
    result = action_proposals.complete_external_execution(
        proposal["id"], "frambo", buf.getvalue()
    )
    assert result["status"] == "executed"
    assert (root / "api.py").read_text() == "new"


def test_external_execution_records_executor_and_room(tmp_path):
    """The executed record must name the executor and the bound room.

    Regression for a canary that finished ``executed`` with empty
    ``executed_by`` and ``room_id`` columns, which made the audit trail
    useless for the external-handoff path.
    """
    import io, zipfile
    project = _make_project(tmp_path)
    _enable_shared(project)
    room_id = "grp:perun:test-project:chat"
    registry.set_project_chat(project["id"], room_id, "owner", "active", 1)
    root = project["path"]
    target = str(root / "api.py")
    (root / "api.py").write_text("old")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    claimed = action_proposals.claim_execution(proposal["id"], "Frambo")
    assert claimed["executor_id"] == "frambo"
    assert claimed["executed_by"] == "frambo"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("api.py", "new")
    result = action_proposals.complete_external_execution(
        proposal["id"], "frambo", buf.getvalue()
    )

    assert result["status"] == "executed"
    assert result["room_id"] == room_id
    assert result["executor_id"] == "frambo"
    assert result["executed_by"] == "frambo"


def test_complete_external_execution_keeps_claimed_on_validation_failure(tmp_path):
    import io, zipfile
    project = _make_project(tmp_path)
    _enable_shared(project)
    root = project["path"]
    target = str(root / "api.py")
    (root / "api.py").write_text("old")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    action_proposals.claim_execution(proposal["id"], "frambo")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("other.py", "new")
    result = action_proposals.complete_external_execution(
        proposal["id"], "frambo", buf.getvalue()
    )
    assert result["ok"] is False
    assert action_proposals.get(proposal["id"])["status"] == "claimed"


def test_complete_external_execution_fails_on_no_target_changes(tmp_path):
    import io, zipfile
    project = _make_project(tmp_path)
    _enable_shared(project)
    root = project["path"]
    target = str(root / "api.py")
    (root / "api.py").write_text("old")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    action_proposals.claim_execution(proposal["id"], "frambo")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        pass  # empty package: no files touched
    result = action_proposals.complete_external_execution(
        proposal["id"], "frambo", buf.getvalue()
    )
    assert result["status"] == "failed"
    assert "NO TARGET CHANGES" in result["result"]
    # A failed completion still names the actor that attempted the handoff.
    assert result["executed_by"] == "frambo"

def test_external_apply_rejects_stale_claim(tmp_path, monkeypatch):
    import io, zipfile

    project = _make_project(tmp_path)
    _enable_shared(project)
    root = project["path"]
    target = str(root / "api.py")
    (root / "api.py").write_text("old")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    action_proposals.claim_execution(proposal["id"], "frambo")

    monkeypatch.setattr(action_proposals, "expire_stale", lambda *a, **k: [])
    with db.db() as conn:
        conn.execute(
            "UPDATE action_proposals SET claim_expires_at=? WHERE id=?",
            (int(time.time()) - 1, proposal["id"]),
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("api.py", "new")
    result = action_proposals.complete_external_execution(
        proposal["id"], "frambo", buf.getvalue()
    )
    assert result["ok"] is False
    assert "claim expired" in result["reason"]
    assert (root / "api.py").read_text() == "old"


def test_expire_stale_fails_abandoned_executing(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    with db.db() as conn:
        conn.execute(
            "UPDATE action_proposals SET status='executing', updated_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", proposal["id"]),
        )
    action_proposals.expire_stale()
    assert action_proposals.get(proposal["id"])["status"] == "failed"


def test_cancel_withdraws_an_approved_unclaimed_proposal(tmp_path):
    """reject() only accepts pending; a superseded approval needs a way out."""
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    # reject() refuses, which is what left proposals claimable forever.
    with pytest.raises(ValueError, match="proposal is approved"):
        action_proposals.reject(proposal["id"], "owner")

    cancelled = action_proposals.cancel(
        proposal["id"], "frambo", reason="superseded"
    )
    assert cancelled["status"] == "rejected"
    assert cancelled["result"] == "cancelled: superseded"

    # It can no longer be claimed.
    with pytest.raises(ValueError, match="proposal is rejected"):
        action_proposals.claim_execution(proposal["id"], "frambo")


def test_cancel_requires_requester_or_owner(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    with pytest.raises(ValueError, match="only the requester"):
        action_proposals.cancel(proposal["id"], "someone-else")

    cancelled = action_proposals.cancel(proposal["id"], "owner", owner="owner")
    assert cancelled["status"] == "rejected"


def test_new_proposal_has_no_execution_mode(tmp_path):
    """An unexecuted proposal must not advertise a pipeline it never used."""
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    assert proposal["execution_mode"] == ""

    action_proposals.approve(proposal["id"], "owner")
    claimed = action_proposals.claim_execution(proposal["id"], "frambo")
    assert claimed["execution_mode"] == "external"


def test_claim_requires_a_valid_approval_token(tmp_path):
    """The local path verifies the HITL token; an external claim must too."""
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    with db.db() as conn:
        conn.execute(
            "UPDATE action_proposals SET approval_token='' WHERE id=?", (proposal["id"],)
        )

    with pytest.raises(ValueError, match="valid approval token"):
        action_proposals.claim_execution(proposal["id"], "frambo")

    # The proposal is untouched and can be re-approved.
    assert action_proposals.get(proposal["id"])["status"] == "approved"


# --- claim-time target-drift check (backlog item 11) ----------------------

def test_claim_refuses_a_target_changed_since_approval(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    root = project["path"]
    target = str(root / "api.py")
    (root / "api.py").write_text("approved content")

    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    (root / "api.py").write_text("someone else's newer work")

    with pytest.raises(ValueError, match="changed since approval"):
        action_proposals.claim_execution(proposal["id"], "frambo")

    # The proposal is untouched and still claimable after re-approval.
    assert action_proposals.get(proposal["id"])["status"] == "approved"


def test_claim_refuses_a_target_created_since_approval(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    root = project["path"]
    target = str(root / "new_file.md")

    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "create"}
    )
    action_proposals.approve(proposal["id"], "owner")
    (root / "new_file.md").write_text("appeared later")

    with pytest.raises(ValueError, match="changed since approval"):
        action_proposals.claim_execution(proposal["id"], "frambo")


def test_claim_refuses_a_target_deleted_since_approval(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    root = project["path"]
    target = str(root / "api.py")
    (root / "api.py").write_text("content")

    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    (root / "api.py").unlink()

    with pytest.raises(ValueError, match="changed since approval"):
        action_proposals.claim_execution(proposal["id"], "frambo")


def test_claim_allows_unchanged_targets(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    root = project["path"]
    target = str(root / "api.py")
    (root / "api.py").write_text("stable")

    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    claimed = action_proposals.claim_execution(proposal["id"], "frambo")
    assert claimed["status"] == "claimed"


def test_claim_of_a_legacy_proposal_without_snapshot_is_allowed(tmp_path):
    """Upgrading must not strand proposals approved before drift tracking."""
    project = _make_project(tmp_path)
    _enable_shared(project)
    root = project["path"]
    target = str(root / "api.py")
    (root / "api.py").write_text("content")

    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    with db.db() as conn:
        conn.execute(
            "UPDATE action_proposals SET approval_snapshot='' WHERE id=?",
            (proposal["id"],),
        )
    (root / "api.py").write_text("changed")

    assert action_proposals.claim_execution(proposal["id"], "frambo")["status"] == "claimed"


def test_apply_refuses_a_target_changed_during_upload(tmp_path):
    import io
    import zipfile

    project = _make_project(tmp_path)
    _enable_shared(project)
    root = project["path"]
    target = str(root / "api.py")
    (root / "api.py").write_text("approved content")

    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")
    action_proposals.claim_execution(proposal["id"], "frambo")

    # Another actor writes to the target while the package is being built.
    (root / "api.py").write_text("newer work during upload")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("api.py", "external edit")
    with pytest.raises(ValueError, match="changed during upload"):
        action_proposals.complete_external_execution(
            proposal["id"], "frambo", buf.getvalue()
        )

    assert (root / "api.py").read_text() == "newer work during upload"
    assert action_proposals.get(proposal["id"])["status"] == "claimed"
