"""SQLite persistence for Perun (registry + unified memory + vectors).

A single WAL-mode database keeps everything atomic: the project registry,
agent sessions/runs, the activity feed, the unified memory store, the
document vector store, and the Telegram chat→project routing table.

Concurrency model: every operation opens its own short-lived connection (cheap
in SQLite) so the FastAPI workers, the orchestrator threads, the Telegram
bridge, and the MCP server can all write safely without sharing connections.
"""

from __future__ import annotations

import json
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
    work_paused   INTEGER DEFAULT 0,
    work_paused_at TEXT DEFAULT '',
    work_paused_by TEXT DEFAULT '',
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
    error                TEXT DEFAULT '',
    created_at           TEXT DEFAULT '',
    admitted_at          TEXT DEFAULT '',
    reply_sent_at        TEXT DEFAULT '',
    lease_epoch          INTEGER DEFAULT 0,
    standalone           INTEGER DEFAULT 0,
    pid                  INTEGER DEFAULT 0,
    run_options          TEXT DEFAULT '{}',
    coroutine_id         INTEGER,
    coroutine_cycle_id   TEXT DEFAULT '',
    coroutine_iteration  INTEGER DEFAULT 0,
    coroutine_fire_key   TEXT DEFAULT '',
    coroutine_trigger    TEXT DEFAULT '',
    coroutine_outcome    TEXT DEFAULT '',
    coroutine_accounting_at TEXT DEFAULT '',
    coroutine_accounted_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_project_id_desc ON runs(project_id, id DESC);
-- Coroutine fires are ordinary runs. The unique fire key is the duplicate-fire
-- guard that survives crashes, restarts and two processes sharing this DB.
CREATE INDEX IF NOT EXISTS idx_runs_coroutine ON runs(coroutine_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_coroutine_fire
    ON runs(coroutine_fire_key) WHERE coroutine_fire_key <> '';
CREATE INDEX IF NOT EXISTS idx_runs_coroutine_accounting
    ON runs(coroutine_accounted_at, status);

-- One owner per run (the process observing the OpenCode session). A stale lease
-- (no heartbeat for 30 s) is taken over by the reconciler; the owner token is
-- checked before finalizing so a zombie cannot overwrite a newer observation.
CREATE TABLE IF NOT EXISTS run_leases (
    run_uid    TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    beat_epoch REAL NOT NULL,
    epoch      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_run_leases_beat ON run_leases(beat_epoch);

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
-- Routing telemetry is queried by kind + window (dashboard panel) and pruned by
-- kind + age; without this index that is a full scan once the table grows.
CREATE INDEX IF NOT EXISTS idx_activity_kind_ts ON activity(kind, ts);

-- Routing coverage, aggregated per day/source/action. One row per combination
-- instead of one activity row per message: a room is chatty, and the Activity
-- view must not drown in "routing: unknown". The panel reads "what share of
-- messages does the router understand?" from here.
CREATE TABLE IF NOT EXISTS routing_stats (
    day     TEXT NOT NULL,
    source  TEXT NOT NULL,
    action  TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source, action)
);

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
    operations      TEXT DEFAULT '[]',  -- JSON list of {path, op}; source of truth
    intent          TEXT NOT NULL,
    targets         TEXT NOT NULL,  -- derived JSON list of paths
    details         TEXT NOT NULL,  -- JSON extra parameters
    manifest_rev    INTEGER DEFAULT 1,
    status          TEXT DEFAULT 'pending',
    expires_at      INTEGER NOT NULL,
    approval_token  TEXT,
    approval_expiry INTEGER,
    approval_snapshot TEXT DEFAULT '',
    approved_by     TEXT,
    approved_at     TEXT,
    result          TEXT,
    executor_id     TEXT DEFAULT '',
    execution_mode  TEXT DEFAULT '',
    claimed_at      TEXT DEFAULT '',
    claim_expires_at INTEGER DEFAULT 0,
    claim_snapshot  TEXT DEFAULT '',
    executed_by     TEXT DEFAULT '',
    -- Package-level approval for external handoff. The proposal approval pins
    -- the intent and pre-image; the uploaded package is a separate artifact
    -- that must be approved by its digest before it is applied.
    package_state        TEXT DEFAULT '',
    package_digest       TEXT DEFAULT '',
    package_manifest     TEXT DEFAULT '',
    package_preview      TEXT DEFAULT '',
    package_uploaded_at  TEXT DEFAULT '',
    package_expires_at   INTEGER DEFAULT 0,
    package_approved_by  TEXT DEFAULT '',
    package_approved_at  TEXT DEFAULT '',
    package_applied_at   TEXT DEFAULT '',
    package_error        TEXT DEFAULT '',
    claim_hashes         TEXT DEFAULT '',
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
    room_id           TEXT DEFAULT '',
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

-- Operator inbox: one row per thing that needs a human decision. ``target_kind``
-- plus ``target_id`` is what makes a reply land in the right context (a session,
-- a permission request, a form, a room, a proposal) even when several sessions
-- are blocked at once. ``dedupe_key`` is unique among open rows so a replayed
-- permission or a re-asked question cannot produce a second card.
CREATE TABLE IF NOT EXISTS attention_requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    target_kind      TEXT NOT NULL,
    target_id        TEXT NOT NULL,
    source           TEXT NOT NULL,
    ref              TEXT NOT NULL DEFAULT '{}',
    question         TEXT NOT NULL,
    options          TEXT NOT NULL DEFAULT '[]',
    state            TEXT NOT NULL DEFAULT 'open',
    answer           TEXT,
    answered_by      TEXT,
    delivery         TEXT,
    dedupe_key       TEXT NOT NULL,
    waiter_seen_epoch INTEGER,
    ttl_epoch        INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    closed_at        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_attention_open
    ON attention_requests(dedupe_key) WHERE state='open';
CREATE INDEX IF NOT EXISTS idx_attention_project ON attention_requests(project_id, state);
CREATE INDEX IF NOT EXISTS idx_attention_target ON attention_requests(target_kind, target_id);

-- Which Telegram chat was notified about which item (and with which message id,
-- so a reply-to can be mapped back to exactly one item).
CREATE TABLE IF NOT EXISTS attention_deliveries (
    attention_id INTEGER NOT NULL REFERENCES attention_requests(id) ON DELETE CASCADE,
    chat_id      TEXT NOT NULL,
    message_id   INTEGER,
    delivered_at TEXT,
    PRIMARY KEY (attention_id, chat_id)
);

-- Replies that could not be delivered yet (PerunChat socket down, service
-- restarting). Drained on reconnect and by the notifier loop.
CREATE TABLE IF NOT EXISTS attention_outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    attention_id INTEGER NOT NULL REFERENCES attention_requests(id) ON DELETE CASCADE,
    target_kind  TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    payload      TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_attention_outbox_open ON attention_outbox(delivered_at);

-- Durable Telegram update queue. The poller only enqueues (offset advances after
-- the row commits), a small keyed executor processes per chat in order, and a
-- crash resumes from here instead of dropping the update.
CREATE TABLE IF NOT EXISTS telegram_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'update',
    payload     TEXT NOT NULL DEFAULT '{}',
    dedupe_key  TEXT NOT NULL UNIQUE,
    state       TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    not_before  REAL NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tg_jobs_state ON telegram_jobs(state, not_before);
CREATE INDEX IF NOT EXISTS idx_tg_jobs_chat ON telegram_jobs(chat_id, state);

-- Per-chat notification mutes ("/mute <project>"), so one noisy project can be
-- silenced without unplugging the operator's pager.
CREATE TABLE IF NOT EXISTS attention_mutes (
    chat_id      TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (chat_id, project_slug)
);

-- ------------------------------------------------------------------------- #
-- Coroutines (scheduled/event-triggered agent jobs)
-- ------------------------------------------------------------------------- #
-- A coroutine is a durable job definition. What it actually executes is an
-- ordinary `runs` row, so the existing streaming/cancel/lease/recovery pipeline
-- is reused. `scope` controls listing/ownership; `execution_project_id` is the
-- directory the agent actually runs in (required, even for global jobs).
CREATE TABLE IF NOT EXISTS coroutines (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    uid                     TEXT UNIQUE NOT NULL,
    name                    TEXT NOT NULL,
    description             TEXT DEFAULT '',
    scope                   TEXT NOT NULL DEFAULT 'project',   -- project|global
    project_id              INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    execution_project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    trigger_type            TEXT NOT NULL DEFAULT 'manual',    -- interval|cron|once|manual|event
    trigger_expr            TEXT DEFAULT '',
    timezone                TEXT DEFAULT '',
    run_at_epoch            REAL DEFAULT 0,
    next_fire_epoch         REAL DEFAULT 0,
    -- The next *cycle* slot, preserved while a loop spends next_fire_epoch on
    -- its iterations. Restored when the cycle ends.
    next_cycle_epoch        REAL DEFAULT 0,
    next_trigger            TEXT DEFAULT 'schedule',   -- schedule|loop|event
    last_fire_at            TEXT DEFAULT '',
    last_run_uid            TEXT DEFAULT '',
    active_cycle_id         TEXT DEFAULT '',
    active_cycle_started_epoch REAL DEFAULT 0,
    active_iteration        INTEGER DEFAULT 0,
    active_trigger_ref      TEXT DEFAULT '',
    status                  TEXT NOT NULL DEFAULT 'active',
    prompt                  TEXT NOT NULL DEFAULT '',
    model                   TEXT DEFAULT '',
    agent                   TEXT DEFAULT '',
    session_mode            TEXT DEFAULT 'fresh',              -- fresh|continue
    session_pk              INTEGER,
    opencode_session_id     TEXT DEFAULT '',
    execution_mode          TEXT DEFAULT 'single',             -- single|loop
    max_iterations          INTEGER DEFAULT 1,                 -- 0 = unlimited
    iteration_delay_seconds INTEGER DEFAULT 5,
    completion_marker       TEXT DEFAULT '',
    stall_iterations        INTEGER DEFAULT 3,
    cycle_max_seconds       INTEGER DEFAULT 0,
    max_runs                INTEGER DEFAULT 0,
    max_runs_per_day        INTEGER DEFAULT 0,
    max_cost_usd_per_cycle  REAL DEFAULT 0,
    max_cost_usd_per_day    REAL DEFAULT 0,
    run_timeout_seconds     INTEGER DEFAULT 0,
    overlap_policy          TEXT DEFAULT 'skip',
    catch_up                TEXT DEFAULT 'skip',               -- skip|once
    jitter_seconds          INTEGER DEFAULT 0,
    failure_policy          TEXT DEFAULT 'pause',              -- continue|pause
    failure_threshold       INTEGER DEFAULT 3,
    restart_policy          TEXT DEFAULT 'resume',             -- resume|pause
    post_result_to_room     INTEGER DEFAULT 0,
    suppress_auto_reply     INTEGER DEFAULT 1,
    post_on_failure         INTEGER DEFAULT 0,
    event_source            TEXT DEFAULT '',
    event_room_id           TEXT DEFAULT '',
    event_mention_only      INTEGER DEFAULT 0,
    event_sender_allow      TEXT DEFAULT '[]',
    event_listen_since      REAL DEFAULT 0,
    event_min_interval_seconds INTEGER DEFAULT 5,
    event_filter            TEXT DEFAULT '{}',
    claim_token             TEXT DEFAULT '',
    dispatch_failures       INTEGER DEFAULT 0,
    last_error              TEXT DEFAULT '',
    options                 TEXT DEFAULT '{}',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    deleted_at              TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_coroutines_due
    ON coroutines(status, next_fire_epoch);
CREATE INDEX IF NOT EXISTS idx_coroutines_project
    ON coroutines(project_id, status);
CREATE INDEX IF NOT EXISTS idx_coroutines_scope
    ON coroutines(scope, status);
CREATE INDEX IF NOT EXISTS idx_coroutines_room
    ON coroutines(trigger_type, event_source, event_room_id, status);

-- Durable event inbox. The bridge (which may run in another process) only
-- writes rows here; the scheduler in `serve` matches them. Dedupe keys make a
-- gateway replay after a reconnect harmless.
CREATE TABLE IF NOT EXISTS coroutine_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL UNIQUE,
    room_id     TEXT DEFAULT '',
    sender      TEXT DEFAULT '',
    payload     TEXT DEFAULT '{}',
    received_at TEXT NOT NULL,
    handled_at  TEXT DEFAULT '',
    expires_at  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_coroutine_events_source
    ON coroutine_events(source, id);
CREATE INDEX IF NOT EXISTS idx_coroutine_events_expiry
    ON coroutine_events(expires_at);
CREATE INDEX IF NOT EXISTS idx_coroutine_events_handled
    ON coroutine_events(handled_at, id);
CREATE INDEX IF NOT EXISTS idx_coroutine_events_room
    ON coroutine_events(room_id, received_at);

-- One row per (event, job): a single event may match several jobs, and each
-- job needs its own fired/skipped/failed record.
CREATE TABLE IF NOT EXISTS coroutine_event_deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER NOT NULL REFERENCES coroutine_events(id) ON DELETE CASCADE,
    coroutine_id INTEGER NOT NULL REFERENCES coroutines(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued|fired|skipped|failed
    run_uid      TEXT DEFAULT '',
    error        TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    handled_at   TEXT DEFAULT '',
    UNIQUE(event_id, coroutine_id)
);
CREATE INDEX IF NOT EXISTS idx_coroutine_deliveries_job
    ON coroutine_event_deliveries(coroutine_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_coroutine_deliveries_pending
    ON coroutine_event_deliveries(status, id);
CREATE INDEX IF NOT EXISTS idx_coroutine_deliveries_created
    ON coroutine_event_deliveries(status, created_at);

-- Results a room-triggered job wants posted back. The scheduler may live in a
-- different process from the PerunChat WebSocket, so this is a durable outbox
-- drained by the bridge (same idea as attention_outbox).
CREATE TABLE IF NOT EXISTS coroutine_room_outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    coroutine_id INTEGER NOT NULL REFERENCES coroutines(id) ON DELETE CASCADE,
    run_uid      TEXT DEFAULT '',
    room_id      TEXT NOT NULL,
    text         TEXT NOT NULL,
    in_reply_to  TEXT DEFAULT '',
    dedupe_key   TEXT NOT NULL UNIQUE,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    delivered_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_coroutine_room_outbox_open
    ON coroutine_room_outbox(delivered_at, id);

-- Scheduler observability: one row, heartbeat updated every tick. It is not a
-- distributed lease (the PID file already enforces one server); it lets the
-- dashboard tell "running" from "thread is dead".
CREATE TABLE IF NOT EXISTS coroutine_heartbeat (
    id              INTEGER PRIMARY KEY,
    owner           TEXT DEFAULT '',
    pid             INTEGER DEFAULT 0,
    started_at      TEXT DEFAULT '',
    last_tick_epoch REAL DEFAULT 0,
    last_tick_ms    INTEGER DEFAULT 0,
    due_count       INTEGER DEFAULT 0,
    version         TEXT DEFAULT ''
);

-- Operator-declared checks (v0.10.59). A check is the *only* command Perun may
-- run without asking again. Declarations live here, in Perun's own store, never
-- in the project tree: no room message, agent tool or approved proposal can add
-- one. ``pin_path``/``pin_sha256`` optionally pin the reviewed script's content.
CREATE TABLE IF NOT EXISTS project_checks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL,
    name            TEXT NOT NULL,
    command         TEXT NOT NULL,
    cwd             TEXT NOT NULL DEFAULT '',
    timeout_seconds INTEGER NOT NULL DEFAULT 120,
    pin_path        TEXT NOT NULL DEFAULT '',
    pin_sha256      TEXT NOT NULL DEFAULT '',
    -- Operator-supplied environment additions (a JSON object). The runner still
    -- starts from a scrubbed base; this only adds paths like JAVA_HOME.
    env             TEXT NOT NULL DEFAULT '{}',
    created_by      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_project_checks_project ON project_checks(project_id, name);

-- Every check execution, kept as an audit trail. A row is inserted as
-- ``running`` before the process starts, so the partial unique index below is a
-- cross-process single-flight lease (the Perun and guard MCP servers are
-- different processes). A crashed run leaves a row that the next call reaps.
CREATE TABLE IF NOT EXISTS check_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL,
    check_name      TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'declared',
    command         TEXT NOT NULL DEFAULT '',
    requester       TEXT NOT NULL DEFAULT '',
    trigger         TEXT NOT NULL DEFAULT '',
    proposal_id     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT '',
    exit_code       INTEGER,
    timeout_seconds INTEGER NOT NULL DEFAULT 120,
    started_epoch   REAL NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    output          TEXT NOT NULL DEFAULT '',
    output_truncated INTEGER NOT NULL DEFAULT 0,
    content_sha256  TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_check_runs_project ON check_runs(project_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_check_runs_one_running
    ON check_runs(project_id) WHERE status='running';

-- A proposal verification asks the operator once and remembers the card. If the
-- room's wait window closes first, the answer still arrives later; the next call
-- reads this row instead of asking a second time. ``used_at`` makes it one-shot,
-- and the sha256 binds the answer to the content the operator actually saw.
CREATE TABLE IF NOT EXISTS check_confirmations (
    id           TEXT PRIMARY KEY,
    project_id   INTEGER NOT NULL,
    proposal_id  TEXT NOT NULL,
    target       TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    attention_id INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    expires_at   REAL NOT NULL DEFAULT 0,
    used_at      TEXT NOT NULL DEFAULT '',
    UNIQUE(proposal_id, target, sha256)
);
CREATE INDEX IF NOT EXISTS idx_check_confirmations_target
    ON check_confirmations(proposal_id, sha256);
-- An index, not only the table constraint: a database whose table was created
-- by an earlier build gains the uniqueness on the next startup.
CREATE UNIQUE INDEX IF NOT EXISTS idx_check_confirmations_unique
    ON check_confirmations(proposal_id, target, sha256);
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
        # Autonomous work pause for shared-project rooms (v0.10.77).
        ("work_paused", "INTEGER DEFAULT 0"),
        ("work_paused_at", "TEXT DEFAULT ''"),
        ("work_paused_by", "TEXT DEFAULT ''"),
    ],
    "runs": [
        ("model_explicit", "INTEGER DEFAULT 0"),
        # Session-centric runner (v0.10.0): the prompt is admitted to an OpenCode
        # session by idempotent id, and the reply is delivered exactly once.
        ("admitted_at", "TEXT DEFAULT ''"),
        ("reply_sent_at", "TEXT DEFAULT ''"),
        ("lease_epoch", "INTEGER DEFAULT 0"),
        ("created_at", "TEXT DEFAULT ''"),
        # A TUI run is an external terminal, not a Perun child process: a restart
        # must not mark it interrupted while the session is still open. ``pid``
        # lets the next process tell "still open" from "closed while we were away".
        ("standalone", "INTEGER DEFAULT 0"),
        ("pid", "INTEGER DEFAULT 0"),
        # In-memory options that used to vanish on a restart (`skip_instructions`,
        # `use_prompt_file`), so a resumed run is the run the caller asked for.
        ("run_options", "TEXT DEFAULT '{}'"),
        # Coroutine attribution (v0.11). A fire is a normal run; these columns
        # let the scheduler account it exactly once and the UI link it back.
        ("coroutine_id", "INTEGER"),
        ("coroutine_cycle_id", "TEXT DEFAULT ''"),
        ("coroutine_iteration", "INTEGER DEFAULT 0"),
        ("coroutine_fire_key", "TEXT DEFAULT ''"),
        ("coroutine_trigger", "TEXT DEFAULT ''"),
        ("coroutine_outcome", "TEXT DEFAULT ''"),
        ("coroutine_accounting_at", "TEXT DEFAULT ''"),
        ("coroutine_accounted_at", "TEXT DEFAULT ''"),
    ],
    # Per-session token/cost snapshot from the OpenCode service. A session total
    # is authoritative, so it is stored (overwritten), never accumulated.
    "sessions": [
        ("tokens_in", "INTEGER DEFAULT 0"),
        ("tokens_out", "INTEGER DEFAULT 0"),
        ("cache_read", "INTEGER DEFAULT 0"),
        ("cost_usd", "REAL DEFAULT 0"),
        ("usage_at", "TEXT DEFAULT ''"),
        ("usage_models", "TEXT DEFAULT '{}'"),
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
        ("execution_mode", "TEXT DEFAULT ''"),
        ("claimed_at", "TEXT DEFAULT ''"),
        ("claim_expires_at", "INTEGER DEFAULT 0"),
        ("claim_snapshot", "TEXT DEFAULT ''"),
        ("executed_by", "TEXT DEFAULT ''"),
        # Target digests captured at approval (v0.8.55): claim refuses a target
        # that changed between approval and external execution.
        ("approval_snapshot", "TEXT DEFAULT ''"),
        # Package-level external handoff approval (v0.11). The uploaded package
        # is staged, reviewed by digest, then applied; pre/post hashes live in
        # package_manifest and the archive digest binds the exact bytes.
        ("package_state", "TEXT DEFAULT ''"),
        ("package_digest", "TEXT DEFAULT ''"),
        ("package_manifest", "TEXT DEFAULT ''"),
        ("package_preview", "TEXT DEFAULT ''"),
        ("package_uploaded_at", "TEXT DEFAULT ''"),
        ("package_expires_at", "INTEGER DEFAULT 0"),
        ("package_approved_by", "TEXT DEFAULT ''"),
        ("package_approved_at", "TEXT DEFAULT ''"),
        ("package_applied_at", "TEXT DEFAULT ''"),
        ("package_error", "TEXT DEFAULT ''"),
        ("claim_hashes", "TEXT DEFAULT ''"),
        # Per-target operations (v0.12): the changeset source of truth. A
        # legacy row is migrated from action_type + targets on startup.
        ("operations", "TEXT DEFAULT '[]'"),
    ],
    # The room binding was not persisted before v0.8.40, so a transfer
    # restored after a restart could not be continued (it looked like it
    # belonged to another room).
    "package_transfers": [
        ("room_id", "TEXT DEFAULT ''"),
    ],
    # Coroutine scheduling parks the next cycle slot separately while a loop
    # spends next_fire_epoch on its iterations.
    "coroutines": [
        ("next_cycle_epoch", "REAL DEFAULT 0"),
        ("dispatch_failures", "INTEGER DEFAULT 0"),
    ],
    # A database whose ``project_checks`` was created by the first checks build
    # gains the operator-declared environment map on the next startup.
    "project_checks": [
        ("env", "TEXT NOT NULL DEFAULT '{}'"),
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
    # ``execution_mode`` used to default to 'perun', so unclaimed proposals from
    # older builds claim a pipeline that never ran. Only rewrite rows that have
    # not executed yet.
    try:
        conn.execute(
            "UPDATE action_proposals SET execution_mode='' "
            "WHERE execution_mode='perun' AND status IN ('pending','approved') "
            "AND executed_by=''"
        )
    except sqlite3.OperationalError:
        pass
    # v0.12 changesets: ``operations`` is the source of truth. One-time migrate
    # typed rows written with the old global action_type. ``other`` rows keep an
    # empty operations list and are refused by the new guards.
    try:
        rows = conn.execute(
            "SELECT id, action_type, targets, operations FROM action_proposals"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        raw_ops = row["operations"] or ""
        if raw_ops and raw_ops != "[]":
            continue
        action = str(row["action_type"] or "").strip().lower()
        if action not in ("create", "edit", "delete"):
            continue
        try:
            targets = json.loads(row["targets"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(targets, list):
            continue
        ops = [
            {"path": str(t), "op": action}
            for t in targets
            if isinstance(t, str) and t.strip()
        ]
        if ops:
            conn.execute(
                "UPDATE action_proposals SET operations=? WHERE id=?",
                (json.dumps(ops, ensure_ascii=False), row["id"]),
            )
    # Cancellations were initially recorded as ``rejected`` with a
    # ``cancelled: <reason>`` result. Give them their own terminal status.
    try:
        conn.execute(
            "UPDATE action_proposals SET status='cancelled', "
            "result=TRIM(SUBSTR(result, ?)) "
            "WHERE status='rejected' AND result LIKE 'cancelled:%'",
            (len("cancelled:") + 1,),
        )
        conn.execute(
            "UPDATE action_proposals SET status='cancelled', result='' "
            "WHERE status='rejected' AND result='cancelled'"
        )
    except sqlite3.OperationalError:
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
SCHEMA_VERSION = 24


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
