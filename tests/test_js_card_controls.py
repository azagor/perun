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


def test_perun_js_offers_the_focus_control_from_the_module():
    """The Focus button is gated in card_controls.js (standalone + pid)."""
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "CardControls.focusControlSpec(" in src
    assert "data-focus" in src
    # A stale cached module must not crash the card renderer.
    assert "focusControlSpec: () => null" in src


def test_perun_js_filters_presence_to_listed_projects():
    """Hidden projects must not light a card or appear in "Working now"."""
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "CardControls.visibleRuns(" in src
    # A stale cached module must not crash the presence renderer.
    assert "visibleRuns: (runs, projects) =>" in src

def test_the_card_run_is_resume_latest_with_a_new_or_choose_menu():
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "CardControls.runMenuSpec(" in src
    assert "resumeLatestProjectTui(p)" in src
    assert "data-tui-menu" in src
    assert "Resume latest" in src
    assert "Choose an existing session" in src
    # A stale cached card_controls.js must not crash the card renderer.
    assert "runMenuSpec: (run) =>" in src
    # The run record and `opencode --session` both receive the chosen id.
    assert "opencode_session_id: sessionId" in src
    assert "session_pk: sessionPk" in src


def test_the_console_defaults_its_session_picker_to_the_latest():
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "const latest = [...sel.options].find" in src
    assert "if (latest) sel.value = latest.value;" in src


def test_the_card_run_button_is_disabled_while_the_session_lookup_runs():
    """A double click must not launch two terminals."""
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "if (btn.disabled) return;" in src
    assert "if (btn.isConnected) btn.disabled = false;" in src


def test_the_console_skips_sessions_without_an_opencode_id():
    """A session with no OpenCode id cannot be resumed."""
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "if (!String(s.opencode_session_id || '').trim()) continue;" in src


def test_terminal_launches_are_single_flight_and_finalized_on_failure():
    """A re-render or a second click must not open a second terminal, and a
    failed launch must not leave a phantom running card."""
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "const tuiLaunchInFlight = new Set();" in src
    assert "if (tuiLaunchInFlight.has(p.slug)) return;" in src
    assert "tuiLaunchInFlight.delete(p.slug);" in src
    assert "if (!opened)" in src
    assert "await api(`/api/runs/${runUid}/finish`" in src


def test_the_resume_path_skips_headless_sessions():
    """A machine-created session must not be auto-resumed as a chat."""
    src = (JS_DIR / "perun.js").read_text(encoding="utf-8")
    assert "SessionPick.latestResumeCandidate" in src
    assert "SessionPick.originLabel" in src
    assert "o.dataset.tui !== 'false'" in src
    # A stale cached session_pick.js must not crash the resume path.
    assert "latestResumeCandidate: (sessions) =>" in src
