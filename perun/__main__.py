"""Perun command-line interface.

Usage::

    python -m perun serve [--host H] [--port P] [--reload] [--bridge]
    python -m perun stop [--timeout S] [--force]
    python -m perun restart [--host H] [--port P] [--bridge] [--foreground] [--timeout S] [--force]
    python -m perun bridge
    python -m perun discover [--roots a;b] [--index]
    python -m perun projects
    python -m perun add <name> <path> [--model M] [--agent A]
    python -m perun project-roots [--list] [--add NAME PATH] [--remove NAME]
    python -m perun context list|get|search|sync|map|index|verify|stats [--project SLUG]
    python -m perun secret list|get|set|delete|password|status
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
from pathlib import Path
from typing import Any

from . import (config, context, daemon, db, discovery, handbook, memory,
               project_roots, registry, secret_store)
from . import secrets as crypto


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
    existing = daemon.is_another_perun_running(settings)
    if existing:
        print(f"[perun] already running on PID {existing} (http://{settings.host}:{settings.port})")
        print("[perun] use 'perun restart' to replace it or 'perun stop' to stop it")
        return 1
    daemon.write_pid()
    # Record the effective launch flags so a later `perun restart` can replay
    # them (e.g. --bridge) instead of silently dropping configuration.
    run_argv = ["serve"]
    if args.host:
        run_argv += ["--host", str(args.host)]
    if args.port:
        run_argv += ["--port", str(args.port)]
    if args.reload:
        run_argv += ["--reload"]
    if args.bridge:
        run_argv += ["--bridge"]
    try:
        daemon.record_run(run_argv)
    except OSError as exc:
        # Degrade, don't block: without a record a later `perun restart` falls
        # back to flag-only behavior (the pre-v0.7.10 status quo).
        print(f"[perun] warning: could not record launch flags for restart: {exc}")
    try:
        if args.bridge:
            _start_bridge_thread()
        from .server import serve

        serve(host=args.host, port=args.port, reload=args.reload)
    except Exception as exc:
        # If the server fails to bind (or crashes on startup), clear the PID
        # file so it doesn't falsely claim we are running.
        daemon.clear_pid()
        raise
    finally:
        daemon.clear_pid()
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop the running Perun server."""
    settings = config.Settings.load()
    existing = daemon.is_another_perun_running(settings)
    if existing is None:
        print("[perun] not running")
        return 0
    print(f"[perun] stopping PID {existing}...")
    if daemon.stop(existing, timeout=args.timeout):
        daemon.clear_pid()
        print("[perun] stopped")
        return 0
    if args.force:
        if daemon.kill(existing):
            daemon.clear_pid()
            print("[perun] killed")
            return 0
    print("[perun] failed to stop gracefully; use --force")
    return 1


def _recorded_flag_value(recorded: list[str], flag: str) -> str | None:
    """Return the value following *flag* in a recorded argv, or None.

    A token that itself looks like a flag is never treated as a value: a
    hand-edited record such as ``["serve", "--host", "--port", "8760"]`` must
    not yield ``--host --port``, which the replacement would fail to parse.
    """
    try:
        i = recorded.index(flag)
    except ValueError:
        return None
    if i + 1 >= len(recorded):
        return None
    value = recorded[i + 1]
    return None if value.startswith("--") else value


def _recorded_port(recorded: list[str]) -> int | None:
    """Return the recorded ``--port`` value as an int, or None."""
    raw = _recorded_flag_value(recorded, "--port")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def cmd_restart(args: argparse.Namespace) -> int:
    """Gracefully stop the running server, then start a new one.

    The replacement replays the previous server's recorded launch flags
    (``--host``/``--port``/``--reload``/``--bridge``; explicit flags here win),
    and runs detached by default so a restart issued from a shell, script, or
    CI step survives that shell's exit; pass ``--foreground`` to watch the new
    server's logs instead.
    """
    settings = config.Settings.load()
    existing = daemon.is_another_perun_running(settings)
    if existing:
        print(f"[perun] stopping existing server (PID {existing})...")
        if not daemon.stop(existing, timeout=args.timeout):
            if args.force:
                if not daemon.kill(existing):
                    print(f"[perun] could not kill PID {existing}; check permissions")
                    return 1
            else:
                print("[perun] failed to stop gracefully; use --force")
                return 1
        daemon.clear_pid()
        print("[perun] stopped")

    # Replay the previous server's recorded launch flags so a plain restart
    # does not silently drop configuration (bridges, reload mode, host/port).
    recorded = daemon.read_recorded_argv() or []
    if not args.host:
        args.host = _recorded_flag_value(recorded, "--host")
    if not args.port:
        args.port = _recorded_port(recorded)
    args.bridge = bool(args.bridge or "--bridge" in recorded)
    args.reload = bool(args.reload or "--reload" in recorded)

    if args.foreground:
        print("[perun] starting server (foreground; Ctrl+C to stop)...")
        return cmd_serve(args)

    # Detached by default. An attached server is killed when the shell that
    # launched it exits (including non-interactive shells such as CI steps or
    # agent harnesses timing out), which silently took the bridges down.
    # ``--detach`` is kept as a no-op for backward compatibility.
    argv = [sys.executable, "-m", "perun", "serve"]
    if args.host:
        argv.extend(["--host", args.host])
    if args.port:
        argv.extend(["--port", str(args.port)])
    if args.bridge:
        argv.append("--bridge")
    if args.reload:
        argv.append("--reload")
    new_pid = daemon.start_detached(argv)
    daemon.write_pid_for(new_pid)
    print(f"[perun] started detached server (PID {new_pid})")
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
def cmd_context(args: argparse.Namespace) -> int:
    """Perun Context — keys, retrieval, sync and drift verification."""
    action = getattr(args, "action", "list") or "list"
    _ensure()
    pid = _context_project_id(getattr(args, "project", None))

    if action == "list":
        keys = context.list_keys(project_id=pid)
        print(context.render_inventory(keys))
        print(f"\n{len(keys)} key(s).")
        return 0

    if action == "get":
        key = getattr(args, "key", "") or ""
        hit = context.get(key, project_id=pid)
        if not hit:
            print(f"[perun] no context doc for key '{key}'")
            return 1
        print(context.render_hit(hit))
        return 0

    if action == "search":
        topic = getattr(args, "topic", "") or ""
        k = int(getattr(args, "k", 3) or 3)
        hits = context.search(topic, project_id=pid, k=k)
        print(context.render_hits(hits))
        return 0

    if action == "sync":
        res = handbook.ensure_indexed()
        print(f"[perun] handbook: {res.get('indexed', 0)} indexed, "
              f"{res.get('unchanged', 0)} unchanged, "
              f"{len(res.get('skipped', []))} skipped")
        for item in res.get("skipped", []):
            print(f"  skipped {item.get('key')}: {item.get('reason')}")
        try:
            from .integrations import install_global

            path = install_global._sync_agents_block()
            print(f"[perun] kernel:   refreshed {path}")
        except Exception as exc:
            print(f"[perun] warning: could not refresh the kernel ({exc})")
        for line in _context_sync_projects(getattr(args, "project", None)):
            print(line)
        return 0

    if action == "map":
        return _context_map(getattr(args, "project", None),
                            write=bool(getattr(args, "write", False)))

    if action == "index":
        source = getattr(args, "file", "") or ""
        key = getattr(args, "key", "") or ""
        if not source or not key:
            print("[perun] index needs a file and a key.")
            return 2
        path = Path(source)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            print(f"[perun] no such file: {path}")
            return 1
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[perun] could not read {path}: {exc}")
            return 1
        title = getattr(args, "title", "") or path.name
        scope = (getattr(args, "scope", "project") or "project").strip().lower()
        if scope not in ("global", "project"):
            scope = "project"
        if scope == "project" and not pid:
            print("[perun] could not resolve a project — pass --project.")
            return 1
        res = context.index_doc(
            key, title, body, scope=scope, project_id=pid,
            summary=getattr(args, "summary", "") or "",
            source_file=str(path),
        )
        if not res.get("ok"):
            print(f"[perun] could not index: {res.get('reason')}")
            return 1
        print(f"[perun] indexed '{context.normalize_key(key)}' "
              f"({res.get('indexed', 0)} written, {res.get('unchanged', 0)} unchanged)")
        return 0

    if action == "verify":
        problems = _context_verify()
        for line in problems["report"]:
            print(line)
        return 1 if problems["problems"] else 0

    if action == "stats":
        _print(_context_stats())
        return 0

    print(f"[perun] unknown context action: {action}")
    return 2


def _context_targets(slug: str | None) -> list[dict[str, Any]]:
    """The projects a context operation applies to."""
    if slug:
        p = registry.get_project(slug) or registry.get_project_by_path(slug)
        return [p] if p else []
    return registry.list_projects()


def _context_sync_projects(slug: str | None) -> list[str]:
    """Index every project's per-project context docs and its original.

    The retained original is a **snapshot**, not a live mirror: once a
    project's context map pins it, it is never overwritten. The pre-migration
    file is the thing the parity contract promises to keep, so re-syncing
    after a migration must not be able to destroy it.
    """
    from . import context_map

    lines: list[str] = []
    for proj in _context_targets(slug):
        pid = int(proj.get("id") or 0)
        root = Path(proj.get("path") or "")
        name = str(proj.get("name") or proj.get("slug") or "project")
        pslug = str(proj.get("slug") or "")

        for f in context_map.project_context_files(root):
            key = context_map.project_context_key(pslug, f)
            try:
                body = f.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                lines.append(f"[perun] {pslug}: could not read {f} ({exc})")
                continue
            res = context.index_doc(
                key, f.stem.replace("-", " ").replace("_", " ").title(), body,
                scope=context.PROJECT_SCOPE, project_id=pid,
                summary=f"{name}: {f.stem.replace('-', ' ')}",
                source_file=str(f),
            )
            if not res.get("ok"):
                lines.append(f"[perun] {pslug}: could not index '{key}' "
                             f"({res.get('reason')})")
            else:
                lines.append(f"[perun] {pslug}: '{key}' "
                             f"({res.get('indexed', 0)} written, "
                             f"{res.get('unchanged', 0)} unchanged)")

        agents = root / "AGENTS.md"
        if not agents.is_file():
            continue
        key = context_map.retained_key(pslug)
        pinned = context_map.load_map(root)
        if pinned and str(pinned.get("retained") or "") == key:
            lines.append(f"[perun] {pslug}: retained original '{key}' is pinned "
                         f"by the context map — not overwritten")
            continue
        try:
            body = agents.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            lines.append(f"[perun] {pslug}: could not read {agents} ({exc})")
            continue
        res = context.index_doc(
            key, f"{name} agent reference (verbatim)", body,
            scope=context.PROJECT_SCOPE, project_id=pid,
            summary=f"the complete {name} AGENTS.md, retained verbatim",
            source_file=str(agents),
        )
        if not res.get("ok"):
            lines.append(f"[perun] {pslug}: could not retain {agents} "
                         f"({res.get('reason')})")
        else:
            lines.append(f"[perun] {pslug}: retained '{key}' "
                         f"({res.get('indexed', 0)} written, "
                         f"{res.get('unchanged', 0)} unchanged)")
    return lines


def _context_map(slug: str | None, *, write: bool = False) -> int:
    """Show (or draft) the losslessness map for a project's AGENTS.md."""
    from . import context_map

    projects = _context_targets(slug)
    if not projects:
        print("[perun] no project to map.")
        return 1
    rc = 0
    for proj in projects:
        root = Path(proj.get("path") or "")
        pslug = str(proj.get("slug") or "")
        agents = root / "AGENTS.md"
        if not agents.is_file():
            print(f"[perun] {pslug}: no AGENTS.md")
            continue
        body = agents.read_text(encoding="utf-8", errors="replace")
        existing = context_map.load_map(root)
        if existing:
            print(f"[perun] {pslug}: a context map already exists "
                  f"({context_map.map_path(root)})")
            for s in existing.get("sections") or []:
                dests = ", ".join(
                    f"{d.get('kind')}"
                    + (f":{d.get('key')}" if d.get("key") else "")
                    for d in (s.get("dest") or []) if isinstance(d, dict))
                print(f"    {str(s.get('bytes')):>6} B  {s.get('heading')}  "
                      f"-> {dests or '(unclassified)'}")
            continue
        sections = context_map.split_sections(body)
        print(f"[perun] {pslug}: {len(sections)} section(s), "
              f"{len(body.encode('utf-8'))} B")
        for s in sections:
            print(f"    {s['bytes']:>6} B  {s['hash'][:12]}  {s['heading']}")
        if write:
            draft = context_map.build_map(pslug, body)
            path = context_map.save_map(root, draft)
            print(f"[perun] {pslug}: draft map written to {path}")
        else:
            print(f"[perun] {pslug}: pass --write to draft "
                  f"{context_map.map_path(root)}")
    return rc


def _context_project_id(slug: str | None) -> int:
    """Resolve a project slug/path (or the cwd's project) to a project id."""
    if slug:
        p = registry.get_project(slug) or registry.get_project_by_path(slug)
        return p["id"] if p else 0
    try:
        cur = Path.cwd().resolve()
    except OSError:
        return 0
    by_path: dict[str, dict[str, Any]] = {}
    for p in registry.list_projects():
        try:
            by_path[os.path.normcase(os.path.abspath(p["path"]))] = p
        except Exception:
            continue
    for candidate in [cur, *cur.parents]:
        hit = by_path.get(os.path.normcase(str(candidate)))
        if hit:
            return int(hit["id"])
    return 0


def _context_verify() -> dict[str, Any]:
    """Check that the kernel, the handbook and the manifest anchors agree."""
    report: list[str] = []
    problems: list[str] = []

    # 1. The global AGENTS.md kernel must match the generated one.
    try:
        from .integrations import install_global

        target = install_global.agents_target()
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        expected = install_global._managed_block()
        if not current:
            problems.append("global AGENTS.md is missing")
            report.append("kernel:      MISSING")
        elif current.strip() != expected.strip():
            problems.append("global AGENTS.md kernel is out of date")
            report.append("kernel:      DRIFT  (run: perun context sync)")
        else:
            report.append("kernel:      ok")
    except Exception as exc:
        problems.append(f"kernel check failed: {exc}")
        report.append(f"kernel:      ERROR {exc}")

    # 2. Every handbook key must be indexed.
    indexed = {k["key"] for k in context.list_keys(project_id=0, scope="global")}
    missing = [e["key"] for e in handbook.ENTRIES if e["key"] not in indexed]
    if missing:
        problems.append(f"handbook keys not indexed: {', '.join(missing)}")
        report.append(f"handbook:    MISSING {', '.join(missing)}")
    else:
        report.append(f"handbook:    ok ({len(indexed)} global key(s))")

    # 3. Every manifest anchor that declares a `memory:` key must resolve.
    unresolved: list[str] = []
    missing_secrets: list[str] = []
    anchors_seen = 0
    for proj in registry.list_projects():
        try:
            from . import manifest as manifest_mod

            data = manifest_mod.load(proj["path"]).get("data") or {}
        except Exception:
            continue
        for anchor in data.get("anchors") or []:
            if not isinstance(anchor, dict):
                continue
            mem = str(anchor.get("memory") or "").strip()
            if mem:
                anchors_seen += 1
                if not context.get(mem, project_id=int(proj["id"])):
                    unresolved.append(f"{proj['slug']}:{mem}")
            if str(anchor.get("kind") or "").lower() == "secret":
                secret_name = str(anchor.get("path") or "").strip()
                if secret_name and not secret_store.get_secret(
                        secret_name, project_id=int(proj["id"]), reveal=False):
                    missing_secrets.append(f"{proj['slug']}:{secret_name}")
    if unresolved:
        problems.append(f"anchors with unresolved memory keys: {', '.join(unresolved)}")
        report.append(f"anchors:     UNRESOLVED {', '.join(unresolved)}")
    else:
        report.append(f"anchors:     ok ({anchors_seen} memory-bound anchor(s))")
    if missing_secrets:
        problems.append(f"secret anchors with no stored secret: {', '.join(missing_secrets)}")
        report.append(f"secrets:     MISSING {', '.join(missing_secrets)}")
    else:
        report.append("secrets:     ok")

    # 4. Every migrated project's AGENTS.md must be provably lossless.
    lossy: list[str] = []
    checked = 0
    for proj in registry.list_projects():
        try:
            from . import context_map

            res = context_map.check(proj)
        except Exception as exc:  # pragma: no cover - defensive
            lossy.append(f"{proj.get('slug')}: check failed ({exc})")
            continue
        if res.get("skipped"):
            continue
        checked += 1
        report.extend(res.get("report") or [])
        lossy.extend(res.get("problems") or [])
    if lossy:
        problems.append(f"losslessness check failed: {len(lossy)} problem(s)")
        report.append(f"lossless:    FAILED ({len(lossy)} problem(s))")
        for line in lossy[:20]:
            report.append(f"  - {line}")
    else:
        report.append(f"lossless:    ok ({checked} migrated project(s))")

    return {"ok": not problems, "problems": problems, "report": report}


def _context_stats() -> dict[str, Any]:
    """Ambient-context budget: how big the AGENTS.md files are, and what repeats."""
    projects: list[dict[str, Any]] = []
    section_index: dict[str, list[str]] = {}

    for proj in registry.list_projects():
        f = Path(proj["path"]) / "AGENTS.md"
        entry: dict[str, Any] = {
            "slug": proj["slug"],
            "path": str(f),
            "exists": f.is_file(),
            "bytes": 0,
            "lines": 0,
            "approx_tokens": 0,
        }
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            entry["bytes"] = len(text.encode("utf-8"))
            entry["lines"] = text.count("\n") + 1
            entry["approx_tokens"] = len(text) // 4
            for heading, body in _split_sections(text):
                digest = memory.content_hash(f"{heading}\n{body}")
                section_index.setdefault(digest, []).append(
                    f"{proj['slug']}#{heading}")
        projects.append(entry)

    duplicated = [
        {"copies": sorted(slugs), "sections": len(slugs)}
        for slugs in section_index.values() if len(slugs) > 1
    ]
    duplicated.sort(key=lambda d: d["sections"], reverse=True)

    global_block: dict[str, Any] = {}
    try:
        from .integrations import install_global

        gpath = install_global.agents_target()
        gtext = gpath.read_text(encoding="utf-8") if gpath.is_file() else ""
        global_block = {
            "path": str(gpath),
            "bytes": len(gtext.encode("utf-8")),
            "approx_tokens": len(gtext) // 4,
        }
    except Exception:
        pass

    total_bytes = sum(p["bytes"] for p in projects) + global_block.get("bytes", 0)
    return {
        "global_agents_md": global_block,
        "projects": projects,
        "worst_project": max(projects, key=lambda p: p["bytes"]) if projects else None,
        "total_bytes": total_bytes,
        "total_approx_tokens": total_bytes // 4,
        "duplicated_sections": duplicated[:40],
        "duplicated_section_count": len(duplicated),
    }


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split Markdown into ``(heading, body)`` pairs at level-2 headings."""
    sections: list[tuple[str, str]] = []
    heading = "(top)"
    buf: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith("## "):
            body = "\n".join(buf).strip()
            if body:
                sections.append((heading, body))
            heading = line[3:].strip() or "(untitled)"
            buf = []
        else:
            buf.append(line)
    body = "\n".join(buf).strip()
    if body:
        sections.append((heading, body))
    return sections


def cmd_secret(args: argparse.Namespace) -> int:
    """Perun's encrypted secret store."""
    action = getattr(args, "action", "list") or "list"
    _ensure()
    pid = _context_project_id(getattr(args, "project", None))

    if action == "status":
        _print(secret_store.unlock_status())
        return 0

    if action == "password":
        pw = getattr(args, "password", None)
        if not pw:
            try:
                pw = crypto.prompt_password()
            except Exception as exc:  # pragma: no cover - no tty
                print(f"[perun] could not read the password ({exc})")
                return 1
        if crypto.persist_password(pw):
            print("[perun] password stored (DPAPI-protected, current user only).")
            return 0
        print("[perun] could not store the password — DPAPI is unavailable here. "
              "Set PERUN_PASSWORD instead.")
        return 1

    if action == "list":
        rows = secret_store.list_secrets(project_id=pid)
        if not rows:
            print("[perun] no secrets stored.")
            return 0
        for r in rows:
            scope_txt = r["scope"]
            if scope_txt == "project":
                scope_txt = f"project:{r['project_id']}"
            print(f"{r['name']}  [{scope_txt}]  {r['hint']}".rstrip())
        return 0

    if action == "get":
        name = getattr(args, "name", "") or ""
        row = secret_store.get_secret(name, project_id=pid)
        if row is None:
            print(f"[perun] no secret named '{name}'.")
            return 1
        if row.get("error"):
            print(f"[perun] {row['error']}")
            return 1
        print(row["value"])
        return 0

    if action == "set":
        name = getattr(args, "name", "") or ""
        value = getattr(args, "value", None)
        if value is None:
            try:
                if not sys.stdin.isatty():
                    value = sys.stdin.read()
            except Exception:
                value = None
        if not value:
            try:
                import getpass

                value = getpass.getpass(f"Value for '{name}': ")
            except Exception:
                value = None
        if not name or not value:
            print("[perun] set needs a name and a value.")
            return 1
        use_scope = (getattr(args, "scope", "auto") or "auto").strip().lower()
        if use_scope == "auto":
            use_scope = "project" if pid else "global"
        if use_scope not in ("global", "project"):
            print("[perun] scope must be one of: auto, global, project")
            return 2
        res = secret_store.set_secret(name, value, scope=use_scope,
                                      project_id=pid,
                                      hint=getattr(args, "hint", "") or "")
        if not res.get("ok"):
            print(f"[perun] could not store the secret: {res.get('reason')}")
            if res.get("hint"):
                print(f"        {res['hint']}")
            return 1
        print(f"[perun] stored '{res['name']}' in {res['scope']} scope.")
        return 0

    if action == "delete":
        name = getattr(args, "name", "") or ""
        removed = secret_store.delete_secret(name, project_id=pid)
        print(f"[perun] deleted '{secret_store.normalize_name(name)}'."
              if removed else f"[perun] no secret named '{name}'.")
        return 0 if removed else 1

    print(f"[perun] unknown secret action: {action}")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="perun", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command")

    p_roots = sub.add_parser(
        "project-roots",
        help="Manage explicit project-root mappings for safe cross-project references.",
    )
    p_roots.add_argument("--add", nargs=2, metavar=("NAME", "PATH"),
                         help="Register or override a project root")
    p_roots.add_argument("--remove", metavar="NAME",
                         help="Remove a user-defined project root override")
    p_roots.add_argument("--list", action="store_true",
                         help="List effective project roots")
    p_roots.set_defaults(func=cmd_project_roots)

    p_ctx = sub.add_parser(
        "context",
        help="Perun Context: reference docs, key inventory, sync and drift checks.",
    )
    ctx_sub = p_ctx.add_subparsers(dest="action")

    pc_list = ctx_sub.add_parser("list", help="Show the context key inventory.")
    pc_list.add_argument("--project", help="scope to a project slug or path")

    pc_get = ctx_sub.add_parser("get", help="Fetch one context doc by exact key.")
    pc_get.add_argument("key", help="context key, e.g. handbook.tools")
    pc_get.add_argument("--project", help="scope to a project slug or path")

    pc_search = ctx_sub.add_parser(
        "search", help="Resolve a topic into context docs (key, label or task).")
    pc_search.add_argument("topic", nargs="?", default="",
                           help="key, anchor label, or a description of the task")
    pc_search.add_argument("--project", help="scope to a project slug or path")
    pc_search.add_argument("--k", type=int, default=3, help="max documents (1-8)")

    pc_sync = ctx_sub.add_parser(
        "sync", help="Re-index the handbook and refresh the global AGENTS.md kernel.")
    pc_sync.add_argument("--project", help="scope to a project slug or path")

    pc_map = ctx_sub.add_parser(
        "map", help="Show (or draft) the losslessness map for a project's AGENTS.md.")
    pc_map.add_argument("--project", help="scope to a project slug or path")
    pc_map.add_argument("--write", action="store_true",
                         help="write a draft .perun/context-map.json")

    pc_index = ctx_sub.add_parser(
        "index", help="Index a file as a context doc under a stable key.")
    pc_index.add_argument("file", help="path to the file to index")
    pc_index.add_argument("--key", required=True, help="the context key")
    pc_index.add_argument("--title", default="", help="human-readable title")
    pc_index.add_argument("--summary", default="", help="one-line summary")
    pc_index.add_argument("--scope", default="project",
                          choices=["project", "global"])
    pc_index.add_argument("--project", help="scope to a project slug or path")

    pc_verify = ctx_sub.add_parser(
        "verify", help="Check that kernel, handbook and manifest anchors agree.")
    pc_verify.add_argument("--project", help="scope to a project slug or path")

    pc_stats = ctx_sub.add_parser(
        "stats", help="Ambient-context budget: AGENTS.md sizes and duplication.")
    pc_stats.add_argument("--project", help="scope to a project slug or path")

    p_ctx.set_defaults(func=cmd_context)

    p_sec = sub.add_parser(
        "secret",
        help="Perun's encrypted secret store: named values, never in a prompt.",
    )
    sec_sub = p_sec.add_subparsers(dest="action")

    ps_list = sec_sub.add_parser("list", help="List secret names (never values).")
    ps_list.add_argument("--project", help="scope to a project slug or path")

    ps_get = sec_sub.add_parser("get", help="Print one secret's value.")
    ps_get.add_argument("name")
    ps_get.add_argument("--project", help="scope to a project slug or path")

    ps_set = sec_sub.add_parser("set", help="Store a secret (encrypted at rest).")
    ps_set.add_argument("name")
    ps_set.add_argument("--value", help="the value; omit to read it from stdin")
    ps_set.add_argument("--hint", default="", help="short non-sensitive description")
    ps_set.add_argument("--scope", default="auto",
                        choices=["auto", "global", "project"])
    ps_set.add_argument("--project", help="scope to a project slug or path")

    ps_del = sec_sub.add_parser("delete", help="Delete a secret.")
    ps_del.add_argument("name")
    ps_del.add_argument("--project", help="scope to a project slug or path")

    ps_pw = sec_sub.add_parser(
        "password", help="Store the master password (DPAPI-protected).")
    ps_pw.add_argument("--password", help="omit to prompt without echo")

    ps_status = sec_sub.add_parser(
        "status", help="Report whether the store can be read and written.")
    ps_status.add_argument("--project", help="scope to a project slug or path")

    p_sec.set_defaults(func=cmd_secret)

    p = sub.add_parser("serve", help="run the HTTP + SSE service and dashboard")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--bridge", action="store_true", help="also start the Telegram bridge")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("stop", help="stop the running Perun server")
    p.add_argument("--timeout", type=float, default=30,
                   help="seconds to wait for graceful shutdown")
    p.add_argument("--force", action="store_true",
                   help="force-kill if graceful shutdown fails")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser(
        "restart",
        help="restart the Perun server, replaying the last recorded launch flags",
    )
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--bridge", action="store_true", help="also start the Telegram bridge")
    p.add_argument("--detach", action="store_true",
                   help="start detached (default since v0.7.9; kept for compatibility)")
    p.add_argument("--foreground", action="store_true",
                   help="run the replacement in this console instead of detaching")
    p.add_argument("--timeout", type=float, default=30,
                   help="seconds to wait for the old server to shut down")
    p.add_argument("--force", action="store_true",
                   help="force-kill the old server if graceful shutdown fails")
    p.set_defaults(func=cmd_restart)

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


def cmd_project_roots(args) -> int:
    """List, add, or remove project-root mappings."""
    if args.add:
        name, path = args.add
        project_roots.add_root(name, path)
        print(f"Registered root: {name} -> {project_roots.get_root(name)}")
        return 0
    if args.remove:
        project_roots.remove_root(args.remove)
        print(f"Removed override: {args.remove}")
        return 0
    roots = project_roots.list_roots()
    print("Effective project roots:")
    for name, path in sorted(roots.items()):
        print(f"  {name}: {path}")
    return 0


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
