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
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Iterable, Sequence

from . import opencode_api, process_tracker, registry

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

#: Window classes that host a terminal. A title-only match is allowed for these
#: with a *weak* hint (the project name), because Windows Terminal hosts the
#: console in a pseudoconsole: the shell is not its child and the window is not
#: in the shell's process tree, so the title is the only signal.
_TERMINAL_WINDOW_CLASSES = {
    "cascadia_hosting_window_class",
    "consolewindowclass",
    "virtualconsoleclass",
    "mintty",
    "conemu",
}

#: A weak (project-name) hint shorter than this is too ambiguous to pick a
#: terminal window on its own (e.g. "ai" inside "ai-playground").
MIN_WEAK_HINT = 3

#: Session titles change rarely; cache them so repeated Focus clicks do not hit
#: the service each time.
SESSION_TITLE_TTL = 30.0
_session_titles: dict[str, tuple[float, str]] = {}
_session_titles_lock = threading.Lock()

#: ``AttachConsole`` is process-wide, so the server must never call it (that
#: would detach its own console). A throwaway detached process asks the shell
#: for its console window and reports the owner instead.
CONSOLE_QUERY_TIMEOUT = 2.5
CONSOLE_HWND_TTL = 30.0
_console_hwnd: dict[int, tuple[float, int]] = {}
_console_hwnd_lock = threading.Lock()

_CONSOLE_HWND_SCRIPT = r"""
import ctypes, sys
from ctypes import wintypes
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32.FreeConsole()
kernel32.AttachConsole.argtypes = [wintypes.DWORD]
kernel32.AttachConsole.restype = wintypes.BOOL
kernel32.GetConsoleWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
if not kernel32.AttachConsole(int(sys.argv[1])):
    sys.exit(3)
hwnd = int(kernel32.GetConsoleWindow() or 0)
if not hwnd:
    sys.exit(4)
print(int(user32.GetWindow(hwnd, 4) or 0) or hwnd)
"""

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
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
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


def _read_console_hwnd(pid: int) -> int:
    """Ask a detached helper for *pid*'s console window handle (0 if none).

    ``AttachConsole`` is process-wide: attaching inside the server would drop
    its own console, so the query runs in a throwaway process. It is a local,
    read-only call; the pid is passed as an integer argument, never through a
    shell.
    """
    exe = sys.executable or ""
    if not exe or os.name != "nt" or getattr(sys, "frozen", False):
        return 0
    try:
        proc = subprocess.run(
            [exe, "-c", _CONSOLE_HWND_SCRIPT, str(int(pid))],
            capture_output=True, text=True, timeout=CONSOLE_QUERY_TIMEOUT,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
    except Exception:  # pragma: no cover - depends on the host
        LOG.debug("console window probe failed for pid %s", pid, exc_info=True)
        return 0
    if proc.returncode != 0:
        return 0
    lines = (proc.stdout or "").strip().splitlines()
    try:
        return int(lines[-1]) if lines else 0
    except (TypeError, ValueError):
        return 0


def _terminal_hwnd(hwnd: int) -> int:
    """The visible terminal window for a console handle, or 0.

    A classic console owns its window directly; a pseudoconsole (Windows 11
    default terminal) returns a hidden ConPTY window whose **owner** is the
    Windows Terminal window hosting the tab. Only a real terminal-class window
    is accepted, so a stale handle can never raise an unrelated program.
    """
    if not hwnd:
        return 0
    user32, _ = _api()
    try:
        owner = int(user32.GetWindow(hwnd, GW_OWNER) or 0)
        target = owner or int(hwnd)
        if not target or not user32.IsWindow(target):
            return 0
        if not user32.IsWindowVisible(target):
            return 0
    except Exception:  # pragma: no cover - defensive
        return 0
    if _window_class(target).lower() not in _TERMINAL_WINDOW_CLASSES:
        return 0
    return target


def console_host_window(pid: int) -> int:
    """The window hosting *pid*'s console, resolved through the console itself.

    This is the deterministic link the process tree cannot give on Windows 11:
    the shell's parent is a ConPTY host that exits, so the terminal window is
    neither an ancestor nor a descendant. The console handle is stable for the
    life of the tab, and its owner is the exact Windows Terminal window - no
    title, project name or OpenCode service call needed.

    Cached briefly because repeated Focus clicks are common and the window does
    not move between them.
    """
    pid = int(pid or 0)
    if pid <= 0:
        return 0
    now = time.monotonic()
    with _console_hwnd_lock:
        cached = _console_hwnd.get(pid)
    if cached and now - cached[0] < CONSOLE_HWND_TTL:
        if _terminal_hwnd(cached[1]):
            return cached[1]
        # The window closed (or its handle was recycled): ask again.
        with _console_hwnd_lock:
            _console_hwnd.pop(pid, None)
    hwnd = _terminal_hwnd(_read_console_hwnd(pid))
    # Cache a hit only: a miss may be transient (the tab is still starting).
    if hwnd:
        with _console_hwnd_lock:
            _console_hwnd[pid] = (now, hwnd)
    return hwnd


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
    is tier 3 when a *strong* hint matches (the launch marker or the run's
    session title). Tier 4 is the last resort: a **terminal-class** window whose
    title carries even a weak hint (the project name) - Windows Terminal hosts a
    pseudoconsole, so its window is in neither the shell's ancestors nor its
    descendants, and the tab title is all we get. A browser window with the same
    words never qualifies. Lower tiers always win; within a tier the title hint
    decides, then z-order.
    """
    excluded = {int(h) for h in exclude}
    strong = tuple(strong_hints or ())
    weak = tuple(h for h in hints if len(h) >= MIN_WEAK_HINT)
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
        window_class = str(window.get("class") or "").lower()
        title_match = _matches_title(title, hints)
        if pid == shell_pid:
            tier = 0
        elif pid in ancestors:
            tier = 1
        elif pid in descendants:
            tier = 2
        elif _matches_title(title, strong):
            tier = 3
        elif window_class in _TERMINAL_WINDOW_CLASSES and _matches_title(title, weak):
            tier = 4
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


def _session_title(session_id: str) -> str:
    """The OpenCode session title, cached briefly ("" when unavailable).

    Windows Terminal shows this title in the tab, so it is the strongest signal
    when a pseudoconsole puts the window outside the shell's process tree. The
    per-call timeout keeps a Focus click responsive even if the service is slow.
    """
    if not session_id:
        return ""
    now = time.monotonic()
    with _session_titles_lock:
        cached = _session_titles.get(session_id)
        if cached and now - cached[0] < SESSION_TITLE_TTL:
            return cached[1]
    title = ""
    try:
        api = opencode_api.default_api()
        if api.ready:  # never trigger CLI discovery from a Focus click
            info = api.session(session_id, timeout=1.0) or {}
            title = " ".join(str(info.get("title") or "").split())[:120]
    except Exception:  # pragma: no cover - service unavailable
        LOG.debug("could not read the session title", exc_info=True)
    if title:  # never cache a miss: the service may come up between clicks
        with _session_titles_lock:
            _session_titles[session_id] = (now, title)
    return title


def _title_hints(run: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``(all hints, strong hints)`` that identify this run's terminal.

    A bare project name is too weak to pick a window on its own (a browser tab
    can contain it). The launcher's ``Perun: <slug>`` marker and the run's
    **OpenCode session title** are strong: Windows Terminal's tab title shows
    the session title, and it replaces the marker once the session starts.
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
    session_id = str(run.get("opencode_session_id") or "").strip()
    title = _session_title(session_id)
    if len(title) >= 4:
        hints.append(title)
        strong.append(title[:60])
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

    # The server's own console must never be raised for a run: `GetConsoleWindow`
    # is the window this process is attached to.
    _, kernel32 = _api()
    try:
        own_console = int(kernel32.GetConsoleWindow() or 0)
    except Exception:  # pragma: no cover - defensive
        own_console = 0

    # Deterministic path first: the console itself names the hosting window. On
    # Windows 11 the shell has no live process-tree link to Windows Terminal (the
    # ConPTY host exits), and the tab title belongs to OpenCode, so title
    # matching is only a fallback.
    direct = console_host_window(pid)
    if direct and direct != own_console:
        title = _window_title(direct)
        method, focused = _activate(direct)
        if focused:
            return {"focused": True, "pid": pid, "hwnd": direct,
                    "method": method, "title": title, "ambiguous": False}
        if method == "flash":
            return {"focused": False, "pid": pid, "hwnd": direct,
                    "method": method, "title": title, "ambiguous": False,
                    "reason": "Windows blocked the foreground switch; "
                              "the terminal button was flashed"}
        # An unexpected activation failure: fall through to the ranked search.

    hints, strong_hints = _title_hints(run)
    ancestors, descendants = _relation_sets(pid)
    windows = _list_windows()
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
