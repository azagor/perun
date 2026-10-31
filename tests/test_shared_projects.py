import json
import sys
from pathlib import Path

from perun import db, manifest, projects, registry, server, shared_projects
from perun.shared_project_sync import retry_pending_room_deletions


def _make_project(tmp_path, name="test-project"):
    (tmp_path / name).mkdir(parents=True)
    project = registry.add_project(name, str(tmp_path / name))
    return project


def _servers(cfg):
    """The server map of a config, whichever ``mcp`` shape it uses."""
    mcp = cfg.get("mcp") or {}
    servers = mcp.get("servers")
    if isinstance(servers, dict):
        return servers
    return {k: v for k, v in mcp.items() if k not in ("servers", "timeout")}


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
        "environment": {"PERUN_HOME": str(_config.HOME),
                        "PERUN_PROJECT_PATH": "."},
    }


def _expected_proposals_entry():
    from perun import config as _config

    return {
        "type": "local",
        "command": [sys.executable, "-m", "perun.proposals_mcp"],
        "enabled": True,
        "environment": {"PERUN_HOME": str(_config.HOME),
                        "PERUN_PROJECT_PATH": "."},
    }


def test_sync_opencode_config_adds_guard_mcp_for_shared_project(tmp_path, monkeypatch):
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v1")
    project = _make_project(tmp_path, "shared-guard")
    _enable_shared(project)
    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    assert "perun-guard" in _servers(cfg)
    assert _servers(cfg)["perun-guard"] == _expected_guard_entry()
    assert _servers(cfg)["perun-proposals"] == _expected_proposals_entry()


def test_sync_opencode_config_uses_portable_default_home(tmp_path, monkeypatch):
    """The default PERUN_HOME must not embed the user's absolute home path."""
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v1")
    project = _make_project(tmp_path, "portable-home")
    _enable_shared(project)
    monkeypatch.setattr(projects.config, "HOME", (Path.home() / ".perun").resolve())
    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    assert _servers(cfg)["perun-guard"]["environment"]["PERUN_HOME"] == "~/.perun"
    assert _servers(cfg)["perun-proposals"]["environment"]["PERUN_HOME"] == "~/.perun"


def test_sync_manifest_adds_guard_mcp_for_shared_project(tmp_path, monkeypatch):
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v1")
    project = _make_project(tmp_path, "shared-manifest-sync")
    _enable_shared(project)
    # sync_manifest re-reads the manifest and should mirror shared state into
    # the project's opencode.json even when sync_chat is True.
    projects.sync_manifest(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    assert "perun-guard" in _servers(cfg)
    assert "perun-proposals" in _servers(cfg)


def test_shared_mcp_config_status_flags_stale_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v1")
    project = _make_project(tmp_path, "stale-mcp")
    _enable_shared(project)
    projects.sync_opencode_config(project)
    status = projects.shared_mcp_config_status(registry.get_project(project["slug"]))
    assert status["stale"] is False

    cfg_path = Path(project["path"]) / "opencode.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    del _servers(cfg)["perun-proposals"]["environment"]["PERUN_PROJECT_PATH"]
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    status = projects.shared_mcp_config_status(registry.get_project(project["slug"]))
    assert status["stale"] is True
    assert any("perun-proposals.PERUN_PROJECT_PATH" in m for m in status["missing"])


def test_shared_mcp_config_status_missing_file(tmp_path):
    project = _make_project(tmp_path, "missing-mcp")
    _enable_shared(project)
    status = projects.shared_mcp_config_status(registry.get_project(project["slug"]))
    assert status["stale"] is True
    assert "opencode.json" in status["warning"]


def test_shared_mcp_config_status_non_shared_is_clean(tmp_path):
    project = _make_project(tmp_path, "plain-mcp")
    status = projects.shared_mcp_config_status(registry.get_project(project["slug"]))
    assert status["stale"] is False


def test_sync_opencode_config_removes_guard_mcp_for_non_shared_project(tmp_path, monkeypatch):
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v1")
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
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    mcp = _servers(cfg)
    # The rewritten entries carry the field the detected CLI shape uses.
    from perun import mcp_servers
    style = mcp_servers.mcp_style()
    assert mcp["perun-guard"] == mcp_servers.entry_for_style(_expected_guard_entry(), style)
    assert mcp["perun-proposals"] == mcp_servers.entry_for_style(_expected_proposals_entry(), style)
    assert mcp["user-tool"]["command"] == ["x"]


def test_sync_unshare_removes_entries_keeps_user_mcp(tmp_path):
    project = _make_project(tmp_path, "unshare")
    _enable_shared(project)
    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    servers = _servers(cfg)
    servers["user-tool"] = {"type": "local", "command": ["x"], "enabled": True}
    if isinstance((cfg.get("mcp") or {}).get("servers"), dict):
        cfg["mcp"]["servers"] = servers
    else:
        cfg["mcp"] = servers
    (Path(project["path"]) / "opencode.json").write_text(json.dumps(cfg), encoding="utf-8")
    data = manifest.from_project(project)
    data["shared"] = False
    manifest.save(project["path"], data, body="# unshare")
    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    assert set(_servers(cfg)) == {"user-tool"}


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
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))
    mcp = _servers(cfg)
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

# --- OpenCode V2 ``mcp.servers`` shape ------------------------------------- #

def test_sync_opencode_config_writes_the_v2_shape_on_a_v2_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v2")
    project = _make_project(tmp_path, "v2-mcp")
    _enable_shared(project)

    projects.sync_opencode_config(project)
    cfg = json.loads((Path(project["path"]) / "opencode.json").read_text(encoding="utf-8"))

    assert "servers" in cfg["mcp"]
    assert "perun-guard" not in cfg["mcp"]          # no flat V1 leftovers
    entry = cfg["mcp"]["servers"]["perun-guard"]
    assert entry["type"] == "local"
    assert entry["environment"]["PERUN_PROJECT_PATH"] == "."
    assert "enabled" not in entry                  # V2 uses ``disabled``
    assert "disabled" not in entry


def test_sync_opencode_config_migrates_a_v1_file_once_and_keeps_others(tmp_path, monkeypatch):
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v2")
    project = _make_project(tmp_path, "v1-to-v2")
    _enable_shared(project)
    cfg_path = Path(project["path"]) / "opencode.json"
    cfg_path.write_text(json.dumps({
        "instructions": ["AGENTS.md"],
        "mcp": {
            "context7": {"type": "remote", "url": "https://mcp.example/mcp"},
            "perun-guard": {"type": "local", "command": ["python", "-m", "old"],
                            "enabled": True, "environment": {}},
        },
    }), encoding="utf-8")

    projects.sync_opencode_config(project)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    assert cfg["instructions"] == ["AGENTS.md"]
    assert "context7" not in cfg["mcp"]                       # moved, not lost
    assert cfg["mcp"]["servers"]["context7"]["url"] == "https://mcp.example/mcp"
    assert cfg["mcp"]["servers"]["perun-guard"]["environment"]["PERUN_PROJECT_PATH"] == "."
    assert cfg_path.with_name("opencode.json.bak").is_file()

    # Second sync is a no-op: the file is already in the target shape.
    assert projects.sync_opencode_config(project)["action"] == "unchanged"


def test_mcp_style_detection_is_conservative(monkeypatch):
    from perun import mcp_servers

    monkeypatch.delenv("PERUN_OPENCODE_MCP_STYLE", raising=False)
    monkeypatch.setattr(mcp_servers, "opencode_version", lambda timeout=5.0: "2.2.0")
    mcp_servers.reset_style_cache()
    assert mcp_servers.mcp_style() == mcp_servers.STYLE_V2

    monkeypatch.setattr(mcp_servers, "opencode_version", lambda timeout=5.0: "1.9.4")
    mcp_servers.reset_style_cache()
    assert mcp_servers.mcp_style() == mcp_servers.STYLE_V1

    # Unparseable or missing CLI: never write a shape an old CLI cannot read.
    monkeypatch.setattr(mcp_servers, "opencode_version", lambda timeout=5.0: "")
    mcp_servers.reset_style_cache()
    assert mcp_servers.mcp_style() == mcp_servers.STYLE_V1
    mcp_servers.reset_style_cache()


def test_entry_for_style_translates_enabled_and_disabled():
    from perun import mcp_servers

    entry = {"type": "local", "command": ["x"], "enabled": True}
    v2 = mcp_servers.entry_for_style(entry, mcp_servers.STYLE_V2)
    assert "enabled" not in v2 and "disabled" not in v2
    off = mcp_servers.entry_for_style({"type": "local", "enabled": False},
                                      mcp_servers.STYLE_V2)
    assert off["disabled"] is True
    back = mcp_servers.entry_for_style(off, mcp_servers.STYLE_V1)
    assert back["enabled"] is True

def test_the_backup_keeps_the_first_pre_migration_copy(tmp_path, monkeypatch):
    """A second MCP change must not overwrite the only pre-migration snapshot."""
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v2")
    project = _make_project(tmp_path, "backup-once")
    _enable_shared(project)
    cfg_path = Path(project["path"]) / "opencode.json"
    cfg_path.write_text(json.dumps({
        "mcp": {"perun-guard": {"type": "local", "command": ["python", "old.py"],
                                "enabled": True}},
    }), encoding="utf-8")

    projects.sync_opencode_config(project)
    backup = cfg_path.with_name("opencode.json.bak")
    assert backup.is_file()
    first = backup.read_text(encoding="utf-8")

    # Change the block again: the snapshot must stay at the pre-migration state.
    projects.sync_opencode_config(project)
    assert backup.read_text(encoding="utf-8") == first
    assert json.loads(first)["mcp"]["perun-guard"]["command"] == ["python", "old.py"]


def test_v2_keeps_mcp_timeout_when_rewriting(tmp_path, monkeypatch):
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v2")
    project = _make_project(tmp_path, "keep-timeout")
    _enable_shared(project)
    cfg_path = Path(project["path"]) / "opencode.json"
    cfg_path.write_text(json.dumps({
        "mcp": {"timeout": {"startup": 45000},
                "servers": {"other": {"type": "remote", "url": "https://x/mcp"}}},
    }), encoding="utf-8")

    projects.sync_opencode_config(project)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["mcp"]["timeout"] == {"startup": 45000}
    assert "other" in cfg["mcp"]["servers"]
    assert "perun-guard" in cfg["mcp"]["servers"]


def test_the_style_probe_runs_once_for_concurrent_callers(monkeypatch):
    from perun import mcp_servers

    monkeypatch.delenv("PERUN_OPENCODE_MCP_STYLE", raising=False)
    calls = {"n": 0}

    def slow_version(timeout=10.0):
        calls["n"] += 1
        return "2.2.0"

    monkeypatch.setattr(mcp_servers, "opencode_version", slow_version)
    mcp_servers.reset_style_cache()
    threads = [__import__("threading").Thread(target=mcp_servers.mcp_style)
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert mcp_servers.mcp_style() == mcp_servers.STYLE_V2
    assert calls["n"] == 1
    mcp_servers.reset_style_cache()
