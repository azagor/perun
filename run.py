#!/usr/bin/env python3
"""Convenience launcher: ``python run.py serve`` == ``python -m perun serve``."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from perun.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
