"""Perun context-map tests — the losslessness contract for AGENTS.md migrations.

Phase 4 of the AGENTS.md optimisation *moves* reference material out of a
project's ``AGENTS.md`` and into the Context layer. Moving material is only
safe if you can prove that nothing was lost — and a proof has to fail when the
proof's premises are broken. These tests cover both directions: a clean
migration passes, and every way of losing content is caught.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from perun import context, context_map, registry


ORIGINAL = """# Demo project

> A demo.

## Rules

* Rule one stays.
* Rule two stays.

## Reference

Some reference material that should be relocated.
"""

REFERENCE_SECTION = "## Reference\n\nSome reference material that should be relocated."


@pytest.fixture(autouse=True)
def _no_embeddings(monkeypatch):
    """Keep the tests independent of a running Ollama."""
    monkeypatch.setattr(context, "embed_texts",
                        lambda texts, prefix="": [[0.0] for _ in texts])


@pytest.fixture()
def project(tmp_path):
    """A registered project with its original ``AGENTS.md`` on disk."""
    root = tmp_path / "demo"
    root.mkdir()
    (root / "AGENTS.md").write_text(ORIGINAL, encoding="utf-8")
    return registry.add_project("Demo", str(root))


@pytest.fixture()
def migrated(project):
    """A project whose migration is complete and provably lossless.

    * the original is retained verbatim under ``<slug>.agents.full``;
    * ``Rules`` stays inline in the live ``AGENTS.md``;
    * ``Reference`` was relocated to ``<slug>.context.reference``.
    """
    root = Path(project["path"])
    slug = project["slug"]

    context.index_doc(context_map.retained_key(slug), "Demo agent reference",
                      ORIGINAL, scope=context.PROJECT_SCOPE,
                      project_id=project["id"])
    context.index_doc(context_map.context_key(slug, "reference"), "Reference",
                      REFERENCE_SECTION, scope=context.PROJECT_SCOPE,
                      project_id=project["id"])

    (root / "AGENTS.md").write_text(
        "# Demo project\n\n> A demo.\n\n## Rules\n\n"
        "* Rule one stays.\n* Rule two stays.\n", encoding="utf-8")

    data = context_map.build_map(slug, ORIGINAL)
    for section in data["sections"]:
        if section["heading"] == "Rules":
            section["dest"] = [{"kind": "inline",
                                "required": ["Rule one stays."]}]
        elif section["heading"] == "Reference":
            section["dest"] = [{"kind": "context",
                                "key": context_map.context_key(slug, "reference")}]
        else:
            section["dest"] = [{"kind": "inline",
                                "required": ["# Demo project"]}]
    context_map.save_map(root, data)
    return project


def _map(project):
    return context_map.load_map(Path(project["path"]))


def _save(project, data):
    context_map.save_map(Path(project["path"]), data)


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def test_split_sections_finds_headings_and_the_preamble():
    sections = context_map.split_sections(ORIGINAL)
    assert [s["heading"] for s in sections] == ["(top)", "Rules", "Reference"]
    assert "Rule one stays." in sections[1]["body"]
    assert sections[1]["bytes"] > 0 and sections[1]["hash"]


def test_split_sections_of_empty_text_is_empty():
    assert context_map.split_sections("") == []
    assert context_map.split_sections("   \n\n ") == []


def test_split_sections_without_a_heading_is_one_section():
    sections = context_map.split_sections("just a body")
    assert len(sections) == 1
    assert sections[0]["heading"] == "(top)"
    assert sections[0]["body"] == "just a body"


def test_split_sections_keeps_the_heading_line_in_the_body():
    """The hash covers the heading too, so a verbatim copy is detectable."""
    sections = context_map.split_sections(ORIGINAL)
    assert sections[2]["body"].startswith("## Reference")


def test_contains_ignores_re_wrapping():
    assert context_map.contains("a b   c", "a b c")
    assert not context_map.contains("a b c", "a x c")


# --------------------------------------------------------------------------- #
# The map file
# --------------------------------------------------------------------------- #
def test_map_round_trips(tmp_path):
    draft = context_map.build_map("demo", ORIGINAL)
    assert draft["version"] == context_map.MAP_VERSION
    assert draft["project"] == "demo"
    assert draft["retained"] == "demo.agents.full"
    assert len(draft["sections"]) == 3
    assert all(not s["dest"] for s in draft["sections"])

    context_map.save_map(tmp_path, draft)
    assert context_map.load_map(tmp_path) == draft
    assert context_map.map_path(tmp_path) == tmp_path / ".perun" / "context-map.json"


def test_load_map_returns_none_without_a_map(tmp_path):
    assert context_map.load_map(tmp_path) is None


def test_load_map_survives_a_corrupt_file(tmp_path):
    p = context_map.map_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert context_map.load_map(tmp_path) is None


def test_the_map_is_valid_json_on_disk(tmp_path):
    context_map.save_map(tmp_path, context_map.build_map("demo", ORIGINAL))
    raw = context_map.map_path(tmp_path).read_text(encoding="utf-8")
    assert json.loads(raw)["source_hash"]


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
def test_context_keys_are_namespaced_by_slug():
    assert context_map.context_key("demo", "commands") == "demo.context.commands"
    assert context_map.retained_key("demo") == "demo.agents.full"


def test_context_doc_helpers(tmp_path):
    p = context_map.write_context_doc(tmp_path, "commands", "body\n")
    assert p == tmp_path / ".perun" / "context" / "commands.md"
    assert p.read_text(encoding="utf-8") == "body\n"

    (context_map.context_dir(tmp_path) / "notes.txt").write_text("x", encoding="utf-8")
    assert [f.name for f in context_map.project_context_files(tmp_path)] == ["commands.md"]
    assert context_map.project_context_key("demo", p) == "demo.context.commands"


# --------------------------------------------------------------------------- #
# A clean migration
# --------------------------------------------------------------------------- #
def test_check_passes_for_a_lossless_migration(migrated):
    res = context_map.check(migrated)
    assert res["ok"], res["problems"]
    assert res["sections"] == 3
    assert not res["skipped"]


def test_check_skips_a_project_without_a_map(project):
    res = context_map.check(project)
    assert res["ok"] and res["skipped"]


def test_check_reports_the_number_of_sections(migrated):
    assert any("3 section(s) accounted for" in line
               for line in context_map.check(migrated)["report"])


# --------------------------------------------------------------------------- #
# Every way of losing content
# --------------------------------------------------------------------------- #
def test_check_fails_when_the_retained_original_is_missing(migrated):
    context.delete(context_map.retained_key(migrated["slug"]),
                   scope=context.PROJECT_SCOPE, project_id=migrated["id"])
    res = context_map.check(migrated)
    assert not res["ok"]
    assert any("retained original" in p for p in res["problems"])


def test_check_fails_when_the_retained_original_is_altered(migrated):
    """P1: the original must still hash to what the map recorded."""
    context.index_doc(context_map.retained_key(migrated["slug"]), "t", "tampered",
                      scope=context.PROJECT_SCOPE, project_id=migrated["id"])
    res = context_map.check(migrated)
    assert not res["ok"]
    assert any("source_hash" in p for p in res["problems"])


def test_check_fails_when_a_section_is_unaccounted_for(migrated):
    data = _map(migrated)
    data["sections"] = [s for s in data["sections"] if s["heading"] != "Rules"]
    _save(migrated, data)
    res = context_map.check(migrated)
    assert not res["ok"]
    assert any("not accounted for" in p for p in res["problems"])


def test_check_fails_when_a_context_doc_is_missing(migrated):
    context.delete(context_map.context_key(migrated["slug"], "reference"),
                   scope=context.PROJECT_SCOPE, project_id=migrated["id"])
    res = context_map.check(migrated)
    assert not res["ok"]
    assert any("does not exist" in p for p in res["problems"])


def test_check_fails_when_a_relocated_section_is_paraphrased(migrated):
    """Relocation must be verbatim — a paraphrase is a silent loss."""
    context.index_doc(context_map.context_key(migrated["slug"], "reference"),
                      "Reference", "## Reference\n\nSomething else entirely.\n",
                      scope=context.PROJECT_SCOPE, project_id=migrated["id"])
    res = context_map.check(migrated)
    assert not res["ok"]
    assert any("not present verbatim" in p for p in res["problems"])


def test_check_fails_when_an_inline_rule_is_lost(migrated):
    p = Path(migrated["path"]) / "AGENTS.md"
    p.write_text("# Demo project\n\n> A demo.\n\n## Rules\n\n"
                 "* Rule two stays.\n", encoding="utf-8")
    res = context_map.check(migrated)
    assert not res["ok"]
    assert any("must stay inline" in p_ for p_ in res["problems"])


def test_check_fails_when_a_section_has_no_destination(migrated):
    data = _map(migrated)
    for section in data["sections"]:
        if section["heading"] == "Rules":
            section["dest"] = []
    _save(migrated, data)
    res = context_map.check(migrated)
    assert not res["ok"]
    assert any("no destination" in p for p in res["problems"])


def test_check_fails_on_an_unknown_destination_kind(migrated):
    data = _map(migrated)
    for section in data["sections"]:
        if section["heading"] == "Rules":
            section["dest"] = [{"kind": "telepathy"}]
    _save(migrated, data)
    res = context_map.check(migrated)
    assert not res["ok"]
    assert any("unknown destination kind" in p for p in res["problems"])


def test_check_fails_when_a_named_secret_is_missing(migrated):
    data = _map(migrated)
    data["sections"][0]["dest"] = [{"kind": "secret", "key": "demo-token"}]
    _save(migrated, data)
    res = context_map.check(migrated)
    assert not res["ok"]
    assert any("not in the store" in p for p in res["problems"])


def test_check_accepts_a_secret_that_is_in_the_store(migrated):
    from perun import secret_store

    secret_store.set_secret("demo-token", "s3cret", project_id=migrated["id"])
    data = _map(migrated)
    data["sections"][0]["dest"] = [{"kind": "secret", "key": "demo-token"}]
    _save(migrated, data)
    res = context_map.check(migrated)
    assert res["ok"], res["problems"]


def test_check_fails_when_the_map_is_malformed(migrated):
    _save(migrated, {"version": 1, "project": "demo"})
    res = context_map.check(migrated)
    assert not res["ok"]
    assert any("sections" in p for p in res["problems"])


# --------------------------------------------------------------------------- #
# The retained original is a snapshot, not a live mirror
# --------------------------------------------------------------------------- #
def test_the_retained_original_is_never_overwritten_by_a_sync(migrated, monkeypatch):
    """A sync after the migration must not clobber the pre-migration file."""
    from perun import __main__ as cli

    root = Path(migrated["path"])
    (root / "AGENTS.md").write_text("# Demo project\n\nRewritten.\n",
                                     encoding="utf-8")
    lines = cli._context_sync_projects(migrated["slug"])
    assert any("not overwritten" in line for line in lines)

    doc = context.get(context_map.retained_key(migrated["slug"]),
                      project_id=migrated["id"], scope=context.PROJECT_SCOPE)
    assert doc["body"] == ORIGINAL.strip()
