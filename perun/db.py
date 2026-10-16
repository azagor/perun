"""SQLite persistence for Perun (registry + unified memory + vectors).

A single WAL-mode database keeps everything atomic: the project registry,
agent sessions/runs, the activity feed, the unified memory store, the
document vector store, and the Telegram chat→project routing table.

Concurrency model: every operation opens its own short-lived connection (cheap
in SQLite) so the FastAPI workers, the orchestrator threads, the Telegram
bridge, and the MCP server can all write safely without sharing connections.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    path          TEXT UNIQUE NOT NULL,
    root          TEXT DEFAULT '',
    kind          TEXT DEFAULT 'dir',
    description   TEXT DEFAULT '',
    tags          TEXT DEFAULT '',
    model         TEXT DEFAULT '',
    agent         TEXT DEFAULT '',
    favorite      INTEGER DEFAULT 0,
    enabled       INTEGER DEFAULT 1,
    color         TEXT DEFAULT '',
    manifest           TEXT DEFAULT '',
    manifest_path      TEXT DEFAULT '',
    manifest_loaded_at TEXT DEFAULT '',
    git_remote    TEXT DEFAULT '',
    git_branch    TEXT DEFAULT '',
    git_provider  TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    opencode_session_id  TEXT,
    title                TEXT DEFAULT '',
    status               TEXT DEFAULT 'active',
    message_count        INTEGER DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    last_error           TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project_updated ON sessions(project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uid              TEXT UNIQUE NOT NULL,
    project_id           INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_pk           INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    opencode_session_id  TEXT,
    prompt               TEXT NOT NULL,
    model                TEXT DEFAULT '',
    model_explicit       INTEGER DEFAULT 0,
    agent                TEXT DEFAULT '',
    status               TEXT DEFAULT 'queued',
    exit_code            INTEGER,
    started_at           TEXT,
    ended_at             TEXT,
    output               TEXT DEFAULT '',
    error                TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_project_id_desc ON runs(project_id, id DESC);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    kind          TEXT DEFAULT 'prompt',
    prompt        TEXT DEFAULT '',
    tools         TEXT DEFAULT '[]',
    skills        TEXT DEFAULT '[]',
    enabled       INTEGER DEFAULT 1,
    sort_order    INTEGER DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);

CREATE TABLE IF NOT EXISTS activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    message     TEXT NOT NULL,
    meta        TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(ts);
CREATE INDEX IF NOT EXISTS idx_activity_project_id ON activity(project_id, id DESC);

-- Unified memory. scope='global' rows use project_id=0 so the UNIQUE
-- constraint works (SQLite treats NULLs as distinct). Deliberately no FK to
-- projects(id): global rows use project_id=0, which has no projects row.
-- Related rows are removed explicitly by registry.delete_project().
CREATE TABLE IF NOT EXISTS memory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL DEFAULT 0,
    scope         TEXT NOT NULL DEFAULT 'global',
    topic         TEXT DEFAULT 'general',
    content       TEXT NOT NULL,
    embedding     BLOB,
    source        TEXT DEFAULT 'manual',
    content_hash  TEXT NOT NULL,
    ts            TEXT NOT NULL,
    UNIQUE(scope, project_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_memory_project ON memory(project_id);
CREATE INDEX IF NOT EXISTS idx_memory_project_id_desc ON memory(project_id, id DESC);

CREATE TABLE IF NOT EXISTS doc_chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_file   TEXT NOT NULL,
    section       TEXT DEFAULT '',
    content       TEXT NOT NULL,
    embedding     BLOB,
    content_hash  TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(project_id, source_file, section)
);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON doc_chunks(project_id);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS bridge_links (
    chat_id     TEXT PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    session_pk  INTEGER,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_bridge_links_project ON bridge_links(project_id);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(path or config.DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_initialized = False


# Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` never
# alters an existing table, so older databases need an explicit ALTER.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "projects": [
        ("manifest", "TEXT DEFAULT ''"),
        ("manifest_path", "TEXT DEFAULT ''"),
        ("manifest_loaded_at", "TEXT DEFAULT ''"),
        ("git_remote", "TEXT DEFAULT ''"),
        ("git_branch", "TEXT DEFAULT ''"),
        ("git_provider", "TEXT DEFAULT ''"),
    ],
    "runs": [
        ("model_explicit", "INTEGER DEFAULT 0"),
    ],
}

# Columns retired after project instructions moved fully to files on disk
# (``<project>/.perun/instructions/*.md``). Older databases still carry them;
# they are dropped once so fresh and upgraded schemas converge. SQLite gained
# ``ALTER TABLE ... DROP COLUMN`` in 3.35.
_REMOVED_COLUMNS: dict[str, list[str]] = {
    "projects": ["instructions", "instructions_path"],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    for table, columns in _REMOVED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name in columns:
            if name in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
                except sqlite3.OperationalError:
                    # SQLite < 3.35 has no DROP COLUMN, and a concurrent
                    # connection may drop it between the PRAGMA and the ALTER.
                    # A retired column left behind is harmless: nothing reads it.
                    pass


def init_db() -> None:
    global _initialized
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    _initialized = True


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()
