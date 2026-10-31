"""Track externally-launched TUI/terminal processes and their lifecycle.

When the dashboard opens OpenCode (or any long-lived terminal session) for a
project, Perun only fires-and-forgets a ``subprocess.Popen`` call. This module
remembers the launched process(es) so Perun can:

* detect when the user closes the TUI,
* explicitly terminate the window from the dashboard Stop button,
* auto-finalize the matching run record.

Process tracking is best-effort. On Windows, ``wt.exe`` exits immediately after
spawning ``WindowsTerminal.exe``; we resolve that child by walking the process
tree. ``psutil`` is used when available; without it we fall back to the raw
``Popen`` handle.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except Exception:  # pragma: no cover - optional dependency
    _HAS_PSUTIL = False

from . import registry


class ProcessTracker:
    """In-memory registry of launched TUI sessions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._watcher: threading.Thread | None = None

    def register(self, run_uid: str, proc: subprocess.Popen, *, shell: str = "") -> None:
        """Store a Popen handle for a TUI session."""
        with self._lock:
            self._entries[run_uid] = {
                "proc": proc,
                "pid": proc.pid,
                "shell": (shell or "").lower(),
                "run_uid": run_uid,
                "started_at": time.time(),
            }
        # Persist the pid so a restart can still tell whether the terminal is
        # open (the in-memory entry does not survive a restart).
        try:
            registry.update_run(run_uid, pid=int(proc.pid))
        except Exception:  # pragma: no cover - tracking is best effort
            pass
        self._ensure_watcher()

    def get(self, run_uid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._entries.get(run_uid)

    def is_alive(self, run_uid: str) -> bool:
        entry = self.get(run_uid)
        if not entry:
            # After a restart the in-memory entry is gone; fall back to the pid
            # persisted when the terminal was launched.
            run = registry.get_run(run_uid) or {}
            pid = int(run.get("pid") or 0)
            return registry.pid_alive(pid) if pid else False
        proc = entry.get("proc")
        if proc and proc.poll() is None:
            return True
        # Popen handle died; try to find a spawned child (e.g. Windows Terminal).
        return bool(self._resolve_child_pid(entry))

    def terminate(self, run_uid: str) -> bool:
        """Kill the process tree for a tracked session and finalize its run."""
        entry = self.get(run_uid)
        if not entry:
            # Explicit Stop after a restart: use the persisted pid.
            run = registry.get_run(run_uid) or {}
            pid = int(run.get("pid") or 0)
            if not pid or not registry.pid_alive(pid):
                return False
            self._kill_tree(pid)
            self.finalize(run_uid)
            return True
        pid = self._resolve_child_pid(entry) or entry.get("pid")
        if pid:
            self._kill_tree(pid)
        # Also terminate the stored Popen handle if still alive.
        proc = entry.get("proc")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                time.sleep(0.3)
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        self.finalize(run_uid)
        return True

    def terminate_all(self) -> int:
        """Kill every tracked terminal process (explicit teardown only).

        Deliberately **not** called on server shutdown: those terminals belong to
        the user, not to Perun, and a restart must leave them open.
        """
        with self._lock:
            entries = list(self._entries.items())
        killed = 0
        for run_uid, entry in entries:
            pid = self._resolve_child_pid(entry) or entry.get("pid")
            if pid:
                try:
                    self._kill_tree(pid)
                    killed += 1
                except Exception:  # pragma: no cover - best effort
                    pass
            proc = entry.get("proc")
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:  # pragma: no cover - best effort
                    pass
            with self._lock:
                self._entries.pop(run_uid, None)
        return killed

    def finalize(self, run_uid: str) -> None:
        """Stop tracking and mark the run record as done if it is still active."""
        with self._lock:
            if run_uid not in self._entries:
                return
            del self._entries[run_uid]
        run = registry.get_run(run_uid)
        if run and run.get("status") in ("queued", "running"):
            registry.update_run(run_uid, status="done",
                                ended_at=registry.now_iso(), pid=0)

    def _resolve_child_pid(self, entry: dict[str, Any]) -> int | None:
        """Find the real interactive process when the launcher exited early."""
        if not _HAS_PSUTIL:
            return None
        shell = entry.get("shell", "")
        parent_pid = entry.get("pid")
        run_uid = entry.get("run_uid")
        if not parent_pid:
            return None
        try:
            parent = psutil.Process(parent_pid)
        except psutil.NoSuchProcess:
            parent = None
        # Walk descendants of the known parent first; fall back to a full
        # scan only when the parent is gone or no descendant matches.
        candidate_pids: list[int] = []
        fallback_to_all = False
        if parent:
            try:
                candidate_pids = [c.pid for c in parent.children(recursive=True)]
            except Exception:
                candidate_pids = []
            if not candidate_pids:
                fallback_to_all = True
        else:
            fallback_to_all = True

        def _scan_marker(pids: list[int]) -> list[tuple[int, Any]]:
            """Return processes carrying the PERUN_RUN_UID marker."""
            matching: list[tuple[int, Any]] = []
            if not run_uid:
                return matching
            marker = f"PERUN_RUN_UID={run_uid}"
            for pid in pids:
                try:
                    p = psutil.Process(pid)
                    if run_uid in (p.environ() or {}).get("PERUN_RUN_UID", ""):
                        matching.append((0, p))
                        continue
                    cmd = " ".join(p.cmdline() or [])
                    if marker in cmd or run_uid in cmd:
                        matching.append((1, p))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return matching

        def _best_marker_match(pids: list[int]) -> int | None:
            matching = _scan_marker(pids)
            if not matching:
                return None
            def _depth(proc: Any) -> int:
                d = 0
                try:
                    for _ in proc.children(recursive=True):
                        d += 1
                except Exception:
                    pass
                return d
            matching.sort(key=lambda x: (x[0], -_depth(x[1])))
            return matching[0][1].pid

        result = _best_marker_match(candidate_pids)
        if result is not None:
            return result
        if fallback_to_all:
            result = _best_marker_match([p.pid for p in psutil.process_iter()])
            if result is not None:
                return result

        # 2) Windows Terminal parent/creation-time heuristic.
        if "wt" in shell:
            cutoff = entry.get("started_at", time.time()) - 5
            pids_to_check = candidate_pids if candidate_pids else (
                [p.pid for p in psutil.process_iter()] if fallback_to_all else [])
            candidates = []
            for pid in pids_to_check:
                try:
                    p = psutil.Process(pid)
                    if "WindowsTerminal" not in (p.name() or ""):
                        continue
                    create_time = p.create_time()
                    ppid = p.ppid()
                    rank = 2
                    if ppid == parent_pid:
                        rank = 0
                    elif parent and ppid == parent.pid:
                        rank = 1
                    elif create_time < cutoff:
                        continue
                    candidates.append((rank, pid, p))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            # Prefer candidates that actually have a shell child.
            for rank, pid, proc in sorted(candidates, key=lambda x: x[0]):
                try:
                    children = proc.children()
                    if any(
                        "cmd" in (c.name() or "").lower()
                        or "powershell" in (c.name() or "").lower()
                        or "pwsh" in (c.name() or "").lower()
                        for c in children
                    ):
                        return pid
                except Exception:
                    continue
            # Fallback to the highest-priority candidate even without validation.
            if candidates:
                return sorted(candidates, key=lambda x: x[0])[0][1]
        return None

    def _kill_tree(self, pid: int) -> None:
        if not _HAS_PSUTIL:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=15,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except Exception:
                    pass
            return
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        try:
            children = proc.children(recursive=True)
        except Exception:
            children = []
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass
        gone, alive = psutil.wait_procs(children + [proc], timeout=3)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass

    def _ensure_watcher(self) -> None:
        if self._watcher and self._watcher.is_alive():
            return
        self._watcher = threading.Thread(target=self._watch_loop, daemon=True)
        self._watcher.start()

    def _watch_loop(self) -> None:
        while True:
            time.sleep(3)
            with self._lock:
                uids = list(self._entries.keys())
            for uid in uids:
                if not self.is_alive(uid):
                    self.finalize(uid)


# Module-level singleton.
TRACKER = ProcessTracker()
