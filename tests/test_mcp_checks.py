"""The MCP surface of declared checks and proposal verification.

The module is loaded directly because an installed ``mcp`` package shadows the
repo's ``mcp/`` directory under the normal import machinery.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from perun import attention, checks, db, registry

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_PATH = REPO_ROOT / "mcp" / "perun_mcp.py"

spec = importlib.util.spec_from_file_location("perun_mcp_checks", MCP_PATH)
assert spec and spec.loader
mcp_mod = importlib.util.module_from_spec(spec)
sys.modules["perun_mcp_checks"] = mcp_mod
spec.loader.exec_module(mcp_mod)


def _ctx(session_id: str = "ses_checks"):
    return SimpleNamespace(
        request_context=SimpleNamespace(meta={"ai.opencode/sessionID": session_id})
    )


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "checks-proj"
    root.mkdir()
    return registry.add_project("checks-proj", str(root), kind="opencode")


def _executed_proposal(project, target: str = "tests/verify.py") -> str:
    proposal_id = "prop_checks"
    with db.db() as conn:
        conn.execute(
            "INSERT INTO action_proposals (id, project_id, requester, action_type, "
            "intent, targets, details, status, expires_at, execution_mode, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, project["id"], "frambo", "create", "deliver a verifier",
             json.dumps([target]), "{}", "executed", 9_999_999_999, "external",
             registry.now_iso(), registry.now_iso()),
        )
    return proposal_id


def _write_script(project, text: str = "print('verified')\n") -> Path:
    script = Path(project["path"]) / "tests" / "verify.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text(text, encoding="utf-8")
    return script


# --------------------------------------------------------------------------- #
# Declared checks
# --------------------------------------------------------------------------- #
def test_list_checks_explains_an_empty_registry(project):
    message = mcp_mod.perun_list_checks(project="checks-proj")
    assert "No checks declared" in message
    assert "perun checks add" in message


def test_list_checks_reports_the_declared_command(project):
    checks.declare(project, "smoke", f'"{sys.executable}" -c "print(1)"',
                   timeout_seconds=45)
    text = mcp_mod.perun_list_checks(project="checks-proj")
    assert "smoke" in text
    assert "45s" in text


def test_run_check_accepts_a_name_never_a_command(project):
    checks.declare(project, "smoke", f'"{sys.executable}" -c "print(42)"')

    assert "42" in mcp_mod.perun_run_check("smoke", project="checks-proj")
    refusal = mcp_mod.perun_run_check("echo pwned", project="checks-proj")
    assert "no check named" in refusal


# --------------------------------------------------------------------------- #
# Proposal verification
# --------------------------------------------------------------------------- #
def test_verification_asks_with_the_exact_command_and_hash(project, monkeypatch):
    script = _write_script(project)
    proposal_id = _executed_proposal(project)
    digest = checks.derive_proposal_verification(
        project, {"id": proposal_id, "status": "executed",
                  "targets": ["tests/verify.py"]})["sha256"]
    seen: dict = {}

    def fake_wait(attention_id, seconds, poll=1.0):  # noqa: ANN001, ANN003
        seen["item"] = registry.get_attention(attention_id)
        return "Run it"

    monkeypatch.setattr(attention, "wait", fake_wait)

    text = mcp_mod.perun_verify_proposal(
        proposal_id, project="checks-proj", ctx=_ctx())

    question = seen["item"]["question"]
    assert "python tests/verify.py" in question
    assert digest in question
    assert seen["item"]["options"][0]["label"] == "Run it"
    assert "ok" in text
    assert "verified" in text
    assert script.exists()


def test_declining_the_confirmation_runs_nothing(project, monkeypatch):
    _write_script(project)
    proposal_id = _executed_proposal(project)
    monkeypatch.setattr(attention, "wait", lambda *a, **k: "Cancel")

    text = mcp_mod.perun_verify_proposal(
        proposal_id, project="checks-proj", ctx=_ctx())

    assert "Not run" in text
    assert checks.history(project["id"]) == []


def test_a_file_changed_after_the_confirmation_is_refused(project, monkeypatch):
    script = _write_script(project)
    proposal_id = _executed_proposal(project)

    def swap_then_approve(attention_id, seconds, poll=1.0):  # noqa: ANN001, ANN003
        script.write_text("print('tampered')\n", encoding="utf-8")
        return "Run it"

    monkeypatch.setattr(attention, "wait", swap_then_approve)

    text = mcp_mod.perun_verify_proposal(
        proposal_id, project="checks-proj", ctx=_ctx())

    assert "changed after the operator" in text
    assert checks.history(project["id"]) == []


def test_a_pending_proposal_cannot_be_verified(project):
    _write_script(project)
    proposal_id = _executed_proposal(project)
    with db.db() as conn:
        conn.execute("UPDATE action_proposals SET status='approved' WHERE id=?",
                     (proposal_id,))

    text = mcp_mod.perun_verify_proposal(
        proposal_id, project="checks-proj", ctx=_ctx())

    assert "only an executed proposal" in text


def test_verification_without_a_session_or_room_is_refused(project):
    _write_script(project)
    proposal_id = _executed_proposal(project)

    text = mcp_mod.perun_verify_proposal(proposal_id, project="checks-proj")

    assert "no chat room" in text
    assert checks.history(project["id"]) == []


def test_verification_falls_back_to_the_project_room(project, monkeypatch):
    """A call with no session meta still asks, in the project's room."""
    _write_script(project)
    proposal_id = _executed_proposal(project)
    registry.set_project_chat(project["id"], "room:checks", "perun")
    seen: dict = {}

    def fake_wait(attention_id, seconds, poll=1.0):  # noqa: ANN001, ANN003
        seen["item"] = registry.get_attention(attention_id)
        return "Run it"

    monkeypatch.setattr(attention, "wait", fake_wait)

    text = mcp_mod.perun_verify_proposal(proposal_id, project="checks-proj")

    assert seen["item"]["target_kind"] == "room"
    assert seen["item"]["target_id"] == "room:checks"
    assert "ok" in text


def test_verification_records_proposal_provenance(project, monkeypatch):
    _write_script(project)
    proposal_id = _executed_proposal(project)
    monkeypatch.setattr(attention, "wait", lambda *a, **k: "Run it")

    mcp_mod.perun_verify_proposal(proposal_id, project="checks-proj", ctx=_ctx())

    row = checks.history(project["id"])[0]
    assert row["source"] == "proposal"
    assert row["proposal_id"] == proposal_id
    assert len(row["content_sha256"]) == 64


def test_a_late_answer_reuses_the_same_card(project, monkeypatch):
    """The room's wait is short; the operator may answer after it closed."""
    _write_script(project)
    proposal_id = _executed_proposal(project)
    monkeypatch.setattr(attention, "wait", lambda *a, **k: None)

    first = mcp_mod.perun_verify_proposal(
        proposal_id, project="checks-proj", ctx=_ctx())
    assert "waiting for the operator" in first

    open_items = registry.open_attention(state="open")
    assert len(open_items) == 1
    att = open_items[0]

    # The operator taps "Run it" later, closing the same card.
    registry.close_attention(att["id"], "answered", answer="Run it",
                             answered_by="operator")

    def no_second_question(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("the operator was asked twice")

    monkeypatch.setattr(attention, "wait", no_second_question)
    second = mcp_mod.perun_verify_proposal(
        proposal_id, project="checks-proj", ctx=_ctx())

    assert "ok" in second
    assert checks.history(project["id"])[0]["source"] == "proposal"


def test_a_late_decline_is_honoured_without_asking_again(project, monkeypatch):
    _write_script(project)
    proposal_id = _executed_proposal(project)
    monkeypatch.setattr(attention, "wait", lambda *a, **k: None)
    mcp_mod.perun_verify_proposal(proposal_id, project="checks-proj", ctx=_ctx())
    att = registry.open_attention(state="open")[0]
    registry.close_attention(att["id"], "answered", answer="Cancel",
                             answered_by="operator")

    def no_second_question(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("the operator was asked twice")

    monkeypatch.setattr(attention, "wait", no_second_question)
    text = mcp_mod.perun_verify_proposal(
        proposal_id, project="checks-proj", ctx=_ctx())

    assert "Not run" in text and "Cancel" in text
    assert checks.history(project["id"]) == []
