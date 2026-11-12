"""VS Code bridge: tickets, hello, selection, terminals and edit broadcast."""

from __future__ import annotations

import os
import time

import pytest

from perun import opencode_api, registry, vscode_bridge


class Loop:
    def is_closed(self):
        return False

    def call_soon_threadsafe(self, fn, value):
        fn(value)


class Outbox:
    def __init__(self):
        self.items = []

    def put_nowait(self, value):
        self.items.append(value)


@pytest.fixture()
def bridge():
    return vscode_bridge.VSCodeBridge()


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    return registry.add_project("vscode-demo", str(root), kind="opencode")


def _hello(bridge, project, outbox, workspace=None):
    issued = bridge.issue_ticket(project["id"], project["path"])
    ticket = bridge.redeem_ticket(issued["ticket"])
    bridge.register("c1", None, Loop(), outbox, ticket)
    bridge.handle_message("c1", {
        "type": "hello",
        "workspace_path": workspace or project["path"],
        "window_id": "w1",
        "protocol": 1,
        "extension_version": "0.1.0",
    })


def test_a_ticket_is_single_use_and_expires(bridge, project):
    ticket = bridge.issue_ticket(project["id"], project["path"])
    assert bridge.redeem_ticket(ticket["ticket"])["project_id"] == project["id"]
    assert bridge.redeem_ticket(ticket["ticket"]) is None

    expired = bridge.issue_ticket(project["id"], project["path"], ttl=5)
    with bridge._lock:
        bridge._tickets[expired["ticket"]]["expires"] = time.time() - 1
    assert bridge.redeem_ticket(expired["ticket"]) is None


def test_hello_returns_the_project_and_recent_state(bridge, project):
    outbox = Outbox()
    _hello(bridge, project, outbox)

    assert outbox.items[-1]["type"] == "hello.ok"
    assert outbox.items[-1]["project"]["slug"] == project["slug"]
    assert outbox.items[-1]["protocol"] == vscode_bridge.PROTOCOL_VERSION


def test_a_connection_cannot_register_a_different_workspace(bridge, project, tmp_path):
    outbox = Outbox()
    _hello(bridge, project, outbox, workspace=str(tmp_path))

    assert outbox.items[-1]["type"] == "error"
    assert "workspace" in outbox.items[-1]["message"]


def test_a_protocol_mismatch_is_refused(bridge, project):
    outbox = Outbox()
    issued = bridge.issue_ticket(project["id"], project["path"])
    ticket = bridge.redeem_ticket(issued["ticket"])
    bridge.register("c1", None, Loop(), outbox, ticket)

    bridge.handle_message("c1", {
        "type": "hello",
        "workspace_path": project["path"],
        "window_id": "w1",
        "protocol": 999,
        "extension_version": "0.0.1",
    })

    assert outbox.items[-1]["type"] == "error"
    assert "protocol" in outbox.items[-1]["message"]


def test_an_unknown_message_is_answered_with_an_error(bridge, project):
    outbox = Outbox()
    _hello(bridge, project, outbox)
    outbox.items.clear()

    bridge.handle_message("c1", {"type": "run.shell", "command": "whoami"})

    assert outbox.items[-1]["type"] == "error"
    assert "unknown" in outbox.items[-1]["message"]


def test_selection_is_validated_and_copied_without_a_terminal(bridge, project, monkeypatch):
    class Api:
        def pending_permissions(self, location):
            return []

        def pending_forms(self, location):
            return []

    monkeypatch.setattr(opencode_api, "default_api", lambda: Api())
    outbox = Outbox()
    _hello(bridge, project, outbox)
    outbox.items.clear()

    bridge.handle_message("c1", {
        "type": "selection.shared",
        "path": "app.py",
        "start_line": 1,
        "end_line": 2,
        "language": "python",
        "text": "alpha\nbeta",
        "dirty": False,
    })

    answer = outbox.items[-1]
    assert answer["type"] == "selection.ok"
    assert answer["action"] == "copy"
    assert answer["ref"] == "@app.py#1-2"
    assert bridge.context_for(project["id"])["path"] == "app.py"


def test_terminal_opened_binds_the_run_and_pid(bridge, project):
    run = registry.create_run("run_vscode_1", project["id"], "TUI",
                              standalone=True, opencode_session_id="ses_v")
    outbox = Outbox()
    _hello(bridge, project, outbox)

    bridge.handle_message("c1", {
        "type": "terminal.opened",
        "run_uid": "run_vscode_1",
        "terminal_id": "term-1",
        "pid": os.getpid(),
    })

    row = registry.get_run("run_vscode_1")
    assert row["terminal_id"] == "term-1"
    assert row["terminal_owner"] == "vscode"
    assert row["pid"] == os.getpid()
    assert run["run_uid"] == "run_vscode_1"


def test_terminal_open_goes_to_one_window_only(bridge, project):
    outbox1, outbox2 = Outbox(), Outbox()
    ticket = bridge.redeem_ticket(bridge.issue_ticket(project["id"], project["path"])["ticket"])
    bridge.register("c1", None, Loop(), outbox1, ticket)
    ticket = bridge.redeem_ticket(bridge.issue_ticket(project["id"], project["path"])["ticket"])
    bridge.register("c2", None, Loop(), outbox2, ticket)
    run = registry.create_run("run_vscode_2", project["id"], "TUI", standalone=True)

    assert bridge.request_terminal(run, project) is True
    assert sum(1 for box in (outbox1, outbox2)
               if any(m.get("type") == "terminal.open" for m in box.items)) == 1


def test_an_edit_event_is_broadcast_to_the_project(bridge, project):
    session = registry.create_session(project["id"], opencode_session_id="ses_v2")
    outbox = Outbox()
    _hello(bridge, project, outbox)
    outbox.items.clear()

    bridge._on_event({
        "type": "message.part.updated",
        "data": {
            "sessionID": "ses_v2",
            "part": {
                "type": "tool",
                "tool": "edit",
                "state": {
                    "status": "completed",
                    "input": {"filePath": "app.py", "oldString": "beta",
                              "newString": "beta\ngamma"},
                },
            },
        },
    })

    reveal = outbox.items[-1]
    assert reveal["type"] == "edit.reveal"
    assert reveal["path"] == "app.py"
    assert reveal["start_line"] == 2
    assert session["opencode_session_id"] == "ses_v2"


def test_unregister_drops_the_window_terminals(bridge, project):
    run = registry.create_run("run_unregister", project["id"], "TUI", standalone=True)
    outbox = Outbox()
    _hello(bridge, project, outbox)
    bridge.handle_message("c1", {
        "type": "terminal.opened",
        "run_uid": "run_unregister",
        "terminal_id": "term-drop",
        "pid": os.getpid(),
    })
    assert "run_unregister" in bridge._terminals

    bridge.unregister("c1")

    assert "run_unregister" not in bridge._terminals
    assert bridge.focus_terminal(run["run_uid"]) is False


def test_run_completion_fallback_reveals_changed_files(bridge, project, monkeypatch):
    from datetime import datetime, timezone
    from pathlib import Path

    target = Path(project["path"]) / "new-file.py"
    target.write_text("print('x')\n", encoding="utf-8")

    class Result:
        returncode = 0
        stdout = "?? new-file.py\n"

    monkeypatch.setattr("perun.vscode_bridge.subprocess.run",
                        lambda *args, **kwargs: Result())
    calls = []
    monkeypatch.setattr("perun.vscode_bridge.vscode_workspace.reveal",
                        lambda path, line=1, **kwargs: calls.append((path, line)))

    since = datetime.now(timezone.utc).isoformat()
    assert bridge.reveal_changes_since(project, since) == 1
    assert calls and calls[0][0].endswith("new-file.py")


def test_an_expired_embed_ticket_is_refused(bridge):
    issued = bridge.issue_embed_ticket(ttl=5)
    with bridge._lock:
        bridge._embed_tickets[issued["ticket"]]["expires"] = time.time() - 1
    assert bridge.redeem_embed_ticket(issued["ticket"]) is False
