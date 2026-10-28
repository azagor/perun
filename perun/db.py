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
import time
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
    agent_models  TEXT DEFAULT '{}',
    favorite      INTEGER DEFAULT 0,
    enabled       INTEGER DEFAULT 1,
    hidden        INTEGER DEFAULT 0,
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
    meta          TEXT DEFAULT '{}',
    room_id       TEXT DEFAULT '',
    kind          TEXT DEFAULT 'note',
    last_used_at  TEXT DEFAULT '',
    hit_count     INTEGER DEFAULT 0,
    valid_to      TEXT DEFAULT '',
    UNIQUE(scope, project_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_memory_project ON memory(project_id);
CREATE INDEX IF NOT EXISTS idx_memory_project_id_desc ON memory(project_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_memory_room ON memory(room_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind);
CREATE INDEX IF NOT EXISTS idx_memory_valid_to ON memory(valid_to);

CREATE TABLE IF NOT EXISTS doc_chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_file   TEXT NOT NULL,
    section       TEXT DEFAULT '',
    content       TEXT NOT NULL,
    embedding     BLOB,
    content_hash  TEXT NOT NULL,
    embedded_hash TEXT DEFAULT '',
    updated_at    TEXT NOT NULL,
    UNIQUE(project_id, source_file, section)
);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON doc_chunks(project_id);

-- Perun Context: keyed, agent-retrievable reference docs. Distinct from
-- ``doc_chunks`` (which mirrors files under a project's ``docs/``) because a
-- context doc has a *stable key* an agent can ask for by name, and its own
-- scope: 'global' rows use project_id=0 and are visible from every project,
-- 'project' rows belong to exactly one project.
CREATE TABLE IF NOT EXISTS context_docs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scope         TEXT NOT NULL DEFAULT 'global',
    project_id    INTEGER NOT NULL DEFAULT 0,
    key           TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL,
    source_file   TEXT NOT NULL DEFAULT '',
    embedding     BLOB,
    content_hash  TEXT NOT NULL,
    embedded_hash TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL,
    UNIQUE(scope, project_id, key)
);
CREATE INDEX IF NOT EXISTS idx_context_docs_key ON context_docs(key);
CREATE INDEX IF NOT EXISTS idx_context_docs_project ON context_docs(project_id);
CREATE INDEX IF NOT EXISTS idx_context_docs_scope ON context_docs(scope, project_id);

-- The encrypted secret store. Values are Fernet blobs produced by
-- ``perun/secrets.py`` (PBKDF2 + AES), never plaintext. ``hint`` is a
-- non-sensitive description so an agent can pick the right secret without
-- revealing it.
CREATE TABLE IF NOT EXISTS secret_store (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'global',
    project_id  INTEGER NOT NULL DEFAULT 0,
    value       TEXT NOT NULL,
    hint        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(scope, project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_secret_store_name ON secret_store(name);
CREATE INDEX IF NOT EXISTS idx_secret_store_project ON secret_store(project_id);

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

-- Shared Projects: PerunChat room bound to a project lifecycle.
CREATE TABLE IF NOT EXISTS project_chats (
    project_id   INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    room_id      TEXT UNIQUE NOT NULL,
    managed_by   TEXT NOT NULL,
    state        TEXT DEFAULT 'active',
    manifest_rev INTEGER DEFAULT 1,
    last_sync_at TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_chats_room ON project_chats(room_id);

-- Shared Projects: HITL-gated action proposals from chat.
CREATE TABLE IF NOT EXISTS action_proposals (
    id              TEXT PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    room_id         TEXT,
    requester       TEXT NOT NULL,
    action_type     TEXT NOT NULL,
    intent          TEXT NOT NULL,
    targets         TEXT NOT NULL,  -- JSON list of paths
    details         TEXT NOT NULL,  -- JSON extra parameters
    manifest_rev    INTEGER DEFAULT 1,
    status          TEXT DEFAULT 'pending',
    expires_at      INTEGER NOT NULL,
    approval_token  TEXT,
    approval_expiry INTEGER,
    approved_by     TEXT,
    approved_at     TEXT,
    result          TEXT,
    executor_id     TEXT DEFAULT '',
    execution_mode  TEXT DEFAULT 'perun',
    claimed_at      TEXT DEFAULT '',
    claim_expires_at INTEGER DEFAULT 0,
    claim_snapshot  TEXT DEFAULT '',
    executed_by     TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_proposals_project ON action_proposals(project_id);
CREATE INDEX IF NOT EXISTS idx_action_proposals_status ON action_proposals(status);
CREATE INDEX IF NOT EXISTS idx_action_proposals_room ON action_proposals(room_id);

-- External execution handoff: upload transfer reassembly slots. Persisted so
-- an interrupted chunked upload survives a Perun bridge restart within the TTL.
CREATE TABLE IF NOT EXISTS package_transfers (
    transfer_id     TEXT PRIMARY KEY,
    proposal_id       TEXT NOT NULL,
    sender            TEXT NOT NULL,
    direction         TEXT NOT NULL,
    sha256            TEXT,
    total_size        INTEGER,
    total_chunks      INTEGER,
    received          TEXT NOT NULL DEFAULT '{}',
    started_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_package_transfers_started ON package_transfers(started_at);

-- Offline room deletions queued when the gateway is unreachable during project
-- deletion. Retried on server startup / bridge reconnect.
CREATE TABLE IF NOT EXISTS pending_room_deletions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id      TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    error        TEXT
);

-- HITL approval-token consumed nonces. Persists across restarts so approved
-- tokens cannot be replayed before their expiry.
CREATE TABLE IF NOT EXISTS hitl_nonces (
    nonce        TEXT PRIMARY KEY,
    consumed_at  INTEGER NOT NULL
);

-- Cross-project dependency graph. Source of truth is each project's manifest
-- (``perun.md`` / ``perun.json``), synced here on registration/manifest refresh
-- so impact analysis and deploy planning can query it directly.
CREATE TABLE IF NOT EXISTS project_dependencies (
    project_id            INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    depends_on_project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    depends_on_slug       TEXT DEFAULT '',
    relationship          TEXT DEFAULT '',
    deploy_command        TEXT DEFAULT '',
    deploy_doc            TEXT DEFAULT '',
    auto_deploy           INTEGER DEFAULT 0,
    reason                TEXT DEFAULT '',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE(project_id, depends_on_slug)
);
CREATE INDEX IF NOT EXISTS idx_project_dependencies_project ON project_dependencies(project_id);
CREATE INDEX IF NOT EXISTS idx_project_dependencies_depends ON project_dependencies(depends_on_project_id);
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
        ("hidden", "INTEGER DEFAULT 0"),
        # Per-agent default models (v0.8.32).
        ("agent_models", "TEXT DEFAULT '{}'"),
    ],
    "runs": [
        ("model_explicit", "INTEGER DEFAULT 0"),
    ],
    # ``embedded_hash`` records which content the stored vector was computed
    # from, so a chunk whose content changed while Ollama was down can be
    # re-embedded instead of silently keeping a stale vector (COALESCE).
    "doc_chunks": [
        ("embedded_hash", "TEXT DEFAULT ''"),
    ],
    "memory": [
        ("meta", "TEXT DEFAULT '{}'"),
        # Room-scoped PerunChat memory (v0.8.22).
        ("room_id", "TEXT DEFAULT ''"),
        # Memory kind for rules-only prefetch (v0.8.28).
        ("kind", "TEXT DEFAULT 'note'"),
        # Fact lifecycle metrics (v0.8.29).
        ("last_used_at", "TEXT DEFAULT ''"),
        ("hit_count", "INTEGER DEFAULT 0"),
        ("valid_to", "TEXT DEFAULT ''"),
    ],
    # Added with the Shared Projects release. ``CREATE TABLE IF NOT EXISTS``
    # cannot add it to a database whose table was created by an earlier build,
    # and a missing column made ``expire_stale()`` raise on every proposals read.
    "action_proposals": [
        ("expires_at", "INTEGER NOT NULL DEFAULT 0"),
        # External execution handoff columns (v0.8.17).
        ("executor_id", "TEXT DEFAULT ''"),
        ("execution_mode", "TEXT DEFAULT 'perun'"),
        ("claimed_at", "TEXT DEFAULT ''"),
        ("claim_expires_at", "INTEGER DEFAULT 0"),
        ("claim_snapshot", "TEXT DEFAULT ''"),
        ("executed_by", "TEXT DEFAULT ''"),
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
        if not existing:
            # A missing table yields no PRAGMA rows (an old database may predate
            # it entirely): skip it — CREATE TABLE IF NOT EXISTS in SCHEMA
            # creates it with the columns already in place.
            continue
        for name, ddl in columns:
            if name not in existing:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
                    # Another process (second Perun startup, MCP server) added
                    # the column between our PRAGMA and the ALTER — SQLite has
                    # no ADD COLUMN IF NOT EXISTS. Same tolerance as DROP.
                    pass
    # ``action_proposals.expires_at`` was added after the Shared Projects
    # release. Proposals written before the ALTER carry the column default of 0;
    # give the still-pending ones a normal TTL instead of expiring them on the
    # first read after the upgrade.
    try:
        conn.execute(
            "UPDATE action_proposals SET expires_at=? "
            "WHERE expires_at=0 AND status='pending'",
            (int(time.time()) + 24 * 3600,),
        )
    except sqlite3.OperationalError:
        # Table missing entirely on a very old database; SCHEMA creates it.
        pass
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


# Bump whenever SCHEMA or the _ADDED_COLUMNS/_REMOVED_COLUMNS tables change, so
# startup can tell "migrations already ran" from "fresh upgrade needed" without
# guessing from table introspection alone.
SCHEMA_VERSION = 10


def init_db() -> None:
    global _initialized
    with db() as conn:
        # Reconcile columns on existing tables *before* executing SCHEMA.
        # SCHEMA contains indexes that reference newer columns (e.g.
        # ``idx_memory_room`` needs ``memory.room_id``); if the table predates
        # the column, creating the index would fail before migrations run.
        try:
            user_ver = conn.execute("PRAGMA user_version").fetchone()
            ver = user_ver[0] if user_ver else 0
        except Exception:
            ver = 0
        _migrate(conn)
        conn.executescript(SCHEMA)
        # Run migrations again in case any newly-created tables need retired
        # columns dropped or future non-additive fixes.
        _migrate(conn)
        if ver < 8:
            try:
                _backfill_memory_kind(conn)
            except Exception:
                pass
        if ver < 9:
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_valid_to ON memory(valid_to)")
            except Exception:
                pass
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    _initialized = True


def _backfill_memory_kind(conn: sqlite3.Connection) -> None:
    import json
    import re

    log_verbs = re.compile(r"^(Released|Session checkpoint|Pushed|Applied|Fixed|Shipped|Implemented|Added|Synced|Updated|Committed|Deployed|Merged|Perun\b.*v\d|PerunChat\b.*v\d)", re.I)
    rule_topics = {"architecture", "deployment", "infrastructure", "security"}
    rows = conn.execute("SELECT id, project_id, scope, topic, source, content, meta, room_id FROM memory WHERE COALESCE(kind,'')='' OR kind='note'").fetchall()
    for r in rows:
        rid = r["id"]
        meta = {}
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            meta = {}
        mtype = str((meta or {}).get("type") or "").strip().lower()
        if mtype == "transcript" or r["room_id"]:
            conn.execute("UPDATE memory SET kind='log' WHERE id=?", (rid,))
            continue
        if (r["source"] or "") == "perunchat":
            conn.execute("UPDATE memory SET kind='log' WHERE id=?", (rid,))
            continue
        topic = r["topic"] or ""
        if topic in ("dependencies", "session_checkpoint", "perunchat", "work"):
            conn.execute("UPDATE memory SET kind='log' WHERE id=?", (rid,))
            continue
        content = (r["content"] or "").strip()
        if log_verbs.match(content):
            conn.execute("UPDATE memory SET kind='log' WHERE id=?", (rid,))
            continue
        if topic in rule_topics:
            conn.execute("UPDATE memory SET kind='rule' WHERE id=?", (rid,))
            continue
        if mtype == "decision":
            conn.execute("UPDATE memory SET kind='decision' WHERE id=?", (rid,))
            continue
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind)")
    except Exception:
        pass


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()
