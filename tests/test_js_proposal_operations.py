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


def test_proposal_peek_renders_operation_chips():
    text = (config.REPO_ROOT / "web" / "js" / "perun.js").read_text(
        encoding="utf-8")
    assert "const operationHtml" in text
    assert '<span class="k">Operations</span>' in text
    assert '<span class="k">Targets</span>' in text  # legacy fallback
