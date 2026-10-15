"""Perun secret storage helpers.

Provides optional password-based encryption for sensitive values stored in
``config.json`` (e.g. the Telegram bot token). The encryption is cross-platform
and uses Fernet with a key derived from a user-supplied password via PBKDF2.

Usage
-----
- Set ``PERUN_PASSWORD`` to a strong password, or call ``prompt_password()``
  interactively.
- Store values as ``enc:<base64>`` in ``config.json``.
- ``Settings.telegram()`` will transparently decrypt ``telegram_token`` when a
  password is available. Without a password the encrypted value stays encrypted
  and Telegram gracefully degrades to "not configured".
"""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import os
from typing import Any

try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    _HAS_CRYPTO = True
except Exception:  # pragma: no cover - optional dependency
    _HAS_CRYPTO = False

_PREFIX = "enc:"
_SALT_LEN = 16
_ITERATIONS = 480_000


class SecretError(Exception):
    """Raised when a secret cannot be encrypted or decrypted."""


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a Fernet-compatible key from *password* and *salt*."""
    if not _HAS_CRYPTO:
        raise SecretError("cryptography is not installed")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _password() -> str | None:
    """Return the configured password without prompting.

    Order: ``PERUN_PASSWORD`` env var, then ``None``. Prompts belong in CLI/UI
    layers, not in library code.
    """
    pw = os.environ.get("PERUN_PASSWORD") or ""
    return pw.strip() or None


def encrypt_value(plain: str, password: str | None = None) -> str:
    """Encrypt *plain* and return an ``enc:<base64>`` string."""
    if not _HAS_CRYPTO:
        raise SecretError("cryptography is not installed")
    pw = (password or _password())
    if not pw:
        raise SecretError("no password supplied for encryption")
    salt = os.urandom(_SALT_LEN)
    key = _derive_key(pw, salt)
    token = Fernet(key).encrypt(plain.encode("utf-8"))
    blob = salt + token
    return f"{_PREFIX}{base64.urlsafe_b64encode(blob).decode('ascii')}"


def decrypt_value(value: str, password: str | None = None) -> str | None:
    """Decrypt an ``enc:<base64>`` value, or return *value* if unencrypted.

    Returns ``None`` if the value is encrypted but no password is available or
    the password is wrong.
    """
    if not value.startswith(_PREFIX):
        return value
    if not _HAS_CRYPTO:
        return None
    pw = password or _password()
    if not pw:
        return None
    encoded = value[len(_PREFIX):]
    try:
        blob = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (binascii.Error, ValueError):
        return None
    if len(blob) < _SALT_LEN:
        return None
    salt, token = blob[:_SALT_LEN], blob[_SALT_LEN:]
    try:
        key = _derive_key(pw, salt)
        plain = Fernet(key).decrypt(token)
    except (InvalidToken, SecretError):
        return None
    return plain.decode("utf-8")


def is_encrypted(value: Any) -> bool:
    """Return True if *value* looks like an encrypted blob."""
    return isinstance(value, str) and value.startswith(_PREFIX)


def prompt_password() -> str:
    """Prompt the user for the Perun password (CLI helper)."""
    return getpass.getpass("Perun password: ")


