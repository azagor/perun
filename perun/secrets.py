"""Perun secret storage helpers.

Provides optional password-based encryption for sensitive values stored in
``config.json`` (e.g. the Telegram bot token). The encryption is cross-platform
and uses Fernet with a key derived from a user-supplied password via PBKDF2.

Usage
-----
- Set ``PERUN_PASSWORD`` to a strong password, or call ``prompt_password()``
  interactively.
- Persist a password for future launches with ``persist_password()``: it is
  encrypted with Windows **DPAPI** (bound to the current user) and written to
  ``<PERUN_HOME>/secret`` — never stored in plaintext.
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

# File under ``config.HOME`` holding the DPAPI-encrypted password (Windows).
_PASSWORD_FILE = "secret"


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


def _dpapi(data: bytes, *, protect: bool) -> bytes | None:
    """Protect (encrypt) or unprotect (decrypt) *data* with Windows DPAPI.

    Returns ``None`` on non-Windows or when DPAPI is unavailable. The result is
    bound to the current Windows user, so another user cannot decrypt it.
    """
    if os.name != "nt" or not data:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _Blob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0,
                ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:  # pragma: no cover - DPAPI unavailable
        return None


def _load_persisted_password() -> str | None:
    """Return the DPAPI-stored password, or ``None`` when there is none."""
    if os.name != "nt":
        return None
    try:
        from . import config

        path = config.HOME / _PASSWORD_FILE
        if not path.is_file():
            return None
        plain = _dpapi(path.read_bytes(), protect=False)
        if plain is None:
            return None
        return plain.decode("utf-8").strip() or None
    except Exception:  # pragma: no cover - defensive
        return None


def _password() -> str | None:
    """Return the configured password without prompting.

    Order: ``PERUN_PASSWORD`` env var, then the DPAPI-stored password file.
    Prompts belong in CLI/UI layers, not in library code.
    """
    pw = os.environ.get("PERUN_PASSWORD") or ""
    if pw.strip():
        return pw.strip()
    return _load_persisted_password()


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


def persist_password(password: str) -> bool:
    """Persist *password* for future launches (Windows only).

    The password is encrypted with Windows **DPAPI** — which binds it to the
    current user — and written to ``<PERUN_HOME>/secret``, so it is never stored
    in plaintext (unlike a raw ``HKCU\\Environment`` value). The
    ``PERUN_PASSWORD`` environment variable still takes precedence when set, so
    hand-configured environments keep working.

    Returns ``True`` on success. On non-Windows, or when DPAPI is unavailable,
    this is a no-op returning ``False``. This only writes the file; a caller that
    needs the password in the *current* process should also set
    ``PERUN_PASSWORD`` (as the unlock endpoint does).
    """
    if os.name != "nt":
        return False
    try:
        if not password:
            return False
        blob = _dpapi(password.encode("utf-8"), protect=True)
        if blob is None:
            return False
        from . import config

        config.ensure_dirs()
        path = config.HOME / _PASSWORD_FILE
        path.write_bytes(blob)
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - best effort on Windows
            pass
        return True
    except Exception:  # pragma: no cover - defensive
        return False


