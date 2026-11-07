"""Telegram rich text: one visual grammar for every message Perun sends.

Telegram has **no coloured text**.  What it does have, since Bot API 9.4, is
coloured *buttons* (``style`` = ``success`` / ``primary`` / ``danger``) and a
rich HTML subset: bold, italic, underline, strikethrough, spoiler, inline code,
pre blocks, links, ``<blockquote>``, expandable blockquotes and ``<tg-time>``
(live relative timestamps).  This module is the single place that turns the
Markdown-ish text Perun composes into that HTML, and the single place that
defines the emoji vocabulary.

Design rules
------------
* **One leading glyph per card, never the only signal.**  Every badge is paired
  with a word in the card header ("✅ Done", "🔐 Permission"), so a client that
  drops emoji still reads correctly.
* **Chunking happens before formatting**, because Telegram counts characters
  after entities are parsed.  :func:`chunk_markdown` never leaves a fenced code
  block open in the middle of a message.
* **No secret is ever formatted here.**  Callers redact first; this module only
  escapes HTML and adds tags.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import List, Optional

MAX_CHARS = 4096

#: The status vocabulary.  Keep it small and reuse it everywhere; a card gets a
#: single leading badge, not a row of unrelated emoji.
BADGES = {
    "ok": "✅",
    "done": "✅",
    "failed": "❌",
    "error": "❌",
    "working": "⏳",
    "queued": "📥",
    "stopped": "🛑",
    "warning": "⚠️",
    "blocked": "⛔",
    "permission": "🔐",
    "form": "📝",
    "proposal": "📋",
    "room": "💬",
    "question": "❓",
    "session": "❓",
    "handoff": "🤝",
    "digest": "🗂",
    "orchestrator": "🛰",
    "tool": "🔧",
    "memory": "🧠",
    "usage": "📊",
    "health": "🩺",
    "steer": "⚡",
    "undo": "↩",
    "project": "📁",
}


def badge(kind: str) -> str:
    """The glyph for a status/kind, or a neutral bell for an unknown one."""
    return BADGES.get(str(kind or "").strip().lower(), "🔔")


def heading(kind: str, title: str, *, project: str = "") -> str:
    """A card header: glyph, bold title, optional project suffix.

    The glyph stays first so older code/tests that key off the first character
    keep working, while the title is the only bold run on the line.
    """
    suffix = f" · {project}" if project else ""
    return f"{badge(kind)} **{title}{suffix}**"


# --------------------------------------------------------------------------- #
# Relative time markers
# --------------------------------------------------------------------------- #
_TIME_MARKER = re.compile(r"\{\{time:([^|}]+)(?:\|([a-zA-Z]+))?\}\}")


def time_marker(iso: str, fmt: str = "r") -> str:
    """A ``{{time:ISO|fmt}}`` marker that :func:`format_html` turns into
    ``<tg-time>`` (live relative time), with a plain fallback for old clients."""
    raw = str(iso or "").strip()
    if not raw:
        return ""
    return "{{time:" + raw + "|" + (fmt or "r") + "}}"


def _unix(iso: str) -> Optional[int]:
    raw = str(iso or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return int(dt.timestamp())
    except (OverflowError, OSError, ValueError):
        return None


def relative_time(iso: str) -> str:
    """A compact fallback label ("3m ago") for clients without ``tg-time``."""
    unix = _unix(iso)
    if unix is None:
        return ""
    delta = max(0, int(time.time()) - unix)
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _time_tag(match: "re.Match[str]") -> str:
    iso, fmt = match.group(1), (match.group(2) or "r")
    unix = _unix(iso)
    if unix is None:
        return match.group(1)
    fallback = relative_time(iso) or match.group(1)
    return f'<tg-time unix="{unix}" format="{fmt}">{_esc(fallback)}</tg-time>'


# --------------------------------------------------------------------------- #
# HTML formatting
# --------------------------------------------------------------------------- #
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _quote_body(raw: str) -> str:
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            stripped = stripped[1:]
            if stripped.startswith(" "):
                stripped = stripped[1:]
        lines.append(stripped)
    return "\n".join(lines)


def _inline(md: str) -> str:
    """Escape and apply the inline tags (bold/italic/underline/strike/link)."""
    md = _esc(md)
    md = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", md, flags=re.S)
    md = re.sub(r"__(?=\S)(.+?)(?<=\S)__(?![\w_])", r"<u>\1</u>", md, flags=re.S)
    md = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", md, flags=re.S)
    md = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>",
                md, flags=re.S)
    md = re.sub(r"^#{1,6}\s*(.+?)\s*$", r"<b>\1</b>", md, flags=re.M)
    md = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', md)
    return md


def format_html(md: str) -> str:
    """Convert Perun's Markdown-ish text to Telegram HTML.

    Supported beyond the original subset: ``__underline__``, ``~~strike~~``,
    ``||spoiler||``, line blockquotes (``> text``) and ``{{time:ISO}}`` markers.
    Unknown/unclosed markup is left as literal text rather than producing an
    unbalanced tag (Telegram rejects the whole message in that case).
    """
    text = (md or "").replace("\r\n", "\n")
    # One stash namespace, one index: block content, inline code, spoilers,
    # blockquotes and time markers must never collide on a placeholder.
    stash: List[tuple] = []

    def _take(kind: str, pattern: str, flags: int = 0):
        def _repl(m: "re.Match[str]") -> str:
            stash.append((kind, m))
            return f"\x00ST{len(stash) - 1}\x00"

        return lambda s: re.sub(pattern, _repl, s, flags=flags)

    take = _take
    text = take("block", r"```([a-zA-Z0-9_+\-]*)\n?(.*?)```", flags=re.S)(text)
    text = take("inline", r"`([^`\n]+)`")(text)
    text = take("spoiler", r"\|\|([^|\n]+)\|\|")(text)

    # Blockquotes: consecutive lines starting with ``>``.  Their bodies are
    # inline-formatted after escaping; the stash keeps the tags out of the
    # outer pass.
    def _take_quote(m: "re.Match[str]") -> str:
        stash.append(("quote", _quote_body(m.group(0))))
        return f"\x00ST{len(stash) - 1}\x00"

    text = re.sub(r"(?m)(?:^>[^\n]*(?:\n|$))+", _take_quote, text)

    # Time markers survive escaping; they are replaced at the end.
    text = take("time", r"\{\{time:([^|}]+)(?:\|([a-zA-Z]+))?\}\}")(text)

    out = _inline(text)

    # Quotes first: their raw bodies still contain the placeholders of the
    # code/spoiler/time entries stashed before them, which the later passes
    # replace globally — inserting a quote after those passes would escape the
    # generated tags instead.
    for index, (kind, payload) in enumerate(stash):
        placeholder = f"\x00ST{index}\x00"
        if kind != "quote":
            continue
        out = out.replace(placeholder, f"<blockquote>{_inline(payload)}</blockquote>")

    for index, (kind, payload) in enumerate(stash):
        placeholder = f"\x00ST{index}\x00"
        if kind == "quote":
            continue
        if kind == "time":
            out = out.replace(placeholder, _time_tag(payload))
        elif kind == "spoiler":
            out = out.replace(
                placeholder,
                f"<tg-spoiler>{_esc(payload.group(1))}</tg-spoiler>",
            )
        elif kind == "inline":
            out = out.replace(
                placeholder, f"<code>{_esc(payload.group(1))}</code>"
            )
        elif kind == "block":
            lang, code = payload.group(1), payload.group(2)
            cls = f' class="language-{lang}"' if lang else ""
            out = out.replace(
                placeholder, f"<pre><code{cls}>{_esc(code)}</code></pre>"
            )
    return out


# --------------------------------------------------------------------------- #
# Plain-text fallback and chunking
# --------------------------------------------------------------------------- #
def plain_text(md: str) -> str:
    """Strip Perun's markup for the plain-text retry after Telegram rejects HTML."""
    text = str(md or "").replace("\r\n", "\n")
    text = re.sub(r"```[a-zA-Z0-9_+\-]*\n?(.*?)```", r"\1", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"\1", text, flags=re.S)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"\1", text, flags=re.S)
    text = re.sub(r"\|\|([^|\n]+)\|\|", r"\1", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", text,
                  flags=re.S)
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^>\s?", "", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1 (\2)", text)
    text = _TIME_MARKER.sub(lambda m: relative_time(m.group(1)) or m.group(1), text)
    text = text.replace("\x00", "")
    return text


def _cut_point(text: str, size: int) -> int:
    """Best boundary at or before *size*, preferring blank lines outside fences."""
    in_fence = False
    best = 0
    best_score = -1
    pos = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        end = pos + len(line)
        if end > size:
            break
        if not in_fence:
            score = 2 if not line.strip() else 1
            if score >= best_score:
                best, best_score = end, score
        pos = end
    return best or size


def _open_fence(text: str) -> str:
    """The opening fence line if *text* ends inside a code block, else ``""``."""
    fence = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            fence = "" if fence else stripped
    return fence


def chunk_markdown(text: str, size: int = MAX_CHARS) -> List[str]:
    """Split Markdown at safe boundaries and keep fenced code blocks balanced."""
    rest = str(text or "").replace("\r\n", "\n")
    if not rest:
        return [""]
    out: List[str] = []
    while len(rest) > size:
        cut = _cut_point(rest, size)
        piece, rest = rest[:cut].rstrip(), rest[cut:].lstrip("\n")
        fence = _open_fence(piece)
        if fence:
            lang = fence[3:].strip()
            piece += "\n```"
            rest = "```" + (lang + "\n" if lang else "\n") + rest
        out.append(piece)
    if rest:
        rest = rest.rstrip()
        if rest:
            out.append(rest)
    return out


__all__ = [
    "BADGES",
    "MAX_CHARS",
    "badge",
    "heading",
    "time_marker",
    "relative_time",
    "format_html",
    "plain_text",
    "chunk_markdown",
]
