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


def test_trust_revocation_logs_and_re_syncs(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _enable_shared(project)

    # Pre-populate trust list with the participant.
    server.SETTINGS.perunchat_trusted = ["frambo"]

    sync_calls = []

    def fake_sync(project_arg, cfg_arg):
        sync_calls.append((project_arg["slug"], cfg_arg.get("shared")))
        return {"ok": True}

    monkeypatch.setattr("perun.shared_project_sync.sync_project_chat", fake_sync)

    # Remove frambo from trust list.
    server._save_trusted([])

    assert server.SETTINGS.perunchat_trusted == []
    assert sync_calls == [(project["slug"], True)]

    activity = registry.list_activity(project["id"])
    kinds = {a["kind"] for a in activity}
    assert "trust_revoked" in kinds

    # Restore trust list for other tests.
    server.SETTINGS.perunchat_trusted = []


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
