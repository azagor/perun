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
    agent_models: dict[str, str] | None = None,
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
    agent_models_json = json.dumps(agent_models or {}, ensure_ascii=False, separators=(',', ':'))
    project: dict[str, Any] | None = None
    while suffix <= 10000:
        try:
            with db.db() as conn:
                existing = db.one(conn, "SELECT 1 FROM projects WHERE slug=?", (slug,))
                if not existing:
                    conn.execute(
                        """INSERT INTO projects
                           (slug,name,path,root,kind,description,tags,model,agent,agent_models,color,
                            created_at,updated_at,last_seen_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (slug, name, path, root, kind, description, tags, model, agent,
                         agent_models_json, color, ts, ts, ts),
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
        "agent", "agent_models", "favorite", "enabled", "hidden", "color",
        "manifest", "manifest_path", "manifest_loaded_at",
        "git_remote", "git_branch", "git_provider",
    }
    sets = {k: v for k, v in fields.items() if k in allowed}
    if "agent_models" in sets:
        value = sets["agent_models"]
        if isinstance(value, dict):
            sets["agent_models"] = json.dumps(value, ensure_ascii=False,
                                               separators=(',', ':'))
        elif not isinstance(value, str):
            sets["agent_models"] = json.dumps({} if value is None else value,
                                               ensure_ascii=False)
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
    d["hidden"] = bool(d.get("hidden"))
    # Decode the per-agent model map; a bad/missing value degrades to {}.
    raw_agent_models = d.pop("agent_models", "{}")
    if isinstance(raw_agent_models, str):
        try:
            parsed = json.loads(raw_agent_models)
            d["agent_models"] = parsed if isinstance(parsed, dict) else {}
        except Exception:
            d["agent_models"] = {}
    elif isinstance(raw_agent_models, dict):
        d["agent_models"] = raw_agent_models
    else:
        d["agent_models"] = {}
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


def list_projects(*, include_hidden: bool = False) -> list[dict[str, Any]]:
    with db.db() as conn:
        if include_hidden:
            rows = db.rows(conn, "SELECT * FROM projects ORDER BY favorite DESC, name COLLATE NOCASE")
        else:
            rows = db.rows(
                conn,
                "SELECT * FROM projects WHERE hidden=0 ORDER BY favorite DESC, name COLLATE NOCASE",
            )
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
# Cross-project dependency graph
# --------------------------------------------------------------------------- #

def _dependency_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["auto_deploy"] = bool(d.get("auto_deploy"))
    return d


def _resolve_dep_target(conn: sqlite3.Connection, slug: str) -> dict[str, Any] | None:
    row = db.one(conn, "SELECT id, slug, name, path FROM projects WHERE slug=?", (slug,))
    if not row:
        return None
    return {"id": row["id"], "slug": row["slug"],
            "name": row["name"], "path": row["path"]}


def sync_dependencies(slug: str, deps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Persist the dependency declarations for *slug*.

    If *deps* is ``None``, the current manifest snapshot is re-read. The
    ``project_dependencies`` table becomes the queryable graph; the manifest
    remains the source of truth and is refreshed by ``load_manifest_from_disk``.

    Returns a status dict with ``synced``, ``removed``, ``missing`` (unregistered
    dependency slugs), and ``warnings``.
    """
    project = get_project(slug)
    if not project:
        raise ValueError(f"no such project: {slug}")
    pid = int(project["id"])

    if deps is None:
        man = get_project_manifest(slug)
        deps = (man.get("data") or {}).get("dependencies") or []
    if not isinstance(deps, list):
        return {"synced": 0, "removed": 0, "missing": [],
                "warnings": ["dependencies must be a list"]}

    warnings: list[str] = []
    ts = now_iso()
    with db.db() as conn:
        # Remove edges that no longer exist, then upsert the current set.
        conn.execute("DELETE FROM project_dependencies WHERE project_id=?", (pid,))
        synced = 0
        seen_slugs: set[str] = set()
        missing: list[str] = []
        for i, item in enumerate(deps, 1):
            if not isinstance(item, dict):
                warnings.append(f"dependency #{i} is not an object")
                continue
            dep_slug = str(item.get("slug") or "").strip()
            if not dep_slug:
                warnings.append(f"dependency #{i} is missing 'slug'")
                continue
            if dep_slug in seen_slugs:
                warnings.append(f"duplicate dependency on '{dep_slug}'")
                continue
            seen_slugs.add(dep_slug)
            target = _resolve_dep_target(conn, dep_slug)
            if target is None:
                missing.append(dep_slug)
            relationship = str(item.get("relationship") or item.get("role") or "").strip()
            deploy_command = str(item.get("deploy_command") or "").strip()
            deploy_doc = str(item.get("deploy_doc") or "").strip()
            reason = str(item.get("reason") or "").strip()
            auto_deploy = 1 if item.get("auto_deploy") else 0
            target_id = target["id"] if target else None
            conn.execute(
                """INSERT INTO project_dependencies
                   (project_id, depends_on_project_id, depends_on_slug, relationship,
                    deploy_command, deploy_doc, auto_deploy, reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, depends_on_slug) DO UPDATE SET
                     depends_on_project_id=excluded.depends_on_project_id,
                     relationship=excluded.relationship,
                     deploy_command=excluded.deploy_command,
                     deploy_doc=excluded.deploy_doc,
                     auto_deploy=excluded.auto_deploy,
                     reason=excluded.reason,
                     updated_at=excluded.updated_at""",
                (pid, target_id, dep_slug, relationship, deploy_command, deploy_doc,
                 auto_deploy, reason, ts, ts),
            )
            synced += 1
    return {"synced": synced, "removed": 0, "missing": missing, "warnings": warnings}


def get_dependencies(
    slug: str,
    *,
    relationship: str | None = None,
    include_missing: bool = False,
) -> list[dict[str, Any]]:
    """Return the projects *slug* directly depends on.

    Each result includes the dependency edge fields plus ``target``
    ``{id, slug, name, path}`` when the target is registered. Missing targets
    are included only when *include_missing* is true.
    """
    project = get_project(slug)
    if not project:
        return []
    pid = int(project["id"])
    sql = """SELECT d.*, p.slug AS target_slug, p.name AS target_name, p.path AS target_path
             FROM project_dependencies d
             LEFT JOIN projects p ON p.id = d.depends_on_project_id
             WHERE d.project_id=?"""
    params: list[Any] = [pid]
    if relationship:
        sql += " AND d.relationship=?"
        params.append(relationship)
    if not include_missing:
        sql += " AND d.depends_on_project_id IS NOT NULL"
    sql += " ORDER BY d.relationship, COALESCE(p.name, d.depends_on_slug) COLLATE NOCASE"
    out: list[dict[str, Any]] = []
    with db.db() as conn:
        for row in db.rows(conn, sql, params):
            dep = _dependency_row_to_dict(row)
            if row["depends_on_project_id"] is not None:
                dep["target"] = {
                    "id": row["depends_on_project_id"],
                    "slug": row["target_slug"],
                    "name": row["target_name"],
                    "path": row["target_path"],
                }
            else:
                dep["target"] = None
                dep["missing_slug"] = row["depends_on_slug"]
            out.append(dep)
    return out


def get_dependents(
    slug: str,
    *,
    relationship: str | None = None,
    include_missing: bool = False,
) -> list[dict[str, Any]]:
    """Return the projects that directly depend on *slug*."""
    project = get_project(slug)
    if not project:
        return []
    pid = int(project["id"])
    sql = """SELECT d.*, p.slug AS source_slug, p.name AS source_name, p.path AS source_path
             FROM project_dependencies d
             JOIN projects p ON p.id = d.project_id
             WHERE d.depends_on_project_id=?"""
    params: list[Any] = [pid]
    if relationship:
        sql += " AND d.relationship=?"
        params.append(relationship)
    sql += " ORDER BY d.relationship, p.name COLLATE NOCASE"
    out: list[dict[str, Any]] = []
    with db.db() as conn:
        for row in db.rows(conn, sql, params):
            dep = _dependency_row_to_dict(row)
            dep["source"] = {
                "id": row["project_id"],
                "slug": row["source_slug"],
                "name": row["source_name"],
                "path": row["source_path"],
            }
            out.append(dep)
    return out


def get_dependency_graph(
    slug: str,
    *,
    direction: str = "downstream",
    relationship: str | None = None,
) -> dict[str, Any]:
    """Compute the transitive dependency graph for *slug*.

    *direction* is ``downstream`` (what *slug* depends on, recursively) or
    ``upstream`` (what depends on *slug*, recursively). Cycle detection stops
    infinite recursion and reports the loop.
    """
    project = get_project(slug)
    if not project:
        return {"error": f"no such project: {slug}"}
    start_id = int(project["id"])

    nodes: dict[int, dict[str, Any]] = {
        start_id: {"id": start_id, "slug": project["slug"],
                   "name": project["name"], "path": project["path"]},
    }
    edges: list[dict[str, Any]] = []
    visited: set[int] = set()
    path: list[int] = []
    cycle: list[int] | None = None

    if direction == "downstream":
        sql = """SELECT d.depends_on_project_id AS other_id,
                        p.slug AS other_slug, p.name AS other_name, p.path AS other_path,
                        d.relationship, d.reason
                 FROM project_dependencies d
                 LEFT JOIN projects p ON p.id = d.depends_on_project_id
                 WHERE d.project_id=?"""
    else:
        sql = """SELECT d.project_id AS other_id,
                        p.slug AS other_slug, p.name AS other_name, p.path AS other_path,
                        d.relationship, d.reason
                 FROM project_dependencies d
                 JOIN projects p ON p.id = d.project_id
                 WHERE d.depends_on_project_id=?"""

    def _walk(current_id: int) -> None:
        nonlocal cycle
        if current_id in visited:
            return
        if current_id in path:
            cycle = path[path.index(current_id):] + [current_id]
            return
        path.append(current_id)
        params: list[Any] = [current_id]
        if relationship:
            params.append(relationship)
        with db.db() as conn:
            for row in db.rows(conn, sql + (" AND d.relationship=?" if relationship else ""), params):
                other_id = row["other_id"]
                if other_id is None:
                    continue
                if other_id not in nodes:
                    nodes[other_id] = {
                        "id": other_id,
                        "slug": row["other_slug"],
                        "name": row["other_name"],
                        "path": row["other_path"],
                    }
                edges.append({
                    "from": current_id,
                    "to": other_id,
                    "relationship": row["relationship"],
                    "reason": row["reason"],
                })
                _walk(other_id)
        path.pop()
        visited.add(current_id)

    _walk(start_id)
    return {
        "direction": direction,
        "start": nodes[start_id],
        "nodes": list(nodes.values()),
        "edges": edges,
        "cycle": cycle,
        "depth": max((len(path) for path in _paths_from(start_id, edges, direction)), default=0),
    }


def _paths_from(start_id: int, edges: list[dict[str, Any]], direction: str) -> list[list[int]]:
    """Return all root-to-leaf paths in the computed graph."""
    by_node: dict[int, list[int]] = {}
    for e in edges:
        by_node.setdefault(e["from"], []).append(e["to"])
    paths: list[list[int]] = []

    def _walk(node: int, current: list[int]) -> None:
        children = by_node.get(node, [])
        if not children:
            paths.append(list(current))
            return
        for child in children:
            if child in current:
                continue
            current.append(child)
            _walk(child, current)
            current.pop()

    _walk(start_id, [start_id])
    return paths


def analyze_impact(
    slug: str,
    *,
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    """Find every project impacted by a change to *slug*.

    Returns the upstream graph plus a flattened list of impacted projects,
    ordered by graph depth (closest dependents first). The optional
    *changed_files* list is recorded so deploy planning can later filter by
    interface surface.
    """
    graph = get_dependency_graph(slug, direction="upstream")
    if graph.get("error"):
        return graph

    start_id = graph["start"]["id"]
    by_id: dict[int, dict[str, Any]] = {n["id"]: n for n in graph["nodes"]}
    edges = graph["edges"]

    # Build depth map via BFS from the start project.
    depth: dict[int, int] = {start_id: 0}
    queue = [start_id]
    while queue:
        current = queue.pop(0)
        for e in edges:
            if e.get("from") != current:
                continue
            other = e["to"]
            if other not in depth:
                depth[other] = depth[current] + 1
                queue.append(other)

    impacted: list[dict[str, Any]] = []
    for node_id, node in by_id.items():
        if node_id == start_id:
            continue
        # Collect reasons from every edge that points to this node.
        reasons: list[str] = []
        for e in edges:
            if e.get("to") == node_id and e.get("reason"):
                reasons.append(e["reason"])
        impacted.append({
            "id": node_id,
            "slug": node["slug"],
            "name": node["name"],
            "path": node["path"],
            "depth": depth.get(node_id, -1),
            "reasons": reasons,
        })
    impacted.sort(key=lambda x: (x["depth"], x["name"].lower()))

    return {
        "project": graph["start"],
        "changed_files": changed_files or [],
        "impacted": impacted,
        "cycle": graph.get("cycle"),
        "total": len(impacted),
    }


def get_deploy_plan(
    slug: str,
    *,
    changed_files: list[str] | None = None,
    include_auto: bool = False,
) -> dict[str, Any]:
    """Build an ordered deploy checklist for a change to *slug*.

    The plan traverses dependents (upstream impact), then includes the changed
    project itself if it has a deploy command/doc. Auto-deploy dependencies are
    flagged but only included in the checklist when *include_auto* is true.

    Each checklist item carries ``project``, ``deploy_command``, ``deploy_doc``,
    ``auto_deploy``, and ``reason``.
    """
    impact = analyze_impact(slug, changed_files=changed_files)
    if impact.get("error"):
        return impact

    project = get_project(slug)
    if not project:
        return {"error": f"no such project: {slug}"}

    items: list[dict[str, Any]] = []
    seen: set[int] = set()

    # Dependents in impact order (deepest first so leaf consumers deploy last).
    for node in reversed(impact["impacted"]):
        pid = node["id"]
        if pid in seen:
            continue
        seen.add(pid)
        edge = _deploy_edge_for(node["slug"], slug)
        if not include_auto and edge.get("auto_deploy"):
            continue
        items.append(_deploy_item(node, edge))

    # Finally the changed project itself, if it declares a deploy command.
    manifest_data = project.get("manifest") or {}
    self_deploy = str(manifest_data.get("deploy") or "").strip()
    if self_deploy:
        items.append({
            "project": {"id": project["id"], "slug": project["slug"],
                        "name": project["name"], "path": project["path"]},
            "deploy_command": self_deploy,
            "deploy_doc": "",
            "auto_deploy": False,
            "reason": "changed project",
        })

    return {
        "project": {"id": project["id"], "slug": project["slug"],
                    "name": project["name"], "path": project["path"]},
        "changed_files": impact.get("changed_files", []),
        "items": items,
        "auto_deploy_skipped": not include_auto,
    }


def _deploy_edge_for(dependent_slug: str, dependency_slug: str) -> dict[str, Any]:
    """Return the dependency edge from *dependent_slug* to *dependency_slug*."""
    dependent = get_project(dependent_slug)
    dependency = get_project(dependency_slug)
    if not dependent or not dependency:
        return {}
    with db.db() as conn:
        row = db.one(
            conn,
            """SELECT d.* FROM project_dependencies d
               WHERE d.project_id=? AND d.depends_on_project_id=?""",
            (int(dependent["id"]), int(dependency["id"])),
        )
        return _dependency_row_to_dict(row) if row else {}


def _deploy_item(node: dict[str, Any], edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "project": node,
        "deploy_command": edge.get("deploy_command", ""),
        "deploy_doc": edge.get("deploy_doc", ""),
        "auto_deploy": edge.get("auto_deploy", False),
        "reason": edge.get("reason", "") or ", ".join(node.get("reasons", [])) or "dependent",
    }


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
    sync_dependencies(slug, man["data"].get("dependencies"))
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


def get_activity(activity_id: int) -> dict[str, Any] | None:
    """One activity row with ``meta`` parsed, or ``None``."""
    with db.db() as conn:
        row = db.one(conn, "SELECT * FROM activity WHERE id=?", (int(activity_id),))
    if not row:
        return None
    entry = dict(row)
    try:
        entry["meta"] = json.loads(entry.get("meta") or "{}")
    except Exception:
        entry["meta"] = {}
    return entry


def misroute_reported(decision_id: int) -> bool:
    """True when this routing decision already has a misroute marker."""
    message = f"misroute reported for routing decision {int(decision_id)}"
    with db.db() as conn:
        row = db.one(
            conn,
            "SELECT 1 FROM activity WHERE kind='routing_misroute' AND message=?",
            (message,),
        )
    return bool(row)


def recent_interrupted_runs(minutes: int = 60) -> list[dict[str, Any]]:
    """Runs this process (or a previous one) marked ``interrupted`` recently."""
    from datetime import datetime, timedelta, timezone

    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=max(1, int(minutes)))
    ).isoformat()
    with db.db() as conn:
        rows = db.rows(
            conn,
            "SELECT r.*, p.slug AS project_slug FROM runs r "
            "LEFT JOIN projects p ON p.id = r.project_id "
            "WHERE r.status='interrupted' AND r.ended_at>=? ORDER BY r.id DESC",
            (cutoff,),
        )
    return [dict(row) for row in rows]


def chats_for_session(session_pk: int | str | None) -> list[str]:
    """Chat ids bound to a session (used to notify a run's owner)."""
    if session_pk in (None, ""):
        return []
    try:
        key = int(session_pk)
    except (TypeError, ValueError):
        return []
    with db.db() as conn:
        rows = db.rows(
            conn, "SELECT chat_id FROM bridge_links WHERE session_pk=?", (key,)
        )
    return [str(row["chat_id"]) for row in rows]


def interrupt_notice_sent(run_uid: str) -> bool:
    """True when this interrupted run already produced a chat notice."""
    with db.db() as conn:
        row = db.one(
            conn,
            "SELECT 1 FROM activity WHERE kind='run_interrupted_notice' AND message=?",
            (f"interrupted run notice for {run_uid}",),
        )
    return bool(row)


def prune_activity(
    keep_days: int = 90,
    *,
    kinds: tuple[str, ...] = ("routing_decision", "routing_misroute"),
) -> int:
    """Delete old routing telemetry rows (never run/proposal audit rows).

    Shadow mode writes one row per inbound message; without a sweep the table
    grows forever. Only the routing kinds are pruned — run history and the
    proposal/audit trail stay.
    """
    from datetime import datetime, timedelta, timezone

    if not kinds:
        return 0
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, int(keep_days)))
    ).isoformat()
    placeholders = ",".join("?" for _ in kinds)
    with db.db() as conn:
        cur = conn.execute(
            f"DELETE FROM activity WHERE kind IN ({placeholders}) AND ts < ?",
            (*kinds, cutoff),
        )
        return int(cur.rowcount or 0)


def routing_decisions(hours: int = 24, limit: int = 50) -> list[dict[str, Any]]:
    """Recent ``routing_decision`` rows, newest first (dashboard panel).

    ``ts`` is stored as an ISO-8601 string, so a lexicographic ``>=`` on the
    cutoff is exact for the same format.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))).isoformat()
    with db.db() as conn:
        rows = db.rows(
            conn,
            "SELECT * FROM activity WHERE kind='routing_decision' AND ts>=? "
            "ORDER BY id DESC LIMIT ?",
            (cutoff, max(1, min(int(limit), 500))),
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        entry = dict(row)
        try:
            entry["meta"] = json.loads(entry.get("meta") or "{}")
        except Exception:
            entry["meta"] = {}
        out.append(entry)
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
