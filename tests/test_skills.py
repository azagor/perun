"""Tests for perun.skills discovery."""

from pathlib import Path

from perun import skills


def test_builtin_tools_is_non_empty():
    assert len(skills.BUILTIN_TOOLS) > 0
    names = {t["name"] for t in skills.BUILTIN_TOOLS}
    assert "read" in names
    assert "bash" in names


def test_builtin_skills_are_returned(monkeypatch):
    # Ensure built-in skills are surfaced even when the global skills dir is empty.
    from perun import config
    monkeypatch.setattr(config, "GLOBAL_SKILLS_DIR", Path("/nonexistent/skills"))
    result = skills.discover_skills(project_path=None)
    names = {s["name"] for s in result}
    assert "code-audit" in names
    assert "android-design-guidelines" in names
    assert "find-skills" in names
    for s in result:
        if s["name"] in {"code-audit", "android-design-guidelines", "find-skills"}:
            assert s.get("builtin") is True


def test_discover_skills_global(tmp_path, monkeypatch):
    base = tmp_path / "skills"
    skill_dir = base / "test-skill"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\n\nbody\n",
        encoding="utf-8",
    )
    from perun import config
    monkeypatch.setattr(config, "GLOBAL_SKILLS_DIR", base)
    result = skills.discover_skills(project_path=None)
    names = {s["name"] for s in result}
    assert "test-skill" in names


def test_discover_tools_project_local(tmp_path):
    tools_dir = tmp_path / ".opencode" / "tools"
    tools_dir.mkdir(parents=True)
    tools_dir.joinpath("database.ts").write_text(
        "export default tool({ description: 'Query DB' });\n",
        encoding="utf-8",
    )
    result = skills.discover_tools(project_path=str(tmp_path))
    names = {t["name"] for t in result}
    assert "database" in names
    assert "read" in names  # built-in still present


def test_discover_tools_ignores_non_tool_files(tmp_path):
    tools_dir = tmp_path / ".opencode" / "tools"
    tools_dir.mkdir(parents=True)
    tools_dir.joinpath("readme.md").write_text("not a tool", encoding="utf-8")
    result = skills.discover_tools(project_path=str(tmp_path))
    names = {t["name"] for t in result}
    assert "readme" not in names


def test_discover_tools_multiple_exports(tmp_path):
    tools_dir = tmp_path / ".opencode" / "tools"
    tools_dir.mkdir(parents=True)
    tools_dir.joinpath("math.ts").write_text(
        "export const add = tool(...);\nexport const multiply = tool(...);\n",
        encoding="utf-8",
    )
    result = skills.discover_tools(project_path=str(tmp_path))
    names = {t["name"] for t in result}
    assert "math_add" in names
    assert "math_multiply" in names
