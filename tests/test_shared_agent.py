from pathlib import Path

from perun import shared_agent


def test_shared_agent_allows_only_guard_tools(tmp_path, monkeypatch):
    target = tmp_path / "agents" / "perun-shared.md"
    monkeypatch.setattr(shared_agent, "_agent_path", lambda: target)

    installed = shared_agent.ensure_shared_agent()

    assert installed == target
    text = target.read_text(encoding="utf-8")
    assert shared_agent.SHARED_AGENT_ID == "perun-shared"
    assert "mode: primary" in text
    assert 'action: "*"' in text and "effect: deny" in text
    assert "perun_guard_*" in text
    assert "guard_*" in text
    assert "native shell" in text.lower() or "cannot use shell" in text.lower()


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
