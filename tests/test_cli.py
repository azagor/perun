"""CLI robustness tests.

The CLI must not crash on Windows when retrieved text contains characters the
legacy console code page (cp1252) cannot encode — e.g. the ``→`` arrow that
appears in Perun's own documentation. ``main()`` forces UTF-8 stdio; these tests
lock that in.
"""

from __future__ import annotations

import io
import sys

from perun import __main__ as cli
from perun import db


def test_force_utf8_stdio_reconfigures_cp1252(monkeypatch):
    raw = io.BytesIO()
    wrapper = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", wrapper)

    cli._force_utf8_stdio()

    # After reconfiguring, a non-cp1252 char writes as UTF-8 instead of raising.
    print("bridge \u2192 project")
    wrapper.flush()
    assert b"\xe2\x86\x92" in raw.getvalue()  # UTF-8 bytes for '→'


def test_recall_with_non_ascii_memory_does_not_crash(monkeypatch):
    raw = io.BytesIO()
    wrapper = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", wrapper)

    from perun import memory

    db.init_db()
    memory.remember("telegram bridge routes \u2192 per project",
                    project_id=0, scope="global")

    rc = cli.main(["recall", "telegram bridge route", "--all", "--k", "3"])
    wrapper.flush()
    assert rc == 0


def test_run_command_starts_session_for_project(monkeypatch, capsys):
    from perun import orchestrator, registry

    db.init_db()
    registry.add_project("P", r"C:\p")
    calls: list = []

    def fake_start(self, ref, prompt, **kw):  # noqa: ANN001, ANN003
        calls.append((ref, prompt))
        return {"run_uid": "runCLI", "status": "queued"}

    monkeypatch.setattr(orchestrator.Orchestrator, "start_run", fake_start)
    rc = cli.main(["run", "p"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out
    # No prompt supplied: cmd_run passes an empty prompt to start_run, which
    # substitutes the default starter prompt internally.
    assert calls == [("p", "")]
    # Explicit prompt is passed through.
    rc = cli.main(["run", "p", "hello"])
    assert rc == 0
    assert calls[-1] == ("p", "hello")


def test_global_instructions_clear_reseeds_default(monkeypatch, tmp_path):
    """``--clear`` removes custom files but the default guidance is re-seeded."""
    from perun import config, orchestrator

    instr = tmp_path / "global"
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", instr)

    # Seed the default block plus a user-authored file.
    orchestrator._seed_default_instructions()
    (instr / "custom.md").write_text(
        "---\nname: Custom\nscope: all_projects\nenabled: true\n---\n\nCUSTOM\n",
        encoding="utf-8",
    )
    assert (instr / "default-agent-guidance.md").is_file()

    rc = cli.main(["global-instructions", "--clear"])
    assert rc == 0
    files = {p.name for p in instr.glob("*.md")}
    assert files == {"default-agent-guidance.md"}
