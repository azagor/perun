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
