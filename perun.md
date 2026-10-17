---
name: Perun
slug: perun
kind: opencode
description: A machine-wide overseer and orchestrator for all your OpenCode projects
  — unified memory, TUI launching, headless run orchestration, web dashboard, and
  a shared Telegram bridge.
tags:
- opencode
- orchestrator
- memory
- dashboard
- telegram
- mcp
docs:
  overview: README.md
  codemap: docs/CODEMAP.md
  technical: docs/TECHNICAL_DOCUMENTATION.md
  changelog: docs/CHANGELOG.md
  agents: AGENTS.md
commands:
  serve: python -m perun serve
  discover: python -m perun discover
  install: python -m perun install --machine
  test: python -m pytest tests/ -q
links:
  repository: C:\my_apps\perun
anchors:
- label: Dashboard
  kind: url
  url: http://127.0.0.1:8760
- label: CODEMAP
  kind: doc
  path: docs/CODEMAP.md
- label: Server endpoints
  kind: doc
  path: docs/TECHNICAL_DOCUMENTATION.md
- label: Release history
  kind: doc
  path: docs/CHANGELOG.md
- label: perun.server
  kind: file
  path: perun/server.py
- label: perun.orchestrator
  kind: file
  path: perun/orchestrator.py
- label: perun.process_tracker
  kind: file
  path: perun/process_tracker.py
- label: perun.memory
  kind: file
  path: perun/memory.py
- label: perun.telegram
  kind: file
  path: perun/telegram.py
- label: requirements.txt
  kind: file
  path: requirements.txt
- label: pyproject.toml
  kind: file
  path: pyproject.toml
- label: .github/workflows
  kind: file
  path: .github/workflows
version: 1
---

Perun is a single local Python service that sits on top of OpenCode. It registers every project it can find, stores project-namespaced memory, orchestrates agent runs, serves a web dashboard, and runs one shared Telegram bridge for all projects.
