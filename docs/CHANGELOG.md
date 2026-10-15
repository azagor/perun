# Perun — Changelog

All notable changes are recorded here. Single source of truth for release notes.

## v0.6.0 - 2026-10-15

A **TUI-first workflow** for Perun. The dashboard no longer tries to stream
`opencode-go/*` models headlessly; instead, the Agent Console and every project
card launch the OpenCode TUI with the composed prompt and queued tasks, and
Perun tracks the external terminal process so runs auto-finalize when the user
closes the window.

### Changed

- **Agent Console is now a TUI launcher.** The chat-style streaming output was
  removed because `opencode-go/*` models can only run inside the OpenCode TUI.
  The console is now a prompt composer + task queue + launch log. The primary
  button is **🖵 OpenCode** and always opens the selected project's TUI with the
  prompt chain.
- **Project card primary action is 🖵 OpenCode.** The previous headless `Run`
  flow is gone; the card opens the TUI directly and the project lights up as
  active.
- **Stop button kills the TUI window.** For tracked TUI sessions, Stop now calls
  `/api/runs/{uid}/kill`, which terminates the terminal process tree and
  finalizes the run record.

### Added

- **`perun/process_tracker.py`** — lifecycle tracking for externally-launched
  terminal/TUI processes. Uses `psutil` to walk process trees, resolves Windows
  Terminal child processes via an injected `PERUN_RUN_UID` environment marker,
  kills the tree on request, and auto-finalizes the matching run record when the
  window closes.
- **`POST /api/runs/tui`** — create a lightweight `running` run record for an
  externally-launched TUI session.
- **`POST /api/runs/{uid}/finish`** — manually finalize a TUI run record.
- **`POST /api/runs/{uid}/kill`** — terminate the tracked terminal/TUI process
  for a run and finalize it.
- **`POST /api/run-in-terminal`** now accepts `run_uid` to link the opened
  terminal to a TUI run record.
- **`/api/runs/{uid}/cancel`** now cancels headless runs through the orchestrator
  *and* terminates tracked TUI sessions, so the Stop button works everywhere.
- Reusable **project tasks** (`tasks` table + CRUD API + UI tab) that can be
  queued into the prompt chain before launching the TUI.
- **Created `perun.md` manifests** for Perun, `trading-bot`, and
  `parental-control` and loaded them into the registry so the dashboard shows
  descriptions, tags, docs links, and anchors for every project.
- **`perun/skills.py`** — discovery of OpenCode skills and tools available to a
  project. Surfaced via `GET /api/skills` and `GET /api/tools`, and used to
  populate searchable multi-select dropdowns in the Tasks tab.
- **`GET /api/skills`** and **`GET /api/tools`** — list skills/tools for a
  project, combining global OpenCode config with project-local `.opencode/`
  directories.
- **Tasks tab UX:** Skills and Tools are now searchable multi-select dropdowns
  with checkboxes, fed by `/api/skills` and `/api/tools`. The **Order** field
  was removed from the UI (the DB column is kept for backwards compatibility);
  queued tasks are ordered in the Agent Console by their queue position.
- **Draggable Agent Console queue.** Queued task chips can be dragged to reorder
  them; the prompt chain is built in the visible queue order.
- **Deep-linkable project tabs.** URLs like `#/project/<slug>/tasks` or
  `#/project/<slug>/console` now open directly on the requested tab, so the
  Tasks/Console tabs are bookmarkable and testable in headless mode.
- **Skill audit and migration.** Scanned all skill directories on the machine,
  archived broken/abandoned skills, migrated useful OpenCode-compatible skills
  into `~/.config/opencode/skills/`, made `kivy-layout-patterns` project-local
  for `trading_bot`, and created a new project-local `trading-bot-build` skill.
  Runtime-reserved skill IDs (`android-design-guidelines`, `find-skills`) were
  removed from the user skills directory so they do not conflict with built-in
  skills.

### Fixed

- **Production-readiness audit fixes.**
  - Added server endpoint tests for task CRUD, TUI run lifecycle,
    `run-in-terminal`, `/api/skills`, and `/api/tools`.
  - `POST /api/runs/{uid}/cancel` now checks `TRACKER.get(run_uid)` before
    calling `terminate()`, avoiding unnecessary process-tracking work for
    headless runs.
  - `POST /api/runs/tui` now returns the updated run record so its status is
    `running` in the response.
  - Multi-select dropdowns no longer let an open panel intercept clicks on a
    sibling toggle; the toggle has a higher stacking index and opening one
    dropdown closes any other open dropdown.
  - Runtime-built-in OpenCode skills (`code-audit`, `android-design-guidelines`,
    `find-skills`) are now surfaced by `/api/skills` so they appear in the
    Tasks dropdown, while still preventing user SKILL.md files from shadowing
    them.
  - Fixed `POST /api/run-in-terminal` for PowerShell: it no longer duplicates
    `-NoExit`/`-Command` flags from `Settings.terminal_cmd()`, which caused the
    "filename, directory name, or volume label syntax is incorrect" error
    when launching the OpenCode TUI on Windows.

## v0.5.0 - 2026-10-14

A **UI-consistency pass** over the dashboard, plus the ability to point Perun at
a **different Telegram bot** without hand-editing `config.json`.

### Fixed

- **CODEMAP accuracy pass.** Corrected stale/incomplete entries after the v0.6.0
  refactor: added the `tasks` table and `terminal` setting, documented
  `ProcessTracker`/`PERUN_RUN_UID` marker behavior, fixed the project-card action
  description, added the new endpoints (`/api/run-in-terminal`,
  `/api/projects/{slug}/tasks`, `/api/runs/tui`, `/api/runs/{uid}/finish`,
  `/api/runs/{uid}/kill`), and added the new test files
  (`test_process_tracker.py`, `test_agents.py`).
- **Removed stale `opencode-go/*` model override** from the bundled
  `trading_bot/opencode.json` so headless runs and TUI launches do not hit the
  "free tier can only be used from within OpenCode" error.
- **Project cards are now consistent.** The orchestrator (control-plane) card
  used to be special-cased in two ways: its whole card (including its action
  button) hijacked clicks to the orchestrator chat, and its primary button read
  `Open chat` while every other card read `Open`. Clicking "Ask" on the control
  card therefore opened the chat instead of the Agent Console. The hijack is
  gone: `projectCard()` now behaves identically for **every** project — `Open`
  opens the project page, and the action button always opens the Agent Console
  with that project preselected.
- Renamed the project-card / topbar action from **"Ask" to "Run"**. "Ask" did
  not describe what the button does (it opens the Agent Console, not a chat) and
  was easily confused with the orchestrator chat. The topbar quick action is now
  `✦ Run`.
- The Agent Console's eyebrow read **"Orchestrator"**; it is an *agent* view, so
  it now reads `Agent`.
- The **orchestrator chat is only reachable from the left toolbar** (its nav
  item is now labelled `Orchestrator Chat`), not from a project card.
- **Agent Console model/agent dropdowns were clipped.** The searchable pickers
  are built from an absolutely-positioned panel inside `.mselect`, but the
  wrapper had `overflow: hidden`, which hid the panel below the input. The
  overflow rule is removed so clicking/tapping Model or Agent opens the full
  filtered list.
- **Dedicated terminal/shell chooser.** A new `Settings.terminal` field and a
  `GET /api/terminals` endpoint let the user pick the preferred command
  interpreter from the shells detected on the machine (PowerShell 7+ / Windows
  PowerShell / Command Prompt / Windows Terminal on Windows; current shell /
  bash / zsh / fish / sh elsewhere). The Agent Console now shows the selected
  shell in the `📋 Command` and `▶ Terminal` buttons, and `POST
  /api/run-in-terminal` opens that terminal with the equivalent `opencode run`
  command.
- **Settings page dropdowns.** `Default agent` and `Orchestrator agent` are
  now searchable agent pickers, and `Embed model` is a dropdown of common
  Ollama embedding models with a custom-value fallback.

### Added

- **Telegram bot settings on the Telegram page** (`POST /api/telegram/bot`). A
  single "Bot settings" card edits the bot **name** (a local label to tell bots
  apart), the **API key / bot token** (a password field that is *never* echoed
  back — the UI shows a `set` badge and the placeholder stays masked; blank keeps
  the current token), and the **allowed chat ids**. A new token is verified with
  Telegram's `getMe` **before** it is persisted, so a typo can never silently
  replace a working bot; sending `token: ""` disconnects.
- **`POST /api/telegram/bridge/restart`** + `installer.restart_bridge_task()`.
  The bridge resolves its token once at startup, so switching bots needs a
  restart. This runs `schtasks /End` (which terminates *only* the process the
  task started — never the dashboard) followed by `schtasks /Run`. If the
  dashboard itself owns the bridge lock, the new instance simply exits on
  `PidLock`, so a second poller still never appears.
- **`Settings.telegram_sources()`** and the `token_source` / `chats_source`
  fields on `/api/telegram/status`. `Settings.telegram()` prefers `config.json`
  and falls back to `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, so the dashboard
  now says *which* one is in force. When the token comes from the environment,
  the page shows a warning, and "Disconnect" is disabled with an explanatory
  tooltip — otherwise it would look broken (the env token would silently take
  over).
- **Start an OpenCode session for a project without typing a prompt first.**
  The Agent Console's **Run** button no longer blocks on an empty prompt; when
  none is supplied Perun uses a default starter prompt (`"Start a working
  session for this project."`) so the session is created in the selected
  project's scope. A matching CLI command `python -m perun run <slug> [prompt]`
  does the same thing from the terminal, and a new REST endpoint
  `POST /api/projects/{slug}/session` exposes it for integrations.
- **Agent dropdown in the Agent Console.** The free-text Agent field is now a
  searchable picker populated with OpenCode's built-in agents (`build`, `plan`,
  `general`, `explore`) plus any user-defined agents found on disk under
  `~/.config/opencode/agents/` or `.opencode/agents/` in the project. A new
  `GET /api/agents` endpoint powers it; if the endpoint is unavailable the UI
  falls back to the built-in list so the dropdown is never empty.
- **Model and Agent overrides are now explicit only.** Perun used to fall back
  to the global `default_model` / `default_agent` settings for every project
  run, which caused surprising provider errors when those defaults pointed to
  region-locked or unavailable models. Now an empty Model/Agent field lets
  OpenCode use its own project defaults; Perun only passes `--model` / `--agent`
  when the user (or the project's own settings) explicitly supplies one.
- **Fixed project-instruction injection format.** The previous XML-style tags
  (`<perun:project_instructions>`) were being interpreted by the model as
  markup, causing the actual prompt to appear empty. Instructions are now
  wrapped in plain-text delimiters (`[PERUN PROJECT INSTRUCTIONS]` /
  `[END PERUN PROJECT INSTRUCTIONS]`) so the starter prompt reaches the agent.
- **Provider-side failures now keep the session active.** If OpenCode creates a
  session but the provider returns an error (quota, region, invalid model, …),
  the run is marked `provider_error` instead of `failed`, and the session row
  stays `active` so you can continue it from the Session dropdown after fixing
  the provider issue.
- **Lightning flourish when Perun replies in the orchestrator chat.** The send
  already flashed the sigil; now the first streamed text of Perun's reply also
  triggers `perunFx.flash`, so every assistant turn is preceded by a visible
  lightning strike.
- **Copy the equivalent command for PowerShell/Terminal from the Agent
  Console.** A `📋 Command` button builds the matching `opencode run` invocation
  for the currently selected project, model, agent, session, and prompt, then
  copies it to the clipboard so you can paste it into PowerShell Preview and run
  the same session there.

### Changed

- `GET /api/settings` no longer echoes `telegram_token` at all (it returned the
  placeholder `***`), and the Telegram token/chat fields were **removed from the
  Settings page** in favour of a pointer to the Telegram page — one source of
  truth for the bot.
- `Settings` gains `telegram_bot_name`.

### Tests

- `tests/test_telegram_bot_api.py` (10 tests): settings never echo the token;
  the bot endpoint saves a label/chats, rejects an invalid token (verified with a
  mocked `getMe`), persists a verified one, clears on `token: ""`, and reports
  the env-vs-config source with config taking precedence.
- `tests/test_installer.py` (+4): `restart_bridge_task()` is a no-op off
  Windows, requires the task, runs `/End` then `/Run`, and survives an `/End`
  failure (a stale/not-running task must not block the re-start).
- `tests/test_server.py` (+1): `POST /api/projects/{slug}/session` starts a
  project-scoped session with an optional prompt; `POST /api/runs` no longer
  rejects an empty prompt.
- `tests/test_orchestrator.py`: empty prompt now uses the default starter prompt.
- `tests/test_cli.py` (+1): `python -m perun run <slug> [prompt]` dispatches to
  the orchestrator.
- `tests/test_agents.py` (3 tests): built-ins, global custom agents, and
  project-local agents are discovered from disk.
- `tests/test_orchestrator.py` (+1): provider errors after session creation keep
  the session active.
- Full suite: **195 passed**.

### Verified

- Rendered DOM (headless Chrome) of `#/dashboard`, `#/projects`, `#/telegram`,
  `#/settings`: 22/22 checks — consistent card actions, the `Run` rename, the
  `Orchestrator Chat` nav item, the Telegram bot-settings card, and the masked
  token field. Live `/api/telegram/status` reported `token_source=env`,
  `bot.username=PerunTheCoderBot`, `bridge_task.exists=true`.

## v0.4.0 - 2026-10-14

Perun becomes **installable on a fresh machine** — one command, plus the private
remote that makes the code reachable in the first place.

### Added

- **Machine installer** (`perun/installer.py`, `python -m perun install --machine`).
  One command takes a bare machine to a working install: prerequisite checks
  (Python ≥ 3.10 and `pip` are required; `git`, the `opencode` CLI, and a
  reachable Ollama are advisory), `pip install -r requirements.txt`, the global
  OpenCode install, and registration of the `PerunBridge` logon task. Flags:
  `--skip-deps`, `--skip-global`, `--skip-task`, `--no-index`; combined with
  `--uninstall` it also removes the task. Because Perun's import graph is
  stdlib-only at module load (`yaml`/`fastapi`/`httpx` are imported lazily), the
  command can bootstrap a machine that has **no** dependencies yet.
- **`PerunBridge` task registration.** The bridge's scheduled task used to be
  created by hand and existed only on the original machine — nothing in the repo
  could reproduce it, so a new machine silently had no Telegram. Now
  `installer.bridge_task_xml()` generates the task deterministically (UTF-16 XML →
  `schtasks /Create /XML /F`) with the settings that keep the poller singular and
  long-lived: `MultipleInstancesPolicy=IgnoreNew`, `ExecutionTimeLimit=PT0S`,
  restart-on-failure (999 × 1 min), a logon trigger for the current user, and an
  `Exec` of `<python> -m perun bridge` in the repo root. Registration is
  idempotent (re-run to repair) and only **starts** the bridge if a Telegram token
  is configured.
- **`bootstrap.ps1`** — the cold-start entry point for a new Windows machine. It
  locates a Python 3.10+ interpreter (honouring `PERUN_PYTHON`) and delegates
  everything else to `python -m perun install --machine`, then creates the
  desktop shortcut to `Perun.cmd`. Switches: `-NoShortcut`, `-SkipDeps`,
  `-SkipGlobal`, `-SkipTask`, `-NoIndex`. It never touches the scheduled task
  itself, so the task contract keeps exactly one implementation.
- `perun status` reports **`bridge_task`** (`windows`, `exists`, `command`,
  `arguments`, `workdir`), parsed from the task's XML rather than from
  locale-dependent `schtasks /V` text.
- `tests/test_installer.py` — 30 tests for the task XML (single-instance
  guarantees, escaping, UTF-16 output), the `schtasks` invocation, prerequisite
  gating, `pip`, orchestration, the CLI flags, and the bootstrap delegation
  contract. `test_installer.py` uses a patchable `installer.is_windows()` so the
  Windows-only paths can run on any platform without faking `os.name`.
- A short **New machine** section in the global instructions template
  (`perun/integrations/templates/INSTRUCTIONS.md`), so an agent working in *any*
  project can point the user at `bootstrap.ps1` / `install --machine`.

### Fixed

- `installer.pip_install()` tolerates both `str` and `bytes` subprocess output
  when summarising pip's result.

### Notes

- Private remote: **`https://github.com/azagor/perun`**. The repo was previously
  local-only, so "install on a new machine" had no distribution path at all.
- `Perun.cmd` is unchanged: it still only `schtasks /run`s an existing
  `PerunBridge` task. Bootstrapping the task is the installer's job.

## v0.3.0 - 2026-10-14

The orchestrator becomes a first-class, always-available agent, and the
dashboard gains a living "storm" layer.

### Added

- **Control plane — the always-present orchestrator.** Perun now registers
  *itself* as a protected project (`perun/control.py`, `config.is_control_path()`),
  so a fresh machine with **zero** projects can still be talked to. The control
  project:
  * is created/kept by `control.ensure()` on server startup, on every Telegram
    `/projects`, and at MCP-server import;
  * can never be deleted (`DELETE /api/projects/{slug}` and `perun_delete_project`
    refuse it) and is badged `orchestrator`;
  * is the default target of `POST /api/runs` when no project is given.
- **Orchestrator chat in the dashboard** (`#/orchestrator`): a streaming chat
  with the top-level agent, reachable with nothing else registered, plus a
  dedicated Settings card for its **model**, **agent**, and **custom
  instructions**.
- **Orchestrator custom instructions** (`Settings.orchestrator_instructions`) — a
  machine-wide persona/rules block injected ahead of every orchestrator prompt
  (they belong to the singleton, not to any user project).
- **Searchable model picker** — a reusable combobox (`modelSelect`) that lists
  every model from every connected provider with a live filter, used for the
  orchestrator, the global default model, and the console.
- **Project-management MCP tools** — `perun_create_project`,
  `perun_update_project`, `perun_delete_project` (requires `confirm=True`, refuses
  control), and `perun_discover`, so an agent can navigate and manage the machine
  the same way the dashboard does.
- **The storm layer** (`web/`): Perun's lightning and glowing eyes as live
  feedback. Three escalating states — **idle**, **awake** (a project touched in
  the last 15 min), and **storm** (an agent run is executing) — drive a canvas
  lightning engine (`perunFx`), a glowing sigil with animated eyes, a topbar
  storm meter, a travelling energy rim + eye pips on working project cards, and a
  full-screen flash when a run starts. All motion respects
  `prefers-reduced-motion`.
- `GET /api/runs/active` — queued/running runs joined with their project
  (`registry.list_active_runs()`), the data behind the live layer.
- **One-click launcher** (`Perun.cmd`, Windows). Double-click to bring Perun up:
  it finds a Python interpreter, resolves the port from `~/.perun/config.json`
  (override `PERUN_PORT`), is **idempotent** (if the dashboard is already
  listening it just opens the browser and exits), opens the dashboard, and runs
  `python -m perun serve`. It **never** starts a Telegram bridge itself — it only
  `schtasks /run`s the single-instance `PerunBridge` task. Overrides:
  `PERUN_PYTHON`, `PERUN_PORT`, `PERUN_NO_BROWSER`.
- **Application icon** — `web/assets/perun.ico` + `perun.png`, a gold lightning
  bolt on the dark card (matching `--gold-light`/`--gold` on `--bg-secondary`),
  generated deterministically by `tools/make_icon.py` (stdlib only). Served as
  the dashboard favicon and used by the desktop shortcut.

### Changed

- `POST /api/runs` with no `project` now targets the orchestrator instead of
  returning HTTP 400.
- `/api/health` reports the control slug (`control`), and `/api/models` returns
  structured `{id, provider, name, label}` entries (plus `providers`/`count`).
- Telegram routing (`_current_project`) keeps the single-user-project
  auto-select and otherwise falls back to the orchestrator; a new
  `/orchestrator` (`/perun`, `/overseer`) command switches to it in place, and
  `/projects` always shows the orchestrator first.
- Project model/agent resolution gained a control-only tier: explicit arg →
  project override → (`control`) `orchestrator_model`/`orchestrator_agent` →
  global default.
- Shared project creation moved to `perun/projects.py`, used by both the HTTP
  wizard and the MCP tools.

### Fixed

- A working project's status pip in the dashboard no longer stays stuck on
  "idle": the card class, pip class, and pip label are recomputed together from a
  single `pipState()` source whenever presence changes (boot also loads projects
  before computing presence, so "awake" pips are correct on first paint).
- The **Telegram bridge can never multiply.** The in-process path
  (`serve --bridge`) now keeps its single-instance lock explicitly
  (`_BRIDGE_LOCK`/`_BRIDGE_THREAD` in `perun/__main__.py`) and refuses a second
  start, instead of relying on an out-of-scope local. Combined with the
  machine-global named mutex (`telegram.PidLock`) and the `PerunBridge` logon
  task's `IgnoreNew` policy, at most one `getUpdates` poller ever runs (a second
  would fight with HTTP 409s).

## v0.2.1 - 2026-10-14

Security hardening for the dashboard API (from a post-0.2.0 code/UI audit) plus a
version-source cleanup.

### Security

- **Cross-site request forgery + DNS rebinding guard** (`perun/server.py`
  `_origin_guard` middleware, `_hostname()`, `_host_allowed()`, `_origin_ok()`).
  The dashboard is a local, unauthenticated API that can start agent runs
  (`POST /api/runs`, executed with `--auto`), so a remote web page must not be
  able to drive it:
  - every `/api/*` request must carry a `Host` header that addresses this machine
    (loopback always; the configured bind address when set) — this blocks **DNS
    rebinding**, where an attacker domain resolves to `127.0.0.1` and the browser
    then treats its page as same-origin;
  - when a browser sends an `Origin`, it must resolve to the same host (or
    loopback) — this blocks **classic CSRF**, including the CORS-safelisted
    `text/plain` request shape.
  A deliberately exposed bind (a concrete LAN address or `0.0.0.0`) still works,
  but `serve()` now prints a prominent warning that the dashboard has no
  authentication.
- **Stored-XSS fixes in the dashboard** (`web/js/perun.js`). Manifest
  **warnings** (derived from repository-controlled manifest content) and
  **session titles** are now HTML-escaped before rendering, and manifest
  **links** pass through a new `safeUrl()` scheme allow-list
  (`http(s)`/`mailto`/relative only). A crafted `perun.md` (`javascript:` link, or
  HTML in an anchor label) can no longer execute script in the dashboard origin —
  which, on a localhost server, would otherwise chain into the fs/git/run APIs.

### Changed

- The reported version now comes from `perun.__version__` (the `/api/health`
  endpoint and the FastAPI app metadata) instead of two hardcoded strings;
  `pyproject.toml` was corrected from a stale `0.1.0` to `0.2.1`.

### Fixed

- The Telegram bridge mis-reported **zero** registered projects as "Multiple
  projects registered" when a plain message arrived with no current project, so a
  fresh install gave confusing advice. It now distinguishes the two cases
  (`perun/telegram.py: run_message`).

### Tests

- `tests/test_server_security.py` (10 tests): loopback/`localhost` hosts allowed;
  foreign `Host` and DNS-rebinding `POST` blocked; cross-origin and `null`
  `Origin` blocked; same-origin `POST` allowed; static routes unguarded; wildcard
  bind allows a LAN host but still blocks cross-origin.
- `tests/test_telegram_routing.py` (14 tests): zero-project behaviour, single
  project auto-selection, multi-project selection, `/project`, `/new`, and the
  deterministic command guards. Suite: **132 passed**.

## v0.2.0 — 2026-10-14

Project management becomes first-class: a creation wizard, per-project Git, custom
project instructions, a self-describing project manifest, and a model picker.

### Added

- **Project creation wizard** (`web/js/perun.js: projectWizard`, `perun/server.py`
  `POST /api/projects`). A four-step flow (Method → Location → Details → Review)
  that can **register an existing folder**, **create a new one** (optionally
  bootstrapping starter files), or **clone a Git remote** into a target path. It
  captures name, kind, description, tags, a per-project default model/agent, and
  custom instructions, can initialise a repository and configure a remote/branch,
  and can index docs immediately. Includes a server-backed **folder browser**
  (`GET /api/fs/list`, `POST /api/fs/mkdir`) and a delete action on every project
  card/panel, gated by a confirmation dialog.
- **Per-project Git integration** (`perun/git.py`, `perun/server.py`). A safe
  wrapper over the `git` CLI (no interactive prompts, explicit args, validated
  remote URLs) exposing `status`, `log`, `init`, `set_remote`, `commit`, `push`,
  and `clone`. Endpoints: `GET /api/projects/{slug}/git`,
  `GET /api/projects/{slug}/git/log`, `POST …/git/init|remote|commit|push`; a
  **Git tab** in the project detail view drives them. Remote URLs are validated
  and cloning/initialisation are explicit.
- **Custom project instructions** (`projects.instructions`, migration in
  `perun/db.py`). Project-scoped rules stored per project and injected by
  `orchestrator.compose_prompt()` into **new** sessions only (continuations
  inherit them from session history), wrapped in
  `<perun:project_instructions>…</perun:project_instructions>`. Editable via
  `GET/PUT /api/projects/{slug}/instructions`, the project **Instructions** tab,
  the wizard, and the MCP tools `perun_get_instructions` / `perun_set_instructions`.
- **Project manifest** (`perun/manifest.py`) — `perun.md`, a Markdown file whose
  **YAML front-matter** describes the project and whose body is free-form notes.
  Structured fields: `version`, `name`, `kind`, `description`, `tags`, `model`,
  `agent`, `instructions`, `docs`, `commands`, `links`, `git`, and typed
  **`anchors`** (`file`, `doc`, `symbol`, `url`, `command`). A plain `perun.json`
  is also accepted (and preserved on write-back). Design rules: the manifest is
  **declarative input** (read on discovery/index/wizard) and never silently
  rewritten by the server — disk writes happen only through explicit
  export/save; unknown keys are preserved (forward-compatible); malformed or
  missing manifests degrade gracefully to discovery heuristics with warnings;
  paths are expected relative to the project root. Merged into the registry
  (`manifest`, `manifest_path`, `manifest_loaded_at` columns) with endpoints
  `GET /api/projects/{slug}/manifest`, `POST …/manifest/refresh|export`,
  `PUT …/manifest`, and MCP tools `perun_get_manifest` / `perun_set_manifest`.
  Scaffolded projects ship a starter `perun.md`.
- **Manifest UI** (`web/js/perun.js`). A **Manifest** tab with a structured
  editor (identity, docs/commands/links key-value rows, anchors, Markdown body),
  "Reload from disk" and "Generate from registry" actions, and warnings display;
  a **manifest card on the Overview tab** renders anchors, links, docs, commands,
  and a safe Markdown rendering (`renderMarkdown`, escaped-by-default, link
  targets restricted to `http(s)`/`mailto`/relative).
- **Model picker.** `GET /api/models` (TTL-cached `opencode models` listing) feeds
  a `<datalist>` wired into the wizard, project config, console, and settings
  model inputs, so a **different default model per project** is easy to choose.
- **Self-registration.** The Perun repository registers itself on service start
  (`discovery.register_self()`), so Perun is one of its own managed projects.
- **Telegram view** reframed as a single **project-agnostic** bot for every
  project (with `/projects`, `/project <slug>` routing) and a live
  `bridge_running` badge from `/api/telegram/status`.

### Changed

- `GET /api/projects/{slug}` now also returns `manifest_body` and
  `manifest_source` so the Overview tab can render the manifest without a second
  request.
- `perun.manifest.dump()` omits empty defaults (`{}`/`[]`/`""`) so generated
  manifests stay readable.

### Tests

- New/extended suites: `tests/test_manifest.py` (14), `tests/test_git.py`,
  `tests/test_scaffold.py`, plus `tests/test_server.py` instructions/manifest
  endpoint coverage, `tests/test_discovery.py` manifest-merge coverage, and
  `tests/test_orchestrator.py` instructions-injection coverage.

## v0.1.0 — 2026-10-14

Initial release. Perun becomes a machine-wide overseer and orchestrator for all
OpenCode projects.

### Added

- **Project registry & discovery** (`perun/registry.py`, `perun/discovery.py`).
  Cheap marker-based classification (`.opencode/`, `opencode.json`, `AGENTS.md`,
  `docs/CODEMAP.md`, `.git/`), depth-bounded scans under configured roots, and
  path-keyed upsert so re-discovery updates rather than duplicates.
- **Unified memory** (`perun/memory.py`, `perun/embeddings.py`). Facts
  (global/project scoped, sensitive-content guarded, deduped, freshness
  weighted) plus a Markdown doc-chunk vector store with CODEMAP-style path and
  identifier extraction. Ollama `nomic-embed-text` embeddings with graceful
  keyword fallback when Ollama is down. Legacy `~/.config/opencode/memory.db`
  import.
- **Agent orchestrator** (`perun/orchestrator.py`). Starts
  `opencode run --format json --auto` with `cwd` set to the target project,
  streams normalized events (text/tool/session/error/end) over an in-process
  `EventBus`, persists runs, logs raw output to `~/.perun/runs/`, and cancels by
  killing the process tree. Semaphore-bounded concurrency.
- **HTTP service + dashboard** (`perun/server.py`, `web/`). REST API, SSE run
  streaming and a global activity stream, and a gold/dark single-page dashboard
  styled after peruninthemachine.com (Dashboard, Projects + detail tabs, Agent
  Console with live output, Memory Explorer, Activity, Telegram, Settings).
- **Shared Telegram bridge** (`perun/telegram.py`). One long-polling process,
  per-chat project/session routing persisted in the DB, Markdown→Telegram-HTML
  formatting, chunking, typing indicator, `/projects /project /new /status
  /stop /remember /recall /index`, single-instance `PidLock`.
- **Global MCP server** (`mcp/perun_mcp.py`) exposing nine `perun_*` tools, and
  an idempotent, reversible global installer
  (`perun/integrations/install_global.py`) that merges `mcp.perun`, global
  instructions, and helper commands into `~/.config/opencode` with a backup.
- **CLI** (`perun/__main__.py`): `serve`, `bridge`, `discover`, `projects`,
  `add`, `status`, `remember`, `recall`, `index`, `import-legacy-memory`,
  `install`/`--uninstall`, `mcp`.
- Documentation (`README.md`, `AGENTS.md`, `docs/CODEMAP.md`,
  `docs/TECHNICAL_DOCUMENTATION.md`, `docs/DESIGN.md`) and a pytest suite.

### Fixed

- **Bridge status no longer collides with the trading_bot bridge.** The
  Telegram single-instance lock used the bare Windows mutex name
  `PerunTelegramBridge`, which is the *same* global name used by
  `trading_bot/scripts/telegram_bridge.py`. Whenever that unrelated bridge was
  running, `perun status` falsely reported `bridge_running: true` (and the
  `~/.perun/telegram.pid` file was absent). The mutex name is now namespaced by
  `PERUN_HOME` (`PerunManagedBridge-<hash>`), and a new side-effect-free
  `PidLock.is_held()` probe drives `perun status` instead of acquire/release
  (which briefly created and deleted the pid file). Regression tests in
  `tests/test_telegram_lock.py`.
- **Provider errors are persisted on the run record.** When `opencode` reports a
  failure only as a stdout `error` event (e.g. a model rejected for the
  account's region), that message was streamed live but never saved, so a
  finished run showed an empty error. `Orchestrator._execute` now collects error
  events and stores the joined text when there is no reply, so the dashboard can
  explain a past failure. Regression test in `tests/test_orchestrator.py`.
- **CLI output is UTF-8 safe on legacy Windows consoles.** `perun recall` (and
  any command that prints retrieved text) crashed with `UnicodeEncodeError` when
  an excerpt contained a character the console code page could not encode — e.g.
  the `→` arrow in Perun's own docs. `main()` now reconfigures stdout/stderr to
  UTF-8 (`errors="replace"`). Regression tests in `tests/test_cli.py`.
