"""HTTP API smoke tests via FastAPI's TestClient."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from perun import server


@pytest.fixture()
def client():
    from perun import server

    server.SETTINGS.auto_discover_on_start = False  # keep tests deterministic
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "uptime" in body and "home" in body


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "PERUN" in r.text


def test_projects_crud(client):
    r = client.post("/api/projects", json={"name": "Demo", "path": r"C:\demo"})
    assert r.status_code == 200
    created = r.json()
    assert created["created"] is True
    slug = created["project"]["slug"]

    r = client.get("/api/projects")
    assert any(p["slug"] == slug for p in r.json()["projects"])

    r = client.patch(f"/api/projects/{slug}", json={"description": "hello"})
    assert r.json()["project"]["description"] == "hello"

    r = client.get(f"/api/projects/{slug}")
    assert r.status_code == 200
    assert r.json()["project"]["slug"] == slug

    r = client.delete(f"/api/projects/{slug}")
    assert r.json()["ok"] is True
    assert client.get(f"/api/projects/{slug}").status_code == 404


def test_project_requires_path(client):
    r = client.post("/api/projects", json={"name": "NoPath"})
    assert r.status_code == 400


def test_memory_remember_and_search(client):
    r = client.post("/api/memory/remember",
                    json={"content": "the widget pipeline is deterministic"})
    assert r.json()["ok"] is True
    # duplicate is accepted but flagged
    r2 = client.post("/api/memory/remember",
                     json={"content": "the widget pipeline is deterministic"})
    assert r2.json()["duplicate"] is True

    # Exact-substring query keeps this test valid even without Ollama.
    r = client.post("/api/memory/search",
                    json={"query": "widget pipeline", "k": 5})
    assert r.status_code == 200
    results = r.json()["results"]
    assert any("widget" in x["content"] for x in results)


def test_memory_sensitive_rejected(client):
    r = client.post("/api/memory/remember",
                    json={"content": "api_key = 'abcdef0123456789abcdef'"})
    assert r.status_code == 400


def test_memory_remember_unknown_project_is_404(client):
    """A typo'd slug must not silently write a *global* fact."""
    r = client.post("/api/memory/remember",
                    json={"content": "should not be stored", "project": "no-such-project"})
    assert r.status_code == 404


def test_memory_prune_rejects_non_positive_keep(client):
    assert client.post("/api/memory/prune", json={"keep": 0}).status_code == 400
    assert client.post("/api/memory/prune", json={"keep": -5}).status_code == 400
    assert client.post("/api/memory/prune", json={"keep": "abc"}).status_code == 400


def test_memory_prune_keeps_positive_count(client):
    client.post("/api/memory/remember", json={"content": "prunable fact"})
    r = client.post("/api/memory/prune", json={"keep": 50, "min_age_days": 0})
    assert r.status_code == 200
    assert "removed" in r.json()


def test_memory_stats(client):
    r = client.get("/api/memory/stats")
    assert "facts_total" in r.json()["stats"]


def test_memory_reembed_endpoint(client):
    r = client.post("/api/memory/reembed", json={"limit": 8})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "facts_embedded" in body
    assert "chunks_embedded" in body


def test_memory_reembed_rejects_invalid_limit(client):
    assert client.post("/api/memory/reembed", json={"limit": 0}).status_code == 400
    assert client.post("/api/memory/reembed", json={"limit": -1}).status_code == 400
    assert client.post("/api/memory/reembed", json={"limit": "abc"}).status_code == 400


def test_reembed_thread_is_idle_under_pytest(monkeypatch):
    """The background re-embed thread must not start during tests."""
    from perun import server

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_reembed_thread_is_idle_under_pytest")
    stop = server._start_reembed_thread()
    assert stop is None


def test_memory_recent_and_stats_filtered_by_project(client, tmp_path):
    """Recent facts and stats endpoints accept a project slug query param."""
    target = tmp_path / "memproj"
    r = client.post("/api/projects", json={
        "method": "new", "name": "MemProj", "path": str(target), "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    # Global fact
    client.post("/api/memory/remember", json={"content": "global fact"})
    # Project fact
    client.post("/api/memory/remember", json={
        "content": "project fact", "scope": "project", "project": slug,
    })

    recent = client.get(f"/api/memory/recent?project={slug}&limit=40").json()
    assert any("project fact" in f["content"] for f in recent["facts"])
    assert not any("global fact" in f["content"] for f in recent["facts"])

    stats = client.get(f"/api/memory/stats?project={slug}").json()
    assert stats["stats"]["facts_project"] >= 1


def test_settings_get_patch(client):
    r = client.get("/api/settings")
    assert r.json()["ok"] is True
    r = client.patch("/api/settings", json={"max_concurrent_runs": 5})
    assert r.json()["settings"]["max_concurrent_runs"] == 5
    assert client.get("/api/settings").json()["settings"]["max_concurrent_runs"] == 5


def test_hitl_secret_is_encrypted_when_password_available(client, tmp_path, monkeypatch):
    from perun import config, server

    config.CONFIG_PATH = tmp_path / "config.json"
    server.SETTINGS.perun_hitl_secret = ""
    r = client.patch("/api/settings", json={
        "perun_hitl_secret": "my-shared-secret",
        "perunchat_password": "test-password",
    })
    assert r.status_code == 200, r.text
    saved = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["perun_hitl_secret"].startswith("enc:")


def test_proposal_approval_uses_configured_owner_name(client, tmp_path, monkeypatch):
    from perun import server

    monkeypatch.setattr(server.SETTINGS, "perun_owner_name", "Ada Lovelace")
    monkeypatch.setattr(server.SETTINGS, "perunchat_trusted", ["ada-bot"])
    target = tmp_path / "shared-ada"
    r = client.post("/api/projects", json={
        "method": "new", "name": "SharedAda", "path": str(target), "bootstrap": False,
        "shared": True, "participants": [{"agent_id": "ada-bot", "roles": ["rw"]}],
    })
    assert r.status_code == 200, r.text
    slug = r.json()["project"]["slug"]

    r = client.post(f"/api/projects/{slug}/proposals", json={
        "requester": "ada-bot",
        "envelope": {"intent": "add logging", "targets": [str(target / "main.py")], "action_type": "edit"},
    })
    assert r.status_code == 200, r.text
    prop_id = r.json()["proposal"]["id"]

    r = client.post(f"/api/projects/{slug}/proposals/{prop_id}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["proposal"]["approved_by"] == "ada lovelace"


def test_proposal_api_rejects_read_only_requester(client, tmp_path, monkeypatch):
    from perun import server

    monkeypatch.setattr(server.SETTINGS, "perunchat_trusted", ["ro-bot"])
    target = tmp_path / "shared-ro"
    r = client.post("/api/projects", json={
        "method": "new", "name": "SharedRO", "path": str(target), "bootstrap": False,
        "shared": True, "participants": [{"agent_id": "ro-bot", "roles": ["ro"]}],
    })
    assert r.status_code == 200, r.text
    slug = r.json()["project"]["slug"]

    r = client.post(f"/api/projects/{slug}/proposals", json={
        "requester": "ro-bot",
        "envelope": {"intent": "add logging", "targets": [str(target / "main.py")], "action_type": "edit"},
    })
    assert r.status_code == 400
    assert "read-only" in r.json()["detail"]


def test_shared_project_registers_guard_mcp(client, tmp_path, monkeypatch):
    """Enabling/disabling shared mode mirrors the perun-guard MCP entry in opencode.json."""
    from perun import server

    monkeypatch.setattr(server.SETTINGS, "perunchat_trusted", ["ada-bot"])
    target = tmp_path / "shared-guard"
    r = client.post("/api/projects", json={
        "method": "new", "name": "SharedGuard", "path": str(target), "bootstrap": False,
        "shared": True, "participants": [{"agent_id": "ada-bot", "roles": ["rw"]}],
    })
    assert r.status_code == 200, r.text
    slug = r.json()["project"]["slug"]

    cfg_path = target / "opencode.json"
    assert cfg_path.exists()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "perun-guard" in cfg.get("mcp", {})
    assert cfg["mcp"]["perun-guard"]["args"] == ["-m", "perun.guard_mcp"]

    # Disabling shared mode removes the guard entry.
    r = client.put(f"/api/projects/{slug}/shared", json={
        "shared": False, "participants": [], "external_deps": []
    })
    assert r.status_code == 200, r.text
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "perun-guard" not in cfg.get("mcp", {})


def test_global_instruction_file_apis(client, tmp_path, monkeypatch):
    from perun import config, orchestrator

    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", tmp_path)
    r = client.post("/api/instructions", json={
        "filename": "guidance.md",
        "name": "Code search guidance",
        "scope": "all_projects",
        "body": "Use ast-grep first.",
    })
    assert r.status_code == 200
    assert r.json()["filename"] == "guidance.md"
    files = client.get("/api/instructions").json()["instructions"]
    assert any(f["filename"] == "guidance.md" and f["enabled"] for f in files)
    detail = client.get("/api/instructions/guidance.md").json()
    assert detail["body"].strip() == "Use ast-grep first."
    r = client.patch("/api/instructions/guidance.md/toggle", json={"enabled": False})
    assert r.json()["enabled"] is False
    client.delete("/api/instructions/guidance.md")
    files_after = client.get("/api/instructions").json()["instructions"]
    assert not any(f["filename"] == "guidance.md" for f in files_after)


def test_global_instruction_get_endpoints_do_not_seed_files(client, tmp_path, monkeypatch):
    """GET /api/instructions must not create files in an empty instructions dir."""
    from perun import config

    fresh = tmp_path / "empty_global"
    fresh.mkdir()
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", fresh)
    r = client.get("/api/instructions")
    assert r.status_code == 200
    assert r.json()["instructions"] == []
    assert not any(f.suffix.lower() == ".md" for f in fresh.iterdir())


def test_project_instruction_get_endpoints_do_not_seed_files(client, tmp_path):
    """GET /api/projects/{slug}/instructions must not create files."""
    from perun import orchestrator

    target = tmp_path / "no_seed"
    r = client.post("/api/projects", json={
        "method": "new", "name": "NoSeed", "path": str(target), "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]
    r = client.get(f"/api/projects/{slug}/instructions")
    assert r.status_code == 200
    assert r.json()["instructions"] == ""
    instr_dir = target / orchestrator.PROJECT_INSTRUCTIONS_DIR
    assert not instr_dir.exists() or not any(f.suffix.lower() == ".md" for f in instr_dir.iterdir())


def test_project_instruction_files_listing_does_not_create_dir(client, tmp_path):
    """GET .../instruction-files is read-only and must not mkdir .perun/instructions."""
    from perun import orchestrator

    target = tmp_path / "listing"
    target.mkdir()
    r = client.post("/api/projects", json={
        "method": "existing", "name": "Listing", "path": str(target),
    })
    assert r.status_code == 200, r.text
    slug = r.json()["project"]["slug"]
    instr_dir = target / orchestrator.PROJECT_INSTRUCTIONS_DIR
    assert not instr_dir.exists()
    r = client.get(f"/api/projects/{slug}/instruction-files")
    assert r.status_code == 200
    assert r.json()["files"] == []
    assert not instr_dir.exists()


def test_instructions_dir_endpoint_is_read_only(client, tmp_path, monkeypatch):
    """GET /api/instructions/dir returns the path without creating it."""
    from perun import config

    fresh = tmp_path / "fresh_instr"
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", fresh)
    r = client.get("/api/instructions/dir")
    assert r.status_code == 200
    assert r.json()["dir"] == str(fresh)
    assert not fresh.exists()


def test_project_instruction_delete_missing_returns_404(client, tmp_path):
    r = client.post("/api/projects", json={
        "method": "new", "name": "DelMissing", "path": str(tmp_path / "dm"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]
    assert client.delete(f"/api/projects/{slug}/instruction-files/missing.md").status_code == 404


def test_model_agent_save_writes_opencode_config(client, tmp_path):
    """Saving a model/agent override also lands in the project's opencode.json,
    so an OpenCode TUI launched from the card honors it."""
    target = tmp_path / "cfgsync"
    r = client.post("/api/projects", json={
        "method": "new", "name": "CfgSync", "path": str(target),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    # Set the override -> opencode.json is created with model/default_agent.
    r = client.patch(f"/api/projects/{slug}", json={
        "model": "anthropic/claude-sonnet-4-5", "agent": "plan",
    })
    assert r.status_code == 200, r.text
    assert r.json()["opencode_config"]["action"] == "created"
    cfg = json.loads((target / "opencode.json").read_text(encoding="utf-8"))
    assert cfg["model"] == "anthropic/claude-sonnet-4-5"
    assert cfg["default_agent"] == "plan"

    # An unrelated PATCH must not touch the config file.
    (target / "opencode.json").write_text(
        json.dumps({"model": "keep/me", "mcp": {}}), encoding="utf-8")
    r = client.patch(f"/api/projects/{slug}", json={"description": "hi"})
    assert r.status_code == 200, r.text
    assert r.json()["opencode_config"]["action"] == "skipped"
    cfg = json.loads((target / "opencode.json").read_text(encoding="utf-8"))
    assert cfg["model"] == "keep/me" and "default_agent" not in cfg

    # Clearing the override removes the keys but preserves other entries.
    r = client.patch(f"/api/projects/{slug}", json={"model": "", "agent": ""})
    assert r.status_code == 200, r.text
    assert r.json()["opencode_config"]["action"] == "updated"
    cfg = json.loads((target / "opencode.json").read_text(encoding="utf-8"))
    assert "model" not in cfg and "default_agent" not in cfg
    assert cfg["mcp"] == {}


def test_model_agent_save_preserves_existing_opencode_jsonc(client, tmp_path):
    """A pre-existing opencode.json (with other keys) keeps them on save."""
    target = tmp_path / "cfgmerge"
    (tmp_path / "cfgmerge").mkdir()
    (target / "opencode.json").write_text(json.dumps({
        "mcp": {"perun": {"type": "local", "command": ["python", "m.py"]}},
    }), encoding="utf-8")
    r = client.post("/api/projects", json={
        "method": "existing", "name": "CfgMerge", "path": str(target),
        "model": "openai/gpt-5",
    })
    slug = r.json()["project"]["slug"]
    cfg = json.loads((target / "opencode.json").read_text(encoding="utf-8"))
    assert cfg["model"] == "openai/gpt-5"
    assert "perun" in cfg["mcp"]


def test_model_agent_save_skips_jsonc_with_comments(client, tmp_path):
    """A commented opencode.jsonc is left untouched so comments are not lost."""
    target = tmp_path / "cfgjsonc"
    target.mkdir()
    (target / "opencode.jsonc").write_text(
        '{\n  // keep me\n  "mcp": {}\n}\n', encoding="utf-8")
    r = client.post("/api/projects", json={
        "method": "existing", "name": "CfgJsonC", "path": str(target),
    })
    slug = r.json()["project"]["slug"]
    r = client.patch(f"/api/projects/{slug}", json={"model": "x/y"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["opencode_config"]["action"] == "skipped"
    assert "warning" in body["opencode_config"]
    assert "// keep me" in (target / "opencode.jsonc").read_text(encoding="utf-8")


def test_model_agent_save_reports_unparseable_config(client, tmp_path):
    """A broken opencode.json is reported, not overwritten."""
    target = tmp_path / "cfgbroken"
    target.mkdir()
    (target / "opencode.json").write_text("not json", encoding="utf-8")
    r = client.post("/api/projects", json={
        "method": "existing", "name": "CfgBroken", "path": str(target),
    })
    slug = r.json()["project"]["slug"]
    r = client.patch(f"/api/projects/{slug}", json={"model": "x/y"})
    assert r.status_code == 200, r.text
    assert r.json()["opencode_config"]["action"] == "error"
    assert "warning" in r.json()["opencode_config"]
    assert (target / "opencode.json").read_text(encoding="utf-8") == "not json"


def test_settings_patch_coerces_and_validates(client):
    from perun import server

    server.SETTINGS.telegram_token = "secret-token"
    r = client.patch("/api/settings", json={
        "max_concurrent_runs": "7",
        "run_timeout": "3600",
        "port": 9000,
        "auto_discover_on_start": "false",
        "telegram_allowed_chats": "123, 456",
    })
    assert r.status_code == 200, r.text
    s = r.json()["settings"]
    assert s["max_concurrent_runs"] == 7
    assert s["run_timeout"] == 3600
    assert s["port"] == 9000
    assert s["auto_discover_on_start"] is False
    assert s["telegram_allowed_chats"] == ["123", "456"]
    # Token must never be echoed.
    assert s["telegram_token"] == ""
    assert s["telegram_token_set"] is True

    bad = client.patch("/api/settings", json={"max_concurrent_runs": "lots"})
    assert bad.status_code == 400
    bad2 = client.patch("/api/settings", json={"port": 70000})
    assert bad2.status_code == 400


def test_settings_patch_rejects_bad_types_and_leaks_no_token(client):
    from perun import server

    server.SETTINGS.telegram_token = "another-secret"
    r = client.patch("/api/settings", json={"max_concurrent_runs": 999})
    assert r.status_code == 200
    assert r.json()["settings"]["telegram_token"] == ""


def test_start_run_validation(client, monkeypatch):
    fake_runs: dict = {}

    def fake_start(ref, prompt, **kw):  # noqa: ANN001, ANN003
        if ref == "ghost":
            raise ValueError(f"no such project: {ref}")
        fake_runs["ref"] = ref
        fake_runs["prompt"] = prompt
        return {"run_uid": "runX", "status": "queued"}

    from perun import server

    monkeypatch.setattr(server.ORCH, "start_run", fake_start)
    # A missing project targets the control plane.
    r = client.post("/api/runs", json={"prompt": "hi"})
    assert r.status_code == 200
    assert fake_runs["ref"] == "perun"
    # An unknown project is an error.
    r = client.post("/api/runs", json={"project": "ghost", "prompt": "hi"})
    assert r.status_code == 400
    # A missing prompt is no longer an error — the orchestrator supplies a
    # default starter prompt so the session is opened in the project's scope.
    r = client.post("/api/runs", json={"project": "perun"})
    assert r.status_code == 200
    assert fake_runs["ref"] == "perun"


def test_start_project_session_endpoint(client, monkeypatch):
    captured: dict = {}

    def fake_start(ref, prompt, **kw):  # noqa: ANN001, ANN003
        if ref == "ghost":
            raise ValueError(f"no such project: {ref}")
        captured["ref"] = ref
        captured["prompt"] = prompt
        return {"run_uid": "runS", "status": "queued"}

    from perun import server

    monkeypatch.setattr(server.ORCH, "start_run", fake_start)
    r = client.post("/api/projects/perun/session", json={})
    assert r.status_code == 200
    assert r.json()["run"]["run_uid"] == "runS"
    assert captured["ref"] == "perun"
    # Custom prompt still works.
    r = client.post("/api/projects/perun/session", json={"prompt": "custom"})
    assert r.status_code == 200
    assert captured["prompt"] == "custom"
    # Unknown project returns 404 from the route guard before start_run.
    assert client.post("/api/projects/ghost/session", json={}).status_code == 404


def test_agents_endpoint(client, monkeypatch):
    from perun import agents, server

    monkeypatch.setattr(agents, "list_agents", lambda: [
        {"id": "build", "name": "Build", "mode": "primary", "description": "x"},
    ])
    # Reset the endpoint's cache so the stub is used.
    server._AGENTS_CACHE["data"] = None
    r = client.get("/api/agents")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert any(a["id"] == "build" for a in body["agents"])


def test_activity_endpoint(client):
    r = client.get("/api/activity")
    assert r.json()["ok"] is True


def test_runs_list(client):
    r = client.get("/api/runs")
    assert r.json()["ok"] is True


def test_404_for_unknown_run(client):
    assert client.get("/api/runs/nope").status_code == 404


def test_active_runs_endpoint(client):
    """The FX endpoint maps active runs to projects and isn't shadowed by
    the ``/api/runs/{run_uid}`` path parameter."""
    from perun import registry

    p = registry.add_project("Active", r"C:\active")
    body = client.get("/api/runs/active").json()
    assert body["ok"] and body["runs"] == [] and body["projects"] == []
    assert set(body) >= {"runs", "live", "projects", "count"}

    registry.create_run("run-active", p["id"], "do work", model="m", agent="a")
    registry.update_run("run-active", status="running")
    body = client.get("/api/runs/active").json()
    assert [r["run_uid"] for r in body["runs"]] == ["run-active"]
    assert body["projects"] == ["active"]
    assert body["runs"][0]["project_slug"] == "active"
    assert body["runs"][0]["project_name"] == "Active"

    registry.update_run("run-active", status="done")
    assert client.get("/api/runs/active").json()["runs"] == []
    # Still a 404 for a genuinely unknown run — proof the order is right.
    assert client.get("/api/runs/nope").status_code == 404


# --------------------------------------------------------------------------- #
# Project wizard + git + filesystem
# --------------------------------------------------------------------------- #
def test_wizard_create_new_project_with_git(client, tmp_path):
    from perun import git

    target = tmp_path / "wizproj"
    r = client.post("/api/projects", json={
        "method": "new", "name": "Wizard", "path": str(target),
        "bootstrap": True, "git_init": True, "git_branch": "main",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    slug = body["project"]["slug"]
    assert (target / "opencode.json").is_file()
    if git.available():
        assert body["git"]["repo"] is True
        # commit via the API
        (target / "file.txt").write_text("hi", encoding="utf-8")
        c = client.post(f"/api/projects/{slug}/git/commit", json={"message": "add file"})
        assert c.status_code == 200
        assert c.json()["committed"] is True
        log = client.get(f"/api/projects/{slug}/git/log")
        assert log.json()["commits"]


def test_fs_list_and_mkdir(client, tmp_path):
    r = client.get("/api/fs/list", params={"path": str(tmp_path)})
    assert r.status_code == 200
    assert r.json()["path"]

    made = tmp_path / "made"
    r = client.post("/api/fs/mkdir", json={"path": str(made)})
    assert r.json()["created"] is True
    assert made.is_dir()

    r = client.get("/api/fs/list", params={"path": str(tmp_path / "missing")})
    assert r.status_code == 400


def test_git_remote_invalid_is_rejected(client, tmp_path):
    from perun import git

    r = client.post("/api/projects", json={
        "method": "new", "name": "GitRemote", "path": str(tmp_path / "gr"),
        "bootstrap": False, "git_init": True,
    })
    slug = r.json()["project"]["slug"]
    bad = client.post(f"/api/projects/{slug}/git/remote", json={"url": "-flag"})
    assert bad.status_code == 400
    if git.available():
        ok = client.post(f"/api/projects/{slug}/git/remote",
                         json={"url": "https://example.com/a/b.git", "branch": "main"})
        assert ok.status_code == 200
        assert ok.json()["git"]["has_remote"] is True


def test_models_endpoint_shape(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    assert "models" in r.json()


def test_providers_endpoint(client, monkeypatch):
    """GET /api/providers proxies OpenCode auth list with connection status."""
    monkeypatch.setattr(
        server.providers, "list_providers",
        lambda refresh=False: {
            "ok": True,
            "providers": [
                {"id": "opencode-go", "name": "OpenCode Go", "connected": True,
                 "status": "connected", "connections": [{"type": "credential", "label": "API key"}]},
                {"id": "ollama-cloud", "name": "Ollama Cloud", "connected": False,
                 "status": "not_connected", "connections": []},
            ],
            "count": 2,
            "error": None,
        })
    r = client.get("/api/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["count"] == 2
    assert body["providers"][0]["connected"] is True
    assert body["providers"][1]["connected"] is False


def test_provider_logout_endpoint(client, monkeypatch):
    """POST /api/providers/{id}/logout shells out to opencode auth logout."""
    monkeypatch.setattr(
        server.providers, "logout_provider",
        lambda pid: {"ok": True, "error": None})
    r = client.post("/api/providers/opencode-go/logout")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_provider_logout_endpoint_returns_400_on_invalid_id(client, monkeypatch):
    """Invalid provider IDs are rejected with HTTP 400."""
    monkeypatch.setattr(
        server.providers, "logout_provider",
        lambda pid: {"ok": False, "error": f"invalid provider id: {pid!r}"})
    r = client.post("/api/providers/bad id!/logout")
    assert r.status_code == 400
    assert "invalid provider id" in r.json()["detail"]


def test_provider_connect_endpoint(client, monkeypatch):
    """POST /api/providers/{id}/connect writes a provider entry."""
    monkeypatch.setattr(
        server.providers, "connect_provider",
        lambda pid, payload: {"ok": True, "provider_id": pid, "reloaded": True, "path": "/tmp/opencode.json"})
    r = client.post("/api/providers/openai/connect", json={"api_key": "sk-test"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["provider_id"] == "openai"


def test_provider_disconnect_endpoint(client, monkeypatch):
    """POST /api/providers/{id}/disconnect removes config and optionally logs out."""
    monkeypatch.setattr(
        server.providers, "disconnect_provider",
        lambda pid: {"ok": True, "logout": {"ok": True}, "config": {"removed": True}})
    r = client.post("/api/providers/openai/disconnect")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# --------------------------------------------------------------------------- #
# Project instructions + manifest
# --------------------------------------------------------------------------- #
def test_project_instructions_roundtrip(client, tmp_path):
    r = client.post("/api/projects", json={
        "method": "new", "name": "Instr", "path": str(tmp_path / "instr"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    assert client.get(f"/api/projects/{slug}/instructions").json()["instructions"] == ""

    r = client.put(f"/api/projects/{slug}/instructions",
                   json={"instructions": "Always run tests."})
    assert r.status_code == 200
    assert r.json()["instructions"] == "Always run tests."
    assert r.json()["filename"] == "default.md"

    assert client.get(f"/api/projects/{slug}/instructions").json()["instructions"] \
        == "Always run tests."
    # The file lives under .perun/instructions/default.md.
    target = tmp_path / "instr" / ".perun" / "instructions" / "default.md"
    assert target.is_file()
    assert "Always run tests." in target.read_text(encoding="utf-8")


def test_project_instruction_files_crud(client, tmp_path):
    r = client.post("/api/projects", json={
        "method": "new", "name": "InstrFiles", "path": str(tmp_path / "if"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    # Empty list initially.
    assert client.get(f"/api/projects/{slug}/instruction-files").json()["files"] == []

    # Create two files.
    r = client.post(f"/api/projects/{slug}/instruction-files/alpha.md", json={
        "name": "Alpha", "body": "ALPHA RULE", "enabled": True,
    })
    assert r.status_code == 200
    assert r.json()["filename"] == "alpha.md"

    client.post(f"/api/projects/{slug}/instruction-files/beta.md", json={
        "name": "Beta", "body": "BETA RULE", "enabled": True,
    })

    # List returns sorted files.
    files = client.get(f"/api/projects/{slug}/instruction-files").json()["files"]
    assert [f["filename"] for f in files] == ["alpha.md", "beta.md"]

    # Read single file.
    detail = client.get(f"/api/projects/{slug}/instruction-files/alpha.md").json()
    assert detail["body"].strip() == "ALPHA RULE"
    assert detail["enabled"] is True

    # Toggle off.
    r = client.patch(f"/api/projects/{slug}/instruction-files/alpha.md/toggle",
                     json={"enabled": False})
    assert r.json()["enabled"] is False
    assert client.get(f"/api/projects/{slug}/instruction-files/alpha.md").json()["enabled"] is False

    # Delete.
    assert client.delete(f"/api/projects/{slug}/instruction-files/beta.md").status_code == 200
    files = client.get(f"/api/projects/{slug}/instruction-files").json()["files"]
    assert [f["filename"] for f in files] == ["alpha.md"]


def test_project_instruction_files_path_traversal(client, tmp_path):
    from urllib.parse import quote

    r = client.post("/api/projects", json={
        "method": "new", "name": "Traverse", "path": str(tmp_path / "tr"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    for bad in ["../escape.md", "a/../b.md", "\\windows.md", ""]:
        encoded = quote(bad, safe="")
        path = f"/api/projects/{slug}/instruction-files/{encoded}"
        res = client.post(path, json={"body": "x"})
        assert res.status_code in (400, 404, 405), f"{bad}: {res.status_code}"

    # A filename containing a path separator does not match the single-segment route.
    res = client.post(f"/api/projects/{slug}/instruction-files/sub/file.md", json={"body": "x"})
    assert res.status_code in (404, 405)


def test_project_instruction_filename_length_and_nul_rejected(client, tmp_path):
    """A 10 000-char or NUL-bearing name must be a 400, not a filesystem 500."""
    from urllib.parse import quote

    r = client.post("/api/projects", json={
        "method": "new", "name": "LongName", "path": str(tmp_path / "ln"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    too_long = "a" * 10_000 + ".md"
    res = client.post(
        f"/api/projects/{slug}/instruction-files/{too_long}", json={"body": "x"})
    assert res.status_code == 400

    with_nul = quote("bad\x00name.md", safe="")
    res = client.post(
        f"/api/projects/{slug}/instruction-files/{with_nul}", json={"body": "x"})
    assert res.status_code == 400


def test_manifest_export_and_read(client, tmp_path):
    target = tmp_path / "manifestproj"
    r = client.post("/api/projects", json={
        "method": "new", "name": "Manifest", "path": str(target), "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]
    (target / "README.md").write_text("# Manifest\n", encoding="utf-8")

    # Export writes perun.md from registry state.
    r = client.post(f"/api/projects/{slug}/manifest/export")
    assert r.status_code == 200, r.text
    assert (target / "perun.md").is_file()

    # Live read reports the manifest.
    r = client.get(f"/api/projects/{slug}/manifest")
    assert r.status_code == 200
    body = r.json()["manifest"]
    assert body["found"] is True
    assert body["data"]["name"] == "Manifest"

    # Exporting twice is a conflict (manifest already exists).
    assert client.post(f"/api/projects/{slug}/manifest/export").status_code == 409


def test_manifest_put_content_updates_registry(client, tmp_path):
    target = tmp_path / "manput"
    r = client.post("/api/projects", json={
        "method": "new", "name": "Before", "path": str(target), "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    content = ("---\nversion: 1\nname: After\ndescription: from manifest\n"
               "tags: [a, b]\n---\n\n# After\n")
    r = client.put(f"/api/projects/{slug}/manifest", json={"content": content})
    assert r.status_code == 200, r.text
    assert (target / "perun.md").is_file()
    # Declared fields flow into the registry.
    proj = client.get(f"/api/projects/{slug}").json()["project"]
    assert proj["name"] == "After"
    assert proj["description"] == "from manifest"
    assert proj["has_manifest"] is True

    # Refresh re-reads from disk.
    r = client.post(f"/api/projects/{slug}/manifest/refresh")
    assert r.status_code == 200
    assert r.json()["manifest"]["data"]["name"] == "After"


def test_manifest_put_structured(client, tmp_path):
    target = tmp_path / "manstruct"
    r = client.post("/api/projects", json={
        "method": "new", "name": "Struct", "path": str(target), "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]
    r = client.put(f"/api/projects/{slug}/manifest", json={
        "data": {"version": 1, "name": "Struct",
                 "anchors": [{"label": "Entry", "kind": "file", "path": "main.py"}]},
        "body": "# Struct\n",
    })
    assert r.status_code == 200, r.text
    snap = client.get(f"/api/projects/{slug}/manifest").json()["manifest"]
    assert snap["data"]["anchors"][0]["path"] == "main.py"


def test_manifest_put_instructions_creates_file(client, tmp_path):
    """A manifest PUT with ``instructions`` seeds .perun/instructions/default.md."""
    target = tmp_path / "maninstr"
    r = client.post("/api/projects", json={
        "method": "new", "name": "InstrPack", "path": str(target), "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    r = client.put(f"/api/projects/{slug}/manifest", json={
        "data": {"version": 1, "name": "InstrPack",
                 "instructions": "MANIFEST RULE 7"},
        "body": "",
    })
    assert r.status_code == 200, r.text

    target_file = target / ".perun" / "instructions" / "default.md"
    assert target_file.is_file()
    assert "MANIFEST RULE 7" in target_file.read_text(encoding="utf-8")


def test_manifest_missing_returns_not_found_on_refresh(client, tmp_path):
    r = client.post("/api/projects", json={
        "method": "new", "name": "NoManifest", "path": str(tmp_path / "nm"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]
    assert client.post(f"/api/projects/{slug}/manifest/refresh").status_code == 404
    # Live read degrades gracefully.
    r = client.get(f"/api/projects/{slug}/manifest")
    assert r.status_code == 200
    assert r.json()["manifest"]["found"] is False


# --------------------------------------------------------------------------- #
# Tasks, skills, tools, TUI runs, run-in-terminal
# --------------------------------------------------------------------------- #
def test_project_tasks_crud(client, tmp_path):
    r = client.post("/api/projects", json={
        "method": "new", "name": "TaskProj", "path": str(tmp_path / "tp"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    # list empty
    r = client.get(f"/api/projects/{slug}/tasks")
    assert r.status_code == 200
    assert r.json()["tasks"] == []

    # create
    r = client.post(f"/api/projects/{slug}/tasks", json={
        "name": "audit", "kind": "combo", "prompt": "audit recent changes",
        "tools": ["read", "grep"], "skills": ["code-audit"], "enabled": True,
    })
    assert r.status_code == 200
    task = r.json()["task"]
    assert task["name"] == "audit"
    assert task["tools"] == ["read", "grep"]
    assert task["skills"] == ["code-audit"]
    assert task["enabled"] == 1
    task_id = task["id"]

    # list
    r = client.get(f"/api/projects/{slug}/tasks")
    assert len(r.json()["tasks"]) == 1

    # update
    r = client.patch(f"/api/projects/{slug}/tasks/{task_id}", json={
        "name": "audit v2", "enabled": False,
    })
    assert r.status_code == 200
    assert r.json()["task"]["name"] == "audit v2"
    assert r.json()["task"]["enabled"] == 0

    # delete
    r = client.delete(f"/api/projects/{slug}/tasks/{task_id}")
    assert r.status_code == 200
    assert client.get(f"/api/projects/{slug}/tasks").json()["tasks"] == []

    # unknown project
    assert client.get("/api/projects/ghost/tasks").status_code == 404
    assert client.post("/api/projects/ghost/tasks", json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/projects/{slug}/tasks/99999").status_code == 404

    # missing name rejected
    assert client.post(f"/api/projects/{slug}/tasks", json={"kind": "prompt"}).status_code == 400


def test_tui_run_lifecycle(client, tmp_path, monkeypatch):
    from perun import process_tracker

    fresh = process_tracker.ProcessTracker()
    monkeypatch.setattr(process_tracker, "TRACKER", fresh)

    r = client.post("/api/projects", json={
        "method": "new", "name": "TUIProj", "path": str(tmp_path / "tui"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    r = client.post("/api/runs/tui", json={"project": slug, "prompt": "hello tui"})
    assert r.status_code == 200
    run_uid = r.json()["run"]["run_uid"]
    assert r.json()["run"]["status"] == "running"

    # finish
    r = client.post(f"/api/runs/{run_uid}/finish")
    assert r.status_code == 200
    run = client.get(f"/api/runs/{run_uid}").json()["run"]
    assert run["status"] == "done"

    # kill on unknown run
    r = client.post("/api/runs/unknown/kill")
    assert r.status_code == 200
    assert r.json()["killed"] is False


def test_cancel_short_circuits_untracked_run(client, monkeypatch):
    """Canceling a headless run should not call process_tracker when the run is not tracked."""
    from perun import process_tracker, server

    fresh = process_tracker.ProcessTracker()
    monkeypatch.setattr(process_tracker, "TRACKER", fresh)

    called: dict = {}

    def fake_cancel(run_uid):
        called["cancel"] = run_uid
        return True

    monkeypatch.setattr(server.ORCH, "cancel", fake_cancel)

    r = client.post("/api/runs/headless-only/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    assert called.get("cancel") == "headless-only"
    assert fresh.get("headless-only") is None


def test_run_in_terminal_starts_tracked_process(client, tmp_path, monkeypatch):
    from perun import process_tracker

    fresh = process_tracker.ProcessTracker()
    monkeypatch.setattr(process_tracker, "TRACKER", fresh)

    r = client.post("/api/projects", json={
        "method": "new", "name": "TermProj", "path": str(tmp_path / "term"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]
    run_uid = "term-run-123"

    # We cannot reliably open a real terminal in unit tests, so stub subprocess.Popen
    # and verify the tracker receives the run_uid.
    class FakeProc:
        pid = 12345
        def poll(self): return None

    orig_popen = __import__("subprocess").Popen
    def fake_popen(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    r = client.post("/api/run-in-terminal", json={
        "project": slug, "tui": True, "run_uid": run_uid,
        "shell": "cmd",
    })
    assert r.status_code == 200
    assert r.json()["launched"] is True
    assert fresh.get(run_uid) is not None
    assert fresh.get(run_uid)["pid"] == 12345

    # Neither a cwd nor a project to take one from is rejected.
    assert client.post("/api/run-in-terminal", json={"shell": "cmd"}).status_code == 400


def test_run_in_terminal_powershell_no_duplicate_flags(client, tmp_path, monkeypatch):
    from perun import process_tracker, config

    fresh = process_tracker.ProcessTracker()
    monkeypatch.setattr(process_tracker, "TRACKER", fresh)

    r = client.post("/api/projects", json={
        "method": "new", "name": "PwshProj", "path": str(tmp_path / "pwsh"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    captured: list[list[str]] = []

    class FakeProc:
        pid = 99999
        def poll(self): return None

    def fake_popen(*args, **kwargs):
        captured.append(list(args[0]))
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    # Force terminal_cmd to return the standard PowerShell argv prefix.
    monkeypatch.setattr(config.Settings, "terminal_cmd",
                        lambda self, shell_hint="": ["pwsh", "-NoExit", "-Command"])

    r = client.post("/api/run-in-terminal", json={
        "project": slug, "shell": "pwsh", "tui": True, "prompt": "hi there",
    })
    assert r.status_code == 200
    argv = captured[-1]
    # Must not duplicate -NoExit/-Command flags.
    assert argv.count("-NoExit") == 1
    assert argv.count("-Command") == 1
    # The command is assembled server-side, with the prompt quoted for PowerShell.
    assert "opencode" in argv[-1]
    assert "--prompt 'hi there'" in argv[-1]
    assert str(tmp_path / "pwsh") in argv[-1]


def test_run_in_terminal_prompt_cannot_inject_powershell(client, tmp_path, monkeypatch):
    """A prompt containing ``$(...)`` must stay data, never PowerShell code."""
    from perun import config

    r = client.post("/api/projects", json={
        "method": "new", "name": "PsInj", "path": str(tmp_path / "psinj"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]
    captured: list[list[str]] = []

    class FakeProc:
        pid = 1
        def poll(self): return None

    monkeypatch.setattr("subprocess.Popen",
                        lambda *a, **kw: (captured.append(list(a[0])), FakeProc())[1])
    monkeypatch.setattr(config.Settings, "terminal_cmd",
                        lambda self, shell_hint="": ["pwsh", "-NoExit", "-Command"])
    evil = "$(calc.exe); rm -rf ~"
    r = client.post("/api/run-in-terminal", json={
        "project": slug, "shell": "pwsh", "tui": True, "prompt": evil,
    })
    assert r.status_code == 200
    text = captured[-1][-1]
    # Single-quoted literal: ``$`` is inert and no doubled quote is introduced.
    assert f"--prompt '{evil}'" in text
    assert "''" not in text


def test_run_in_terminal_cmd_uses_string_command_line(client, tmp_path, monkeypatch):
    from perun import process_tracker, config

    fresh = process_tracker.ProcessTracker()
    monkeypatch.setattr(process_tracker, "TRACKER", fresh)

    r = client.post("/api/projects", json={
        "method": "new", "name": "CmdProj", "path": str(tmp_path / "cmd"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    captured: list[tuple[object, ...]] = []

    class FakeProc:
        pid = 55555
        def poll(self): return None

    def fake_popen(*args, **kwargs):
        captured.append((args, kwargs))
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(config.Settings, "terminal_cmd",
                        lambda self, shell_hint="": ["cmd.exe"])

    r = client.post("/api/run-in-terminal", json={
        "project": slug, "shell": "cmd", "tui": True, "prompt": "hi",
    })
    assert r.status_code == 200
    (popen_args, popen_kwargs) = captured[-1]
    # The command text must reach cmd.exe verbatim. list2cmdline would escape
    # the inner quotes as \" (C-runtime style), which cmd.exe cannot parse and
    # turns into "The filename, directory name, or volume label syntax is
    # incorrect." So no backslash-escaped quotes may appear.
    assert isinstance(popen_args[0], str)
    assert popen_args[0].startswith("cmd.exe /k ")
    assert '\\"' not in popen_args[0]
    cmd_dir = str(tmp_path / "cmd")
    assert popen_args[0].endswith(f'cd /d "{cmd_dir}" && opencode --prompt "hi"')
    assert popen_kwargs.get("cwd") == cmd_dir


def test_run_in_terminal_wt_keeps_raw_quotes(client, tmp_path, monkeypatch):
    from perun import process_tracker, config

    fresh = process_tracker.ProcessTracker()
    monkeypatch.setattr(process_tracker, "TRACKER", fresh)

    r = client.post("/api/projects", json={
        "method": "new", "name": "WtProj", "path": str(tmp_path / "wt"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    captured: list[tuple[object, ...]] = []

    class FakeProc:
        pid = 44444
        def poll(self): return None

    def fake_popen(*args, **kwargs):
        captured.append((args, kwargs))
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(config.Settings, "terminal_cmd",
                        lambda self, shell_hint="": ["wt.exe", "-d", "."])

    r = client.post("/api/run-in-terminal", json={
        "project": slug, "shell": "wt", "tui": True,
    })
    assert r.status_code == 200
    (popen_args, _) = captured[-1]
    assert isinstance(popen_args[0], str)
    assert popen_args[0].startswith("wt.exe -d ")
    assert '\\"' not in popen_args[0]
    wt_dir = str(tmp_path / "wt")
    assert popen_args[0].endswith(f' cmd /k cd /d "{wt_dir}" && opencode')


def test_run_in_terminal_full_path_terminal_is_recognized(client, tmp_path, monkeypatch):
    """A configured terminal with a full Windows path must still be recognized."""
    from perun import process_tracker, config

    fresh = process_tracker.ProcessTracker()
    monkeypatch.setattr(process_tracker, "TRACKER", fresh)

    r = client.post("/api/projects", json={
        "method": "new", "name": "FullPathTerm", "path": str(tmp_path / "fpt"),
        "bootstrap": False,
    })
    slug = r.json()["project"]["slug"]

    captured: list[tuple[object, ...]] = []

    class FakeProc:
        pid = 33333
        def poll(self): return None

    def fake_popen(*args, **kwargs):
        captured.append((args, kwargs))
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    # Simulate a user who configured an absolute path to Windows Terminal.
    monkeypatch.setattr(config.Settings, "terminal_cmd",
                        lambda self, shell_hint="": [r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\wt.exe", "-d", "."])

    r = client.post("/api/run-in-terminal", json={
        "project": slug, "shell": r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\wt.exe", "tui": True,
    })
    assert r.status_code == 200
    (popen_args, _) = captured[-1]
    assert isinstance(popen_args[0], str)
    # Must route through the Windows Terminal branch (cmd /k ...) not the POSIX -c branch.
    assert ' cmd /k ' in popen_args[0]
    assert 'wt.exe -d ' in popen_args[0]
    assert '\\"' not in popen_args[0]


def test_run_in_terminal_auth_login_quotes_provider(client, monkeypatch):
    from perun import config

    captured: list[list[str]] = []

    class FakeProc:
        pid = 7
        def poll(self): return None

    monkeypatch.setattr("subprocess.Popen",
                        lambda *a, **kw: (captured.append(list(a[0])), FakeProc())[1])
    monkeypatch.setattr(config.Settings, "terminal_cmd",
                        lambda self, shell_hint="": ["pwsh", "-NoExit", "-Command"])
    r = client.post("/api/run-in-terminal", json={
        "action": "auth-login", "provider": "openai", "shell": "pwsh",
    })
    assert r.status_code == 200
    assert captured[-1][-1].endswith("opencode auth login 'openai'")
    # A provider id that is not a plain slug is refused.
    bad = client.post("/api/run-in-terminal", json={
        "action": "auth-login", "provider": "x; calc", "shell": "pwsh",
    })
    assert bad.status_code == 400


def test_skills_and_tools_endpoints(client, tmp_path, monkeypatch):
    from perun import skills

    # Stub global skills/tools so the test doesn't depend on the user's home dir.
    def fake_discover_skills(project_path=None):
        return [{"name": "audit", "description": "x", "project_local": False}]

    def fake_discover_tools(project_path=None):
        return [
            {"name": "bash", "description": "shell", "project_local": False, "builtin": True},
            {"name": "custom", "description": "c", "project_local": True, "builtin": False},
        ]

    monkeypatch.setattr(skills, "discover_skills", fake_discover_skills)
    monkeypatch.setattr(skills, "discover_tools", fake_discover_tools)

    r = client.get("/api/skills")
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["skills"][0]["name"] == "audit"

    r = client.get("/api/tools")
    assert r.status_code == 200
    assert r.json()["count"] == 2
    names = {t["name"] for t in r.json()["tools"]}
    assert names == {"bash", "custom"}

    # project scoping still resolves the project
    assert client.get("/api/skills?project=perun").status_code == 200
    assert client.get("/api/tools?project=perun").status_code == 200
    assert client.get("/api/skills?project=ghost").status_code == 200
