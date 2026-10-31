"""Pre-change copies of the configs Perun rewrites.

Perun edits two kinds of ``opencode.json``: the project one (``perun-guard`` /
``perun-proposals``) and the global one (Perun's own MCP server, written by the
installer). Both used to be backed up next to the original — a project config
lives in a git repository, so a sibling ``.bak`` surfaced as an untracked file in
the Git tab and could be committed with ``git add -A``.

Copies now live under ``PERUN_HOME/backups/<label>/`` (``<label>`` is the project
slug, or ``opencode`` for the global config), are named with a second-resolution
stamp, and are pruned to the newest ``DEFAULT_KEEP``. Nothing outside the backup
directory is ever deleted by this module.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import List, Optional

from . import config

LOG = logging.getLogger("perun.backups")

DEFAULT_KEEP = 5


def backup_dir(label: str) -> Path:
    """Where copies for one label (a project slug, or ``opencode``) live."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-"
                   for c in (label or "misc").strip()) or "misc"
    return (config.BACKUPS_DIR / safe).resolve()


def _copies(directory: Path, stem: str) -> List[Path]:
    """Existing copies for *stem*, oldest first (the stamp sorts lexically)."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.is_file() and p.name.startswith(stem + "-"))


def prune(directory: Path, stem: str, keep: int = DEFAULT_KEEP) -> int:
    """Delete all but the newest *keep* copies; returns how many went."""
    if keep < 1:
        return 0
    copies = _copies(directory, stem)
    removed = 0
    for old in copies[:-keep]:
        try:
            old.unlink()
            removed += 1
        except OSError:  # pragma: no cover - a locked file is not worth failing over
            LOG.debug("could not remove an old backup: %s", old, exc_info=True)
    return removed


def save_copy(path: str | Path, *, label: str, keep: int = DEFAULT_KEEP,
              now: Optional[float] = None) -> Optional[Path]:
    """Copy *path* into the backup tree and prune older copies.

    Returns the new copy, or ``None`` when there is nothing to copy or the copy
    failed (a backup must never break the write it is protecting).
    """
    source = Path(path)
    if not source.is_file():
        return None
    directory = backup_dir(label)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
        stem = source.name
        dest = directory / f"{stem}-{stamp}"
        # Two saves inside one second must not overwrite each other.
        counter = 2
        while dest.exists():
            dest = directory / f"{stem}-{stamp}-{counter}"
            counter += 1
        shutil.copy2(source, dest)
    except OSError:
        LOG.debug("could not back up %s", source, exc_info=True)
        return None
    prune(directory, stem, keep)
    return dest
