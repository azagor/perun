"""HTTP API smoke tests via FastAPI's TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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


def test_memory_stats(client):
    r = client.get("/api/memory/stats")
    assert "facts_total" in r.json()["stats"]


def test_settings_get_patch(client):
    r = client.get("/api/settings")
    assert r.json()["ok"] is True
    r = client.patch("/api/settings", json={"max_concurrent_runs": 5})
    assert r.json()["settings"]["max_concurrent_runs"] == 5
    assert client.get("/api/settings").json()["settings"]["max_concurrent_runs"] == 5


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

    assert client.get(f"/api/projects/{slug}/instructions").json()["instructions"] \
        == "Always run tests."
    # Also surfaced on the project detail.
    detail = client.get(f"/api/projects/{slug}").json()
    assert detail["project"]["instructions"] == "Always run tests."


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
        "project": slug, "command": "opencode", "run_uid": run_uid,
        "shell": "cmd",
    })
    assert r.status_code == 200
    assert r.json()["launched"] is True
    assert fresh.get(run_uid) is not None
    assert fresh.get(run_uid)["pid"] == 12345

    # missing command rejected
    assert client.post("/api/run-in-terminal", json={"project": slug}).status_code == 400


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
        "project": slug, "command": "cd \"C:\\\\x\"; opencode", "shell": "pwsh",
    })
    assert r.status_code == 200
    argv = captured[-1]
    # Must not duplicate -NoExit/-Command flags.
    assert argv.count("-NoExit") == 1
    assert argv.count("-Command") == 1
    assert argv[-1] == "cd \"C:\\\\x\"; opencode"


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
        "project": slug,
        "command": 'cd /d "C:\\\\x" && opencode --prompt "hi"',
        "shell": "cmd",
    })
    assert r.status_code == 200
    (popen_args, popen_kwargs) = captured[-1]
    # Passing the command as a single string prevents Python's list2cmdline from
    # wrapping it in outer quotes, which cmd.exe would strip and corrupt.
    assert isinstance(popen_args[0], str)
    assert "cmd.exe /k " in popen_args[0]
    assert 'cd /d "C:\\\\x" && opencode --prompt "hi"' in popen_args[0]
    assert popen_kwargs.get("cwd") == str(tmp_path / "cmd")


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
