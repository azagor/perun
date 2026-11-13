"""Local model management for the Perun dashboard.

Only Ollama can be driven from here (``/api/pull``, ``/api/tags``,
``/api/delete``). Any other OpenAI-compatible runtime (LM Studio, vLLM,
LocalAI) is supported for *inference* by :mod:`perun.local_llm`, but its models
are installed by the user — ``local_ai_download="manual"`` prints the exact
command instead of downloading anything.

A pull is a background thread: the HTTP request returns a job id immediately and
the dashboard polls it. Progress is persisted so a reload re-attaches, and a
cancelled/failed pull is never marked complete.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import config, local_llm, registry

#: Ollama tags are ``name[:tag]``; keep the check strict because the value is
#: sent to a local HTTP API and later shown in the UI.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,119}$")

#: One pull at a time: parallel pulls saturate the link and the disk.
_PULL_LOCK = threading.Lock()

#: Reservation set by ``start_pull`` before the worker thread takes _PULL_LOCK,
#: so two rapid Prepare clicks cannot create two jobs.
_START_LOCK = threading.Lock()
_ACTIVE_JOB: int | None = None

#: Jobs the operator asked to cancel.
_CANCELLED: set[int] = set()

#: Refuse to start a pull when the model store has less room than this. Ollama
#: reports the real size only once the pull starts, so this is a safety net, not
#: a size prediction.
MIN_FREE_BYTES = 2 * 1024**3

_PROGRESS_INTERVAL = 0.75


def native_host(settings: config.Settings | None = None) -> str:
    """Ollama's native API root (the OpenAI base URL without ``/v1``)."""
    base = local_llm.base_url(settings)
    return base[:-3] if base.endswith("/v1") else base


def _tag_map(host: str, timeout: float = 2.5) -> dict[str, int] | None:
    """``{model: size_bytes}`` from ``GET /api/tags``; ``None`` when offline."""
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    out: dict[str, int] = {}
    for item in data.get("models") or []:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = int(item.get("size") or 0)
    return out


def _installed(tags: dict[str, int], model: str) -> bool:
    if not model:
        return False
    if model in tags:
        return True
    return f"{model}:latest" in tags


def _size(tags: dict[str, int], model: str) -> int:
    if model in tags:
        return tags[model]
    return tags.get(f"{model}:latest", 0)


def _model_dir() -> Path:
    env = (os.environ.get("OLLAMA_MODELS") or "").strip()
    if env:
        return Path(env).expanduser()
    candidate = Path.home() / ".ollama" / "models"
    return candidate if candidate.is_dir() else config.HOME


def disk_free_bytes() -> int:
    try:
        return int(shutil.disk_usage(_model_dir()).free)
    except OSError:
        return 0


def _busy() -> bool:
    """A job is running or reserved by ``start_pull``."""
    return _ACTIVE_JOB is not None or _PULL_LOCK.locked()


def status(settings: config.Settings | None = None) -> dict[str, Any]:
    """Everything the Settings card needs. Never raises."""
    s = settings or config.Settings.load()
    model = (s.local_ai_model or "").strip()
    host = native_host(s)
    tags = _tag_map(host)
    installed = _installed(tags, model) if tags is not None else False
    return {
        "available": tags is not None,
        "endpoint": host,
        "base_url": local_llm.base_url(s),
        "model": model,
        "installed": installed,
        "size_bytes": _size(tags, model) if tags else 0,
        "disk_free_bytes": disk_free_bytes(),
        "policy": s.local_ai_download,
        "enabled": s.local_ai_enabled,
        "shadow": s.local_ai_shadow,
        "small_model": s.local_ai_small_model,
        "commands": [f"ollama pull {model}"] if model else [],
        "models": sorted(
            # Ollama lists cloud tags too; they are remote and defeat the point.
            ({"name": n, "size": s} for n, s in (tags or {}).items()
             if not n.endswith(":cloud")),
            key=lambda m: m["name"].lower()),
        "job": registry.latest_local_ai_job(model),
        "busy": _busy(),
    }


def _update_job(job_id: int, **fields: Any) -> None:
    try:
        registry.update_local_ai_job(job_id, **fields)
    except Exception:  # pragma: no cover - progress must never break the pull
        pass


def _run_pull(job_id: int, model: str, host: str) -> None:
    global _ACTIVE_JOB
    acquired = _PULL_LOCK.acquire(blocking=False)
    try:
        if not acquired:
            _update_job(job_id, state="failed", error="another download is running",
                        ended_at=registry.now_iso())
            return
        _update_job(job_id, state="running", message="starting")
        payload = json.dumps({"model": model, "stream": True}).encode("utf-8")
        req = urllib.request.Request(
            f"{host}/api/pull", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        last = 0.0
        last_total = 0
        with urllib.request.urlopen(req, timeout=60) as resp:
            while True:
                if job_id in _CANCELLED:
                    _CANCELLED.discard(job_id)
                    _update_job(job_id, state="cancelled", message="cancelled",
                                ended_at=registry.now_iso())
                    return
                line = resp.readline()
                if not line:
                    break
                try:
                    event = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if event.get("error"):
                    _update_job(job_id, state="failed", error=str(event["error"])[:500],
                                ended_at=registry.now_iso())
                    return
                now = time.time()
                completed = int(event.get("completed") or 0)
                total = int(event.get("total") or 0)
                # Persist at a bounded rate, and immediately when the total first
                # appears so a reload can show the size instead of 0.
                if (now - last >= _PROGRESS_INTERVAL
                        or (total and completed >= total)
                        or (total and total != last_total)):
                    last = now
                    last_total = total
                    _update_job(job_id, completed=completed, total=total,
                                message=str(event.get("status") or "")[:200])
        tags = _tag_map(host, timeout=5.0)
        if tags is None or not _installed(tags, model):
            _update_job(job_id, state="failed", error="model did not appear in /api/tags",
                        ended_at=registry.now_iso())
            return
        _update_job(job_id, state="done", message="installed", ended_at=registry.now_iso())
    except Exception as exc:
        _update_job(job_id, state="failed", error=str(exc)[:500],
                    ended_at=registry.now_iso())
    finally:
        if acquired:
            _PULL_LOCK.release()
        with _START_LOCK:
            _ACTIVE_JOB = None


def start_pull(model: str, settings: config.Settings | None = None) -> dict[str, Any]:
    """Queue a pull; returns the job row immediately. Never blocks."""
    global _ACTIVE_JOB
    s = settings or config.Settings.load()
    name = (model or s.local_ai_model or "").strip()
    if not name or not _MODEL_RE.match(name):
        return {"ok": False, "reason": "invalid model name"}
    if _busy():
        return {"ok": False, "busy": True, "reason": "a download is already running"}
    host = native_host(s)
    tags = _tag_map(host)
    if tags is None:
        return {"ok": False, "reason": "ollama_not_running", "commands": [f"ollama pull {name}"]}
    if _installed(tags, name):
        return {"ok": True, "installed": True, "model": name, "job": None}
    free = disk_free_bytes()
    if free and free < MIN_FREE_BYTES:
        return {"ok": False, "reason": "not enough disk space",
                "disk_free_bytes": free, "required_bytes": MIN_FREE_BYTES}
    with _START_LOCK:
        if _busy():  # another request reserved the slot while we checked the host
            return {"ok": False, "busy": True, "reason": "a download is already running"}
        job = registry.create_local_ai_job(name, "queued")
        _ACTIVE_JOB = int(job["id"])
    registry.prune_local_ai_jobs()
    thread = threading.Thread(target=_run_pull, args=(int(job["id"]), name, host),
                              name="perun-local-ai-pull", daemon=True)
    try:
        thread.start()
    except Exception as exc:  # pragma: no cover - thread creation failure
        with _START_LOCK:
            _ACTIVE_JOB = None
        _update_job(int(job["id"]), state="failed", error=str(exc)[:300],
                    ended_at=registry.now_iso())
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "model": name, "job": registry.get_local_ai_job(int(job["id"]))}


def cancel_job(job_id: int) -> dict[str, Any]:
    job = registry.get_local_ai_job(int(job_id))
    if not job or str(job.get("state") or "") in ("done", "failed", "cancelled"):
        # Nothing to cancel; do not leave the id in the in-memory set forever.
        return {"ok": True, "job": job, "cancelled": False}
    _CANCELLED.add(int(job_id))
    return {"ok": True, "job": job, "cancelled": True}


def remove(model: str, settings: config.Settings | None = None) -> dict[str, Any]:
    """Delete a model through Ollama. Explicit user action only."""
    name = (model or "").strip()
    if not name or not _MODEL_RE.match(name):
        return {"ok": False, "reason": "invalid model name"}
    host = native_host(settings)
    payload = json.dumps({"model": name}).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/delete", data=payload,
        headers={"Content-Type": "application/json"}, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"ok": 200 <= int(getattr(resp, "status", 200)) < 300, "model": name}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "reason": str(exc)}


def ensure(settings: config.Settings | None = None) -> dict[str, Any]:
    """Download the configured model when the policy allows it and it is missing."""
    s = settings or config.Settings.load()
    if (s.local_ai_download or "auto") != "auto":
        return {"ok": False, "manual": True,
                "commands": status(s).get("commands", [])}
    st = status(s)
    if st["installed"]:
        return {"ok": True, "installed": True}
    return start_pull(s.local_ai_model, s)


def model_installed(settings: config.Settings | None = None) -> bool:
    s = settings or config.Settings.load()
    tags = _tag_map(native_host(s))
    return bool(tags is not None and _installed(tags, (s.local_ai_model or "").strip()))


__all__ = [
    "cancel_job",
    "disk_free_bytes",
    "ensure",
    "model_installed",
    "native_host",
    "remove",
    "start_pull",
    "status",
]
