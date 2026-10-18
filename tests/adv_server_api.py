"""Aggressive HTTP API tests for ``perun/server.py`` (FastAPI + SSE).

These tests deliberately probe hostile input: missing slugs, malformed bodies,
wrong types, oversized payloads, path traversal, oversized params and client
disconnects. We only assert *correct* behaviour (4xx, never 5xx, never escape
the sandbox); failing tests are findings, not test bugs.

Rules: never start real opencode/Telegram processes.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from perun import server


# --------------------------------------------------------------------------- #
# Raw ASGI helper for SSE.
#
# ``TestClient.stream`` (Starlette 1.8 / httpx 0.28 in this env) blocks forever
# on an open-ended ``text/event-stream`` response, so we drive the ASGI app
# directly: connect, read the response start, then send ``http.disconnect`` and
# wait for the generator to finish. This exercises the real middleware stack.
# --------------------------------------------------------------------------- #
async def _asgi_stream_once(path: str):
    body_q: asyncio.Queue = asyncio.Queue()
    await body_q.put({"type": "http.request", "body": b"", "more_body": False})
    events: list[dict] = []
    started = asyncio.Event()

    async def receive():
        return await body_q.get()

    async def send(msg):
        events.append(msg)
        if msg["type"] == "http.response.start":
            started.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8760")],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8760),
    }
    task = asyncio.create_task(server.app(scope, receive, send))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
    except asyncio.TimeoutError:
        task.cancel()
        raise AssertionError(f"SSE {path}: no response start within 5s")
    status = next(e["status"] for e in events if e["type"] == "http.response.start")
    headers = dict(
        (k.decode().lower(), v.decode())
        for k, v in next(e for e in events
                         if e["type"] == "http.response.start")["headers"])
    await asyncio.sleep(0.2)          # let the first chunk flush
    await body_q.put({"type": "http.disconnect"})
    try:
        await asyncio.wait_for(task, timeout=6)   # generator must unwind
    except asyncio.TimeoutError:
        task.cancel()
        raise AssertionError(f"SSE {path}: generator did not stop after disconnect")
    return status, headers


@pytest.fixture()
def client():
    server.SETTINGS.auto_discover_on_start = False
    # ``raise_server_exceptions=False`` makes unhandled endpoint exceptions show
    # up as a real HTTP 500 response instead of propagating out of TestClient.
    # That is exactly what the hostile-input tests assert against: a bad body or
    # path must be answered with 4xx, never by crashing the request handler.
    with TestClient(server.app, base_url="http://127.0.0.1:8760",
                    raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def project(client, tmp_path):
    """A registered project for endpoints that require an existing slug."""
    target = tmp_path / "advproj"
    target.mkdir()
    r = client.post("/api/projects", json={
        "method": "new", "name": "AdvProj", "path": str(target), "bootstrap": False,
    })
    assert r.status_code == 200, r.text
    return r.json()["project"]["slug"], target


BOGUS = "no-such-slug-xyz-123"

_SLUG_GETS = [
    "/api/projects/{s}",
    "/api/projects/{s}/sessions",
    "/api/projects/{s}/activity",
    "/api/projects/{s}/memory",
    "/api/projects/{s}/instructions",
    "/api/projects/{s}/instruction-files",
    "/api/projects/{s}/manifest",
    "/api/projects/{s}/git",
    "/api/projects/{s}/git/log",
    "/api/projects/{s}/tasks",
]


# --------------------------------------------------------------------------- #
# (a) unknown slug / id  ->  404, never 500
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tmpl", _SLUG_GETS)
def test_get_unknown_slug_is_404(client, tmpl):
    r = client.get(tmpl.format(s=BOGUS))
    assert r.status_code == 404, f"{r.status_code}: {r.text[:200]}"


def test_get_unknown_slug_instruction_file_404(client):
    r = client.get(f"/api/projects/{BOGUS}/instruction-files/x.md")
    assert r.status_code == 404, r.text[:200]


def test_get_unknown_run_uid_404(client):
    r = client.get("/api/runs/deadbeefdeadbeef")
    assert r.status_code == 404, r.text[:200]


def test_delete_unknown_slug_404(client):
    assert client.delete(f"/api/projects/{BOGUS}").status_code == 404


def test_patch_unknown_slug_404(client):
    r = client.patch(f"/api/projects/{BOGUS}", json={"description": "x"})
    assert r.status_code == 404


def test_unknown_run_uid_stream_404():
    """Correct behaviour: streaming an unknown run id should 404, not open a
    never-ending SSE stream. (Endpoint never validates run_uid.)"""
    status, _ = asyncio.run(_asgi_stream_once("/api/runs/deadbeefdeadbeef/stream"))
    assert status == 404, f"unknown run stream returned {status} instead of 404"


# --------------------------------------------------------------------------- #
# (b) malformed / empty / wrong-type bodies  ->  4xx
# --------------------------------------------------------------------------- #
def test_post_projects_empty_body_4xx(client):
    r = client.post("/api/projects", json={})
    assert 400 <= r.status_code < 500, r.text[:200]


def test_post_projects_invalid_json_4xx(client):
    r = client.post("/api/projects", content="this is not json",
                    headers={"content-type": "application/json"})
    assert 400 <= r.status_code < 500, r.text[:200]


def test_post_projects_missing_path_400(client):
    r = client.post("/api/projects", json={"name": "NoPath"})
    assert r.status_code == 400


def test_post_projects_wrong_type_path_4xx(client):
    """Correct behaviour: reject non-string path with 4xx, not crash with 500."""
    r = client.post("/api/projects", json={"name": "X", "path": 12345})
    assert 400 <= r.status_code < 500, r.text[:200]


def test_post_remember_empty_body_400(client):
    assert client.post("/api/memory/remember", json={}).status_code == 400


def test_post_remember_wrong_type_content_4xx(client):
    """content should be a string; an int must not blow up with 500."""
    r = client.post("/api/memory/remember", json={"content": 12345})
    assert 400 <= r.status_code < 500, r.text[:200]


def test_post_remember_invalid_json_4xx(client):
    r = client.post("/api/memory/remember", content="{bad",
                    headers={"content-type": "application/json"})
    assert 400 <= r.status_code < 500


def test_post_memory_search_empty_400(client):
    assert client.post("/api/memory/search", json={}).status_code == 400


def test_post_memory_search_bad_k_4xx(client):
    r = client.post("/api/memory/search", json={"query": "x", "k": "not-an-int"})
    assert 400 <= r.status_code < 500, r.text[:200]


def test_post_fs_mkdir_empty_400(client):
    assert client.post("/api/fs/mkdir", json={}).status_code == 400


def test_post_fs_mkdir_wrong_type_path_4xx(client):
    """path must be a string; an int must be rejected, not crash the server."""
    r = client.post("/api/fs/mkdir", json={"path": 987654})
    assert 400 <= r.status_code < 500, r.text[:200]


def test_post_prune_wrong_type_keep_4xx(client):
    r = client.post("/api/memory/prune", json={"keep": "abc"})
    assert 400 <= r.status_code < 500, r.text[:200]


def test_post_grep_missing_pattern_400(client):
    assert client.post("/api/search/grep", json={}).status_code == 400


def test_post_grep_unknown_project_404(client):
    r = client.post("/api/search/grep", json={"pattern": "x", "project": BOGUS})
    assert r.status_code == 404


def test_post_grep_include_wrong_type_4xx(client, project):
    slug, _ = project
    r = client.post("/api/search/grep",
                    json={"pattern": "x", "project": slug, "include": ["*.py"]})
    assert 400 <= r.status_code < 500, r.text[:200]


def test_post_huge_body_4xx(client):
    """A 5 MB body must be rejected with 4xx, never a 500."""
    payload = {"name": "x" * (5 * 1024 * 1024)}
    r = client.post("/api/projects", content=json.dumps(payload),
                    headers={"content-type": "application/json"})
    assert 400 <= r.status_code < 500, f"status={r.status_code}"


def test_post_telegram_unlock_empty_4xx(client):
    r = client.post("/api/telegram/unlock", json={})
    assert 400 <= r.status_code < 500, r.text[:200]


# --------------------------------------------------------------------------- #
# (c) hostile path / query params: traversal, NUL, huge, unicode
# --------------------------------------------------------------------------- #
def test_instruction_file_traversal_etc_passwd(client, project):
    """Filename traversal must not let us read /etc/passwd."""
    slug, _ = project
    for name in ("../../../../etc/passwd", "..%2f..%2f..%2fetc%2fpasswd",
                 "....//....//etc/passwd", "..\\..\\..\\etc\\passwd"):
        r = client.get(f"/api/projects/{slug}/instruction-files/{name}")
        assert r.status_code != 200, f"leaked via {name!r}"
        assert "root:" not in r.text, f"leaked via {name!r}"


def test_instruction_file_ssh_key_not_readable(client, project):
    slug, _ = project
    r = client.get(
        f"/api/projects/{slug}/instruction-files/%2e%2e%2f%2e%2e%2f.ssh%2fid_rsa")
    assert r.status_code != 200
    assert "PRIVATE KEY" not in r.text


def test_global_instruction_traversal(client):
    for name in ("../../../../etc/passwd", "..%2f..%2f..%2fetc%2fpasswd"):
        r = client.get(f"/api/instructions/{name}")
        assert r.status_code != 200, f"leaked via {name!r}"
        assert "root:" not in r.text


def test_filename_with_nul_byte_4xx(client, project):
    slug, _ = project
    r = client.get(f"/api/projects/{slug}/instruction-files/bad%00name.md")
    assert 400 <= r.status_code < 500, f"status={r.status_code} body={r.text[:120]}"


def test_filename_very_long_4xx(client, project):
    slug, _ = project
    r = client.get(f"/api/projects/{slug}/instruction-files/{'a' * 10000}.md")
    assert 400 <= r.status_code < 500, f"status={r.status_code}"


def test_filename_unicode_4xx(client, project):
    """A unicode filename that does not exist should 404, not 500."""
    slug, _ = project
    r = client.get(f"/api/projects/{slug}/instruction-files/zażółć-日本語-🚀.md")
    assert r.status_code in (400, 404), r.text[:120]


def test_fs_list_nonexistent_path_400(client):
    r = client.get("/api/fs/list", params={"path": "/no/such/dir/xyz123"})
    assert r.status_code == 400


def test_fs_list_traversal_stays_sane(client):
    """Listing a path with ../ is allowed (file browser) but must not 500."""
    r = client.get("/api/fs/list", params={"path": "../../../../etc"})
    assert r.status_code < 500, r.text[:120]


def test_memory_recent_bad_limit_4xx(client):
    r = client.get("/api/memory/recent", params={"limit": "abc"})
    assert 400 <= r.status_code < 500, r.text[:120]


def test_memory_stats_unknown_project_ok(client):
    """Unknown project slug on stats/recent must not 500 (falls back to global)."""
    assert client.get("/api/memory/stats", params={"project": BOGUS}).status_code < 500
    assert client.get("/api/memory/recent", params={"project": BOGUS}).status_code < 500


# --------------------------------------------------------------------------- #
# (d) headers: CORS / content type / security
# --------------------------------------------------------------------------- #
def test_no_permissive_cors_for_evil_origin(client):
    """OPTIONS preflight from an untrusted origin must not grant CORS access."""
    r = client.options("/api/projects",
                       headers={"Origin": "http://evil.example",
                                "Access-Control-Request-Method": "GET"})
    allow = r.headers.get("access-control-allow-origin", "")
    assert allow not in ("*", "http://evil.example"), (
        f"permissive CORS: {allow!r} status={r.status_code}")


def test_cross_origin_get_blocked(client):
    """GET with a cross-origin Origin header must not be served with CORS allow."""
    r = client.get("/api/health", headers={"Origin": "http://evil.example"})
    allow = r.headers.get("access-control-allow-origin", "")
    assert allow not in ("*", "http://evil.example"), f"allow={allow!r}"


def test_api_responses_are_json(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


def test_404_body_is_json(client):
    r = client.get(f"/api/projects/{BOGUS}")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"


def test_unexpected_host_blocked(client):
    """A non-loopback Host header must be refused (anti DNS-rebinding)."""
    r = client.get("/api/health", headers={"Host": "evil.example.com"})
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# (e) SSE: subscribe/unsubscribe must not leak subscribers
# --------------------------------------------------------------------------- #
def _global_subs():
    return len(getattr(server.ORCH.bus, "_global_subs", []))


def test_sse_content_type_and_headers():
    status, headers = asyncio.run(_asgi_stream_once("/api/stream"))
    assert status == 200
    assert headers.get("content-type", "").startswith("text/event-stream")


def test_sse_subscribers_return_to_zero():
    """20 connect/disconnect cycles must leave no global subscribers behind."""

    async def cycles():
        baseline = _global_subs()
        for _ in range(20):
            await _asgi_stream_once("/api/stream")
        return baseline, _global_subs()

    baseline, after = asyncio.run(cycles())
    assert after == baseline, f"subscriber leak: baseline={baseline} after={after}"


# --------------------------------------------------------------------------- #
# (f) concurrency: 30 parallel list GETs must not hit sqlite "database is locked"
# --------------------------------------------------------------------------- #
def test_concurrent_list_gets_no_sqlite_lock(client):
    urls = ["/api/projects", "/api/activity", "/api/memory/recent",
            "/api/health", "/api/settings"]
    errors: list[str] = []

    def hit(u: str):
        return u, client.get(u)

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as ex:
        futs = [ex.submit(hit, urls[i % len(urls)]) for i in range(30)]
        for fut in concurrent.futures.as_completed(futs):
            u, r = fut.result()
            if r.status_code >= 500:
                errors.append(f"{u} -> {r.status_code}: {r.text[:120]}")
            elif "database is locked" in r.text.lower():
                errors.append(f"{u} -> locked")

    assert not errors, "concurrent failures: " + "; ".join(errors[:5])


def test_concurrent_creates_no_lock(client, tmp_path):
    """20 parallel project creations must not raise sqlite lock errors."""
    errors: list[str] = []

    def create(i: int):
        return client.post("/api/projects", json={
            "name": f"P{i}", "path": str(tmp_path / f"p{i}"), "bootstrap": False,
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        for fut in concurrent.futures.as_completed(
                [ex.submit(create, i) for i in range(20)]):
            r = fut.result()
            if r.status_code >= 500 or "database is locked" in r.text.lower():
                errors.append(f"{r.status_code}: {r.text[:120]}")

    assert not errors, "concurrent create failures: " + "; ".join(errors[:5])
