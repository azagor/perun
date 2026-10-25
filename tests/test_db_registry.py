"""Registry + database layer tests."""

from __future__ import annotations

import os

import pytest

from perun import db, registry


def test_init_db_creates_tables():
    with db.db() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("projects", "sessions", "runs", "activity", "memory",
              "doc_chunks", "settings", "bridge_links"):
        assert t in names


def test_projects_table_has_no_instruction_columns():
    """Instruction text lives on disk, never in the projects table."""
    with db.db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
    assert "instructions" not in cols
    assert "instructions_path" not in cols


def test_init_db_drops_retired_instruction_columns():
    """An older DB that stored instruction text has those columns dropped."""
    with db.db() as conn:
        conn.execute("ALTER TABLE projects ADD COLUMN instructions TEXT DEFAULT ''")
        conn.execute("ALTER TABLE projects ADD COLUMN instructions_path TEXT DEFAULT ''")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
    assert {"instructions", "instructions_path"} <= cols

    db.init_db()

    with db.db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
    assert "instructions" not in cols
    assert "instructions_path" not in cols


def test_init_db_migrates_action_proposals_expires_at():
    """An older DB without ``expires_at`` is upgraded and pending rows kept.

    ``CREATE TABLE IF NOT EXISTS`` never adds columns to an existing table, and
    a missing ``expires_at`` made ``action_proposals.expire_stale()`` raise on
    every proposals request (the dashboard's 500). The backfill also prevents
    legacy pending rows from being expired instantly by the new DEFAULT 0.
    """
    with db.db() as conn:
        conn.execute("DROP TABLE action_proposals")
        conn.execute(
            """CREATE TABLE action_proposals (
                id TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL,
                room_id TEXT,
                requester TEXT NOT NULL,
                action_type TEXT NOT NULL,
                intent TEXT NOT NULL,
                targets TEXT NOT NULL,
                details TEXT NOT NULL,
                manifest_rev INTEGER DEFAULT 1,
                status TEXT DEFAULT 'pending',
                approval_token TEXT,
                approval_expiry INTEGER,
                approved_by TEXT,
                approved_at TEXT,
                result TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            "INSERT INTO action_proposals "
            "(id, project_id, requester, action_type, intent, targets, details, "
            " status, created_at, updated_at) "
            "VALUES ('p_legacy', 1, 'frambo', 'edit', 'old', '[]', '{}', "
            "        'pending', 't', 't')"
        )

    db.init_db()

    with db.db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(action_proposals)")}
        row = conn.execute(
            "SELECT expires_at FROM action_proposals WHERE id='p_legacy'"
        ).fetchone()
    assert "expires_at" in cols
    assert row["expires_at"] > 0, "legacy pending rows must not expire immediately"


def test_init_db_is_idempotent():
    """Running init_db twice (with a retired column present) never raises."""
    with db.db() as conn:
        conn.execute("ALTER TABLE projects ADD COLUMN instructions TEXT DEFAULT ''")
    db.init_db()
    db.init_db()  # second run finds nothing left to drop
    with db.db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
    assert "instructions" not in cols


def test_migrate_tolerates_drop_column_unsupported(monkeypatch):
    """SQLite < 3.35 (or a race) must not abort ``_migrate`` when DROP COLUMN fails."""
    import sqlite3

    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def __iter__(self):
            return iter(self._rows)

        def fetchall(self):
            return self._rows

    class _Conn:
        def __init__(self, columns):
            self._columns = columns

        def execute(self, sql, params=()):
            if "PRAGMA table_info" in sql:
                return _Cursor([{"name": c} for c in self._columns])
            if "DROP COLUMN" in sql:
                raise sqlite3.OperationalError("near \"DROP\": syntax error")
            return _Cursor([])

    # Should neither raise nor propagate the OperationalError.
    db._migrate(_Conn(["id", "instructions", "instructions_path"]))


def test_add_get_list_update_delete_project():
    p = registry.add_project("Trading Bot", r"C:\apps\trading_bot",
                             kind="opencode", description="bot")
    assert p["slug"] == "trading-bot"
    assert p["kind"] == "opencode"
    assert p["favorite"] is False
    assert registry.get_project("trading-bot")["name"] == "Trading Bot"
    assert registry.get_project(p["id"])["slug"] == "trading-bot"

    registry.update_project("trading-bot", favorite=1, description="updated")
    got = registry.get_project("trading-bot")
    assert got["favorite"] is True
    assert got["description"] == "updated"

    assert [x["slug"] for x in registry.list_projects()] == ["trading-bot"]
    assert registry.delete_project("trading-bot") is True
    assert registry.get_project("trading-bot") is None


def test_slug_uniqueness():
    registry.add_project("Same", r"C:\a\same")
    second = registry.add_project("Same", r"C:\a\same2")
    assert second["slug"] == "same-2"


@pytest.mark.skipif(os.name != "nt",
                    reason="os.path.normcase folds case only on Windows")
def test_get_project_by_path_is_normcase():
    registry.add_project("Proj", r"C:\Apps\Proj")
    found = registry.get_project_by_path(r"c:\apps\proj")
    assert found is not None and found["slug"] == "proj"


def test_project_dict_counts():
    p = registry.add_project("P", r"C:\p")
    registry.create_session(p["id"], "oc-1")
    registry.create_run("uid1", p["id"], "hello")
    from perun import memory
    memory.remember("a fact", project_id=p["id"], scope="project")
    detail = registry.get_project("p")
    assert detail["session_count"] == 1
    assert detail["memory_count"] == 1


def test_session_lifecycle():
    p = registry.add_project("P", r"C:\p")
    s = registry.create_session(p["id"], "oc-abc", title="first")
    assert s["status"] == "active"
    registry.update_session(s["id"], message_count=3, status="error",
                            last_error="boom")
    got = registry.get_session(s["id"])
    assert got["message_count"] == 3 and got["status"] == "error"
    assert registry.find_session_by_opencode(p["id"], "oc-abc")["id"] == s["id"]
    assert len(registry.list_sessions(p["id"])) == 1
    registry.delete_session(s["id"])
    assert registry.get_session(s["id"]) is None


def test_run_lifecycle():
    p = registry.add_project("P", r"C:\p")
    run = registry.create_run("abc123", p["id"], "do a thing",
                              model="m", agent="build")
    assert run["status"] == "queued"
    registry.update_run("abc123", status="done", exit_code=0, output="ok")
    got = registry.get_run("abc123")
    assert got["status"] == "done" and got["output"] == "ok"
    assert len(registry.list_runs(p["id"])) == 1
    assert len(registry.list_runs()) == 1


def test_activity_feed():
    p = registry.add_project("P", r"C:\p")
    registry.log_activity(p["id"], "run", "started", {"k": 1})
    registry.log_activity(None, "system", "global event")
    items = registry.list_activity(p["id"])
    assert items[0]["kind"] == "run" and items[0]["meta"] == {"k": 1}
    allitems = registry.list_activity()
    assert len(allitems) == 2


def test_bridge_links_upsert():
    p = registry.add_project("P", r"C:\p")
    registry.set_bridge_link("chat1", p["id"], None)
    link = registry.get_bridge_link("chat1")
    assert link["project_id"] == p["id"] and link["session_pk"] is None
    registry.set_bridge_link("chat1", p["id"], 7)
    assert registry.get_bridge_link("chat1")["session_pk"] == 7


def test_add_project_loads_manifest(tmp_path):
    """Registration outside discovery (CLI ``perun add``, wizard) must still
    populate manifest data so the dashboard overview shows it like any
    discovered project."""
    proj = tmp_path / "manifested"
    proj.mkdir()
    (proj / "perun.md").write_text(
        "---\n"
        "name: Manifested Project\n"
        "kind: opencode\n"
        "description: Declared in the manifest\n"
        "tags:\n"
        "  - alpha\n"
        "  - beta\n"
        "---\n\n"
        "Body text.\n",
        encoding="utf-8",
    )
    p = registry.add_project("Manifested Project", str(proj))
    assert p["has_manifest"] is True
    assert p["kind"] == "opencode"
    assert p["description"] == "Declared in the manifest"
    assert "alpha" in p["tags"] and "beta" in p["tags"]
    stored = registry.get_project_manifest(p["slug"])
    assert stored["data"]["name"] == "Manifested Project"
    assert stored["body"].startswith("Body text.")
    assert p["manifest_path"] == str(proj / "perun.md")


def test_add_project_without_manifest_is_unaffected(tmp_path):
    proj = tmp_path / "bare"
    proj.mkdir()
    p = registry.add_project("Bare Project", str(proj))
    assert p["has_manifest"] is False
    assert p["kind"] == "dir"
