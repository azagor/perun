"""Synchronise a Perun shared project's participant list with its PerunChat room.

The sync service is the single place that knows how to:

* derive the dedicated ``grp:`` room id from a project slug,
* create the managed room on the PerunChat gateway,
* reconcile the gateway's ACL with the manifest participant list,
* record the binding in the local ``project_chats`` table.

It is intentionally decoupled from the HTTP handlers so it can also be invoked
from the bridge reconnect path and from project lifecycle hooks.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import config, db, registry, shared_projects

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "perun"
DEFAULT_ROOM_KIND = "chat"


class SyncError(Exception):
    """A user-facing or recoverable sync failure."""


def _rest_base(endpoint: str) -> str:
    """Derive the REST base URL from a gateway WebSocket endpoint."""
    url = (endpoint or "").strip()
    if not url:
        return ""
    # Gateway WebSocket endpoints look like wss://host/v1/chat/ws
    rest = url.replace("/chat/ws", "").replace("/ws", "")
    if rest.startswith("wss://"):
        rest = "https://" + rest[6:]
    elif rest.startswith("ws://"):
        rest = "http://" + rest[5:]
    return rest.rstrip("/")


class SharedProjectSync:
    def __init__(self, settings: config.Settings | None = None) -> None:
        self.settings = settings or config.Settings.load()
        self._agent_id: str | None = None

    def _key_and_base(self) -> tuple[str, str]:
        key, endpoint = self.settings.perunchat()
        if not key:
            raise SyncError("PerunChat API key not configured")
        base = _rest_base(endpoint)
        if not base:
            raise SyncError("PerunChat endpoint not configured")
        return key, base

    def _client(self) -> httpx.Client:
        key, base = self._key_and_base()
        return httpx.Client(
            base_url=base,
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    def agent_id(self) -> str:
        """Return Perun's own agent id on the gateway, caching the result."""
        if self._agent_id:
            return self._agent_id
        with self._client() as client:
            resp = client.get("/api/v1/me")
            resp.raise_for_status()
            data = resp.json()
        agent_id = str(data.get("agent_id") or "").strip().lower()
        if not agent_id:
            raise SyncError("PerunChat gateway did not return an agent_id")
        self._agent_id = agent_id
        return agent_id

    def room_id(self, project_slug: str, tenant: str = DEFAULT_TENANT) -> str:
        return shared_projects.project_chat_room_id(tenant, project_slug)

    def ensure_room(
        self,
        room_id: str,
        desired_members: list[str],
    ) -> dict[str, Any]:
        """Create the managed group room or verify we own it."""
        managed_by = self.agent_id()
        members = sorted({managed_by, *(m.lower() for m in desired_members if m)})
        with self._client() as client:
            resp = client.post(
                "/api/v1/rooms/group",
                json={"room_id": room_id, "members": members, "managed_by": managed_by},
            )
            if resp.status_code == 409:
                # Room exists under another manager. We cannot take it over.
                raise SyncError(f"room {room_id} is managed by another agent")
            resp.raise_for_status()
            return resp.json()

    def list_members(self, room_id: str) -> list[str]:
        with self._client() as client:
            resp = client.get(f"/api/v1/rooms/{room_id}/members")
            resp.raise_for_status()
            data = resp.json()
        return [str(m).lower() for m in data.get("members", [])]

    def add_member(self, room_id: str, agent_id: str) -> None:
        with self._client() as client:
            resp = client.post(
                f"/api/v1/rooms/{room_id}/members",
                json={"agent_id": agent_id.lower()},
            )
            resp.raise_for_status()

    def remove_member(self, room_id: str, agent_id: str) -> None:
        with self._client() as client:
            resp = client.delete(
                f"/api/v1/rooms/{room_id}/members/{agent_id.lower()}"
            )
            resp.raise_for_status()

    def sync_members(
        self,
        room_id: str,
        desired_members: list[str],
    ) -> dict[str, Any]:
        """Reconcile gateway ACL to match *desired_members* (plus Perun itself)."""
        desired = {self.agent_id(), *(m.lower() for m in desired_members if m)}
        try:
            current = set(self.list_members(room_id))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # Room does not exist yet; ensure_room will create it.
                self.ensure_room(room_id, desired_members)
                current = set(self.list_members(room_id))
            else:
                raise

        to_add = desired - current
        to_remove = current - desired
        errors: list[str] = []
        for agent_id in to_add:
            try:
                self.add_member(room_id, agent_id)
            except Exception as exc:  # pragma: no cover - network best-effort
                errors.append(f"add {agent_id}: {exc}")
        for agent_id in to_remove:
            try:
                self.remove_member(room_id, agent_id)
            except Exception as exc:  # pragma: no cover - network best-effort
                errors.append(f"remove {agent_id}: {exc}")

        if errors:
            raise SyncError("; ".join(errors))
        return {"added": sorted(to_add), "removed": sorted(to_remove)}

    def delete_room(self, room_id: str) -> None:
        with self._client() as client:
            resp = client.delete(f"/api/v1/rooms/{room_id}")
            # 404 is fine: the room is already gone.
            if resp.status_code != 404:
                resp.raise_for_status()


def desired_members(cfg: dict[str, Any]) -> list[str]:
    """Return participant agent ids from a shared-project config."""
    return [p["agent_id"] for p in cfg.get("participants", []) if p.get("agent_id")]


def sync_project_chat(
    project: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    tenant: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Ensure the dedicated chat for *project* matches its participant list.

    Returns a status dict; on total failure the local ``project_chats`` row is
    still created/updated with state ``pending`` so a later reconcile can finish.
    """
    pid = int(project["id"])
    slug = project["slug"]
    cfg = cfg if cfg is not None else shared_projects.load(project["path"])

    if not cfg.get("shared") and not cfg.get("participants"):
        # Not a shared project; remove any stale binding.
        chat = registry.get_project_chat(pid)
        if chat:
            try:
                sync = SharedProjectSync()
                sync.delete_room(chat["room_id"])
            except Exception as exc:
                logger.warning("Could not delete stale project chat for %s: %s", slug, exc)
            registry.delete_project_chat(pid)
        return {"ok": True, "action": "none", "reason": "not_shared"}

    members = desired_members(cfg)
    room_id = shared_projects.project_chat_room_id(tenant, slug)
    manifest_rev = cfg.get("manifest_rev", 1)

    sync = SharedProjectSync()
    try:
        managed_by = sync.agent_id()
    except Exception as exc:
        logger.warning("Could not determine PerunChat agent id for %s: %s", slug, exc)
        managed_by = "perun"

    try:
        sync.ensure_room(room_id, members)
        result = sync.sync_members(room_id, members)
        registry.set_project_chat(
            pid,
            room_id=room_id,
            managed_by=managed_by,
            state="active",
            manifest_rev=manifest_rev,
        )
        registry.update_project_chat(pid, last_sync_at=registry.now_iso())
        registry.log_activity(
            pid,
            "chat_synced",
            f"Project chat {room_id} synchronized",
            {"room_id": room_id, "added": result.get("added", []), "removed": result.get("removed", [])},
        )
        return {"ok": True, "room_id": room_id, **result}
    except Exception as exc:
        logger.warning("Project chat sync failed for %s: %s", slug, exc)
        registry.set_project_chat(
            pid,
            room_id=room_id,
            managed_by=managed_by,
            state="pending",
            manifest_rev=manifest_rev,
        )
        registry.log_activity(
            pid,
            "chat_sync_failed",
            f"Project chat sync failed: {exc}",
            {"room_id": room_id, "error": str(exc)},
        )
        return {"ok": False, "room_id": room_id, "error": str(exc)}


def _queue_room_deletion(room_id: str, error: str) -> None:
    with registry.db.db() as conn:
        conn.execute(
            "INSERT INTO pending_room_deletions (room_id, attempted_at, error) VALUES (?,?,?)",
            (room_id, registry.now_iso(), error),
        )


def retry_pending_room_deletions() -> dict[str, Any]:
    """Retry gateway room deletions that failed while offline."""
    with registry.db.db() as conn:
        rows = db.rows(conn, "SELECT id, room_id, error FROM pending_room_deletions")
    sync = SharedProjectSync()
    deleted: list[str] = []
    failed: list[dict[str, Any]] = []
    for row in rows:
        try:
            sync.delete_room(row["room_id"])
            deleted.append(row["room_id"])
            with registry.db.db() as conn:
                conn.execute("DELETE FROM pending_room_deletions WHERE id=?", (row["id"],))
        except Exception as exc:
            failed.append({"room_id": row["room_id"], "error": str(exc)})
            with registry.db.db() as conn:
                conn.execute(
                    "UPDATE pending_room_deletions SET attempted_at=?, error=? WHERE id=?",
                    (registry.now_iso(), str(exc), row["id"]),
                )
    return {"ok": True, "deleted": deleted, "failed": failed}


def delete_project_chat(project: dict[str, Any]) -> dict[str, Any]:
    """Lifecycle-coupled teardown: delete the gateway room and local binding."""
    pid = int(project["id"])
    chat = registry.get_project_chat(pid)
    if not chat:
        return {"ok": True, "action": "none"}
    error = None
    try:
        SharedProjectSync().delete_room(chat["room_id"])
    except Exception as exc:
        error = str(exc)
        logger.warning("Could not delete project chat for %s: %s", project["slug"], exc)
        _queue_room_deletion(chat["room_id"], error)
    registry.delete_project_chat(pid)
    registry.log_activity(
        pid,
        "chat_deleted",
        f"Project chat {chat['room_id']} deleted" + (f" (gateway error: {error})" if error else ""),
        {"room_id": chat["room_id"], "gateway_error": error},
    )
    return {"ok": True, "room_id": chat["room_id"], "deleted": True, "gateway_error": error}


def reconcile_all_project_chats() -> dict[str, Any]:
    """Reconcile every shared project's chat (used on gateway reconnect)."""
    results: list[dict[str, Any]] = []
    for project in registry.list_projects():
        cfg = shared_projects.load(project["path"])
        if cfg.get("shared") or cfg.get("participants"):
            results.append(sync_project_chat(project, cfg))
    return {"ok": True, "results": results}
