"""HTTP API tests for the Coroutines tab."""

from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from perun import config, registry, server


@pytest.fixture()
def client():
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as c:
        yield c


@pytest.fixture()
def headers():
    return {"X-Perun-Token": config.api_token()}


@pytest.fixture()
def project(tmp_path):
    path = tmp_path / "api-project"
    path.mkdir()
    return registry.add_project("api-project", str(path), kind="dir")


def _body(project, **overrides):
    body = {
        "name": "API daily",
        "scope": "project",
        "execution_project_id": project["id"],
        "trigger_type": "cron",
        "trigger_expr": "0 12 * * *",
        "timezone": "Europe/Warsaw",
        "prompt": "Do it",
        "execution_mode": "single",
    }
    body.update(overrides)
    return body


def test_status_and_empty_list(client):
    r = client.get("/api/coroutines/status")
    assert r.status_code == 200
    assert "counts" in r.json()
    r = client.get("/api/coroutines")
    assert r.status_code == 200
    assert r.json()["coroutines"] == []


def test_create_list_detail_pause_resume_delete(client, headers, project):
    r = client.post("/api/coroutines", json=_body(project), headers=headers)
    assert r.status_code == 200, r.text
    job = r.json()["coroutine"]
    assert job["status"] == "paused"
    assert job["next_fires"]
    uid = job["uid"]

    r = client.get("/api/coroutines")
    assert len(r.json()["coroutines"]) == 1
    r = client.get(f"/api/coroutines/{uid}")
    assert r.status_code == 200
    assert len(r.json()["coroutine"]["next_fires"]) == 5

    r = client.post(f"/api/coroutines/{uid}/pause", headers=headers)
    assert r.json()["coroutine"]["status"] == "paused"
    r = client.post(f"/api/coroutines/{uid}/resume", headers=headers)
    assert r.json()["coroutine"]["status"] == "active"
    r = client.post(f"/api/coroutines/{uid}/stop", headers=headers)
    assert r.json()["coroutine"]["status"] == "stopped"

    r = client.delete(f"/api/coroutines/{uid}", headers=headers)
    assert r.status_code == 200 and r.json()["deleted"] is True
    assert client.get(f"/api/coroutines/{uid}").status_code == 404


def test_create_activate_and_duplicate(client, headers, project):
    r = client.post("/api/coroutines",
                    json=_body(project, activate=True), headers=headers)
    job = r.json()["coroutine"]
    assert job["status"] == "active"
    r = client.post(f"/api/coroutines/{job['uid']}/duplicate", headers=headers)
    assert r.status_code == 200
    copy = r.json()["coroutine"]
    assert copy["uid"] != job["uid"]
    assert copy["status"] == "paused"


def test_global_job_is_created_on_the_control_plane(client, headers, project):
    r = client.post("/api/coroutines", headers=headers, json=_body(
        project, name="global reminder", scope="global",
        execution_project_id=project["id"],
    ))
    assert r.status_code == 200, r.text
    job = r.json()["coroutine"]
    assert job["project_id"] is None
    assert job["execution_project_name"] == "Orchestrator (control plane)"
    client.delete(f"/api/coroutines/{job['uid']}", headers=headers)


def test_validation_error_is_structured(client, headers, project):
    r = client.post("/api/coroutines",
                    json=_body(project, trigger_type="cron",
                               trigger_expr="not a cron"), headers=headers)
    assert r.status_code == 400
    assert "errors" in r.json()["detail"]


def test_preview(client, headers):
    r = client.post("/api/coroutines/preview",
                    json={"trigger_type": "cron", "trigger_expr": "0 12 * * *",
                          "timezone": "Europe/Warsaw"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["valid"] is True
    assert len(r.json()["next_fires"]) == 5


def test_patch_can_activate_in_the_same_request(client, headers, project):
    r = client.post("/api/coroutines", json=_body(project), headers=headers)
    job = r.json()["coroutine"]
    assert job["status"] == "paused"
    r = client.patch(f"/api/coroutines/{job['uid']}", headers=headers,
                     json={"prompt": "Updated prompt", "activate": True})
    assert r.status_code == 200, r.text
    updated = r.json()["coroutine"]
    assert updated["status"] == "active"
    assert updated["prompt"] == "Updated prompt"


def test_webhook_secret_and_ingress(client, headers, project):
    r = client.post("/api/coroutines", headers=headers, json=_body(
        project, name="hook", trigger_type="event", event_source="webhook",
        trigger_expr="", prompt="Handle it",
    ))
    job = r.json()["coroutine"]
    url = job["webhook_url"]
    secret = job["webhook_secret"]
    assert url and secret and job["webhook_configured"] is True
    assert secret not in url  # the URL is safe to log

    r = client.post(url, json={"hello": "world"},
                    headers={"X-Perun-Webhook-Secret": secret})
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    # A wrong/missing secret is rejected.
    assert client.post(url, json={},
                       headers={"X-Perun-Webhook-Secret": "wrong"}).status_code == 401
    assert client.post(url, json={}).status_code == 401

    r = client.post(f"/api/coroutines/{job['uid']}/rotate-secret", headers=headers)
    assert r.status_code == 200
    assert r.json()["webhook_url"] and r.json()["webhook_secret"]


def test_webhook_rejects_an_unexpected_host(client, headers, project):
    r = client.post("/api/coroutines", headers=headers, json=_body(
        project, name="hook-host", trigger_type="event",
        event_source="webhook", trigger_expr="", prompt="Handle it",
    ))
    job = r.json()["coroutine"]
    r = client.post(job["webhook_url"], json={}, headers={
        "X-Perun-Webhook-Secret": job["webhook_secret"],
        "Host": "evil.example",
    })
    assert r.status_code == 403


def test_test_event_and_events_inbox(client, headers, project):
    r = client.post("/api/coroutines", headers=headers, json=_body(
        project, name="room", trigger_type="event",
        event_source="room.message", event_room_id="grp:test",
        trigger_expr="", prompt="Reply",
    ))
    job = r.json()["coroutine"]
    r = client.post(f"/api/coroutines/{job['uid']}/test-event",
                    json={}, headers=headers)
    assert r.status_code == 200
    # The test job is paused, so a manual tick records a skipped delivery and
    # never starts a real agent run.
    server.COROUTINES.tick()
    r = client.get(f"/api/coroutines/{job['uid']}/events")
    assert r.status_code == 200
    assert r.json()["events"]


def test_global_event_jobs_are_rejected_on_the_api(client, headers, project):
    r = client.post("/api/coroutines", headers=headers, json=_body(
        project, scope="global", trigger_type="event",
        event_source="webhook", trigger_expr="", prompt="Handle it",
    ))
    assert r.status_code == 400
    assert "trigger_type" in r.json()["detail"]["errors"]


def test_a_one_shot_reminder_can_be_created(client, headers, project, control_repo):
    when = int(time.time()) + 3600
    r = client.post("/api/coroutines", headers=headers, json=_body(
        project, name="one-shot reminder", scope="global",
        trigger_type="once", trigger_expr="", run_at_epoch=when,
        prompt="Remind me",
    ))
    assert r.status_code == 200, r.text
    job = r.json()["coroutine"]
    assert job["trigger_type"] == "once"
    assert job["run_at_epoch"] == when
    assert job["execution_project_name"] == "Orchestrator (control plane)"


def test_unlimited_loop_requires_caps_on_the_api(client, headers, project):
    r = client.post("/api/coroutines", headers=headers, json=_body(
        project, execution_mode="loop", max_iterations=0,
    ))
    assert r.status_code == 400
    assert "max_runs_per_day" in r.json()["detail"]["errors"]
