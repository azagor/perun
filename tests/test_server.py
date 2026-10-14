"""HTTP API smoke tests via FastAPI's TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from perun import server

    server.SETTINGS.auto_discover_on_start = False  # keep tests deterministic
    with TestClient(server.app) as c:
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


def test_start_run_validation(client):
    r = client.post("/api/runs", json={"prompt": "hi"})
    assert r.status_code == 400  # missing project
    r = client.post("/api/runs", json={"project": "ghost", "prompt": "hi"})
    assert r.status_code == 400  # unknown project


def test_activity_endpoint(client):
    r = client.get("/api/activity")
    assert r.json()["ok"] is True


def test_runs_list(client):
    r = client.get("/api/runs")
    assert r.json()["ok"] is True


def test_404_for_unknown_run(client):
    assert client.get("/api/runs/nope").status_code == 404
