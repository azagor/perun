"""Tests for perun/grep.py helpers that do not require a real project tree."""

from __future__ import annotations

import pytest

from perun import grep


@pytest.mark.parametrize("pattern", [
    "(a+)+",
    "(a*)+",
    "(a+)*",
    "(a+){2,}",
    "((a+)+)",
    "((a+)+)*",
    "(\\d+)*",
    "((a+)|b)+",
])
def test_looks_catastrophic_rejects_nested_quantifiers(pattern: str):
    assert grep._looks_catastrophic(pattern) is True


@pytest.mark.parametrize("pattern", [
    "",
    "a+",
    "a*",
    "(a+)",
    "(a+)?",
    "(?:a+)|b+",
    "[a-z]+",
    "foo|bar",
    "a+b+c+",
])
def test_looks_catastrophic_accepts_safe_patterns(pattern: str):
    assert grep._looks_catastrophic(pattern) is False
