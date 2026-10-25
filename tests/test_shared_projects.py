import json
import sys
from pathlib import Path

from perun import db, manifest, projects, registry, server, shared_projects
from perun.shared_project_sync import retry_pending_room_deletions


def _make_project(tmp_path, name="test-project"):
    (tmp_path / name).mkdir(parents=True)
    project = registry.add_project(name, str(tmp_path / name))
    return project


def _enable_shared(project, participants=None):
    data = manifest.from_project(project)
    data["shared"] = True
    data["participants"] = participants or [{"agent_id": "frambo", "roles": ["rw"]}]
    manifest.save(project["path"], data, body=f"# {project['name']}")


def test_describe_project_chat_disabled(tmp_path):
    from perun.shared_project_sync import describe_project_chat

    project = _make_project(tmp_path, "plain-desc")
    info = describe_project_chat(project)
    assert info["state"] == "disabled"
    assert info["room_id"] == "grp:perun:plain-desc:chat"
    assert info["label"] == f"{project['name']} project chat"
    assert info["error"] is None


def test_describe_project_chat_reports_gateway_error_without_binding(tmp_path, monkeypatch):
    from perun.shared_project_sync import describe_project_chat

    project = _make_project(tmp_path, "shared-desc")
    _enable_shared(project, [{"agent_id": "frambo", "roles": ["rw"]}])

    class FakeSync:
        def list_members(self, room_id):
            raise RuntimeError("gateway unreachable")

    monkeypatch.setattr("perun.shared_project_sync.SharedProjectSync", FakeSync)
    info = describe_project_chat(project, trusted=["frambo"])
    assert info["state"] == "unbound"
    assert info["expected_members"] == ["frambo"]
    assert info["members"] is None
    assert "gateway unreachable" in info["error"]


def test_describe_project_chat_filters_untrusted_expected_members(tmp_path, monkeypatch):
    from perun.shared_project_sync import describe_project_chat

    project = _make_project(tmp_path, "shared-filter")
    _enable_shared(
        project,
        [
            {"agent_id": "frambo", "roles": ["rw"]},
            {"agent_id": "perun-other-machine", "roles": ["rw"]},
        ],
    )

    class FakeSync:
        def list_members(self, room_id):
            raise RuntimeError("gateway unreachable")

    monkeypatch.setattr("perun.shared_project_sync.SharedProjectSync", FakeSync)
    info = describe_project_chat(project, trusted=["frambo"])
    assert info["expected_members"] == ["frambo"]
    assert "perun-other-machine" not in info["expected_members"]


def test_describe_project_chat_returns_live_members(tmp_path, monkeypatch):
    from perun.shared_project_sync import describe_project_chat

    project = _make_project(tmp_path, "shared-live")
    _enable_shared(project, [{"agent_id": "frambo", "roles": ["rw"]}])
    registry.set_project_chat(project["id"], "grp:perun:shared-live:chat", "perun", "active", 2)

    class FakeSync:
        def list_members(self, room_id):
            return ["frambo", "perun", "intruder"]

    monkeypatch.setattr("perun.shared_project_sync.SharedProjectSync", FakeSync)
    info = describe_project_chat(project)
    assert info["state"] == "active"
    assert info["members"] == ["frambo", "intruder", "perun"]
    assert info["managed_by"] == "perun"
    assert info["manifest_rev"] == 2


def test_shared_api_returns_project_chat_info(tmp_path, monkeypatch):
    """GET /shared must describe the chat even when the gateway is unreachable."""
    from fastapi.testclient import TestClient
    from perun import server
    from perun.shared_project_sync import SyncError

    class FakeSync:
        def list_members(self, room_id):
            raise SyncError("gateway down")

    monkeypatch.setattr("perun.shared_project_sync.SharedProjectSync", FakeSync)
    old = server.SETTINGS.auto_discover_on_start
    server.SETTINGS.auto_discover_on_start = False
    try:
        with TestClient(server.app, base_url="http://127.0.0.1:8760") as client:
            path = tmp_path / "api-shared"
            path.mkdir()
            created = client.post("/api/projects", json={"name": "API Shared", "path": str(path)})
            assert created.status_code == 200
            slug = created.json()["project"]["slug"]
            _enable_shared(registry.get_project(slug))

            body = client.get(f"/api/projects/{slug}/shared").json()
            assert body["ok"] is True
            assert body["shared"]["shared"] is True
            chat = body["project_chat"]
            assert chat["room_id"] == f"grp:perun:{slug}:chat"
            assert chat["state"] in ("unbound", "pending")
            assert chat["error"] and "gateway down" in chat["error"]
    finally:
        server.SETTINGS.auto_discover_on_start = old


def test_shared_sync_endpoint_forces_reconcile(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from perun import server

    calls = []

    def fake_sync(project, cfg):
        calls.append(project["slug"])
        registry.set_project_chat(project["id"], f"grp:perun:{project['slug']}:chat",
                                  "perun", "active", cfg.get("manifest_rev", 1))
        return {"ok": True, "room_id": f"grp:perun:{project['slug']}:chat"}

    class FakeSync:
        def list_members(self, room_id):
            return ["perun", "frambo"]

    monkeypatch.setattr("perun.shared_project_sync.sync_project_chat", fake_sync)
    monkeypatch.setattr("perun.shared_project_sync.SharedProjectSync", FakeSync)
    old = server.SETTINGS.auto_discover_on_start
    server.SETTINGS.auto_discover_on_start = False
    try:
        with TestClient(server.app, base_url="http://127.0.0.1:8760") as client:
            path = tmp_path / "api-sync"
            path.mkdir()
            created = client.post("/api/projects", json={"name": "API Sync", "path": str(path)})
            slug = created.json()["project"]["slug"]
            _enable_shared(registry.get_project(slug))

            body = client.post(f"/api/projects/{slug}/shared/sync").json()
            assert body["ok"] is True
            assert body["result"]["ok"] is True
            assert body["project_chat"]["state"] == "active"
            assert body["project_chat"]["members"] == ["frambo", "perun"]
            assert calls == [slug]
    finally:
        server.SETTINGS.auto_discover_on_start = old


def test_trust_revocation_logs_and_re_syncs(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)

    # Pre-populate trust list with the participant.
    server.SETTINGS.perunchat_trusted = ["frambo"]

    sync_calls = []

    def fake_sync(project_arg, cfg_arg):
        sync_calls.append((
            project_arg["slug"],
            cfg_arg.get("shared"),
            [p["agent_id"] for p in cfg_arg.get("participants", [])],
        ))
        return {"ok": True}

    monkeypatch.setattr("perun.shared_project_sync.sync_project_chat", fake_sync)

    # Remove frambo from trust list.
    server._save_trusted([])

    assert server.SETTINGS.perunchat_trusted == []
    assert sync_calls == [(project["slug"], True, [])]
    updated = shared_projects.load(project["path"])
    assert updated["participants"] == []
    assert updated["manifest_rev"] >= 2

    activity = registry.list_activity(project["id"])
    kinds = {a["kind"] for a in activity}
    assert "trust_revoked" in kinds

    # Restore trust list for other tests.
    server.SETTINGS.perunchat_trusted = []


def test_sync_filters_untrusted_participants_from_room_acl(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from perun import shared_project_sync

    project = _make_project(tmp_path, "shared-acl-filter")
    _enable_shared(
        project,
        [
            {"agent_id": "frambo", "roles": ["rw"]},
            {"agent_id": "perun-other-machine", "roles": ["rw"]},
        ],
    )
    calls = []

    class FakeSync:
        settings = SimpleNamespace(perunchat_trusted=["frambo"])

        def agent_id(self):
            return "perun-this-machine"

        def ensure_room(self, room_id, members):
            calls.append(("ensure", room_id, list(members)))

        def sync_members(self, room_id, members):
            calls.append(("sync", room_id, list(members)))
            return {"added": [], "removed": [], "members": list(members)}

    monkeypatch.setattr(shared_project_sync, "SharedProjectSync", FakeSync)
    shared_project_sync.sync_project_chat(project)

    assert calls[0][2] == ["frambo"]
    assert calls[1][2] == ["frambo"]


def test_sync_opencode_config_adds_guard_mcp_for_shared_project(tmp_path):
    project = _make_project(tmp_path, "shared-guard")
    _enable_shared(project)
    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    assert "perun-guard" in cfg.get("mcp", {})
    assert cfg["mcp"]["perun-guard"] == {
        "command": sys.executable, "args": ["-m", "perun.guard_mcp"]
    }


def test_sync_opencode_config_removes_guard_mcp_for_non_shared_project(tmp_path):
    project = _make_project(tmp_path, "plain-guard")
    (Path(project["path"]) / "opencode.json").write_text(
        json.dumps({"mcp": {"perun-guard": {"command": "x"}}}), encoding="utf-8"
    )
    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    assert "perun-guard" not in cfg.get("mcp", {})


def test_retry_pending_room_deletions(tmp_path, monkeypatch):
    deleted: list[str] = []

    class FakeSync:
        def delete_room(self, room_id: str) -> None:
            if room_id == "room-fail":
                raise RuntimeError("gateway down")
            deleted.append(room_id)

    monkeypatch.setattr("perun.shared_project_sync.SharedProjectSync", FakeSync)

    with db.db() as conn:
        conn.execute(
            "INSERT INTO pending_room_deletions (room_id, attempted_at, error) VALUES (?,?,?)",
            ("room-ok", "2024-01-01T00:00:00", "offline"),
        )
        conn.execute(
            "INSERT INTO pending_room_deletions (room_id, attempted_at, error) VALUES (?,?,?)",
            ("room-fail", "2024-01-01T00:00:00", "offline"),
        )

    result = retry_pending_room_deletions()
    assert result["ok"] is True
    assert "room-ok" in result["deleted"]
    assert "room-fail" in {f["room_id"] for f in result["failed"]}
    assert deleted == ["room-ok"]

    with db.db() as conn:
        rows = conn.execute("SELECT room_id FROM pending_room_deletions").fetchall()
        assert [r["room_id"] for r in rows] == ["room-fail"]
