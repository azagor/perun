"""VS Code API: status, linking, tickets and the companion WebSocket."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as c:
        yield c


@pytest.fixture()
def project(tmp_path):
    from perun import registry

    root = tmp_path / "repo"
    root.mkdir()
    return registry.add_project("vscode-server", str(root), kind="opencode")


def test_link_generates_a_workspace_and_status_reports_it(client, project):
    r = client.post(f"/api/projects/{project['slug']}/vscode/link",
                    json={"launch_mode": "vscode-terminal"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace"].endswith(".code-workspace")
    assert body["project"]["ide"]["vscode_linked"] is True
    assert body["project"]["ide"]["launch_mode"] == "vscode-terminal"

    status = client.get("/api/vscode/status").json()
    assert status["linked_projects"] == 1


def test_unlink_removes_the_managed_workspace(client, project):
    client.post(f"/api/projects/{project['slug']}/vscode/link", json={})
    r = client.post(f"/api/projects/{project['slug']}/vscode/unlink")
    assert r.status_code == 200
    assert r.json()["removed_workspace"] is True
    assert client.get("/api/vscode/status").json()["linked_projects"] == 0


def test_a_ticket_is_bound_to_the_workspace(client, project):
    r = client.post("/api/vscode/ticket", json={"workspace_path": project["path"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ticket"] and body["project"]["slug"] == project["slug"]

    unknown = client.post("/api/vscode/ticket", json={"workspace_path": "C:\\nope"})
    assert unknown.status_code == 404


def test_the_websocket_requires_a_valid_ticket(client, project):
    r = client.post("/api/vscode/ticket", json={"workspace_path": project["path"]})
    ticket = r.json()["ticket"]

    with client.websocket_connect(f"/api/vscode/ws?ticket={ticket}") as ws:
        ws.send_json({
            "type": "hello",
            "workspace_path": project["path"],
            "window_id": "w1",
            "protocol": 1,
            "extension_version": "0.1.0",
        })
        answer = ws.receive_json()
        assert answer["type"] == "hello.ok"
        assert answer["project"]["slug"] == project["slug"]

    with pytest.raises(Exception):
        with client.websocket_connect("/api/vscode/ws?ticket=bad"):
            pass


def test_opening_a_run_without_a_companion_is_a_clear_conflict(client, project):
    from perun import registry

    registry.create_run("run_no_client", project["id"], "TUI", standalone=True)
    r = client.post("/api/runs/run_no_client/vscode/open")
    assert r.status_code == 409
    assert "companion" in r.json()["detail"]


def test_the_embed_ticket_allows_a_framed_dashboard_until_it_expires(client):
    ticket = client.post("/api/vscode/embed-ticket").json()["ticket"]

    page = client.get(f"/?embed={ticket}")
    assert page.status_code == 200
    assert "x-frame-options" not in page.headers
    assert "frame-ancestors *" in page.headers.get("content-security-policy", "")

    # A webview reload/prefetch must still load while the ticket is alive.
    replay = client.get(f"/?embed={ticket}")
    assert "x-frame-options" not in replay.headers

    normal = client.get("/")
    assert normal.headers["x-frame-options"] == "DENY"

    unknown = client.get("/?embed=not-a-ticket")
    assert unknown.headers["x-frame-options"] == "DENY"


def test_the_link_endpoint_rejects_an_unknown_launch_mode(client, project):
    r = client.post(f"/api/projects/{project['slug']}/vscode/link",
                    json={"launch_mode": "emacs"})
    assert r.status_code == 400
    assert "launch_mode" in r.json()["detail"]
