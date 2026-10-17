"""Regression tests for the global-facts migration tool."""
from __future__ import annotations

from perun import control, db, memory, registry

from tools import migrate_global_facts


def test_migrate_moves_perun_facts_and_skips_non_perun(tmp_path):
    p = registry.add_project("userproj", str(tmp_path / "user"))
    # Perun-specific global fact
    memory.remember("Perun v9.9.9 ships a new widget.", scope="global", topic="version_build")
    # Non-Perun global fact
    memory.remember("Generic cross-project convention.", scope="global", topic="general")
    # User project fact
    memory.remember("user fact", project_id=p["id"], scope="project")

    result = migrate_global_facts.migrate()

    assert result["moved"] == 1
    assert result["duplicates_skipped"] == 0
    assert result["duplicates_deleted"] is False
    # Non-Perun fact stays global.
    with db.db() as conn:
        global_count = db.one(conn, "SELECT COUNT(*) c FROM memory WHERE scope='global' AND project_id=0")["c"]
        assert global_count == 1
        perun_id = control.ensure()["id"]
        moved = db.one(
            conn,
            "SELECT COUNT(*) c FROM memory WHERE project_id=? AND content LIKE ?",
            (perun_id, "%Perun v9.9.9 ships a new widget%"),
        )["c"]
        assert moved == 1


def test_migrate_is_non_destructive_for_duplicates(tmp_path):
    perun_id = control.ensure()["id"]
    text = "Perun v9.9.9 duplicate test."
    memory.remember(text, scope="global", topic="version_build")
    memory.remember(text, project_id=perun_id, scope="project", topic="version_build")

    before_total = memory.stats(None)["facts_total"]
    result = migrate_global_facts.migrate()
    after_total = memory.stats(None)["facts_total"]

    assert result["moved"] == 0
    assert result["duplicates_skipped"] == 1
    assert result["duplicates_deleted"] is False
    assert after_total == before_total


def test_migrate_can_delete_duplicates_when_requested(tmp_path):
    perun_id = control.ensure()["id"]
    text = "Perun v9.9.9 delete duplicate test."
    memory.remember(text, scope="global", topic="version_build")
    memory.remember(text, project_id=perun_id, scope="project", topic="version_build")

    before_total = memory.stats(None)["facts_total"]
    result = migrate_global_facts.migrate(delete_duplicates=True)
    after_total = memory.stats(None)["facts_total"]

    assert result["moved"] == 0
    assert result["duplicates_skipped"] == 1
    assert result["duplicates_deleted"] == 1
    assert after_total == before_total - 1
