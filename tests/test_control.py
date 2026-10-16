"""Control-plane (orchestrator) behaviour.

The orchestrator is the self-registered ``Perun`` project. It must always exist,
be recognised by path, be protected from removal, and carry its custom
instructions + persona into a new session's prompt.
"""

from __future__ import annotations

from perun import config, control, orchestrator, registry
from perun.orchestrator import compose_prompt


def test_ensure_registers_the_control_project(control_repo):  # noqa: ARG001
    proj = control.ensure()
    assert proj["slug"] == "perun"
    assert control.is_control(proj)
    # Idempotent — a second call returns the same row.
    again = control.ensure()
    assert again["id"] == proj["id"]


def test_is_control_is_path_based(control_repo):  # noqa: ARG001
    assert control.is_control(control.ensure()) is True
    assert control.is_control(registry.add_project("Other", r"C:\other")) is False
    assert control.is_control(None) is False
    assert control.is_control({}) is False


def test_resolve_defaults_to_control(control_repo):  # noqa: ARG001
    assert control.is_control(control.resolve(None))
    assert control.is_control(control.resolve("does-not-exist"))
    other = registry.add_project("Other", r"C:\other")
    assert control.resolve(other["slug"])["id"] == other["id"]


def test_control_persona_injected_into_new_session(control_repo):  # noqa: ARG001
    proj = control.ensure()
    out = compose_prompt(proj, "List my projects", new_session=True)
    assert "orchestrator" in out.lower()
    assert out.endswith("List my projects")
    # Continuations do not repeat the persona.
    assert compose_prompt(proj, "again", new_session=False) == "again"


def test_custom_orchestrator_instructions_are_injected(control_repo, tmp_path, monkeypatch):  # noqa: ARG001
    proj = control.ensure()
    monkeypatch.setattr(config, "INSTRUCTIONS_DIR", tmp_path)
    (tmp_path / "orch.md").write_text(
        "---\nname: Orch\nscope: orchestrator\nenabled: true\n---\n\nAlways reply in bullet points.\n",
        encoding="utf-8",
    )
    out = compose_prompt(proj, "hi", new_session=True)
    assert "Always reply in bullet points." in out


def test_non_control_project_gets_no_persona(control_repo, tmp_path):  # noqa: ARG001
    orchestrator.write_project_instruction_file(str(tmp_path), "Be terse.")
    other = registry.add_project("Other", str(tmp_path))
    out = compose_prompt(other, "hi", new_session=True)
    assert "Be terse." in out
    assert "machine-wide overseer" not in out


def test_project_instructions_from_file_are_injected(control_repo, tmp_path):  # noqa: ARG001
    orchestrator.write_project_instruction_file(
        str(tmp_path), "Use tabs, not spaces.", name="Custom")
    other = registry.add_project("FileInstr", str(tmp_path))
    out = compose_prompt(other, "hi", new_session=True)
    assert "Use tabs, not spaces." in out


def test_control_project_is_protected_from_http_delete(control_repo):  # noqa: ARG001
    from fastapi.testclient import TestClient

    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as client:
        health = client.get("/api/health").json()
        assert health["control"] == "perun"
        r = client.delete("/api/projects/perun")
        assert r.status_code == 400
        assert "cannot be removed" in r.json()["detail"]


def test_health_exposes_control_slug(control_repo):  # noqa: ARG001
    from fastapi.testclient import TestClient

    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as client:
        body = client.get("/api/health").json()
        assert "control" in body and body["control"]


def test_models_endpoint_is_structured(control_repo):  # noqa: ARG001
    from fastapi.testclient import TestClient

    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as client:
        body = client.get("/api/models").json()
        assert "models" in body and "providers" in body and "count" in body
        for m in body["models"]:
            assert set(m) >= {"id", "provider", "name"}


def test_run_without_project_targets_control(control_repo, monkeypatch):  # noqa: ARG001
    from fastapi.testclient import TestClient

    from perun import server

    server.SETTINGS.auto_discover_on_start = False
    captured: dict = {}

    def fake_start(ref, prompt, **kw):  # noqa: ANN001, ANN003
        captured["ref"] = ref
        return {"run_uid": "runX", "status": "queued"}

    monkeypatch.setattr(server.ORCH, "start_run", fake_start)
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as client:
        r = client.post("/api/runs", json={"prompt": "hello"})
        assert r.status_code == 200
    assert captured["ref"] == "perun"
