"""Deterministic message router.

Turns one inbound chat message into a ``RouteDecision``: what the operator wants
(``run|ask|status|remember|recall|stop``) and which project it belongs to. No
model, no network, no I/O — this is the rules tier that must keep working when
Ollama, OpenCode or the network are unavailable.

Design notes
------------

* **Slugs are the identity.** Project *tags* are only used when a tag is unique
  across the registry and not a generic word: the live registry tags every
  project with ``docs``/``tests``/``opencode``, and ``perunchat`` even carries
  ``perun`` as a tag, so matching tags blindly makes almost every message
  ambiguous.
* **Names are matched as whole phrases**, never stemmed. ``trading-bot`` is
  displayed as *"Perun The Trader"*; stemming that name would make the word
  "perun" resolve to the trading bot.
* **Stemming uses slug words only** and requires a 4+ character word, so Polish
  inflections ("peruna", "perunie", "tradingu") resolve while "perun" cannot
  swallow "perunchat" (4 extra characters is beyond the suffix budget).
* **Ambiguity is never resolved by guessing.** Two projects at the same
  confidence and span produce ``clarify`` with the candidates.
* **Unknown input is unchanged behaviour.** If no action keyword is present the
  decision is ``unknown`` and the caller keeps its existing path (an LLM turn).
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# Actions that change state or spend agent tokens.
MUTATING = frozenset({"run", "stop", "remember"})

ACTIONS = ("run", "ask", "status", "remember", "recall", "stop", "clarify", "unknown")

# Words that appear on many projects and therefore cannot identify one.
_GENERIC_TAGS = frozenset(
    {
        "agents",
        "agents_md",
        "api",
        "audio",
        "bot",
        "build",
        "chat",
        "code",
        "codemap",
        "css",
        "dashboard",
        "deployment",
        "docs",
        "fastapi",
        "gateway",
        "html",
        "hitl",
        "javascript",
        "manifest",
        "mcp",
        "memory",
        "nginx",
        "opencode",
        "orchestrator",
        "privacy",
        "python",
        "sdk",
        "security",
        "sqlite",
        "telegram",
        "tests",
        "tools",
        "web",
        "website",
        "windows",
    }
)

# Keyword sets are matched as whole tokens after normalisation. Both diacritic
# and ASCII spellings are listed because normalisation preserves Polish letters.
_KEYWORDS: dict[str, frozenset[str]] = {
    "stop": frozenset(
        {"stop", "zatrzymaj", "zatrzymac", "zatrzymać", "przerwij", "anuluj", "cancel", "halt", "kill"}
    ),
    "remember": frozenset(
        {"remember", "zapamietaj", "zapamiętaj", "zapamietac", "zapamiętać", "zapisz"}
    ),
    "recall": frozenset(
        {"recall", "przypomnij", "przypomnienie", "odszukaj"}
    ),
    "status": frozenset({"status", "stan", "state", "sytuacja", "raport"}),
    "run": frozenset(
        {
            "run",
            "uruchom",
            "uruchomić",
            "uruchomic",
            "wykonaj",
            "wykonać",
            "wykonac",
            "zrob",
            "zrób",
            "napraw",
            "naprawić",
            "naprawic",
            "fix",
            "zbuduj",
            "zbudować",
            "zbudowac",
            "build",
            "deploy",
            "wdroz",
            "wdroż",
            "wdrożyć",
            "wdrozyc",
            "przetestuj",
            "dodaj",
            "dodać",
            "dodac",
            "zmien",
            "zmień",
            "zmienic",
            "zmienić",
            "usun",
            "usuń",
            "usunac",
            "usunąć",
            "popraw",
            "poprawić",
            "poprawic",
            "zaktualizuj",
            "update",
        }
    ),
}

_QUESTION_WORDS = frozenset(
    {
        "co",
        "jak",
        "dlaczego",
        "gdzie",
        "kiedy",
        "czy",
        "kto",
        "ile",
        "what",
        "how",
        "why",
        "where",
        "when",
        "who",
        "which",
        "can",
        "is",
        "are",
        "does",
        "did",
    }
)

# Phrase triggers that are not a single token.
_RECALL_PHRASES = ("co wiesz o", "co pamietasz o", "co pamiętasz o", "what do you know about")
_STATUS_PHRASES = ("jak leci", "co slychac", "co słychać")


@dataclass(frozen=True)
class RouteDecision:
    """One routing outcome.

    ``confidence`` is the confidence of the *project* match (1.0 exact slug or
    name, 0.95 unique tag, 0.8 stem, 0.5 current-project fallback, 0.0 none).
    """

    action: str
    project: str | None
    confidence: float
    matched_by: str
    candidates: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "project": self.project,
            "confidence": round(self.confidence, 3),
            "matched_by": self.matched_by,
            "candidates": list(self.candidates),
            "reason": self.reason,
        }


def normalize(text: Any) -> str:
    """Casefold and reduce to space-separated alphanumeric words.

    Hyphens and underscores become spaces, so ``trading-bot`` and
    ``trading_bot`` normalise identically. Polish letters survive.
    """
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    cleaned = "".join(ch if ch.isalnum() else " " for ch in folded)
    return " ".join(cleaned.split())


def _tags(project: dict[str, Any]) -> list[str]:
    raw = project.get("tags") or ""
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = str(raw).split(",")
    return [item.strip() for item in items if item.strip()]


def _project_terms(
    projects: Sequence[dict[str, Any]],
) -> list[tuple[str, str, float, str]]:
    """Return ``(term, matched_by, confidence, slug)`` for every matchable term."""
    slug_words: dict[str, int] = {}
    for project in projects:
        for word in normalize(project.get("slug")).split():
            slug_words[word] = slug_words.get(word, 0) + 1

    terms: list[tuple[str, str, float, str]] = []
    for project in projects:
        slug = str(project.get("slug") or "").strip()
        if not slug:
            continue
        slug_term = normalize(slug)
        if slug_term:
            terms.append((slug_term, "slug", 1.0, slug))
        name_term = normalize(project.get("name"))
        if name_term and name_term != slug_term:
            terms.append((name_term, "name", 1.0, slug))
        for tag in _tags(project):
            tag_term = normalize(tag)
            if not tag_term or " " in tag_term or len(tag_term) < 4:
                continue
            if tag_term in _GENERIC_TAGS or tag_term in slug_words:
                continue
            # Only a tag that identifies exactly one project is an alias.
            owners = [
                str(other.get("slug"))
                for other in projects
                if tag_term in {normalize(t) for t in _tags(other)}
            ]
            if len(set(owners)) == 1:
                terms.append((tag_term, "tag", 0.95, slug))
    return terms


def _phrase_span(text_words: Sequence[str], term_words: Sequence[str]) -> tuple[int, int] | None:
    """Return ``(start_index, length)`` of the first whole-word occurrence."""
    length = len(term_words)
    if not length or length > len(text_words):
        return None
    for start in range(len(text_words) - length + 1):
        if tuple(text_words[start : start + length]) == tuple(term_words):
            return start, length
    return None


def _phrase_span_inflected(
    text_words: Sequence[str], term_words: Sequence[str]
) -> tuple[int, int] | None:
    """Match a multi-word slug whose final word carries an inflection.

    "trading-bot" must match "trading-bota"/"trading-bocie"; the earlier words
    stay exact so "perun in the machine" cannot be matched by "perun ...".
    """
    length = len(term_words)
    if length < 2 or length > len(text_words):
        return None
    for start in range(len(text_words) - length + 1):
        if tuple(text_words[start : start + length - 1]) != tuple(term_words[:-1]):
            continue
        last = text_words[start + length - 1]
        if last == term_words[-1] or _stem_match(term_words[-1], last):
            return start, length
    return None


# Polish palatalisation at the stem boundary: "perunchat" -> "perunchacie",
# "bot" -> "bocie", "krok" -> "kroku". A full stemmer is out of scope; this
# table covers the inflections project names actually take.
_PALATAL = {
    "t": "c",
    "d": "dz",
    "k": "c",
    "g": "dz",
    "r": "rz",
    "ł": "l",
    "s": "ś",
    "z": "ź",
    "n": "ń",
    "c": "cz",
}


def _stem_match(word: str, text_word: str) -> bool:
    """True when ``text_word`` is ``word`` plus an inflectional ending."""
    if len(text_word) <= len(word) or len(text_word) - len(word) > 3:
        return False
    if text_word.startswith(word):
        return True
    if len(word) < 2 or text_word[: len(word) - 1] != word[:-1]:
        return False
    alternate = _PALATAL.get(word[-1])
    if not alternate:
        return False
    return text_word[len(word) - 1 :].startswith(alternate)


def _stem_owner(
    word: str, word_owners: dict[str, set[str]]
) -> str | None:
    """Pick the project a bare stem belongs to, or ``None`` when it is ambiguous.

    ``perun`` is a word in both ``perun`` and ``perun-in-the-machine``; a bare
    "peruna"/"perunie" means the project called Perun, while the longer project
    is referenced by its phrase (or by "machine"). Prefer the slug with the
    fewest words; a tie means "ask", not "guess".
    """
    owners = word_owners.get(word) or set()
    if not owners:
        return None
    ranked = sorted(owners, key=lambda slug: (len(normalize(slug).split()), len(slug)))
    if len(ranked) > 1:
        first_words = len(normalize(ranked[0]).split())
        second_words = len(normalize(ranked[1]).split())
        if first_words == second_words:
            return None
    return ranked[0]


def resolve_project(
    text: str, projects: Sequence[dict[str, Any]]
) -> tuple[str | None, tuple[str, ...], str, float]:
    """Resolve a project mention.

    Returns ``(slug, candidates, matched_by, confidence)``. ``candidates`` is
    non-empty when two projects match equally well; callers must then clarify.
    """
    norm = normalize(text)
    words = norm.split()
    if not words:
        return None, (), "none", 0.0

    terms = _project_terms(projects)
    word_owners: dict[str, set[str]] = {}
    for project in projects:
        slug = str(project.get("slug") or "").strip()
        for word in normalize(slug).split():
            word_owners.setdefault(word, set()).add(slug)

    hits: list[tuple[float, int, int, str, str]] = []  # conf, span, start, slug, by

    for term, matched_by, confidence, slug in terms:
        term_words = term.split()
        span = _phrase_span(words, term_words)
        if span is not None:
            start, length = span
            hits.append((confidence, length, start, slug, matched_by))
            continue
        # "trading-bota", "perun-in-the-machinie": slug with an inflected tail.
        if len(term_words) > 1:
            inflected = _phrase_span_inflected(words, term_words)
            if inflected is not None:
                start, length = inflected
                hits.append((0.9, length, start, slug, "inflected"))

    # Stem pass: slug words of 4+ characters, allowing up to three trailing
    # characters of inflection ("perun" -> "peruna", "trading" -> "tradingu").
    for term, matched_by, _confidence, _slug in terms:
        if matched_by != "slug":
            continue
        for word in term.split():
            if len(word) < 4:
                continue
            owner = _stem_owner(word, word_owners)
            if owner is None:
                continue
            for index, text_word in enumerate(words):
                if text_word == word:
                    continue
                if _stem_match(word, text_word):
                    hits.append((0.8, 1, index, owner, "stem"))

    if not hits:
        return None, (), "none", 0.0

    # Best first: confidence, then longest span, then earliest position.
    hits.sort(key=lambda item: (-item[0], -item[1], item[2]))
    best_conf, best_span, _best_start, best_slug, best_by = hits[0]
    tied = {
        slug
        for conf, span, _start, slug, _by in hits
        if conf == best_conf and span == best_span
    }
    if len(tied) > 1:
        return None, tuple(sorted(tied)), "ambiguous", best_conf
    return best_slug, (), best_by, best_conf


# Phrases where a keyword is part of a domain term rather than a command.
# "dodaj stop loss do trading-bota" must not cancel a run.
_KEYWORD_BLOCKERS = {
    "stop": ("stop loss", "stop limit", "stop-loss"),
}


# Words that may precede the real command ("please run …", "proszę uruchom …").
_POLITE = frozenset({"prosze", "proszę", "please", "hej", "hey", "ok", "okay"})
_NEGATIONS = frozenset({"nie", "never", "dont", "don't", "nope"})


def _leading_tokens(tokens: Sequence[str]) -> list[str]:
    """Content tokens after an optional mention and politeness word.

    ``normalize`` strips ``@``, so ``@perun status`` arrives as ``perun status``;
    a leading bot name is skipped the same way a politeness word is.
    """
    index = 0
    while index < len(tokens) and tokens[index] in _POLITE:
        index += 1
    if index < len(tokens) and tokens[index] in {"perun", "peruntheoverseer", "overseer"}:
        index += 1
    while index < len(tokens) and tokens[index] in _POLITE:
        index += 1
    return list(tokens[index:])


def detect_action(text: str) -> str:
    """Classify the requested action from explicit keywords only.

    Commands must appear in the **leading window** (after an optional mention or
    "please"). Matching a keyword anywhere turned "dodaj komendę status" into a
    status query and stored prose that merely mentioned *zapamiętać* as a memory
    fact; those false positives suppress the agent run that was actually asked
    for. Negation ("nie uruchom testów") also yields ``unknown`` instead of
    starting a run.
    """
    norm = normalize(text)
    if not norm:
        return "unknown"
    tokens = norm.split()
    head = _leading_tokens(tokens)
    padded = f" {norm} "

    if tokens and (tokens[0] in _NEGATIONS or tokens[:2] == ["do", "not"]):
        return "unknown"

    # Phrase commands are anchored at the start of the message.
    head_text = f" {' '.join(head)} "
    for phrase in _RECALL_PHRASES:
        if head_text.startswith(f" {phrase} "):
            return "recall"
    for phrase in _STATUS_PHRASES:
        if head_text.startswith(f" {phrase} "):
            return "status"

    # A question in the opening words wins over a verb later in the sentence:
    # "jak dodać status" is a question, not a status command. Commands in the
    # leading window are checked first so "przypomnij, co ustaliliśmy" stays a
    # recall command.
    window = set(head[:2])
    for action in ("stop", "remember", "recall", "status"):
        if not (window & _KEYWORDS[action]):
            continue
        blocked = any(f" {blocker} " in padded for blocker in _KEYWORD_BLOCKERS.get(action, ()))
        if not blocked:
            return action

    if set(tokens[:3]) & _QUESTION_WORDS:
        return "ask"

    if window & _KEYWORDS["run"]:
        return "run"

    if text.strip().endswith("?"):
        return "ask"
    return "unknown"


def is_addressed(
    text: str, *, agent_ids: Iterable[str] = (), is_dm: bool = False
) -> bool:
    """True when a room message is aimed at Perun (or is a direct message)."""
    if is_dm:
        return True
    raw = str(text or "")
    if raw.lstrip().startswith("/"):
        return True
    lowered = raw.casefold()
    ids = [str(aid).strip().casefold() for aid in agent_ids if str(aid).strip()]
    for agent_id in ids:
        if f"@{agent_id}" in lowered:
            return True
    # Generic forms people actually type.
    return "@perun" in lowered


def route(
    text: str,
    *,
    projects: Sequence[dict[str, Any]],
    current: str | None = None,
) -> RouteDecision:
    """Decide what a message asks for. Pure function: no I/O, no model."""
    action = detect_action(text)
    project, candidates, matched_by, confidence = resolve_project(text, projects)

    if candidates:
        return RouteDecision(
            action="clarify",
            project=None,
            confidence=confidence,
            matched_by=matched_by,
            candidates=candidates,
            reason=f"ambiguous project: {', '.join(candidates)}",
        )

    if action == "unknown":
        return RouteDecision(
            action="unknown",
            project=project or current,
            confidence=confidence,
            matched_by=matched_by,
            reason="no action keyword",
        )

    if project is None:
        # No mention: mutating actions stay on the bound project, as today.
        return RouteDecision(
            action=action,
            project=current,
            confidence=0.5 if current else 0.0,
            matched_by="current" if current else "none",
            reason="no project mention; using current project" if current else "no project",
        )

    return RouteDecision(
        action=action,
        project=project,
        confidence=confidence,
        matched_by=matched_by,
        reason=f"matched {matched_by} '{project}'",
    )


def _main(argv: Sequence[str]) -> int:
    """Offline inspection: ``python -m perun.routing "uruchom testy w perunie"``."""
    from . import registry

    text = " ".join(argv).strip()
    if not text:
        print('usage: python -m perun.routing "<message>"', file=sys.stderr)
        return 2
    projects = registry.list_projects(include_hidden=True)
    decision = route(text, projects=projects)
    print(json.dumps(decision.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual tool
    raise SystemExit(_main(sys.argv[1:]))
