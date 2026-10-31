"""OpenCode transport: auth, location scoping, idempotent prompt, degradation."""

from __future__ import annotations

import base64
import json
import subprocess

import pytest

from perun import opencode_api


class StubResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.content = b"" if payload is None else json.dumps(payload).encode()
        self.text = text or (self.content.decode() if self.content else "")

    def json(self):
        return self._payload


class StubClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, headers=None):  # noqa: A002
        self.calls.append({"method": method, "url": url, "json": json,
                           "headers": headers or {}})
        return self.responses.pop(0)

    def close(self):
        pass


def _api(responses, *, password="pw"):
    api = opencode_api.OpenCodeAPI(
        base_url="http://127.0.0.1:9999", password=password,
        discovery=lambda: "http://127.0.0.1:9999",
    )
    api._client = StubClient(responses)
    return api


def test_service_password_prefers_the_environment(monkeypatch):
    monkeypatch.setenv("OPENCODE_SERVICE_PASSWORD", "from-env")
    assert opencode_api._service_password() == "from-env"


def test_service_password_reads_the_service_file(monkeypatch, tmp_path):
    service = tmp_path / "service.json"
    service.write_text('{"password": "file-pw"}', encoding="utf-8")
    monkeypatch.delenv("OPENCODE_SERVICE_PASSWORD", raising=False)
    monkeypatch.setattr(opencode_api, "SERVICE_FILE", service)
    assert opencode_api._service_password() == "file-pw"


def test_discovery_parses_the_cli_output(monkeypatch):
    class Proc:
        returncode = 0
        stdout = '{"urls": ["http://127.0.0.1:28894"], "version": "2.2.0"}'
        stderr = ""

    monkeypatch.setattr(opencode_api, "_opencode_argv", lambda *a: ["opencode", *a])
    monkeypatch.setattr(opencode_api.subprocess, "run",
                        lambda *a, **k: Proc())
    assert opencode_api.discover_base_url() == "http://127.0.0.1:28894"


def test_discovery_returns_empty_without_the_cli(monkeypatch):
    monkeypatch.setattr(opencode_api, "_opencode_argv", lambda *a: [])
    assert opencode_api.discover_base_url() == ""


def test_requests_use_basic_auth_and_encode_the_location():
    api = _api([StubResponse(payload={"data": []})])

    api.pending_permissions(r"C:\my apps\perun")

    call = api._client.calls[0]
    expected = "Basic " + base64.b64encode(b"opencode:pw").decode()
    assert call["headers"]["Authorization"] == expected
    assert "location%5Bdirectory%5D=C%3A%5Cmy%20apps%5Cperun" in call["url"]


def test_prompt_carries_the_idempotency_key_and_delivery():
    api = _api([StubResponse(payload={"data": {"id": "msg_att_1", "delivery": "queue"}})])

    result = api.prompt("ses_1", "hello", delivery="steer", message_id="msg_att_7")

    assert api._client.calls[0]["json"] == {
        "text": "hello", "delivery": "steer", "resume": True, "id": "msg_att_7",
    }
    assert result["id"] == "msg_att_1"


def test_prompt_coerces_an_unknown_delivery_mode():
    api = _api([StubResponse(payload={"data": {"id": "msg_1"}})])

    api.prompt("ses_1", "hello", delivery="explode")

    assert api._client.calls[0]["json"]["delivery"] == "queue"


def test_a_401_reloads_the_password_once(monkeypatch):
    monkeypatch.setattr(opencode_api, "_service_password", lambda: "rotated")
    api = _api([StubResponse(status=401, text="unauthorized"),
                StubResponse(payload={"data": {}})])

    assert api.active_sessions() == {}


def test_404_is_optional_and_becomes_empty(monkeypatch):
    api = _api([StubResponse(status=404, text="gone")])
    assert api.pending_permissions(r"C:\x") == []


def test_http_errors_surface_with_the_status():
    api = _api([StubResponse(status=400, text="bad request")])

    with pytest.raises(opencode_api.OpenCodeHTTPError) as info:
        api.active_sessions()
    assert info.value.status == 400


def test_an_unreachable_service_degrades(monkeypatch):
    class Boom:
        def request(self, *a, **k):
            raise OSError("connection refused")

        def close(self):
            pass

    api = opencode_api.OpenCodeAPI(base_url="http://127.0.0.1:1", password="pw",
                                   discovery=lambda: "http://127.0.0.1:1")
    api._client = Boom()

    with pytest.raises(opencode_api.OpenCodeUnavailable):
        api.active_sessions()


def test_an_empty_base_url_and_no_credential_is_unavailable(monkeypatch):
    monkeypatch.setattr(opencode_api, "discover_base_url", lambda: "")
    monkeypatch.setattr(opencode_api, "_service_password", lambda: "")
    api = opencode_api.OpenCodeAPI()

    with pytest.raises(opencode_api.OpenCodeUnavailable):
        api.locations()


class StreamResponse:
    status_code = 200

    def iter_lines(self):
        yield ""
        yield "data: {\"type\": \"session.idle\", \"data\": {\"sessionID\": \"ses_1\"}}"
        yield ": heartbeat"

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class StreamClient:
    def __init__(self):
        self.calls = []

    def stream(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return StreamResponse()

    def close(self):
        pass


def test_iter_events_decodes_sse_data_lines_and_filters_types():
    api = _api([])
    api._client = StreamClient()

    events = list(api.iter_events(types=["session.idle"]))

    assert [e["type"] for e in events] == ["session.idle"]
    call = api._client.calls[0]
    assert call["params"] == [("types", "session.idle")]
    assert "Authorization" in call["headers"]


def test_create_session_sends_location_model_and_agent():
    api = _api([StubResponse(payload={"data": {"id": "ses_new"}})])

    created = api.create_session(r"C:\proj", model="ollama/qwen3.5:latest",
                                 agent="build", title="hello")

    assert created["id"] == "ses_new"
    body = api._client.calls[0]["json"]
    assert body["location"] == {"directory": r"C:\proj"}
    assert body["model"] == {"providerID": "ollama", "id": "qwen3.5:latest"}
    assert body["agent"] == "build"
    assert body["title"] == "hello"


def test_event_hub_dispatches_by_session_and_isolates_a_failing_subscriber():
    import threading
    import time

    def source():
        yield {"type": "session.text.delta", "data": {"sessionID": "s1", "n": 1}}
        yield {"type": "session.text.delta", "data": {"sessionID": "s2", "n": 2}}
        time.sleep(0.2)

    seen = []
    hub = opencode_api.EventHub(source=source)
    hub.subscribe("s1", lambda event: seen.append(event["data"]["n"]))
    hub.subscribe("s1", lambda event: (_ for _ in ()).throw(RuntimeError("boom")))
    hub.start()
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    hub.stop()

    assert seen == [1]
