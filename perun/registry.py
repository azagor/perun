"""Project registry, sessions, runs, activity feed, and bridge links."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import config, db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return s or "project"


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
def add_project(
    name: str,
    path: str,
    *,
    root: str = "",
    kind: str = "dir",
    description: str = "",
    tags: str = "",
    model: str = "",
    agent: str = "",
    color: str = "",
    instructions: str = "",
) -> dict[str, Any]:
    slug = slugify(name)
    ts = now_iso()
    with db.db() as conn:
        # ensure the slug is unique
        base, n = slug, 1
        while db.one(conn, "SELECT 1 FROM projects WHERE slug=?", (slug,)):
            n += 1
            slug = f"{base}-{n}"
        conn.execute(
            """INSERT INTO projects
               (slug,name,path,root,kind,description,tags,model,agent,color,
                instructions,created_at,updated_at,last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (slug, name, path, root, kind, description, tags, model, agent,
             color, instructions, ts, ts, ts),
        )
        row = db.one(conn, "SELECT * FROM projects WHERE slug=?", (slug,))
        return project_dict(conn, row)


def update_project(slug: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {
        "name", "path", "root", "kind", "description", "tags", "model",
        "agent", "favorite", "enabled", "color", "instructions",
        "manifest", "manifest_path", "manifest_loaded_at",
        "git_remote", "git_branch", "git_provider",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return get_project(slug)
    sets["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in sets)
    with db.db() as conn:
        conn.execute(f"UPDATE projects SET {cols} WHERE slug=?",
                     (*sets.values(), slug))
        row = db.one(conn, "SELECT * FROM projects WHERE slug=?", (slug,))
        return project_dict(conn, row) if row else None


def touch_project(project_id: int, seen: bool = False) -> None:
    with db.db() as conn:
        if seen:
            conn.execute(
                "UPDATE projects SET last_seen_at=?, updated_at=? WHERE id=?",
                (now_iso(), now_iso(), project_id),
            )
        else:
            conn.execute("UPDATE projects SET updated_at=? WHERE id=?",
                         (now_iso(), project_id))


def get_project(ref: str | int | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    with db.db() as conn:
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            row = db.one(conn, "SELECT * FROM projects WHERE id=?", (int(ref),))
        else:
            row = db.one(conn, "SELECT * FROM projects WHERE slug=?", (ref,))
        return project_dict(conn, row) if row else None


def get_project_by_path(path: str) -> dict[str, Any] | None:
    import os
    norm = os.path.normcase(os.path.abspath(path))
    with db.db() as conn:
        for row in db.rows(conn, "SELECT * FROM projects"):
            if os.path.normcase(os.path.abspath(row["path"])) == norm:
                return project_dict(conn, row)
    return None


def project_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    pid = row["id"]
    d = dict(row)
    d["favorite"] = bool(d.get("favorite"))
    d["enabled"] = bool(d.get("enabled"))
    # The control plane is the self-registered Perun repo; the dashboard uses
    # this flag to badge it and to protect it from deletion.
    d["is_control"] = config.is_control_path(d.get("path"))
    # Expose the parsed manifest (front-matter data only; the body is served by
    # the dedicated /manifest endpoint to keep list payloads small).
    raw_manifest = d.get("manifest") or ""
    d["has_manifest"] = bool(raw_manifest)
    if raw_manifest:
        try:
            snap = json.loads(raw_manifest)
            d["manifest"] = snap.get("data") or {}
        except Exception:
            d["manifest"] = {}
    else:
        d["manifest"] = {}
    d["session_count"] = db.one(
        conn, "SELECT COUNT(*) c FROM sessions WHERE project_id=?", (pid,)
    )["c"]
    d["memory_count"] = db.one(
        conn, "SELECT COUNT(*) c FROM memory WHERE project_id=?", (pid,)
    )["c"]
    d["chunk_count"] = db.one(
        conn, "SELECT COUNT(*) c FROM doc_chunks WHERE project_id=?", (pid,)
    )["c"]
    last = db.one(
        conn, "SELECT ts,kind,message FROM activity WHERE project_id=? ORDER BY id DESC LIMIT 1",
        (pid,),
    )
    d["last_activity"] = dict(last) if last else None
    return d


def list_projects() -> list[dict[str, Any]]:
    with db.db() as conn:
        rows = db.rows(conn, "SELECT * FROM projects ORDER BY favorite DESC, name COLLATE NOCASE")
        return [project_dict(conn, r) for r in rows]


def delete_project(slug: str) -> bool:
    with db.db() as conn:
        cur = conn.execute("DELETE FROM projects WHERE slug=?", (slug,))
        return cur.rowcount > 0


def set_project_manifest(slug: str, snapshot: str, path: str = "") -> None:
    """Store the parsed manifest snapshot read from disk."""
    ts = now_iso()
    with db.db() as conn:
        conn.execute(
            "UPDATE projects SET manifest=?, manifest_path=?, manifest_loaded_at=?, "
            "updated_at=? WHERE slug=?",
            (snapshot, path, ts, ts, slug),
        )


def get_project_manifest(slug: str) -> dict[str, Any]:
    """Return the stored manifest snapshot ``{data, body, source}`` (or empty)."""
    with db.db() as conn:
        row = db.one(conn, "SELECT manifest FROM projects WHERE slug=?", (slug,))
    if not row or not row["manifest"]:
        return {"data": {}, "body": "", "source": ""}
    try:
        snap = json.loads(row["manifest"])
        return {"data": snap.get("data") or {}, "body": snap.get("body") or "",
                "source": snap.get("source") or ""}
    except Exception:
        return {"data": {}, "body": "", "source": ""}


# --------------------------------------------------------------------------- #
# Sessions (Perun's record of OpenCode sessions per project)
# --------------------------------------------------------------------------- #
def create_session(project_id: int, opencode_session_id: str = "",
                   title: str = "") -> dict[str, Any]:
    ts = now_iso()
    with db.db() as conn:
        cur = conn.execute(
            """INSERT INTO sessions
               (project_id,opencode_session_id,title,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?)""",
            (project_id, opencode_session_id, title, "active", ts, ts),
        )
        row = db.one(conn, "SELECT * FROM sessions WHERE id=?", (cur.lastrowid,))
        return dict(row)


def get_session(pk: int) -> dict[str, Any] | None:
    with db.db() as conn:
        row = db.one(conn, "SELECT * FROM sessions WHERE id=?", (pk,))
        return dict(row) if row else None


def find_session_by_opencode(project_id: int, oc_id: str) -> dict[str, Any] | None:
    with db.db() as conn:
        row = db.one(
            conn, "SELECT * FROM sessions WHERE project_id=? AND opencode_session_id=?",
            (project_id, oc_id),
        )
        return dict(row) if row else None


def update_session(pk: int, **fields: Any) -> None:
    allowed = {"opencode_session_id", "title", "status", "message_count", "last_error"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    sets["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in sets)
    with db.db() as conn:
        conn.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*sets.values(), pk))


def list_sessions(project_id: int, limit: int = 100) -> list[dict[str, Any]]:
    with db.db() as conn:
        rows = db.rows(
            conn,
            "SELECT * FROM sessions WHERE project_id=? ORDER BY updated_at DESC LIMIT ?",
            (project_id, limit),
        )
        return [dict(r) for r in rows]


def delete_session(pk: int) -> None:
    with db.db() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (pk,))


# --------------------------------------------------------------------------- #
# Runs (one agent invocation)
# --------------------------------------------------------------------------- #
def create_run(run_uid: str, project_id: int, prompt: str, *,
               session_pk: int | None = None, opencode_session_id: str = "",
               model: str = "", agent: str = "") -> dict[str, Any]:
    with db.db() as conn:
        conn.execute(
            """INSERT INTO runs
               (run_uid,project_id,session_pk,opencode_session_id,prompt,model,agent,status)
               VALUES (?,?,?,?,?,?,?, 'queued')""",
            (run_uid, project_id, session_pk, opencode_session_id, prompt, model, agent),
        )
        row = db.one(conn, "SELECT * FROM runs WHERE run_uid=?", (run_uid,))
        return dict(row)


def update_run(run_uid: str, **fields: Any) -> None:
    allowed = {"session_pk", "opencode_session_id", "status", "exit_code",
               "started_at", "ended_at", "output", "error"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k}=?" for k in sets)
    with db.db() as conn:
        conn.execute(f"UPDATE runs SET {cols} WHERE run_uid=?", (*sets.values(), run_uid))


def get_run(run_uid: str) -> dict[str, Any] | None:
    with db.db() as conn:
        row = db.one(conn, "SELECT * FROM runs WHERE run_uid=?", (run_uid,))
        return dict(row) if row else None


def list_runs(project_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with db.db() as conn:
        if project_id:
            rows = db.rows(
                conn,
                "SELECT * FROM runs WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            )
        else:
            rows = db.rows(conn, "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def list_active_runs() -> list[dict[str, Any]]:
    """Runs that are queued or running right now, joined with their project.

    This is the "an agent is actively working" signal the dashboard animates on:
    unlike the in-memory :meth:`orchestrator.Orchestrator.active` list (which only
    knows about live child processes in *this* server), it survives a restart and
    carries the owning project's slug/name so each project card can light up.
    """
    with db.db() as conn:
        rows = db.rows(
            conn,
            """SELECT r.run_uid, r.status, r.prompt, r.model, r.agent,
                      r.session_pk, r.opencode_session_id,
                      r.started_at,
                      p.slug AS project_slug, p.name AS project_name,
                      p.path AS project_path
               FROM runs r JOIN projects p ON p.id = r.project_id
               WHERE r.status IN ('queued', 'running')
               ORDER BY r.id DESC""",
        )
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Activity feed
# --------------------------------------------------------------------------- #
def log_activity(project_id: int | None, kind: str, message: str,
                 meta: dict[str, Any] | None = None) -> dict[str, Any]:
    with db.db() as conn:
        cur = conn.execute(
            "INSERT INTO activity (project_id,ts,kind,message,meta) VALUES (?,?,?,?,?)",
            (project_id or 0, now_iso(), kind, message, json.dumps(meta or {})),
        )
        row = db.one(conn, "SELECT * FROM activity WHERE id=?", (cur.lastrowid,))
        return dict(row)


def list_activity(project_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with db.db() as conn:
        if project_id:
            rows = db.rows(
                conn,
                "SELECT * FROM activity WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            )
        else:
            rows = db.rows(conn, "SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d.get("meta") or "{}")
            except Exception:
                d["meta"] = {}
            out.append(d)
        return out


# --------------------------------------------------------------------------- #
# Telegram bridge links
# --------------------------------------------------------------------------- #
def get_bridge_link(chat_id: str) -> dict[str, Any] | None:
    with db.db() as conn:
        row = db.one(conn, "SELECT * FROM bridge_links WHERE chat_id=?", (chat_id,))
        return dict(row) if row else None


def set_bridge_link(chat_id: str, project_id: int | None,
                    session_pk: int | None) -> None:
    with db.db() as conn:
        conn.execute(
            """INSERT INTO bridge_links (chat_id,project_id,session_pk,updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 project_id=excluded.project_id,
                 session_pk=excluded.session_pk,
                 updated_at=excluded.updated_at""",
            (chat_id, project_id, session_pk, now_iso()),
        )
