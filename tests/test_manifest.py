"""Tests for the project manifest (perun.md / perun.json)."""

from __future__ import annotations

import json

from perun import manifest


def test_parse_front_matter_and_body():
    text = """---
version: 1
name: Demo
tags: [python, cli]
docs:
  readme: README.md
commands:
  test: python -m pytest
anchors:
  - label: Entry
    kind: file
    path: main.py
  - label: Site
    kind: url
    url: https://example.com
  - label: Run tests
    kind: command
    command: test
---

# Demo

Hello body.
"""
    data, body, warnings = manifest.parse(text)
    assert warnings == []
    assert data["name"] == "Demo"
    assert data["tags"] == ["python", "cli"]
    assert data["docs"]["readme"] == "README.md"
    assert data["commands"]["test"] == "python -m pytest"
    kinds = {a["label"]: a["kind"] for a in data["anchors"]}
    assert kinds == {"Entry": "file", "Site": "url", "Run tests": "command"}
    assert body.strip() == "# Demo\n\nHello body."


def test_normalize_coerces_and_preserves_unknown_keys():
    data, warnings = manifest.normalize({
        "version": "2",
        "name": 123,
        "tags": "a, b,c",
        "unknown_future_key": {"nested": True},
        "anchors": [
            {"label": "No locator", "kind": "file"},
            {"kind": "url", "url": "https://x.y"},  # label inferred from url
        ],
    })
    assert data["version"] == 2
    assert data["name"] == "123"
    assert data["tags"] == ["a", "b", "c"]
    assert data["unknown_future_key"] == {"nested": True}
    assert any("missing" in w for w in warnings)
    # The url-only anchor keeps its url-derived label.
    assert data["anchors"][0]["url"] == "https://x.y"


def test_malformed_yaml_degrades_gracefully():
    text = "---\nname: [unterminated\n---\nbody\n"
    data, body, warnings = manifest.parse(text)
    assert data["version"] == manifest.SCHEMA_VERSION
    assert any("invalid YAML" in w for w in warnings)


def test_missing_front_matter_is_all_body():
    data, body, warnings = manifest.parse("# Just markdown\n")
    assert data == {"version": 1, "tags": [], "docs": {}, "commands": {},
                    "links": {}, "anchors": [], "git": {}}
    assert body.strip() == "# Just markdown"


def test_load_and_save_roundtrip(tmp_path):
    data = {
        "version": 1, "name": "Round", "tags": ["x"],
        "anchors": [{"label": "R", "kind": "doc", "path": "README.md"}],
    }
    target = manifest.save(tmp_path, data, "# Round\n\nBody text.")
    assert target.name == "perun.md"
    snap = manifest.load(tmp_path)
    assert snap["found"] is True
    assert snap["data"]["name"] == "Round"
    assert "Body text." in snap["body"]
    assert snap["warnings"] == []


def test_load_prefers_md_over_json(tmp_path):
    (tmp_path / "perun.json").write_text(json.dumps({"name": "FromJson"}),
                                         encoding="utf-8")
    (tmp_path / "perun.md").write_text("---\nname: FromMd\n---\n", encoding="utf-8")
    snap = manifest.load(tmp_path)
    assert snap["data"]["name"] == "FromMd"


def test_load_json_fallback(tmp_path):
    (tmp_path / "perun.json").write_text(json.dumps({"name": "JsonOnly", "tags": ["j"]}),
                                         encoding="utf-8")
    snap = manifest.load(tmp_path)
    assert snap["found"] is True
    assert snap["data"]["name"] == "JsonOnly"
    assert snap["data"]["tags"] == ["j"]


def test_load_absent_returns_not_found(tmp_path):
    snap = manifest.load(tmp_path)
    assert snap["found"] is False
    assert snap["data"] == {}


def test_to_project_fields():
    data, _ = manifest.normalize({
        "name": "P", "kind": "opencode", "tags": ["a", "b"], "model": "m/x",
        "git": {"remote": "https://x/y.git", "branch": "dev"},
    })
    fields = manifest.to_project_fields(data)
    assert fields["name"] == "P"
    assert fields["tags"] == "a,b"
    assert fields["model"] == "m/x"
    assert fields["git_remote"] == "https://x/y.git"
    assert fields["git_branch"] == "dev"


def test_sync_manifest_routes_instructions_to_file(tmp_path):
    """A manifest's ``instructions`` is written to .perun/instructions/default.md."""
    from perun import orchestrator, projects, registry

    manifest.save(str(tmp_path), {
        "version": 1, "name": "SyncInstr", "instructions": "FOLLOW MANIFEST RULE",
    }, "")
    p = registry.add_project("SyncInstr", str(tmp_path))
    projects.sync_manifest(p)

    target = tmp_path / orchestrator.PROJECT_INSTRUCTIONS_DIR / "default.md"
    assert target.is_file()
    assert "FOLLOW MANIFEST RULE" in target.read_text(encoding="utf-8")
    # Runtime instructions come from the file, not the database.
    blocks = orchestrator._project_instruction_blocks(registry.get_project(p["slug"]))
    assert "FOLLOW MANIFEST RULE" in "\n".join(blocks)
    # A later sync must not clobber a dashboard edit (seed-once).
    target.write_text("---\nname: Edited\nenabled: true\n---\n\nuser edit\n",
                      encoding="utf-8")
    projects.sync_manifest(registry.get_project(p["slug"]))
    assert "user edit" in target.read_text(encoding="utf-8")


def test_from_project_suggests_anchors(tmp_path):
    (tmp_path / "README.md").write_text("# hi", encoding="utf-8")
    project = {"name": "P", "slug": "p", "path": str(tmp_path), "kind": "opencode",
               "tags": "python,cli"}
    data = manifest.from_project(project)
    assert data["name"] == "P"
    assert data["tags"] == ["python", "cli"]
    labels = {a["label"] for a in data["anchors"]}
    assert "Readme" in labels
    # And it must serialise cleanly.
    text = manifest.dump(data, manifest.default_body(project))
    assert text.startswith("---\n")


def test_dump_is_valid_and_reparseable(tmp_path):
    data = {"version": 1, "name": "D", "tags": ["a"],
            "anchors": [{"label": "A", "kind": "file", "path": "a.py"}]}
    text = manifest.dump(data, "# D")
    (tmp_path / "perun.md").write_text(text, encoding="utf-8")
    snap = manifest.load(tmp_path)
    assert snap["data"]["name"] == "D"
    assert snap["data"]["anchors"][0]["path"] == "a.py"


def test_dump_omits_empty_defaults():
    text = manifest.dump({"version": 1, "name": "N", "docs": {}, "tags": [],
                          "links": {}, "commands": {}, "anchors": [], "git": {}})
    assert "docs" not in text and "tags" not in text and "git" not in text
    assert "version" in text and "name" in text


def test_save_back_to_existing_json(tmp_path):
    (tmp_path / "perun.json").write_text(json.dumps({"name": "J"}), encoding="utf-8")
    target = manifest.save(tmp_path, {"version": 1, "name": "J2", "tags": ["x"]},
                           "# Body\n\nnotes")
    assert target.name == "perun.json"
    assert not (tmp_path / "perun.md").exists()
    snap = manifest.load(tmp_path)
    assert snap["data"]["name"] == "J2"
    assert snap["data"]["tags"] == ["x"]
    assert "notes" in snap["body"]


def test_parse_json_body_key(tmp_path):
    (tmp_path / "perun.json").write_text(
        json.dumps({"name": "J", "body": "# Hi\n\ntext"}), encoding="utf-8")
    snap = manifest.load(tmp_path)
    assert snap["data"]["name"] == "J"
    assert "text" in snap["body"]
    assert "body" not in snap["data"]
