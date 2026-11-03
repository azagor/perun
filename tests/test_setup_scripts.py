"""Smoke tests for the web installers (setup.ps1 / setup.sh)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_setup_ps1_exists_and_parses():
    path = REPO_ROOT / "setup.ps1"
    assert path.is_file()
    # Sanity-check the public parameters are documented.
    content = path.read_text(encoding="utf-8")
    assert "InstallDir" in content
    assert "RepoUrl" in content
    assert "azagor/perun" in content

    ps = shutil.which("powershell")
    if not ps:
        pytest.skip("powershell not available")

    # Parse the script without executing it.
    cmd = (
        f"try {{ [scriptblock]::Create((Get-Content -Raw '{path}')) }} "
        "catch { Write-Output $_.Exception.Message; exit 1 }"
    )
    res = subprocess.run(
        [ps, "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_setup_sh_exists_and_parses():
    path = REPO_ROOT / "setup.sh"
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert "--install-dir" in content
    assert "azagor/perun" in content

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")

    # Some Windows machines have a WSL stub that cannot launch bash.
    probe = subprocess.run([bash, "--version"], capture_output=True, text=True, timeout=10, check=False)
    if probe.returncode != 0 or "bash" not in (probe.stdout + probe.stderr).lower():
        pytest.skip("usable bash not available")

    res = subprocess.run(
        [bash, "-n", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr


# --------------------------------------------------------------------------- #
# Perun.cmd: the double-click launcher
# --------------------------------------------------------------------------- #
def test_launcher_never_puts_the_interpreter_in_a_for_f_backtick():
    """cmd strips the outer quotes of a `for /f` backtick command.

    A machine-wide install runs "C:\\Program Files\\Python312\\python.exe"; inside
    ``for /f ... in (`%PY% ...`)`` that line reaches cmd as C:\\Program and the
    window prints "'C:\\Program' is not recognized as an internal or external
    command" on every launch. Direct calls keep the quotes, so the launcher must
    only use exit codes for those probes.
    """
    content = (REPO_ROOT / "Perun.cmd").read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in content.splitlines()
        if "for /f" in line.lower()
        and "%PY%" in line
        and not line.lstrip().lower().startswith("rem")
    ]
    assert not offenders, offenders


def test_launcher_repairs_missing_python_dependencies():
    content = (REPO_ROOT / "Perun.cmd").read_text(encoding="utf-8")

    assert "PERUN_SKIP_DEP_CHECK" in content
    assert "requirements.txt" in content
    assert "-m pip install" in content
    # A failed system install must be retried per-user before giving up.
    assert "--user" in content


def test_launcher_dependency_check_covers_requirements():
    """Keep the launcher's hardcoded package list in step with requirements.txt."""
    import re

    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = [
        re.split(r"[<>=!;\[ ]", line.strip(), maxsplit=1)[0]
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith(("#", "-"))
    ]
    content = (REPO_ROOT / "Perun.cmd").read_text(encoding="utf-8")
    check_line = next(line for line in content.splitlines() if "ns='" in line)
    declared = check_line.split("ns='", 1)[1].split("'", 1)[0].split()

    assert names == declared, "update the package list in Perun.cmd"


def test_launcher_honours_an_explicit_perun_port():
    content = (REPO_ROOT / "Perun.cmd").read_text(encoding="utf-8")
    port_block = content.split('set "PORT=8760"', 1)[1].split('set "URL=', 1)[0]

    assert port_block.index("if defined PERUN_PORT") < port_block.index("if exist")


def test_installer_verifies_python_dependencies():
    content = (REPO_ROOT / "installer" / "install-prereqs.ps1").read_text(encoding="utf-8")

    assert "Test-PerunDependencies" in content
    assert "--user" in content
    assert "requirements.txt" in content
    # pip output must reach installer.log, not disappear into a hidden console.
    assert "pip: $line" in content


def test_installer_dependency_check_covers_requirements():
    """Keep the installer's hardcoded package list in step with requirements.txt."""
    import re

    requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = [
        re.split(r"[<>=!;\[ ]", line.strip(), maxsplit=1)[0]
        for line in requirements.splitlines()
        if line.strip() and not line.strip().startswith(("#", "-"))
    ]
    content = (REPO_ROOT / "installer" / "install-prereqs.ps1").read_text(encoding="utf-8")
    check_line = next(line for line in content.splitlines() if 'ns="' in line)
    declared = check_line.split('ns="', 1)[1].split('"', 1)[0].split()

    assert names == declared, "update the package list in install-prereqs.ps1"
