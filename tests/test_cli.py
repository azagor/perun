"""CLI robustness tests.

The CLI must not crash on Windows when retrieved text contains characters the
legacy console code page (cp1252) cannot encode — e.g. the ``→`` arrow that
appears in Perun's own documentation. ``main()`` forces UTF-8 stdio; these tests
lock that in.
"""

from __future__ import annotations

import io
import json
import os
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


def test_memory_stats_cli_json(capsys):
    from perun import memory

    db.init_db()
    memory.remember("cli stats test fact", scope="global", kind="rule")
    rc = cli.main(["memory", "stats", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    data = json.loads(captured.out)
    assert "facts_total" in data


def test_memory_list_cli_json(capsys):
    from perun import memory

    db.init_db()
    memory.remember("cli list test fact", scope="global", kind="rule")
    rc = cli.main(["memory", "list", "--kind", "rule", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    data = json.loads(captured.out)
    assert any("cli list test fact" in (f.get("content") or "") for f in data)


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


def test_docs_audit_cli_finds_drift(monkeypatch, tmp_path, capsys):
    from perun import config, registry, scaffold

    db.init_db()
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    config.ensure_dirs()

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / "docs").mkdir(parents=True, exist_ok=True)
    (proj_dir / "docs" / "CHANGELOG.md").write_text("# Changelog\n\n## v1.0.0\n", encoding="utf-8")
    pkg_dir = proj_dir / "proj"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text('__version__ = "1.0.0"\n', encoding="utf-8")
    (pkg_dir / "app.py").write_text("def run(): pass\n", encoding="utf-8")
    # CODEMAP references a missing file and an identifier not in app.py.
    (proj_dir / "docs" / "CODEMAP.md").write_text(
        "# CODEMAP\n\n"
        "## Core\n\n"
        "### `proj/missing.py`\n\n"
        "* Uses `nonexistent()`.\n\n"
        "### `proj/app.py`\n\n"
        "* Uses `run()`.\n",
        encoding="utf-8",
    )
    registry.add_project("Proj", str(proj_dir))

    rc = cli.main(["docs", "audit", "proj"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "audit complete" in captured.out
    assert "missing file" in captured.out


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


def test_restart_replays_recorded_flags(monkeypatch, tmp_path):
    """A plain restart must not silently drop recorded flags such as --bridge."""
    from perun import daemon

    monkeypatch.setattr(daemon, "RUN_RECORD_PATH", tmp_path / "server.json")
    daemon.record_run(
        ["serve", "--host", "0.0.0.0", "--port", "8760", "--bridge"], pid=1
    )

    launched: dict = {}

    def fake_detached(argv):  # noqa: ANN001
        launched["argv"] = argv
        return 4242

    monkeypatch.setattr(daemon, "is_another_perun_running", lambda settings: None)
    monkeypatch.setattr(daemon, "start_detached", fake_detached)
    monkeypatch.setattr(daemon, "write_pid_for", lambda pid: None)

    assert cli.main(["restart"]) == 0
    assert launched["argv"] == [
        sys.executable, "-m", "perun", "serve",
        "--host", "0.0.0.0", "--port", "8760", "--bridge",
    ]


def test_restart_explicit_flags_override_recorded(monkeypatch, tmp_path):
    """Flags passed on the restart command line win over the recorded ones."""
    from perun import daemon

    monkeypatch.setattr(daemon, "RUN_RECORD_PATH", tmp_path / "server.json")
    daemon.record_run(["serve", "--host", "0.0.0.0", "--port", "8760"], pid=1)

    launched: dict = {}

    def fake_detached(argv):  # noqa: ANN001
        launched["argv"] = argv
        return 4242

    monkeypatch.setattr(daemon, "is_another_perun_running", lambda settings: None)
    monkeypatch.setattr(daemon, "start_detached", fake_detached)
    monkeypatch.setattr(daemon, "write_pid_for", lambda pid: None)

    assert cli.main(["restart", "--port", "1234"]) == 0
    assert launched["argv"] == [
        sys.executable, "-m", "perun", "serve",
        "--host", "0.0.0.0", "--port", "1234",
    ]


def test_serve_records_launch_argv(monkeypatch, tmp_path):
    """``perun serve`` records its effective flags for later restarts."""
    from perun import daemon, server

    monkeypatch.setattr(daemon, "RUN_RECORD_PATH", tmp_path / "server.json")
    monkeypatch.setattr(daemon, "is_another_perun_running", lambda settings: None)
    monkeypatch.setattr(daemon, "write_pid", lambda pid=None: None)
    monkeypatch.setattr(daemon, "clear_pid", lambda force=False: None)

    served: dict = {}

    def fake_serve(host=None, port=None, reload=False):  # noqa: A002
        served.update(host=host, port=port, reload=reload)

    monkeypatch.setattr(server, "serve", fake_serve)

    assert cli.main(["serve", "--port", "1234"]) == 0
    assert served == {"host": None, "port": 1234, "reload": False}
    assert daemon.read_recorded_argv() == ["serve", "--port", "1234"]


def test_serve_survives_record_write_failure(monkeypatch, capsys):
    """A failed launch-flag record must warn, not prevent serve from starting."""
    from perun import daemon, server

    def boom(argv=None):  # noqa: ANN001
        raise OSError("disk full")

    monkeypatch.setattr(daemon, "is_another_perun_running", lambda settings: None)
    monkeypatch.setattr(daemon, "write_pid", lambda pid=None: None)
    monkeypatch.setattr(daemon, "clear_pid", lambda force=False: None)
    monkeypatch.setattr(daemon, "record_run", boom)
    monkeypatch.setattr(server, "serve", lambda host=None, port=None, reload=False: None)

    assert cli.main(["serve"]) == 0
    assert "could not record launch flags" in capsys.readouterr().out


def test_recorded_flag_value_rejects_flag_as_value():
    """A flag-looking token in a hand-edited record is not a valid value."""
    assert cli._recorded_flag_value(["serve", "--host", "0.0.0.0"], "--host") == "0.0.0.0"
    assert cli._recorded_flag_value(["serve", "--host", "--port", "8760"], "--host") is None
    assert cli._recorded_flag_value(["serve", "--port", "8760"], "--port") == "8760"
    assert cli._recorded_flag_value(["serve", "--host"], "--host") is None  # no value
    assert cli._recorded_flag_value(["serve", "--bridge"], "--host") is None  # absent
    assert cli._recorded_port(["serve", "--port", "not-a-number"]) is None
    assert cli._recorded_port(["serve", "--port", "--bridge"]) is None
    assert cli._recorded_port(["serve", "--port", "8760"]) == 8760


def test_restart_foreground_replays_recorded_flags(monkeypatch, tmp_path):
    """The foreground path must receive the replayed flags too, never detach."""
    from perun import daemon

    monkeypatch.setattr(daemon, "RUN_RECORD_PATH", tmp_path / "server.json")
    daemon.record_run(["serve", "--bridge"], pid=1)

    served: dict = {}
    detached: list = []

    def fake_serve(args):  # noqa: ANN001
        served["args"] = args
        return 0

    monkeypatch.setattr(daemon, "is_another_perun_running", lambda settings: None)
    monkeypatch.setattr(daemon, "start_detached",
                        lambda argv: detached.append(argv) or 1)
    monkeypatch.setattr(cli, "cmd_serve", fake_serve)

    assert cli.main(["restart", "--foreground"]) == 0
    assert served["args"].bridge is True
    assert detached == []


# --------------------------------------------------------------------------- #
# Context-budget tooling
# --------------------------------------------------------------------------- #
def test_context_dedupe_removes_duplicate_subsections(monkeypatch, tmp_path, capsys):
    """``perun context dedupe`` removes byte-identical ``###`` blocks."""
    from perun import registry

    db.init_db()
    root = tmp_path / "demo"
    root.mkdir()
    registry.add_project("demo", str(root))

    agents = root / "AGENTS.md"
    agents.write_text(
        "# Demo\n\n## Rules\n\n- Rule one.\n\n### Module\n\nAlpha.\n\n### Module\n\nAlpha.\n\n",
        encoding="utf-8")

    assert cli.main(["context", "dedupe", "--project", "demo"]) == 0
    text = agents.read_text(encoding="utf-8")
    assert text.count("### Module") == 1
    assert "Alpha." in text
    captured = capsys.readouterr()
    assert "removed 1 duplicate" in captured.out


def test_context_dedupe_preserves_non_duplicate_content(monkeypatch, tmp_path, capsys):
    """``perun context dedupe`` leaves non-duplicate sections untouched."""
    from perun import registry

    db.init_db()
    root = tmp_path / "demo"
    root.mkdir()
    registry.add_project("demo", str(root))

    agents = root / "AGENTS.md"
    original = "# Demo\n\n## Rules\n\n- Rule one.\n\n### Module A\n\nAlpha.\n\n### Module B\n\nBravo.\n\n"
    agents.write_text(original, encoding="utf-8")

    assert cli.main(["context", "dedupe", "--project", "demo"]) == 0
    assert agents.read_text(encoding="utf-8") == original
    captured = capsys.readouterr()
    assert "no duplicate" in captured.out


def test_context_stats_returns_valid_json(capsys):
    """``perun context stats`` emits a JSON object with the ledger keys."""
    db.init_db()
    rc = cli.main(["context", "stats"])
    captured = capsys.readouterr()
    assert rc == 0
    data = json.loads(captured.out)
    assert "global_agents_md" in data
    assert "global_instructions" in data
    assert "mcp_server" in data
    assert "skills" in data
    assert "projects" in data
    assert "floor_tokens" in data


def test_context_stats_handles_empty_registry(capsys):
    """``perun context stats`` does not crash when no projects are registered."""
    db.init_db()
    rc = cli.main(["context", "stats"])
    captured = capsys.readouterr()
    assert rc == 0
    data = json.loads(captured.out)
    assert data["projects"] == []
    assert data["floor_tokens"] is not None


def test_context_verify_detects_pointer_drift(monkeypatch, tmp_path, capsys):
    """``perun context verify`` fails when the instructions pointer is stale."""
    from perun.integrations import install_global

    db.init_db()
    fake = tmp_path / "instructions.md"
    fake.write_text("# Old full guide\n\nLots of duplicated text.\n", encoding="utf-8")
    monkeypatch.setattr(install_global, "instructions_target", lambda: fake)

    res = cli._context_verify()
    captured = capsys.readouterr()
    assert not res["ok"]
    assert any("POINTER DRIFT" in line for line in res["report"])


def test_context_verify_passes_for_fresh_pointer(monkeypatch, tmp_path):
    """``perun context verify`` passes when the instructions pointer is current."""
    from perun.integrations import install_global

    db.init_db()
    fake = tmp_path / "instructions.md"
    fake.write_text(install_global._INSTRUCTIONS_POINTER, encoding="utf-8")
    monkeypatch.setattr(install_global, "instructions_target", lambda: fake)

    res = cli._context_verify()
    assert any("instructions: ok (pointer)" in line for line in res["report"])


def test_context_instruction_files_ignore_escaping_refs(monkeypatch, tmp_path):
    """``instructions[]`` entries that escape the config root are ignored."""
    from perun.integrations import install_global

    db.init_db()
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "opencode.json"

    # A legitimate file inside the config dir and one that escapes it.
    legit = cfg_dir / "legit.md"
    legit.write_text("ok", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("leak", encoding="utf-8")

    cfg_file.write_text(
        json.dumps({"instructions": ["legit.md", "../../secret.txt"]}),
        encoding="utf-8")
    monkeypatch.setattr(install_global, "config_dir", lambda: cfg_dir)
    monkeypatch.setattr(install_global, "opencode_json", lambda: cfg_file)
    monkeypatch.setattr(install_global, "agents_target", lambda: cfg_dir / "agents.md")
    monkeypatch.setattr(install_global, "instructions_target", lambda: cfg_dir / "perun.md")

    files = cli._context_instruction_files()
    paths = {f["path"] for f in files}
    assert str(legit) in paths
    assert str(outside) not in paths


# --------------------------------------------------------------------------- #
# Owner check for hidden-project maintenance commands
# --------------------------------------------------------------------------- #
def test_is_owner_true_on_windows(monkeypatch):
    from perun import config

    monkeypatch.setattr(sys, "platform", "win32")

    class FakeHome:
        def stat(self):
            class St:
                st_uid = 0
            return St()

    monkeypatch.setattr(config, "HOME", FakeHome())
    assert cli._is_owner() is True


def test_is_owner_true_when_unix_uid_matches(monkeypatch):
    from perun import config

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "getuid", lambda: 1234, raising=False)

    class FakeHome:
        def stat(self):
            class St:
                st_uid = 1234
            return St()

    monkeypatch.setattr(config, "HOME", FakeHome())
    assert cli._is_owner() is True


def test_is_owner_false_when_unix_uid_mismatches(monkeypatch):
    from perun import config

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "getuid", lambda: 1234, raising=False)

    class FakeHome:
        def stat(self):
            class St:
                st_uid = 999
            return St()

    monkeypatch.setattr(config, "HOME", FakeHome())
    assert cli._is_owner() is False


def test_is_owner_false_when_home_stat_fails(monkeypatch):
    from perun import config

    class FakeHome:
        def stat(self):
            raise OSError("no such directory")

    monkeypatch.setattr(config, "HOME", FakeHome())
    assert cli._is_owner() is False

