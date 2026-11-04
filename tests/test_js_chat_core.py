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


def test_the_chat_fallback_covers_every_chat_core_method():
    """A missing chat_core.js must not throw at a call the shim lacks."""
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    shim = src.split("const ChatCore = window.ChatCore || {", 1)[1].split("};", 1)[0]
    for name in set(re.findall(r"ChatCore\.(\w+)\(", src)):
        assert f"{name}:" in shim, name


def test_proposal_focus_runs_after_the_card_is_inserted():
    """The focus must run after replaceWith, or querySelector finds nothing.

    Regression: focusing inside proposalsCard (before the caller inserts the
    card) silently consumed pendingProposalFocus and never landed on the row.
    """
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    insertion = src.index("proposalsPlaceholder.replaceWith(proposalsCardEl);")
    focus = src.index("if (pendingProposalFocus)", insertion)
    assert focus > insertion
    card = src[src.index("async function proposalsCard("):
               src.index("function projectConfigForm(")]
    assert "pendingProposalFocus = null" not in card


def test_object_payloads_render_as_a_json_preview():
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "function chatPayloadView" in src
    assert "chat-structured" in src
    assert "String(msg.payload)" not in src


def test_the_operator_inbox_is_a_page_and_surfaced_in_the_chat():
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    page = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "'#/attention': viewAttention" in src
    assert "async function viewAttention" in src
    assert "pc-needs-you" in src
    assert "loadChatNeedsYou" in src
    assert 'data-route="#/attention"' in page

def test_the_chat_banner_orders_room_asks_by_message_time():
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "message_ts" in src
    start = src.index("const items = (res.items || []).filter")
    assert "message_ts" in src[start:start + 700]
