"""The MCP side: an agent can ask the operator and get the answer back.

Loading mcp/perun_mcp.py directly because an installed `mcp` package shadows the
repo's `mcp/` directory under the normal import machinery.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from perun import attention, registry

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_PATH = REPO_ROOT / "mcp" / "perun_mcp.py"

spec = importlib.util.spec_from_file_location("perun_mcp_attention", MCP_PATH)
assert spec and spec.loader
mcp_mod = importlib.util.module_from_spec(spec)
sys.modules["perun_mcp_attention"] = mcp_mod
spec.loader.exec_module(mcp_mod)


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "ask-proj").mkdir()
    return registry.add_project("ask-proj", str(tmp_path / "ask-proj"), kind="dir")


def _ctx(session_id: str = "ses_mcp"):
    return SimpleNamespace(
        request_context=SimpleNamespace(meta={"ai.opencode/sessionID": session_id})
    )


def test_session_id_is_read_from_the_request_meta():
    assert mcp_mod._attention_session_id(_ctx("ses_abc")) == "ses_abc"
    assert mcp_mod._attention_session_id(None) == ""
    assert mcp_mod._attention_session_id(SimpleNamespace()) == ""


def test_ask_operator_records_the_question_against_that_session(project, monkeypatch):
    seen: dict = {}

    def fake_wait(attention_id, seconds, poll=1.0):  # noqa: ANN001, ANN003
        seen["id"] = attention_id
        seen["seconds"] = seconds
        seen["item"] = registry.get_attention(attention_id)
        return "yes, ship it"

    monkeypatch.setattr(attention, "wait", fake_wait)

    answer = mcp_mod.perun_ask_operator(
        "Ship it?", project="ask-proj", wait_seconds=60, ctx=_ctx("ses_mcp")
    )

    assert answer == "yes, ship it"
    assert seen["item"]["target_kind"] == "session"
    assert seen["item"]["target_id"] == "ses_mcp"
    assert seen["item"]["source"] == "ask"
    assert seen["item"]["question"] == "Ship it?"


def test_ask_operator_lists_options_and_returns_a_handle_on_timeout(project, monkeypatch):
    monkeypatch.setattr(attention, "wait", lambda *a, **k: None)

    reply = mcp_mod.perun_ask_operator(
        "Deploy where?", options=["staging", "prod"], project="ask-proj",
        ctx=_ctx("ses_mcp"),
    )

    assert "No answer yet (att_" in reply
    item = registry.open_attention()[0]
    assert "staging" in item["question"]
    assert [o["label"] for o in item["options"]] == ["staging", "prod"]


def test_ask_operator_requires_a_project(monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    reply = mcp_mod.perun_ask_operator("Who am I?", ctx=_ctx("ses_unknown"))
    assert "Could not resolve a project" in reply


def test_await_operator_returns_the_answer_without_asking_again(project, monkeypatch):
    item = attention.request(project, "session", "ses_mcp", "ask", "Ship it?")
    monkeypatch.setattr(attention, "wait", lambda *a, **k: "later yes")
    before = len(registry.open_attention())

    answer = mcp_mod.perun_await_operator(item["id"], wait_seconds=30)

    assert answer == "later yes"
    assert len(registry.open_attention()) == before


def test_await_operator_reports_a_closed_item(project, monkeypatch):
    item = attention.request(project, "session", "ses_mcp", "ask", "Ship it?")
    registry.close_attention(item["id"], "answered", answer="done", answered_by="x")

    assert "done" in mcp_mod.perun_await_operator(item["id"])


def test_attention_list_shows_open_items(project):
    attention.request(project, "session", "ses_mcp", "ask", "Ship it?")

    text = mcp_mod.perun_attention_list()

    assert "att_" in text
    assert "session" in text
