"""Bring the terminal window that hosts a run's TUI session to the foreground.

The dashboard's project card offers a **Focus** button while a TUI run is
active. Multiple terminals are easy to confuse, so the button has to raise the
one window that actually hosts *that* run.

Attribution comes from :meth:`process_tracker.ProcessTracker.run_terminal_pid`,
which honours the same ``PERUN_RUN_UID`` marker Stop uses: a recycled PID must
never point the operator at an unrelated program. Focusing is non-destructive,
but the same evidence is required.

Windows only for now; other platforms return a clear reason and the dashboard
hides the button (``/api/runs/active`` reports ``focus_supported``).

The window match is a best effort by design:

* a terminal launched with ``CREATE_NEW_CONSOLE`` owns its window through a
  ``conhost``/``OpenConsole`` **child** of the shell;
* Windows Terminal hosts the tab in an **ancestor** (``WindowsTerminal.exe``);
* several Windows Terminal tabs share one window, so raising the window is the
  best a public API allows. A title hint (the launcher writes ``Perun: <slug>``,
  and OpenCode may replace it with the session title) picks the right window
  when the OS exposes it.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import wintypes
from typing import Any, Iterable, Sequence

from . import process_tracker, registry

try:  # optional: the tracker degrades without psutil, so must focusing
    import psutil  # type: ignore
except Exception:  # pragma: no cover - depends on the interpreter
    psutil = None  # type: ignore[assignment]

LOG = logging.getLogger("perun.focus")

SW_RESTORE = 9
GW_OWNER = 4
FLASHW_ALL = 0x00000003
FLASHW_TIMERNOFG = 0x0000000C
MIN_WINDOW_PIXELS = 40

_user32: Any = None
_kernel32: Any = None


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]


def supported() -> bool:
    """Whether this platform can raise another process's terminal window.

    ``psutil`` is required for the pid relation (ancestor/descendant windows),
    so a Windows install without it hides the button instead of offering an
    action that would mostly fail.
    """
    return os.name == "nt" and psutil is not None


def _api() -> tuple[Any, Any]:
    """Load and prototype user32/kernel32 once (64-bit HWND-safe)."""
    global _user32, _kernel32
    if _user32 is not None and _kernel32 is not None:
        return _user32, _kernel32
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [
        wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
    user32.FlashWindowEx.restype = wintypes.BOOL
    if hasattr(user32, "SwitchToThisWindow"):
        user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        user32.SwitchToThisWindow.restype = None

    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetConsoleWindow.argtypes = []
    kernel32.GetConsoleWindow.restype = wintypes.HWND

    _user32, _kernel32 = user32, kernel32
    return user32, kernel32


def _window_title(hwnd: int) -> str:
    user32, _ = _api()
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def _window_class(hwnd: int) -> str:
    user32, _ = _api()
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _list_windows() -> list[dict[str, Any]]:
    """Every visible top-level window with a title, in z-order (top first)."""
    user32, _ = _api()
    windows: list[dict[str, Any]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _collect(hwnd: int, _lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _window_title(hwnd)
            if not title:
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width < MIN_WINDOW_PIXELS or height < MIN_WINDOW_PIXELS:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            windows.append({
                "hwnd": int(hwnd),
                "pid": int(pid.value),
                "title": title,
                "class": _window_class(hwnd),
                "order": len(windows),
                "visible": True,
            })
        except Exception:  # pragma: no cover - a window can vanish mid-enum
            pass
        return True

    try:
        user32.EnumWindows(callback_type(_collect), 0)
    except Exception:  # pragma: no cover - defensive
        LOG.debug("could not enumerate windows", exc_info=True)
    return windows


def _relation_sets(shell_pid: int) -> tuple[set[int], set[int]]:
    """Ancestors and descendants of *shell_pid* (empty without psutil)."""
    if psutil is None:
        return set(), set()
    try:
        proc = psutil.Process(shell_pid)
    except Exception:
        return set(), set()
    ancestors: set[int] = set()
    descendants: set[int] = set()
    try:
        ancestors = {p.pid for p in proc.parents()}
    except Exception:
        pass
    try:
        descendants = {c.pid for c in proc.children(recursive=True)}
    except Exception:
        pass
    return ancestors, descendants


def _matches_title(title: str, hints: Sequence[str]) -> bool:
    lowered = title.lower()
    return any(h and h.lower() in lowered for h in hints)


def _rank_candidates(windows: Iterable[dict[str, Any]], *,
                     shell_pid: int, ancestors: set[int],
                     descendants: set[int], hints: Sequence[str],
                     strong_hints: Sequence[str] = (),
                     exclude: Iterable[int] = ()) -> list[tuple[tuple[int, int, int],
                                                                    dict[str, Any]]]:
    """Score windows as hosts of *shell_pid*'s terminal; best first (pure).

    Tier 0 is the shell itself, 1 an ancestor (Windows Terminal or an IDE that
    owns the terminal), 2 a descendant (conhost/OpenConsole). A title-only match
    is tier 3 and only used when the process tree cannot answer, so it requires
    a *strong* hint (the launch marker or the run's session title) - a bare
    project name could just as easily be a browser tab. Lower tiers always win;
    within a tier the title hint decides, then z-order.
    """
    excluded = {int(h) for h in exclude}
    strong = tuple(strong_hints or ())
    ranked: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    for window in windows:
        if not window.get("visible", True):
            continue
        if int(window.get("hwnd", 0)) in excluded:
            continue
        title = str(window.get("title") or "")
        if not title:
            continue
        pid = int(window.get("pid") or 0)
        title_match = _matches_title(title, hints)
        if pid == shell_pid:
            tier = 0
        elif pid in ancestors:
            tier = 1
        elif pid in descendants:
            tier = 2
        elif _matches_title(title, strong):
            tier = 3
        else:
            continue
        score = (tier, 0 if title_match else 1, int(window.get("order", 0)))
        ranked.append((score, window))
    ranked.sort(key=lambda item: item[0])
    return ranked


def _activate(hwnd: int) -> tuple[str, bool]:
    """Raise *hwnd*; returns ``(method, focused)`` and flashes on refusal."""
    user32, kernel32 = _api()
    method = "setforegroundwindow"
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            method = "restore+setforegroundwindow"
        if user32.SetForegroundWindow(hwnd):
            return method, True

        # Windows refuses SetForegroundWindow unless the caller owns the
        # foreground. Attaching to the foreground thread lifts that restriction.
        foreground = user32.GetForegroundWindow()
        foreground_thread = 0
        if foreground:
            thread_id = wintypes.DWORD()
            foreground_thread = int(user32.GetWindowThreadProcessId(
                foreground, ctypes.byref(thread_id)) or thread_id.value)
        current_thread = int(kernel32.GetCurrentThreadId() or 0)
        if foreground_thread and current_thread and foreground_thread != current_thread:
            attached = bool(user32.AttachThreadInput(
                foreground_thread, current_thread, True))
            try:
                user32.BringWindowToTop(hwnd)
                if user32.SetForegroundWindow(hwnd):
                    return "attachthreadinput", True
            finally:
                if attached:
                    user32.AttachThreadInput(foreground_thread, current_thread, False)

        switch = getattr(user32, "SwitchToThisWindow", None)
        if switch is not None:
            switch(hwnd, True)
            if user32.GetForegroundWindow() == hwnd:
                return "switchtothiswindow", True
    except Exception:  # pragma: no cover - API availability varies
        LOG.debug("foreground activation failed", exc_info=True)

    try:
        info = FLASHWINFO(
            ctypes.sizeof(FLASHWINFO), hwnd,
            FLASHW_ALL | FLASHW_TIMERNOFG, 3, 0)
        user32.FlashWindowEx(ctypes.byref(info))
        return "flash", False
    except Exception:  # pragma: no cover - defensive
        return "unavailable", False


def _title_hints(run: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``(all hints, strong hints)`` that identify this run's terminal.

    A bare project name is too weak to pick a window on its own (a browser tab
    can contain it). The launcher's ``Perun: <slug>`` marker and the run's
    prompt/session title are strong enough for a title-only match.
    """
    hints: list[str] = []
    strong: list[str] = []
    project = registry.get_project(int(run.get("project_id") or 0)) or {}
    for value in (project.get("slug"), project.get("name")):
        text = str(value or "").strip()
        if text:
            marker = f"perun: {text}"
            hints.extend([marker, text])
            strong.append(marker)
            break
    prompt = " ".join(str(run.get("prompt") or "").split())
    if len(prompt) >= 8:
        snippet = prompt[:40]
        hints.append(snippet)
        strong.append(snippet)
    return hints, strong


_choice_lock = threading.Lock()
_last_choice: dict[str, int] = {}


def _pick_candidate(run_uid: str,
                    ranked: list[tuple[tuple[int, int, int], dict[str, Any]]]
                    ) -> tuple[dict[str, Any], bool]:
    """Choose the best window, cycling through equally good ones.

    Several Windows Terminal windows share one ``WindowsTerminal.exe`` process,
    so the process tree cannot separate them; a title hint usually can. When it
    cannot, repeated Focus clicks walk the tied candidates instead of always
    raising the same window.
    """
    best_score, best = ranked[0]
    tied = [window for score, window in ranked if score[:2] == best_score[:2]]
    if len(tied) <= 1:
        return best, False
    with _choice_lock:
        previous = _last_choice.get(run_uid)
        index = next((i for i, window in enumerate(tied)
                      if int(window.get("hwnd", 0)) == previous), -1)
        chosen = tied[(index + 1) % len(tied)]
        _last_choice[run_uid] = int(chosen.get("hwnd", 0))
    return chosen, True


def focus_run_terminal(run_uid: str) -> dict[str, Any]:
    """Raise the window that hosts *run_uid*'s TUI terminal.

    Returns ``{"focused": bool, "reason": str, "method": str, ...}``. Never
    raises for the expected "no window" cases; the dashboard toasts the reason.
    """
    run = registry.get_run(run_uid) or {}
    if not run:
        return {"focused": False, "reason": "no such run"}
    if not run.get("standalone"):
        return {"focused": False, "reason": "not a TUI session"}
    if not supported():
        return {"focused": False,
                "reason": "focusing a terminal window is only supported on Windows"}

    pid = process_tracker.TRACKER.run_terminal_pid(run_uid)
    if not pid:
        return {"focused": False,
                "reason": "no attributable terminal for this run"}

    hints, strong_hints = _title_hints(run)
    ancestors, descendants = _relation_sets(pid)
    windows = _list_windows()
    _, kernel32 = _api()
    try:
        own_console = int(kernel32.GetConsoleWindow() or 0)
    except Exception:  # pragma: no cover - defensive
        own_console = 0
    ranked = _rank_candidates(
        windows, shell_pid=pid, ancestors=ancestors, descendants=descendants,
        hints=hints, strong_hints=strong_hints, exclude=(own_console,))
    if not ranked:
        return {"focused": False, "pid": pid,
                "reason": "no terminal window found for this run"}

    window, ambiguous = _pick_candidate(run_uid, ranked)
    method, focused = _activate(int(window["hwnd"]))
    if focused:
        return {"focused": True, "pid": pid, "hwnd": int(window["hwnd"]),
                "method": method, "title": window["title"],
                "ambiguous": ambiguous}
    if method == "flash":
        return {"focused": False, "pid": pid, "hwnd": int(window["hwnd"]),
                "method": method, "title": window["title"],
                "ambiguous": ambiguous,
                "reason": "Windows blocked the foreground switch; "
                          "the terminal button was flashed"}
    return {"focused": False, "pid": pid, "method": method,
            "reason": "the terminal window could not be activated"}
