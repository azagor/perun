"""Run ``/update-docs`` after a proposal changes a project.

A shared-project proposal is executed by an agent that is **sandboxed to its
approved target paths** (``PERUN_ALLOWLIST`` + ``perun-guard`` + a post-run
filesystem audit). Documentation is outside those targets, so the executor can
never update it — which is why projects ended up with an empty
``docs/CHANGELOG.md`` while their code changed.

The docs step therefore runs on its own: after a proposal reaches ``executed``,
Perun opens a session in the project and runs the installed **``/update-docs``
command** through the same service endpoint the TUI uses
(``POST /api/session/{id}/command``). The command's own template drives what
gets written — Perun does not re-implement it.

If the command is not installed (a machine where the global install never ran),
a single deterministic CHANGELOG line is appended instead, so the changelog is
never silently empty.

Gated by ``docs_update_after_execution`` (default on). One run per project at a
time; a second executed proposal while one is in flight is skipped, not queued.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from . import config, manifest, opencode_api, registry

LOG = logging.getLogger("perun.docs")

DOCS_COMMAND = "update-docs"
DEFAULT_CHANGELOG = "docs/CHANGELOG.md"
# How long a docs step waits for the machine-wide slot before giving the
# proposal back (seconds). Short: starting a docs run is two HTTP calls.
SLOT_TIMEOUT = 30.0

_lock = threading.Lock()
_running: set[int] = set()
# Proposal ids already handled in this process: several callbacks can observe the
# same completion (dashboard, policy auto-approve, external handoff), and the
# docs step must run once.
_seen: set[str] = set()
# One docs run at a time machine-wide: doc rewrites are cheap but not free, and
# two agents editing the same CHANGELOG would fight.
_semaphore = threading.BoundedSemaphore(1)


def reset_state() -> None:
    """Forget which proposals are handled or running (tests, fresh process)."""
    with _lock:
        _seen.clear()
        _running.clear()


def enabled(settings: Optional["config.Settings"] = None) -> bool:
    """Whether the pipeline should update docs after an execution."""
    try:
        current = settings or config.Settings.load()
    except Exception:  # pragma: no cover - defensive
        return False
    return bool(getattr(current, "docs_update_after_execution", True))


def changelog_path(project: Dict[str, Any]) -> Path:
    """The changelog the project declares, or ``docs/CHANGELOG.md``.

    A declared path is honoured only when it stays inside the project: the
    manifest is project content, and a proposal can edit it, so a
    ``docs.changelog: ../../…`` must never turn the post-execution step into a
    write outside the project.
    """
    root = Path(str(project.get("path") or ""))
    declared = ""
    try:
        snap = manifest.load(str(root))
        data = snap.get("data") if isinstance(snap, dict) else None
        declared = str(((data or {}).get("docs") or {}).get("changelog") or "").strip()
    except Exception:  # pragma: no cover - a broken manifest must not block docs
        declared = ""
    if declared:
        try:
            from . import path_guard

            if path_guard.check(str(root), declared)[0]:
                return root / declared
        except Exception:  # pragma: no cover - a guard failure falls through
            pass
        LOG.warning("ignoring docs.changelog outside the project: %s", declared)
    return root / DEFAULT_CHANGELOG


def _entry(proposal: Dict[str, Any]) -> str:
    intent = " ".join(str(proposal.get("intent") or "").split())
    if len(intent) > 120:
        intent = intent[:117] + "…"
    targets = [str(t) for t in (proposal.get("targets") or [])[:5]]
    shown = ", ".join(f"`{Path(t).name}`" for t in targets)
    line = f"- {intent or 'change'}"
    if shown:
        line += f" ({shown})"
    return line + f" — proposal `{proposal.get('id')}`"


def append_changelog(project: Dict[str, Any], proposal: Dict[str, Any]) -> Optional[Path]:
    """Append one bullet under ``## Unreleased`` (idempotent per proposal id).

    The fallback for a machine without the ``/update-docs`` command, and a
    guarantee that an executed change is never invisible in the changelog.
    """
    target = changelog_path(project)
    proposal_id = str(proposal.get("id") or "")
    root = Path(str(project.get("path") or ""))
    if not root.is_dir():
        # Never recreate a project directory: a deleted project must not come
        # back as an empty shell with a changelog in it.
        LOG.debug("skipping the changelog entry: %s is not a directory", root)
        return None
    try:
        if target.is_file():
            text = target.read_text(encoding="utf-8")
        else:
            name = project.get("name") or project.get("slug") or "Project"
            text = f"# {name} - Changelog\n\nAll notable changes are recorded here.\n"
        if proposal_id and proposal_id in text:
            return None
        lines = text.replace("\r\n", "\n").split("\n")
        try:
            start = lines.index("## Unreleased")
        except ValueError:
            start = None
        entry = _entry(proposal)
        if start is None:
            # No Unreleased section: put one under the title.
            insert_at = 1 if lines and lines[0].startswith("# ") else 0
            block = ["", "## Unreleased", "", entry, ""]
            lines[insert_at:insert_at] = block
        else:
            end = next((i for i in range(start + 1, len(lines))
                        if lines[i].startswith("## ")), len(lines))
            body = lines[start + 1:end]
            # Exactly one blank line after the heading and before the next one.
            while body and not body[-1].strip():
                body.pop()
            while body and not body[0].strip():
                body.pop(0)
            body = ([""] + body + [entry, ""]) if body else (["", entry, ""])
            lines[start + 1:end] = body
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    except OSError:
        LOG.debug("could not append a changelog entry to %s", target, exc_info=True)
        return None
    return target


def command_installed(api: "opencode_api.OpenCodeAPI", project: Dict[str, Any]) -> bool:
    """Whether ``/update-docs`` is visible in this project."""
    try:
        names = {str(c.get("name") or "").lstrip("/") for c in
                 api.commands(str(project.get("path") or ""))}
    except Exception:  # pragma: no cover - an older service has no /api/command
        return False
    return DOCS_COMMAND in names


def _work(project: Dict[str, Any], proposal: Dict[str, Any],
          api: Optional["opencode_api.OpenCodeAPI"]) -> None:
    project_id = int(project["id"])
    proposal_id = str(proposal.get("id") or "")
    try:
        # A short wait absorbs a burst of executions; past that, give the
        # proposal back so a later attempt can still run its docs step.
        if not _semaphore.acquire(timeout=SLOT_TIMEOUT):
            with _lock:
                _seen.discard(proposal_id)
            LOG.info("docs: another update is running; leaving proposal %s for later",
                     proposal_id)
            return
        try:
            client = api or opencode_api.default_api()
            if not command_installed(client, project):
                path = append_changelog(project, proposal)
                registry.log_activity(
                    project_id, "docs_updated",
                    f"changelog entry for proposal {proposal.get('id')} "
                    f"(the /{DOCS_COMMAND} command is not installed)",
                    {"proposal_id": proposal.get("id"),
                     "path": str(path) if path else ""},
                )
                return
            session = client.create_session(
                str(project.get("path") or ""),
                title=f"docs: {project.get('slug') or project_id}",
            )
            session_id = str((session or {}).get("id") or "")
            if not session_id:
                raise opencode_api.OpenCodeError("could not create a docs session")
            client.run_command(session_id, DOCS_COMMAND,
                               str(project.get("slug") or ""))
            registry.log_activity(
                project_id, "docs_updated",
                f"/{DOCS_COMMAND} started for proposal {proposal.get('id')}",
                {"proposal_id": proposal.get("id"), "session_id": session_id},
            )
            LOG.info("docs: /%s started in %s (session %s)",
                     DOCS_COMMAND, project.get("slug"), session_id)
        finally:
            _semaphore.release()
    except Exception:
        LOG.debug("docs update failed for %s", project.get("slug"), exc_info=True)
        # The change is already applied: do not lose the trace of it.
        try:
            append_changelog(project, proposal)
        except Exception:  # pragma: no cover - best effort
            pass
    finally:
        with _lock:
            _running.discard(project_id)


def after_execution(project: Dict[str, Any], proposal: Dict[str, Any],
                    *, api: Optional["opencode_api.OpenCodeAPI"] = None,
                    settings: Optional["config.Settings"] = None) -> bool:
    """Start the docs step for an executed proposal (fire and forget).

    Returns True when a thread was started. Never blocks the caller: execution
    already finished, so a slow or refusing docs run must not delay it.
    """
    status = str(proposal.get("status") or "")
    if status != "executed":
        return False
    if not enabled(settings):
        return False
    proposal_id = str(proposal.get("id") or "")
    project_id = int(project["id"])
    with _lock:
        if proposal_id and proposal_id in _seen:
            return False
        if project_id in _running:
            LOG.debug("docs update already running for %s", project.get("slug"))
            return False
        if proposal_id:
            _seen.add(proposal_id)
            if len(_seen) > 500:
                # Bounded: old ids can never come back.
                _seen.difference_update(sorted(_seen)[:250])
        _running.add(project_id)
    try:
        threading.Thread(
            target=_work, args=(project, proposal, api), daemon=True,
            name=f"docs-update-{project.get('slug') or project_id}",
        ).start()
    except RuntimeError:  # pragma: no cover - thread exhaustion is rare
        # Nothing was started: do not pretend the proposal was handled.
        with _lock:
            _running.discard(project_id)
            if proposal_id:
                _seen.discard(proposal_id)
        LOG.warning("could not start the docs update for %s", project.get("slug"),
                    exc_info=True)
        return False
    return True
