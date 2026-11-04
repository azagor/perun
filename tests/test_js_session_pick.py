"""Runs the Node.js unit test for the dashboard's session-choice rules."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_DIR = REPO_ROOT / "web" / "js"


def test_session_pick_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    res = subprocess.run(
        [node, str(JS_DIR / "session_pick.test.js")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_dashboard_loads_session_pick_before_perun_js():
    """The pure rule must be a script tag, and above perun.js."""
    page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    srcs = re.findall(r'<script[^>]*src="([^"]+)"', page)
    names = [s.split("?", 1)[0] for s in srcs]
    assert "js/session_pick.js" in names, names
    assert names.index("js/session_pick.js") < names.index("js/perun.js"), names
