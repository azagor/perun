"""Machine-install tests.

Covers the parts that are easy to get silently wrong: the ``PerunBridge`` task
XML (the single-instance guarantees), the ``schtasks`` invocation, prerequisite
gating, ``pip`` invocation, and the bootstrap script's delegation contract.

Nothing here touches the real OpenCode config or the real scheduled task — the
global install is always skipped and ``schtasks`` is faked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

from perun import config, installer

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _completed(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
    return subprocess.CompletedProcess(args=["schtasks"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _parse(xml: str) -> ElementTree.Element:
    """Parse the declarative XML (UTF-16 declaration, so hand it matching bytes)."""
    return ElementTree.fromstring(xml.encode("utf-16"))


# --------------------------------------------------------------------------- #
# Helpers / platform
# --------------------------------------------------------------------------- #
def test_is_windows_matches_platform():
    assert installer.is_windows() == (os.name == "nt")


def test_python_version_gate():
    assert installer.python_version_ok((3, 10)) is True
    assert installer.python_version_ok((3, 14)) is True
    assert installer.python_version_ok((3, 9)) is False
    assert installer.python_version_ok((2, 7)) is False


def test_decode_console_handles_utf16_and_plain():
    assert installer._decode_console("héllo".encode("utf-16")) == "héllo"
    assert installer._decode_console(b"plain ascii") == "plain ascii"
    assert installer._decode_console(b"") == ""


def test_requirements_file_points_into_repo():
    req = installer.requirements_file()
    assert req.name == "requirements.txt"
    assert req.is_file(), "the repo ships a requirements.txt"


def test_python_exe_honours_perun_python(monkeypatch, tmp_path):
    fake = tmp_path / "python.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("PERUN_PYTHON", str(fake))
    assert installer._python_exe() == str(fake.resolve())


def test_python_exe_ignores_bogus_perun_python(monkeypatch, tmp_path):
    monkeypatch.setenv("PERUN_PYTHON", str(tmp_path / "does-not-exist.exe"))
    assert Path(installer._python_exe()).name.lower().startswith("python")


def test_task_user_combines_domain(monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "ATVI")
    monkeypatch.setenv("USERNAME", "bob")
    assert installer._task_user() == "ATVI\\bob"
    monkeypatch.delenv("USERDOMAIN")
    monkeypatch.setenv("USERNAME", "bob")
    assert installer._task_user() == "bob"


# --------------------------------------------------------------------------- #
# Prerequisites
# --------------------------------------------------------------------------- #
def test_check_prerequisites_shape(monkeypatch):
    monkeypatch.setattr(installer, "ollama_status", lambda *a, **k: (False, "n/a"))
    checks = installer.check_prerequisites()
    names = {c["name"] for c in checks}
    assert {"python", "pip", "git", "opencode", "ollama"} <= names
    for c in checks:
        assert set(c) == {"name", "ok", "required", "detail"}
    py = next(c for c in checks if c["name"] == "python")
    assert py["ok"] is True and py["required"] is True
    # Ollama is advisory, never required.
    assert next(c for c in checks if c["name"] == "ollama")["required"] is False


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
def test_pip_install_builds_command(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _completed(0, stdout=b"Successfully installed fastapi")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    res = installer.pip_install()
    assert res["ok"] is True
    assert captured["cmd"][:3] == [sys.executable, "-m", "pip"]
    assert "-r" in captured["cmd"]
    assert captured["cmd"][-1].endswith("requirements.txt")


def test_pip_install_reports_failure(monkeypatch):
    monkeypatch.setattr(installer.subprocess, "run",
                        lambda cmd, **kw: _completed(1, stderr=b"ERROR: no network"))
    res = installer.pip_install()
    assert res["ok"] is False
    assert "no network" in res["output_tail"]


# --------------------------------------------------------------------------- #
# Bridge task XML
# --------------------------------------------------------------------------- #
def test_bridge_task_xml_is_wellformed_and_single_instance():
    xml = installer.bridge_task_xml(
        python_exe=r"C:\Python314\python.exe",
        workdir=r"C:\my_apps\perun",
        user=r"ATVI\lszczepankowski",
    )
    root = _parse(xml)
    assert root.tag.endswith("Task")

    assert root.findtext("t:RegistrationInfo/t:URI", namespaces=NS) == r"\PerunBridge"
    assert "getUpdates" in (root.findtext("t:RegistrationInfo/t:Description", namespaces=NS) or "")

    # The three guarantees that keep the Telegram poller singular / alive.
    assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=NS) == "IgnoreNew"
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=NS) == "PT0S"
    assert root.findtext("t:Settings/t:RestartOnFailure/t:Count", namespaces=NS) == "999"
    assert root.findtext("t:Settings/t:StartWhenAvailable", namespaces=NS) == "true"

    # Logon trigger for the right user.
    assert root.find(".//t:LogonTrigger", NS) is not None
    assert root.findtext(".//t:LogonTrigger/t:UserId", namespaces=NS) == r"ATVI\lszczepankowski"

    # The action is exactly `python -m perun bridge` in the repo.
    exec_el = root.find(".//t:Actions/t:Exec", NS)
    assert exec_el is not None
    assert exec_el.findtext("t:Command", namespaces=NS) == r"C:\Python314\python.exe"
    assert exec_el.findtext("t:Arguments", namespaces=NS) == "-m perun bridge"
    assert exec_el.findtext("t:WorkingDirectory", namespaces=NS) == r"C:\my_apps\perun"


def test_bridge_task_xml_escapes_special_paths():
    xml = installer.bridge_task_xml(
        python_exe=r"C:\R&D\py.exe", workdir=r"C:\a&b", user=r"A&B\c",
    )
    root = _parse(xml)  # raises if the '&' was not escaped
    assert root.find(".//t:Actions/t:Exec/t:Command", NS).text == r"C:\R&D\py.exe"
    assert root.find(".//t:Actions/t:Exec/t:WorkingDirectory", NS).text == r"C:\a&b"
    assert root.findtext(".//t:LogonTrigger/t:UserId", namespaces=NS) == r"A&B\c"


def test_bridge_task_xml_defaults_to_this_repo_and_user(monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "DOM")
    monkeypatch.setenv("USERNAME", "me")
    root = _parse(installer.bridge_task_xml(python_exe="py.exe"))
    assert root.find(".//t:Actions/t:Exec/t:WorkingDirectory", NS).text == str(config.REPO_ROOT)
    assert root.findtext(".//t:LogonTrigger/t:UserId", namespaces=NS) == "DOM\\me"


# --------------------------------------------------------------------------- #
# Registering / removing the task
# --------------------------------------------------------------------------- #
def test_register_bridge_task_skips_on_non_windows(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: False)
    res = installer.register_bridge_task()
    assert res == {"ok": False, "skipped": True, "reason": "the bridge task is Windows-only"}


def test_register_bridge_task_writes_utf16_xml_and_creates(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(installer, "_python_exe", lambda: r"C:\Python314\python.exe")

    calls: list[list[str]] = []
    captured: list[bytes] = []

    def fake(args, timeout=60):
        calls.append(list(args))
        path = Path(args[args.index("/XML") + 1])
        captured.append(path.read_bytes())
        return _completed(0)

    monkeypatch.setattr(installer, "_run_schtasks", fake)

    res = installer.register_bridge_task()
    assert res["ok"] is True
    assert calls[0][0] == "/Create"
    assert installer.TASK_NAME in calls[0]
    assert "/F" in calls[0]  # idempotent overwrite

    # The XML is written as UTF-16 (what schtasks /Create /XML expects) ...
    assert captured[0][:2] in (b"\xff\xfe", b"\xfe\xff")
    assert "PerunBridge" in captured[0].decode("utf-16")
    # ... and the temp file is cleaned up.
    assert not Path(calls[0][calls[0].index("/XML") + 1]).exists()


def test_register_bridge_task_start_runs_it(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(installer, "_python_exe", lambda: "py.exe")
    calls: list[list[str]] = []

    def fake(args, timeout=60):
        calls.append(list(args))
        return _completed(0)

    monkeypatch.setattr(installer, "_run_schtasks", fake)
    res = installer.register_bridge_task(start=True)
    assert res["started"] is True
    assert calls[-1][:2] == ["/Run", "/TN"]
    assert calls[-1][2] == installer.TASK_NAME


def test_register_bridge_task_reports_failure(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(installer, "_python_exe", lambda: "py.exe")
    monkeypatch.setattr(installer, "_run_schtasks",
                        lambda a, timeout=60: _completed(1, stderr=b"ERROR: Access is denied."))
    res = installer.register_bridge_task()
    assert res["ok"] is False
    assert "Access is denied" in res["reason"]


def test_remove_bridge_task_noop_when_absent(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(installer, "bridge_task_exists", lambda: False)
    res = installer.remove_bridge_task()
    assert res["ok"] is True and res["removed"] is False


def test_remove_bridge_task_deletes_when_present(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(installer, "bridge_task_exists", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(installer, "_run_schtasks",
                        lambda a, timeout=60: (calls.append(list(a)), _completed(0))[1])
    res = installer.remove_bridge_task()
    assert res["removed"] is True
    assert calls[0][:1] == ["/Delete"]


# --------------------------------------------------------------------------- #
# Task status
# --------------------------------------------------------------------------- #
def test_bridge_task_status_parses_xml(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    xml = installer.bridge_task_xml(python_exe=r"C:\py.exe", workdir=r"C:\perun", user=r"d\u")
    monkeypatch.setattr(installer, "_run_schtasks",
                        lambda a, timeout=60: _completed(0, stdout=xml.encode("utf-16")))
    status = installer.bridge_task_status()
    assert status["exists"] is True
    assert status["command"] == r"C:\py.exe"
    assert status["arguments"] == "-m perun bridge"
    assert status["workdir"] == r"C:\perun"


def test_bridge_task_status_absent(monkeypatch):
    monkeypatch.setattr(installer, "is_windows", lambda: True)
    monkeypatch.setattr(installer, "_run_schtasks", lambda a, timeout=60: _completed(1))
    status = installer.bridge_task_status()
    assert status["exists"] is False
    assert "command" not in status


def _ok_check():
    return [{"name": "python", "ok": True, "required": True, "detail": "3.14"}]


def test_machine_install_all_skips_is_clean(monkeypatch, capsys):
    monkeypatch.setattr(installer, "check_prerequisites", _ok_check)
    registered: list[bool] = []
    monkeypatch.setattr(installer, "register_bridge_task",
                        lambda start=False: (registered.append(start), {"ok": True})[1])

    rc = installer.machine_install(skip_deps=True, skip_global=True, skip_task=True)
    assert rc == 0
    assert registered == []  # --skip-task must not register anything
    out = capsys.readouterr().out
    assert "Perun is installed" in out


def test_machine_install_aborts_on_required_failure(monkeypatch, capsys):
    monkeypatch.setattr(installer, "check_prerequisites",
                        lambda: [{"name": "python", "ok": False, "required": True, "detail": "3.6"}])
    rc = installer.machine_install(skip_deps=True, skip_global=True, skip_task=True)
    assert rc == 1
    assert "aborting" in capsys.readouterr().out


def test_machine_install_registers_and_starts_with_token(monkeypatch, capsys):
    monkeypatch.setattr(installer, "check_prerequisites", _ok_check)
    monkeypatch.setattr(config.Settings, "load", classmethod(
        lambda cls: SimpleNamespace(telegram=lambda: ("123:abc", ["1"]), port=8760)))
    seen: list[bool] = []
    monkeypatch.setattr(installer, "register_bridge_task", lambda start=False: (
        seen.append(start),
        {"ok": True, "python": "py.exe", "workdir": "C:\\perun", "started": start},
    )[1])

    rc = installer.machine_install(skip_deps=True, skip_global=True)
    assert rc == 0
    assert seen == [True]  # token configured -> start the bridge now
    assert "bridge  started" in capsys.readouterr().out


def test_machine_install_registers_without_token(monkeypatch, capsys):
    monkeypatch.setattr(installer, "check_prerequisites", _ok_check)
    monkeypatch.setattr(config.Settings, "load", classmethod(
        lambda cls: SimpleNamespace(telegram=lambda: ("", []), port=8760)))
    seen: list[bool] = []
    monkeypatch.setattr(installer, "register_bridge_task", lambda start=False: (
        seen.append(start),
        {"ok": True, "python": "py.exe", "workdir": "C:\\perun", "started": False},
    )[1])

    rc = installer.machine_install(skip_deps=True, skip_global=True)
    assert rc == 0
    assert seen == [False]  # no token -> do not start a poller
    assert "next logon" in capsys.readouterr().out


def test_machine_install_skips_deps_but_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(installer, "check_prerequisites", _ok_check)
    monkeypatch.setattr(installer, "pip_install",
                        lambda *a, **k: {"ok": False, "output_tail": "boom", "summary": "", "command": []})
    rc = installer.machine_install(skip_global=True, skip_task=True)
    assert rc == 1
    assert "--skip-deps" in capsys.readouterr().out


def test_uninstall_machine_reports(monkeypatch, capsys):
    monkeypatch.setattr(installer, "remove_bridge_task", lambda: {"ok": True, "removed": True})
    assert installer.uninstall_machine() == 0
    assert "removed" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# CLI + bootstrap contract
# --------------------------------------------------------------------------- #
def test_cli_install_accepts_machine_flags():
    from perun.__main__ import build_parser

    args = build_parser().parse_args(
        ["install", "--machine", "--skip-deps", "--skip-global", "--skip-task", "--no-index"])
    assert args.machine is True
    assert args.skip_deps is True
    assert args.skip_global is True
    assert args.skip_task is True
    assert args.no_index is True
    assert args.func.__name__ == "cmd_install"


def test_status_includes_bridge_task(monkeypatch, capsys):
    from perun.__main__ import build_parser

    monkeypatch.setattr(installer, "bridge_task_status",
                        lambda: {"windows": True, "exists": True, "command": "py.exe"})
    args = build_parser().parse_args(["status"])
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["bridge_task"]["exists"] is True


def test_bootstrap_script_delegates_to_machine_install():
    script = config.REPO_ROOT / "bootstrap.ps1"
    assert script.is_file(), "bootstrap.ps1 ships at the repo root"
    text = script.read_text(encoding="utf-8")
    assert "install" in text and "--machine" in text
    assert "PERUN_PYTHON" in text          # honours / sets the interpreter override
    assert "requirements.txt" in text       # documents the dependency step
    assert "schtasks" not in text.lower()   # delegation: never touches the task itself
    assert "Perun.cmd" in text              # shortcuts the one-click launcher
