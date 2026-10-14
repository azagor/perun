"""Registry + database layer tests."""

from __future__ import annotations

from perun import db, registry


def test_init_db_creates_tables():
    with db.db() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("projects", "sessions", "runs", "activity", "memory",
              "doc_chunks", "settings", "bridge_links"):
        assert t in names


def test_add_get_list_update_delete_project():
    p = registry.add_project("Trading Bot", r"C:\apps\trading_bot",
                             kind="opencode", description="bot")
    assert p["slug"] == "trading-bot"
    assert p["kind"] == "opencode"
    assert p["favorite"] is False
    assert registry.get_project("trading-bot")["name"] == "Trading Bot"
    assert registry.get_project(p["id"])["slug"] == "trading-bot"

    registry.update_project("trading-bot", favorite=1, description="updated")
    got = registry.get_project("trading-bot")
    assert got["favorite"] is True
    assert got["description"] == "updated"

    assert [x["slug"] for x in registry.list_projects()] == ["trading-bot"]
    assert registry.delete_project("trading-bot") is True
    assert registry.get_project("trading-bot") is None


def test_slug_uniqueness():
    registry.add_project("Same", r"C:\a\same")
    second = registry.add_project("Same", r"C:\a\same2")
    assert second["slug"] == "same-2"


def test_get_project_by_path_is_normcase():
    registry.add_project("Proj", r"C:\Apps\Proj")
    found = registry.get_project_by_path(r"c:\apps\proj")
    assert found is not None and found["slug"] == "proj"


def test_project_dict_counts():
    p = registry.add_project("P", r"C:\p")
    registry.create_session(p["id"], "oc-1")
    registry.create_run("uid1", p["id"], "hello")
    from perun import memory
    memory.remember("a fact", project_id=p["id"], scope="project")
    detail = registry.get_project("p")
    assert detail["session_count"] == 1
    assert detail["memory_count"] == 1


def test_session_lifecycle():
    p = registry.add_project("P", r"C:\p")
    s = registry.create_session(p["id"], "oc-abc", title="first")
    assert s["status"] == "active"
    registry.update_session(s["id"], message_count=3, status="error",
                            last_error="boom")
    got = registry.get_session(s["id"])
    assert got["message_count"] == 3 and got["status"] == "error"
    assert registry.find_session_by_opencode(p["id"], "oc-abc")["id"] == s["id"]
    assert len(registry.list_sessions(p["id"])) == 1
    registry.delete_session(s["id"])
    assert registry.get_session(s["id"]) is None


def test_run_lifecycle():
    p = registry.add_project("P", r"C:\p")
    run = registry.create_run("abc123", p["id"], "do a thing",
                              model="m", agent="build")
    assert run["status"] == "queued"
    registry.update_run("abc123", status="done", exit_code=0, output="ok")
    got = registry.get_run("abc123")
    assert got["status"] == "done" and got["output"] == "ok"
    assert len(registry.list_runs(p["id"])) == 1
    assert len(registry.list_runs()) == 1


def test_activity_feed():
    p = registry.add_project("P", r"C:\p")
    registry.log_activity(p["id"], "run", "started", {"k": 1})
    registry.log_activity(None, "system", "global event")
    items = registry.list_activity(p["id"])
    assert items[0]["kind"] == "run" and items[0]["meta"] == {"k": 1}
    allitems = registry.list_activity()
    assert len(allitems) == 2


def test_bridge_links_upsert():
    p = registry.add_project("P", r"C:\p")
    registry.set_bridge_link("chat1", p["id"], None)
    link = registry.get_bridge_link("chat1")
    assert link["project_id"] == p["id"] and link["session_pk"] is None
    registry.set_bridge_link("chat1", p["id"], 7)
    assert registry.get_bridge_link("chat1")["session_pk"] == 7
