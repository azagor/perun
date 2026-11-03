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


def test_control_project_skips_local_project_instructions(control_repo):  # noqa: ARG001
    """The control plane is the overseer, not a coding session in the Perun
    repo, so it should not carry Perun's own .perun/instructions files into every
    chat turn.
    """
    orchestrator.write_project_instruction_file(
        str(control_repo), "This is Perun repo guidance.")
    proj = control.ensure()
    assert control.is_control(proj)
    out = compose_prompt(proj, "hi", new_session=True)
    assert "machine-wide overseer" in out
    assert "This is Perun repo guidance." not in out


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


def test_control_project_is_hidden_from_project_list(control_repo, monkeypatch):  # noqa: ARG001
    """The API hides the Perun project unless this install has an operator.

    The dashboard asks for hidden projects so the owner can manage the control
    plane, but a fresh install has no owner (no name, no paired chat) and must
    not see it merely because the dashboard is reachable over loopback.
    """
    from fastapi.testclient import TestClient

    from perun import server

    control.ensure()
    server.SETTINGS.auto_discover_on_start = False
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    saved = (list(server.SETTINGS.telegram_allowed_chats),
             server.SETTINGS.perun_owner_name)
    try:
        server.SETTINGS.telegram_allowed_chats = []
        server.SETTINGS.perun_owner_name = ""
        with TestClient(server.app, base_url="http://127.0.0.1:8760") as client:
            default = client.get("/api/projects").json()
            assert "perun" not in {p["slug"] for p in default["projects"]}

            # A brand-new install cannot ask its way into the hidden project.
            asked = client.get("/api/projects?include_hidden=true").json()
            assert "perun" not in {p["slug"] for p in asked["projects"]}

        # Once an operator is configured, the owner's dashboard sees it again.
        server.SETTINGS.telegram_allowed_chats = ["4242"]
        with TestClient(server.app, base_url="http://127.0.0.1:8760") as client:
            included = client.get("/api/projects?include_hidden=true").json()
            assert "perun" in {p["slug"] for p in included["projects"]}
    finally:
        (server.SETTINGS.telegram_allowed_chats,
         server.SETTINGS.perun_owner_name) = saved


def test_owner_configured_needs_a_name_or_a_paired_chat(monkeypatch):
    """A fresh install is nobody's yet; naming/pairing makes it the owner's."""
    from perun import config

    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    settings = config.Settings()
    settings.telegram_allowed_chats = []
    settings.perun_owner_name = ""
    assert settings.owner_configured() is False

    settings.perun_owner_name = "Lukasz"
    assert settings.owner_configured() is True

    settings.perun_owner_name = ""
    settings.telegram_allowed_chats = ["4242"]
    assert settings.owner_configured() is True

    settings.telegram_allowed_chats = []
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")
    assert settings.owner_configured() is True


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


def test_dashboard_shell_versions_assets_and_disables_heuristic_cache(control_repo):  # noqa: ARG001
    """A release must invalidate the browser's cached dashboard script.

    The chat-ordering fix shipped in 0.8.10 was invisible to clients whose
    browser had cached ``perun.js?v=0.7.47`` under heuristic freshness.  The
    shell now rewrites the query string to the running version and asks the
    browser to revalidate JS/CSS/HTML.
    """
    from fastapi.testclient import TestClient

    from perun import __version__, server

    server.SETTINGS.auto_discover_on_start = False
    with TestClient(server.app, base_url="http://127.0.0.1:8760") as client:
        page = client.get("/")
        assert page.status_code == 200
        assert f"perun.js?v={__version__}" in page.text
        assert "perun.js?v=0.7.47" not in page.text
        assert page.headers.get("cache-control") == "no-cache"

        script = client.get("/js/perun.js")
        assert script.status_code == 200
        assert script.headers.get("cache-control") == "no-cache"
