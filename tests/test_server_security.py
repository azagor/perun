"""Anti-CSRF / anti-DNS-rebinding guard tests.

The dashboard is a local, unauthenticated API by design. These tests pin the
guard that keeps a *remote* web page from driving it: the ``Host`` header must
address this machine, and a browser-supplied ``Origin`` must resolve to an
allowed host.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as c:
        yield c


def test_loopback_host_allowed(client):
    assert client.get("/api/health").status_code == 200


def test_localhost_host_allowed(client):
    assert client.get("/api/health", headers={"host": "localhost:8760"}).status_code == 200


def test_foreign_host_blocked(client):
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 403


def test_dns_rebinding_post_blocked(client):
    r = client.post("/api/projects", json={"name": "X", "path": r"C:\x"},
                    headers={"host": "evil.example:8760"})
    assert r.status_code == 403


def test_cross_origin_post_blocked(client):
    r = client.post("/api/projects", json={"name": "X", "path": r"C:\x"},
                    headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_null_origin_blocked(client):
    r = client.post("/api/projects", json={"name": "X", "path": r"C:\x"},
                    headers={"origin": "null"})
    assert r.status_code == 403


def test_same_origin_post_allowed(client):
    r = client.post("/api/projects", json={"name": "OK", "path": r"C:\ok"},
                    headers={"origin": "http://127.0.0.1:8760"})
    assert r.status_code == 200


def test_dashboard_route_is_not_guarded(client):
    """Only /api/* is guarded — static assets keep working with any Host."""
    assert client.get("/", headers={"host": "whatever.example"}).status_code == 200


def test_wildcard_bind_allows_lan_host(monkeypatch):
    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    monkeypatch.setattr(server.SETTINGS, "host", "0.0.0.0")
    with TestClient(server.app, base_url="http://192.168.1.5:8760") as c:
        assert c.get("/api/health").status_code == 200


def test_exposed_bind_still_blocks_cross_origin(monkeypatch):
    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    monkeypatch.setattr(server.SETTINGS, "host", "0.0.0.0")
    with TestClient(server.app, base_url="http://192.168.1.5:8760") as c:
        r = c.post("/api/projects", json={"name": "X", "path": r"C:\x"},
                   headers={"origin": "http://evil.example"})
        assert r.status_code == 403
