"""The build-time roster index: slug parity with upstream, tolerant parsing."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "agency_index.py"
_spec = importlib.util.spec_from_file_location("agency_index_tool", _TOOL)
agency_index = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(agency_index)


def _tarball(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(f"agency-agents-abc123/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _agent(name: str, description: str = "Does a thing", color: str = "blue") -> str:
    return f"---\nname: {name}\ndescription: {description}\ncolor: {color}\n---\nBody\n"


@pytest.mark.parametrize("name,expected", [
    ("Frontend Developer", "frontend-developer"),
    ("X/Twitter Intelligence Analyst", "x-twitter-intelligence-analyst"),
    ("  Weird   Spacing  ", "weird-spacing"),
    ("AI Engineer", "ai-engineer"),
    ("C++ Engineer", "c-engineer"),
])
def test_slug_rule_matches_upstream(name, expected):
    # Upstream names the installed file after this slug; a mismatch would make
    # every installed agent unrecognisable.
    assert agency_index.slugify(name) == expected


def test_frontmatter_reads_quoted_values_and_stops_at_the_fence():
    text = "---\nname: \"Quoted Name\"\ndescription: 'it: has a colon'\ncolor: '#123456'\n---\nname: not frontmatter\n"
    fields = agency_index._frontmatter(text)
    assert fields["name"] == "Quoted Name"
    assert fields["description"] == "it: has a colon"
    assert fields["color"] == "#123456"


def test_build_skips_docs_and_nested_markdown():
    archive = _tarball({
        "divisions.json": json.dumps({"divisions": {"engineering": {"label": "Engineering"}}}),
        "engineering/engineering-code-reviewer.md": _agent("Code Reviewer"),
        "engineering/README.md": "# Engineering\n",
        "engineering/notes/extra.md": _agent("Nested Doc"),
        "strategy/runbook.md": _agent("Runbook"),
        "design/design-ui-designer.md": _agent("UI Designer"),
    })

    index = agency_index.build("abc123", archive=archive)
    slugs = [a["slug"] for a in index["agents"]]
    assert slugs == ["ui-designer", "code-reviewer"]
    assert index["divisions"] == {
        "design": {"label": "Design", "count": 1},
        "engineering": {"label": "Engineering", "count": 1},
    }
    assert index["collisions"] == []
    assert index["agents"][1]["path"] == "engineering/engineering-code-reviewer.md"


def test_build_reports_duplicate_slugs():
    archive = _tarball({
        "engineering/a-code-reviewer.md": _agent("Code Reviewer"),
        "testing/b-code-reviewer.md": _agent("Code Reviewer"),
    })
    index = agency_index.build("abc123", archive=archive)
    assert index["collisions"] == ["code-reviewer"]
    assert len(index["agents"]) == 2


def test_render_is_valid_json_and_round_trips():
    archive = _tarball({"engineering/one.md": _agent("One Thing", "Describes — with an em dash")})
    index = agency_index.build("abc123", archive=archive)
    parsed = json.loads(agency_index.render(index))
    assert parsed["commit"] == "abc123"
    assert parsed["agents"][0]["description"] == "Describes — with an em dash"
