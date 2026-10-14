"""Global-install merge/uninstall tests against a temp OpenCode config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from perun.integrations import install_global


@pytest.fixture()
def fake_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # On Windows the code honours XDG_CONFIG_HOME too (see config_dir()).
    return tmp_path / "opencode"


def test_install_creates_mcp_and_instructions(fake_xdg):
    assert install_global.install(index=False) == 0
    cfg = json.loads(install_global.opencode_json().read_text(encoding="utf-8"))
    assert "perun" in cfg["mcp"]
    assert cfg["mcp"]["perun"]["type"] == "local"
    assert cfg["mcp"]["perun"]["command"][-1].endswith("perun_mcp.py")
    assert any("perun/INSTRUCTIONS.md" in i for i in cfg["instructions"])
    assert install_global.instructions_target().is_file()
    # templates were copied into commands/
    assert (fake_xdg / "commands" / "perun-projects.md").is_file()


def test_install_preserves_unrelated_keys(fake_xdg):
    cfg_path = install_global.opencode_json()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "model": "opencode/big-pickle",
        "mcp": {"other": {"type": "local", "command": ["x"]}},
    }), encoding="utf-8")

    install_global.install(index=False)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["model"] == "opencode/big-pickle"
    assert "other" in cfg["mcp"]
    assert "perun" in cfg["mcp"]


def test_install_backs_up_existing(fake_xdg):
    cfg_path = install_global.opencode_json()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{}", encoding="utf-8")
    install_global.install(index=False)
    backups = list(cfg_path.parent.glob("opencode.json.perun-backup-*"))
    assert backups, "expected a backup of the previous config"


def test_install_is_idempotent(fake_xdg):
    install_global.install(index=False)
    install_global.install(index=False)
    cfg = json.loads(install_global.opencode_json().read_text(encoding="utf-8"))
    assert cfg["instructions"].count("perun/INSTRUCTIONS.md") == 1
    assert list(cfg["mcp"]).count("perun") == 1


def test_uninstall_removes_only_perun(fake_xdg):
    cfg_path = install_global.opencode_json()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "model": "opencode/big-pickle",
        "mcp": {"other": {"type": "local", "command": ["x"]}},
        "instructions": ["some/existing.md"],
    }), encoding="utf-8")

    install_global.install(index=False)
    install_global.uninstall()

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["model"] == "opencode/big-pickle"
    assert "other" in cfg["mcp"]
    assert "perun" not in cfg["mcp"]
    assert cfg["instructions"] == ["some/existing.md"]
    assert not install_global.instructions_target().exists()


def test_status_reports_installed(fake_xdg):
    assert install_global.status()["installed"] is False
    install_global.install(index=False)
    assert install_global.status()["installed"] is True
