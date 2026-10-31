"""Runs the Node.js unit tests for the dashboard chat log decisions."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_DIR = REPO_ROOT / "web" / "js"


def test_chat_core_js():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    res = subprocess.run(
        [node, "--test", str(JS_DIR / "chat_core.test.js")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr


def test_dashboard_loads_chat_core_before_perun_js():
    """chat_core.js must be a script tag, and above perun.js."""
    page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    srcs = re.findall(r'<script[^>]*src="([^"]+)"', page)
    names = [s.split("?", 1)[0] for s in srcs]
    assert "js/chat_core.js" in names, names
    assert names.index("js/chat_core.js") < names.index("js/perun.js"), names


def test_perun_js_has_no_private_copies_of_the_guards():
    """The predicates must only live in chat_core.js."""
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "ChatCore.shouldRender(" in src
    assert "ChatCore.shouldSend(" in src
    # The old inline debounce: `text.trim() === _lastSentText && now - _lastSentAt`
    assert "_lastSentText && now" not in src
