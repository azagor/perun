"""Runs the Node.js unit test for the Agent Console prompt chain."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_prompt_chain_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    test_file = REPO_ROOT / "tests" / "test_prompt_chain.js"
    res = subprocess.run(
        [node, str(test_file)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr

def test_the_coroutine_wizard_composes_selected_project_tasks():
    src = (REPO_ROOT / "web" / "js" / "perun.js").read_text(encoding="utf-8")
    assert "openCoroutineTaskPicker" in src
    assert "coroutineTaskBox" in src
    assert "buildCoroutinePrompt" in src
    assert "prompt: coroutinePrompt(w)" in src
    assert "Project tasks" in src
