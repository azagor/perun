"""The dashboard exposes the VS Code link, launch modes and selection flow."""

from __future__ import annotations

import json
from pathlib import Path

from perun import config


def _js() -> str:
    return (config.REPO_ROOT / "web" / "js" / "perun.js").read_text(encoding="utf-8")


def test_the_project_card_has_a_vscode_badge_and_action():
    text = _js()
    assert "vscode_linked" in text
    assert "data-vscode" in text
    assert "openProjectVsCode" in text


def test_the_project_config_has_an_ide_card():
    text = _js()
    assert "pc-vscode-linked" in text
    assert "pc-vscode-mode" in text
    assert "pc-vscode-follow" in text
    assert "pc-vscode-status" in text
    assert "refreshVSCodeStatus" in text
    assert "/vscode/link" in text
    assert "/vscode/unlink" in text


def test_the_run_flow_can_host_the_tui_in_vscode():
    text = _js()
    assert "/vscode/open" in text
    assert "vscode-terminal" in text
    assert "external-terminal" in text
    assert "Run in VS Code terminal" in text


def test_the_wizard_can_link_the_new_project():
    text = _js()
    assert "vscode_link" in text
    assert "Link with Visual Studio Code" in text
    # Linking is opt-in: a generated .code-workspace is a new VS Code trust scope.
    assert "vscode_link: false" in text
    assert "new VS Code trust scope" in text


def test_the_ide_badge_shows_a_pending_link_edit():
    text = _js()
    assert "will link (save)" in text
    assert "will unlink (save)" in text


def test_the_companion_package_is_a_valid_extension_manifest():
    root = Path(config.REPO_ROOT) / "extensions" / "vscode"
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert package["name"] == "perun-overseer"
    assert package["main"] == "./dist/extension.js"
    assert "onStartupFinished" in package["activationEvents"]
    assert "perun.sendSelection" in {
        command["command"] for command in package["contributes"]["commands"]
    }
    assert "perun.openTui" in {
        command["command"] for command in package["contributes"]["commands"]
    }
    assert "startTui" in (root / "src" / "extension.js").read_text(encoding="utf-8")
    extension = (root / "src" / "extension.js").read_text(encoding="utf-8")
    assert "showQuickPick" in extension
    assert "Resume latest" in extension
    assert "TERMINAL_STATE_KEY" in extension
    assert "rebindTerminal" in extension
    assert "vscode.env.remoteName" in extension
    assert "vscode.workspace.isTrusted" in extension
    assert "perun.dashboard" not in extension
    assert "simpleBrowser.show" in extension
    assert "embed-ticket" in extension
    assert "ViewColumn.Beside" in extension
    assert (root / "src" / "extension.js").is_file()
