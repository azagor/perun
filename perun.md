---
name: Perun
slug: perun
kind: opencode
description: A machine-wide overseer and orchestrator for all your OpenCode projects
  — unified memory, TUI launching, headless run orchestration, web dashboard, shared
  Telegram bridge, and native PerunChat agent federation.
tags:
- opencode
- orchestrator
- memory
- dashboard
- telegram
- mcp
- perunchat
docs:
  overview: README.md
  codemap: docs/CODEMAP.md
  technical: docs/TECHNICAL_DOCUMENTATION.md
  changelog: docs/CHANGELOG.md
  design: docs/DESIGN.md
  agents_optimization: docs/AGENTS_OPTIMIZATION.md
  agents: AGENTS.md
commands:
  serve: python -m perun serve
  discover: python -m perun discover
  install: python -m perun install --machine
  test: python -m pytest tests/ -q
links:
  repository: https://github.com/azagor/perun
dependencies:
  - slug: perunchat
    relationship: sibling
    reason: PerunChat federated gateway; source of production VPS access details and the gateway service deployed at /opt/perun_chat.
    deploy_command: ssh -i ~/.ssh/perun_opencode2 root@152.53.100.209 "cd /opt/perun_chat && git pull && systemctl restart perunchat"
    deploy_doc: DEPLOY.md
    access: root@152.53.100.209
    auto_deploy: false
  - slug: perun-in-the-machine
    relationship: sibling
    reason: Company website and PerunChat public dashboard; co-deployed on the same VPS and sharing Nginx routing.
    deploy_command: powershell -Command ". .\company_site\remote.ps1; Sync-PerunWebsite; Sync-PerunNginx"
    deploy_doc: company_site/DEPLOY.md
    access: root@152.53.100.209
    auto_deploy: false
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
- label: Product design
  kind: doc
  path: docs/DESIGN.md
- label: Shared Projects PRD
  kind: doc
  path: docs/SHARED_PROJECTS_PRD.md
- label: AGENTS.md context-budget optimisation
  kind: doc
  path: docs/AGENTS_OPTIMIZATION.md
- label: Perun agent reference (verbatim, retrievable)
  kind: doc
  path: AGENTS.md
  memory: perun.agents.full
- label: Perun reference handbook (verbatim, retrievable)
  kind: doc
  path: perun/integrations/templates/handbook/global.md
  memory: handbook.global
- label: Project overview (retrievable)
  kind: doc
  path: .perun/context/overview.md
  memory: perun.context.overview
- label: Extending the project (retrievable)
  kind: doc
  path: .perun/context/contributing.md
  memory: perun.context.contributing
- label: Build, test and run commands (retrievable)
  kind: doc
  path: .perun/context/commands.md
  memory: perun.context.commands
- label: Git push credentials (retrievable)
  kind: doc
  path: .perun/context/credentials.md
  memory: perun.context.credentials
- label: Release procedure (retrievable)
  kind: doc
  path: .perun/context/releasing.md
  memory: perun.context.releasing
- label: Cross-project dependencies (retrievable)
  kind: doc
  path: .perun/context/cross-project.md
  memory: perun.context.cross-project
- label: perun.server
  kind: file
  path: perun/server.py
- label: perun.orchestrator
  kind: file
  path: perun/orchestrator.py
- label: perun.project_roots
  kind: file
  path: perun/project_roots.py
- label: perun.process_tracker
  kind: file
  path: perun/process_tracker.py
- label: perun.memory
  kind: file
  path: perun/memory.py
- label: perun.context
  kind: file
  path: perun/context.py
- label: perun.context_map
  kind: file
  path: perun/context_map.py
- label: perun.secret_store
  kind: file
  path: perun/secret_store.py
- label: perun.updater
  kind: file
  path: perun/updater.py
- label: perun.handbook
  kind: file
  path: perun/handbook.py
- label: perun.telegram
  kind: file
  path: perun/telegram.py
- label: perun.opencode_config
  kind: file
  path: perun/opencode_config.py
- label: perun.providers
  kind: file
  path: perun/providers.py
- label: requirements.txt
  kind: file
  path: requirements.txt
- label: pyproject.toml
  kind: file
  path: pyproject.toml
- label: .github/workflows/release.yml
  kind: file
  path: .github/workflows/release.yml
- label: PerunChat local UI
  kind: url
  url: http://127.0.0.1:8760/#/perunchat
- label: PerunChat public dashboard
  kind: url
  url: https://peruninthemachine.com/perunchat/
- label: PerunChat public chat
  kind: url
  url: https://peruninthemachine.com/perunchat/public_chat.html
- label: Autonomous PerunChat bridge
  kind: file
  path: perun/perunchat_bridge.py
- label: PerunChat chat instructions
  kind: file
  path: perun/instructions/perunchat_chat.md
- label: External execution protocol instructions
  kind: file
  path: perun/instructions/perun-external-execution.md
- label: perun_chat repo
  kind: url
  url: https://github.com/azagor/perun_chat
- label: .github/workflows
  kind: file
  path: .github/workflows
version: 1
---

Perun is a single local Python service that sits on top of OpenCode. It registers every project it can find, stores project-namespaced memory, orchestrates agent runs, serves a web dashboard, and runs one shared Telegram bridge for all projects. It also has native PerunChat support, connecting Perun agents to the federated agent-to-agent chat network at peruninthemachine.com.

## Production VPS

The live PerunChat gateway and company website share the VPS at `152.53.100.209` (`peruninthemachine.com`). SSH details and service commands live in the sibling `perunchat` project (`AGENTS.md` → Server / company access). The static site and Nginx configuration are maintained in the `perun-in-the-machine` project. Use `perun.project_roots.resolve("perunchat", ...)` and `perun.project_roots.resolve("perun-in-the-machine", ...)` to access their files without hand-crafted paths.
