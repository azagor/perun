"""Tests for the dashboard's Focus control (raise the run's terminal window).

Window activation itself is Windows-only and cannot run in CI; the window
candidate ranking and every gating rule are pure and covered here, and the
endpoint is exercised with the activation layer stubbed.
"""

from __future__ import annotations

import pytest

from perun import registry, terminal_focus


def _win(hwnd, pid, title="Terminal", order=0, cls="ConsoleWindowClass", visible=True):
    return {"hwnd": hwnd, "pid": pid, "title": title, "class": cls,
            "order": order, "visible": visible}


class _Kernel:
    @staticmethod
    def GetConsoleWindow():
        return 0


# --------------------------------------------------------------------------- #
# Candidate ranking (pure)
# --------------------------------------------------------------------------- #
def test_rank_prefers_the_shell_then_an_ancestor_then_a_descendant():
    windows = [
        _win(1, 900, "unrelated"),
        _win(2, 300, "conhost child"),
        _win(3, 200, "windows terminal"),
        _win(4, 100, "the shell"),
    ]
    ranked = terminal_focus._rank_candidates(
        windows, shell_pid=100, ancestors={200}, descendants={300}, hints=[])

    assert [w["hwnd"] for _, w in ranked] == [4, 3, 2]


def test_rank_uses_a_title_hint_only_without_a_process_relation():
    windows = [
        _win(1, 700, "Perun: perun - docs"),
        _win(2, 800, "something else"),
    ]
    ranked = terminal_focus._rank_candidates(
        windows, shell_pid=100, ancestors=set(), descendants=set(),
        hints=["perun: perun"], strong_hints=["perun: perun"])

    assert [w["hwnd"] for _, w in ranked] == [1]

    # A real process relation always beats a title-only match.
    ranked = terminal_focus._rank_candidates(
        windows + [_win(3, 100, "shell")], shell_pid=100,
        ancestors=set(), descendants=set(), hints=["perun: perun"],
        strong_hints=["perun: perun"])
    assert ranked[0][1]["hwnd"] == 3


def test_a_bare_project_name_is_not_enough_for_a_title_only_match():
    """A browser tab can mention the project; only strong hints or a terminal
    window may be picked."""
    browser = [_win(1, 900, "Perun - Overseer - Google Chrome",
                    cls="Chrome_WidgetWin_1")]
    ranked = terminal_focus._rank_candidates(
        browser, shell_pid=100, ancestors=set(), descendants=set(),
        hints=["perun", "perun: perun"], strong_hints=["perun: perun"])
    assert ranked == []

    # The launch marker (or the session title) is strong enough.
    ranked = terminal_focus._rank_candidates(
        [_win(2, 900, "Perun: perun - docs")], shell_pid=100,
        ancestors=set(), descendants=set(), hints=["perun"],
        strong_hints=["perun: perun"])
    assert [w["hwnd"] for _, w in ranked] == [2]


def test_rank_excludes_the_own_console_and_invisible_windows():
    windows = [
        _win(1, 100, "perun console"),
        _win(2, 100, "hidden", visible=False),
        _win(3, 100, "real"),
    ]
    ranked = terminal_focus._rank_candidates(
        windows, shell_pid=100, ancestors=set(), descendants=set(),
        hints=[], exclude=(1,))

    assert [w["hwnd"] for _, w in ranked] == [3]


def test_rank_prefers_a_title_match_within_the_same_tier():
    windows = [
        _win(1, 200, "windows terminal", order=0),
        _win(2, 200, "Perun: focus-proj", order=1),
    ]
    ranked = terminal_focus._rank_candidates(
        windows, shell_pid=100, ancestors={200}, descendants=set(),
        hints=["perun: focus-proj"])

    assert ranked[0][1]["hwnd"] == 2


# --------------------------------------------------------------------------- #
# Gating: what may be focused
# --------------------------------------------------------------------------- #
def test_a_headless_run_has_no_window(monkeypatch):
    monkeypatch.setattr(terminal_focus.registry, "get_run",
                        lambda uid: {"run_uid": uid, "standalone": 0})

    out = terminal_focus.focus_run_terminal("abc")

    assert out["focused"] is False
    assert "TUI" in out["reason"]


def test_an_unknown_run_is_reported(monkeypatch):
    monkeypatch.setattr(terminal_focus.registry, "get_run", lambda uid: None)

    assert terminal_focus.focus_run_terminal("nope")["focused"] is False


def test_an_unsupported_platform_degrades(monkeypatch):
    monkeypatch.setattr(terminal_focus.registry, "get_run",
                        lambda uid: {"run_uid": uid, "standalone": 1})
    monkeypatch.setattr(terminal_focus, "supported", lambda: False)

    out = terminal_focus.focus_run_terminal("abc")

    assert out["focused"] is False
    assert "Windows" in out["reason"]


def test_without_an_attributable_terminal_nothing_is_focused(monkeypatch):
    monkeypatch.setattr(terminal_focus.registry, "get_run",
                        lambda uid: {"run_uid": uid, "standalone": 1, "pid": 4242})
    monkeypatch.setattr(terminal_focus, "supported", lambda: True)
    monkeypatch.setattr(terminal_focus.process_tracker.TRACKER,
                        "run_terminal_pid", lambda uid: None)

    out = terminal_focus.focus_run_terminal("abc")

    assert out["focused"] is False
    assert "attributable" in out["reason"]


def test_focus_raises_the_best_window(monkeypatch):
    monkeypatch.setattr(
        terminal_focus.registry, "get_run",
        lambda uid: {"run_uid": uid, "standalone": 1, "pid": 100,
                     "project_id": 0, "prompt": "hello world"})
    monkeypatch.setattr(terminal_focus, "supported", lambda: True)
    monkeypatch.setattr(terminal_focus.process_tracker.TRACKER,
                        "run_terminal_pid", lambda uid: 100)
    monkeypatch.setattr(terminal_focus, "_relation_sets", lambda pid: (set(), set()))
    monkeypatch.setattr(terminal_focus, "_list_windows",
                        lambda: [_win(1, 100, "pwsh"), _win(2, 900, "other")])
    monkeypatch.setattr(terminal_focus, "_api", lambda: (None, _Kernel()))
    monkeypatch.setattr(terminal_focus, "_activate",
                        lambda hwnd: ("setforegroundwindow", True))

    out = terminal_focus.focus_run_terminal("abc")

    assert out["focused"] is True
    assert out["hwnd"] == 1
    assert out["pid"] == 100


def test_a_blocked_foreground_switch_reports_the_flash(monkeypatch):
    monkeypatch.setattr(
        terminal_focus.registry, "get_run",
        lambda uid: {"run_uid": uid, "standalone": 1, "pid": 100,
                     "project_id": 0, "prompt": ""})
    monkeypatch.setattr(terminal_focus, "supported", lambda: True)
    monkeypatch.setattr(terminal_focus.process_tracker.TRACKER,
                        "run_terminal_pid", lambda uid: 100)
    monkeypatch.setattr(terminal_focus, "_relation_sets", lambda pid: (set(), set()))
    monkeypatch.setattr(terminal_focus, "_list_windows", lambda: [_win(1, 100, "pwsh")])
    monkeypatch.setattr(terminal_focus, "_api", lambda: (None, _Kernel()))
    monkeypatch.setattr(terminal_focus, "_activate", lambda hwnd: ("flash", False))

    out = terminal_focus.focus_run_terminal("abc")

    assert out["focused"] is False
    assert "flashed" in out["reason"]


def test_focus_is_windows_only_for_now():
    import os

    assert terminal_focus.supported() is (
        os.name == "nt" and terminal_focus.psutil is not None)


def test_window_enumeration_smoke():
    """Exercise the ctypes prototypes without activating anything."""
    if not terminal_focus.supported():
        pytest.skip("window enumeration needs Windows and psutil")
    windows = terminal_focus._list_windows()
    assert isinstance(windows, list)
    for window in windows:
        assert isinstance(window["hwnd"], int) and window["hwnd"] > 0
        assert isinstance(window["pid"], int)
        assert isinstance(window["title"], str) and window["title"]


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as c:
        yield c


def test_focus_endpoint_returns_the_tracker_result(client, monkeypatch, tmp_path):
    from perun import server, terminal_focus

    (tmp_path / "focus-api").mkdir()
    project = registry.add_project("focus-api", str(tmp_path / "focus-api"), kind="dir")
    registry.create_run("focus-run", project["id"], "hi", standalone=True)

    monkeypatch.setattr(terminal_focus, "focus_run_terminal",
                        lambda uid: {"focused": True, "method": "attachthreadinput",
                                     "pid": 4242, "hwnd": 7})
    r = client.post("/api/runs/focus-run/terminal/focus")

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["focused"] is True
    assert body["pid"] == 4242


def test_focus_endpoint_404_for_an_unknown_run(client):
    assert client.post("/api/runs/nope/terminal/focus").status_code == 404


def test_active_runs_reports_focus_support(client, monkeypatch):
    from perun import terminal_focus

    monkeypatch.setattr(terminal_focus, "supported", lambda: False)
    body = client.get("/api/runs/active").json()
    assert body["focus_supported"] is False


def test_repeated_focus_cycles_between_equally_good_windows(monkeypatch):
    """Windows Terminal windows share one process; clicking again must cycle."""
    monkeypatch.setattr(
        terminal_focus.registry, "get_run",
        lambda uid: {"run_uid": uid, "standalone": 1, "pid": 100,
                     "project_id": 0, "prompt": ""})
    monkeypatch.setattr(terminal_focus, "supported", lambda: True)
    monkeypatch.setattr(terminal_focus.process_tracker.TRACKER,
                        "run_terminal_pid", lambda uid: 100)
    monkeypatch.setattr(terminal_focus, "_relation_sets", lambda pid: (set(), set()))
    monkeypatch.setattr(
        terminal_focus, "_list_windows",
        lambda: [_win(1, 100, "first"), _win(2, 100, "second")])
    monkeypatch.setattr(terminal_focus, "_api", lambda: (None, _Kernel()))
    monkeypatch.setattr(terminal_focus, "_activate",
                        lambda hwnd: ("setforegroundwindow", True))
    terminal_focus._last_choice.clear()

    first = terminal_focus.focus_run_terminal("cycle")
    second = terminal_focus.focus_run_terminal("cycle")

    assert first["ambiguous"] is True and second["ambiguous"] is True
    assert {first["hwnd"], second["hwnd"]} == {1, 2}

class _FakeSessionAPI:
    ready = True

    def __init__(self, title):
        self.title = title

    def session(self, sid, *, timeout=None):
        assert sid == "ses_x"
        return {"title": self.title}


def test_title_hints_include_the_opencode_session_title(monkeypatch):
    """Windows Terminal shows the session title, not our launch marker."""
    monkeypatch.setattr(terminal_focus.opencode_api, "default_api",
                        lambda: _FakeSessionAPI("Perun path spaces and psutil"))
    monkeypatch.setattr(terminal_focus.registry, "get_project",
                        lambda ref: {"slug": "perun", "name": "Perun"})
    terminal_focus._session_titles.clear()

    hints, strong = terminal_focus._title_hints({
        "project_id": 1, "opencode_session_id": "ses_x", "prompt": "",
    })

    assert "Perun path spaces and psutil" in strong
    assert "Perun path spaces and psutil" in hints


def test_title_hints_skip_an_unready_service(monkeypatch):
    """A Focus click must never trigger CLI-based service discovery."""
    class _Unready:
        ready = False

        def session(self, sid, *, timeout=None):  # pragma: no cover - must not be called
            raise AssertionError("discovery would block the click")

    monkeypatch.setattr(terminal_focus.opencode_api, "default_api",
                        lambda: _Unready())
    monkeypatch.setattr(terminal_focus.registry, "get_project",
                        lambda ref: {"slug": "perun", "name": "Perun"})
    terminal_focus._session_titles.clear()

    hints, _strong = terminal_focus._title_hints({"project_id": 1,
                                                 "opencode_session_id": "ses_x"})
    assert "perun" in hints


def test_a_terminal_class_title_match_is_the_last_resort():
    """Windows Terminal hosts a pseudoconsole: the title is all we get."""
    windows = [
        _win(1, 900, "OC | Perun path spaces", cls="CASCADIA_HOSTING_WINDOW_CLASS"),
        _win(2, 901, "Perun - Overseer", cls="Chrome_WidgetWin_1"),
    ]
    ranked = terminal_focus._rank_candidates(
        windows, shell_pid=100, ancestors=set(), descendants=set(),
        hints=["perun"], strong_hints=[])

    assert [w["hwnd"] for _, w in ranked] == [1]

def test_a_failing_session_lookup_does_not_break_the_hints(monkeypatch):
    class _Broken:
        ready = True

        def session(self, sid, *, timeout=None):
            raise RuntimeError("service exploded")

    monkeypatch.setattr(terminal_focus.opencode_api, "default_api",
                        lambda: _Broken())
    monkeypatch.setattr(terminal_focus.registry, "get_project",
                        lambda ref: {"slug": "perun", "name": "Perun"})
    terminal_focus._session_titles.clear()

    hints, strong = terminal_focus._title_hints({"project_id": 1,
                                                 "opencode_session_id": "ses_broken"})

    assert "perun: perun" in strong
    assert "perun" in hints


def test_the_session_title_is_cached_between_clicks(monkeypatch):
    calls = []

    class _Counting:
        ready = True

        def session(self, sid, *, timeout=None):
            calls.append(sid)
            return {"title": "Cached title"}

    monkeypatch.setattr(terminal_focus.opencode_api, "default_api",
                        lambda: _Counting())
    monkeypatch.setattr(terminal_focus.registry, "get_project",
                        lambda ref: {"slug": "perun", "name": "Perun"})
    terminal_focus._session_titles.clear()
    run = {"project_id": 1, "opencode_session_id": "ses_cached"}

    first = terminal_focus._title_hints(run)
    second = terminal_focus._title_hints(run)

    assert "Cached title" in first[1]
    assert "Cached title" in second[1]
    assert calls == ["ses_cached"]


def test_short_weak_hints_do_not_pick_a_terminal():
    """A 2-letter project name is too ambiguous for the title fallback."""
    windows = [_win(1, 900, "ai-playground - Windows Terminal",
                    cls="CASCADIA_HOSTING_WINDOW_CLASS")]
    ranked = terminal_focus._rank_candidates(
        windows, shell_pid=100, ancestors=set(), descendants=set(),
        hints=["ai"], strong_hints=[])

    assert ranked == []

def test_a_failed_session_lookup_is_not_cached(monkeypatch):
    """A miss must not stick for the TTL: the service can come up before the next click."""
    calls = []

    class _Flaky:
        ready = True
        fail = True

        def session(self, sid, *, timeout=None):
            calls.append(sid)
            if self.fail:
                raise RuntimeError("service warming up")
            return {"title": "Back online"}

    api = _Flaky()
    monkeypatch.setattr(terminal_focus.opencode_api, "default_api", lambda: api)
    monkeypatch.setattr(terminal_focus.registry, "get_project",
                        lambda ref: {"slug": "perun", "name": "Perun"})
    terminal_focus._session_titles.clear()
    run = {"project_id": 1, "opencode_session_id": "ses_flaky"}

    assert terminal_focus._title_hints(run)[1] == ["perun: perun"]
    api.fail = False
    hints, strong = terminal_focus._title_hints(run)

    assert "Back online" in strong
    assert calls == ["ses_flaky", "ses_flaky"]
