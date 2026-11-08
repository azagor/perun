"""Telegram rich-text rendering: tags, emoji, time markers and safe chunking."""

from __future__ import annotations

from perun.telegram import PerunTelegramBridge
from perun.telegram_format import (
    badge,
    chunk_markdown,
    format_html,
    heading,
    plain_text,
    time_marker,
)


def test_badge_and_heading_keep_a_leading_glyph():
    assert badge("permission") == "🔐"
    assert badge("unknown-kind") == "🔔"
    head = heading("proposal", "Proposal", project="perun")
    assert head.startswith("📋")
    assert "**Proposal · perun**" in head


def test_inline_tags_are_supported():
    out = format_html("**bold** *italic* __under__ ~~gone~~ ||secret||")
    assert "<b>bold</b>" in out
    assert "<i>italic</i>" in out
    assert "<u>under</u>" in out
    assert "<s>gone</s>" in out
    assert "<tg-spoiler>secret</tg-spoiler>" in out


def test_html_is_escaped_before_tags_are_added():
    out = format_html("2 < 3 & 4 > 1")
    assert "2 &lt; 3 &amp; 4 &gt; 1" in out


def test_code_is_never_parsed_for_markup():
    out = format_html("```python\nprint('**not bold**')\n```")
    assert '<pre><code class="language-python">' in out
    assert "**not bold**" in out
    assert "<b>" not in out

    inline = format_html("run `a * b` now")
    assert "<code>a * b</code>" in inline
    assert "<i>" not in inline


def test_blockquotes_keep_inline_formatting():
    out = format_html("> **danger**\n> second line")
    assert "<blockquote><b>danger</b>\nsecond line</blockquote>" in out


def test_time_marker_becomes_a_live_relative_time():
    out = format_html("started " + time_marker("2026-11-07T10:00:00+00:00"))
    assert '<tg-time unix="' in out
    assert 'format="r"' in out
    assert "</tg-time>" in out


def test_plain_text_strips_every_marker():
    text = plain_text(
        "**bold** *italic* __under__ ~~gone~~ ||secret||\n"
        "> quote\n`code` [docs](https://example.com)\n"
        + time_marker("2026-11-07T10:00:00+00:00")
    )
    assert "**" not in text and "__" not in text and "~~" not in text
    assert "||" not in text
    assert "> quote" not in text
    assert "`" not in text
    assert "docs (https://example.com)" in text
    assert "tg-time" not in text


def test_nul_placeholders_in_agent_text_cannot_corrupt_the_output():
    out = format_html("before \x00ST0\x00 after **bold**")
    assert "\x00" not in out
    assert "<b>bold</b>" in out


def test_chunking_never_leaves_a_code_fence_open():
    body = "\n".join(f"line {i} " + "x" * 20 for i in range(30))
    text = "intro\n```python\n" + body + "\n```\noutro"
    chunks = chunk_markdown(text, 120)
    assert len(chunks) > 1
    for piece in chunks:
        assert len(piece) <= 150  # reopened fence adds a few chars
        assert piece.count("```") % 2 == 0


def test_chunking_prefers_paragraph_boundaries():
    text = ("a" * 40 + "\n\n") * 6
    chunks = chunk_markdown(text, 120)
    assert all(piece.endswith("a") for piece in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_send_keeps_the_keyboard_when_html_is_rejected():
    bridge = PerunTelegramBridge(None, token="test", allowed=[])
    calls = []

    def fake_api(method, **params):
        calls.append(dict(params))
        if params.get("parse_mode") == "HTML":
            return {"ok": False, "error_code": 400,
                    "description": "can't parse entities"}
        return {"ok": True, "result": {"message_id": 11}}

    bridge.api = fake_api  # type: ignore[assignment]
    markup = {"inline_keyboard": [[{"text": "OK", "callback_data": "x"}]]}

    message_id = bridge.send("1", "**hi**", reply_markup=markup)

    assert message_id == 11
    assert calls[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in calls[1]
    assert calls[1]["text"] == "hi"
    assert calls[1]["reply_markup"] == markup


def test_send_degrades_coloured_buttons_on_an_old_server():
    bridge = PerunTelegramBridge(None, token="test", allowed=[])
    calls = []

    def fake_api(method, **params):
        calls.append(dict(params))
        rows = (params.get("reply_markup") or {}).get("inline_keyboard") or []
        if any("style" in button for row in rows for button in row):
            return {"ok": False, "error_code": 400,
                    "description": "BUTTON_STYLE_INVALID"}
        return {"ok": True, "result": {"message_id": 12}}

    bridge.api = fake_api  # type: ignore[assignment]
    markup = {"inline_keyboard": [[{"text": "OK", "callback_data": "x",
                                    "style": "success"}]]}

    message_id = bridge.send("1", "plain", reply_markup=markup)

    assert message_id == 12
    assert calls[0]["reply_markup"]["inline_keyboard"][0][0]["style"] == "success"
    assert "style" not in calls[2]["reply_markup"]["inline_keyboard"][0][0]


def test_send_drops_the_keyboard_only_as_a_last_resort():
    bridge = PerunTelegramBridge(None, token="test", allowed=[])
    calls = []

    def fake_api(method, **params):
        calls.append(dict(params))
        if "reply_markup" in params:
            return {"ok": False, "error_code": 400,
                    "description": "bad reply markup"}
        return {"ok": True, "result": {"message_id": 12}}

    bridge.api = fake_api  # type: ignore[assignment]
    markup = {"inline_keyboard": [[{"text": "OK", "callback_data": "x"}]]}

    message_id = bridge.send("1", "plain", reply_markup=markup)

    assert message_id == 12
    assert "reply_markup" in calls[2]
    assert "reply_markup" not in calls[3]


def test_send_does_not_retry_a_network_failure():
    bridge = PerunTelegramBridge(None, token="test", allowed=[])
    calls = []

    def fake_api(method, **params):
        calls.append(dict(params))
        return {}                       # timeout / unreachable

    bridge.api = fake_api  # type: ignore[assignment]
    markup = {"inline_keyboard": [[{"text": "OK", "callback_data": "x"}]]}

    assert bridge.send("1", "plain", reply_markup=markup) is None
    assert len(calls) == 1              # a blind retry could duplicate it


def test_an_unchanged_card_edit_is_a_success():
    bridge = PerunTelegramBridge(None, token="test", allowed=[])
    calls = []

    def fake_api(method, **params):
        calls.append(dict(params))
        return {"ok": False, "error_code": 400,
                "description": "Bad Request: message is not modified"}

    bridge.api = fake_api  # type: ignore[assignment]

    assert bridge._edit_card("1", 5, "same text") is True
    assert len(calls) == 1              # no plain/degraded/drop retries
