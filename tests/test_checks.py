"""Operator-declared checks: storage, execution, pinning, and leases."""

from __future__ import annotations

import sys
import time

import pytest

from perun import checks, registry


def _project(tmp_path, name="Checks"):
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    return registry.add_project(name, str(root), kind="opencode")


def _py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


# --------------------------------------------------------------------------- #
# Declarations
# --------------------------------------------------------------------------- #
def test_declare_list_and_remove(tmp_path):
    project = _project(tmp_path)
    check = checks.declare(project, "smoke", _py("print('ok')"), timeout_seconds=30)

    assert check["name"] == "smoke"
    assert check["timeout_seconds"] == 30
    listed = checks.list_checks(project["id"])
    assert [c["name"] for c in listed] == ["smoke"]
    assert listed[0]["last_status"] is None

    assert checks.remove(project["id"], "smoke") is True
    assert checks.list_checks(project["id"]) == []
    assert checks.remove(project["id"], "smoke") is False


def test_declare_is_an_upsert(tmp_path):
    project = _project(tmp_path)
    checks.declare(project, "smoke", _py("print(1)"))
    checks.declare(project, "smoke", _py("print(2)"), timeout_seconds=45)

    listed = checks.list_checks(project["id"])
    assert len(listed) == 1
    assert "print(2)" in listed[0]["command"]
    assert listed[0]["timeout_seconds"] == 45


def test_a_check_name_is_an_identifier(tmp_path):
    project = _project(tmp_path)
    for bad in ("", "Has Spaces", "../escape", "-leading", "x" * 41):
        with pytest.raises(checks.CheckError):
            checks.declare(project, bad, _py("print(1)"))


def test_the_working_directory_cannot_escape_the_project(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(checks.CheckError):
        checks.declare(project, "escape", _py("print(1)"), cwd="..")
    (tmp_path / "proj" / "sub").mkdir()
    check = checks.declare(project, "sub", _py("print(1)"), cwd="sub")
    assert check["cwd"] == "sub"


def test_the_timeout_is_clamped(tmp_path):
    project = _project(tmp_path)
    assert checks.declare(project, "a", _py("print(1)"),
                          timeout_seconds=99999)["timeout_seconds"] == checks.MAX_TIMEOUT
    with pytest.raises(checks.CheckError):
        checks.declare(project, "b", _py("print(1)"), timeout_seconds=0)
    with pytest.raises(checks.CheckError):
        checks.declare(project, "c", _py("print(1)"), timeout_seconds="soon")


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def test_a_successful_check_reports_exit_zero_and_output(tmp_path):
    project = _project(tmp_path)
    checks.declare(project, "smoke", _py("print('hello from the check')"))

    result = checks.run_declared(project, "smoke", requester="tester")

    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert "hello from the check" in result["output"]
    assert result["run_id"] > 0
    assert result["source"] == "declared"


def test_a_failing_check_is_failed_not_an_exception(tmp_path):
    project = _project(tmp_path)
    checks.declare(project, "bad", _py("raise SystemExit(3)"))

    result = checks.run_declared(project, "bad")

    assert result["status"] == "failed"
    assert result["exit_code"] == 3


def test_a_timeout_kills_the_process_tree(tmp_path):
    project = _project(tmp_path)
    checks.declare(project, "slow", _py("import time; print('started', flush=True); time.sleep(30)"),
                   timeout_seconds=1)

    started = time.monotonic()
    result = checks.run_declared(project, "slow")
    elapsed = time.monotonic() - started

    assert result["status"] == "timeout"
    assert result["timed_out"] is True
    assert "started" in result["output"]
    assert elapsed < 20


def test_the_environment_is_scrubbed(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("PERUN_FAKE_SECRET", "s3cret")
    checks.declare(
        project, "env",
        _py("import os; print(os.environ.get('PERUN_FAKE_SECRET', 'MISSING'))"),
    )

    result = checks.run_declared(project, "env")

    assert "MISSING" in result["output"]
    assert "s3cret" not in result["output"]


def test_a_run_is_recorded_in_the_history(tmp_path):
    project = _project(tmp_path)
    checks.declare(project, "smoke", _py("print(1)"))

    result = checks.run_declared(project, "smoke", requester="room")
    rows = checks.history(project["id"], "smoke")

    assert len(rows) == 1
    assert rows[0]["id"] == result["run_id"]
    assert rows[0]["status"] == "ok"
    assert rows[0]["requester"] == "room"
    assert checks.list_checks(project["id"])[0]["last_status"] == "ok"


def test_a_pinned_script_must_still_match(tmp_path):
    project = _project(tmp_path)
    script = tmp_path / "proj" / "verify.py"
    script.write_text("print('v1')\n", encoding="utf-8")
    checks.declare(project, "verify", _py("print('unused')"),
                   pin="verify.py")

    script.write_text("print('v2')\n", encoding="utf-8")
    with pytest.raises(checks.CheckError, match="changed"):
        checks.run_declared(project, "verify")


def test_an_unknown_check_lists_what_is_declared(tmp_path):
    project = _project(tmp_path)
    checks.declare(project, "known", _py("print(1)"))
    with pytest.raises(checks.CheckError, match="declared: known"):
        checks.run_declared(project, "nope")


def test_a_second_run_in_the_same_project_is_refused(tmp_path):
    project = _project(tmp_path)
    checks.declare(project, "smoke", _py("print(1)"))
    with registry.db.db() as conn:
        conn.execute(
            "INSERT INTO check_runs (project_id, check_name, source, command, "
            "status, timeout_seconds, started_epoch, created_at) "
            "VALUES (?,?,?,?, 'running', ?, ?, ?)",
            (project["id"], "smoke", "declared", "x", 120, time.time(),
             registry.now_iso()),
        )
    with pytest.raises(checks.CheckBusy):
        checks.run_declared(project, "smoke")


def test_a_crashed_run_is_reaped(tmp_path):
    project = _project(tmp_path)
    with registry.db.db() as conn:
        conn.execute(
            "INSERT INTO check_runs (project_id, check_name, source, command, "
            "status, timeout_seconds, started_epoch, created_at) "
            "VALUES (?,?,?,?, 'running', ?, ?, ?)",
            (project["id"], "smoke", "declared", "x", 120,
             time.time() - 10_000, registry.now_iso()),
        )

    assert checks.reap_stale(project["id"]) == 1
    assert checks.history(project["id"])[0]["status"] == "abandoned"


# --------------------------------------------------------------------------- #
# Proposal verification
# --------------------------------------------------------------------------- #
def _proposal(**overrides):
    base = {
        "id": "prop_test",
        "status": "executed",
        "targets": ["tests/verify.py"],
    }
    base.update(overrides)
    return base


def test_only_an_executed_proposal_can_be_verified(tmp_path):
    project = _project(tmp_path)
    (tmp_path / "proj" / "tests").mkdir()
    (tmp_path / "proj" / "tests" / "verify.py").write_text("print(1)\n")
    for status in ("approved", "failed", "cancelled"):
        with pytest.raises(checks.CheckError, match="only an executed"):
            checks.derive_proposal_verification(project, _proposal(status=status))


def test_the_command_is_derived_from_the_approved_script(tmp_path):
    project = _project(tmp_path)
    (tmp_path / "proj" / "tests").mkdir()
    script = tmp_path / "proj" / "tests" / "verify.py"
    script.write_text("print('ok')\n", encoding="utf-8")

    derived = checks.derive_proposal_verification(project, _proposal())

    assert derived["argv"][0] == sys.executable
    assert derived["argv"][-1].endswith("verify.py")
    assert derived["display"].startswith("python")
    assert len(derived["sha256"]) == 64


def test_a_proposal_target_cannot_escape_the_project(tmp_path):
    project = _project(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("print(1)\n", encoding="utf-8")
    with pytest.raises(checks.CheckError, match="outside the project"):
        checks.derive_proposal_verification(
            project, _proposal(targets=[str(outside)]))


def test_a_proposal_without_a_script_target_is_refused(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(checks.CheckError, match="no runnable script target"):
        checks.derive_proposal_verification(
            project, _proposal(targets=["README.md", "docs/PLAN.md"]))


def test_several_script_targets_need_an_explicit_one(tmp_path):
    project = _project(tmp_path)
    (tmp_path / "proj" / "tests").mkdir()
    (tmp_path / "proj" / "tests" / "a.py").write_text("print(1)\n")
    (tmp_path / "proj" / "tests" / "b.py").write_text("print(2)\n")
    targets = ["tests/a.py", "tests/b.py"]

    with pytest.raises(checks.CheckError, match="several script targets"):
        checks.derive_proposal_verification(project, _proposal(targets=targets))
    derived = checks.derive_proposal_verification(
        project, _proposal(targets=targets), target="tests/b.py")
    assert derived["target"] == "tests/b.py"


def test_a_changed_target_after_confirmation_is_refused(tmp_path):
    project = _project(tmp_path)
    (tmp_path / "proj" / "tests").mkdir()
    script = tmp_path / "proj" / "tests" / "verify.py"
    script.write_text("print('v1')\n", encoding="utf-8")
    confirmed = checks.derive_proposal_verification(project, _proposal())["sha256"]

    script.write_text("print('v2')\n", encoding="utf-8")
    with pytest.raises(checks.CheckError, match="changed after the operator"):
        checks.run_proposal_verification(
            project, _proposal(), expected_sha256=confirmed)


def test_a_confirmed_proposal_runs_and_records_provenance(tmp_path):
    project = _project(tmp_path)
    (tmp_path / "proj" / "tests").mkdir()
    script = tmp_path / "proj" / "tests" / "verify.py"
    script.write_text("print('verified')\n", encoding="utf-8")
    derived = checks.derive_proposal_verification(project, _proposal())

    result = checks.run_proposal_verification(
        project, _proposal(), expected_sha256=derived["sha256"],
        requester="room")

    assert result["status"] == "ok"
    assert "verified" in result["output"]
    row = checks.history(project["id"])[0]
    assert row["source"] == "proposal"
    assert row["proposal_id"] == "prop_test"
    assert row["content_sha256"] == derived["sha256"]


def test_format_result_is_honest_about_the_exit_code(tmp_path):
    project = _project(tmp_path)
    checks.declare(project, "bad", _py("raise SystemExit(2)"))
    result = checks.run_declared(project, "bad")
    text = checks.format_result(result)
    assert "failed" in text
    assert "exit 2" in text

def test_deleting_a_project_removes_its_checks_and_history(tmp_path):
    project = _project(tmp_path)
    checks.declare(project, "smoke", _py("print(1)"))
    checks.run_declared(project, "smoke")

    assert registry.delete_project(project["slug"]) is True

    assert checks.list_checks(project["id"]) == []
    assert checks.history(project["id"]) == []


# --------------------------------------------------------------------------- #
# Durable operator confirmation
# --------------------------------------------------------------------------- #
def test_a_confirmation_is_one_shot_and_releasable(tmp_path):
    project = _project(tmp_path)
    row = checks.record_confirmation(
        project["id"], "prop_x", "tests/v.py", "a" * 64, attention_id=7)

    assert checks.claim_confirmation(row["id"]) is True
    assert checks.claim_confirmation(row["id"]) is False
    checks.release_confirmation(row["id"])
    assert checks.claim_confirmation(row["id"]) is True


def test_a_confirmation_expires(tmp_path):
    project = _project(tmp_path)
    row = checks.record_confirmation(
        project["id"], "prop_x", "tests/v.py", "b" * 64, attention_id=8)
    assert checks.get_confirmation(
        project["id"], "prop_x", "tests/v.py", "b" * 64) is not None

    with registry.db.db() as conn:
        conn.execute("UPDATE check_confirmations SET expires_at=0 WHERE id=?",
                     (row["id"],))

    assert checks.get_confirmation(
        project["id"], "prop_x", "tests/v.py", "b" * 64) is None

def test_terminal_control_sequences_are_stripped(tmp_path):
    project = _project(tmp_path)
    checks.declare(project, "ctl",
                   _py("print('\\x1b]0;pwned\\x07ok\\x08')"))

    result = checks.run_declared(project, "ctl")

    assert "ok" in result["output"]
    assert "\x1b" not in result["output"]
    assert "\x07" not in result["output"]
    assert "\x08" not in result["output"]


def test_extra_environment_is_passed_to_the_check(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.delenv("CHECK_EXTRA", raising=False)
    checks.declare(
        project, "env-plus",
        _py("import os; print(os.environ.get('CHECK_EXTRA', 'MISSING'))"),
        env={"CHECK_EXTRA": "from-declaration"},
    )

    result = checks.run_declared(project, "env-plus")

    assert "from-declaration" in result["output"]


def test_an_invalid_environment_key_is_rejected(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(checks.CheckError, match="environment key"):
        checks.declare(project, "bad-env", _py("print(1)"),
                       env={"NOT A KEY": "x"})


def test_old_runs_are_pruned_per_project(tmp_path):
    project = _project(tmp_path)
    with registry.db.db() as conn:
        for i in range(checks.KEEP_RUNS + 5):
            conn.execute(
                "INSERT INTO check_runs (project_id, check_name, source, command, "
                "status, timeout_seconds, started_epoch, created_at) "
                "VALUES (?,?,?,?, 'ok', 120, ?, ?)",
                (project["id"], "smoke", "declared", "x", time.time(),
                 registry.now_iso()),
            )

    checks.reap_stale(project["id"])

    with registry.db.db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM check_runs WHERE project_id=?",
            (project["id"],),
        ).fetchone()["n"]
    assert count == checks.KEEP_RUNS


def test_a_machine_wide_cap_refuses_another_run(tmp_path):
    project = _project(tmp_path)
    checks.declare(project, "smoke", _py("print(1)"))
    busy_projects = []
    for i in range(checks.MAX_CONCURRENT_RUNS):
        other = tmp_path / f"cap{i}"
        other.mkdir()
        busy_projects.append(registry.add_project(f"cap{i}", str(other)))
    with registry.db.db() as conn:
        for busy in busy_projects:
            conn.execute(
                "INSERT INTO check_runs (project_id, check_name, source, command, "
                "status, timeout_seconds, started_epoch, created_at) "
                "VALUES (?,?,?,?, 'running', 120, ?, ?)",
                (busy["id"], "smoke", "declared", "x", time.time(),
                 registry.now_iso()),
            )

    with pytest.raises(checks.CheckBusy, match="already running"):
        checks.run_declared(project, "smoke")


def test_powershell_and_batch_targets_derive_commands(tmp_path):
    project = _project(tmp_path)
    tests = tmp_path / "proj" / "tests"
    tests.mkdir()
    (tests / "v.ps1").write_text("Write-Output ok\n", encoding="utf-8")
    (tests / "v.bat").write_text("@echo ok\n", encoding="utf-8")

    ps = checks.derive_proposal_verification(
        project, _proposal(targets=["tests/v.ps1"]))
    bat = checks.derive_proposal_verification(
        project, _proposal(targets=["tests/v.bat"]))

    assert "powershell" in ps["argv"][0].lower()
    assert "cmd" in bat["argv"][0].lower()

def test_recording_the_same_confirmation_keeps_one_row(tmp_path):
    project = _project(tmp_path)
    first = checks.record_confirmation(
        project["id"], "prop_x", "tests/v.py", "d" * 64, attention_id=1)
    second = checks.record_confirmation(
        project["id"], "prop_x", "tests/v.py", "d" * 64, attention_id=2)

    assert first["id"] == second["id"]
    assert second["attention_id"] == 2
    with registry.db.db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM check_confirmations WHERE proposal_id=?",
            ("prop_x",),
        ).fetchone()["n"]
    assert count == 1
