"""Perun secret store tests.

The secret store is the sanctioned home for credentials: a value is encrypted
at rest, never listed, and pulled on demand by name. These tests cover the
three properties that make it safe to move credentials out of ``AGENTS.md``:

* the plaintext never reaches the disk;
* a listing never contains a value;
* the project scope shadows the global scope.
"""

from __future__ import annotations

import pytest

from perun import db, registry, secret_store
from perun import secrets as crypto


@pytest.fixture(autouse=True)
def _password(monkeypatch):
    """A deterministic master password for every test in this module."""
    monkeypatch.setenv("PERUN_PASSWORD", "test-master-password")


def _row(name: str) -> dict:
    with db.db() as conn:
        row = db.one(conn, "SELECT * FROM secret_store WHERE name=?", (name,))
    return dict(row) if row else {}


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #
def test_normalize_name_is_case_insensitive():
    assert secret_store.normalize_name("Deploy-Token") == "deploy-token"
    assert secret_store.normalize_name("  ssh / prod key ") == "ssh/prod-key"
    assert secret_store.normalize_name("") == ""


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #
def test_set_and_get_round_trip():
    res = secret_store.set_secret("deploy-token", "s3cr3t-value")
    assert res["ok"] and res["name"] == "deploy-token"
    row = secret_store.get_secret("deploy-token")
    assert row is not None
    assert row["value"] == "s3cr3t-value"
    assert row["scope"] == secret_store.GLOBAL_SCOPE


def test_set_replaces_an_existing_secret():
    secret_store.set_secret("deploy-token", "first")
    secret_store.set_secret("deploy-token", "second")
    row = secret_store.get_secret("deploy-token")
    assert row["value"] == "second"


def test_get_returns_none_for_an_unknown_name():
    assert secret_store.get_secret("nope") is None


def test_set_requires_a_name_and_a_value():
    assert secret_store.set_secret("", "v")["ok"] is False
    assert secret_store.set_secret("k", "")["ok"] is False


# --------------------------------------------------------------------------- #
# Encryption at rest
# --------------------------------------------------------------------------- #
def test_value_is_encrypted_on_disk():
    secret_store.set_secret("deploy-token", "plaintext-marker")
    row = _row("deploy-token")
    assert row["value"].startswith("enc:")
    assert "plaintext-marker" not in row["value"]
    assert crypto.is_encrypted(row["value"])


def test_set_without_a_password_fails_cleanly(monkeypatch):
    monkeypatch.setattr(crypto, "_password", lambda: None)
    res = secret_store.set_secret("deploy-token", "value")
    assert res["ok"] is False
    assert res["reason"]
    assert "PERUN_PASSWORD" in res["hint"]
    assert secret_store.get_secret("deploy-token") is None


def test_get_reports_an_undecryptable_value(monkeypatch):
    secret_store.set_secret("deploy-token", "value")
    monkeypatch.setattr(crypto, "_password", lambda: None)
    row = secret_store.get_secret("deploy-token")
    assert row is not None
    assert row["value"] is None
    assert row["error"]


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def test_listing_never_contains_a_value():
    secret_store.set_secret("deploy-token", "s3cr3t-value", hint="GitHub deploy token")
    rows = secret_store.list_secrets()
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "deploy-token"
    assert row["hint"] == "GitHub deploy token"
    assert "value" not in row
    assert "s3cr3t-value" not in str(row)


# --------------------------------------------------------------------------- #
# Scope resolution
# --------------------------------------------------------------------------- #
def test_project_scope_wins_over_global():
    proj = registry.add_project("P", r"C:\p")
    secret_store.set_secret("deploy-token", "global-value")
    secret_store.set_secret("deploy-token", "project-value",
                            scope=secret_store.PROJECT_SCOPE, project_id=proj["id"])
    assert secret_store.get_secret("deploy-token", project_id=proj["id"])["value"] \
        == "project-value"
    assert secret_store.get_secret("deploy-token", project_id=0)["value"] \
        == "global-value"


def test_project_scope_requires_a_project():
    res = secret_store.set_secret("k", "v", scope=secret_store.PROJECT_SCOPE)
    assert res["ok"] is False


def test_global_scope_is_stored_without_a_project_id():
    secret_store.set_secret("k", "v", scope=secret_store.GLOBAL_SCOPE,
                             project_id=42)
    assert _row("k")["project_id"] == 0


# --------------------------------------------------------------------------- #
# Deleting
# --------------------------------------------------------------------------- #
def test_delete_removes_the_secret():
    secret_store.set_secret("deploy-token", "value")
    assert secret_store.delete_secret("deploy-token") is True
    assert secret_store.get_secret("deploy-token") is None
    assert secret_store.delete_secret("deploy-token") is False


def test_delete_only_touches_the_visible_scope():
    """Deleting a project-scoped secret must not delete the global one."""
    proj = registry.add_project("P", r"C:\p")
    secret_store.set_secret("k", "global-value")
    secret_store.set_secret("k", "project-value",
                            scope=secret_store.PROJECT_SCOPE, project_id=proj["id"])
    assert secret_store.delete_secret("k", project_id=proj["id"]) is True
    # The project row is gone; the global row survives and now resolves.
    assert secret_store.get_secret("k", project_id=proj["id"])["value"] == "global-value"
    assert secret_store.get_secret("k", project_id=0)["value"] == "global-value"


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def test_unlock_status_reports_readiness():
    status = secret_store.unlock_status()
    assert status["ok"] is True
    assert status["can_write"] is True
    assert status["password_available"] is True


def test_unlock_status_reports_a_missing_password(monkeypatch):
    monkeypatch.setattr(crypto, "_password", lambda: None)
    status = secret_store.unlock_status()
    assert status["can_write"] is False
    assert status["note"]
