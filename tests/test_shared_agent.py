from pathlib import Path

import yaml

from perun import action_proposals, registry, shared_agent


def _frontmatter(md):
    return yaml.safe_load(md.split("---")[1])


def test_shared_agent_uses_v2_permissions_array(tmp_path, monkeypatch):
    target = tmp_path / "agents" / "perun-shared.md"
    monkeypatch.setattr(shared_agent, "_agent_path", lambda: target)

    installed = shared_agent.ensure_shared_agent()

    assert installed == target
    text = target.read_text(encoding="utf-8")
    assert shared_agent.SHARED_AGENT_ID == "perun-shared"
    assert "mode: primary" in text
    assert "native shell" in text.lower() or "cannot use shell" in text.lower()
    assert "perun-guard_guard_write_file" in text
    assert "Do not call the `skill` tool" in text

    fm = _frontmatter(text)
    assert "permission" not in fm
    assert isinstance(fm.get("permissions"), list)
    # Broad deny must come before guard allows (last-match-wins in V2).
    actions = [p["action"] for p in fm["permissions"]]
    assert actions[0] == "*"
    assert "skill" in actions
    assert fm["permissions"][actions.index("skill")]["effect"] == "deny"
    assert "guard_*" in actions
    assert "perun_guard_*" in actions
    assert "perun-guard_*" in actions
    # Write/edit guard tools stay allowed for the shared executor.
    assert "guard_write_file" not in actions or fm["permissions"][actions.index("guard_write_file")]["effect"] == "allow"
    assert "guard_edit_file" not in actions or fm["permissions"][actions.index("guard_edit_file")]["effect"] == "allow"


def test_ensure_shared_agent_gracefully_fails_when_parent_dir_is_not_writable(
    tmp_path, monkeypatch
):
    """Installation is best-effort: if the parent directory cannot be created,
    ensure_shared_agent() returns the target path and leaves it to the caller
    to decide how to fail."""
    target = tmp_path / "agents" / "perun-shared.md"
    monkeypatch.setattr(shared_agent, "_agent_path", lambda: target)

    def fake_mkdir(self, *args, **kwargs):
        if str(self).endswith("agents"):
            raise PermissionError("read-only parent")
        return None

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)

    result = shared_agent.ensure_shared_agent()

    assert result == target
    assert not target.is_file()


def test_execution_prompt_uses_prefixed_guard_tools(tmp_path):
    project_path = tmp_path / "prompt-project"
    project_path.mkdir()
    project = registry.add_project("PromptProject", str(project_path))
    prompt = action_proposals._build_execution_prompt(
        {"requester": "frambo", "intent": "write a file", "targets": ["a.txt"],
         "details": {}},
        project,
    )
    assert "perun-guard_guard_write_file" in prompt
    assert "Do not call the `skill` tool" in prompt
    assert "Unable to load skill" in prompt
