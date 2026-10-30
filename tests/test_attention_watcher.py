"""The watcher turns OpenCode's pending requests into inbox items, exactly once."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from perun import attention, attention_watcher, config, registry


class FakeAPI:
    def __init__(self):
        self.permissions: dict[str, list[dict]] = {}
        self.forms: dict[str, list[dict]] = {}
        self.active: dict[str, dict] = {}
        self.locations_list: list[str] = []
        self.sessions: dict[str, dict] = {}

    def session(self, session_id):
        return self.sessions.get(session_id, {})

    def active_sessions(self):
        return dict(self.active)

    def locations(self):
        return list(self.locations_list)

    def pending_permissions(self, location):
        return list(self.permissions.get(location, []))

    def pending_forms(self, location):
        return list(self.forms.get(location, []))

    def close(self):
        pass


def _settings(**overrides):
    base = dict(
        attention_enabled=True, attention_shadow=False, attention_projects=[],
        attention_operator_names=[], attention_ttl_minutes=240,
        attention_poll_seconds=5, attention_replay_open_minutes=15,
        attention_max_per_hour=6, attention_quiet_hours="",
        attention_show_paths=False, attention_plain_text_confirm=True,
        attention_default_delivery="queue",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture()
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings()))
    attention_watcher.reset_cache()
    yield fake
    attention_watcher.reset_cache()


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "watch-proj").mkdir()
    return registry.add_project("watch-proj", str(tmp_path / "watch-proj"), kind="dir")


def _permission(project_id, session_id="ses_watch", request_id="per_1"):
    return {
        "id": request_id,
        "sessionID": session_id,
        "action": "read",
        "resources": [".env"],
        "save": ["*"],
        "source": {"type": "tool", "messageID": "msg_parent"},
    }


def test_permission_creates_one_item_and_does_not_duplicate(api, project):
    api.active = {"ses_watch": {"type": "running"}}
    api.sessions = {"ses_watch": {
        "id": "ses_watch", "title": "Fix totals",
        "location": {"directory": project["path"]},
    }}
    api.permissions = {project["path"]: [_permission(project["id"])]}

    first = attention_watcher.poll_once(api)
    second = attention_watcher.poll_once(api)

    assert first["created"] == 1
    assert second["created"] == 0
    items = registry.open_attention()
    assert len(items) == 1
    assert items[0]["target_kind"] == "permission"
    assert items[0]["ref"]["session_title"] == "Fix totals"
    assert items[0]["ref"]["save"] == ["*"]
    assert attention.render(items[0]).startswith("🔐")


def test_a_request_answered_in_the_tui_closes_its_item(api, project, monkeypatch):
    api.active = {"ses_watch": {"type": "running"}}
    api.sessions = {"ses_watch": {"location": {"directory": project["path"]}}}
    api.permissions = {project["path"]: [_permission(project["id"])]}
    created = attention_watcher.poll_once(api)["created"]
    assert created == 1

    api.permissions = {project["path"]: []}
    result = attention_watcher.poll_once(api)

    assert result["closed"] == 1
    item = registry.open_attention(state="cancelled")[0]
    assert item["answer"] == "resolved outside Perun"


def test_forms_become_items_with_their_field_list(api, project):
    api.active = {"ses_form": {"type": "running"}}
    api.sessions = {"ses_form": {"location": {"directory": project["path"]}}}
    api.forms = {project["path"]: [{
        "id": "frm_1", "sessionID": "ses_form", "title": "Deploy?",
        "fields": [{"key": "env", "type": "string", "required": True}],
    }]}

    assert attention_watcher.poll_once(api)["created"] == 1

    item = registry.open_attention()[0]
    assert item["target_kind"] == "form"
    assert item["ref"]["form_id"] == "frm_1"
    assert item["ref"]["fields"][0]["key"] == "env"


def test_only_active_locations_are_polled_until_the_sweep(api, project, tmp_path):
    other = tmp_path / "other-dir"
    other.mkdir()
    registry.add_project("other-dir", str(other), kind="dir")
    third = tmp_path / "third-dir"
    third.mkdir()
    registry.add_project("third-dir", str(third), kind="dir")
    api.active = {"ses_watch": {"type": "running"}}
    api.sessions = {"ses_watch": {"location": {"directory": project["path"]}}}
    api.locations_list = [str(other)]
    api.permissions = {str(other): [_permission(project["id"], request_id="per_other")]}

    # A fresh process sweeps once, so the other loaded location is picked up.
    attention_watcher.poll_once(api)
    assert [i["target_id"] for i in registry.open_attention()] == ["per_other"]

    # A new request at a location that is neither active nor freshly swept
    # waits for the next five-minute sweep instead of being polled every tick.
    api.locations_list = [str(other), str(third)]
    api.permissions[str(third)] = [_permission(project["id"], request_id="per_third")]
    attention_watcher.poll_once(api)
    assert [i["target_id"] for i in registry.open_attention()] == ["per_other"]

    attention_watcher._last_sweep = 0.0
    attention_watcher.poll_once(api)
    assert sorted(i["target_id"] for i in registry.open_attention()) == [
        "per_other", "per_third"]


def test_disabled_attention_does_no_work(api, project, monkeypatch):
    monkeypatch.setattr(config.Settings, "load",
                        staticmethod(lambda: _settings(attention_enabled=False)))
    api.active = {"ses_watch": {"type": "running"}}
    api.sessions = {"ses_watch": {"location": {"directory": project["path"]}}}
    api.permissions = {project["path"]: [_permission(project["id"])]}

    assert attention_watcher.poll_once(api)["created"] == 0
    assert registry.open_attention() == []


def test_unknown_directories_are_ignored(api, tmp_path):
    api.active = {"ses_x": {"type": "running"}}
    api.sessions = {"ses_x": {"location": {"directory": str(tmp_path / "nowhere")}}}
    api.permissions = {str(tmp_path / "nowhere"): [_permission(1, "ses_x", "per_x")]}

    assert attention_watcher.poll_once(api)["created"] == 0
