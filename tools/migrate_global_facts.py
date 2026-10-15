"""Move Perun-related global facts onto the control-plane project.

Run once from the Perun repo:
    python tools/migrate_global_facts.py

By default duplicate global rows are left alone so the migration is
non-destructive. Pass --delete-duplicates to remove them.
"""
from __future__ import annotations

import argparse
import json

from perun import control, db


def migrate(*, delete_duplicates: bool = False) -> dict:
    control_project = control.ensure()
    target_id = control_project["id"]
    moved = 0
    skipped = 0

    with db.db() as conn:
        rows = db.rows(conn, "SELECT id, content FROM memory WHERE scope='global' AND project_id=0")
        for r in rows:
            content = (r["content"] or "").lower()
            text = r["content"] or ""
            is_perun_fact = (
                "perun" in content
                or ("telegram" in content and "perunbridge" in content)
                or ("opencode" in content and "v0." in text)
                or "csrf" in content
                or "dns rebinding" in content
            )
            if not is_perun_fact:
                continue

            h = db.one(conn, "SELECT content_hash FROM memory WHERE id=?", (r["id"],))["content_hash"]
            dup = db.one(
                conn,
                "SELECT id FROM memory WHERE scope='project' AND project_id=? AND content_hash=?",
                (target_id, h),
            )
            if dup:
                if delete_duplicates:
                    conn.execute("DELETE FROM memory WHERE id=?", (r["id"],))
                skipped += 1
                continue

            conn.execute(
                "UPDATE memory SET scope='project', project_id=? WHERE id=?",
                (target_id, r["id"]),
            )
            moved += 1

        remaining_global = db.one(
            conn, "SELECT COUNT(*) c FROM memory WHERE scope='global' AND project_id=0"
        )["c"]
        perun_project_facts = db.one(
            conn, "SELECT COUNT(*) c FROM memory WHERE project_id=?", (target_id,)
        )["c"]

    return {
        "control_project_id": target_id,
        "control_project_slug": control_project["slug"],
        "moved": moved,
        "duplicates_skipped": skipped,
        "duplicates_deleted": delete_duplicates and skipped,
        "remaining_global_facts": remaining_global,
        "perun_project_facts": perun_project_facts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate Perun facts onto the control-plane project")
    parser.add_argument("--delete-duplicates", action="store_true",
                        help="delete global rows that already exist on the perun project")
    args = parser.parse_args()
    print(json.dumps(migrate(delete_duplicates=args.delete_duplicates), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
