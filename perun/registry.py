"""Project registry, sessions, runs, activity feed, and bridge links."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import Any

from . import config, db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Latin letters that carry no canonical decomposition, so NFD alone would drop
# them (``ł`` is the one Polish letter in this set). Everything else —
# ``ż ó ć ę ś ź ń ą`` — is handled by decomposition + stripping combining marks.
_TRANSLITERATE = str.maketrans({
    "ł": "l", "Ł": "l", "đ": "d", "ð": "d", "ø": "o", "þ": "th",
    "ß": "ss", "æ": "ae", "œ": "oe", "ĳ": "ij",
})


def slugify(value: str) -> str:
    """URL-safe slug of *value*.

    Diacritics are transliterated (``Zażółć`` → ``zazolc``) rather than dropped,
    so Polish project names keep readable, collision-resistant slugs. A name
    with no alphanumerics at all (e.g. emoji-only) gets a stable hash-derived
    slug so it stays distinguishable from the ``project`` placeholder used for
    an empty name.
    """
    raw = (value or "").strip()
    if not raw:
        return "project"
    s = raw.lower().translate(_TRANSLITERATE)
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if not s:
        digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:8]
        return f"proj-{digest}"
    return s


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
) -> dict[str, Any]:
    if not (name or "").strip():
        raise ValueError("project name is required")
    # ``projects.path`` is UNIQUE, but its collision message says nothing about
    # *which* constraint failed — report duplicate paths as a path error up
    # front instead of burning every slug candidate and blaming the slug.
    # Comparison is case-sensitive (Windows folds case in lookups, but two
    # projects registered under differently-cased paths are distinct entries)
    # and ignores trailing separators. Nesting is allowed: registering a path
    # inside another project creates a legitimate sub-project.
    try:
        norm = os.path.normpath(os.path.abspath(str(path)))
    except (OSError, ValueError):
        norm = os.path.normpath(str(path))
    clash = None
    with db.db() as conn:
        for row in db.rows(conn, "SELECT slug, path FROM projects"):
            if row["path"] == path:
                clash = row
                break
            try:
                other = os.path.normpath(os.path.abspath(str(row["path"])))
            except (OSError, ValueError):
                continue
            if other == norm:
                clash = row
                break
    if clash is not None:
        raise ValueError(
            f"path already registered: {path} (project '{clash['slug']}')"
        )
    # Keep the slug short enough that numeric suffixes still fit in 60 chars.
    base_slug = slugify(name)[:54].strip("-") or "project"
    ts = now_iso()
    # Suffixes start at 2 to match the historical behavior (same → same-2).
    # The counter is unbounded: 22 projects sharing one name must all succeed.
    slug = base_slug
    suffix = 1
    project: dict[str, Any] | None = None
    while suffix <= 10000:
        try:
            with db.db() as conn:
                existing = db.one(conn, "SELECT 1 FROM projects WHERE slug=?", (slug,))
                if not existing:
                    conn.execute(
                        """INSERT INTO projects
                           (slug,name,path,root,kind,description,tags,model,agent,color,
                            created_at,updated_at,last_seen_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (slug, name, path, root, kind, description, tags, model, agent,
                         color, ts, ts, ts),
                    )
                    row = db.one(conn, "SELECT * FROM projects WHERE slug=?", (slug,))
                    project = project_dict(conn, row)
                    break
        except sqlite3.IntegrityError:
            # Lost the race for this slug (or hit path UNIQUE in a race); the
            # path check above covers sequential duplicates — try next slug.
            pass
        suffix += 1
        slug = f"{base_slug}-{suffix}"
    if project is None:
        raise ValueError("could not generate a unique project slug")
    # Registration paths that bypass discovery (CLI ``perun add``, the create
    # wizard) must still get manifest data, otherwise the dashboard overview
    # shows the project without its manifest. Best-effort: a broken perun.md
    # never fails registration.
    try:
        project = load_manifest_from_disk(project["slug"]) or project
    except Exception:
        pass
    return project


def update_project(slug: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {
        "name", "path", "root", "kind", "description", "tags", "model",
        "agent", "favorite", "enabled", "color",
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
        # A slug always wins: a project named ``123`` gets slug ``123`` and
        # must stay reachable. Row ids remain the fallback for numeric refs.
        row = None
        if not isinstance(ref, int):
            row = db.one(conn, "SELECT * FROM projects WHERE slug=?", (ref,))
        if row is None and (isinstance(ref, int) or str(ref).isdigit()):
            row = db.one(conn, "SELECT * FROM projects WHERE id=?", (int(ref),))
        return project_dict(conn, row) if row else None


def get_project_by_path(path: str) -> dict[str, Any] | None:
    """Resolve *path* to a registered project.

    Resolution order:

    1. case-sensitive exact match (so two projects registered under
       differently-cased paths each resolve to themselves),
    2. ``normcase`` exact match (Windows case-insensitive lookup, trailing
       separators ignored),
    3. realpath exact match (a path reached through a symlink resolves to the
       project stored under its real location),
    4. closest registered **ancestor** — a session started inside a project
       subdirectory resolves to the enclosing project.
    """
    try:
        raw = os.path.normpath(os.path.abspath(str(path)))
    except (OSError, ValueError):
        raw = os.path.normpath(str(path))
    folded = os.path.normcase(raw)
    try:
        real = os.path.normcase(os.path.realpath(str(path)))
    except (OSError, ValueError):
        real = folded
    with db.db() as conn:
        rows = list(db.rows(conn, "SELECT * FROM projects"))

        def _raw_of(row: sqlite3.Row) -> str:
            try:
                return os.path.normpath(os.path.abspath(str(row["path"])))
            except (OSError, ValueError):
                return os.path.normpath(str(row["path"]))

        for row in rows:  # 1) exact, case-sensitive
            if _raw_of(row) == raw:
                return project_dict(conn, row)
        for row in rows:  # 2) exact, case-folded
            if os.path.normcase(_raw_of(row)) == folded:
                return project_dict(conn, row)
        for row in rows:  # 3) exact, symlinks resolved
            try:
                stored_real = os.path.normcase(os.path.realpath(str(row["path"])))
            except (OSError, ValueError):
                continue
            if stored_real == real:
                return project_dict(conn, row)
        best: sqlite3.Row | None = None  # 4) closest ancestor
        best_len = -1
        for row in rows:
            try:
                stored_real = os.path.normcase(os.path.realpath(str(row["path"])))
            except (OSError, ValueError):
                continue
            prefix = stored_real.rstrip(os.sep) + os.sep
            if len(stored_real) > best_len and real.startswith(prefix):
                best = row
                best_len = len(stored_real)
        if best is not None:
            return project_dict(conn, best)
    return None


def project_dict(conn: sqlite3.Connection, row: sqlite3.Row,
                 session_count: int | None = None,
                 memory_count: int | None = None,
                 chunk_count: int | None = None,
                 last_activity: dict[str, Any] | None = None) -> dict[str, Any]:
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
    d["session_count"] = session_count if session_count is not None else db.one(
        conn, "SELECT COUNT(*) c FROM sessions WHERE project_id=?", (pid,)
    )["c"]
    d["memory_count"] = memory_count if memory_count is not None else db.one(
        conn, "SELECT COUNT(*) c FROM memory WHERE project_id=?", (pid,)
    )["c"]
    d["chunk_count"] = chunk_count if chunk_count is not None else db.one(
        conn, "SELECT COUNT(*) c FROM doc_chunks WHERE project_id=?", (pid,)
    )["c"]
    if last_activity is not None:
        d["last_activity"] = last_activity
    else:
        last = db.one(
            conn, "SELECT ts,kind,message FROM activity WHERE project_id=? ORDER BY id DESC LIMIT 1",
            (pid,),
        )
        d["last_activity"] = dict(last) if last else None
    return d


def list_projects() -> list[dict[str, Any]]:
    with db.db() as conn:
        rows = db.rows(conn, "SELECT * FROM projects ORDER BY favorite DESC, name COLLATE NOCASE")
        if not rows:
            return []
        pids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(pids))
        sessions_counts = dict(conn.execute(
            f"SELECT project_id, COUNT(*) FROM sessions WHERE project_id IN ({placeholders}) GROUP BY project_id",
            pids,
        ))
        memory_counts = dict(conn.execute(
            f"SELECT project_id, COUNT(*) FROM memory WHERE project_id IN ({placeholders}) GROUP BY project_id",
            pids,
        ))
        chunk_counts = dict(conn.execute(
            f"SELECT project_id, COUNT(*) FROM doc_chunks WHERE project_id IN ({placeholders}) GROUP BY project_id",
            pids,
        ))
        last_activities: dict[int, dict[str, Any]] = {}
        # Only the newest activity row per project: the table grows without
        # bound, and this runs on every /api/health hit.
        for r in conn.execute(
            f"SELECT project_id, ts, kind, message FROM activity "
            f"WHERE id IN (SELECT MAX(id) FROM activity "
            f"WHERE project_id IN ({placeholders}) GROUP BY project_id)",
            pids,
        ):
            last_activities[r["project_id"]] = dict(r)
        return [
            project_dict(
                conn, r,
                session_count=sessions_counts.get(r["id"], 0),
                memory_count=memory_counts.get(r["id"], 0),
                chunk_count=chunk_counts.get(r["id"], 0),
                last_activity=last_activities.get(r["id"]),
            )
            for r in rows
        ]


def delete_project(slug: str) -> bool:
    with db.db() as conn:
        row = db.one(conn, "SELECT id FROM projects WHERE slug=?", (slug,))
        if not row:
            return False
        pid = row["id"]
        # Explicit cleanup for related rows. SQLite FKs are not enabled by default
        # (global facts use project_id=0), so delete every related table manually.
        conn.execute("DELETE FROM memory WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM activity WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM doc_chunks WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM runs WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM sessions WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM project_chats WHERE project_id=?", (pid,))
        conn.execute("UPDATE bridge_links SET project_id=NULL, session_pk=NULL WHERE project_id=?", (pid,))
        cur = conn.execute("DELETE FROM projects WHERE id=?", (pid,))
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


# --------------------------------------------------------------------------- #
# Shared project chats
# --------------------------------------------------------------------------- #
def get_project_chat(project_id: int) -> dict[str, Any] | None:
    with db.db() as conn:
        row = db.one(conn, "SELECT * FROM project_chats WHERE project_id=?", (project_id,))
        return dict(row) if row else None


def get_project_chat_by_room(room_id: str) -> dict[str, Any] | None:
    """Look up the managed project chat bound to *room_id*."""
    if not room_id:
        return None
    with db.db() as conn:
        row = db.one(conn, "SELECT * FROM project_chats WHERE room_id=?", (room_id,))
        return dict(row) if row else None


def set_project_chat(
    project_id: int,
    room_id: str,
    managed_by: str,
    state: str = "active",
    manifest_rev: int = 1,
) -> dict[str, Any]:
    ts = now_iso()
    with db.db() as conn:
        conn.execute(
            """INSERT INTO project_chats
               (project_id, room_id, managed_by, state, manifest_rev, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(project_id) DO UPDATE SET
                 room_id=excluded.room_id,
                 managed_by=excluded.managed_by,
                 state=excluded.state,
                 manifest_rev=excluded.manifest_rev,
                 updated_at=excluded.updated_at""",
            (project_id, room_id, managed_by, state, manifest_rev, ts, ts),
        )
        row = db.one(conn, "SELECT * FROM project_chats WHERE project_id=?", (project_id,))
        return dict(row) if row else {}


def update_project_chat(project_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {"room_id", "managed_by", "state", "manifest_rev", "last_sync_at"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return get_project_chat(project_id)
    sets["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in sets)
    with db.db() as conn:
        conn.execute(f"UPDATE project_chats SET {cols} WHERE project_id=?",
                     (*sets.values(), project_id))
        row = db.one(conn, "SELECT * FROM project_chats WHERE project_id=?", (project_id,))
        return dict(row) if row else None


def delete_project_chat(project_id: int) -> bool:
    with db.db() as conn:
        cur = conn.execute("DELETE FROM project_chats WHERE project_id=?", (project_id,))
        return cur.rowcount > 0


def load_manifest_from_disk(slug: str) -> dict[str, Any] | None:
    """Load ``perun.md`` for a registered project and refresh its manifest data.

    Stores the snapshot (shown by the dashboard overview/manifest tabs) and
    applies the fields the manifest declares, so projects registered without
    discovery (``perun add``, the create wizard) are as self-describing as
    discovered ones. Returns the refreshed project, or None if unknown.
    """
    from . import manifest as manifest_module

    project = get_project(slug)
    if not project:
        return None
    man = manifest_module.load(project["path"])
    if not man["found"]:
        return project
    declared = manifest_module.to_project_fields(man["data"])
    if declared:
        update_project(slug, **declared)  # unknown keys are filtered out
    set_project_manifest(slug, manifest_module.snapshot(man), man["path"])
    return get_project(slug)


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
               model: str = "", model_explicit: bool = False,
               agent: str = "") -> dict[str, Any]:
    with db.db() as conn:
        conn.execute(
            """INSERT INTO runs
               (run_uid,project_id,session_pk,opencode_session_id,prompt,
                model,model_explicit,agent,status)
               VALUES (?,?,?,?,?,?,?,?, 'queued')""",
            (run_uid, project_id, session_pk, opencode_session_id, prompt,
             model, int(bool(model_explicit)), agent),
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
            """SELECT r.run_uid, r.status, r.prompt, r.model, r.model_explicit,
                      r.agent,
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
# Tasks (reusable project actions)
# --------------------------------------------------------------------------- #
def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["tools"] = json.loads(d.get("tools") or "[]")
    except Exception:
        d["tools"] = []
    try:
        d["skills"] = json.loads(d.get("skills") or "[]")
    except Exception:
        d["skills"] = []
    return d


def create_task(project_id: int, *, name: str, kind: str = "prompt",
                prompt: str = "", tools: list[str] | None = None,
                skills: list[str] | None = None, enabled: bool = True,
                sort_order: int = 0) -> dict[str, Any]:
    ts = now_iso()
    with db.db() as conn:
        conn.execute(
            """INSERT INTO tasks
               (project_id, name, kind, prompt, tools, skills, enabled,
                sort_order, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (project_id, name, kind, prompt,
             json.dumps(tools or []), json.dumps(skills or []),
             int(bool(enabled)), sort_order, ts, ts),
        )
        row = db.one(conn, "SELECT * FROM tasks WHERE id=last_insert_rowid()")
        return _task_dict(row)


def update_task(task_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {"name", "kind", "prompt", "tools", "skills", "enabled", "sort_order"}
    sets: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("tools", "skills"):
            sets[k] = json.dumps(v if isinstance(v, list) else [])
        elif k == "enabled":
            sets[k] = int(bool(v))
        else:
            sets[k] = v
    if not sets:
        return get_task(task_id)
    sets["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in sets)
    with db.db() as conn:
        conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*sets.values(), task_id))
        row = db.one(conn, "SELECT * FROM tasks WHERE id=?", (task_id,))
        return _task_dict(row) if row else None


def get_task(task_id: int) -> dict[str, Any] | None:
    with db.db() as conn:
        row = db.one(conn, "SELECT * FROM tasks WHERE id=?", (task_id,))
        return _task_dict(row) if row else None


def delete_task(task_id: int) -> None:
    with db.db() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))


def list_tasks(project_id: int, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    with db.db() as conn:
        sql = "SELECT * FROM tasks WHERE project_id=?"
        params: list[Any] = [project_id]
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY sort_order, id"
        rows = db.rows(conn, sql, params)
        return [_task_dict(r) for r in rows]


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
