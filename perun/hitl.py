"""Human-in-the-Loop (HITL) approval token system for Perun action proposals.

Mirrors the PerunChat gateway token semantics so the two planes stay aligned:
  HMAC-SHA256(K_server, proposal_id || action_hash || expiry_epoch || nonce)

* TTL: 180 seconds
* Nonce replay window: 300 seconds
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

_PROPOSAL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NONCE_RE = re.compile(r"[0-9a-f]{16,64}")


@dataclass
class HITLConfig:
    secret_key: bytes
    token_ttl_seconds: int = 180
    nonce_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        # Single-use tokens rely on the nonce outliving the token. Without this
        # guard a short nonce window lets an expired token's nonce be reused.
        if self.nonce_ttl_seconds < self.token_ttl_seconds + 5:
            raise ValueError(
                "nonce_ttl_seconds must be at least token_ttl_seconds + 5"
            )


class ApprovalStore:
    """Tracks consumed nonces to prevent token replay.

    Nonces are persisted to SQLite so restarts do not clear replay protection.
    """

    def __init__(self, window_seconds: int = 300) -> None:
        self._window_seconds = window_seconds

    def _now(self) -> float:
        return time.time()

    def _db(self):
        from . import db

        return db.db()

    def consume(self, nonce: str, now: Optional[float] = None) -> bool:
        if now is None:
            now = self._now()
        self._evict_expired(now)
        with self._db() as conn:
            try:
                conn.execute(
                    "INSERT INTO hitl_nonces (nonce, consumed_at) VALUES (?,?)",
                    (nonce, int(now)),
                )
                return True
            except Exception:
                return False

    def _evict_expired(self, now: float) -> None:
        cutoff = int(now - self._window_seconds)
        with self._db() as conn:
            conn.execute("DELETE FROM hitl_nonces WHERE consumed_at<?", (cutoff,))


class HITLTokenEngine:
    def __init__(self, config: HITLConfig) -> None:
        self.config = config
        self.store = ApprovalStore(window_seconds=config.nonce_ttl_seconds)

    @staticmethod
    def action_hash(action: Dict[str, Any]) -> str:
        """Deterministic hash of execution details for token binding."""
        canonical = json.dumps(
            action,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ) if isinstance(action, dict) else str(action)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def issue(self, proposal_id: str, execution_details: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(proposal_id, str) or not _PROPOSAL_ID_RE.fullmatch(proposal_id):
            raise ValueError("invalid proposal_id for HITL token")
        if not isinstance(execution_details, dict):
            raise ValueError("execution_details must be an object")
        expiry = int(time.time()) + self.config.token_ttl_seconds
        nonce = secrets.token_hex(16)
        ahash = self.action_hash(execution_details)
        message = f"{proposal_id}||{ahash}||{expiry}||{nonce}".encode()
        token = hmac.new(self.config.secret_key, message, hashlib.sha256).hexdigest()
        return {
            "token": token,
            "proposal_id": proposal_id,
            "action_hash": ahash,
            "expiry_epoch": expiry,
            "nonce": nonce,
        }

    def validate(
        self,
        token: str,
        proposal_id: str,
        execution_details: Dict[str, Any],
        expiry_epoch: int,
        nonce: str,
    ) -> Tuple[bool, Optional[str]]:
        # Reject malformed inputs before touching the nonce store so a bad call
        # cannot burn a legitimate nonce.
        if (
            not isinstance(token, str)
            or not token.isascii()
            or not isinstance(proposal_id, str)
            or not _PROPOSAL_ID_RE.fullmatch(proposal_id)
            or not isinstance(nonce, str)
            or not _NONCE_RE.fullmatch(nonce)
            or not isinstance(expiry_epoch, int)
            or isinstance(expiry_epoch, bool)
            or not isinstance(execution_details, dict)
        ):
            return False, "invalid_signature"
        if int(time.time()) > expiry_epoch:
            return False, "expired"
        ahash = self.action_hash(execution_details)
        message = f"{proposal_id}||{ahash}||{expiry_epoch}||{nonce}".encode()
        expected = hmac.new(self.config.secret_key, message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.encode(), token.encode()):
            return False, "invalid_signature"
        if not self.store.consume(nonce):
            return False, "nonce_replay"
        return True, None


def _default_secret() -> bytes:
    """Return the HITL signing secret.

    Resolution order:
      1. ``PERUN_HITL_SECRET`` environment variable (preferred for secrets).
      2. ``Settings.perun_hitl_secret`` (persisted in config.json, may be
         encrypted with ``PERUN_PASSWORD``).
      3. A hash of ``PERUN_HOME`` (single-machine default).

    For multi-node deployments all nodes must share the same secret from (1) or
    (2); otherwise tokens issued on one node cannot be validated on another.
    """
    from . import config, secrets as _secrets

    raw = os.environ.get("PERUN_HITL_SECRET", "").strip()
    if not raw:
        raw = config.Settings.load().perun_hitl_secret.strip()
    if raw and _secrets.is_encrypted(raw):
        try:
            decrypted = _secrets.decrypt_value(raw)
            if decrypted:
                raw = decrypted
        except Exception:
            # No password available: leave the encrypted string in place. Tokens
            # will be invalid, but the service stays up and the error is visible
            # when the operator tries to approve/execute a proposal.
            pass
    if not raw:
        raw = str(config.HOME)
    return hashlib.sha256(raw.encode()).digest()


def default_engine() -> HITLTokenEngine:
    return HITLTokenEngine(HITLConfig(secret_key=_default_secret()))
