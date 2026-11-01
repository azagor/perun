"""Runs the Node.js unit tests for the dashboard's project-card decisions."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_DIR = REPO_ROOT / "web" / "js"


def test_card_controls_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    res = subprocess.run(
        [node, "--test", str(JS_DIR / "card_controls.test.js")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_dashboard_loads_card_controls_before_perun_js():
    """The pure card rule must be a script tag, and above perun.js."""
    page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    srcs = re.findall(r'<script[^>]*src="([^"]+)"', page)
    names = [s.split("?", 1)[0] for s in srcs]
    assert "js/card_controls.js" in names, names
    assert names.index("js/card_controls.js") < names.index("js/perun.js"), names


def test_perun_js_renders_the_run_control_from_the_module():
    """The Run/Stop decision lives in card_controls.js, not in perun.js."""
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "CardControls.runControlSpec(" in src
    # The bug: a hardcoded Run button next to an always-rendered run action.
    assert "data-tui title=\"Run OpenCode" not in src
