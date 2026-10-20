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


def test_backfill_docs_cli_all_projects(monkeypatch, tmp_path, capsys):
    from perun import config, registry, scaffold

    db.init_db()
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    config.ensure_dirs()

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    registry.add_project("Proj", str(proj_dir))

    rc = cli.main(["backfill-docs", "--all"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "backfill complete" in captured.out
    assert (proj_dir / "AGENTS.md").is_file()
    assert (proj_dir / "perun.md").is_file()
    assert (proj_dir / "docs" / "CHANGELOG.md").is_file()


def test_validate_docs_cli_reports_issues(monkeypatch, tmp_path, capsys):
    from perun import config, registry

    db.init_db()
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    config.ensure_dirs()

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    registry.add_project("Proj", str(proj_dir))

    rc = cli.main(["validate-docs", "proj"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "validation complete" in captured.out


def test_refresh_deps_cli_records_facts(monkeypatch, tmp_path, capsys):
    from perun import config, registry

    db.init_db()
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    config.ensure_dirs()

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    p = registry.add_project("Proj", str(proj_dir))
    (proj_dir / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    rc = cli.main(["refresh-deps", "proj"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "refresh complete" in captured.out
    assert "1 fact" in captured.out


def test_restart_detaches_by_default(monkeypatch, capsys):
    """``perun restart`` must start its replacement detached from the shell.

    An attached server dies with the shell that launched it (a CI step, a
    script, an agent harness hitting its timeout), silently taking the
    Telegram/PerunChat bridges down with it.
    """
    from perun import daemon

    launched: dict = {}

    def fake_detached(argv):  # noqa: ANN001
        launched["argv"] = argv
        return 4242

    monkeypatch.setattr(daemon, "is_another_perun_running", lambda settings: None)
    monkeypatch.setattr(daemon, "start_detached", fake_detached)
    monkeypatch.setattr(daemon, "write_pid_for",
                        lambda pid: launched.setdefault("pid", pid))

    rc = cli.main(["restart", "--bridge"])
    captured = capsys.readouterr()
    assert rc == 0
    assert launched["argv"] == [sys.executable, "-m", "perun", "serve", "--bridge"]
    assert launched["pid"] == 4242
    assert "detached server (PID 4242)" in captured.out


def test_restart_foreground_stays_attached(monkeypatch, capsys):
    """``--foreground`` restores the old watch-the-logs behavior."""
    from perun import daemon

    served: dict = {}

    def fake_serve(args):  # noqa: ANN001
        served["args"] = args
        return 0

    monkeypatch.setattr(daemon, "is_another_perun_running", lambda settings: None)
    monkeypatch.setattr(cli, "cmd_serve", fake_serve)

    rc = cli.main(["restart", "--foreground"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "foreground" in captured.out
    assert served["args"].foreground is True


def test_restart_detach_flag_still_accepted(monkeypatch):
    """``--detach`` is a no-op since v0.7.9 (detached is the default) but
    existing scripts passing it must keep working."""
    from perun import daemon

    monkeypatch.setattr(daemon, "is_another_perun_running", lambda settings: None)
    monkeypatch.setattr(daemon, "start_detached", lambda argv: 7)
    monkeypatch.setattr(daemon, "write_pid_for", lambda pid: None)

    assert cli.main(["restart", "--detach"]) == 0
