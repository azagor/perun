"""Tests for the cross-project dependency graph subsystem."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from perun import db, manifest, projects, registry


def _add(name: str, path: str) -> dict:
    return registry.add_project(name, path)


def _write_manifest(path: Path, data: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    manifest.save(path, data, "# test")


def test_project_dependencies_table_exists():
    with db.db() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "project_dependencies" in names


def test_sync_dependencies_creates_edges():
    a = _add("A", r"C:\a")
    b = _add("B", r"C:\b")

    res = registry.sync_dependencies(a["slug"], [
        {"slug": b["slug"], "relationship": "runtime", "reason": "B powers A"},
    ])

    assert res["synced"] == 1
    assert res["missing"] == []
    deps = registry.get_dependencies(a["slug"])
    assert len(deps) == 1
    assert deps[0]["target"]["slug"] == b["slug"]
    assert deps[0]["relationship"] == "runtime"
    assert deps[0]["reason"] == "B powers A"


def test_sync_dependencies_reports_missing_target():
    a = _add("A", r"C:\a")
    res = registry.sync_dependencies(a["slug"], [
        {"slug": "missing-project", "relationship": "sibling"},
    ])
    assert res["synced"] == 1
    assert res["missing"] == ["missing-project"]

    deps = registry.get_dependencies(a["slug"], include_missing=True)
    assert len(deps) == 1
    assert deps[0]["target"] is None

    # Missing targets are hidden by default.
    assert registry.get_dependencies(a["slug"], include_missing=False) == []


def test_sync_dependencies_replaces_old_edges():
    a = _add("A", r"C:\a")
    b = _add("B", r"C:\b")
    c = _add("C", r"C:\c")

    registry.sync_dependencies(a["slug"], [{"slug": b["slug"]}])
    registry.sync_dependencies(a["slug"], [{"slug": c["slug"]}])

    deps = registry.get_dependencies(a["slug"])
    assert {d["target"]["slug"] for d in deps} == {c["slug"]}


def test_get_dependents():
    a = _add("A", r"C:\a")
    b = _add("B", r"C:\b")
    registry.sync_dependencies(b["slug"], [{"slug": a["slug"]}])

    dents = registry.get_dependents(a["slug"])
    assert len(dents) == 1
    assert dents[0]["source"]["slug"] == b["slug"]


def test_dependency_graph_downstream():
    a = _add("A", r"C:\a")
    b = _add("B", r"C:\b")
    c = _add("C", r"C:\c")
    registry.sync_dependencies(a["slug"], [{"slug": b["slug"]}])
    registry.sync_dependencies(b["slug"], [{"slug": c["slug"]}])

    graph = registry.get_dependency_graph(a["slug"], direction="downstream")
    assert graph["direction"] == "downstream"
    assert {n["slug"] for n in graph["nodes"]} == {a["slug"], b["slug"], c["slug"]}
    assert len(graph["edges"]) == 2
    assert graph["cycle"] is None


def test_dependency_graph_upstream():
    a = _add("A", r"C:\a")
    b = _add("B", r"C:\b")
    c = _add("C", r"C:\c")
    registry.sync_dependencies(b["slug"], [{"slug": a["slug"]}])
    registry.sync_dependencies(c["slug"], [{"slug": b["slug"]}])

    graph = registry.get_dependency_graph(a["slug"], direction="upstream")
    assert graph["direction"] == "upstream"
    slugs = {n["slug"] for n in graph["nodes"]}
    assert slugs == {a["slug"], b["slug"], c["slug"]}
    # Edges go from dependency to dependent in upstream direction.
    assert {(e["from"], e["to"]) for e in graph["edges"]} == {
        (a["id"], b["id"]), (b["id"], c["id"]),
    }


def test_dependency_graph_detects_cycle():
    a = _add("A", r"C:\a")
    b = _add("B", r"C:\b")
    c = _add("C", r"C:\c")
    registry.sync_dependencies(a["slug"], [{"slug": b["slug"]}])
    registry.sync_dependencies(b["slug"], [{"slug": c["slug"]}])
    registry.sync_dependencies(c["slug"], [{"slug": a["slug"]}])

    graph = registry.get_dependency_graph(a["slug"], direction="downstream")
    assert graph["cycle"] is not None
    assert a["id"] in graph["cycle"]


def test_analyze_impact_orders_by_depth():
    a = _add("A", r"C:\a")
    b = _add("B", r"C:\b")
    c = _add("C", r"C:\c")
    d = _add("D", r"C:\d")
    # A -> B -> C; A -> D
    registry.sync_dependencies(b["slug"], [{"slug": a["slug"], "reason": "B needs A"}])
    registry.sync_dependencies(c["slug"], [{"slug": b["slug"], "reason": "C needs B"}])
    registry.sync_dependencies(d["slug"], [{"slug": a["slug"], "reason": "D needs A"}])

    impact = registry.analyze_impact(a["slug"])
    assert impact["total"] == 3
    slugs = [n["slug"] for n in impact["impacted"]]
    assert slugs.index("b") < slugs.index("c")
    assert slugs.index("d") < slugs.index("c")

    b_node = next(n for n in impact["impacted"] if n["slug"] == "b")
    assert "B needs A" in b_node["reasons"]


def test_deploy_plan_includes_dependents_and_self():
    a = _add("A", r"C:\a")
    b = _add("B", r"C:\b")
    registry.sync_dependencies(b["slug"], [
        {"slug": a["slug"], "deploy_command": "deploy-b",
         "deploy_doc": "B/DEPLOY.md", "reason": "B depends on A"},
    ])

    plan = registry.get_deploy_plan(a["slug"])
    assert plan["project"]["slug"] == a["slug"]
    items = plan["items"]
    assert len(items) == 1
    assert items[0]["project"]["slug"] == b["slug"]
    assert items[0]["deploy_command"] == "deploy-b"
    assert items[0]["deploy_doc"] == "B/DEPLOY.md"


def test_deploy_plan_skips_auto_deploy_by_default():
    a = _add("A", r"C:\a")
    b = _add("B", r"C:\b")
    registry.sync_dependencies(b["slug"], [
        {"slug": a["slug"], "deploy_command": "deploy-b", "auto_deploy": True},
    ])

    plan = registry.get_deploy_plan(a["slug"])
    assert plan["items"] == []
    assert plan["auto_deploy_skipped"] is True

    plan = registry.get_deploy_plan(a["slug"], include_auto=True)
    assert len(plan["items"]) == 1


def test_deploy_plan_returns_error_for_unknown_project():
    plan = registry.get_deploy_plan("does-not-exist")
    assert "error" in plan


def test_load_manifest_from_disk_syncs_dependencies(tmp_path):
    _write_manifest(tmp_path, {
        "name": "Lib", "slug": "lib",
        "dependencies": [
            {"slug": "consumer", "relationship": "library"},
        ],
    })
    proj = registry.add_project("Lib", str(tmp_path))
    consumer = registry.add_project("Consumer", str(tmp_path / "consumer"))

    refreshed = registry.load_manifest_from_disk(proj["slug"])
    assert refreshed is not None
    deps = registry.get_dependencies(proj["slug"])
    assert len(deps) == 1
    assert deps[0]["target"]["id"] == consumer["id"]


def test_projects_sync_manifest_syncs_dependencies(tmp_path):
    _write_manifest(tmp_path, {"name": "Svc", "slug": "svc",
                               "dependencies": [{"slug": "db"}]})
    svc = registry.add_project("Svc", str(tmp_path))
    db_proj = registry.add_project("DB", str(tmp_path / "db"))

    projects.sync_manifest(svc)
    deps = registry.get_dependencies(svc["slug"])
    assert len(deps) == 1
    assert deps[0]["target"]["id"] == db_proj["id"]


def test_manifest_normalizes_dependency_fields(tmp_path):
    _write_manifest(tmp_path, {
        "name": "Svc",
        "dependencies": [
            {"slug": "dep", "role": "runtime", "auto_deploy": 1,
             "deploy_command": "echo ok", "unknown": "kept"},
        ],
    })
    snap = manifest.load(tmp_path)
    dep = snap["data"]["dependencies"][0]
    assert dep["relationship"] == "runtime"
    assert dep["auto_deploy"] is True
    assert dep["deploy_command"] == "echo ok"
    assert dep["unknown"] == "kept"


def test_manifest_from_project_preserves_dependencies(tmp_path):
    _write_manifest(tmp_path, {
        "name": "Svc", "slug": "svc",
        "dependencies": [{"slug": "dep", "relationship": "runtime"}],
    })
    proj = registry.add_project("Svc", str(tmp_path))
    proj = registry.load_manifest_from_disk(proj["slug"])
    data = manifest.from_project(proj)
    assert data["dependencies"][0]["slug"] == "dep"


def test_cli_dependencies_json(capsys, tmp_path):
    from perun.__main__ import main

    a = _add("A", str(tmp_path / "a"))
    b = _add("B", str(tmp_path / "b"))
    registry.sync_dependencies(a["slug"], [{"slug": b["slug"]}])

    rc = main(["dependencies", a["slug"], "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project"] == a["slug"]
    assert out["dependencies"][0]["target"]["slug"] == b["slug"]


def test_cli_dependencies_impact(capsys, tmp_path):
    from perun.__main__ import main

    a = _add("A", str(tmp_path / "a"))
    b = _add("B", str(tmp_path / "b"))
    registry.sync_dependencies(b["slug"], [{"slug": a["slug"]}])

    rc = main(["dependencies", a["slug"], "--impact", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total"] == 1
    assert out["impacted"][0]["slug"] == b["slug"]


def test_cli_deploy_plan(capsys, tmp_path):
    from perun.__main__ import main

    a = _add("A", str(tmp_path / "a"))
    b = _add("B", str(tmp_path / "b"))
    registry.sync_dependencies(b["slug"], [
        {"slug": a["slug"], "deploy_command": "deploy-b"},
    ])

    rc = main(["deploy-plan", a["slug"], "--changed", "a.py", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["changed_files"] == ["a.py"]
    assert out["items"][0]["project"]["slug"] == b["slug"]
    assert out["items"][0]["deploy_command"] == "deploy-b"


@pytest.fixture
def client():
    from starlette.testclient import TestClient
    from perun import server
    return TestClient(server.app, headers={"Host": "localhost"})


def test_api_dependencies(client, tmp_path):
    a = _add("A", str(tmp_path / "a"))
    b = _add("B", str(tmp_path / "b"))
    registry.sync_dependencies(a["slug"], [{"slug": b["slug"]}])

    resp = client.get(f"/api/projects/{a['slug']}/dependencies")
    assert resp.status_code == 200
    data = resp.json()
    assert data["dependencies"][0]["target"]["slug"] == b["slug"]


def test_api_impact(client, tmp_path):
    a = _add("A", str(tmp_path / "a"))
    b = _add("B", str(tmp_path / "b"))
    registry.sync_dependencies(b["slug"], [{"slug": a["slug"]}])

    resp = client.get(f"/api/projects/{a['slug']}/impact")
    assert resp.status_code == 200
    data = resp.json()
    assert data["analysis"]["total"] == 1


def test_api_deploy_plan(client, tmp_path):
    a = _add("A", str(tmp_path / "a"))
    b = _add("B", str(tmp_path / "b"))
    registry.sync_dependencies(b["slug"], [
        {"slug": a["slug"], "deploy_command": "deploy-b"},
    ])

    resp = client.get(f"/api/projects/{a['slug']}/deploy-plan?changed=a.py")
    assert resp.status_code == 200
    data = resp.json()
    assert data["plan"]["changed_files"] == ["a.py"]
    assert data["plan"]["items"][0]["deploy_command"] == "deploy-b"
