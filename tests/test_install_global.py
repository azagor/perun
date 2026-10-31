"""Global-install merge/uninstall tests against a temp OpenCode config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from perun.integrations import install_global


@pytest.fixture()
def fake_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Backups live under PERUN_HOME/backups; keep the real home untouched.
    from perun import backups, config

    monkeypatch.setattr(config, "BACKUPS_DIR", tmp_path / "perun-home" / "backups")
    assert backups.backup_dir("opencode").is_relative_to(tmp_path)
    # On Windows the code honours XDG_CONFIG_HOME too (see config_dir()).
    return tmp_path / "opencode"


def _servers(cfg):
    """The server map of a config, whichever ``mcp`` shape it uses."""
    mcp = cfg.get("mcp") or {}
    servers = mcp.get("servers")
    if isinstance(servers, dict):
        return servers
    return {k: v for k, v in mcp.items() if k not in ("servers", "timeout")}


def test_install_creates_mcp_and_instructions(fake_xdg):
    assert install_global.install(index=False) == 0
    cfg = json.loads(install_global.opencode_json().read_text(encoding="utf-8"))
    servers = _servers(cfg)
    assert "perun" in servers
    assert servers["perun"]["type"] == "local"
    assert servers["perun"]["command"][-1].endswith("perun_mcp.py")
    assert any("perun/INSTRUCTIONS.md" in i for i in cfg["instructions"])
    assert install_global.instructions_target().is_file()
    # templates were copied into commands/
    assert (fake_xdg / "commands" / "perun-projects.md").is_file()
    assert (fake_xdg / "commands" / "perun-ask.md").is_file()
    assert (fake_xdg / "commands" / "logwork.md").is_file()
    assert (fake_xdg / "commands" / "update-docs.md").is_file()
    # commands registered in opencode.json
    assert "perun-projects" in cfg["command"]
    assert "perun-ask" in cfg["command"]
    assert "logwork" in cfg["command"]
    assert "update-docs" in cfg["command"]


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
    assert "other" in _servers(cfg)
    assert "perun" in _servers(cfg)


def test_install_backs_up_existing(fake_xdg):
    cfg_path = install_global.opencode_json()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{}", encoding="utf-8")
    install_global.install(index=False)
    from perun import backups as _backups

    copies = _backups._copies(_backups.backup_dir("opencode"), "opencode.json")
    assert copies, "expected a backup of the previous config"
    assert not list(cfg_path.parent.glob("opencode.json*backup*"))


def test_install_is_idempotent(fake_xdg):
    install_global.install(index=False)
    install_global.install(index=False)
    cfg = json.loads(install_global.opencode_json().read_text(encoding="utf-8"))
    assert cfg["instructions"].count("perun/INSTRUCTIONS.md") == 1
    assert list(_servers(cfg)).count("perun") == 1


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
    assert "other" in _servers(cfg)
    assert "perun" not in _servers(cfg)
    assert cfg["instructions"] == ["some/existing.md"]
    assert not install_global.instructions_target().exists()


def test_status_reports_installed(fake_xdg):
    assert install_global.status()["installed"] is False
    install_global.install(index=False)
    assert install_global.status()["installed"] is True
    assert install_global.status()["agents_block"] is True


def test_install_writes_global_agents_block(fake_xdg):
    install_global.install(index=False)
    agents = install_global.agents_target()
    assert agents.is_file()
    text = agents.read_text(encoding="utf-8")
    assert install_global.AGENTS_BEGIN in text
    assert install_global.AGENTS_END in text
    assert "perun" in text.lower()


def test_agents_block_preserves_user_content(fake_xdg):
    agents = install_global.agents_target()
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text("# My global rules\n\n- be nice\n", encoding="utf-8")

    install_global.install(index=False)
    text = agents.read_text(encoding="utf-8")
    assert "be nice" in text
    assert install_global.AGENTS_BEGIN in text

    install_global.uninstall()
    after = agents.read_text(encoding="utf-8")
    assert "be nice" in after
    assert install_global.AGENTS_BEGIN not in after


def test_agents_block_is_idempotent(fake_xdg):
    install_global.install(index=False)
    first = install_global.agents_target().read_text(encoding="utf-8")
    install_global.install(index=False)
    second = install_global.agents_target().read_text(encoding="utf-8")
    assert first == second
    assert second.count(install_global.AGENTS_BEGIN) == 1


def test_uninstall_deletes_agents_file_when_only_perun(fake_xdg):
    install_global.install(index=False)
    assert install_global.agents_target().is_file()
    install_global.uninstall()
    assert not install_global.agents_target().exists()


def test_install_pins_perun_home_in_mcp_entry(fake_xdg):
    from perun import config

    install_global.install(index=False)
    cfg = json.loads(install_global.opencode_json().read_text(encoding="utf-8"))
    env = _servers(cfg)["perun"].get("environment") or {}
    assert env.get("PERUN_HOME") == str(config.HOME)


def test_install_tolerates_jsonc_and_keeps_comments_keys(fake_xdg):
    cfg_path = install_global.opencode_json()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        "{\n"
        '  // the user\'s model\n'
        '  "model": "opencode/big-pickle",\n'
        '  "mcp": {"other": {"type": "local", "command": ["x"]}},\n'
        "}\n",
        encoding="utf-8",
    )
    assert install_global.install(index=False) == 0
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["model"] == "opencode/big-pickle"
    assert "other" in _servers(cfg)
    assert "perun" in _servers(cfg)


def test_install_aborts_on_unparseable_config(fake_xdg):
    cfg_path = install_global.opencode_json()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    original = "{ this is not json at all ::: "
    cfg_path.write_text(original, encoding="utf-8")

    assert install_global.install(index=False) == 2
    # The user's file is left exactly as it was (no silent replacement).
    assert cfg_path.read_text(encoding="utf-8") == original
    from perun import backups as _backups

    assert _backups._copies(_backups.backup_dir("opencode"), "opencode.json") == []


def test_install_honours_opencode_config_env(fake_xdg, monkeypatch, tmp_path):
    target = tmp_path / "elsewhere" / "my-opencode.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OPENCODE_CONFIG", str(target))

    assert install_global.opencode_json() == target
    assert install_global.install(index=False) == 0
    cfg = json.loads(target.read_text(encoding="utf-8"))
    assert "perun" in _servers(cfg)


def test_reinstall_does_not_pile_up_backups(fake_xdg):
    cfg_path = install_global.opencode_json()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{}", encoding="utf-8")

    install_global.install(index=False)
    install_global.install(index=False)
    install_global.install(index=False)
    from perun import backups as _backups

    copies = _backups._copies(_backups.backup_dir("opencode"), "opencode.json")
    assert len(copies) == 1, f"expected a single backup, got {len(copies)}"


def test_instructions_pointer_is_written(fake_xdg):
    """Install writes the thin pointer, not the full kernel."""
    assert install_global.install(index=False) == 0
    pointer = install_global.instructions_target()
    assert pointer.is_file()
    text = pointer.read_text(encoding="utf-8")
    assert text == install_global._INSTRUCTIONS_POINTER
    # It must not contain the full AGENTS.md kernel again.
    assert install_global.AGENTS_BEGIN not in text


def test_instructions_pointer_is_idempotent(fake_xdg):
    """Re-installing does not backup the pointer when it has not changed."""
    install_global.install(index=False)
    pointer = install_global.instructions_target()
    first_mtime = pointer.stat().st_mtime_ns
    install_global.install(index=False)
    assert pointer.read_text(encoding="utf-8") == install_global._INSTRUCTIONS_POINTER
    # No backup of the pointer should have been created.
    from perun import backups as _backups

    assert _backups._copies(_backups.backup_dir("opencode"), "INSTRUCTIONS.md") == []
    assert not list(pointer.parent.glob("INSTRUCTIONS.md*backup*"))


def test_uninstall_removes_instructions_pointer(fake_xdg):
    install_global.install(index=False)
    pointer = install_global.instructions_target()
    assert pointer.is_file()
    install_global.uninstall()
    assert not pointer.exists()

def test_install_writes_the_v2_shape_and_migrates_a_v1_entry(fake_xdg, monkeypatch):
    """On a V2 CLI the global config gets ``mcp.servers``, not a flat ``mcp.perun``."""
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v2")
    cfg_path = install_global.opencode_json()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "instructions": ["keep-me.md"],
        "mcp": {
            "perun": {"type": "local", "command": ["python", "old.py"], "enabled": True},
            "other": {"type": "remote", "url": "https://mcp.example/mcp"},
        },
    }), encoding="utf-8")

    assert install_global.install(index=False) == 0
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    assert list(cfg["mcp"]) == ["servers"]
    assert cfg["mcp"]["servers"]["perun"]["command"][-1].endswith("perun_mcp.py")
    assert "enabled" not in cfg["mcp"]["servers"]["perun"]
    assert cfg["mcp"]["servers"]["other"]["url"] == "https://mcp.example/mcp"
    assert "keep-me.md" in cfg["instructions"]


def test_uninstall_removes_perun_from_either_shape(fake_xdg, monkeypatch):
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v2")
    install_global.install(index=False)
    cfg_path = install_global.opencode_json()

    assert install_global.uninstall() == 0
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "perun" not in _servers(cfg)


def test_status_understands_the_v2_shape(fake_xdg, monkeypatch):
    monkeypatch.setenv("PERUN_OPENCODE_MCP_STYLE", "v2")
    install_global.install(index=False)
    info = install_global.status()
    assert info["installed"] is True
    assert info["mcp"]["environment"]["PERUN_HOME"]
