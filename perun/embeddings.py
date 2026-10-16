"""Ollama embedding transport + vector math, shared by the whole of Perun.

This is the project-agnostic version of ``tools/memory_utils.py`` from the
trading_bot memory framework. It talks to a local Ollama server and embeds
with ``nomic-embed-text`` (768 dims) using contrastive prefixes, exactly as the
per-project tooling does, so existing memories/chunks remain comparable.
"""

from __future__ import annotations

import json
import math
import re
import struct
import time
import urllib.error
import urllib.request

try:  # numpy is optional but present in the standard Perun environment
    import numpy as _np  # type: ignore
except ImportError:  # pragma: no cover
    _np = None

from . import config

DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "
MAX_CHARS_PER_CHUNK = 20000

# CODEMAP-compatible extraction (file paths + code identifiers) so doc chunks
# carry the same navigation metadata as the per-project tooling.
IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_\.]*)`")
FILE_RE = re.compile(r"`([^`:]+\.(?:py|ps1|sh|js|ts|html|css|md|json|ya?ml|kv))(?::[^`]*)?`")


class EmbeddingError(RuntimeError):
    """Raised when Ollama is unreachable or the embedding call fails."""


def _settings():
    return config.Settings.load()


_OLLAMA_HEALTH_CACHE: dict[str, Any] = {"ts": 0.0, "ok": False}
_OLLAMA_HEALTH_TTL = 15.0


def ollama_ok(timeout: float = 2.0) -> bool:
    """Best-effort cached health check for the configured Ollama server."""
    now = time.time()
    if now - _OLLAMA_HEALTH_CACHE["ts"] < _OLLAMA_HEALTH_TTL:
        return _OLLAMA_HEALTH_CACHE["ok"]
    cfg = _settings()
    url = f"{cfg.ollama_host.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        ok = False
    _OLLAMA_HEALTH_CACHE.update(ts=now, ok=ok)
    return ok


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    if _np is not None:
        va = _np.asarray(a, dtype=_np.float32)
        vb = _np.asarray(b, dtype=_np.float32)
        denom = float(_np.linalg.norm(va) * _np.linalg.norm(vb))
        return float(_np.dot(va, vb) / denom) if denom else 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def pack_vector(vec: list[float]) -> bytes:
    if _np is not None:
        return _np.asarray(vec, dtype=_np.float32).tobytes()
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> list[float]:
    if not blob:
        return []
    if _np is not None:
        return _np.frombuffer(blob, dtype=_np.float32).tolist()
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def embed_texts(texts: list[str], prefix: str = "") -> list[list[float]]:
    """Embed ``texts`` with nomic-embed-text via Ollama's batch ``/api/embed``."""
    if not texts:
        return []
    cfg = _settings()
    if prefix:
        texts = [f"{prefix}{t[:MAX_CHARS_PER_CHUNK]}" for t in texts]
    url = f"{cfg.ollama_host.rstrip('/')}/api/embed"
    payload = json.dumps({"model": cfg.embed_model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EmbeddingError(
            f"Cannot reach Ollama at {cfg.ollama_host} ({exc}). "
            f"Start it with `ollama serve`, then `ollama pull {cfg.embed_model}`."
        ) from exc
    embeddings = data.get("embeddings")
    if not embeddings or len(embeddings) != len(texts):
        raise EmbeddingError(
            f"Embedding count mismatch: asked {len(texts)}, got {len(embeddings or [])}"
        )
    return embeddings


def embed_one(text: str, prefix: str = QUERY_PREFIX) -> list[float] | None:
    """Best-effort single embedding — returns ``None`` when Ollama is down."""
    try:
        return embed_texts([text], prefix=prefix)[0]
    except EmbeddingError:
        return None


def extract_metadata(content: str) -> dict[str, list[str]]:
    return {
        "paths": list(dict.fromkeys(FILE_RE.findall(content))),
        "identifiers": list(dict.fromkeys(IDENT_RE.findall(content))),
    }
