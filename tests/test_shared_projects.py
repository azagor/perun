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


def _expected_guard_entry():
    from perun import config as _config

    return {
        "type": "local",
        "command": [sys.executable, "-m", "perun.guard_mcp"],
        "enabled": True,
        "environment": {"PERUN_HOME": str(_config.HOME)},
    }


def _expected_proposals_entry():
    from perun import config as _config

    return {
        "type": "local",
        "command": [sys.executable, "-m", "perun.proposals_mcp"],
        "enabled": True,
        "environment": {"PERUN_HOME": str(_config.HOME)},
    }


def test_sync_opencode_config_adds_guard_mcp_for_shared_project(tmp_path):
    project = _make_project(tmp_path, "shared-guard")
    _enable_shared(project)
    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    assert "perun-guard" in cfg.get("mcp", {})
    assert cfg["mcp"]["perun-guard"] == _expected_guard_entry()
    assert cfg["mcp"]["perun-proposals"] == _expected_proposals_entry()


def test_sync_opencode_config_uses_portable_default_home(tmp_path, monkeypatch):
    """The default PERUN_HOME must not embed the user's absolute home path."""
    project = _make_project(tmp_path, "portable-home")
    _enable_shared(project)
    monkeypatch.setattr(projects.config, "HOME", (Path.home() / ".perun").resolve())
    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    assert cfg["mcp"]["perun-guard"]["environment"]["PERUN_HOME"] == "~/.perun"
    assert cfg["mcp"]["perun-proposals"]["environment"]["PERUN_HOME"] == "~/.perun"


def test_sync_manifest_adds_guard_mcp_for_shared_project(tmp_path):
    project = _make_project(tmp_path, "shared-manifest-sync")
    _enable_shared(project)
    # sync_manifest re-reads the manifest and should mirror shared state into
    # the project's opencode.json even when sync_chat is True.
    projects.sync_manifest(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    assert "perun-guard" in cfg.get("mcp", {})
    assert "perun-proposals" in cfg.get("mcp", {})


def test_sync_opencode_config_removes_guard_mcp_for_non_shared_project(tmp_path):
    project = _make_project(tmp_path, "plain-guard")
    (Path(project["path"]) / "opencode.json").write_text(
        json.dumps({"mcp": {"perun-guard": {"command": "x"}}}), encoding="utf-8"
    )
    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    assert "perun-guard" not in cfg.get("mcp", {})


def test_sync_rewrites_old_shape_mcp_entries(tmp_path):
    """Old Desktop-style {command, args} entries become local command arrays."""
    project = _make_project(tmp_path, "old-shape")
    _enable_shared(project)
    (Path(project["path"]) / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "perun-guard": {"command": sys.executable, "args": ["-m", "perun.guard_mcp"]},
                    "perun-proposals": {"command": sys.executable, "args": ["-m", "perun.proposals_mcp"]},
                    "user-tool": {"type": "local", "command": ["x"], "enabled": True},
                }
            }
        ),
        encoding="utf-8",
    )
    projects.sync_opencode_config(project)
    mcp = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))["mcp"]
    assert mcp["perun-guard"] == _expected_guard_entry()
    assert mcp["perun-proposals"] == _expected_proposals_entry()
    assert mcp["user-tool"]["command"] == ["x"]


def test_sync_unshare_removes_entries_keeps_user_mcp(tmp_path):
    project = _make_project(tmp_path, "unshare")
    _enable_shared(project)
    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    cfg["mcp"]["user-tool"] = {"type": "local", "command": ["x"], "enabled": True}
    (Path(project["path"]) / "opencode.json").write_text(json.dumps(cfg), encoding="utf-8")
    data = manifest.from_project(project)
    data["shared"] = False
    manifest.save(project["path"], data, body="# unshare")
    projects.sync_opencode_config(project)
    mcp = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8")).get("mcp", {})
    assert set(mcp) == {"user-tool"}


def test_sync_is_idempotent_for_shared_project(tmp_path):
    project = _make_project(tmp_path, "idem")
    _enable_shared(project)
    assert projects.sync_opencode_config(project)["action"] != "unchanged"
    first = (Path(project["path"]) / "opencode.json").read_text(encoding="utf-8")
    assert projects.sync_opencode_config(project)["action"] == "unchanged"
    assert (Path(project["path"]) / "opencode.json").read_text(encoding="utf-8") == first


def test_sync_non_shared_never_writes_empty_mcp(tmp_path):
    """A non-shared project whose mcp block only contained Perun entries ends up
    with no mcp key at all after those entries are removed."""
    project = _make_project(tmp_path, "no-stray")
    (Path(project["path"]) / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "perun-guard": {"command": "x"},
                    "perun-proposals": {"command": "x"},
                }
            }
        ),
        encoding="utf-8",
    )
    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    assert "mcp" not in cfg


def test_sync_non_shared_with_user_mcp_keeps_user_mcp(tmp_path):
    """A non-shared project keeps user MCP entries while only Perun entries are removed."""
    project = _make_project(tmp_path, "no-stray-user-mcp")
    (Path(project["path"]) / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "perun-guard": {"command": "x"},
                    "user-tool": {"type": "local", "command": ["x"], "enabled": True},
                }
            }
        ),
        encoding="utf-8",
    )
    projects.sync_opencode_config(project)
    mcp = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8")).get("mcp", {})
    assert "perun-guard" not in mcp
    assert "user-tool" in mcp


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


def test_normalize_persists_auto_approve(tmp_path):
    project = _make_project(tmp_path)
    _enable_shared(project)
    data, _ = shared_projects.normalize(manifest.from_project(project))
    assert "auto_approve" not in data

    data["auto_approve"] = True
    normalized, _ = shared_projects.normalize(data)
    assert normalized["auto_approve"] is True

    manifest.save(project["path"], normalized, body="# test")
    loaded = shared_projects.load(project["path"])
    assert loaded["auto_approve"] is True


def test_is_auto_approve_uses_per_project_flag(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)
    assert shared_projects.is_auto_approve(project["path"]) is False

    data = manifest.from_project(project)
    data["auto_approve"] = True
    manifest.save(project["path"], data, body="# test")
    assert shared_projects.is_auto_approve(project["path"]) is True

    # Per-project explicit false overrides global true.
    data = manifest.from_project(project)
    data["auto_approve"] = False
    manifest.save(project["path"], data, body="# test")
    assert shared_projects.is_auto_approve(project["path"]) is False


def test_is_policy_auto_approve_uses_per_project_flag(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)
    assert shared_projects.is_policy_auto_approve(project["path"]) is False

    data = manifest.from_project(project)
    data["policy_auto_approve"] = True
    manifest.save(project["path"], data, body="# test")
    assert shared_projects.is_policy_auto_approve(project["path"]) is True


def test_is_policy_auto_approve_falls_back_to_global_default(tmp_path, monkeypatch):
    from perun import config

    project = _make_project(tmp_path)
    _enable_shared(project)
    assert shared_projects.is_policy_auto_approve(project["path"]) is False

    monkeypatch.setattr(config.Settings, "load", lambda: config.Settings(shared_auto_approve=True))
    assert shared_projects.is_policy_auto_approve(project["path"]) is True

    # Per-project explicit false overrides global true.
    data = manifest.from_project(project)
    data["policy_auto_approve"] = False
    manifest.save(project["path"], data, body="# test")
    assert shared_projects.is_policy_auto_approve(project["path"]) is False
