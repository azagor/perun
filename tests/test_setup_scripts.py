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
