"""Event-driven ingestion: one poll per burst, polling as the fallback."""

from __future__ import annotations

import threading
import time

import pytest

from perun import attention_watcher, opencode_api


class FakeHub:
    """Stands in for ``EventHub`` without opening an SSE connection."""

    def __init__(self, subscribe_error: Exception | None = None):
        self.subscribe_error = subscribe_error
        self.handlers: dict[str, list] = {}
        self.started = 0
        self.unsubscribed: list[str] = []

    def subscribe(self, session_id, handler):
        if self.subscribe_error is not None:
            raise self.subscribe_error
        token = f"{session_id}:{len(self.handlers.get(session_id, []))}"
        self.handlers.setdefault(session_id, []).append(handler)
        return token

    def unsubscribe(self, token):
        self.unsubscribed.append(token)

    def start(self):
        self.started += 1

    def emit(self, event):
        for handler in list(self.handlers.get(opencode_api.EventHub.GLOBAL_SESSION, [])):
            handler(event)


@pytest.fixture()
def hub(monkeypatch):
    fake = FakeHub()
    monkeypatch.setattr(attention_watcher, "INGEST_DEBOUNCE_SECONDS", 0.05)
    polls: list[int] = []
    monkeypatch.setattr(attention_watcher, "poll_once",
                        lambda api=None: polls.append(1) or {"created": 0})
    assert attention_watcher.start_event_ingest(fake) is True
    fake.polls = polls
    yield fake
    attention_watcher.stop_event_ingest()


def _wait(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_ingest_subscribes_globally_and_starts_the_hub(hub):
    assert hub.started == 1
    assert opencode_api.EventHub.GLOBAL_SESSION in hub.handlers
    assert attention_watcher.event_ingest_active() is True


def test_stopping_unsubscribes_and_clears_the_worker(hub):
    attention_watcher.stop_event_ingest()
    assert attention_watcher.event_ingest_active() is False
    assert hub.unsubscribed


def test_a_burst_of_events_produces_one_poll(hub):
    for index in range(4):
        hub.emit({"type": "permission.asked",
                  "data": {"sessionID": f"ses_{index}", "id": f"per_{index}"}})
    assert _wait(lambda: len(hub.polls) == 1)
    # The debounce window closes the burst: still exactly one poll.
    time.sleep(0.3)
    assert len(hub.polls) == 1
    assert attention_watcher.ingest_stats()["events"] == 4


def test_unrelated_events_do_not_poll(hub):
    for event_type in ("message.created", "session.updated", "form.replied.ok"):
        hub.emit({"type": event_type, "data": {"sessionID": "ses_1"}})
    time.sleep(0.3)
    assert hub.polls == []


def test_form_events_are_ingested(hub):
    hub.emit({"type": "form.created", "data": {"sessionID": "ses_1"}})
    assert _wait(lambda: len(hub.polls) == 1)


def test_an_event_without_a_type_or_data_is_ignored(hub):
    hub.emit({})
    hub.emit({"type": None})
    hub.emit({"type": "permission.asked"})  # no data: the poll reads live state
    assert _wait(lambda: len(hub.polls) == 1)


def test_a_failing_subscription_returns_false_instead_of_raising(monkeypatch):
    broken = FakeHub(subscribe_error=RuntimeError("no service"))
    assert attention_watcher.start_event_ingest(broken) is False
    assert attention_watcher.event_ingest_active() is False


def test_starting_twice_keeps_one_worker(hub):
    assert attention_watcher.start_event_ingest(hub) is True
    assert hub.started == 1
    assert len(hub.handlers[opencode_api.EventHub.GLOBAL_SESSION]) == 1


def test_the_hub_delivers_every_session_to_a_global_subscriber():
    seen: list[dict] = []
    hub = opencode_api.EventHub(api=object(), source=lambda: iter(()))
    token = hub.subscribe(opencode_api.EventHub.GLOBAL_SESSION, seen.append)
    hub._dispatch({"type": "permission.asked", "data": {"sessionID": "ses_a"}})
    hub._dispatch({"type": "permission.asked", "data": {"sessionID": "ses_b"}})
    hub._dispatch({"type": "server.ready"})  # no session id at all
    assert len(seen) == 3
    hub.unsubscribe(token)
    hub._dispatch({"type": "permission.asked", "data": {"sessionID": "ses_c"}})
    assert len(seen) == 3


def test_a_session_subscriber_still_only_sees_its_own_session():
    seen: list[dict] = []
    hub = opencode_api.EventHub(api=object(), source=lambda: iter(()))
    hub.subscribe("ses_a", seen.append)
    hub._dispatch({"type": "message.created", "data": {"sessionID": "ses_b"}})
    assert seen == []
    hub._dispatch({"type": "message.created", "data": {"sessionID": "ses_a"}})
    assert len(seen) == 1


def test_a_failing_subscriber_does_not_starve_the_others():
    seen: list[dict] = []
    hub = opencode_api.EventHub(api=object(), source=lambda: iter(()))

    def boom(_event):
        raise RuntimeError("subscriber bug")

    hub.subscribe(opencode_api.EventHub.GLOBAL_SESSION, boom)
    hub.subscribe(opencode_api.EventHub.GLOBAL_SESSION, seen.append)
    hub._dispatch({"type": "permission.asked", "data": {"sessionID": "ses_a"}})
    assert len(seen) == 1


def test_poll_once_is_serialized(monkeypatch):
    """The event worker and the reconciliation loop never poll at once."""
    active = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def slow_poll(api=None):
        with guard:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        time.sleep(0.1)
        with guard:
            active["now"] -= 1
        return {"created": 0}

    monkeypatch.setattr(attention_watcher, "_poll_once_locked", slow_poll)
    threads = [threading.Thread(target=attention_watcher.poll_once) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert active["peak"] == 1
