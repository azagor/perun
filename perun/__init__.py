"""Perun — overseer and orchestrator for all OpenCode projects.

Perun is a local, single-binary orchestrator that sits *on top* of OpenCode.
It provides, for every project on the machine and regardless of which folder
OpenCode was opened in:

* a central project registry with discovery,
* a unified, project-namespaced memory + semantic document store,
* an agent orchestrator that starts and streams OpenCode sessions,
* a shared Telegram bridge with per-project routing,
* a web dashboard (HTML/CSS/JS) styled after peruninthemachine.com,
* a global MCP server (``perun``) that any project inherits automatically.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.10.12"
