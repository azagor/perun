"""CLI robustness tests.

The CLI must not crash on Windows when retrieved text contains characters the
legacy console code page (cp1252) cannot encode — e.g. the ``→`` arrow that
appears in Perun's own documentation. ``main()`` forces UTF-8 stdio; these tests
lock that in.
"""

from __future__ import annotations

import io
import sys

from perun import __main__ as cli
from perun import db


def test_force_utf8_stdio_reconfigures_cp1252(monkeypatch):
    raw = io.BytesIO()
    wrapper = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", wrapper)

    cli._force_utf8_stdio()

    # After reconfiguring, a non-cp1252 char writes as UTF-8 instead of raising.
    print("bridge \u2192 project")
    wrapper.flush()
    assert b"\xe2\x86\x92" in raw.getvalue()  # UTF-8 bytes for '→'


def test_recall_with_non_ascii_memory_does_not_crash(monkeypatch):
    raw = io.BytesIO()
    wrapper = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", wrapper)

    from perun import memory

    db.init_db()
    memory.remember("telegram bridge routes \u2192 per project",
                    project_id=0, scope="global")

    rc = cli.main(["recall", "telegram bridge route", "--all", "--k", "3"])
    wrapper.flush()
    assert rc == 0
