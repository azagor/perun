"""The post-execution docs step: the real /update-docs command, and its fallback."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from perun import docs_sync, manifest, registry


def _project(tmp_path, name="docs-proj"):
    (tmp_path / name).mkdir(parents=True)
    project = registry.add_project(name, str(tmp_path / name))
    project["path"] = Path(project["path"])
    (project["path"] / "api.py").write_text("x = 1\n", encoding="utf-8")
    return project


def _proposal(project, proposal_id="prop_docs_1", intent="add validation",
              targets=None):
    return {
        "id": proposal_id,
        "project_id": project["id"],
        "status": "executed",
        "intent": intent,
        "targets": targets or [str(project["path"] / "api.py")],
    }


class FakeAPI:
    def __init__(self, commands=("update-docs",), fail_command=False):
        self.commands_list = [{"name": n} for n in commands]
        self.fail_command = fail_command
        self.sessions: list[dict] = []
        self.commands_run: list[tuple[str, str]] = []

    def commands(self, directory):
        return list(self.commands_list)

    def create_session(self, directory, *, model="", agent="", title=""):
        self.sessions.append({"directory": directory, "title": title})
        return {"id": f"ses_docs_{len(self.sessions)}"}

    def run_command(self, session_id, name, text=""):
        if self.fail_command:
            raise RuntimeError("service refused the command")
        self.commands_run.append((session_id, name))
        return {"ok": True}


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(docs_sync, "enabled", lambda settings=None: True)


@pytest.fixture(autouse=True)
def _fresh_state():
    """The handled-proposal set is process-wide; tests must not inherit it."""
    docs_sync.reset_state()
    yield
    docs_sync.reset_state()


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_docs_step_runs_the_installed_command(tmp_path, enabled):
    project = _project(tmp_path)
    api = FakeAPI()

    assert docs_sync.after_execution(project, _proposal(project), api=api) is True
    assert _wait(lambda: api.commands_run)
    assert api.commands_run == [("ses_docs_1", "update-docs")]
    assert api.sessions[0]["directory"] == str(project["path"])
    assert _wait(lambda: registry.activity_exists(
        "docs_updated", "/update-docs started for proposal prop_docs_1"))


def test_a_missing_command_falls_back_to_a_changelog_line(tmp_path, enabled):
    project = _project(tmp_path)
    api = FakeAPI(commands=())

    assert docs_sync.after_execution(project, _proposal(project), api=api) is True
    changelog = docs_sync.changelog_path(project)
    assert _wait(changelog.is_file)
    text = changelog.read_text(encoding="utf-8")
    assert "## Unreleased" in text
    assert "add validation" in text
    assert "prop_docs_1" in text
    assert api.sessions == []


def test_a_failing_command_still_records_the_change(tmp_path, enabled):
    project = _project(tmp_path)
    api = FakeAPI(fail_command=True)

    docs_sync.after_execution(project, _proposal(project), api=api)
    changelog = docs_sync.changelog_path(project)
    assert _wait(changelog.is_file)
    assert "prop_docs_1" in changelog.read_text(encoding="utf-8")


def test_the_step_is_off_for_an_unexecuted_proposal_or_disabled_setting(tmp_path):
    project = _project(tmp_path)
    failed = dict(_proposal(project), status="failed")
    assert docs_sync.after_execution(project, failed, api=FakeAPI()) is False

    executed = _proposal(project, proposal_id="prop_docs_off")
    assert docs_sync.after_execution(project, executed, api=FakeAPI()) is False


def test_only_one_docs_run_per_project_at_a_time(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(docs_sync, "enabled", lambda settings=None: True)
    started = threading.Event()
    release = threading.Event()

    class SlowAPI(FakeAPI):
        def create_session(self, directory, **kwargs):
            started.set()
            assert release.wait(5)
            return super().create_session(directory, **kwargs)

    api = SlowAPI()
    assert docs_sync.after_execution(project, _proposal(project), api=api) is True
    assert started.wait(5)
    # A second execution while the first is still running is skipped.
    second = _proposal(project, proposal_id="prop_docs_2")
    assert docs_sync.after_execution(project, second, api=api) is False
    release.set()
    assert _wait(lambda: (docs_sync._running == set()))


def test_the_changelog_entry_is_idempotent_and_preserves_entries(tmp_path):
    project = _project(tmp_path)
    path = docs_sync.changelog_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# demo - Changelog\n\nAll notable changes are recorded here.\n\n"
        "## Unreleased\n\n- an older entry\n\n## v0.1.0\n\n- shipped\n",
        encoding="utf-8",
    )
    proposal = _proposal(project)

    assert docs_sync.append_changelog(project, proposal) == path
    text = path.read_text(encoding="utf-8")
    assert "- an older entry" in text
    assert "- shipped" in text
    assert text.index("an older entry") < text.index("prop_docs_1")
    assert text.index("prop_docs_1") < text.index("## v0.1.0")

    # Same proposal again: nothing is added twice.
    assert docs_sync.append_changelog(project, proposal) is None
    assert text.count("prop_docs_1") == 1


def test_the_changelog_path_follows_the_manifest(tmp_path):
    project = _project(tmp_path)
    data = manifest.from_project(project)
    data["docs"] = {"changelog": "CHANGELOG.md"}
    manifest.save(project["path"], data, body="# demo")

    assert docs_sync.changelog_path(project) == project["path"] / "CHANGELOG.md"
