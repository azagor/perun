"""Check GitHub for a newer Perun release and install it in place.

Perun ships as a signed Windows installer attached to every GitHub release, and
it is also commonly run straight from a git checkout. Both shapes are updatable:

* **installer** — the running copy has no ``.git`` (the Inno Setup package
  excludes it). The update downloads the release's ``PerunSetup.exe``, verifies
  its published SHA-256 when one exists, and runs it silently.
* **git** — the running copy *is* a checkout. The update is
  ``git fetch`` + ``git pull --ff-only``. It refuses to run when tracked files
  are modified: that work is the user's, not ours to discard.

Replacing the code a server is running from cannot be done from inside that
server, so applying an update is a **two-phase** operation:

1. **Prepare** (this process, server still live) — resolve the release,
   download/verify the installer or fetch the commits, and record progress so
   the dashboard can stream it.
2. **Apply** (a detached runner) — wait for the server to exit, install or
   pull, start a new server, then poll ``/api/health`` until the new version
   answers. The runner is a separate process precisely because it has to
   outlive the process it replaces, and because it must survive a failed
   install to report what went wrong.

Everything is best-effort and never raises into the server: a failed check is a
``{"ok": false, "error": …}`` payload, a failed update is a recorded job state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__, config, daemon

GITHUB_REPO = "azagor/perun"
API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
USER_AGENT = f"Perun/{__version__} (+https://github.com/{GITHUB_REPO})"

#: How long a successful release check stays authoritative. The dashboard polls
#: this endpoint; GitHub's unauthenticated API allows 60 requests/hour per IP.
CHECK_TTL = 1800.0

#: The asset the release workflow publishes for the Windows installer.
INSTALLER_ASSET = "PerunSetup.exe"

#: Job states that mean "do not start another update".
ACTIVE_STATES = frozenset({
    "preparing", "downloading", "verifying", "prepared",
    "waiting", "applying", "restarting", "starting",
})

_STATE_LOCK = threading.RLock()
_CHECK_LOCK = threading.Lock()

_LOG_LIMIT = 200


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _state_path() -> Path:
    return config.HOME / "update-state.json"


def _check_path() -> Path:
    return config.HOME / "update-check.json"


def _plan_path() -> Path:
    return config.HOME / "update-plan.json"


def updates_dir() -> Path:
    """Where downloaded installers and runner logs live."""
    path = config.HOME / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(exc: BaseException, limit: int = 300) -> str:
    text = str(exc) or exc.__class__.__name__
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace *path* with *data* (readers never see half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _creationflags() -> int:
    if os.name == "nt":
        # Never flash a console window for a background git/pip call.
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Version comparison
# --------------------------------------------------------------------------- #
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+](?P<pre>.*))?$", re.IGNORECASE)


def parse_version(text: str | None) -> tuple[int, ...]:
    """``"v0.7.21"`` → ``(0, 7, 21)``; ``None``/junk → ``(0,)``.

    A pre-release tag sorts *below* the matching final release: the last element
    of the key is ``1`` for a final version and ``0`` for ``0.8.0-rc1``.
    """
    match = _VERSION_RE.match(str(text or "").strip())
    if not match:
        return (0,)
    numbers = [int(part) for part in match.group(1).split(".")]
    while len(numbers) < 4:
        numbers.append(0)
    return (*numbers, 0 if match.group("pre") else 1)


def is_newer(latest: str | None, current: str | None) -> bool:
    """True when *latest* is a strictly newer release than *current*."""
    return parse_version(latest) > parse_version(current)


# --------------------------------------------------------------------------- #
# Release checking
# --------------------------------------------------------------------------- #
def _fetch_latest_release(timeout: float = 12.0) -> dict[str, Any]:
    import httpx

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = client.get(API_LATEST)
        if response.status_code == 404:
            raise LookupError(f"no published releases in {GITHUB_REPO}")
        response.raise_for_status()
        return response.json()


def _release_view(raw: dict[str, Any]) -> dict[str, Any]:
    tag = str(raw.get("tag_name") or "").strip()
    assets = []
    for asset in raw.get("assets") or []:
        url = asset.get("browser_download_url")
        if not url:
            continue
        assets.append({
            "name": str(asset.get("name") or ""),
            "size": int(asset.get("size") or 0),
            "url": str(url),
            "content_type": str(asset.get("content_type") or ""),
        })
    return {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "name": raw.get("name") or tag,
        "url": raw.get("html_url") or RELEASES_URL,
        "published_at": raw.get("published_at"),
        "notes": raw.get("body") or "",
        "prerelease": bool(raw.get("prerelease")),
        "assets": assets,
    }


def _check_payload(
    release: dict[str, Any] | None,
    *,
    ok: bool,
    error: str | None,
    checked_at: str | None,
    stale: bool,
) -> dict[str, Any]:
    current = __version__
    latest = (release or {}).get("version") or None
    return {
        "ok": ok,
        "error": error,
        "stale": stale,
        "current": current,
        "latest": latest,
        "update_available": bool(latest) and is_newer(latest, current),
        "release": release,
        "checked_at": checked_at,
        "repo": GITHUB_REPO,
        "releases_url": RELEASES_URL,
        "mode": install_mode(),
    }


def check(refresh: bool = False, timeout: float = 12.0) -> dict[str, Any]:
    """The latest GitHub release, compared with the running version.

    Cached for :data:`CHECK_TTL`. When GitHub cannot be reached the last known
    answer is returned with ``ok: false`` and ``stale: true`` — the dashboard
    can then say "could not check" instead of silently hiding a release.
    """
    with _CHECK_LOCK:
        cached = _read_json(_check_path()) or {}
        cached_release = cached.get("release") if isinstance(cached.get("release"), dict) else None
        checked_at = cached.get("checked_at")
        fresh = (
            cached_release is not None
            and isinstance(cached.get("ts"), (int, float))
            and (time.time() - float(cached["ts"])) < CHECK_TTL
        )
        if fresh and not refresh:
            return _check_payload(cached_release, ok=True, error=None,
                                  checked_at=checked_at, stale=False)
        try:
            release = _release_view(_fetch_latest_release(timeout=timeout))
        except Exception as exc:  # noqa: BLE001 - a check must never raise
            if cached_release is not None:
                return _check_payload(cached_release, ok=False, error=_short(exc),
                                      checked_at=checked_at, stale=True)
            return _check_payload(None, ok=False, error=_short(exc),
                                  checked_at=None, stale=False)
        payload = {"ts": time.time(), "checked_at": _now(), "release": release}
        try:
            _write_json(_check_path(), payload)
        except OSError:
            pass
        return _check_payload(release, ok=True, error=None,
                              checked_at=payload["checked_at"], stale=False)


# --------------------------------------------------------------------------- #
# How is this copy of Perun installed?
# --------------------------------------------------------------------------- #
def install_mode() -> dict[str, Any]:
    """Describe how the running copy can be updated.

    ``git``        — a checkout; update with ``git pull``.
    ``installer``  — an Inno Setup install; update with the release installer.
    ``unsupported``— neither shape is recognised; the user must update by hand.
    """
    root = Path(config.REPO_ROOT)
    if (root / ".git").exists():
        return {
            "mode": "git",
            "root": str(root),
            "label": "git checkout",
            "updatable": True,
            "detail": "Updates with git pull from origin.",
        }
    if os.name == "nt" and (root / "Perun.cmd").exists():
        return {
            "mode": "installer",
            "root": str(root),
            "label": "Windows installer",
            "updatable": True,
            "detail": "Updates with the release installer (Windows may ask for administrator permission).",
        }
    return {
        "mode": "unsupported",
        "root": str(root),
        "label": "manual update required",
        "updatable": False,
        "detail": ("This copy of Perun is neither a git checkout nor a Windows "
                   "installer. Update it with the tool that installed it."),
    }


def _git(root: Path, *args: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_creationflags(),
    )


def git_state(root: str | Path) -> dict[str, Any]:
    """Branch, remote and working-tree state of a checkout."""
    path = Path(root)
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    remote = _git(path, "remote", "get-url", "origin").stdout.strip()
    porcelain = _git(path, "status", "--porcelain").stdout
    dirty: list[str] = []
    untracked: list[str] = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        name = line[3:].strip()
        if line.startswith("??"):
            untracked.append(name)
        else:
            dirty.append(name)
    return {
        "branch": branch,
        "remote": remote,
        "dirty": dirty,
        "untracked": untracked,
        "clean": not dirty,
    }


# --------------------------------------------------------------------------- #
# Job state
# --------------------------------------------------------------------------- #
def _blank_state() -> dict[str, Any]:
    return {
        "job": None,
        "state": "idle",
        "message": "",
        "progress": 0.0,
        "from_version": __version__,
        "to_version": None,
        "mode": None,
        "error": None,
        "log": [],
        "started_at": None,
        "finished_at": None,
    }


def _read_state() -> dict[str, Any]:
    data = _read_json(_state_path()) or {}
    state = _blank_state()
    state.update({k: v for k, v in data.items() if k in state})
    if not isinstance(state.get("log"), list):
        state["log"] = []
    return state


def _write_state(patch: dict[str, Any]) -> dict[str, Any]:
    with _STATE_LOCK:
        state = _read_state()
        state.update(patch)
        _write_json(_state_path(), state)
        return state


def _set_state(**patch: Any) -> dict[str, Any]:
    """Merge *patch* into the persisted job state."""
    with _STATE_LOCK:
        state = _read_state()
        state.update(patch)
        if state["state"] in {"done", "failed"} and not state.get("finished_at"):
            state["finished_at"] = _now()
        _write_json(_state_path(), state)
        return state


def _log(message: str, *, level: str = "info") -> None:
    """Append a progress line to the job state (bounded)."""
    with _STATE_LOCK:
        state = _read_state()
        log = list(state.get("log") or [])
        log.append({"at": _now(), "level": level, "line": message})
        state["log"] = log[-_LOG_LIMIT:]
        state["message"] = message
        _write_json(_state_path(), state)


def status() -> dict[str, Any]:
    """The persisted job plus live facts the dashboard needs to render it."""
    state = _read_state()
    current = __version__
    return {
        "ok": True,
        "current": current,
        "job": state,
        "mode": install_mode(),
        "update_available": bool(
            state.get("to_version") and is_newer(state["to_version"], current)
        ),
        "active": state.get("state") in ACTIVE_STATES,
    }


def dismiss() -> dict[str, Any]:
    """Forget a finished job (so the success banner stops showing)."""
    with _STATE_LOCK:
        state = _read_state()
        if state["state"] in ACTIVE_STATES:
            raise UpdateError("an update is in progress")
        _write_json(_state_path(), _blank_state())
    return status()


# --------------------------------------------------------------------------- #
# Prepare (runs inside the server, while the dashboard is still live)
# --------------------------------------------------------------------------- #
class UpdateError(RuntimeError):
    """A refused or failed update, with a message safe to show a user."""


def _serve_args() -> list[str]:
    """Launch flags for the replacement server, replayed from the live one."""
    recorded = daemon.read_recorded_argv() or []
    args: list[str] = []

    def flag(name: str) -> str | None:
        try:
            index = recorded.index(name)
        except ValueError:
            return None
        if index + 1 < len(recorded):
            return recorded[index + 1]
        return None

    host = flag("--host")
    port = flag("--port")
    if host:
        args += ["--host", host]
    if port:
        args += ["--port", port]
    if "--bridge" in recorded:
        args.append("--bridge")
    return args


def _port() -> int:
    recorded = daemon.read_recorded_argv() or []
    if "--port" in recorded:
        try:
            return int(recorded[recorded.index("--port") + 1])
        except (IndexError, ValueError):
            pass
    try:
        return int(config.Settings.load().port)
    except Exception:
        return 8760


def _pick_asset(release: dict[str, Any], *, installer: bool) -> dict[str, Any]:
    assets = release.get("assets") or []
    for asset in assets:
        name = (asset.get("name") or "").lower()
        if installer and name == INSTALLER_ASSET.lower():
            return asset
    if installer:
        for asset in assets:
            if (asset.get("name") or "").lower().endswith(".exe"):
                return asset
    raise UpdateError(
        f"release {release.get('tag') or ''} has no installer asset "
        f"({INSTALLER_ASSET})"
    )


def _pick_checksum(release: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any] | None:
    wanted = {f"{asset['name']}.sha256".lower(), f"{asset['name']}.sha256sum".lower(),
              "sha256sums.txt", "checksums.txt"}
    for candidate in release.get("assets") or []:
        if (candidate.get("name") or "").lower() in wanted:
            return candidate
    return None


def _download(
    url: str,
    dest: Path,
    *,
    label: str,
    on_progress: Callable[[int, int], None] | None = None,
    timeout: float = 120.0,
) -> Path:
    """Stream *url* to *dest* atomically, reporting ``(done, total)``."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    headers = {"User-Agent": USER_AGENT}
    done = 0
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise UpdateError(f"download failed ({response.status_code}) for {label}")
            total = int(response.headers.get("content-length") or 0)
            with tmp.open("wb") as fh:
                for chunk in response.iter_bytes(64 * 1024):
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
    os.replace(tmp, dest)
    return dest


def _human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _fetch_checksum(checksum_asset: dict[str, Any], asset_name: str) -> str | None:
    """Download a checksum asset and return the hash recorded for *asset_name*."""
    import httpx

    with httpx.Client(timeout=30.0, follow_redirects=True,
                      headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(checksum_asset["url"])
        response.raise_for_status()
        text = response.text
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # "<hash>  <name>" (sha256sum) or just the hash on its own line.
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower() != asset_name.lower():
            if not parts[-1].lstrip("*").lower().endswith(asset_name.lower()):
                continue
        candidate = parts[0].lstrip("*").lower()
        if re.fullmatch(r"[0-9a-f]{64}", candidate):
            return candidate
    return None


def _download_installer(job: str, release: dict[str, Any]) -> Path:
    asset = _pick_asset(release, installer=True)
    dest = updates_dir() / f"{asset['name']}-{release.get('version') or 'latest'}"
    size = asset.get("size") or 0

    state = {"last": 0.0}

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if now - state["last"] < 0.25 and done != total:
            return
        state["last"] = now
        total = total or size or 0
        fraction = (done / total) if total else 0.0
        _set_state(
            state="downloading",
            progress=round(min(0.9, max(0.0, fraction)), 3),
            message=(f"Downloading {asset['name']} — "
                     f"{_human_bytes(done)} / {_human_bytes(total)}"),
        )

    _set_state(state="downloading", progress=0.0,
               message=f"Downloading {asset['name']} ({_human_bytes(size)})…")
    _log(f"Downloading {asset['url']}")
    _download(asset["url"], dest, label=asset["name"], on_progress=progress)
    cleanup_old_downloads()

    _set_state(state="verifying", progress=0.92, message="Verifying the download…")
    checksum_asset = _pick_checksum(release, asset)
    actual = _sha256_file(dest)
    if checksum_asset:
        expected = _fetch_checksum(checksum_asset, asset["name"])
        if not expected:
            _log(f"Checksum asset {checksum_asset['name']} had no entry for "
                 f"{asset['name']}; skipping verification.", level="warn")
        elif expected != actual:
            try:
                dest.unlink()
            except OSError:
                pass
            raise UpdateError(
                f"checksum mismatch for {asset['name']}: expected {expected[:12]}…, "
                f"got {actual[:12]}…"
            )
        else:
            _log(f"SHA-256 verified ({actual[:12]}…).", level="ok")
    else:
        _log("This release publishes no checksum; the download was not verified "
             "beyond HTTPS from GitHub.", level="warn")
    return dest


def _git_fetch(root: str) -> None:
    _set_state(state="preparing", progress=0.2, message="Fetching the latest commits…")
    result = _git(Path(root), "fetch", "--tags", "--prune", "origin", timeout=300)
    if result.returncode != 0:
        raise UpdateError(f"git fetch failed: {_short(result.stderr or result.stdout)}")
    _log("Fetched origin.", level="ok")


def apply(*, confirm: bool = False, force: bool = False, restart: bool = True) -> dict[str, Any]:
    """Start an update job. Returns the job status immediately.

    ``confirm`` is mandatory: replacing the running installation is not
    something that may happen as a side effect of a stray request.
    """
    if not confirm:
        raise UpdateError("refusing to update without confirm=true")

    info = check(refresh=True)
    if not info.get("update_available"):
        raise UpdateError(f"Perun is already up to date (v{__version__})")

    mode = install_mode()
    if not mode.get("updatable"):
        raise UpdateError(mode.get("detail") or "this installation cannot self-update")

    if mode["mode"] == "git" and not force:
        work = git_state(mode["root"])
        if work["dirty"]:
            preview = ", ".join(work["dirty"][:5])
            more = " …" if len(work["dirty"]) > 5 else ""
            raise UpdateError(
                "the Perun checkout has uncommitted changes "
                f"({preview}{more}). Commit or stash them, then update again."
            )

    with _STATE_LOCK:
        current = _read_state()
        if current["state"] in ACTIVE_STATES:
            raise UpdateError("an update is already in progress")

    job = uuid.uuid4().hex[:12]
    _set_state(
        job=job,
        state="preparing",
        message="Preparing the update…",
        progress=0.02,
        from_version=__version__,
        to_version=info["latest"],
        mode=mode["mode"],
        error=None,
        log=[],
        started_at=_now(),
        finished_at=None,
    )
    _log(f"Updating Perun {__version__} → {info['latest']} ({mode['label']}).")

    thread = threading.Thread(
        target=_prepare,
        args=(job, info, mode, restart),
        name="perun-update-prepare",
        daemon=True,
    )
    thread.start()
    return status()


def _prepare(job: str, info: dict[str, Any], mode: dict[str, Any], restart: bool) -> None:
    try:
        plan: dict[str, Any] = {
            "job": job,
            "mode": mode["mode"],
            "root": mode["root"],
            "from_version": __version__,
            "to_version": info["latest"],
            "pid": os.getpid(),
            "port": _port(),
            "serve_args": _serve_args(),
            "state_file": str(_state_path()),
            "created_at": _now(),
        }
        if mode["mode"] == "installer":
            installer = _download_installer(job, info["release"] or {})
            plan["installer"] = str(installer)
        else:
            _git_fetch(mode["root"])

        _write_json(_plan_path(), plan)
        _set_state(state="prepared", progress=1.0,
                   message="Update ready — restarting Perun…")
        _log("Handing over to the update runner.")

        runner_pid = _launch_runner(plan)
        _log(f"Update runner started (PID {runner_pid}).")

        if not restart:
            _set_state(state="prepared",
                       message="Update ready — restart Perun to finish.")
            _log("Restart skipped; restart Perun manually to finish the update.",
                 level="warn")
            return
        _shutdown_soon()
    except Exception as exc:  # noqa: BLE001 - report, never crash the server
        _log(f"Update failed: {_short(exc)}", level="error")
        _set_state(state="failed", error=_short(exc),
                   message=f"Update failed: {_short(exc)}", finished_at=_now())


def _launch_runner(plan: dict[str, Any]) -> int:
    """Start the detached process that applies the update after we exit."""
    root = Path(plan["root"])
    argv = [sys.executable, "-m", "perun", "update", "runner",
            "--plan", str(_plan_path())]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    # The runner must not inherit a "we are inside the server" signal.
    return daemon.start_detached(argv, cwd=str(root), env=env)


def _shutdown_soon(delay: float = 0.8, signal_timeout: float = 30.0) -> None:
    """Let the HTTP response flush, then ask uvicorn to exit gracefully.

    First we set ``should_exit`` on the live uvicorn ``Server`` instance. If
    that succeeds we trust uvicorn to run its lifespan shutdown handlers and
    exit the process. If no server handle is found, or if we are still alive
    after sending SIGINT, we clear the PID file and force-exit as a last
    resort so the detached update runner can take over the port.
    """

    def _go() -> None:
        time.sleep(delay)
        try:
            from . import server

            if server.request_shutdown():
                # Uvicorn will exit on its own; give it a bounded window before
                # any harder fallback. If the process exits in the meantime this
                # daemon thread is simply terminated.
                time.sleep(signal_timeout)
        except Exception:  # noqa: BLE001 - fall through to signal fallback
            pass

        try:
            import signal

            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(signal_timeout)
        except Exception:  # noqa: BLE001 - last resort
            pass

        try:
            daemon.clear_pid()
        finally:
            os._exit(0)

    threading.Thread(target=_go, name="perun-update-shutdown", daemon=True).start()


# --------------------------------------------------------------------------- #
# Apply (the detached runner)
# --------------------------------------------------------------------------- #
def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def _wait_health(port: int, version: str | None, timeout: float = 150.0) -> dict[str, Any] | None:
    """Poll ``/api/health`` until a server answers; prefer the new version."""
    import httpx

    deadline = time.time() + timeout
    best: dict[str, Any] | None = None
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=2.5) as client:
                response = client.get(f"http://127.0.0.1:{port}/api/health")
                if response.status_code == 200:
                    data = response.json()
                    best = data
                    if not version or str(data.get("version")) == str(version):
                        return data
        except Exception:  # noqa: BLE001 - the server is expected to be down
            pass
        time.sleep(1.0)
    return best


def _apply_git(plan: dict[str, Any]) -> None:
    root = Path(plan["root"])
    requirements = root / "requirements.txt"
    before = _sha256_file(requirements) if requirements.exists() else None

    _set_state(state="applying", message="Pulling the new release…")
    result = _git(root, "pull", "--ff-only", timeout=300)
    if result.returncode != 0:
        raise UpdateError(f"git pull failed: {_short(result.stderr or result.stdout)}")
    summary = " ".join((result.stdout or "").split())[:300]
    _log(f"git pull: {summary or 'up to date'}", level="ok")

    after = _sha256_file(requirements) if requirements.exists() else None
    if before != after and requirements.exists():
        _set_state(state="applying", message="Installing updated dependencies…")
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(requirements), "-q"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=1800, creationflags=_creationflags(),
        )
        if pip.returncode != 0:
            raise UpdateError(f"pip install failed: {_short(pip.stderr or pip.stdout)}")
        _log("Dependencies installed.", level="ok")


def _installer_mutex_running() -> bool:
    """Return True if another PerunSetup installer instance is already running."""
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq PerunSetup.exe", "/NH"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5,
            )
            return "PerunSetup.exe" in (proc.stdout or "")
        except Exception:
            return False
    try:
        proc = subprocess.run(
            ["pgrep", "-x", "PerunSetup"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except Exception:
        return False


def _wait_for_installer_mutex(timeout: float = 60.0) -> None:
    """Block until no other PerunSetup installer is running."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _installer_mutex_running():
            return
        time.sleep(1.0)
    raise UpdateError("another PerunSetup installer instance is already running")


def _apply_installer(plan: dict[str, Any]) -> None:
    installer = Path(plan["installer"])
    if not installer.exists():
        raise UpdateError(f"installer not found: {installer}")
    _wait_for_installer_mutex()
    log_file = updates_dir() / f"installer-{plan.get('job', 'update')}.log"
    _set_state(state="applying",
               message="Running the installer (Windows may ask for permission)…")
    # /CLOSEAPPLICATIONS and /FORCECLOSEAPPLICATIONS make the installer close
    # any running Perun processes that would otherwise keep files locked and
    # cause a code-1 failure. /NORESTARTAPPLICATIONS prevents the installer from
    # restarting the app itself; the update runner already restarts Perun after
    # the installer finishes. The runner already waits for the old server PID,
    # but child processes or a second console instance can still hold handles.
    proc = subprocess.run(
        [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
         "/SP-", "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS",
         "/NORESTARTAPPLICATIONS", f"/LOG={log_file}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=1800, creationflags=_creationflags(),
    )
    if proc.returncode != 0:
        log_tail = ""
        try:
            if log_file.exists():
                log_tail = log_file.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            pass
        raise UpdateError(
            f"installer exited with code {proc.returncode} "
            f"(log: {log_file})"
            + (f"\n\n--- installer log tail ---\n{log_tail}" if log_tail else "")
        )
    _log(f"Installer finished (log: {log_file}).", level="ok")


def _start_server(plan: dict[str, Any]) -> int:
    root = Path(plan["root"])
    argv = [sys.executable, "-m", "perun", "serve", *(plan.get("serve_args") or [])]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    return daemon.start_detached(argv, cwd=str(root), env=env)


def run_runner(plan_path: str | None = None) -> int:
    """Entry point for the detached ``perun update runner`` process."""
    path = Path(plan_path) if plan_path else _plan_path()
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[perun] update runner: cannot read plan {path}: {exc}", flush=True)
        return 1

    job = plan.get("job") or "unknown"
    log_file = updates_dir() / f"update-{job}.log"

    def runner_log(line: str, level: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] {level.upper():5s} {line}\n")
        except OSError:
            pass
        print(f"[perun] {line}", flush=True)

    try:
        _set_state(state="waiting", message="Waiting for Perun to stop…")
        runner_log("Waiting for the running server to exit…")
        _wait_for_pid(plan.get("pid"), runner_log)
        _wait_for_port(plan.get("port"), runner_log)

        runner_log(f"Applying the {plan.get('mode')} update to {plan.get('to_version')}…")
        if plan.get("mode") == "installer":
            _apply_installer(plan)
        else:
            _apply_git(plan)

        _set_state(state="starting", message="Starting Perun…")
        runner_log("Starting the updated server…")
        _start_server(plan)

        health = _wait_health(int(plan.get("port") or 8760), plan.get("to_version"))
        if not health:
            raise UpdateError(
                "the updated server did not answer within 150 s; "
                "start Perun again from the Start menu or Perun.cmd"
            )
        if plan.get("to_version") and str(health.get("version")) != str(plan["to_version"]):
            _set_state(
                state="failed",
                error=f"server reports v{health.get('version')} after updating to v{plan['to_version']}",
                message="The update ran but the new version did not take effect.",
                finished_at=_now(),
            )
            runner_log("New version did not take effect.", "error")
            try:
                path.unlink()
            except OSError:
                pass
            return 1

        _set_state(
            state="done",
            progress=1.0,
            error=None,
            message=f"Updated to v{plan.get('to_version')} — Perun restarted.",
            finished_at=_now(),
        )
        _log(f"Update complete: v{plan.get('from_version')} → v{plan.get('to_version')}.",
             level="ok")
        runner_log(f"Done: v{plan.get('from_version')} → v{plan.get('to_version')}.", "ok")
        try:
            path.unlink()
        except OSError:
            pass
        return 0
    except Exception as exc:  # noqa: BLE001 - the runner must record, not crash
        message = _short(exc)
        runner_log(f"Update failed: {message}", "error")
        _log(f"Update failed: {message}", level="error")
        _set_state(state="failed", error=message,
                   message=f"Update failed: {message}", finished_at=_now())
        # Best effort: bring *a* server back, even if the update did not apply.
        try:
            if not _port_in_use(int(plan.get("port") or 8760)):
                runner_log("Restarting the previous server so the dashboard is not left down…")
                _start_server(plan)
        except Exception:  # noqa: BLE001
            pass
        try:
            path.unlink()
        except OSError:
            pass
        return 1


def _wait_for_pid(pid: int | None, log: Callable[[str], None], timeout: float = 90.0) -> None:
    if not pid:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not daemon.is_running(int(pid)):
            return
        time.sleep(0.5)
    log(f"PID {pid} is still alive after {timeout:.0f}s; stopping it.")
    if not daemon.stop(int(pid), timeout=20.0):
        daemon.kill(int(pid))
    time.sleep(1.0)


def _wait_for_port(port: int | None, log: Callable[[str], None], timeout: float = 45.0) -> None:
    if not port:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_in_use(int(port)):
            return
        time.sleep(0.4)
    log(f"Port {port} is still in use after {timeout:.0f}s; continuing anyway.")


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #
def cleanup_old_downloads(keep: int = 3) -> None:
    """Delete stale installers (a failed download can leave a large file)."""
    try:
        files = sorted(
            (p for p in updates_dir().iterdir()
             if p.is_file() and (p.suffix == ".exe" or p.name.endswith(".part"))),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
