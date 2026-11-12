"""VS Code edit extraction and selection validation."""

from __future__ import annotations

from perun.vscode_events import (
    extract_edit,
    is_edit_event,
    selection_ref,
    validate_selection,
)


def _tool_event(tool: str, payload: dict, *, status: str = "completed",
                session: str = "ses_1") -> dict:
    return {
        "type": "message.part.updated",
        "data": {
            "sessionID": session,
            "part": {
                "type": "tool",
                "tool": tool,
                "state": {"status": status, "input": payload},
            },
        },
    }


def test_a_write_reveals_the_new_file(tmp_path):
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("line one\nline two\n", encoding="utf-8")

    edit = extract_edit(
        _tool_event("write", {"filePath": str(target), "content": "line one\nline two\n"}),
        str(tmp_path),
    )

    assert edit == {
        "path": "src/app.py",
        "start_line": 1,
        "end_line": 2,
        "kind": "added",
        "tool": "write",
    }


def test_an_edit_reveals_the_new_text_range(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("alpha\nbeta\ndelta\ngamma\n", encoding="utf-8")

    edit = extract_edit(
        _tool_event("edit", {
            "filePath": str(target),
            "oldString": "beta",
            "newString": "beta\ndelta",
        }),
        str(tmp_path),
    )

    assert edit["path"] == "app.py"
    assert edit["start_line"] == 2
    assert edit["end_line"] == 3
    assert edit["kind"] == "changed"


def test_a_delete_is_marked_deleted(tmp_path):
    target = tmp_path / "gone.py"
    target.write_text("keep\nremove me\n", encoding="utf-8")

    edit = extract_edit(
        _tool_event("edit", {
            "filePath": str(target),
            "oldString": "remove me",
            "newString": "",
        }),
        str(tmp_path),
    )

    assert edit["kind"] == "deleted"
    assert edit["start_line"] == 2


def test_a_path_outside_the_project_is_refused(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    assert extract_edit(
        _tool_event("write", {"filePath": str(outside), "content": "secret"}),
        str(tmp_path),
    ) is None


def test_git_internals_are_never_revealed(tmp_path):
    (tmp_path / ".git").mkdir()
    target = tmp_path / ".git" / "config"
    target.write_text("[core]", encoding="utf-8")

    assert extract_edit(
        _tool_event("write", {"filePath": str(target), "content": "[core]"}),
        str(tmp_path),
    ) is None


def test_a_patch_uses_the_hunk_line(tmp_path):
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/app.py\n"
        "@@ -1,2 +7,3 @@\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )

    edit = extract_edit(_tool_event("patch", {"patchText": patch}), str(tmp_path))

    assert edit == {
        "path": "src/app.py",
        "start_line": 7,
        "end_line": 9,
        "kind": "changed",
        "tool": "patch",
    }


def test_text_and_reasoning_events_are_not_edits():
    assert is_edit_event({"data": {"part": {"type": "text", "text": "hi"}}}) is False
    assert is_edit_event(_tool_event("bash", {"command": "ls"})) is False
    assert is_edit_event(_tool_event("write", {"filePath": "a.py"})) is True
    assert is_edit_event(_tool_event("write", {"filePath": "a.py"},
                                     status="running")) is False


def test_selection_ref_and_validation(tmp_path):
    assert selection_ref("src/app.py", 3, 3) == "@src/app.py#3"
    assert selection_ref("src/app.py", 3, 8) == "@src/app.py#3-8"

    selection = validate_selection({
        "path": "src/app.py",
        "start_line": 3,
        "end_line": 8,
        "language": "python",
        "text": "x" * 40_000,
    }, str(tmp_path))

    assert selection["ref"] == "@src/app.py#3-8"
    assert len(selection["text"]) == 32_000
    assert validate_selection({"path": "../outside.py"}, str(tmp_path)) is None
