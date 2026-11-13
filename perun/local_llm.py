"""Small local language model client (OpenAI-compatible).

Perun uses a local model only for **bounded, non-critical** work: a changelog
bullet, a notification summary, OpenCode's own small tasks. Nothing here may
ever block or fail a run, so every call degrades to ``""`` (or ``{}``) and the
caller keeps its previous behaviour.

The transport is the OpenAI-compatible ``/v1/chat/completions`` endpoint, which
Ollama, LM Studio, vLLM and LocalAI all expose. No third-party dependency is
used: the stdlib ``urllib`` keeps this module importable before dependencies are
installed, like the rest of Perun's install path.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from . import config

#: Cached health probe. Ollama is optional, so callers ask often.
_HEALTH: dict[str, Any] = {"ts": 0.0, "ok": False, "base": ""}
_HEALTH_TTL = 20.0

#: Hard cap on a returned string, so a runaway model cannot flood a prompt.
_MAX_RESPONSE_CHARS = 20_000


def base_url(settings: config.Settings | None = None) -> str:
    """OpenAI-compatible base URL for the configured local runtime."""
    s = settings or config.Settings.load()
    explicit = (getattr(s, "local_ai_base_url", "") or "").strip().rstrip("/")
    if explicit:
        return explicit
    host = (getattr(s, "ollama_host", "") or "http://127.0.0.1:11434").strip().rstrip("/")
    if host.endswith("/v1"):
        return host
    return f"{host}/v1"


def model_name(settings: config.Settings | None = None) -> str:
    s = settings or config.Settings.load()
    return (getattr(s, "local_ai_model", "") or "").strip()


def available(timeout: float = 2.0, settings: config.Settings | None = None) -> bool:
    """Whether the local runtime answers ``GET {base}/models`` (cached)."""
    base = base_url(settings)
    now = time.time()
    if _HEALTH["base"] == base and now - _HEALTH["ts"] < _HEALTH_TTL:
        return bool(_HEALTH["ok"])
    ok = False
    try:
        req = urllib.request.Request(f"{base}/models")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= int(getattr(resp, "status", 200)) < 300
    except Exception:
        ok = False
    _HEALTH.update(ts=now, ok=ok, base=base)
    return ok


def _post(payload: dict[str, Any], *, timeout: float, base: str) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200)), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() if hasattr(exc, "read") else b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(str(exc)) from exc


def complete(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 256,
    timeout: float = 8.0,
    json_object: bool = False,
    settings: config.Settings | None = None,
) -> str:
    """One non-streaming completion. Returns ``""`` on any failure."""
    text = (prompt or "").strip()
    if not text:
        return ""
    s = settings or config.Settings.load()
    model = model_name(s)
    if not model:
        return ""
    messages: list[dict[str, str]] = []
    if (system or "").strip():
        messages.append({"role": "system", "content": system.strip()})
    messages.append({"role": "user", "content": text})
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max(1, int(max_tokens)),
        "temperature": 0.2,
        "stream": False,
    }
    if json_object:
        payload["response_format"] = {"type": "json_object"}
    base = base_url(s)
    try:
        status, raw = _post(payload, timeout=timeout, base=base)
        if status == 400 and json_object:
            # Not every OpenAI-compatible runtime accepts response_format.
            payload.pop("response_format", None)
            status, raw = _post(payload, timeout=timeout, base=base)
    except Exception:
        return ""
    if not 200 <= status < 300:
        return ""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    if not isinstance(content, str):
        return ""
    content = "".join(ch for ch in content if ch in "\n\t" or ord(ch) >= 32)
    return content.strip()[:_MAX_RESPONSE_CHARS]


def complete_json(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 512,
    timeout: float = 10.0,
    settings: config.Settings | None = None,
) -> dict[str, Any]:
    """Completion parsed as a JSON object; ``{}`` when the model is unusable."""
    raw = complete(
        prompt,
        system=system,
        max_tokens=max_tokens,
        timeout=timeout,
        json_object=True,
        settings=settings,
    )
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def probe(settings: config.Settings | None = None) -> dict[str, Any]:
    """Round-trip a tiny prompt; used by ``POST /api/local-ai/test``.

    The timeout is generous on purpose: the first call after a download loads
    the model from disk (tens of seconds on a cold cache), and a false "offline"
    report is worse than a slow button.
    """
    started = time.time()
    reply = complete(
        "Reply with the single word: ready",
        max_tokens=8,
        timeout=60.0,
        settings=settings,
    )
    return {
        "ok": bool(reply),
        "latency_ms": int((time.time() - started) * 1000),
        "reply": reply[:100],
        "model": model_name(settings),
        "base_url": base_url(settings),
    }


__all__ = [
    "available",
    "base_url",
    "complete",
    "complete_json",
    "model_name",
    "probe",
]
