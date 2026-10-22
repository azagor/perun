"""Tests for the Perun self-updater."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from perun import config, updater


def test_version_comparison():
    assert updater.parse_version("v0.7.21") == (0, 7, 21, 0, 1)
    assert updater.parse_version("0.8.0-rc1") == (0, 8, 0, 0, 0)
    assert updater.parse_version("1") == (1, 0, 0, 0, 1)
    assert updater.parse_version(None) == (0,)

    assert updater.is_newer("0.7.22", "0.7.21")
    assert updater.is_newer("0.8.0", "0.7.21")
    assert not updater.is_newer("0.7.21", "0.7.21")
    assert not updater.is_newer("0.7.20", "0.7.21")
    # release candidates sort below final releases
    assert updater.is_newer("0.8.0", "0.8.0-rc1")


class _ReleaseClient:
    def __init__(self, payload: dict[str, Any] | Exception):
        self.payload = payload
        self.got = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url: str, **kw):
        self.got.append(url)
        if isinstance(self.payload, Exception):
            raise self.payload
        return _Response(self.payload)

    def stream(self, method: str, url: str, **kw):
        raise RuntimeError("not used")


class _Response:
    def __init__(self, data: dict[str, Any], status: int = 200):
        self.data = data
        self.status_code = status
        self.headers = {}

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


def test_check_parses_release_and_detects_update(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_check_path", lambda: tmp_path / "check.json")
    release = {
        "tag_name": "v0.8.0",
        "name": "v0.8.0",
        "html_url": "https://github.com/azagor/perun/releases/tag/v0.8.0",
        "published_at": "2026-10-22T00:00:00Z",
        "body": "test release",
        "prerelease": False,
        "assets": [{"name": "PerunSetup.exe", "size": 1000,
                    "browser_download_url": "https://example.com/PerunSetup.exe",
                    "content_type": "application/x-msdos-program"}],
    }
    monkeypatch.setattr("httpx.Client", lambda *a, **k: _ReleaseClient(release))
    monkeypatch.setattr(updater, "__version__", "0.7.21")
    info = updater.check(refresh=True)
    assert info["ok"]
    assert info["latest"] == "0.8.0"
    assert info["update_available"]
    # cache is persisted
    assert (tmp_path / "check.json").exists()


def test_check_falls_back_to_cached_on_network_error(monkeypatch, tmp_path):
    check_path = tmp_path / "check.json"
    check_path.write_text(json.dumps({
        "ts": time.time() - 9999,
        "checked_at": "2026-01-01T00:00:00Z",
        "release": {"tag_name": "v0.9.0", "version": "0.9.0", "name": "v0.9.0", "html_url": "x",
                    "published_at": "2026-01-01T00:00:00Z", "body": "", "prerelease": False,
                    "assets": []},
    }))
    monkeypatch.setattr(updater, "_check_path", lambda: check_path)
    monkeypatch.setattr("httpx.Client", lambda *a, **k: _ReleaseClient(RuntimeError("offline")))
    monkeypatch.setattr(updater, "__version__", "0.7.21")
    info = updater.check(refresh=True)
    assert not info["ok"]
    assert info["stale"]
    assert info["latest"] == "0.9.0"
    assert info["update_available"]


def test_install_mode_detects_git_checkout(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr(config, "REPO_ROOT", str(repo))
    mode = updater.install_mode()
    assert mode["mode"] == "git"
    assert mode["updatable"]


def test_install_mode_detects_windows_install(monkeypatch, tmp_path):
    repo = tmp_path / "Perun"
    repo.mkdir()
    (repo / "Perun.cmd").write_text("@echo off\n")
    monkeypatch.setattr(config, "REPO_ROOT", str(repo))
    monkeypatch.setattr(os, "name", "nt")
    mode = updater.install_mode()
    assert mode["mode"] == "installer"
    assert mode["updatable"]


def test_apply_refuses_without_update(monkeypatch, tmp_path):
    release = {
        "tag_name": "v0.7.21",
        "name": "v0.7.21",
        "html_url": "x", "published_at": "2026-01-01T00:00:00Z", "body": "",
        "prerelease": False, "assets": [],
    }
    monkeypatch.setattr("httpx.Client", lambda *a, **k: _ReleaseClient(release))
    monkeypatch.setattr(updater, "_check_path", lambda: tmp_path / "check.json")
    monkeypatch.setattr(updater, "__version__", "0.7.21")
    with pytest.raises(updater.UpdateError):
        updater.apply(confirm=True)


def test_apply_refuses_unsupported_mode(monkeypatch, tmp_path):
    repo = tmp_path / "some"
    repo.mkdir()
    monkeypatch.setattr(config, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater, "__version__", "0.7.21")
    # fake an available release
    monkeypatch.setattr(updater, "check", lambda refresh=False: {
        "ok": True, "current": "0.7.21", "latest": "0.8.0", "update_available": True,
        "release": {"version": "0.8.0", "assets": []},
    })
    with pytest.raises(updater.UpdateError):
        updater.apply(confirm=True)


def test_apply_refuses_dirty_git_checkout(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "dirty.txt").write_text("x")
    monkeypatch.setattr(config, "REPO_ROOT", str(repo))

    def fake_git(root, *args, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        if args[:2] == ("status", "--porcelain"):
            R.stdout = " M dirty.txt\n"
        return R()

    monkeypatch.setattr(updater, "_git", fake_git)
    monkeypatch.setattr(updater, "__version__", "0.7.21")
    monkeypatch.setattr(updater, "check", lambda refresh=False: {
        "ok": True, "current": "0.7.21", "latest": "0.8.0", "update_available": True,
        "release": {"version": "0.8.0", "assets": []},
    })
    with pytest.raises(updater.UpdateError) as exc:
        updater.apply(confirm=True)
    assert "uncommitted changes" in str(exc.value)


def test_status_and_dismiss(tmp_path):
    updater._write_json(tmp_path / "state.json", {
        "state": "done", "to_version": "0.8.0", "from_version": "0.7.21",
        "log": [{"at": "x", "level": "ok", "line": "done"}],
    })
    # patch state path for this test
    old_path = updater._state_path
    updater._state_path = lambda: tmp_path / "state.json"
    try:
        st = updater.status()
        assert st["job"]["state"] == "done"
        st2 = updater.dismiss()
        assert st2["job"]["state"] == "idle"
    finally:
        updater._state_path = old_path


def test_download_verifies_checksum(monkeypatch, tmp_path):
    installer_bytes = b"fake installer"
    checksum = "b5b105c79173b9c0f0e6f8e1f9b8a8f8f8f8f8f8f8f8f8f8f8f8f8f8f8f8f8f8"

    class _StreamCtx:
        def __init__(self, r): self.r = r
        def __enter__(self): return self.r
        def __exit__(self, *a): return False

    class _StreamClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, **kw):
            class R:
                status_code = 200
                headers = {"content-length": str(len(installer_bytes))}
                def iter_bytes(self, chunk_size=1024):
                    yield installer_bytes
                def raise_for_status(self): pass
            return R()
        def stream(self, method, url, **kw):
            r = self.get(url)
            return _StreamCtx(r)

    # proper checksum
    release = {
        "tag_name": "v0.8.0", "name": "v0.8.0", "html_url": "x",
        "published_at": "2026-01-01T00:00:00Z", "body": "", "prerelease": False,
        "assets": [
            {"name": "PerunSetup.exe", "size": len(installer_bytes),
             "url": "https://example.com/PerunSetup.exe",
             "content_type": "application/x-msdos-program"},
            {"name": "PerunSetup.exe.sha256", "size": 80,
             "url": "https://example.com/PerunSetup.exe.sha256",
             "content_type": "text/plain"},
        ],
    }

    monkeypatch.setattr("httpx.Client", lambda *a, **k: _StreamClient())
    monkeypatch.setattr(updater, "updates_dir", lambda: tmp_path)

    import hashlib
    real_hash = hashlib.sha256(installer_bytes).hexdigest()
    def fetch_checksum(asset, name):
        return real_hash
    monkeypatch.setattr(updater, "_fetch_checksum", fetch_checksum)

    path = updater._download_installer("job", release)
    assert path.exists()


def test_runner_refuses_missing_plan(tmp_path):
    plan_path = tmp_path / "no-such-plan.json"
    rc = updater.run_runner(str(plan_path))
    assert rc == 1


try:
    from fastapi.testclient import TestClient
    from perun import server

    @pytest.fixture()
    def client():
        server.SETTINGS.auto_discover_on_start = False
        with TestClient(server.app, base_url="http://127.0.0.1:8760") as c:
            yield c

    def test_api_update_check(client):
        r = client.get("/api/update/check")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert "current" in body and "latest" in body

    def test_api_update_apply_refuses_when_up_to_date(client):
        r = client.post("/api/update/apply", json={})
        assert r.status_code == 400
        assert "up to date" in r.json()["detail"].lower()
except ImportError:
    pass


def test_apply_git_pull_and_installs_requirements(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "requirements.txt").write_text("fastapi\n")
    calls = []

    def fake_git(root, *args, **kw):
        class R:
            returncode = 0
            stdout = "Already up to date."
            stderr = ""
        calls.append(args)
        if args[:2] == ("pull", "--ff-only"):
            (root / "requirements.txt").write_text("fastapi>=0.110\n")
        return R()

    monkeypatch.setattr(updater, "_git", fake_git)
    pip_calls = []

    def fake_pip(args, **kw):
        pip_calls.append(args)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_pip)
    plan = {"root": str(repo), "job": "j1"}
    updater._apply_git(plan)
    assert ("pull", "--ff-only") in calls
    assert any("pip" in str(a) for a in pip_calls)


def test_apply_installer_fails_logs_error(monkeypatch, tmp_path):
    installer = tmp_path / "PerunSetup.exe"
    installer.write_text("fake")

    def fake_run(args, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "installer failed"
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(updater, "updates_dir", lambda: tmp_path)
    plan = {"installer": str(installer), "job": "j1"}
    with pytest.raises(updater.UpdateError) as exc:
        updater._apply_installer(plan)
    assert "exited with code 1" in str(exc.value)


def test_wait_for_pid_stops_stale_process(monkeypatch):
    calls = []
    monkeypatch.setattr(updater.daemon, "is_running", lambda pid: True)
    monkeypatch.setattr(updater.daemon, "stop", lambda pid, timeout: (calls.append(("stop", pid)) or False))
    monkeypatch.setattr(updater.daemon, "kill", lambda pid: calls.append(("kill", pid)))
    updater._wait_for_pid(1234, lambda msg: None, timeout=0.1)
    assert ("stop", 1234) in calls
    assert ("kill", 1234) in calls


def test_apply_rejects_concurrent_update(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr(config, "REPO_ROOT", str(repo))
    monkeypatch.setattr(updater, "__version__", "0.7.21")
    monkeypatch.setattr(updater, "check", lambda refresh=False: {
        "ok": True, "current": "0.7.21", "latest": "0.8.0", "update_available": True,
        "release": {"version": "0.8.0", "assets": []},
    })
    updater._write_json(tmp_path / "state.json", {"state": "downloading"})
    old_path = updater._state_path
    updater._state_path = lambda: tmp_path / "state.json"
    try:
        with pytest.raises(updater.UpdateError) as exc:
            updater.apply(confirm=True)
        assert "already in progress" in str(exc.value)
    finally:
        updater._state_path = old_path
