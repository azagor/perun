"""Perun command-line interface.

Usage::

    python -m perun serve [--host H] [--port P] [--reload] [--bridge]
    python -m perun bridge
    python -m perun discover [--roots a;b] [--index]
    python -m perun projects
    python -m perun add <name> <path> [--model M] [--agent A]
    python -m perun status [--json]
    python -m perun encrypt-config [--password PWD]
    python -m perun remember <text> [--project SLUG] [--global] [--topic T]
    python -m perun recall <query> [--project SLUG] [--k N] [--all]
    python -m perun index <slug> [--all]
    python -m perun run <slug> [prompt] [--model M] [--agent A] [--title T] [--wait]
    python -m perun import-legacy-memory [--path P]
    python -m perun backfill-docs [slug] [--all]
    python -m perun validate-docs [slug] [--all]
    python -m perun refresh-deps [slug] [--all]
    python -m perun install [--uninstall] [--machine]
    python -m perun mcp
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any

from . import config, db, discovery, memory, registry


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _ensure() -> None:
    db.init_db()
    config.ensure_dirs()


def _force_utf8_stdio() -> None:
    """Make stdout/stderr tolerate non-ASCII text on legacy Windows consoles.

    The default console encoding on Windows is often ``cp1252``; printing a
    retrieved memory excerpt that contains a character such as ``→`` would then
    raise ``UnicodeEncodeError``. Reconfiguring to UTF-8 (replacing what the
    console cannot render) keeps the CLI's output robust. Best-effort: some
    streams (pytest capture, redirected handles) do not support reconfiguring.
    """

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _roots(arg: str | None) -> list[str]:
    if not arg:
        return config.Settings.load().roots
    sep = ";" if ";" in arg else os.pathsep
    return [p.strip() for p in arg.split(sep) if p.strip()]


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_serve(args: argparse.Namespace) -> int:
    _ensure()
    settings = config.Settings.load()
    if args.bridge:
        _start_bridge_thread()
    from .server import serve

    serve(host=args.host, port=args.port, reload=args.reload)
    return 0


# Process-lifetime references for the in-process Telegram bridge. The lock must
# stay alive so its named-mutex handle is never released early (see
# ``_start_bridge_thread``), and the thread reference lets a second call detect
# that the bridge is already up in this process.
_BRIDGE_LOCK = None
_BRIDGE_THREAD = None
_IN_PROCESS_BRIDGE = None


def _start_bridge_thread() -> None:
    """Start the Telegram bridge in-process, at most once per *machine*.

    The bridge is a **singleton**: ``getUpdates`` allows only one consumer per
    bot token, so two pollers fight with HTTP 409s. We guard against duplicates
    in three layers:

    1. Process-level: do not start a second thread in the same dashboard process.
    2. Machine-level: :class:`perun.telegram.PidLock` (named mutex on Windows)
       prevents another ``perun bridge``/``PerunBridge`` task from racing us.
    3. Network-level: Telegram returns HTTP 409 if another poller is active; the
       bridge loop backs off rather than fighting.

    If no Telegram token is configured, the bridge is silently skipped. If the
    lock is already held, we log that the bridge is already running elsewhere
    and do not start a second poller.
    """
    global _BRIDGE_LOCK, _BRIDGE_THREAD, _IN_PROCESS_BRIDGE

    from .orchestrator import Orchestrator
    from .telegram import PerunTelegramBridge, _setup_logging, PidLock

    if _BRIDGE_THREAD is not None and _BRIDGE_THREAD.is_alive():
        print("[perun] telegram bridge already running in this process")
        return

    settings = config.Settings.load()
    token, allowed = settings.telegram()
    if not token:
        print("[perun] no telegram token configured; bridge not started")
        return

    _setup_logging(config.LOGS_DIR / "telegram.log")
    lock = PidLock(config.HOME / "telegram.pid")
    if not lock.acquire():
        print("[perun] telegram bridge already running; not starting a second one")
        return
    # Keep the lock referenced for the whole process lifetime. PidLock owns a
    # Windows named-mutex handle; if the object were garbage-collected the
    # handle could close and a later `perun bridge` could acquire the same name,
    # producing a second poller.
    _BRIDGE_LOCK = lock
    bridge = PerunTelegramBridge(Orchestrator(settings), token=token, allowed=allowed)
    _IN_PROCESS_BRIDGE = bridge
    _BRIDGE_THREAD = threading.Thread(target=bridge.run, daemon=True, name="perun-telegram")
    _BRIDGE_THREAD.start()
    print("[perun] telegram bridge started in-process")


def reload_in_process_bridge() -> dict[str, Any]:
    """Reload the in-process bridge with the current settings token.

    Returns ``{reloaded: true}`` when the bridge was updated, otherwise
    ``{reloaded: false, reason: ...}``. This lets the dashboard apply a new
    token/password without closing the window.
    """
    global _BRIDGE_THREAD, _IN_PROCESS_BRIDGE
    settings = config.Settings.load()
    token, allowed = settings.telegram()
    if not token:
        return {"reloaded": False, "reason": "no telegram token configured"}

    if _IN_PROCESS_BRIDGE is None or (_BRIDGE_THREAD is None) or not _BRIDGE_THREAD.is_alive():
        return {"reloaded": False, "reason": "no in-process bridge"}

    _IN_PROCESS_BRIDGE.update_token(token)
    _IN_PROCESS_BRIDGE.allowed = allowed
    return {"reloaded": True}


def start_or_reload_in_process_bridge() -> dict[str, Any]:
    """Start the in-process bridge if it is not running, otherwise reload it.

    Used after a password unlock so the bridge picks up the newly available
    decrypted token. Returns ``{started: true}`` or ``{reloaded: true}``.
    """
    global _IN_PROCESS_BRIDGE
    if _IN_PROCESS_BRIDGE is None:
        _start_bridge_thread()
        return {"started": _IN_PROCESS_BRIDGE is not None}
    return reload_in_process_bridge()


def cmd_bridge(args: argparse.Namespace) -> int:
    _ensure()
    from .orchestrator import Orchestrator
    from .telegram import run_bridge

    return run_bridge(Orchestrator())


def cmd_discover(args: argparse.Namespace) -> int:
    _ensure()
    roots = _roots(args.roots)
    result = discovery.discover_and_sync(roots, max_depth=args.depth)
    if args.index:
        for slug in result.get("created", []) + result.get("updated", []):
            proj = registry.get_project(slug)
            if proj:
                memory.index_project_docs(proj["id"])
    _print({k: v for k, v in result.items() if k != "found"})
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    _ensure()
    for p in registry.list_projects():
        fav = "*" if p.get("favorite") else " "
        print(f"{fav} {p['slug']:<24} {p['kind']:<9} {p['path']}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    _ensure()
    path = os.path.abspath(args.path)
    existing = registry.get_project_by_path(path)
    if existing:
        project = registry.update_project(
            existing["slug"], name=args.name,
            model=args.model or "", agent=args.agent or "",
        )
        _print({"ok": True, "created": False, "project": project})
        return 0
    project = registry.add_project(args.name, path, model=args.model or "",
                                   agent=args.agent or "")
    registry.log_activity(project["id"], "discover", "Project registered via CLI")
    _print({"ok": True, "created": True, "project": project})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _ensure()
    projects = registry.list_projects()
    stats = memory.stats()
    try:
        from .telegram import PidLock
        bridge_running = PidLock.is_held(config.HOME / "telegram.pid")
    except Exception:
        bridge_running = None
    try:
        from . import installer
        bridge_task = installer.bridge_task_status()
    except Exception:
        bridge_task = None
    payload = {
        "home": str(config.HOME),
        "db": str(config.DB_PATH),
        "db_exists": config.DB_PATH.exists(),
        "projects": len(projects),
        "memory": stats,
        "bridge_running": bridge_running,
        "bridge_task": bridge_task,
    }
    if getattr(args, "json", False):
        _print(payload)
    else:
        # Human-readable summary for the console.
        print(f"home:       {payload['home']}")
        print(f"db:         {payload['db']} (exists={payload['db_exists']})")
        print(f"projects:   {payload['projects']}")
        print(f"memory:     {payload['memory']}")
        print(f"bridge:     {payload['bridge_running']}")
        if payload.get("bridge_task"):
            task = payload["bridge_task"]
            print(f"bridge task: exists={task.get('exists')} raw_ok={task.get('raw_ok')}"
                  f" cmd=\"{task.get('command')}\" args=\"{task.get('arguments')}\"")
    return 0


def cmd_encrypt_config(args: argparse.Namespace) -> int:
    """Encrypt sensitive values in config.json using PERUN_PASSWORD or a prompt."""
    _ensure()
    from . import secrets as _secrets
    password = args.password or _secrets._password() or _secrets.prompt_password()
    settings = config.Settings.load()
    changed = []
    token = settings.telegram_token
    if token and not _secrets.is_encrypted(token):
        settings.telegram_token = _secrets.encrypt_value(token, password)
        changed.append("telegram_token")
    if changed:
        settings.save()
        print(f"[perun] encrypted {', '.join(changed)}")
    else:
        print("[perun] nothing to encrypt")
    return 0


def cmd_global_instructions(args: argparse.Namespace) -> int:
    _ensure()
    from . import orchestrator

    if args.clear:
        # Delete every instruction file *before* re-seeding, so the default
        # guidance block always survives a clear.
        for f in orchestrator._instruction_files():
            f.unlink()
        orchestrator._seed_default_instructions()
        print("[perun] all custom global instruction files cleared; the default block was re-seeded")
        return 0
    orchestrator._seed_default_instructions()
    if args.text:
        default_path = config.INSTRUCTIONS_DIR / "custom.md"
        text = (
            "---\nname: Custom guidance\nscope: all_projects\nenabled: true\n---\n\n"
            + args.text + "\n"
        )
        default_path.write_text(text, encoding="utf-8")
        print(f"[perun] global instructions written to {default_path}")
        return 0
    files = orchestrator._instruction_files()
    if not files:
        print("[perun] no custom global instruction files; the default block is active")
        return 0
    for f in files:
        name, scope, enabled, body = orchestrator._load_instruction_file(f)
        status = "enabled" if enabled else "disabled"
        print(f"\n--- {name} ({f.name}) [{scope}] {status} ---\n{body}\n")
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    _ensure()
    project = registry.get_project(args.project) if args.project else None
    scope = "global" if args.global_scope else ("project" if project else "global")
    pid = 0 if scope == "global" else (project["id"] if project else 0)
    res = memory.remember(args.text, project_id=pid, scope=scope,
                          topic=args.topic, source="cli")
    _print(res)
    return 0 if res.get("ok") else 1


def cmd_recall(args: argparse.Namespace) -> int:
    _ensure()
    project = registry.get_project(args.project) if args.project else None
    results = memory.unified_search(
        args.query, project["id"] if project else 0, k=args.k,
        all_projects=args.all,
    )
    if not results:
        print("(no results)")
        return 0
    for i, r in enumerate(results, 1):
        tag = "DOC" if r.get("source_type") == "doc" else "FACT"
        where = r.get("source_file") or r.get("topic") or ""
        preview = " ".join((r.get("content") or "").split())[:220]
        print(f"[{i}] {tag} score={r.get('score')} {where}")
        print(f"    {preview}")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    _ensure()
    if args.all:
        total = 0
        for p in registry.list_projects():
            res = memory.index_project_docs(p["id"])
            total += res.get("chunks", 0)
            print(f"  {p['slug']}: {res.get('chunks', 0)} chunk(s)")
        print(f"indexed {total} chunk(s) across {len(registry.list_projects())} project(s)")
        return 0
    project = registry.get_project(args.slug)
    if not project:
        print(f"no such project: {args.slug}")
        return 1
    _print(memory.index_project_docs(project["id"]))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Start an OpenCode session for a project.

    If no prompt is supplied, Perun sends a default starter prompt so the session
    is opened in the project's scope immediately.
    """
    _ensure()
    from .orchestrator import Orchestrator

    project = registry.get_project(args.slug)
    if not project:
        print(f"no such project: {args.slug}")
        return 1
    orch = Orchestrator()
    prompt = (args.prompt or "").strip()
    kwargs = {"model": args.model or "", "agent": args.agent or "",
              "title": args.title or ""}
    if args.wait:
        final = orch.run_and_wait(args.slug, prompt, **kwargs)
        run = final["run"]
        print(f"[perun] run {run['run_uid']} finished for {args.slug} [{run.get('status')}]")
        if final["reply"]:
            print(final["reply"])
        return 0 if run.get("status") == "done" else 1
    run = orch.start_run(args.slug, prompt, **kwargs)
    print(f"[perun] run {run['run_uid']} started for {args.slug}")
    _print({"ok": True, "run": run})
    return 0


def cmd_import_legacy(args: argparse.Namespace) -> int:
    _ensure()
    _print(memory.import_legacy_memory(args.path))
    return 0


def cmd_backfill_docs(args: argparse.Namespace) -> int:
    """Create missing scaffold docs/manifest for one or all projects."""
    _ensure()
    from . import scaffold

    targets = _resolve_project_targets(args.slug, args.all)
    if targets is None:
        return 1

    total_created: list[str] = []
    for proj in targets:
        try:
            created = scaffold.backfill(proj["path"], proj.get("name") or "")
        except Exception as exc:  # pragma: no cover - defensive
            print(f"{proj['slug']}: error — {exc}")
            continue
        if created:
            print(f"{proj['slug']}: created {', '.join(created)}")
            total_created.extend(f"{proj['slug']}/{c}" for c in created)
            # Re-sync manifest if perun.md was created.
            if "perun.md" in created:
                try:
                    from . import projects as _projects
                    _projects.sync_manifest(proj)
                except Exception:  # pragma: no cover - best effort
                    pass
        else:
            print(f"{proj['slug']}: nothing to create")
    print(f"backfill complete — {len(total_created)} file(s) created")
    return 0


def _resolve_project_targets(slug: str | None, all_projects: bool) -> list[dict[str, Any]] | None:
    """Resolve a CLI project argument into a list of project dicts."""
    if all_projects:
        targets = registry.list_projects()
        if not targets:
            print("no projects registered")
            return []
        return targets
    if not slug:
        proj = registry.get_project_by_path(os.getcwd())
        if not proj:
            print("no project specified and none found in the current directory")
            return None
        return [proj]
    proj = registry.get_project(slug)
    if not proj:
        print(f"no such project: {slug}")
        return None
    return [proj]


def cmd_validate_docs(args: argparse.Namespace) -> int:
    """Validate perun.md manifests for stale docs/anchors."""
    _ensure()
    from . import scaffold

    targets = _resolve_project_targets(args.slug, args.all)
    if targets is None:
        return 1

    issues = 0
    for proj in targets:
        try:
            result = scaffold.validate_manifest(proj["path"])
        except Exception as exc:  # pragma: no cover - defensive
            print(f"{proj['slug']}: error — {exc}")
            continue
        if result.get("reason"):
            print(f"{proj['slug']}: {result['reason']}")
            issues += 1
        elif result["missing_docs"] or result["missing_anchors"]:
            print(f"{proj['slug']}:")
            for m in result["missing_docs"]:
                print(f"  missing doc: {m}")
            for m in result["missing_anchors"]:
                print(f"  missing anchor: {m}")
            issues += len(result["missing_docs"]) + len(result["missing_anchors"])
        else:
            print(f"{proj['slug']}: ok")
    print(f"\nvalidation complete — {issues} issue(s) found")
    return 0 if issues == 0 else 1


def cmd_refresh_deps(args: argparse.Namespace) -> int:
    """Parse dependency files and store them as project-scoped memory facts."""
    _ensure()
    from . import discovery, scaffold

    targets = _resolve_project_targets(args.slug, args.all)
    if targets is None:
        return 1

    existing_by_path = {os.path.normcase(os.path.abspath(p["path"])): p for p in registry.list_projects()}
    total_facts = 0
    for proj in targets:
        try:
            discovery.record_dependencies(proj["id"], proj["path"])
            discovery.record_local_references(proj, existing_by_path)
            deps = scaffold.parse_dependencies(proj["path"])
            count = sum(len(v) for v in deps.values())
            print(f"{proj['slug']}: indexed {count} dependency/relationship fact(s)")
            total_facts += count
        except Exception as exc:  # pragma: no cover - defensive
            print(f"{proj['slug']}: error — {exc}")
    print(f"\nrefresh complete — {total_facts} fact(s) recorded")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    _ensure()
    from . import installer
    from .integrations import install_global

    if args.uninstall:
        if args.machine:
            installer.uninstall_machine()
        return install_global.uninstall()
    if args.machine:
        return installer.machine_install(
            skip_deps=args.skip_deps,
            skip_global=args.skip_global,
            skip_task=args.skip_task,
            task=args.task,
            skip_index=args.no_index,
            force=args.force,
        )
    return install_global.install(force=args.force, index=not args.no_index)


def cmd_mcp(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(config.REPO_ROOT / "mcp"))
    _ensure()
    from perun_mcp import main as mcp_main  # type: ignore

    mcp_main()
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="perun", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="run the HTTP + SSE service and dashboard")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--bridge", action="store_true", help="also start the Telegram bridge")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("bridge", help="run the shared Telegram bridge")
    p.set_defaults(func=cmd_bridge)

    p = sub.add_parser("discover", help="scan roots and register projects")
    p.add_argument("--roots", default=None, help="path-separator (or ';') separated roots")
    p.add_argument("--depth", type=int, default=1)
    p.add_argument("--index", action="store_true", help="index docs of touched projects")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("projects", help="list registered projects")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("add", help="register a project")
    p.add_argument("name")
    p.add_argument("path")
    p.add_argument("--model", default="")
    p.add_argument("--agent", default="")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("status", help="show Perun status")
    p.add_argument("--json", action="store_true", help="output machine-readable JSON")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("encrypt-config",
                       help="encrypt sensitive values in config.json (requires PERUN_PASSWORD)")
    p.add_argument("--password", default=None, help="password for encryption")
    p.set_defaults(func=cmd_encrypt_config)

    p = sub.add_parser("global-instructions",
                       help="show or set global Markdown instruction files")
    p.add_argument("text", nargs="?", default=None,
                   help="new instructions written to custom.md (omit to list files)")
    p.add_argument("--clear", action="store_true",
                   help="delete all custom instruction files and re-seed the default block")
    p.set_defaults(func=cmd_global_instructions)

    p = sub.add_parser("remember", help="store a fact in unified memory")
    p.add_argument("text")
    p.add_argument("--project", default=None)
    p.add_argument("--global", dest="global_scope", action="store_true")
    p.add_argument("--topic", default="general")
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("recall", help="search unified memory")
    p.add_argument("query")
    p.add_argument("--project", default=None)
    p.add_argument("--k", type=int, default=6)
    p.add_argument("--all", action="store_true", help="search every project")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("index", help="index project docs")
    p.add_argument("slug", nargs="?", default=None)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("run", help="start an OpenCode session for a project")
    p.add_argument("slug", help="project slug")
    p.add_argument("prompt", nargs="?", default="",
                   help="initial prompt (optional; a starter prompt is used if omitted)")
    p.add_argument("--model", default="")
    p.add_argument("--agent", default="")
    p.add_argument("--title", default="",
                   help="session title (default: Perun · project · run id)")
    p.add_argument("--wait", action="store_true",
                   help="wait for the run to finish and print the reply")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("import-legacy-memory",
                       help="migrate ~/.config/opencode/memory.db into Perun")
    p.add_argument("--path", default=None)
    p.set_defaults(func=cmd_import_legacy)

    p = sub.add_parser("backfill-docs",
                       help="create missing scaffold docs/manifest for project(s)")
    p.add_argument("slug", nargs="?", default=None,
                   help="project slug (defaults to current directory's project)")
    p.add_argument("--all", action="store_true",
                   help="backfill every registered project")
    p.set_defaults(func=cmd_backfill_docs)

    p = sub.add_parser("validate-docs",
                       help="check perun.md manifests for stale docs/anchors")
    p.add_argument("slug", nargs="?", default=None,
                   help="project slug (defaults to current directory's project)")
    p.add_argument("--all", action="store_true",
                   help="validate every registered project")
    p.set_defaults(func=cmd_validate_docs)

    p = sub.add_parser("refresh-deps",
                       help="parse dependency files and store them as memory facts")
    p.add_argument("slug", nargs="?", default=None,
                   help="project slug (defaults to current directory's project)")
    p.add_argument("--all", action="store_true",
                   help="refresh dependencies for every registered project")
    p.set_defaults(func=cmd_refresh_deps)

    p = sub.add_parser("install", help="install Perun globally into OpenCode")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-index", action="store_true")
    p.add_argument("--machine", action="store_true",
                   help="full machine setup: prerequisites, dependencies, and the global "
                        "OpenCode install (the PerunBridge logon task is opt-in via --task)")
    p.add_argument("--skip-deps", action="store_true",
                   help="with --machine: do not pip install requirements.txt")
    p.add_argument("--skip-global", action="store_true",
                   help="with --machine: do not touch the global OpenCode config")
    p.add_argument("--skip-task", action="store_true",
                   help="with --machine: do not register the PerunBridge task (default; kept for compatibility)")
    p.add_argument("--task", action="store_true",
                   help="with --machine: register the PerunBridge logon task (auto-start at logon)")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("mcp", help="run the Perun MCP server (stdio)")
    p.set_defaults(func=cmd_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        from . import __version__
        print(f"perun {__version__}")
        return 0
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
