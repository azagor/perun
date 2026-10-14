"""Perun command-line interface.

Usage::

    python -m perun serve [--host H] [--port P] [--reload] [--bridge]
    python -m perun bridge
    python -m perun discover [--roots a;b] [--index]
    python -m perun projects
    python -m perun add <name> <path> [--model M] [--agent A]
    python -m perun status
    python -m perun remember <text> [--project SLUG] [--global] [--topic T]
    python -m perun recall <query> [--project SLUG] [--k N] [--all]
    python -m perun index <slug> [--all]
    python -m perun import-legacy-memory [--path P]
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
    if args.bridge and settings.telegram()[0]:
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


def _start_bridge_thread() -> None:
    """Start the Telegram bridge in-process, at most once per process.

    The bridge is a **singleton**: ``getUpdates`` allows only one consumer per
    bot token, so two pollers fight with HTTP 409s. Single-instance is enforced
    machine-wide by a named mutex (:class:`perun.telegram.PidLock`), which also
    protects against a separate ``perun bridge`` process or the ``PerunBridge``
    logon task racing us. If the lock is already held we never start a second
    bridge.
    """
    global _BRIDGE_LOCK, _BRIDGE_THREAD

    from .orchestrator import Orchestrator
    from .telegram import PerunTelegramBridge, _setup_logging, PidLock

    if _BRIDGE_THREAD is not None and _BRIDGE_THREAD.is_alive():
        print("[perun] telegram bridge already running in this process")
        return

    settings = config.Settings.load()
    token, allowed = settings.telegram()
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
    _BRIDGE_THREAD = threading.Thread(target=bridge.run, daemon=True, name="perun-telegram")
    _BRIDGE_THREAD.start()
    print("[perun] telegram bridge started in-process")


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
    _print({
        "home": str(config.HOME),
        "db": str(config.DB_PATH),
        "db_exists": config.DB_PATH.exists(),
        "projects": len(projects),
        "memory": stats,
        "bridge_running": bridge_running,
        "bridge_task": bridge_task,
    })
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


def cmd_import_legacy(args: argparse.Namespace) -> int:
    _ensure()
    _print(memory.import_legacy_memory(args.path))
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
    p.set_defaults(func=cmd_status)

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

    p = sub.add_parser("import-legacy-memory",
                       help="migrate ~/.config/opencode/memory.db into Perun")
    p.add_argument("--path", default=None)
    p.set_defaults(func=cmd_import_legacy)

    p = sub.add_parser("install", help="install Perun globally into OpenCode")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-index", action="store_true")
    p.add_argument("--machine", action="store_true",
                   help="full machine setup: prerequisites, dependencies, the global "
                        "OpenCode install, and the PerunBridge logon task")
    p.add_argument("--skip-deps", action="store_true",
                   help="with --machine: do not pip install requirements.txt")
    p.add_argument("--skip-global", action="store_true",
                   help="with --machine: do not touch the global OpenCode config")
    p.add_argument("--skip-task", action="store_true",
                   help="with --machine: do not register the PerunBridge task")
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
