"""Tests for the deterministic message router (no model, no I/O)."""

from __future__ import annotations

import pytest

from perun import routing
from perun.routing import RouteDecision, detect_action, is_addressed, resolve_project, route

# Mirrors the live registry, including the traps: trading-bot is displayed as
# "Perun The Trader", perunchat tags itself "perun", and most tags are generic.
PROJECTS = [
    {
        "slug": "ai-playground",
        "name": "ai_playground",
        "tags": "agents_md,codemap,docs,manifest,opencode,tests",
    },
    {
        "slug": "parental-control",
        "name": "Parental Control",
        "tags": "python,fastapi,windows,audio,websocket,privacy",
    },
    {
        "slug": "perun",
        "name": "Perun",
        "tags": "opencode,orchestrator,memory,dashboard,telegram,mcp,perunchat",
    },
    {
        "slug": "perun-in-the-machine",
        "name": "Perun In The Machine",
        "tags": "website,company,marketing,nginx,html,css,javascript,perunchat",
    },
    {
        "slug": "trading-bot",
        "name": "Perun The Trader",
        "tags": "android,kivy,trading,crypto,buildozer",
    },
    {"slug": "perunchat", "name": "PerunChat", "tags": "chat,federation,agents,websocket,perun,mcp,sdk"},
]

SLUGS = {p["slug"] for p in PROJECTS}


# --- project resolution ----------------------------------------------------


def test_exact_slug_resolves() -> None:
    slug, candidates, matched_by, confidence = resolve_project("uruchom testy w perunie", PROJECTS)
    assert slug == "perun"
    assert candidates == ()
    assert matched_by == "stem"


def test_slug_word_matches_exactly() -> None:
    slug, candidates, matched_by, _ = resolve_project("status perun", PROJECTS)
    assert slug == "perun"
    assert matched_by == "slug"
    assert candidates == ()


def test_longer_project_wins_over_prefix() -> None:
    slug, _, _, _ = resolve_project("deploy perun in the machine", PROJECTS)
    assert slug == "perun-in-the-machine"


def test_perunchat_does_not_collide_with_perun() -> None:
    slug, candidates, _, _ = resolve_project("zrestartuj perunchat", PROJECTS)
    assert slug == "perunchat"
    assert candidates == ()


def test_polish_inflections_resolve() -> None:
    for text, expected in [
        ("zapamietaj to dla peruna", "perun"),
        ("w perunie jest blad", "perun"),
        ("rozmawialem z perunem", "perun"),
        ("zrestartuj perunchatu", "perunchat"),
        ("napraw tradingu", "trading-bot"),
    ]:
        slug, candidates, matched_by, _ = resolve_project(text, PROJECTS)
        assert slug == expected, (text, slug, candidates, matched_by)
        assert matched_by in {"slug", "stem"}


def test_unrelated_word_does_not_match() -> None:
    slug, candidates, matched_by, confidence = resolve_project("peruwianski alfabet", PROJECTS)
    assert slug is None
    assert candidates == ()
    assert matched_by == "none"
    assert confidence == 0.0


def test_two_projects_are_ambiguous() -> None:
    slug, candidates, matched_by, _ = resolve_project("porownaj perun i perunchat", PROJECTS)
    assert slug is None
    assert set(candidates) == {"perun", "perunchat"}
    assert matched_by == "ambiguous"


def test_display_name_resolves() -> None:
    slug, _, matched_by, _ = resolve_project("perun the trader", PROJECTS)
    assert slug == "trading-bot"
    assert matched_by == "name"


def test_unique_tag_is_an_alias() -> None:
    slug, _, matched_by, confidence = resolve_project("sprawdz crypto", PROJECTS)
    assert slug == "trading-bot"
    assert matched_by == "tag"
    assert confidence == pytest.approx(0.95)


def test_generic_tags_do_not_match() -> None:
    for text in ("napraw testy", "zaktualizuj dokumentacje docs", "sprawdz opcode opencode"):
        slug, candidates, _, _ = resolve_project(text, PROJECTS)
        assert slug is None, (text, slug)
        assert candidates == ()


def test_ambiguous_tag_is_ignored() -> None:
    # "perun" is a tag on perunchat but is also another project's slug.
    slug, candidates, _, _ = resolve_project("perun", PROJECTS)
    assert slug == "perun"
    assert candidates == ()


def test_tags_may_be_a_list() -> None:
    projects = [{"slug": "x-proj", "name": "X", "tags": ["distinctive", "docs"]}]
    slug, _, matched_by, _ = resolve_project("uzyj distinctive", projects)
    assert (slug, matched_by) == ("x-proj", "tag")


# --- action detection ------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("status", "status"),
        ("pokaz status", "status"),
        ("co slychac", "status"),
        ("zatrzymaj", "stop"),
        ("stop", "stop"),
        ("anuluj to", "stop"),
        ("zapamietaj, ze lubie rano", "remember"),
        ("zapamiętaj że wolę rano", "remember"),
        ("przypomnij co ustalilismy", "recall"),
        ("co wiesz o perunie", "recall"),
        ("uruchom testy", "run"),
        ("napraw blad w tradingu", "run"),
        ("fix the build", "run"),
        ("wdroz na produkcje", "run"),
        ("jak dziala router?", "ask"),
        ("what does the router do", "ask"),
        ("uruchom testy?", "run"),  # imperative beats the question mark
        ("", "unknown"),
        ("👍", "unknown"),
        ("a co z tym deployem", "ask"),
    ],
)
def test_detect_action(text: str, expected: str) -> None:
    assert detect_action(text) == expected


def test_stop_loss_is_not_a_stop_command() -> None:
    # Domain term, not an imperative: must stay a run/ask, never cancel.
    assert detect_action("dodaj stop loss do trading-bota") != "stop"
    assert detect_action("ustaw stop limit na 5%") != "stop"


# --- route() policy --------------------------------------------------------


def test_route_run_with_explicit_project() -> None:
    decision = route("uruchom testy w perunie", projects=PROJECTS, current="trading-bot")
    assert decision == RouteDecision(
        action="run",
        project="perun",
        confidence=0.8,
        matched_by="stem",
        reason="matched stem 'perun'",
    )


def test_route_run_without_project_uses_current() -> None:
    decision = route("uruchom testy", projects=PROJECTS, current="trading-bot")
    assert decision.action == "run"
    assert decision.project == "trading-bot"
    assert decision.matched_by == "current"


def test_route_run_without_project_or_current() -> None:
    decision = route("uruchom testy", projects=PROJECTS, current=None)
    assert decision.action == "run"
    assert decision.project is None
    assert decision.matched_by == "none"
    assert decision.confidence == 0.0


def test_route_ambiguous_project_clarifies() -> None:
    decision = route("uruchom testy w perun i perunchat", projects=PROJECTS, current="perun")
    assert decision.action == "clarify"
    assert decision.project is None
    assert set(decision.candidates) == {"perun", "perunchat"}
    assert "ambiguous" in decision.reason


def test_route_ambiguous_read_only_also_clarifies() -> None:
    decision = route("status perun i perunchat", projects=PROJECTS)
    assert decision.action == "clarify"
    assert set(decision.candidates) == {"perun", "perunchat"}


def test_route_unknown_is_unchanged_behaviour() -> None:
    decision = route("dzieki, do zobaczenia", projects=PROJECTS, current="perun")
    assert decision.action == "unknown"
    assert decision.project == "perun"


def test_route_question_without_project_stays_ask() -> None:
    decision = route("a co z tym deployem", projects=PROJECTS, current="perun")
    assert (decision.action, decision.project) == ("ask", "perun")


def test_route_remember_without_project_uses_current() -> None:
    decision = route("zapamietaj ze wole rano", projects=PROJECTS, current="perun")
    assert (decision.action, decision.project) == ("remember", "perun")


def test_route_empty_text() -> None:
    decision = route("   ", projects=PROJECTS)
    assert (decision.action, decision.project) == ("unknown", None)


def test_route_is_json_safe() -> None:
    payload = route("uruchom testy w perunie", projects=PROJECTS).as_dict()
    assert payload == {
        "action": "run",
        "project": "perun",
        "confidence": 0.8,
        "matched_by": "stem",
        "candidates": [],
        "reason": "matched stem 'perun'",
    }


# --- addressed detection ---------------------------------------------------


def test_addressed_by_mention() -> None:
    assert is_addressed("@peruntheoverseer zrob status", agent_ids=["PerunTheOverseer"])


def test_addressed_generic_perun() -> None:
    assert is_addressed("@perun status")


def test_not_addressed_between_peers() -> None:
    assert not is_addressed("frambo: sprawdzilem te pliki", agent_ids=["peruntheoverseer"])


def test_direct_message_is_addressed() -> None:
    assert is_addressed("cokolwiek", is_dm=True)


def test_command_is_addressed() -> None:
    assert is_addressed("/status")


# --- normalization ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Trading-Bot", "trading bot"),
        ("trading_bot", "trading bot"),
        ("  Perun   IN the   machine ", "perun in the machine"),
        ("PERUŃ", "peruń"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert routing.normalize(raw) == expected


def test_palatalised_inflection_resolves() -> None:
    # "perunchat" -> "perunchacie" swaps t for ci at the stem boundary.
    slug, candidates, matched_by, _ = resolve_project("co wiesz o perunchacie", PROJECTS)
    assert slug == "perunchat"
    assert matched_by == "stem"
    assert candidates == ()


def test_change_verbs_are_runs() -> None:
    for text in ("dodaj stop loss do trading-bota", "zmien limit w parental-control", "usun stary plik w perunie"):
        decision = route(text, projects=PROJECTS)
        assert decision.action == "run", text


def test_hyphenated_slug_with_inflected_tail() -> None:
    for text in ("dodaj stop loss do trading-bota", "sprawdz trading-bocie"):
        decision = route(text, projects=PROJECTS)
        assert decision.project == "trading-bot", text
        assert decision.matched_by == "inflected"
