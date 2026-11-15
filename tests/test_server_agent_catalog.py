"""Agent catalogue API and project-scoped agent discovery.

Perun does not install third-party agents, so these tests place a file exactly
where the upstream installer would and assert Perun adopts/guards it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from perun import agent_catalog, config


@pytest.fixture()
def client(monkeypatch):
    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as c:
        yield c


@pytest.fixture(autouse=True)
def no_runtime_probe(monkeypatch):
    """Never shell out to `opencode debug agents` from the API tests."""
    monkeypatch.setattr(agent_catalog, "registered", lambda *a, **k: {
        "ok": False, "count": None, "ids": [], "error": "disabled in tests"})
    manifest = Path(config.HOME) / "agent_catalog.json"
    if manifest.exists():
        manifest.unlink()
    yield
    if manifest.exists():
        manifest.unlink()


@pytest.fixture()
def project(tmp_path):
    from perun import registry

    root = tmp_path / "repo"
    (root / ".opencode" / "agents").mkdir(parents=True)
    return registry.add_project("catalog-server", str(root), kind="opencode")


def _install(project, slug: str = "code-reviewer") -> Path:
    path = Path(project["path"]) / ".opencode" / "agents" / f"{slug}.md"
    path.write_text(
        f"---\nname: {slug.replace('-', ' ').title()}\n"
        f"description: {slug} specialist\nmode: subagent\ncolor: '#3B82F6'\n---\nBody\n",
        encoding="utf-8")
    return path


def test_status_reports_the_index_and_the_budget(client, project):
    body = client.get(f"/api/agent-catalog/status?project={project['slug']}&measure=false").json()
    assert body["ok"] is True
    assert body["index"]["available"] is True
    assert body["index"]["agents"] > 200
    assert body["slots"]["limit"] == agent_catalog.DEFAULT_LIMIT
    assert body["upstream"]["url"].startswith("https://github.com/")
    assert body["counts"]["candidate"] == 0


def test_status_rejects_an_unknown_project(client):
    r = client.get("/api/agent-catalog/status?project=nope&measure=false")
    assert r.status_code == 404


def test_adopt_then_remove_a_project_agent(client, project):
    path = _install(project)

    status = client.get(f"/api/agent-catalog/status?project={project['slug']}&measure=false").json()
    assert status["counts"]["candidate"] == 1
    assert status["agents"][0]["slug"] == "code-reviewer"

    adopted = client.post("/api/agent-catalog/adopt", json={"project": project["slug"]})
    assert adopted.status_code == 200, adopted.text
    assert len(adopted.json()["adopted"]) == 1

    after = client.get(f"/api/agent-catalog/status?project={project['slug']}&measure=false").json()
    assert after["counts"]["adopted"] == 1

    removed = client.post("/api/agent-catalog/remove", json={"path": str(path)})
    assert removed.status_code == 200, removed.text
    assert removed.json()["backup"]
    assert not path.exists()


def test_remove_refuses_an_edited_agent(client, project):
    path = _install(project)
    client.post("/api/agent-catalog/adopt", json={"project": project["slug"]})
    path.write_text(path.read_text(encoding="utf-8") + "\nmine\n", encoding="utf-8")

    r = client.post("/api/agent-catalog/remove", json={"path": str(path)})
    assert r.status_code == 400
    assert "edited" in r.json()["detail"]
    assert path.exists()


def test_adopt_rejects_a_path_outside_the_agent_directories(client, project, tmp_path):
    outside = tmp_path / "code-reviewer.md"
    outside.write_text("---\nname: Code Reviewer\n---\n", encoding="utf-8")

    r = client.post("/api/agent-catalog/adopt",
                    json={"project": project["slug"], "paths": [str(outside)]})
    assert r.status_code == 200
    assert r.json()["adopted"] == []
    assert r.json()["skipped"]


def test_team_endpoint_refuses_a_selection_over_the_limit(client):
    r = client.get("/api/agent-catalog/team?divisions=engineering&limit=3")
    assert r.status_code == 400
    assert "ceiling" in r.json()["detail"]

    ok = client.get("/api/agent-catalog/team?divisions=engineering&limit=200")
    assert ok.status_code == 200
    body = ok.json()
    assert body["count"] <= 200
    assert "code-reviewer" in body["agents"]


def test_project_scoped_agent_listing_includes_local_agents(client, project):
    from perun import server

    _install(project, "code-reviewer")
    server._AGENTS_CACHE.update(ts=0.0, data=None)
    server._AGENTS_CACHE["projects"].clear()

    scoped = client.get(f"/api/agents?project={project['slug']}&refresh=true").json()
    assert scoped["scope"] == "project"
    assert any(a["id"] == "code-reviewer" for a in scoped["agents"])

    server._AGENTS_CACHE.update(ts=0.0, data=None)
    global_list = client.get("/api/agents?refresh=true").json()
    assert global_list["scope"] == "global"
    assert not any(a["id"] == "code-reviewer" for a in global_list["agents"])


def test_unknown_project_agent_listing_is_404(client):
    assert client.get("/api/agents?project=nope").status_code == 404
