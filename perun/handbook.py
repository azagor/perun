"""The Perun handbook — Perun's own reference material as retrievable context.

The handbook is the *body* of reference material that used to live, in full,
in the global ``AGENTS.md`` block. It is now a set of **context docs** with
stable keys, indexed into the local store and retrieved on demand through
:func:`perun.context.search`.

Two properties make this safe for the *awareness parity* contract:

* ``handbook.global`` keeps the **complete previous global guide, verbatim**.
  Nothing was deleted — it was relocated, and it is still reachable in one
  call.
* ``handbook.tools`` is **generated from the live MCP registry**, so the tool
  reference cannot drift away from the tools that actually exist.

The kernel (the small always-on block in ``AGENTS.md``) is rendered from
:data:`ENTRIES`, so the key table and the handbook never disagree.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any

from . import config, context

HANDBOOK_DIR = config.TEMPLATES_DIR / "handbook"

# Re-entrancy guard for :func:`ensure_indexed` — see the docstring there.
_indexing = False

# The handbook manifest. This is the single source of truth for the keys, so
# the kernel's key table, the indexed context docs, and ``perun context
# list`` can never drift apart.
ENTRIES: list[dict[str, Any]] = [
    {
        "key": "handbook.tools",
        "title": "Perun MCP tool reference",
        "summary": "every perun_* tool, its arguments and what it returns",
        "file": "tools.md",
        "generated": True,
    },
    {
        "key": "handbook.search",
        "title": "Code search: which tool, when",
        "summary": "grep / ast-grep / unified search, RRF ranking, fallbacks",
        "file": "search.md",
    },
    {
        "key": "handbook.memory",
        "title": "Memory protocol",
        "summary": "facts vs docs, scoping, freshness, the sensitive-data guard",
        "file": "memory.md",
    },
    {
        "key": "handbook.commands",
        "title": "Global slash commands",
        "summary": "/perun-projects, /perun-ask, /logwork, /update-docs",
        "file": "commands.md",
    },
    {
        "key": "handbook.cross-project",
        "title": "Cross-project references",
        "summary": "perun.project_roots.resolve, sibling repos, why not hand-crafted paths",
        "file": "cross-project.md",
    },
    {
        "key": "handbook.sessions",
        "title": "Agent runs",
        "summary": "starting runs, models, agents, streaming, cancellation",
        "file": "sessions.md",
    },
    {
        "key": "handbook.onboarding",
        "title": "Perun on this machine",
        "summary": "dashboard, Telegram bridge, fresh-machine setup and repair",
        "file": "onboarding.md",
    },
    {
        "key": "handbook.secrets",
        "title": "The secret store",
        "summary": "named credentials, encrypted at rest, pulled on demand",
        "file": "secrets.md",
    },
    {
        "key": "handbook.global",
        "title": "The original Perun global guide",
        "summary": "the complete previous global AGENTS.md block, verbatim",
        "file": "global.md",
    },
]


# --------------------------------------------------------------------------- #
# The generated tool reference
# --------------------------------------------------------------------------- #
def _load_mcp_module():
    """Load ``mcp/perun_mcp.py`` by path.

    The installed ``mcp`` package shadows the repository's ``mcp/`` directory,
    so the server cannot be imported as ``mcp.perun_mcp``.
    """
    path = config.MCP_SERVER
    if not path.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("perun_mcp_server", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _describe_param(name: str, spec: dict[str, Any], required: bool) -> str:
    kind = str(spec.get("type") or "any")
    desc = str(spec.get("description") or "").strip().replace("|", "\\|")
    if not desc:
        desc = kind
    return f"| `{name}` | {kind} | {'yes' if required else 'no'} | {desc} |"


def _list_mcp_tools() -> list[Any]:
    """Return the live MCP tool list, or an empty list on failure.

    Note: this helper uses ``asyncio.run()`` and must only be called from
    synchronous contexts (CLI, install paths, or threadpool workers).
    """
    module = _load_mcp_module()
    if module is None:
        return []
    try:
        return asyncio.run(module.mcp.list_tools())
    except Exception:
        return []


def render_tool_reference() -> str:
    """Render the MCP tool reference from the live registry.

    Returns an empty string when the MCP server cannot be loaded — the caller
    then skips the doc rather than indexing a stale copy.
    """
    tools = _list_mcp_tools()
    if not tools:
        return ""
    lines = [
        "# Perun MCP tool reference",
        "",
        "Generated from the live MCP registry — it cannot drift from the tools",
        "that actually exist. Every tool accepts an optional `project` (slug or",
        "absolute path); when omitted, the project is resolved from the current",
        "working directory.",
        "",
    ]
    for tool in sorted(tools, key=lambda t: t.name):
        lines.append(f"## {tool.name}")
        desc = (tool.description or "").strip()
        if desc:
            lines.append("")
            lines.append(desc)
        schema = getattr(tool, "inputSchema", None) or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        if props:
            lines.append("")
            lines.append("| Argument | Type | Required | Description |")
            lines.append("|---|---|---|---|")
            for name, spec in props.items():
                if not isinstance(spec, dict):
                    spec = {}
                lines.append(_describe_param(name, spec, name in required))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def tool_schema_stats() -> dict[str, Any]:
    """Return the size of the live MCP tool schema surface.

    This is the part of the prompt floor that comes from tool definitions,
    not from instruction files. It is measured so the ambient ledger is
    complete.

    Note: this helper uses ``asyncio.run()`` internally and must only be called
    from synchronous contexts (CLI, install paths, or threadpool workers).
    """
    import json as _json

    tools = _list_mcp_tools()
    if not tools:
        return {"tools": 0, "schema_bytes": 0, "approx_tokens": 0}
    payload = []
    for tool in sorted(tools, key=lambda t: t.name):
        payload.append({
            "name": tool.name,
            "description": tool.description,
            "inputSchema": getattr(tool, "inputSchema", None) or {},
        })
    try:
        raw = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        return {"tools": len(tools), "schema_bytes": 0, "approx_tokens": 0,
                "error": f"could not serialise tool schemas: {exc}"}
    return {"tools": len(tools), "schema_bytes": len(raw),
            "approx_tokens": len(raw) // 4}


# --------------------------------------------------------------------------- #
# Collecting and indexing
# --------------------------------------------------------------------------- #
def collect() -> list[dict[str, Any]]:
    """Build the list of context docs that make up the handbook."""
    docs: list[dict[str, Any]] = []
    for entry in ENTRIES:
        if entry.get("generated"):
            body = render_tool_reference()
            source = "generated:mcp"
        else:
            path = HANDBOOK_DIR / str(entry.get("file") or "")
            try:
                body = path.read_text(encoding="utf-8") if path.is_file() else ""
            except OSError:
                body = ""
            source = f"templates/handbook/{entry.get('file')}"
        if not body.strip():
            continue
        docs.append({
            "key": entry["key"],
            "title": entry["title"],
            "summary": entry["summary"],
            "body": body,
            "source_file": source,
            "scope": context.GLOBAL_SCOPE,
            "project_id": 0,
        })
    return docs


def ensure_indexed() -> dict[str, Any]:
    """Index the handbook into the local store.

    Safe to call on every process start: docs whose body has not changed are
    left untouched, so no embedding call is made after the first run.

    The re-entrancy guard matters: rendering the tool reference loads the MCP
    server module, and that module calls back into this function on import.
    Without the guard that re-entry would recurse.
    """
    global _indexing
    if _indexing:
        return {"ok": True, "indexed": 0, "unchanged": 0, "skipped": [],
                "reason": "re-entrant"}
    _indexing = True
    try:
        return context.index_docs(collect())
    except Exception as exc:  # pragma: no cover - never block startup
        return {"ok": False, "reason": str(exc)}
    finally:
        _indexing = False


# --------------------------------------------------------------------------- #
# Kernel rendering
# --------------------------------------------------------------------------- #
def render_key_table() -> str:
    """Render the markdown key table shown in the AGENTS.md kernel."""
    lines = ["| Key | Covers |", "|---|---|"]
    for entry in ENTRIES:
        lines.append(f"| `{entry['key']}` | {entry['summary']} |")
    return "\n".join(lines)


def render_kernel(template: str) -> str:
    """Fill a kernel template's ``{{KEY_TABLE}}`` placeholder."""
    return (template or "").replace("{{KEY_TABLE}}", render_key_table())
