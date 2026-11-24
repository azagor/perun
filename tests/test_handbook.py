"""The generated MCP tool reference must track the live FastMCP registry."""

from __future__ import annotations

from perun import handbook


def test_tool_reference_is_generated_from_the_live_registry():
    body = handbook.render_tool_reference()
    assert body.startswith("# Perun MCP tool reference")
    # A tool added long after the first generated copy must be present; an
    # empty or stale reference means the registry read failed silently.
    assert "## perun_ask_operator" in body


def test_schema_stats_measure_the_live_tools():
    stats = handbook.tool_schema_stats()
    assert stats["tools"] >= 30
    assert stats["schema_bytes"] > 0
