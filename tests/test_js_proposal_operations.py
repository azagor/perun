"""The Shared-tab proposal row renders the changeset operations."""

from pathlib import Path

from perun import config


def test_proposal_row_renders_operation_chips():
    text = (config.REPO_ROOT / "web" / "js" / "perun.js").read_text(
        encoding="utf-8")
    assert "Array.isArray(prop.operations)" in text
    assert "op.op" in text
    assert 'data-target="${esc(target)}"' in text
    assert "prop.targets" in text  # legacy rows still fall back to targets
