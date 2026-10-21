"""Perun secret store — named values, encrypted at rest, for agents to use.

Why this exists
---------------
``AGENTS.md`` is ambient context: whatever is in it is paid for in *every*
prompt of *every* session, and it is visible to every model, tool log and
transcript on the machine. A credential belongs in neither place. It belongs
in a store that is:

* **encrypted at rest** — the value on disk is a Fernet blob
  (``perun/secrets.py``: PBKDF2-HMAC-SHA256 + AES), and the master password
  itself is protected by Windows **DPAPI**, bound to the current user;
* **addressable by name** — an agent is told the *name* (``deploy-token``),
  not the value;
* **pulled on demand** — one prompt that needs the secret gets it, instead of
  every prompt carrying it.

The knowledge of *how* to reach a secret stays in the project file. The
knowledge of *the secret* does not.

Layout
------
``scope='global'`` rows use ``project_id=0`` and are visible from every
project. ``scope='project'`` rows belong to exactly one project. Lookup
prefers the project scope, then falls back to global — the same resolution
order used by :mod:`perun.context`.

The sensitive-data guard in :mod:`perun.memory` refuses to store credential-
looking text in the vector memory; this module is the sanctioned place for
it, and the two policies together mean a secret can no longer leak into the
embedding store by accident.
"""

from __future__ import annotations

import re
from typing import Any

from . import db, registry
from . import secrets as crypto

GLOBAL_SCOPE = "global"
PROJECT_SCOPE = "project"

_NAME_CLEAN = re.compile(r"[^a-z0-9._/-]+")


def normalize_name(name: str) -> str:
    """Normalise a secret name to its canonical, case-insensitive form.

    ``"Deploy Token"``, ``"deploy-token"`` and ``"deploy token"`` are the same
    secret. Separators survive, so a name may address a namespace:
    ``"ssh / prod key"`` becomes ``"ssh/prod-key"``.
    """
    raw = re.sub(r"\s+", "-", (name or "").strip().casefold())
    raw = re.sub(r"[-_]{2,}", "-", raw)
    raw = re.sub(r"-+([./])", r"\1", raw)
    raw = re.sub(r"([./])-+", r"\1", raw)
    raw = _NAME_CLEAN.sub("-", raw)
    return raw.strip("-./")


def _scope_filter(scope: str, project_id: int) -> tuple[str, list[Any]]:
    """Build the WHERE clause for a scope lookup."""
    if scope == GLOBAL_SCOPE:
        return "scope='global' AND project_id=0", []
    if scope == PROJECT_SCOPE:
        return "scope='project' AND project_id=?", [int(project_id)]
    if project_id:
        return ("(scope='global' AND project_id=0) OR "
                "(scope='project' AND project_id=?)", [int(project_id)])
    return "scope='global' AND project_id=0", []


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def set_secret(name: str, value: str, *, scope: str = GLOBAL_SCOPE,
               project_id: int = 0, hint: str = "") -> dict[str, Any]:
    """Store (or replace) a secret. The value is encrypted before it is written."""
    key = normalize_name(name)
    if not key:
        return {"ok": False, "reason": "empty name"}
    if not value:
        return {"ok": False, "reason": "empty value"}
    if scope not in (GLOBAL_SCOPE, PROJECT_SCOPE):
        scope = GLOBAL_SCOPE
    if scope == PROJECT_SCOPE and not project_id:
        return {"ok": False, "reason": "project scope needs project_id"}
    if scope == GLOBAL_SCOPE:
        project_id = 0
    try:
        blob = crypto.encrypt_value(value)
    except crypto.SecretError as exc:
        return {
            "ok": False,
            "reason": str(exc),
            "hint": "set PERUN_PASSWORD, or run: perun secret password",
        }
    ts = registry.now_iso()
    with db.db() as conn:
        conn.execute(
            """INSERT INTO secret_store
               (name, scope, project_id, value, hint, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(scope, project_id, name) DO UPDATE SET
                 value=excluded.value,
                 hint=excluded.hint,
                 updated_at=excluded.updated_at""",
            (key, scope, int(project_id), blob, hint or "", ts, ts),
        )
    return {"ok": True, "name": key, "scope": scope}


def _resolve_row(name: str, scope: str = "any", project_id: int = 0) -> dict[str, Any] | None:
    """Return the row a lookup would resolve to — the project scope wins."""
    where, params = _scope_filter(scope, project_id)
    with db.db() as conn:
        rows = db.rows(
            conn,
            f"SELECT * FROM secret_store WHERE {where} AND name=? "
            f"ORDER BY CASE scope WHEN 'project' THEN 0 ELSE 1 END",
            (*params, name),
        )
    return dict(rows[0]) if rows else None


def delete_secret(name: str, *, scope: str = "any", project_id: int = 0) -> bool:
    """Delete a secret. Returns ``True`` when a row was removed.

    In the default (``any``) scope only the row a lookup would resolve is
    removed — the project-scoped one, or the global one when there is no
    project-scoped shadow. Deleting every visible copy of a name would be a
    surprising way to lose a global secret.
    """
    key = normalize_name(name)
    if not key:
        return False
    if scope in (GLOBAL_SCOPE, PROJECT_SCOPE):
        where, params = _scope_filter(scope, project_id)
        with db.db() as conn:
            cur = conn.execute(
                f"DELETE FROM secret_store WHERE {where} AND name=?", (*params, key))
            return cur.rowcount > 0
    row = _resolve_row(key, "any", project_id)
    if row is None:
        return False
    with db.db() as conn:
        cur = conn.execute("DELETE FROM secret_store WHERE id=?", (row["id"],))
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def list_secrets(*, scope: str = "any", project_id: int = 0) -> list[dict[str, Any]]:
    """List the secrets visible in a scope — **metadata only, never values**."""
    where, params = _scope_filter(scope, project_id)
    with db.db() as conn:
        rows = db.rows(
            conn,
            f"SELECT name, scope, project_id, hint, created_at, updated_at "
            f"FROM secret_store WHERE {where} ORDER BY scope, name",
            tuple(params),
        )
    return [dict(r) for r in rows]


def get_secret(name: str, *, scope: str = "any", project_id: int = 0,
               reveal: bool = True) -> dict[str, Any] | None:
    """Fetch one secret.

    With ``reveal=True`` the decrypted value is included. The project scope
    wins over the global scope. Returns ``None`` when the name is unknown.
    """
    key = normalize_name(name)
    if not key:
        return None
    row = _resolve_row(key, scope, project_id)
    if row is None:
        return None
    out: dict[str, Any] = {
        "name": row["name"],
        "scope": row["scope"],
        "project_id": row["project_id"],
        "hint": row["hint"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if not reveal:
        out["value"] = None
        return out
    plain = crypto.decrypt_value(row["value"])
    if plain is None:
        out["value"] = None
        out["error"] = (
            "the secret is encrypted but cannot be decrypted — set "
            "PERUN_PASSWORD or run: perun secret password"
        )
    else:
        out["value"] = plain
    return out


def unlock_status() -> dict[str, Any]:
    """Report whether the store can be read and written right now."""
    has_crypto = crypto._HAS_CRYPTO
    has_pw = bool(crypto._password())
    return {
        "ok": has_crypto,
        "cryptography": has_crypto,
        "password_available": has_pw,
        "can_write": has_crypto and has_pw,
        "note": "" if (has_crypto and has_pw) else (
            "cryptography is not installed" if not has_crypto
            else "no password available — set PERUN_PASSWORD or run: perun secret password"
        ),
    }
