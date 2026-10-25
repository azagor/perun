import time
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


def test_execute_approved_proposal(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    target = str(project["path"] / "api.py")
    proposal = action_proposals.create(
        project, "frambo", {"intent": "x", "targets": [target], "action_type": "edit"}
    )
    action_proposals.approve(proposal["id"], "owner")

    mock_orchestrator = type("MockOrchestrator", (), {})()
    mock_orchestrator.run_and_wait = lambda *a, **kw: {"run": {"status": "completed"}, "reply": "done"}
    executed = action_proposals.execute(proposal["id"], orchestrator_instance=mock_orchestrator)

    assert executed["status"] == "executed"
    assert executed["result"] == "done"


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
