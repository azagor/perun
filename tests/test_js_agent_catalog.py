"""Static checks for the dashboard's agent-catalogue wiring.

The catalogue panel is DOM code, so these assertions guard the contract that
matters: the project picker asks for a project-scoped list (otherwise a
project-local roster is invisible), and the panel only offers the custody
actions Perun actually owns.
"""

from __future__ import annotations

from perun import config


def _js() -> str:
    return (config.REPO_ROOT / "web" / "js" / "perun.js").read_text(encoding="utf-8")


def test_the_agent_picker_can_be_scoped_to_a_project():
    text = _js()
    assert "/api/agents?project=${encodeURIComponent(slug)}" in text
    assert "state.agentsByProject" in text
    # The picker reads its option source lazily, so a project list loaded after
    # the control is built still shows up on refresh.
    assert "typeof opts.agents === 'function'" in text


def test_the_catalogue_panel_explains_both_install_paths():
    text = _js()
    assert "/api/agent-catalog/status?project=" in text
    assert "/api/agent-catalog/adopt" in text
    assert "/api/agent-catalog/backup" in text
    # v0.16.1 added a Settings installer, so the project panel must not claim
    # Perun never installs (that copy was stale).
    assert "Install them from Settings" in text
    assert "Perun does not install these" not in text


def test_the_settings_card_can_install_from_the_pinned_snapshot():
    text = _js()
    assert "function agentRosterSettingsCard" in text
    assert "v.appendChild(agentRosterSettingsCard())" in text
    assert "/api/agent-catalog/install" in text
    # The card must promise exactly what the backend guarantees.
    assert "never overwrites an agent you have edited" in text
    assert "pinned snapshot" in text


def test_the_settings_card_exposes_the_ceiling():
    text = _js()
    assert "Agent ceiling" in text
    assert "/api/agent-catalog/limit" in text
    # The panels only need counts; asking for the full list would ship ~100 KB.
    assert "agents=false" in text


def test_the_panel_is_rendered_for_a_project_config_card():
    text = _js()
    assert 'id="pc-catalog"' in text
    assert "renderAgentCatalogPanel(card.querySelector('#pc-catalog'), p.slug)" in text
