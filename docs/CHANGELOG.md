# Perun — Changelog

All notable changes are recorded here. Single source of truth for release notes.

## Unreleased

_No unreleased changes yet._

## v0.8.54

*Released 2026-10-30.*

## v0.8.53

*Released 2026-10-30.*

## v0.8.52

*Released 2026-10-30.*

### Added

- **`POST /api/projects/{slug}/proposals/{id}/cancel`** and
  `action_proposals.cancel()`: the requester or the project owner can withdraw an
  approved-but-unclaimed proposal. `reject()` only accepts `pending`, so a
  superseded approval used to stay claimable for its whole lifetime with no way
  to retire it.

### Fixed

- **External uploads survive a transient finalize failure.** The reassembly slot
  is now discarded only after `complete_external_execution` returns; a claim that
  expired or was revoked during a slow upload no longer throws away every chunk.
  Base64 decode and hashing run outside the transfer lock.
- **Upload frame validation.** A chunk without an integer `seq` is rejected
  instead of being stored as chunk 0, and `package_begin` requires a 64-hex
  `sha256`.
- **`execution_mode` is empty until something executes.** The column defaulted to
  `perun`, so an approved-unclaimed proposal looked like it belonged to Perun's
  own pipeline; the bridge view omits the field when unset.
- **A failed prompt-file write no longer leaks the prompt.** The temp file holds
  raw chat history and was previously left for the 24 h sweep if the write
  raised.
- **External claims verify the approval token.** `claim_execution` now refuses a
  proposal whose HITL token is missing or corrupt, matching what the local
  execution path already enforced.
- **Legacy `execution_mode` rows are reset** for proposals that never executed, so
  the label no longer claims a pipeline that did not run.

### Documentation

- Refreshed `CODEMAP.md` and `TECHNICAL_DOCUMENTATION.md` for short-lived
  WebSocket tickets, self-service key rotation, external-apply claim locking,
  path/ZIP/HITL hardening, and the PerunChat gateway security controls.

## v0.8.51

*Released 2026-10-30.*

### Fixed

- **`/api/perunchat/ws-ticket` is loopback-only**, matching the legacy key
  endpoint. A remote caller can no longer mint a ticket for the stored key even
  with the Perun API token.

## v0.8.50

*Released 2026-10-30.*

### Fixed

- **The dashboard no longer puts the PerunChat API key in a WebSocket URL.**
  It calls the new `POST /api/perunchat/ws-ticket` proxy, which asks the gateway
  for a 30-second single-use ticket, and connects with `?ticket=`. The raw key
  stays in the server-side request header.

## v0.8.49

*Released 2026-10-30.*

### Fixed

- **External apply locks the claim before writing.** `complete_external_execution`
  atomically moves `claimed → executing` (matching executor and unexpired lease)
  before `apply_package`, so a claim that expired or was taken by another agent
  cannot still write. A stale `executing` row is failed after 12 hours.
- **Handoff paths and ZIP entries are stricter.** Targets containing `$`, `%` or
  `~` are rejected; sensitive-name checks run relative to the allowlist root
  (no false positives from an ancestor named `venv`); trailing dots/spaces are
  normalised; ZIP entries are rejected on case-insensitive duplicates, shell
  characters or backslashes; staging uses `mkstemp`.
- **HITL validation checks the signature before consuming the nonce.** Malformed
  tokens/nonces/expiries return `invalid_signature` instead of raising or
  burning a live nonce; `action_hash` uses canonical JSON for nested data; and
  `HITLConfig` rejects a nonce window shorter than the token TTL + 5 s.
- **Proposal targets are de-duplicated and type-checked**, and a `create`
  proposal must touch every approved target before it can be `executed`.

## v0.8.48

*Released 2026-10-30.*

### Fixed

- **External-handoff success frames carry `ok: true`.** `package_accepted` used
  to omit `ok`, so the MCP client treated a successful upload as `isError: true`
  even though the file had been applied. Rejection frames now carry
  `ok: false` as well. The client additionally treats a matching
  `package_accepted` without `ok` as success for older Perun builds.

## v0.8.47

*Released 2026-10-30.*

## v0.8.46

*Released 2026-10-30.*

### Fixed

- **Local executions also record `executed_by`.** The in-process pipeline now
  writes `executed_by='perun'` alongside `execution_mode='perun'` when it moves
  an approved proposal to `executing`, so the field covers both execution modes.
  `executed_by` names the actor that attempted or applied the change; pair it
  with `status` to distinguish success from failure.
- **Proposal room binding is tested both ways.** Added regression coverage for
  an explicit envelope `room_id` overriding the bound project chat, a retried
  legacy record with an empty room inheriting the current room, and executor
  attribution surviving a failed external completion.
- **`external_execution_claim_ttl` is now honoured and editable.** External
  claims use the configured lease (clamped to 1 s–7 days) instead of the
  hard-coded constant, and the value is exposed in **Settings → External claim
  TTL (s)** / `PATCH /api/settings`.
- **Stalled upload slots are reclaimed on idle, not total time.** Every
  accepted package chunk refreshes the slot deadline, so a slow but progressing
  upload is no longer swept mid-transfer. The idle TTL is configurable as
  `external_execution_package_ttl` (**Settings → Package slot idle TTL (s)**,
  30 min default, 60 s floor), and the sweep/restart loader both honour it.

## v0.8.45

*Released 2026-10-29.*

### Fixed

- **Executed proposals now record who ran them and where they came from.**
  `executed_by` is written when an approved proposal is claimed for external
  execution and again when the uploaded package is applied, using the same
  actor id as `executor_id`. Claim revocation and claim expiry clear both
  fields so a reverted claim cannot look executed.
- **Dashboard/API proposals inherit the bound project-chat room.** A proposal
  created without a chat envelope now falls back to the project's managed
  `project_chats.room_id`, so `room_id` is no longer empty on records that did
  not come through the PerunChat MCP server.
- `perun-proposals.get_proposal` (and therefore `execution_status`) now returns
  `room_id`, `executor_id`, `executed_by` and `execution_mode` alongside the
  existing status fields.

## v0.8.44

*Released 2026-10-29.*

### Fixed

- **The PerunChat log repairs its own ordering.** After inserting a message the
  dashboard re-sorts every timestamped line in the DOM, so restored history or
  an older write path can no longer leave newer messages above older ones.

## v0.8.43

*Released 2026-10-29.*

### Documentation

- Document the lost-`package_begin` retry rule: use a fresh `transfer_id`;
  the old slot expires after the 30-minute TTL and counts against the
  8-transfer cap until then.

## v0.8.42

*Released 2026-10-29.*

### Fixed

- **Directed handoff preserves the claimant's agent-id casing.** Responses use
  the exact authenticated id, and the gateway compares the managed-room owner
  case-insensitively, so mixed-case ids are delivered and validated correctly.
- **Restored upload progress uses exact base64 sizes.** A decoded-chunk length
  avoids rejecting a final chunk because of base64 padding.

## v0.8.41

*Released 2026-10-29.*

### Fixed

- **Persisted transfers retain their room binding.** `package_transfers` now
  stores `room_id`, so an upload can resume after a bridge restart instead of
  being rejected as belonging to another room. Loading prunes stale rows to the
  8-slot cap.

### Tests

- Covered policy auto-approve for a sole `rw` requester, a disabled policy, and
  an already-approved proposal.

## v0.8.40

*Released 2026-10-29.*

### Added

- **Directed external-handoff delivery.** Execution results, download packages
  and upload chunks now use `recipient_id`, so only the intended agent receives
  them and they are never replayed from room history. The gateway rejects
  unaddressed package chunks, `package_ready` or result frames from non-owners,
  and package chunks addressed to anyone other than the room owner.

## v0.8.39

*Released 2026-10-29.*

### Fixed

- **Execution result correlation.** `application/perun-execution-result`
  payloads now always carry `op`, `proposal_id` and `transfer_id`, including
  error responses.

## v0.8.38

*Released 2026-10-29.*

### Added

- **MCP handoff tools for external claimants.** The sibling `perunchat` MCP
  bridge now exposes `perunchat_claim_execution`,
  `perunchat_request_download_package`, `perunchat_upload_package`,
  `perunchat_write_target_file`, `perunchat_execution_status`,
  `perunchat_transfer_status`, and `perunchat_revoke_execution_claim`, so an
  MCP-capable remote agent can run the claim/download/upload flow without
  hand-crafting WebSocket frames.

### Fixed

- **Package transfers cannot be hijacked or reset.** `package_begin` rejects a
  duplicate `transfer_id` instead of replacing an in-flight slot, caps
  concurrent uploads at 8, and refuses to overwrite a persisted row owned by a
  different sender. A bad transfer id no longer forces the victim to restart.
- **Transfer test isolation.** `package_transfers` is cleared between tests.

### Documentation

- Refreshed the CODEMAP, technical reference, design notes, README, Shared
  Projects PRD, and project context docs against the v0.8.37 codebase:
  MCP-based proposal/approval flow, bounded autonomous peer review,
  approve-only defaults, external ZIP handoff caps/rollback, manifest
  dependency graph and `deploy-plan`, and dashboard asset cache-busting.

## v0.8.37

*Released 2026-10-29.*

### Fixed

- **The dashboard can no longer serve a stale chat script.** `GET /` rewrites
  the `?v=` asset query strings to the running Perun version and the static
  mount sends `Cache-Control: no-cache` for JS/CSS/HTML (validated by ETag).
  Browsers that had cached `perun.js?v=0.7.47` kept rendering PerunChat in
  arrival order long after the timestamp-sorting fix shipped.
- **The public live chat renders messages chronologically.** Timestamped frames
  are inserted at their sorted position instead of appended, restored
  localStorage history is sorted before rendering (and no longer swallowed by
  its own dedup check), and a reply always appears after its parent.

## v0.8.36

*Released 2026-10-29.*

### Fixed

- **Download packages are size-capped.** `pack()` enforces the configured
  package/entry/entry-count caps before building the archive, so a claimant
  cannot turn a large directory target into an unbounded download.
- **Upload transfers are byte-capped.** `package_begin` requires `sha256`;
  chunks are base64-validated on receipt, per-chunk and cumulative bytes are
  capped against the announced total, and duplicate chunks are not double-counted.
- **Audit diffs ignore sensitive paths.** `fs_audit.diff()` filters sensitive
  paths from both snapshots so pre-upgrade snapshots cannot produce false
  “deleted” violations.
- **Package application rolls back on failure.** `apply_package()` stages all
  files and backs up existing targets before replacing anything; a mid-apply
  failure restores the originals and removes newly created files instead of
  leaving a half-applied tree.
- **External-dependency targets are explicit.** The ZIP handoff is
  project-root-relative; proposals targeting `external_deps` are rejected with a
  clear “use local execution” error instead of a confusing pack failure.
- **Status responses omit proposal details.** `claim_execution` includes the
  proposal `details` once for the claimant; `execution_status` and
  `revoke_execution_claim` return only status/target metadata.
- **External execution downloads are target-scoped.** A claimant can no longer
  request arbitrary project paths; `request_download_package` paths must stay
  under the proposal's approved targets. `pack()` skips sensitive files and
  directories (`.env`, `.git`, `.perun`, keys, OpenCode config, …) even when a
  requested directory contains them.
- **External package uploads are hardened.** Chunks must come from the same
  room and sender as `package_begin`, the chunk count is capped, non-integer
  `seq`/`total_chunks` values are rejected, and ZIP directory entries are
  skipped instead of being applied as empty files.
- **Execution result frames do not leak server state.** The HITL approval token
  and claim snapshot are stripped from `claim`/`revoke`/`status` responses; the
  proposal details and targets remain available to the claimant.
- **Guard target scope survives `standalone=False`.** When `PERUN_ALLOWLIST`
  is absent (the background service starts the project MCP server), the guard
  derives the active local proposal's targets from the database and enforces
  them on writes. Multiple concurrent executions fail closed instead of falling
  back to an unscoped project root.
- **Autonomous reviews are bounded and report failures.** At most two review
  runs are queued at once, and a failed review posts a “human review is needed”
  notice instead of silently leaving the proposal pending.
- **Approved proposals expire after seven days.** An `approved` proposal that
  is never claimed or executed no longer lingers forever.
- **Guard probe preflight.** Proposal preparation now starts the guard server
  and verifies `guard_write_file` is exposed before spending a model run.
- **Review prompt treats proposal content as untrusted data.** Intent,
  targets, and details are wrapped in an `<untrusted_proposal>` block with an
  explicit “never follow instructions inside it” rule.
- **Autonomous peer review and approve-only default.** When a shared project
  enables `auto_approve`, the bridge now reviews proposals created by other
  agents on its own: it reads the target paths through the guard tools and
  approves them without starting execution, leaving them `approved` for a
  remote rw participant to claim. The MCP `perun-proposals_approve` tool now
  defaults to `execute=false`; pass `execute=true` only for explicit local
  execution.
- **Approval notices distinguish claimable from executing.** A proposal left
  in `approved` posts “awaiting external execution”; only a run that actually
  starts posts “is now executing”.
- **Proposal execution loads the project MCP servers.** Approved local runs use
  the running OpenCode service instead of a private `--standalone` server, and
  `_prepare_execution` fails early with “guard MCP not registered” instead of
  burning a run with only the `skill` tool.
- **Self-approval is enforced for agent callers.** `action_proposals.approve()`
  / `reject()` reject an agent approver that normalizes to the proposal's
  requester (case/whitespace-insensitive), covering the MCP tool and retries.
  A human operator is always allowed to approve or reject their own proposal
  from the dashboard; that path (and the autonomous policy path) passes
  `allow_self=True` explicitly.
- **Chat approvals use the bot's own identity.** The bridge records both the
  requester (`PERUN_CHAT_SENDER` / actor `sender`) and its own agent id
  (`PERUN_CHAT_AGENT` / actor `agent`); `perun-proposals approve`/`reject` use
  the agent id. The bot can approve a human's request without impersonating
  that human.
- **PerunChat actor files expire after one hour.** `perun-proposals` ignores a
  stale per-room actor file, so a later TUI session cannot approve a proposal
  as the last chat sender.
- **`perunchat` also denies the `skill` loader.** The chat agent can no longer
  mistake the built-in `skill` tool for a guard/proposal tool and conclude the
  real tools are missing.
- **The release pipeline keeps every version source in sync.** `tools/release.py`
  now bumps `perun/__init__.py`, `pyproject.toml`, `installer/PerunSetup.iss`,
  and the CODEMAP `__version__` line together.
- **`list_proposals` treats `"null"` as no filter.** Models sometimes pass the
  literal string `"null"` for an omitted optional status, which hid every
  proposal and made the bot report “no proposals at all”. The tool now
  normalizes `null`/`none`/`any`/`all`/empty to no status filter.
- **Chat participants verify execution by reading the target.** The
  `perunchat` prompt now requires a `perun-guard_guard_read_file` (or
  `list_directory` / `search_files`) read-back after an execution/state digest
  and reporting the actual content; a digest alone is not proof.
- **Shared-project execution uses the exact guard tool names.** The
  `perun-shared` agent now allows the prefixed `perun-guard_guard_*` tools,
  denies the built-in `skill` loader, and tells the model to retry the guard
  tool if it accidentally calls `skill` and gets “Unable to load skill …”. The
  execution prompt names the same tools. This unblocks the CANARY.md canary,
  which now executes and writes the expected three lines.
- **PerunChat agent prompt uses the exact MCP tool names.** The `perunchat`
  persona now names `perun-proposals_list_proposals`, `perun-proposals_approve`,
  `perun-proposals_propose`, `perun-proposals_get_proposal`, and the
  `perun-guard_*` read tools, instead of the unprefixed labels. This stops the
  agent from claiming the tools are absent when they are actually connected.
- **PerunChat `approve`/`reject` work with a static MCP environment.** The
  bridge now writes the per-message sender to a per-room actor file under
  `PERUN_HOME/perunchat_actors/`; `perun-proposals` falls back to it when
  `PERUN_CHAT_SENDER` is absent, so the room owner can approve a peer's
  proposal instead of failing with `PERUN_CHAT_SENDER is not set`.

## v0.8.35

*Released 2026-10-29.*

### Added

- **Shared-config health check and per-project hint.** The dashboard
  **Health check & fix** action now also re-syncs every shared project's
  `opencode.json` so `perun-guard` / `perun-proposals` carry
  `PERUN_PROJECT_PATH`. The Shared tab warns when a project's config is stale
  and offers a per-project **Sync config** button. Both paths are
  project-agnostic: the global action walks the registry and only touches
  projects whose manifest says `shared: true`.

### Fixed

- **`perun-proposals` is bound to the project in the TUI.** The generated
  shared-project `opencode.json` sets `PERUN_PROJECT_PATH=.` for both
  `perun-guard` and `perun-proposals`, and the proposals MCP server falls back
  to its process cwd when the variable is absent. `list_proposals` no longer
  fails with `PERUN_PROJECT_PATH is not set` for a project-local MCP server
  launched by OpenCode.
- **Release script no longer corrupts the changelog.** `tools/release.py` now
  promotes the existing Unreleased entries into the new version section instead
  of leaving them under a fresh empty Unreleased heading. The technical-doc
  revision entry also drops the placeholder bullet.

## v0.8.34

*Released 2026-10-29.*

### Fixed

- **PerunChat bridge runs bound project chats against the bound project.** The
  reply loop now passes the shared-project slug to `run_and_wait` for managed
  `grp:` rooms, with `agent="perunchat"` and the configured chat model, instead
  of always falling back to the control-plane project. Unbound/public rooms
  continue to use `control.ensure()`.

- **Portable MCP config and correct chat permissions.** Shared-project
  `opencode.json` now writes `PERUN_HOME=~/.perun` for the default home instead
  of embedding the absolute user path, so committed project configs do not leak
  the OS username. The `perunchat` agent denies `*guard_write_file` and
  `*guard_edit_file` (the real V2 action names are `<server>_<tool>`) and allows
  `perun_perunchat_room_memory` for older room context.

## v0.8.33

*Released 2026-10-29.*

### Fixed

- **No more blinking console windows on Windows.** Background calls to the
  OpenCode CLI (`opencode models`, `opencode auth list`, `opencode auth export`,
  `opencode auth logout`, `opencode reload`) and installer/search subprocesses
  now set `CREATE_NO_WINDOW`, so the dashboard and bridge no longer flash a
  visible `cmd.exe` window.

## v0.8.32

*Released 2026-10-28.*

### Added

- **Per-project default `plan` and `build` models.** The project manifest and
  dashboard Config tab now support an `agent_models` map (`plan`, `build`, and any
  future agent id → model id). Perun resolves `agent_models[agent]` before the
  generic `project.model` when starting a run, and mirrors the values into the
  project's `opencode.json` under `agent.<id>.model` so the OpenCode TUI can honor
  them. Control-plane runs ignore project per-agent defaults.


## v0.8.31

_Released 2026-10-28._

### Added

- **Anchoring actually resolves.** Manifest anchors are now matched by
  token/substring label as well as exact label, and file/doc anchors fall back
  to an indexed ``doc_chunks`` section or a small stub with fetch instructions.
  ``perun_context("CODEMAP")`` and ``perun_context("the technical documentation")``
  now return a bounded hit instead of an unrelated semantic match.
- **CODEMAP-aware chunking.** ``docs/CODEMAP.md`` is split at ``###`` module
  headings, producing chunks titled ``<H2> › <file>`` and capped at ~2.5 KB.
  Retrieval names the module it describes instead of returning a generic
  "part N" slice.
- **Compact CODEMAP outline.** ``perun context sync`` and ``perun docs index``
  generate and index a ``<slug>.codemap`` outline context doc from the CODEMAP,
  giving agents a ~2–4 KB navigation map instead of the full CODEMAP.
- **Deterministic morphology for prefetch.** ``_stem()`` now handles ``-ing``,
  ``-ed``, ``-ation``, ``-tion``, ``-er``, ``-ly``, ``-ness`` and a small
  equivalence map, so ``release``/``releasing``/``released`` share a stem.
- **``perun docs audit``.** New CLI command that parses ``docs/CODEMAP.md``,
  checks every backticked file path exists, verifies code identifiers against
  the referenced Python files via AST, and ensures the package version appears
  in CHANGELOG and CODEMAP.
- **Embedding-assisted anchor/prefetch fallback.** When Ollama is available,
  anchor label similarity and prefetch use local ``nomic-embed-text`` vectors
  as a fallback; when Ollama is down the deterministic rules still work.

### Fixed

- **Prefetch no longer drops documents > 4 KB.** The first matching doc is now
  truncated to the remaining budget instead of being skipped, so exact-key
  requests for large context docs (e.g. ``perun.agents.full``) always return a
  hit and cannot be displaced by a smaller, unrelated doc.
- **Stale vectors cannot mis-rank results.** ``context._semantic()`` and
  ``_anchor_path_hit()`` now ignore embeddings whose ``embedded_hash`` does
  not match the current ``content_hash``.
- **Removed unused ``perun docs audit --strict`` flag.** The command always
  exits non-zero when drift is found.
- **Owner-level maintenance commands include hidden projects.**
  ``perun docs audit --all``, ``backfill-docs --all``, ``validate-docs --all``,
  and ``refresh-deps --all`` now list hidden projects when the current process
  owns Perun's data directory (always on Windows, uid-checked on Unix).

## v0.8.30

_Released 2026-10-28._

### Added

- **Memory observability.** The metrics already collected by the lifecycle work
  are now exposed through CLI, API, and dashboard:
  - CLI: `perun memory stats [--project] [--json]`,
    `perun memory list [--project] [--kind] [--limit] [--json]`,
    `perun memory repair [--limit]`.
  - API: `GET /api/memory/usage` returns a rolling-window snapshot of
    search/prefetch activity, zero-result rate, and average results per search.
  - Dashboard Memory tab now shows facts by kind, top-hit rules, zero-hit rules,
    pending embeddings count, recent usage stats, and a "Repair embeddings"
    button.
- **In-memory search/prefetch telemetry.** `search_facts`, `search_docs`,
  `unified_search`, and `prefetch_rules` record tiny events to a bounded,
  in-memory deque (2 000 events). Data is scoped by project in `usage_stats()`
  and never persisted.
- **Extended memory stats.** `memory.stats()` now reports `facts_zero_hit`,
  `oldest_unhit_rule`, `facts_expiring_7d`, and `facts_expiring_30d`.

### API / CLI

- `GET /api/memory/stats` and `GET /api/memory/usage` accept an optional
  `project` slug query parameter.
- `POST /api/memory/reembed` is wired to the dashboard Repair button.

## v0.8.29

_Released 2026-10-28._

### Added

- **Memory fact lifecycle.** Facts can now be corrected and retired:
  - `memory.forget(id)` deletes one fact.
  - `memory.supersede(id, content)` replaces a fact while preserving its stable id.
  - `remember(..., valid_to=...)` sets an ISO-8601 expiry; expired facts are
    invisible to search and removed by `prune()`.
  - `memory.touch_fact(id)` records usage; `search_facts()` updates `hit_count`
    and `last_used_at` automatically.
- **Near-duplicate merge for rules/decisions.** Storing a `kind='rule'` fact that
  is semantically ≥ 93 % similar to an existing rule updates the existing row
  instead of minting a new one, keeping the rule corpus compact.

### Changed

- **Calibrated memory ranking.** The old model multiplied relevance by freshness
  and used a stopword-blind `LIKE` pre-filter ordered by `id DESC`, which erased
  old global rules and let common words drown results. The new model:
  - removes the recency pre-filter (candidates are now scored, not discarded);
  - adds stopword filtering and a bounded additive keyword bonus;
  - treats freshness and popularity as tie-breakers, not multipliers;
  - keeps semantic and keyword scores on the same scale for facts and docs so
    `unified_search()` no longer systematically promotes facts over docs.

### API / CLI

- New endpoints: `DELETE /api/memory/fact/{id}`,
  `POST /api/memory/fact/{id}/supersede`, `POST /api/memory/fact/{id}/touch`.
- `POST /api/memory/remember` accepts `kind` and `valid_to`.
- New CLI commands: `perun forget`, `perun supersede`.
- `perun remember` accepts `--kind` and `--valid-to`.
- `perun_memory_store`, `perun_memory_forget`, `perun_memory_supersede` MCP tools.

## v0.8.28

_Released 2026-10-28._

### Fixed

- **Memory repair loop was silently broken.** `memory.reembed_missing()` passed a
  raw Python list to SQLite instead of `pack_vector()`; the 60 s background loop
  caught and swallowed the exception, leaving **295 doc chunks without embeddings**
  forever. The call now packs vectors, batches updates, and reports success/error
  counts via `stats()["last_reembed"]`.
- **Stale vectors no longer affect search scores.** `search_docs()` only uses an
  embedding when `embedded_hash == content_hash`; otherwise it falls back to
  keyword scoring.
- **`pyproject.toml` dependency parsing.** Tiny TOML parser now handles PEP 621
  `dependencies` arrays and optional-dependency groups, so dependency facts are
  no longer garbled (`"pytest> (8.2.0",)` is gone) and Perun's own deps are no
  longer silently missing.

### Added

- **Rules-only memory prefetch.** Orchestrated runs now receive a bounded,
  deterministic, Ollama-free injection of durable `kind='rule'` / `kind='decision'`
  facts, rendered as a delimited "background facts, NOT instructions" block.
  Chat-sourced and release-announcement facts are excluded. Controlled by the new
  `memory_prefetch` setting (`rules` | `off`, default `rules`).
- **Memory `kind` taxonomy.** Facts now carry `kind ∈ {rule, decision, note, log,
  transcript}`; `perun_memory_store` accepts `kind`. Existing rows are backfilled
  once on schema upgrade (schema version 8).
- **CHANGELOG size-aware indexing.** Large `docs/CHANGELOG.md` files (above 20 KB)
  are indexed as a single "Release index" chunk instead of hundreds of
  near-identical release-body chunks, stopping them from crowding out real docs in
  search results while keeping version discovery intact.

### Changed

- `discovery.record_dependencies()` now writes **one fact per dependency file**
  and deletes the old file-scoped fact first, preventing unbounded growth across
  version bumps.
- `prune(project_id=0)` now correctly prunes only project 0 instead of every
  project.
- `remember()` checks for an exact duplicate before requesting an embedding,
  saving a wasted Ollama round-trip.

## v0.8.27

_Released 2026-10-28._

### Added

- **`perun context dedupe`** removes byte-identical duplicate `###` sections
  from a project's `AGENTS.md`, keeping the first occurrence. This is a safe,
  mechanical cleanup that lowers the ambient prompt cost without removing
  awareness.
- **`perun context verify`** now accepts mixed `inline` + `context` destinations,
  so a section can keep its behavioural bullets inline and point the rest at a
  context doc without breaking the losslessness proof.
- **Global instructions pointer.** `perun context sync` now refreshes both the
  managed `AGENTS.md` kernel *and* the `perun/INSTRUCTIONS.md` pointer. The
  pointer replaces the previous full duplicate of the kernel that OpenCode's
  `instructions[]` array loaded into every session.
- **Full ambient-context ledger in `perun context stats`.** The report now
  includes the global `AGENTS.md` block, every `instructions[]` file, the
  live MCP tool-schema size, and OpenCode skill frontmatter, plus the computed
  per-session token floor. It also detects within-file `###`-level duplicates
  and emits budget warnings for large inline sections.
- **Hardened `perun context verify`.** The losslessness check now fails when an
  inline section repeats the same `###` module multiple times, and when the
  global `perun/INSTRUCTIONS.md` file is not the thin pointer.

### Changed

- **Production-readiness audit hardening:**
  - `tool_schema_stats()` now degrades gracefully if a live tool schema cannot
    be serialised.
  - `_context_instruction_files()` resolves `instructions[]` entries strictly
    inside the global config directory or the project root; escaping paths are
    ignored.
  - The `perun context verify` size guardrail now measures the *live* inline
    portion of a section, so mixed `inline` + `context` destinations report an
    accurate ambient cost.
  - New regression tests cover mixed destinations, repeated inline
    subsections, `perun context dedupe`, `perun context stats`, pointer drift,
    path escaping, and the thin instructions pointer lifecycle.

## v0.8.26

### Fixed

- **Schema v7 migration ordering.** Existing databases that predated the
  `memory.room_id` column failed to start after updating to v0.8.24 because
  `db.init_db()` executed SCHEMA (which creates an index on `room_id`) before
  running migrations that add the column. `_migrate()` is now invoked before
  `executescript(SCHEMA)`, and again afterward for newly-created tables.

## v0.8.25

_No user-facing changes; placeholder entry correcting the version tag._

## v0.8.24

### Added

- **Confined filesystem MCP tools.** `mcp/perun_mcp.py` now exposes
  `perun_file_exists`, `perun_list_directory`, and `perun_read_file`. Read
  access is scoped to registered project paths, the Perun home/config trees,
  and the Perun runtime source tree; paths outside those roots are rejected.
  These tools let agents running in confined JavaScript environments (e.g. the
  OpenCode `execute` runtime, which has no `require`/`fs` access) inspect files
  without leaving the Perun MCP tool surface.
- **Release automation script** (`tools/release.py`). One command bumps the
  version, updates docs, syncs internal project content to the private
  `perun_internal` companion repo, commits, tags, and pushes both repos.

### Changed

- Updated `docs/CODEMAP.md` and `docs/TECHNICAL_DOCUMENTATION.md` to list all
  current MCP tools, including `perunchat_room_memory` and the new filesystem
  helpers.
- Hardened the global instruction kernel
  (`perun/integrations/templates/INSTRUCTIONS.md`) to require Perun's bundled
  MCP search tools (`perun_unified_search`, `perun_grep`, `perun_ast_grep`,
  global variants, `perun_memory_search`) over raw `grep`, and to point agents
  at the new filesystem MCP tools when direct filesystem access is unavailable.

## v0.8.23

_No user-facing changes; placeholder entry._

## v0.8.22

### Fixed

- **Orchestrator chat works with Ollama Cloud and other local-style providers
  on Windows.** The root cause was the way `opencode` is launched: on Windows it
  resolves to a `.cmd` file executed via `cmd /c`. Even prompts well below the
  8191-character cmd.exe limit were being mangled by cmd.exe's parser when they
  contained newlines or Unicode, so the model saw an empty instruction block
  instead of the user's request. `_execute` now forces a temporary prompt file
  whenever the resolved `opencode` invocation goes through `cmd /c`, and it
  still auto-switches for long prompts on other platforms. Control-plane runs
  also stop injecting the Perun source-code project's own
  `.perun/instructions/*.md` files, cutting the prompt from ~13K to ~2.7K
  characters. The orchestrator persona now mentions `perun_context` so the
  overseer can fetch reference docs on demand instead of carrying them inline.
- **PerunChat prompt cap reduced from 100K to 12K characters.** PerunChat
  already passes prompts via a temp file, so it was not affected by the Windows
  command-line bug, but the 100K cap left too much room for models with smaller
  context windows. The lower cap trims older room history first and keeps the
  latest message, making reply generation more reliable on 8K-context local
  models.

### Added

- **PerunChat room memory redesign.** Private, group, and shared-project rooms
  now store searchable room memory instead of inlining entire conversations
  into every prompt:
  - New `room_id` column on the `memory` table (schema v7) with a covering
    index, plus `memory.search_room_facts()` for scoped recall.
  - New MCP tool `perunchat_room_memory(query, room, k)` lets agents query room
    history on demand.
  - PerunChat stores transcript facts for private/group/project rooms, pruning
    each room to the most recent 250 transcripts. Public-room transcripts are
    intentionally skipped.
  - Inline recent history reduced from 6 to 4 messages for rooms that have
    long-term memory; public rooms keep 6 messages inline because they have no
    room memory.
  - Updated perunchat agent instructions and `perun/instructions/perunchat_chat.md`
    to describe the new recall tool and public-room ephemeral behavior.

### Changed

- Updated `docs/CODEMAP.md`, `docs/TECHNICAL_DOCUMENTATION.md`, and `README.md`
  to reflect v0.8.21: added `perun/package_handoff.py`, the `/api/shutdown`
  endpoint, external execution handoff details, and the local-host-agent
  execution-path clarification.

## v0.8.21

### Changed

- **Clarified shared-project execution paths for the local Perun agent.** The
  project-chat agent instructions now explicitly state that the local host
  agent (running on the same machine as the project files) should use the
  normal shared-project execution pipeline after approval, not the remote
  ZIP-over-PerunChat handoff. The external execution protocol reference is now
  clearly scoped to remote/external rw participant agents only.

## v0.8.20

### Added

- **Dashboard shutdown button.** Added a **Shutdown Perun** button to the
  topbar, next to Quick Run. It calls `POST /api/shutdown`, which gracefully
  signals uvicorn to exit, so users can stop the local service without leaving
  the dashboard. A confirmation dialog prevents accidental shutdowns, and the
  button points out that Perun can be restarted with the usual shortcut.

## v0.8.19

### Fixed

- **Dashboard shows the Perun control project for the owner again.** The
  control plane is intentionally hidden from the default `GET /api/projects`
  list so external/shared installs do not see the source-code project. The
  local dashboard is owner-only, so it now requests
  `/api/projects?include_hidden=true` and renders the Perun project alongside
  the user's own projects. This fixes a regression where the owner's dashboard
  appeared to lose the Perun project after v0.8.17.
- **Orchestrator chat now honors the orchestrator model setting.** The control
  project can carry a stale model override (e.g. `opencode/kimi-k2.7-code`), and
  it used to take precedence over the dedicated `orchestrator_model` /
  `default_model` settings. The orchestrator chat now ignores the self-registered
  control project's stored model and uses `orchestrator_model` → `default_model`,
  so requests to use Ollama DeepSeek models actually reach Ollama Cloud instead of
  falling back to a cloud provider blocked by privacy settings.
- **Telegram `/projects` only exposes the Perun project to the static owner
  allow-list.** Runtime-paired chats are still authorised to talk to the bot,
  but they no longer see the hidden Perun orchestrator project in the project
  list or keyboard.

## v0.8.18

### Fixed

- **PerunChat bridge now starts after saving credentials in the dashboard.**
  The bridge previously cached its settings object at startup; when the
  dashboard saved PerunChat credentials, `server.py` replaced
  `ORCH.settings` with a new object, so the bridge kept reading the old
  `perunchat_auto_reply = False` value and reported `bridge: stopped`. The
  `settings` property now always reads from `self.orchestrator.settings`, and a
  regression test guards the fix.

## v0.8.17

### Added

- **External execution handoff for shared projects.** RW participants (or the room
  owner) can claim an approved proposal and upload a ZIP of file changes over
  PerunChat using their own model/tokens; Perun validates and applies the package
  locally without burning its own LLM tokens. Uses `application/perun-execution`
  control frames and `application/perun-package` base64 ZIP chunks over the
  existing WebSocket. Added `claim_execution`, `complete_external_execution`,
  `revoke_execution_claim`, claim expiry sweep, and guards on local
  execution/deletion of claimed proposals.
- **Safe ZIP work-package module.** New `perun/package_handoff.py` packs,
  unpacks, validates, and applies ZIP archives with guards against traversal,
  symlinks, encrypted entries, zip-bombs, size caps, and writes outside the
  approved targets.
- **Persisted transfer reassembly.** Upload transfer slots are persisted to
  SQLite (`package_transfers` table) and reloaded on bridge restart so interrupted
  chunked uploads can resume within the 30-minute TTL. Added `transfer_status`
  control op so external agents can query received/missing chunks.
- **Dashboard external-execution UI.** Proposal cards now show `claimed` status,
  claim expiry countdown, a revoke button, clickable target chips, and polling.
  Added `POST /api/projects/{slug}/proposals/{id}/claim` and
  `.../revoke-claim` endpoints.
- **Documentation and instructions.** New `perun/instructions/perun-external-execution.md`
  protocol reference; updated `perun/instructions/perunchat_chat.md`, the
  `[shared-project-context]` block, and approval notices to reference the handoff.

### Changed

- **Schema version 6.** Added `executor_id`, `execution_mode`, `claimed_at`,
  `claim_expires_at`, `claim_snapshot`, and `executed_by` columns to
  `action_proposals`, plus the new `package_transfers` table.
- **Shared-agent install now mirrors within the same config base.**
  `ensure_shared_agent()` writes the restricted agent to both `agents/` and
  `agent/` under the canonical OpenCode config directory and returns the
  canonical path, fixing test drift from multi-base auto-install.

### Fixed

- **OpenCode TUI providers now appear on the Perun providers page.**
  `perun.providers` now tolerates list-form `providers` sections in
  `opencode.json[c]`, normalises key capitalisation from `opencode auth list`,
  and falls back to deriving connected providers from `opencode models` when
  neither auth nor configured providers are present.
- **`perunchat.md` agent loads cleanly.** Removed the invalid `tools.available`
  list from the generated PerunChat agent front matter so OpenCode parses and
  installs the agent successfully.
- **Control plane hidden from the dashboard project list.** The self-registered
  `Perun` source-code project is now marked `hidden` on registration and excluded
  from `GET /api/projects` by default (opt-in via `?include_hidden=true`).
- **Dashboard project list hides the orchestrator; Telegram still shows it to
  the owner.** The self-registered Perun control project is excluded from
  `GET /api/projects` (and therefore the web dashboard project list), but it
  remains visible and selectable in the Telegram `/projects` list and keyboard
  for the paired owner chat. It is also still reachable via `/orchestrator`
  and as the default no-project target.
- **Windows installer covers all runtime requirements.** Added Node.js 22.14.0
  LTS as a first-class prerequisite component in `installer/PerunSetup.iss`
  and `installer/install-prereqs.ps1`; the machine-install health check in
  `perun/installer.py` now reports Node.js presence too.
- **Shared-project MCP servers stay in sync.** `sync_manifest()` and the
  manual `POST /api/projects/{slug}/shared/sync` endpoint now call
  `sync_opencode_config()` so `perun-guard` and `perun-proposals` are added
  to (or removed from) the project's `opencode.json` whenever the manifest or
  room ACL is reconciled, not only when shared settings are explicitly updated.
- **Provider and MCP discovery is more robust across OpenCode installs.**
  Global OpenCode config discovery now reads from all known locations
  (``~/.config/opencode`` plus ``APPDATA``/``LOCALAPPDATA`` opencode dirs on
  Windows), so providers and MCP servers added through the OpenCode
  desktop/TUI are visible in Perun even when they use a platform-specific
  config directory. ``opencode auth list`` output is also accepted in both
  top-level-list and ``{"providers": [...]}`` wrapped forms.

### Security

- **Installer ships source code, not owner documentation.** `perun.md`,
  `AGENTS.md`, `README.md`, `docs/`, and `.perun/` are now excluded from the
  Windows (`installer/PerunSetup.iss`) and macOS (`installer/macos/build.sh`)
  installers so external users receive a working Perun service without the
  project manifest, deployment details, or local context docs. Those internal
  project files belong to the private companion repo
  **<https://github.com/azagor/perun_internal>**. The external-execution
  protocol reference lives in the shipped
  `perun/instructions/perun-external-execution.md` so it remains available to
  users. The control project still self-registers safely when `perun.md` is
  absent.

## v0.8.16

### Added

### Changed

- **Proposal completion now syncs the project chat ACL.** `_on_proposal_completed`
  uses `projects.sync_manifest(..., sync_chat=True)` so participant changes made
  by a shared-project proposal are reflected in the PerunChat room immediately.

## v0.8.13

### Added

- **Tests for v0.8.12 shared-project features.** Added regression tests for
  `_on_proposal_completed` (manifest sync + doc re-index) and for manifest
  dependency rendering in `_shared_project_context`.

## v0.8.16

### Added

- **PerunChat agent auto-install and health check.** The `perunchat` and `perun-shared` OpenCode agents are now auto-installed/updated in both `agents/` and `agent/` config directories (respecting `OPENCODE_CONFIG_DIR`/`XDG_CONFIG_HOME`), fixing cases where the agent was missing on other instances. Added `GET/POST /api/health/check` endpoint and a "Health check & fix" button on the Dashboard. Broader config discovery fallbacks added.

## v0.8.15

### Added

- **PerunChat agent auto-install and health check.** The `perunchat` and `perun-shared` OpenCode agents are now auto-installed/updated in both `agents/` and `agent/` config directories (respecting `OPENCODE_CONFIG_DIR`/`XDG_CONFIG_HOME`), fixing cases where the agent was missing on other instances. Added `GET/POST /api/health/check` endpoint and a "Health check & fix" button on the Dashboard.

## v0.8.12

### Added

- **Post-proposal doc refresh.** When a shared-project action proposal finishes,
  Perun now re-syncs the project manifest (including `dependencies:` edges) and
  re-indexes project docs before posting the State Digest, so agents in the
  project chat see the latest manifest and dependency graph immediately.
- **Project dependencies in chat context.** The `[shared-project-context]` block
  now includes cross-project dependencies declared in `perun.md`, capped at 20
  entries.

## v0.8.11

### Changed

- **Longer PerunChat dashboard history.** The dashboard chat panel now keeps
  500 messages in memory and 500 in `localStorage` (up from 200/100), matching
  the increased gateway replay buffer.

### Added

- **Memory MCP tools in PerunChat instructions.** The bridge persona now lists
  `perun_memory_store` / `perun_memory_search` as preferred tools for durable
  facts, with `[FACT: ...]` markers kept as a legacy fallback.

## v0.8.10

### Fixed

- **Dashboard chat robustness.** Messages without a usable timestamp now sort to
  the end of the log instead of the beginning, and the operator-message cleanup
  loop no longer mutates the `Set` while iterating.

## v0.8.9

### Fixed

- **Installer version alignment.** `installer/PerunSetup.iss` now matches the
  package version (`0.8.9`) so the Windows installer reports the correct
  release.

## v0.8.8

### Fixed

- **Version source consistency.** `pyproject.toml` now matches `perun.__version__`
  so package builds report the same version as the running service.
- **Dashboard chat history cleanup.** When the PerunChat panel's message history
  hits its cap, the oldest DOM line is removed along with the in-memory entry,
  keeping the rendered log and `messageHistory` in sync.

## v0.8.7

### Added

- **PerunChat dashboard online count.** The dashboard auto-join room picker now
  shows the number of authenticated agents currently online in each room
  (`online_count` from `GET /api/perunchat/rooms`), alongside the durable member
  count.

### Fixed

- **Dashboard chat message ordering.** The PerunChat panel now keeps messages
  sorted by `timestamp` when restoring from `localStorage` and when inserting
  out-of-order replays or recovered state digests. This prevents old messages
  from appearing at the bottom of the project chat after reconnects or page
  refreshes.

## v0.8.6

### Added

- **Cross-project dependency graph.** Projects now declare dependencies in
  `perun.md` under a `dependencies:` list (slug, relationship, reason,
  deploy_command, deploy_doc, auto_deploy, plus extension keys). The graph is
  normalized into the new SQLite `project_dependencies` table on every manifest
  refresh, making impact analysis and deploy planning fast and queryable.
- **Impact analysis.** `registry.analyze_impact(slug)` walks the upstream graph
  with cycle detection and returns every project affected by a change, ordered
  by graph depth.
- **Deploy planning.** `registry.get_deploy_plan(slug)` produces an ordered
  checklist of deploy steps for the changed project and its dependents. Auto-
  deploy items are flagged and skipped by default.
- **CLI for dependencies and deploy planning.**
  `python -m perun dependencies <slug> [--impact] [--json]` and
  `python -m perun deploy-plan <slug> [--changed FILE ...] [--include-auto] [--json]`.
- **REST endpoints for the dependency graph.**
  `GET /api/projects/{slug}/dependencies`, `/dependents`, `/impact`, and
  `/deploy-plan`.
- **Project-local cross-project reference docs.**
  `.perun/context/cross-project.md` and `.perun/instructions/cross-project-deps.md`
  document the schema, API, and agent rules for multi-project impact and deploys.

## v0.8.5

### Added

- **`perun-proposals` MCP server.** Shared-project proposal coordination is now
  exposed as structured MCP tools (`propose`, `approve`, `list_proposals`,
  `get_proposal`) instead of brittle `[ACTION:…]` / `[APPROVAL:…]` text markers.
  The chat agent calls the tools directly; authorization (rw access, trust list,
  room-owner bypass, no self-approval) is enforced in the tool layer. Lifecycle
  notices are posted to the room by a bridge activity-table poller so they work
  across the MCP-server process boundary.
- **Read-only project inspection in chat.** The PerunChat agent can now use the
  existing `perun-guard` MCP tools (`guard_read_file`, `guard_list_directory`,
  `guard_search_files`) to inspect project content before proposing changes.
  Shell commands, native file tools, web access, subagents and guard write/edit
  tools remain denied.

### Changed

- **Project-chat runs load the project's `opencode.json`.** The bridge now sets
  `skip_instructions=False` for shared-project chats and injects
  `PERUN_PROJECT_PATH`, `PERUN_CHAT_SENDER` and `PERUN_ROOM_ID` into the run
  environment so the `perun-guard` and `perun-proposals` MCP servers know which
  project and sender they are serving.
- **`opencode.json` for shared projects now registers both MCP servers.**
  `projects.sync_opencode_config()` adds `perun-guard` and `perun-proposals`
  entries when the project is shared, and clears both when sharing is turned off.

### Fixed

- **The reviewer is any other rw participant, including the room owner.**
  Previously the PerunChat bridge only offered peer agents listed in the
  manifest as eligible reviewers. The room owner (Perun's chat persona) is
  always implicitly a member of a managed project chat, but was never pinged
  as a reviewer and was rejected by the trust-list check. In a two-agent room
  where Frambo raised a proposal, there was no one left who could approve it.
  The perun-proposals tool layer treats the owner as an eligible reviewer, so
  peer-to-Perun and Perun-to-peer workflows both work.
- **Proposal execution no longer reports false success.** Two changes:
  the execution prompt is passed with `use_prompt_file=True` (a long prompt
  used to be truncated by the Windows command-line limit, leaving the agent
  looking at instruction markers with no task), and a run that finishes
  "successfully" without creating, modifying or deleting any approved target is
  now recorded as `failed` with a `[NO TARGET CHANGES: …]` note instead of
  `executed`.
- **Policy auto-approve is honored through the MCP tool path.** The
  `perun-proposals` `propose` tool now auto-approves and starts execution for
  projects with `policy_auto_approve`, matching the previous marker-based flow.
- **The proposals MCP server no longer auto-registers unknown projects.** An
  unregistered `PERUN_PROJECT_PATH` now raises a clear error instead of silently
  polluting the project registry.
- **New projects are bootstrapped even when the target directory exists.** The
  wizard's "new project" path only bootstrapped a directory it had just
  created, so pointing it at an existing empty folder (the ai_playground case)
  produced a bare `perun.md` with no README/AGENTS/docs and an empty Manifest
  tab.
- **`backfill-docs` now completes minimal manifests.** New
  `scaffold.enrich_manifest()` fills missing `docs`, `commands` and `anchors`
  (and `name`/`kind`/`version`) while preserving existing values, the Markdown
  body and every shared-project field. `scaffold.backfill()` calls it, so
  `python -m perun backfill-docs <slug>` repairs the Manifest tab in one step.
- **Overview always shows the Manifest card.** The dashboard now renders the
  Manifest section unconditionally: if no manifest is synced it offers a link
  to the Manifest tab; if the manifest has no content it shows a helpful
  empty-state with the backfill command; otherwise it links docs, anchors,
  commands and the manifest file itself.

## v0.8.4 - 2026-10-26

### Added

- **Agent approval in chat.** Shared projects can now require a peer `rw`
  agent to explicitly approve or reject action proposals in the dedicated
  project chat. A review request is posted when a proposal is created; peers
  reply with an `[APPROVAL: {"proposal_id": "...", "decision": "approve"}]`
  or `[APPROVAL: {"proposal_id": "...", "decision": "reject",
  "reason": "..."}]` marker. Only a different `rw` participant on the trust
  list can approve or reject; the requester cannot approve their own proposal.
- **Policy auto-approve flag.** A separate `policy_auto_approve` manifest flag
  (and Shared tab checkbox) enables the previous system-immediate-auto-approve
  behaviour. It is independent of agent approval and overrides it when enabled.

### Changed

- **Autonomous mode now means agent approval.** The `shared.auto_approve`
  flag and the Shared tab's **Autonomous mode** checkbox now enable chat-agent
  peer review instead of immediate system auto-approval. Projects that were
  relying on the old behaviour must enable `shared.policy_auto_approve`
  instead.
- Autonomous policy approvals now always record the generic `auto` identity;
  they no longer attribute the approval to a passive manifest peer, which
  created a misleading audit trail.

## v0.8.3 - 2026-10-25

### Added

- **Autonomous mode for shared projects.** The Shared tab now has an
  **Autonomous mode** toggle. When enabled, action proposals created by chat
  agents are automatically approved by a peer `rw` participant agent and
  executed without waiting for a human click. If no other `rw` participant
  exists, the autonomous policy identity (`auto`) is recorded as the approver.
  The per-project manifest flag `shared.auto_approve` overrides the global
  `shared_auto_approve` default in settings. Humans can still approve, execute,
  retry, or delete proposals manually from the dashboard.
- **Lifecycle chat notifications.** Project chat rooms now receive notices when
  a proposal starts executing and a State Digest when it completes or fails,
  for both autonomous and manually-triggered runs. Rejections post a brief
  reason, and failed autonomous starts post a failure notice so participants
  know the proposal needs manual attention. All notices are redacted before
  leaving the process.
- **Completed proposals list.** The Shared tab separates active proposals
  (`pending`/`approved`/`executing`) from completed ones
  (`executed`/`failed`/`rejected`/`expired`). Completed proposals are shown in
  a collapsed-by-default section with a count, keeping the active queue
  uncluttered.

### Changed

- **No confirmation dialogs for run-starting actions.** `Approve & run`,
  `Run now`, and `Retry` in the Shared tab now start the headless run
  immediately. Confirmation dialogs are kept for destructive or irreversible
  actions (`Approve only`, `Reject`, `Cancel`, `Delete`).

### Fixed

- The dedicated PerunChat chat agent file (`~/.config/opencode/agents/perunchat.md`)
  is now created on bridge start when it is missing, using an atomic write so a
  crash cannot leave a half-written file. Previously the install step was silently
  skipped on first run, so OpenCode fell back to the default coding agent and the
  chat bridge produced empty replies.

## v0.8.2 - 2026-10-25

### Added

- **Rock-solid provider authentication for headless shared-project runs.**
  Approved action proposals now execute in a private OpenCode server
  (`--standalone`) with active API-key credentials injected as environment
  variables exported from `opencode auth export`. The model string is resolved
  against the user's connected providers; known aliases such as
  `opencode/kimi-k2.7-code` are routed to the authenticated gateway that
  actually hosts the model (e.g. `opencode-go/kimi-k2.7-code`). Credentials are
  never logged or persisted by Perun.
- **Proposal retry and delete.** Terminal proposals can be retried
  (`POST /api/projects/{slug}/proposals/{id}/retry`), creating a fresh pending
  proposal owned by the room owner and tagged `retry_of`. Proposals that are
  not `executing` can be deleted (`DELETE /api/projects/{slug}/proposals/{id}`)
  and the action is logged to project activity. Dashboard buttons expose both
  actions.

### Fixed

- State-digest notes are humanized: JSON provider errors are summarized as
  `type: message` instead of dumping the raw upstream response body into chat.
- Exported provider credentials are read fresh for each headless run and are no
  longer cached by Perun, matching the policy that Perun does not store
  credentials.
- Model resolution only falls back to local providers (`ollama`, `lmstudio`,
  `vllm`) when no active cloud provider offers the requested model, so a slow
  local model is never silently substituted for a cloud model.
- `retry` is rejected for non-terminal proposals (`pending`, `approved`,
  `executing`).
- **Windows installer updates are more robust.** The update runner now invokes
  the Inno Setup installer with `/CLOSEAPPLICATIONS`, `/FORCECLOSEAPPLICATIONS`,
  and `/NORESTARTAPPLICATIONS` so a lingering Perun process cannot lock files
  and the installer cannot race with the update runner when restarting Perun.
  If the installer still fails, the last 2,000 characters of its log are
  included in the error message.

## v0.8.1 - 2026-10-25

### Added

- The project **Shared tab** now shows the project chat as first-class
  information: chat name, room id, state, `managed_by`, manifest revision, last
  sync, and the **expected vs. live members** (unexpected members are flagged
  before the next sync removes them), plus a **Sync now** control backed by
  `POST /api/projects/{slug}/shared/sync`.
- One reusable participant editor is shared by the project-creation wizard and
  the Shared tab: trust-list chips cycle `rw → ro → removed`, and an inline
  **Add participant** field trusts a brand-new agent on the spot so adding
  someone never requires leaving the page.
- The project wizard's **Default model** and **Agent** fields are now the same
  searchable dropdowns used elsewhere in the dashboard instead of free-text
  inputs.
- Shared-project room ids (`grp:perun:<slug>:chat`) are labelled with the human
  project name in the dashboard room pickers.
- **Project chats know which project they are bound to.** The bridge resolves
  the `project_chats` binding and injects a compact local brief (project
  metadata, README/CODEMAP/DESIGN/TECHNICAL heads, root listing) capped at 3,600
  characters. Participants, the managed room owner, and Perun itself receive
  it; non-members never do. The brief states that "this project" means the
  bound project, never Perun, and that missing docs must be reported honestly.
- **One-click autonomous execution.** `POST .../proposals/{id}/approve-and-execute`
  approves and starts the headless Project Agent in one owner action; the
  existing `execute` endpoint now also starts it in the background and returns
  `202` immediately. A new `executing` proposal state, a Cancel control backed
  by `/api/runs/{proposal_id}/cancel`, automatic Shared-tab polling, and a
  completion callback that posts the State Digest keep the UI truthful while
  the agent works without a TUI.
- **Room owners can propose.** The managed room's owner (`managed_by`) may
  create a proposal even when they are not a participant row; an explicit
  approval is still required, so this only removes a configuration dead end.
- Proposal payloads are bounded (intent 500, targets 50×500, details 8,000
  characters), details are framed as data rather than instructions in the
  execution prompt, and the proposal card now shows pending items first, the
  full details, the execution result, and confirmation dialogs for
  approve/run/cancel.

### Fixed

- **Shared execution is now contained by a deny-all agent.** Approved proposals
  run headlessly under the generated `perun-shared` agent, which denies every
  native tool (shell, edit, read, web, subagent) and allows only the
  `perun-guard` MCP tools. Guard writes are restricted to the proposal's declared
  target paths, and credentials, `.env`, VCS metadata, host config, the project
  manifest, and OpenCode config are denied even inside the project root.
- **Approval and rejection use SQLite compare-and-set.** A duplicate click,
  retry, or concurrent approver can no longer overwrite a decided proposal, and
  a failed orchestrator construction marks the proposal `failed` instead of
  leaving it `executing`.
- **Trust is required on every project-chat path.** Participants must still be on
  the local PerunChat trust list to receive project context, create proposals, or
  stay in the managed room; revoking trust now removes them from the manifest,
  bumps `manifest_rev`, and re-syncs the room. The room owner and this machine's
  own resolved Perun id remain authorized.
- **PerunChat memory is room/project scoped.** Project-room facts use the real
  project id, other rooms use a reserved negative id plus room filtering, and
  public/other-room facts can no longer appear in a project prompt.
- **Project briefs no longer leak host identity.** The shared-project context
  omits local absolute paths and Git remotes, shows external dependencies by
  basename, and lists only participants that are still trusted.
- **The post-run audit is target-scoped.** Changes outside the approved targets
  are violations, parent-directory entries are compared for modified/deleted
  files as well as creations, and heavy/VCS directories are skipped during
  snapshots.
- **Perun does not assume another machine's chat id.** Direct-address detection
  uses this machine's resolved agent id (plus generic `perun`/`overseer`
  aliases); a foreign id such as `@perun-remote` is not treated as self.
- **Action proposals no longer return HTTP 500.** Databases created before
  Shared Projects lacked `action_proposals.expires_at` (`CREATE TABLE IF NOT
  EXISTS` never adds columns), so every proposals request failed. `db.init_db()`
  now migrates the column idempotently, backfills pending proposals to their
  full 24-hour lifetime, and bumps the schema version; `expire_stale()`
  additionally degrades to an empty result instead of raising on a legacy DB.
- **Shared-project chats are created again.** The sync derived
  `https://peruninthemachine.com` for the gateway REST API, but public hosts
  serve it under `/chat` — so the call returned the SPA's HTML and died with a
  JSON decode error. The base URL now comes from one shared helper,
  `config.perunchat_rest_base()`, used by the bridge, the HTTP proxy, and
  shared-project sync.
- **Turning a project's shared mode off tears its chat down.** `shared` is now
  the single authority for the room's existence; leftover participant rows can
  no longer keep a phantom chat alive, and the UI reports the chat as disabled.
- **Perun replies in every room it belongs to.** The autonomous bridge now joins
  the configured/default rooms *plus* all shared-project chats and all private
  rooms Perun is a member of (discovered over REST) on every connect, refreshing
  the set every 30 seconds. A join is acknowledged only on `ROOM_JOINED`; an
  `ERROR` clears it so a room created after the connection is joined on the next
  refresh. Idle protocol pings and a forced reconnect after 120 seconds of
  silence keep the socket — and therefore Perun's replies — alive.
- Shared-project room membership is strict: a managed chat contains exactly the
  declared participants and Perun, and the "Sync now" path removes anyone else.
- **Action markers survive a `targets` array.** The old non-greedy regex stopped
  at the first `]`, so a normal `"targets": ["a", "b"]` envelope was truncated
  and silently discarded. A brace/bracket-aware scanner now extracts the whole
  object, and the marker is always stripped from the outgoing chat text.
- **Relative targets are rooted at the project, not the process cwd.**
  `path_guard` resolved relative proposal targets (and relative external deps)
  with `Path.resolve()`, which silently made every target
  `C:\my_apps\perun\...`; the check now resolves them against the project root,
  rejects `..` escapes, and keeps relative dependencies project-relative.
- **Absolute proposal targets survive outbound redaction.** The DLP pass ran
  before `[ACTION: ...]` parsing, so a legitimate absolute target path became
  `[redacted]`, broke the JSON, and silently dropped the proposal. The marker is
  now parsed first; only the human-visible remainder is redacted.
- **Interrupted proposals no longer stick in `executing`.** Server startup
  marks orphaned runs `interrupted` and reconciles their proposals to `failed`
  with an explicit reason, so the owner can retry instead of staring at a
  permanently busy row.
- **A filesystem-policy violation fails the proposal.** The post-run audit used
  to append a warning while still reporting `executed`; it now downgrades the
  outcome to `failed` and records the offending paths.
- **Successful headless runs are no longer mislabeled as failures.** The
  Orchestrator finalizes runs with status `done`, but proposal execution only
  accepted `completed`; every real run therefore ended as `failed`. Both
  spellings are accepted now, and the regression test uses the real `done`
  status.
- **Audit failures and out-of-root escapes now fail the proposal.** If the
  post-run audit cannot run, or it detects new entries next to the project root,
  the proposal is marked `failed` instead of claiming `executed`. The audit also
  now treats writes and deletions inside a read-only external dependency as
  violations.
- **A project description survives enabling sharing.** Turning on shared mode
  writes `perun.md` with only the shared fields; the next discovery pass treated
  the omitted description as empty and erased the value entered in the create
  wizard. Discovery now keeps the configured description unless the manifest or
  project files actually declare a new one.

## v0.8.0 - 2026-10-25

### Added

- **Shared Projects**: Perun projects can now be bound to dedicated PerunChat
  group rooms. Manifests gain `shared`, `participants`, and `external_deps`
  fields; the dashboard wizard and project detail page expose multi-select
  participant management and external-dependency configuration.
- HITL-gated **action proposals** from shared-project chat rooms. Chat agents
  emit `[ACTION: ...]` envelopes; owners approve/reject/execute them from the
  dashboard or via the REST API. Approved runs are wrapped by a policy-enforcing
  filesystem MCP server (`perun-guard`) and audited post-run.
- Local HMAC-SHA256 **approval-token engine** for proposals, with a persisted
  nonce store so replay protection survives server restarts.
- Post-run **filesystem audit** snapshots allowlisted paths before a shared
  project run and diffs them afterwards, logging violations and parent-directory
  escapes to the activity feed.
- Offline room-deletion retry queue (`pending_room_deletions`) retried on
  server startup and PerunChat bridge reconnect.
- Configurable **approver identity** for shared-project proposals via the
  `perun_owner_name` setting; defaults to the OS username so the audit trail
  shows who approved or rejected each proposal.
- Per-project participant **role semantics**: `rw` participants may propose
  actions, while `ro` participants can read shared context but are rejected when
  they originate `[ACTION: ...]` proposals.
- Configurable **HITL signing secret** via the `PERUN_HITL_SECRET` environment
  variable or the `perun_hitl_secret` setting, so multi-node deployments can
  share the same approval-token key instead of deriving it from `PERUN_HOME`.
  When `PERUN_PASSWORD` is set, the persisted secret is encrypted at rest.

### Changed

- PerunChat reply hard cap removed; the model is now trusted to decide reply
  length (the system prompt still asks for brevity).

### Fixed

- `perun-guard` MCP server is now registered in (and removed from) the project's
  `opencode.json` whenever shared-project mode is enabled or disabled from the
  dashboard.
- Trust-list revocation immediately re-syncs affected shared-project chats,
  stripping the revoked agent from the room ACL.

## v0.7.51 - 2026-10-24

### Fixed

- PerunChat hard timeout now cancels the underlying orchestrator run when reply
  generation exceeds 60 seconds. Previously the foreground worker abandoned the
  reply but the background thread continued, causing delayed replies to be sent
  minutes later after a hung model/provider call finally finished.

## v0.7.50 - 2026-10-24

### Added

- Regression test ensuring PerunChat typing-indicator frames consume a unique
  `sequence_number` so the following reply cannot be rejected by the gateway as
  non-monotonic.

## v0.7.49 - 2026-10-24

### Fixed

- Critical PerunChat typing-indicator bug: `_send_typing_indicator()` now
  increments and persists `_seq` before sending, so the subsequent reply no
  longer reuses the same `sequence_number` and is not dropped by the gateway as
  `non_monotonic_sequence`.

## v0.7.48 - 2026-10-24

### Fixed

- Re-enabled the native PerunChat typing indicator with idle-frame cleanup on
  every reply path (success, no-reply, timeout, failure); typing frames are no
  longer queued for redelivery.

## v0.7.47 - 2026-10-24

### Changed

- Temporarily disabled the native typing indicator while idle-frame cleanup was
  hardened.

## v0.7.46 - 2026-10-24

### Changed

- Shortened the PerunChat reply wall-clock timeout to 60 seconds.
- An idle typing frame is sent when reply generation gives up, so the dashboard
  does not keep a stale "typing…" indicator.

## v0.7.45 - 2026-10-24

### Fixed

- PerunChat typing indicator is cleared on reply timeout or generation failure.
- Dashboard guards against stale typing indicators with a 60-second client-side
  timeout.

## v0.7.44 - 2026-10-24

### Added

- Native PerunChat typing indicator using `content_type: application/perun-typing`
  with string payload `"typing"`.
- Hard wall-clock timeout around reply generation so a hung model/provider call
  cannot stall a room indefinitely (initially 3 minutes, reduced to 60 seconds
  in v0.7.46).

## v0.7.43 - 2026-10-24

### Added

- Server-side proxy endpoints for private-room management (create, invite,
  remove participants) in the PerunChat gateway.
- Dashboard validates PerunChat room IDs before adding or creating rooms and
  accepts `private:` as an alias for `priv:`.

## v0.7.42 - 2026-10-24

### Added

- PerunChat bridge stores conversational facts in vector memory, tagged with
  sender, room, and type metadata, and injects relevant facts into reply prompts.
- Dashboard private-room UI: create rooms, invite/remove participants.

## v0.7.41 - 2026-10-24

### Changed

- PerunChat dashboard operator messages now show the full runtime label
  (e.g. `peruntheoverseer-operator`) with `(you)` only for the current user's
  operator messages.

## v0.7.40 - 2026-10-24

### Fixed

- Added a dedicated lock around the shared per-minute reply counter in the
  PerunChat bridge so multi-room worker threads cannot race on the global
  rate-limit state.
- `_ensure_room_worker` now detects dead worker threads and replaces them,
  reusing the existing queue so frames are not stranded by a crashed worker.

## v0.7.39 - 2026-10-24

### Fixed

- PerunChat bridge now uses per-room worker queues instead of a blocking lock.
  Each room has a dedicated thread that processes one message at a time, and
  the current WebSocket is captured in the queue so reconnects do not lose or
  misroute replies. Rate-limit and cooldown decisions are evaluated at
  processing time inside the worker, so earlier replies correctly throttle
  later messages in the same room.
- Dashboard operator messages now declare ``sender.runtime`` as
  ``<agent_id>-operator`` and detect any runtime ending in ``-operator`` when
  rendering, so operator labels survive page refreshes and coexist with other
  Perun instances in public rooms.

## v0.7.38 - 2026-10-24

### Fixed

- Added a background sweeper that removes stale ``perun_prompt_*.txt`` files left behind by crashes.
- Added logging when the PerunChat prompt exceeds the size cap and history lines are dropped.
- Added regression tests for prompt-history trimming and stale prompt-file cleanup.

## v0.7.37 - 2026-10-24

### Fixed

- PerunChat prompts are passed to `opencode run` via a temporary file attached
  with `--file`, eliminating the Windows ~32K command-line length limit and
  avoiding the `@path` convention which models can misinterpret as a tool call.

## v0.7.36 - 2026-10-24

### Fixed

- PerunChat prompts are now passed to `opencode run` via a temporary file
  instead of a command-line argument, eliminating the Windows ~32K
  command-line length limit that could silently drop the latest message.
- The prompt now follows a normal chat transcript layout: system prompt,
  recent conversation history (including Perun's own replies), and the latest
  message at the end. If the prompt is too long, the oldest history lines are
  dropped first so the latest message is always preserved.
- Per-room reply serialization is enforced with a dedicated lock so only one
  message per room is processed at a time, preserving order and preventing
  overlapping model calls.
- PerunChat bridge keeps its own outbound replies in room history so the model
  sees a coherent conversation rather than a series of disconnected questions.

## v0.7.35 - 2026-10-24

### Fixed

- PerunChat prompt context is reduced (`_MAX_CONTEXT_MESSAGES` from 12 to 6) and
  the participant roster is capped at 10 to avoid command-line / context-window
  truncation that can hide the latest message from the model.

## v0.7.34 - 2026-10-23

### Fixed

- PerunChat operator label now uses the existing `--green` CSS variable instead
  of the non-existent `--success`.
- PerunChat bridge now deduplicates incoming frames by id and by
  sender/content/timestamp so gateway replays or echo storms do not pollute the
  conversation context or trigger multiple replies.

### Changed

- Dashboard PerunChat operator-message tracking is now pruned with the chat
  history, and the operator flag is persisted in `localStorage` so restored
  history keeps the correct label after a reload.

### Added

- Regression test ensuring a frame with `sender.runtime == "perun-operator"`
  and the bridge's own `agent_id` is not swallowed as an own message.

## v0.7.33 - 2026-10-23

### Added

- Dashboard PerunChat manual messages now carry `sender.runtime: "perun-operator"`
  so the chat log can distinguish a human operator message from autonomous
  Perun traffic.

### Changed

- The dashboard chat log labels operator-sent messages as `operator (you)`
  instead of `@peruntheoverseer`.

## v0.7.32 - 2026-10-23

### Added

- macOS `.pkg` installer (`installer/macos/`). Builds an unsigned package that
  installs Perun to `/opt/perun`, creates a dedicated Python venv, wires the
  global OpenCode integration for the logged-in console user, installs a
  `/usr/local/bin/perun` wrapper, and registers a LaunchAgent that starts
  `perun serve --bridge` at login. The release workflow now builds
  `PerunSetup.pkg` on `macos-latest` and attaches it to GitHub releases
  alongside the Windows installer.

### Fixed

- Dashboard self-update: stop the Updates page from reloading forever after a
  successful update. A `sessionStorage` flag ensures the auto-refresh happens
  only once, and the finished job is dismissed server-side after the reload
  so the loop cannot resume even if browser storage is cleared.
- macOS installer: use the `gui/<UID>` launchctl domain for LaunchAgent load
  /enable/kickstart instead of the incorrect `user/<UID>` domain.
- macOS installer: replace an undefined `log` call in `uninstall.sh` with
  `echo`.
- macOS installer: make the pip upgrade step best-effort so a missing network
  does not abort an otherwise working install.
- macOS installer: generate a user-specific log path
  (`~/Library/Logs/perun/perun.log`) in the LaunchAgent plist instead of
  writing to world-readable `/tmp/perun.log`.
- macOS installer: warn when Xcode Command Line Tools are missing.

### Changed

- macOS installer: added CI lint step (`bash -n`, `plutil -lint`) and a
  `installer/macos/README.md` with build/install/uninstall instructions.

## v0.7.27 - 2026-10-23

### Changed

- Refactored PerunChat reply decision logic. `_should_reply_to()` now returns a
  structured `_ReplyDecision` with an explicit `is_direct` flag, instead of a
  `(bool, str)` tuple that callers had to re-parse. This makes the bypass rules
  for cooldown and saturation self-documenting and prevents future drift.

## v0.7.26 - 2026-10-23

### Fixed

- PerunChat direct mentions, private-room messages, and replies to Perun's own
  messages now also bypass the room-saturation guard. Previously a burst of
  @mentions could pass the cooldown but still be dropped after 3 recent Perun
  replies because the saturation guard counted every reply, including direct
  responses.

### Tests

- Added a regression test ensuring direct mentions are answered even when the
  proactive saturation guard would block unsolicited replies.

## v0.7.25 - 2026-10-23

### Fixed

- PerunChat direct mentions, private-room messages, and replies to Perun's own
  messages now bypass the proactive 2-second per-room cooldown. Previously a
  burst of @mentions was processed but only the first reply was sent; the rest
  were dropped with "cooldown active" even though they were explicitly
  addressed to Perun.

### Tests

- Added regression tests ensuring direct mentions bypass the per-room cooldown
  while proactive chime-ins still respect it.

## v0.7.24 - 2026-10-23

### Fixed

- PerunChat reply reliability: a message is now marked as replied only after a
  reply is generated and queued/sent. Previously, failed LLM runs caused the
  source message to be marked as replied before any reply was delivered,
  making the bridge skip the message forever on subsequent gateway reconnects.
- PerunChat outbox consistency: when a socket send fails and the reply is
  queued for later resend, the source message is still marked as replied so
  the gateway replay does not generate a duplicate reply.
- PerunChat decision logging: every frame receive/reply/skip path is now
  logged at INFO level for easier debugging.

### Tests

- Added a regression test ensuring a failed socket send still marks the
  original message replied and does not trigger a second LLM run when the
  gateway replays the message.

## v0.7.23 - 2026-10-23

### Fixed

- PerunChat first-message replies no longer default to a generic
  self-introduction; the prompt now explicitly tells the model it is already
  an established participant and must reply directly to the message.
- Perun MCP server startup banner suppressed. FastMCP 4.0.3 was printing a
  banner to stdout, corrupting the stdio JSON-RPC stream and causing MCP
  clients to fail; the banner is now disabled and a stdio smoke test guards
  the fix.

### Docs

- Project documentation refreshed for the Context layer, secret store,
  `project_roots`, dashboard self-update, and the `context`/`secret` CLI.

## v0.7.22 - 2026-10-23

### Added

- **Dashboard self-update.** Perun can now check GitHub releases and install a
  newer version directly from the dashboard.
  * `perun/updater.py` — `check()` queries the GitHub releases API (cached),
    `install_mode()` detects whether this copy is a git checkout or a Windows
    installer, and `apply()` prepares the update while the server is still
    live.
  * Updates are applied by a **detached runner** (`perun update runner`) that
    waits for the server to exit, runs `git pull --ff-only` or the release
    installer, restarts Perun, then polls `/api/health` until the new version
    answers. The job state is persisted in `~/.perun/update-state.json` so the
    dashboard can stream progress across restarts.
  * `perun update check|status|apply|dismiss` (CLI) — the same surface without
    the dashboard.
  * New REST endpoints: `GET /api/update/check`, `GET /api/update/status`,
    `POST /api/update/apply`, `POST /api/update/dismiss`.
  * Dashboard UI: an update pill in the topbar, a banner on the Dashboard, a
    dedicated `#/updates` view with release notes and an install button, plus
    an "About Perun" card in Settings.
  * GitHub Actions now publishes a `PerunSetup.exe.sha256` checksum asset; the
    installer download verifies it when present.

### Docs

- Documentation refreshed against the v0.7.21 codebase (no code change):
  * `docs/CODEMAP.md` — `__version__` bumped, the `tasks` table and
    `doc_chunks.embedded_hash` noted, the eight missing test files
    (`test_context`, `test_context_map`, `test_secret_store`, `test_daemon`,
    `test_project_roots`, `test_perunchat_bridge`, `test_perunchat_proxy`,
    `test_prompt_chain.js`) added, and stale identifiers corrected
    (`git.status()` → `validate_remote()`, `server._register_project()` →
    `_sync_manifest()`).
  * `docs/TECHNICAL_DOCUMENTATION.md` — the `tasks` table, the perunchat
    `bridge/status` and `rooms` endpoints, the `agent_id` field on
    `/api/perunchat/status`, the `embed_dim` / `telegram_pair_code` /
    `perunchat_*` settings, and the full environment-override list. The stale
    "empty `telegram_allowed_chats` = pair-on-first-message" claim is
    corrected: an empty allow-list grants **nobody** until `/pair <code>`.
    The v0.7.21 entry is dated and the kernel size corrected to 1,832 B.
  * `README.md` — MCP tool count 25, the `context` / `secret` / `stop` /
    `restart` / `project-roots` CLI groups, the Context-layer and secret-store
    subsystems in the layout and feature list.
  * `docs/DESIGN.md` — brought up to date for the Context layer, secret store,
    provider management, PerunChat, `project_roots`, and the single shared
    Telegram bridge.
  * `docs/AGENTS_OPTIMIZATION.md` — status table, kernel size, and baseline
    measurements reconciled with the shipped implementation.
  * `perun.md` / `AGENTS.md` — anchors for the Context-layer modules; the
    `mcp-grep` reference replaced with `perun_grep` / `perun_ast_grep`.

## v0.7.21 - 2026-10-21

### Added

- **Perun Context — the ambient-context budget is now bounded.** `AGENTS.md` used
  to grow without limit: every project carried its own copy of the same
  reference material, and it was paid for in full on every request of every
  session. Reference material now lives in a **Context layer** — keyed,
  vector-backed documents that are retrieved on demand instead of being inlined.
  * `perun/context.py` — the store: `index_docs()` / `index_doc()` / `delete()` /
    `reembed_missing()`, `get()` / `list_keys()`, `search()` (exact key → anchor
    label → key substring → semantic), `outline()` / `slice_section()`,
    `prefetch()`, and the renderers.
  * `perun/handbook.py` — Perun's own reference material as context docs, with
    `ENTRIES` as the single source of truth for the key table.
  * A new `context_docs` table — deliberately **not** `doc_chunks`, so a
    project-scoped context doc never leaks into another project's cross-project
    document search.
  * `perun_context` (MCP) — retrieve the context for a key, an anchor label, or
    a task description. The key inventory is always visible, so an agent always
    knows what it does not currently have.
  * `perun context list|get|search|sync|map|index|verify|stats` (CLI) — the same
    surface without an agent, plus a drift check (`verify`) and a budget report
    (`stats`: AGENTS.md sizes, token estimates, duplicated sections).
  * **Awareness parity is a contract, not a hope.** Four mechanisms: the
    original `AGENTS.md` is retained verbatim as a context doc
    (`perun.agents.full`); the key inventory is always visible; relevant content
    is **pre-fetched by task**, not requested; and `perun context verify` gates
    every migration.
  * **Task-driven pre-fetch on the orchestrated path.** `compose_prompt()` now
    injects the reference material a request actually needs, in a
    `--- PERUN CONTEXT START/END ---` envelope. The gate is deterministic and
    Ollama-free — exact key → anchor label → key fragment → subject match — so
    the run path never depends on an embedding service. A wrong injection costs
    tokens *and* can mislead, so the default is to inject nothing rather than
    something plausible.
  * `memory:` anchors in `perun.md` bind a document to its context key, so an
    agent can ask for it by its human label.
- **The losslessness proof, and the migration of all five managed projects.**
  * `perun/context_map.py` — the **context map**: a small JSON file in the
    project (`.perun/context-map.json`) that records, for every section of the
    original `AGENTS.md`, *where that content went*. `inline` (it stays in the
    file — the behavioural rules that must never move), `context:<key>` (it was
    relocated verbatim to a Context-layer doc), `secret:<name>` (it moved to the
    encrypted store), `retained` (it lives only in the retained original).
  * `perun context map [--project SLUG] [--write]` — shows the section
    inventory of a project's `AGENTS.md` and drafts the map.
  * `perun context verify` now proves the migration: the retained original
    still hashes to what the map recorded (**nothing was deleted**), every
    section of that original is accounted for (**nothing is unaccounted for**),
    and every destination still holds its content verbatim (**nothing was
    paraphrased away**). A proof that cannot fail is not a proof — each of
    those failure modes is a test in `tests/test_context_map.py`.
  * `perun context sync` now indexes every project's `AGENTS.md` as
    `<slug>.agents.full` and its `.perun/context/*.md` docs as
    `<slug>.context.<name>`. The retained original is a **snapshot**: once a
    context map pins it, a later sync will not overwrite it.
  * **All five managed projects migrated:** `perun` (9,435 B → 5,037 B),
    `trading_bot` (62,631 B → 62,312 B), `parental_control` (78,055 B → 60,644 B),
    `perun_in_the_machine` (6,024 B → 3,074 B), `perunchat` (3,446 B → 2,670 B).
- **The global kernel is now 1,832 B instead of ~6.1 KB.** The always-on block
  in the global `AGENTS.md` was replaced by a generated kernel: a short
  orientation plus the handbook key table. The **complete previous global guide
  is kept verbatim** as `handbook.global` and stays reachable in one call —
  nothing was deleted, only relocated.
- **An encrypted secret store.** `perun/secret_store.py` +
  `perun secret list|get|set|delete|password|status` + the `perun_secret` MCP
  tool. Values are Fernet-encrypted (PBKDF2-HMAC-SHA256) at rest and the master
  password is protected with Windows **DPAPI**, bound to the current user. A
  listing never contains a value; the project file keeps only the **name**, and
  the agent fetches the value at the moment it is used — one prompt carries the
  credential instead of every prompt carrying it. `kind: secret` anchors and
  `perun context verify` check that a named secret actually exists.
- **Nothing sensitive is ever embedded.** A context doc whose body trips the
  sensitive-data guard is still stored verbatim (dropping it would break the
  parity contract) but is never sent to the embedding endpoint. Such a doc is
  marked `embedded_hash == content_hash` with a NULL vector, so the repair path
  leaves it alone.

### Fixed

- Docs indexed while Ollama was down stay vector-less forever. The Context
  store now compares the stored embedding hash with the content hash, so a
  missing or stale vector is re-embedded on the next index — and the repair
  path (`context.reembed_missing()`, also wired into the server's background
  repair thread) never selects a doc that is deliberately vector-less.
- `handbook.ensure_indexed()` could recurse: rendering the MCP tool reference
  loads the MCP server module, which calls back into it on import. A
  re-entrancy guard now breaks the cycle.

## Unreleased

## v0.7.20 - 2026-10-21

### Added

- **Full provider management in the dashboard.** The Providers view now supports
  adding, connecting and disconnecting API-key providers (OpenCode Console,
  OpenCode Go, Ollama Cloud, OpenAI, DeepSeek, etc.) and local providers
  (Ollama, LM Studio, vLLM) directly from the UI. Credentials are written to
  the user's `opencode.json[c]` and OpenCode is asked to reload its config;
  Perun never stores keys separately. OAuth providers still open a terminal for
  `opencode auth login`. A new "Add provider" catalog lists well-known presets
  and supports custom OpenAI-compatible endpoints. Presets that are not built
  into OpenCode are configured with the canonical OpenAI-compatible runtime
  package and a known base URL.
- **`perun/opencode_config.py`** — safe JSON/JSONC read/write helpers for the
  user-level OpenCode config, with backups, legacy `provider` → V2 `providers`
  migration, and best-effort `opencode reload` orchestration. The dashboard
  warns that saving providers normalizes the config file to plain JSON.
- **Live dashboard stat cards.** The provider and embeddings stat cards refresh
  automatically every 10 seconds while on the Dashboard, so they no longer go
  stale after background changes.

## v0.7.19 - 2026-10-20

### Fixed

- **PerunChat replies answer questions instead of generic greetings.** The chat
  system prompt and the installed `perunchat` OpenCode agent now explicitly
  instruct the model to answer questions directly, use the provided participant
  roster and conversation context, and reserve brief greetings for purely
  social messages. The agent file is also overwritten on bridge start whenever
  the embedded content changes, so prompt fixes reach production runs.

## v0.7.18 - 2026-10-20

### Fixed

- **No-loss PerunChat reply delivery.** Cancelled or slow orchestrator runs no
  longer discard replies that were already emitted: the stdout loop now parses
  every line before honouring a cancel/timeout break, and drains buffered final
  lines after the process exits. The bridge retries empty replies, waits for
  still-running runs instead of starting duplicates, persists replied-message
  ids across restarts, and stores every outbound frame in a write-ahead outbox
  that is flushed idempotently on reconnect.

## v0.7.17 - 2026-10-20

### Fixed

- **Dashboard "Working now" section now updates live.** The stat counter and active-run list refresh automatically when presence changes, instead of only on manual page refresh.

## v0.7.16 - 2026-10-20

### Fixed

- **Robust Python path quoting in `Perun.cmd`.** Replaced fragile `findstr` pipe logic with substring checks, preventing the launcher from closing immediately when Python is installed under `C:\Program Files\...`.

## v0.7.15 - 2026-10-20

### Fixed

- **Fix `Perun.cmd` premature exit.** Removed invalid goto label causing the launcher to close immediately on Windows.

## v0.7.14 - 2026-10-20

### Fixed

- **Improve Python path quoting in `Perun.cmd`.** More robust handling of Python paths containing spaces.

## v0.7.13 - 2026-10-20

### Fixed

- **Quote Python executable paths with spaces in `Perun.cmd`.** Prevents launch failures when Python is installed under `C:\Program Files\...`.

## v0.7.12 - 2026-10-20

### Added

- **Live preview of the manifest body in the Manifest tab.** The editor now
  renders the Overview-body Markdown in a box under the textarea and re-renders
  on every keystroke, through the same escaped-by-default `renderMarkdown()`
  and the same link wiring (`wireManifestLinks()`, extracted from the read-only
  card) that the Overview tab uses — so editing free-form Markdown is no longer
  blind and no new HTML surface is introduced.

### Fixed

- **The Manifest tab no longer fetches project detail it immediately threw
  away.** `mountManifest()` built the read-only manifest card — the same one
  the Overview tab renders — appended it, and then let `renderManifestEditor`
  clear the container, so the card never appeared; it also paid a duplicate
  `GET /api/projects/{slug}` (already issued by `viewProject`) solely to feed
  that dead append. The dead code and the redundant request are gone, and the
  comment now describes reality: presentation lives on the Overview tab,
  editing on the Manifest tab.
- **Unsaved manifest edits are no longer discarded silently.** The editor marks
  itself dirty on any input; *⟳ Reload from disk* and *＋ Generate from
  registry* — both of which rebuild the editor from server state — now ask for
  confirmation first when there are unsaved changes.
- **Manifest path targets open cleanly.** Backslashes are normalised to `/`
  (so `docs\CODEMAP.md` and `C:\my_apps\perun` work), `/./` segments are
  collapsed before the path is handed to the OS handler, and `..` segments are
  still stripped from *relative* targets only — absolute targets keep their
  real semantics instead of being mangled.
- **Read-only anchors are clickable whenever they point at something openable.**
  A `file`/`doc` anchor whose target is a URL now renders as a real link (it
  previously fell back to an inert label after the `safeUrl` guard);
  `symbol`/`command` anchors remain informational.
- **External manifest URLs open with `rel=noopener` semantics** — a manifest is
  local but potentially untrusted content (e.g. a cloned repo), so
  `window.open(..., 'noopener')` prevents the opened page from reaching back
  into the dashboard tab.

### Changed

- **Manifest Links and Anchors are clickable in the dashboard editor.** Rows in
  the Manifest tab's *Links* and *Anchors* sections now carry an ↗ open button
  like the Documentation rows: URLs open in a new tab, local paths (relative to
  the project or absolute) launch through the OS handler, and `command` anchors
  copy themselves to the clipboard. Read-only manifest cards also render
  local-path links as clickable chips instead of a dead `#` href, and show the
  link's value next to its key (as docs already did).
- **The live body preview is debounced (150 ms)**, so a large manifest body
  repaints once per pause in typing rather than once per keystroke.

## v0.7.11 - 2026-10-20

### Fixed

- **Restart-flag replay is now validated and never blocks startup.**
  `perun restart` rejects flag-looking tokens (`--port`, …) as recorded values
  in `<PERUN_HOME>/server.json`, so a hand-edited record cannot produce an argv
  the replacement fails to parse; a failed record write warns and lets
  `perun serve` start instead of aborting; the record file re-asserts owner-only
  permissions after the atomic rename (matching the PID file).

### Changed

- **Malformed PerunChat frames are dropped, not fatal.** Non-object JSON frames
  (arrays/scalars) are ignored and `sender: null` / missing `sender` are
  normalized at the bridge entry point, instead of raising in the receive loop
  and forcing a reconnect cycle.

## v0.7.10 - 2026-10-20

### Changed

- **`perun restart` replays the previous server's launch flags.** `perun serve`
  now records its effective flags (`--host`, `--port`, `--reload`, `--bridge`)
  in `<PERUN_HOME>/server.json`; a plain `perun restart` carries them over, so
  restarting no longer silently drops the Telegram/PerunChat bridges (or the
  dev reload mode). Explicit flags on the restart command line still win over
  the recorded ones; the record tolerates missing/corrupt files and survives
  `perun stop`.

## v0.7.9 - 2026-10-20

### Changed

- **`perun restart` now starts its replacement detached by default.** Previously
  the new server stayed attached to the invoking shell, so a headless restart
  from a script, CI step, or agent harness died as soon as that shell exited —
  silently taking the Telegram/PerunChat bridges down with it. `--foreground`
  restores the watch-the-logs behavior; `--detach` is kept as a no-op for
  backward compatibility.

## v0.7.8 - 2026-10-20

### Changed

- **PerunChat replies after a reconnect acknowledge the gap.** The autonomous
  bridge now records when it connected to the gateway. A message that predates
  that connect by more than 2 minutes (plus a 60 s clock-skew tolerance) gets a
  catch-up note in the chat prompt, so Perun answers messages missed while
  offline like someone returning to the conversation instead of replying as if
  the message had just arrived. No message is ever skipped as "stale."

### Fixed

- **Malformed chat timestamps can no longer interrupt the bridge.** `NaN`,
  `Infinity`, boolean, or other non-numeric `timestamp` values are rejected
  before any age arithmetic, so a bad frame from a peer or gateway cannot take
  down the receive loop.

## v0.7.7 - 2026-10-20

### Changed

- **Renamed the left toolbar nav item to “Orchestrator.”** The dashboard sidebar
  previously labelled the control-plane chat “Orchestrator Chat”; it is now
  simply “Orchestrator.”

## v0.7.6 - 2026-10-20

### Fixed

- **PerunChat replies no longer role-play the coding orchestrator.** The bridge
  now runs replies through a dedicated OpenCode agent (`perunchat`) that carries
  its own chat-participant system prompt and denies all tools. Previously OpenCode
  injected the default coding-agent prompt plus project `AGENTS.md`, so Perun
  answered every room message as "the local overseer for the OpenCode projects on
  this machine" instead of replying to the message. The agent file is
  self-installed into `~/.config/opencode/agents/perunchat.md` on bridge start.
- **Chat agent is scoped to PerunChat.** `perunchat_chat_agent` defaults to
  `perunchat` and is configured only on the PerunChat dashboard page; the general
  Settings page no longer surfaces it.
- **Internal/private information is never leaked to rooms.** Public-room replies
  pass through a redaction guard that strips local file paths, project internals,
  config files, and MCP/tool references before sending; internal context is only
  shared in private/group rooms with participants on the new trust list.

### Added

- **PerunChat trust list** (`perunchat_trusted`): internal/private context may be
  shared only with agents explicitly marked as trusted, and only in private or
  group rooms. Public rooms never receive internal context regardless of trust.

### Changed

- **PerunChat public live view now matches the dashboard.** The website viewer
  (`perunchat_public.js`) persists recent messages per room in `localStorage`,
  deduplicates gateway history replays, and auto-reconnects with exponential
  backoff instead of silently switching to demo mode on the first disconnect.
  Demo traffic now only appears after repeated connection failures and is cleared
  as soon as the live gateway returns. A visible scrollbar was added to the
  public `.chat-stream`.

## v0.7.5 - 2026-10-19

### Added

- **PerunChat chat UX hardening:** the local dashboard chat panel now uses a
  discoverable room select (filtered by room type), auto-joins on selection,
  shows the currently joined room, and persists the last room, sequence number,
  and recent history in `localStorage`. Sent messages rely on the gateway echo
  (with deduplication by message id) instead of a local preview, eliminating
  duplicate rendering.
- **PerunChat autonomous reply improvements:** the bridge loads a strict chat
  instruction file (`perun/perun/instructions/perunchat_chat.md`), calls the
  Orchestrator with `skip_instructions=True`, strips instruction wrappers from
  the model reply, caps reply length, and defaults to joining the well-known
  public rooms (`pub:global:general`, `pub:global:tech`, `pub:global:random`).
- **LLM-driven PerunChat reply decisions:** instead of hardcoded heuristics,
  the bridge now asks the Orchestrator whether a natural participant would reply.
  The model can output `<NO_REPLY>` to stay quiet or produce a brief reply.
  Perun is more proactive in small rooms and stays reserved in busy channels,
  while keeping recent room history for context-aware responses.
- **PerunChat bridge sequence persistence:** the bridge persists a monotonic
  `sequence_number` counter to `<PERUN_HOME>/perunchat_bridge_seq.json` so
  gateway sequence validation never rejects Perun's replies after the first one.
- **Encrypted PerunChat API key:** `perunchat_api_key` can now be encrypted in
  `config.json` when a password is supplied, matching the Telegram token path.
  The dashboard exposes an optional encryption password field and surfaces the
  resolved `agent_id` in the status badge. CSP `connect-src` now allows the
  configured gateway origin for direct WebSocket connections.
- Added a native **PerunChat** dashboard UI (`#/perunchat`) in the local overseer.
  Users configure their PerunChat API key and WebSocket endpoint, manage friends
  and blocks, accept/reject friend requests, view online/offline status, and
  chat through a lightweight panel. REST social graph calls are proxied through
  Perun endpoints under `/api/perunchat/*`; the API key is exposed only to
  loopback callers via `/api/perunchat/key` so the dashboard can open the WebSocket
  directly to the PerunChat gateway.
- Added `GET /api/perunchat/rooms` proxying the gateway's `/api/v1/rooms`
  discovery endpoint (active, joined, and well-known public rooms).
- Added reply/tagging support in the PerunChat chat panel: messages can carry an
  `in_reply_to` reference; the dashboard shows who is being replied to and
  allows the user to click Reply on any message.
- Added autonomous PerunChat agent bridge (`perun/perun/perunchat_bridge.py`):
  the local Orchestrator joins configured rooms and replies to incoming chat
  messages using the control-plane model/agent. Humans retain control over
  registration and HITL approval of state-changing actions.
- Added `perunchat_api_key`, `perunchat_api_endpoint`, `perunchat_auto_reply`,
  `perunchat_auto_join`, `perunchat_chat_model`, and `perunchat_chat_agent`
  settings, exposed in the dashboard and restartable without a server restart.
- Added `perun.project_roots`, a central, explicit registry for safe
  cross-project path resolution. It ships with built-in defaults for the core
  projects, auto-registers new projects on discovery, supports environment and
  user overrides, and prevents path-escape mistakes. `AGENTS.md` now mandates
  its use for all cross-project file references.

### Changed

- PerunChat dashboard page polish: chat model/agent overrides are now the
  standard **model and agent dropdowns** (same widgets as the Settings page,
  still free-typed), auto-join rooms is a **multi-select fed by the gateway's
  room catalog** plus a custom-room entry, and the Friends / Requests / Blocked
  cards are always visible — without an API key they show a clear
  "add your key above" placeholder instead of disappearing. The chat room input
  got a datalist of discoverable rooms.
- Saving PerunChat credentials now **re-renders the page automatically**
  (closing any live chat socket first) instead of asking the user to reload.
- `registry.add_project` now loads `perun.md` on **every** registration path
  (CLI `perun add`, create wizard), not just discovery, so projects always have
  manifest data in the overview/manifest tabs. Existing rows are repaired by
  `python -m perun discover` or `registry.load_manifest_from_disk(slug)`.
- Updated `README.md`, `perun.md`, `AGENTS.md`, `docs/CODEMAP.md`, and
  `docs/TECHNICAL_DOCUMENTATION.md` to reflect PerunChat integration.

### Fixed

- Fixed the `python -m perun project-roots` CLI dispatch (the subparser was
  missing `set_defaults(func=...)`, so it fell back to the help screen).
- Fixed chat history rendering after a hard refresh.
- Fixed sending chat messages with the Enter key.

## v0.7.4 - 2026-10-19

### Fixed

- **Dashboard terminal launch diagnostics.** The frontend no longer replaces the
  server error with a generic “Could not open a terminal” toast; the actual
  message (e.g. missing executable) is now surfaced.
- **Configured terminal paths with directories.** A terminal setting given as a
  full Windows path (e.g. `C:\...\wt.exe`) is now recognized as `wt`/`cmd`/`pwsh`
  both when resolving the executable and when choosing the command-line syntax,
  instead of falling through to the POSIX `-c` branch and failing to open.
- **Dashboard TUI launch falls back to project slug.** Both the project card and
  Agent Console now pass the project slug to `/api/run-in-terminal` so a missing
  `cwd` can be resolved from the registry instead of failing with
  “cwd (or a project with a path) is required”.
- **`perun stop` / `perun restart` lifecycle commands.** A new `perun.daemon`
  module tracks the server PID in `<PERUN_HOME>/perun.pid`. `perun serve` now
  refuses to start if another instance is already running and suggests the
  proper command; `perun restart` gracefully terminates the old server and
  starts a replacement, avoiding the previous blind kill-and-pray approach.

## v0.7.3 - 2026-10-18

### Security

- **Mandatory local API token.** Every state-changing `/api/*` request (any
  method other than `GET`/`HEAD`/`OPTIONS`) now requires
  `X-Perun-Token: <token>` (or `Authorization: Bearer <token>`), compared with
  `hmac.compare_digest`, and is rejected with **401** otherwise. The token is
  `PERUN_HOME/api-token`, created atomically (`O_CREAT|O_EXCL`) with `0600`
  permissions and overridable per process with `PERUN_API_TOKEN`. The dashboard
  receives it injected into `index.html` as `window.PERUN_TOKEN` by `GET /` (and
  the frontend attaches it automatically); `GET /api/settings` never returns it.
  Read-only `GET`/SSE flows are unchanged.
- **Origin check compares the effective port.** `_origin_guard`
  (`_origin_parts`/`_origin_ok`) now requires the `Origin` scheme *and* effective
  port (80/443 when absent) to match the request host, closing a default-port
  bypass. Middleware order is Host → Origin → token.
- **Telegram allow-list is no longer first-come-first-served.** An empty
  `telegram_allowed_chats` grants nobody; pairing uses a persisted one-time code
  (`Settings.telegram_pair_code`, generated with `os.urandom(4).hex()`,
  `hmac.compare_digest`-checked, handled *before* the auth gate) via `/pair <code>`.
  The code is exposed only to loopback callers through `/api/telegram/status` and
  is blanked in `GET /api/settings`. `/stop` is now per-chat.
- **`POST /api/run-in-terminal` builds the command server-side.** It accepts
  structured fields (`cwd`/`project`, `prompt`, `model`, `agent`, `session`,
  `run_uid`, `tui`, `shell`) or `{action:"auth-login", provider}`; a free-text
  `command` is refused, and every value is quoted for the target shell
  (`_ps_quote`/`_posix_quote`/`_cmd_quote`) so a prompt containing PowerShell
  `$(...)` can no longer execute. Newlines/NUL are rejected.
- **`git push` validates the remote and branch.** The remote must appear in
  `git remote` output (blocking `--receive-pack=` option injection); branch names
  reject empty/`-`-prefixed/control/space/`:` values; `--` precedes positionals.
- **`perun_telegram_send` refuses chats outside the allow-list** (exfiltration
  guard), and the destructive/durable MCP tools gained the same `confirm` gate as
  `perun_delete_project` (`perun_set_instructions`, `perun_set_manifest`,
  `perun_discover` with `roots`, `perun_create_project` for clones).
- **Instruction-file writes are contained.**
  `orchestrator._safe_instruction_filename()` reduces a manifest-supplied name to
  a plain `*.md` basename, and `write_project_instruction_file()` refuses paths
  that escape the project root (symlinked `.perun`/file) instead of following
  them.
- **Orchestrator children no longer inherit raw secrets.** `_child_env()` strips
  `TELEGRAM_BOT_TOKEN`, `TG_BOT_TOKEN`, `PERUN_API_TOKEN`, `PERUN_PASSWORD`,
  sets `PERUN_DEPTH`, and injects the resolved Telegram token as
  `PERUN_TELEGRAM_TOKEN` so agent MCP servers can still use `perun_telegram_send`.
  `MAX_AGENT_DEPTH = 2` stops unbounded agent recursion.

### Fixed

- **Orchestrator.** Cancelling a *queued* run deadlocked (the slot limiter is now
  a mutable `_SlotLimiter`); the terminal `end` event is single-shot (`_ended` +
  `_emit_end`); POSIX runs lead a new session so `terminate_tree()` signals the
  whole process group (`killpg`); a restart marks still-active runs `interrupted`
  (not `done`) and shutdown cancels active runs and terminates tracked terminals;
  subscriber queues are bounded (`SUB_QUEUE_LIMIT = 4096`, drop-oldest); run
  history is dropped on a 300 s timer; `stream()` polls every 15 s with an
  `is_final` short-circuit; `cancel()` is idempotent and handles queued runs; run
  logs are line-buffered so they update during a run.
- **Memory/search.** Secret filtering keys off the raw score (freshness affects
  ordering only); `GLOBAL_FRESHNESS_HALF_LIFE_DAYS = 365`; `prune(keep=...)`
  guards negatives; `chunk_markdown` dedupes repeated titles; `_rrf_fuse` dedupes
  by `(project, file, line)`; the query embedding is computed once and threaded
  through `unified_search`/`search_facts`/`search_docs`; global facts/docs are
  deduped by `(source_type, id)` at best score; `max_code<=0` returns `[]`.
- **grep/search.** Fail-closed root gate (`_is_project_path_safe`),
  `..`-stripping `_sanitize_root`, pattern validation (empty/NUL/`MAX_PATTERN_LEN
  = 512`), nested-quantifier rejection (`_looks_catastrophic`), `max_results<0` →
  `invalid_input` and `==0` → empty ok, keeping parsed stdout when ripgrep exits
  `2` (with a `warnings` field), `--crlf`, whole-word `(?:)` wrapping in the
  Python fallback, root + nested `.gitignore` handling, symlink-escape skipping,
  and an 8 MB per-file cap in the fallback (ripgrep stays uncapped).
  `_looks_catastrophic()` now catches nested forms like `((a+)+)` thanks to a
  recursive parser.
- **Memory search.** `search_facts()` and `search_docs()` use `casefold()`
  consistently, so queries like `straße` match stored `strasse`.
- **Indexing.** `index_project_docs()` returns and logs `skipped_sensitive`, the
  count of document chunks that contained secrets and were not persisted.
- **Server shutdown.** The blocking run-cancel + terminal-terminate cleanup now
  runs in `asyncio.to_thread()` so the event loop is not stalled during shutdown.
- **Registry/config/discovery.** `slugify` transliterates diacritics (including
  `ł`), falls back to a hash for alphanumeric-less names, and never returns an
  empty slug; `add_project("")` raises `ValueError`; duplicate paths raise
  `ValueError`; slugs are capped at 60 chars with an unbounded `-N` suffix loop;
  `get_project` is slug-first with an id fallback; `get_project_by_path` does four
  passes (exact/real/case/ancestor); `discovery.register()` stores the realpath and
  `discover_and_sync` reports a `missing` list; `projects.register_or_update`
  `expanduser()`s the path; `Settings.load()` quarantines an unparseable
  `config.json` (`.corrupt-<stamp>`) instead of letting the next `save()` overwrite
  it, and `save()` is atomic with `0600` set before writing; `config` honours
  `XDG_CONFIG_HOME` like the installer; `_seed_default_instructions()` runs from
  `Orchestrator.__init__` instead of at import; `list_projects()` reads one
  activity row per project; the agent scan covers `agent/` as well as `agents/`.
- **Server.** `_str_field` rejects wrong JSON types with **400**;
  `_resolve_project` returns **404** for an unknown *non-empty* slug instead of
  silently falling back to global scope; `POST /api/memory/prune` requires
  `keep >= 1`; instruction filenames reject NUL and names longer than 120 chars
  with **400**; `GET /api/runs/{uid}/stream` returns **404** for an unknown run.
- **Installer.** `_read_json` tolerates JSONC but **aborts** (exit code 2) on an
  unparseable target config instead of silently replacing it; `OPENCODE_CONFIG` is
  honoured; the MCP entry pins `environment.PERUN_HOME`; `opencode.json` is backed
  up and rewritten only when it actually changes (no more backup pile-up).
- **Telegram.** POSIX `PidLock` creates its pid file with `O_EXCL`, removing the
  check-then-write race that let two bridges both "acquire".
- **`Settings.telegram()` honours `PERUN_TELEGRAM_TOKEN`** (injected by the
  orchestrator) ahead of config and raw environment variables;
  `telegram_sources()` reports `"orchestrator"` when it is in use.
- **Embeddings are repaired automatically.** `memory.reembed_missing()` runs on a
  background thread (5 s startup delay, then every 60 s) and is exposed as
  `POST /api/memory/reembed`, so facts/doc chunks written while Ollama was down
  are re-embedded without manual intervention.
- **Terminal (Linux).** `config.POSIX_TERMINALS` + `terminal_cmd()` launch a real
  emulator (kitty/wezterm/ghostty/alacritty/foot/konsole/gnome-terminal/
  xfce4-terminal/xterm) instead of a TTY-less `bash -c`, and the POSIX fallback
  drops into an interactive shell so the window stays open.
- **Portability.** `test_get_project_by_path_is_normcase` is guarded to Windows,
  and the MCP "no project context" test uses a portable temp directory, so the
  suite passes on Linux as well as Windows.

### Changed

- **Documentation audit against the codebase.** `docs/CODEMAP.md`: corrected
  `__version__` (`0.7.2`), spelled out that the global
  `default_model`/`default_agent` fallbacks apply only to the control plane,
  added the missing providers/skills/tools/MCP/search endpoint groups, the
  on-demand self-signed behavior of `installer/sign.ps1`, both missing `tools/`
  scripts, and entries for the nine test files absent from the test map.
  `docs/TECHNICAL_DOCUMENTATION.md`: added the missing endpoint-table rows
  (`GET /api/terminals` and the project `instruction-files` CRUD), removed a
  stray blank line that split the table, removed a duplicated heading, fixed the
  stale "Latest release details (v0.6.4)" label and the leftover
  `perun.__version__ reports 0.7.0` claim, and added §14 revision entries for
  v0.7.1/v0.7.2. `README.md`: full CLI reference (all 18 subcommands), a note
  that releases self-sign when the cert secrets are unset, and the complete
  `tools/` layout line. `AGENTS.md`: added the `validate-docs` check to Commands.

## v0.7.2 - 2026-10-17

### Added

- **README "Under the hood" section.** Documents how Perun handles **project
  awareness** (self-describing `perun.md` anchors, discovery, indexed docs,
  injected project instructions), **token efficiency** (once-per-session
  instruction injection under 32/128 KiB budgets, bounded top-k retrieval,
  30-day freshness weighting, incremental indexing, capped run output), and
  **memory management** (facts + doc chunks, dedup, graceful degradation, a
  single WAL-mode store). Also corrects the MCP tool count to 23.

### Fixed

- **Release builds can sign without a checked-in certificate.** `installer/sign.ps1`
  aborted with `PFX not found` whenever `installer/certs/PerunSelfSign.pfx` was
  absent (it is never tracked), even though the self-signed fallback — which
  creates the certificate on demand — was supposed to handle exactly that case.
  The existence check now applies only to externally supplied PFXs, so GitHub
  Actions releases produce a signed installer when `PERUN_SIGN_PFX_BASE64` /
  `PERUN_SIGN_PASSWORD` secrets are not set.

- **Project/orchestrator model is now actually forwarded to `opencode run`.**
  The Telegram bridge (and any caller that did not pass an explicit model) used
  to record the project's model on the run but never pass `--model`, because
  `model_explicit` was set from the caller's argument only. OpenCode then
  silently fell back to its own machine-wide default model — e.g. a project
  pinned to `opencode/big-pickle` ran on another provider and failed with
  `provider.quota` 402 "Insufficient account funds". `start_run` now resolves
  and forwards the model for every source (call argument, project override, or
  control-plane setting); only an empty resolution still lets OpenCode choose.
  A whitespace-only override is treated as unset.

  **Behavior change:** a project whose configured `model` is invalid/retired
  will now fail loudly instead of silently running on OpenCode's own default —
  the run record and the executed model are guaranteed to match.

## v0.7.1 - 2026-10-17

### Added

- **`/update-docs` global slash command.** Refreshes `docs/CODEMAP.md`,
  `docs/CHANGELOG.md`, and `docs/TECHNICAL_DOCUMENTATION.md`, plus any docs
  declared in the project's `perun.md` manifest (`docs` map or anchors with
  `kind: doc`). Registered globally via
  `perun/integrations/templates/command_update-docs.md`.

- **Global slash-command reference now includes `/update-docs`.**
  `perun/integrations/templates/INSTRUCTIONS.md` lists `/perun-projects`,
  `/perun-ask`, `/logwork`, and `/update-docs`.

- **Richer project scaffolding.** New projects now get `docs/CHANGELOG.md`,
  `docs/TECHNICAL_DOCUMENTATION.md`, and `docs/CODEMAP.md` in addition to the
  existing `perun.md`, `AGENTS.md`, `README.md`, and `.opencode/` skeleton.
  The generated `AGENTS.md` points to `perun.md` as the primary source of truth
  and anchors the generated docs plus detected dependency files
  (`package.json`, `requirements.txt`, `Dockerfile`, CI configs, etc.).

- **`python -m perun backfill-docs [slug] [--all]`.** Creates the same scaffold
  files for existing projects, skipping any files that already exist. Safe to
  run across every registered project to bring older projects up to the new
  standard.

- **Auto-refresh manifest anchors on discovery.** `perun discover` now appends
  anchors for newly-detected dependency/build/CI files to existing `perun.md`
  manifests without clobbering hand-edited content.

- **Dependency parsing and cross-project references.** `perun discover` and
  `python -m perun refresh-deps` read `package.json`, `requirements.txt`,
  `pyproject.toml`, `Cargo.toml`, and `go.mod` and store each dependency as a
  project-scoped memory fact. Local `file:` references in `package.json` and
  `-e ./path` references in `requirements.txt` are recorded as relationship
  facts, enabling queries like "which projects use React?" or "which projects
  depend on X?".

- **Multi-language command suggestions.** New and backfilled projects get
  per-stack commands (e.g., `npm-install`, `pytest`, `cargo-build`, `go-test`,
  `maven-build`) when multiple language stacks are present.

- **Manifest validation.** `python -m perun validate-docs [slug] [--all]` checks
  `perun.md` for docs or anchors that point to missing files.

### Changed

- **Telegram bridge is no longer auto-started at logon.** The `PerunBridge`
  scheduled task is now opt-in (`python -m perun install --machine --task` or
  `bootstrap.ps1 -Task`). By default the bridge starts with the dashboard
  (`Perun.cmd` / `python -m perun serve --bridge`) and stops when the dashboard
  closes. Running the machine installer removes any previously registered
  `PerunBridge` task unless `--task` is passed.

## v0.7.0 - 2026-10-16

### Added

- **Dashboard model/agent overrides are mirrored into `opencode.json`.** Saving
  a model or agent on a project (dashboard config tab, wizard, or
  `perun_update_project`) now writes the override into the project's own
  `opencode.json` (`model` / `default_agent` keys), so an OpenCode TUI launched
  from the project card honors it too. Existing keys are preserved, clearing the
  override removes the key, and an unparseable config is never clobbered.
  Implemented as `projects.sync_opencode_config()`.
- **Providers tab + dashboard preview.** New `GET /api/providers` and
  `POST /api/providers/{id}/logout` endpoints surface OpenCode auth providers
  and their connection status. A dedicated **Providers** tab and a dashboard
  stat card show connected/total providers; the Connect button opens a terminal
  running `opencode auth login <id>` so Perun never sees API keys.
- **`/logwork` global slash command.** Saves a one-sentence work summary to
  Perun memory via `perun_memory_store`. Called inside a project it stores a
  project-scoped fact; called from the orchestrator / global context it stores a
  global fact. Implemented as `perun/integrations/templates/command_logwork.md`
  and registered by `install_global.py` in `~/.config/opencode/opencode.json`.
- **Global slash-command reference.** `perun/integrations/templates/INSTRUCTIONS.md`
  and the managed `~/.config/opencode/AGENTS.md` now list `/perun-projects`,
  `/perun-ask`, and `/logwork`.
- **Web installers.** Added `setup.ps1` (Windows) and `setup.sh` (Unix) at the
  repo root. They can be run straight from GitHub with `iwr ... | iex` or
  `curl ... | bash`; they clone (or reuse) the repo, install Python
  dependencies, and run the machine installer. `README.md`,
  `docs/CODEMAP.md`, and `docs/TECHNICAL_DOCUMENTATION.md` now document the
  one-liner install paths.
- **Windows installer (`PerunSetup.exe`).** Added an Inno Setup project under
  `installer/` that builds a wizard installer. It can install missing
  prerequisites (Python, git, OpenCode CLI, Ollama), installs Perun, registers
  the `PerunBridge` task, creates shortcuts, and provides a clean uninstall.
  Build it with `installer\build.ps1`.
- **Self-sign helper (`installer\sign.ps1`).** Creates a self-signed
  code-signing certificate and signs `PerunSetup.exe` with `signtool`. Useful
  for dev/enterprise builds; public releases still need a trusted certificate.
- **CI/CD release signing.** Added `.github/workflows/release.yml` to build
  and sign `PerunSetup.exe` automatically on `v*` tags. `sign.ps1` now reads
  an external PFX from `PERUN_SIGN_PFX` / `PERUN_SIGN_PFX_BASE64` and
  `PERUN_SIGN_PASSWORD` environment variables, so a free Certum open-source
  code-signing certificate can be used for trusted releases.

### Changed

- `install_global.py` now installs/removes `logwork.md` alongside the existing
  `perun-projects.md` and `perun-ask.md` command files.

### Fixed

- **Installer verifies SHA256 hashes for downloaded binaries.**
  `installer/install-prereqs.ps1` now checks every downloaded installer
  (Python, Git, OpenCode standalone zip, Ollama fallback EXE) against a
  built-in hash table before executing it. The Ollama fallback URL is pinned to
  a specific GitHub release so the hash stays stable.
- **Provider cache is thread-safe.** `perun/providers.py` guards the provider
  list cache with a `threading.Lock` to prevent races on concurrent dashboard
  requests.
- **Invalid provider logout returns HTTP 400.** `POST /api/providers/{id}/logout`
  now raises `400 Bad Request` when the provider ID is invalid or `opencode`
  rejects the logout, instead of returning `200 OK` with `ok: false`.
- **Skill tasks now invoke TUI slash commands.** In the Agent Console, queued
  tasks with `kind: "skill"` emit `/<skillname>` at the start of the prompt
  chain so OpenCode runs them as slash commands instead of just mentioning the
  skill name in instructions.
- **Consistent “Run” labelling.** The project-card and Agent Console action
  buttons are now labelled **Run** (previously “🖵 OpenCode”), and the Agent
  Console shows “Run 2 tasks” when tasks are queued.
- **Skill name validation.** `perun/skills.py` now rejects skill manifests whose
  `name` contains characters outside `[a-z0-9_-]`, preventing malformed names
  from reaching the TUI as slash commands.
- **Agent Console prompt chain is unit-tested.** The prompt-building logic is
  now in `web/js/prompt_chain.js` and covered by `tests/test_prompt_chain.js`
  run from `tests/test_js_prompt_chain.py`.
- **Memory view filters by project without a query.** Selecting a project in the
  Memory Explorer now reloads recent facts and stats for that project even when
  the query field is empty; semantic search is still used when a query is entered.
- **Dashboard TUI launch no longer passes `run`-only flags.** The project-card
  and Agent Console TUI commands used to append `--model` and `--agent` to the
  base `opencode` invocation, which the OpenCode CLI rejects with
  `Unrecognized flag: --model in command opencode`. The TUI command now only
  passes flags accepted by the base `opencode` command (`--prompt` and
  `--session`) and leaves model/agent resolution to OpenCode's project defaults.

## v0.6.4 - 2026-10-16

### Added

- **MCP Servers dashboard view.** New `#/mcp` page (nav item *MCP Servers*) lists
  every MCP server OpenCode is configured to load — the machine-wide
  `~/.config/opencode/opencode.json[c]` plus each registered project's
  `opencode.json[c]` — with scope, transport, command/URL, resolved executable,
  and a config-derived status (`ready`/`missing`/`disabled`/`remote`/`invalid`).
  A project server that overrides a global name of the same name is marked
  `Overridden by`. Backed by `perun/mcp_servers.py` and `GET /api/mcp/servers`.
- **On-demand live MCP probe.** The `Test` button (and `Test all enabled`) calls
  `POST /api/mcp/servers/probe`, which spawns a local server (or calls a remote
  URL) and performs the real MCP `initialize` + `tools/list` handshake, reporting
  the live tool list, latency, and any captured stderr. Nothing spawns
  automatically, and the command always comes from disk — never the request — so
  the endpoint cannot be used to run arbitrary commands.

### Removed

- **Legacy instruction migration is gone.** Instruction text now lives *only* on
  disk (global `~/.config/opencode/perun/instructions/*.md`, project
  `<project>/.perun/instructions/*.md`). Deleted
  `orchestrator._migrate_legacy_project_instructions()` and
  `orchestrator._migrate_legacy_text_instructions()` (plus the `.migrated`
  marker), the `perun_migrate_project_instructions` MCP tool, and every call
  site in `server.py` and `mcp/perun_mcp.py`.
- **The `projects.instructions` / `projects.instructions_path` columns are
  dropped.** `db._migrate()` removes them from older databases on next start so
  fresh and upgraded schemas converge; `registry.add_project`/`update_project`
  no longer accept them, and `manifest.from_project()` (export) no longer emits
  `instructions`.

### Changed

- **One writer for project instruction files.**
  `orchestrator.write_project_instruction_file()` is now the shared implementation
  (with `overwrite=False` for seed-once semantics) used by the project API, MCP
  tools, discovery, and manifest sync — replacing three copy-pasted helpers in
  `discovery.py`, `projects.py`, and the MCP server.
- **Manifest `instructions` is always routed to a file.** `projects.sync_manifest()`
  now writes declared `instructions` to `.perun/instructions/default.md`
  (seed-once) like discovery does, instead of writing the legacy DB column. This
  fixes an inconsistency where discovery wrote a file but manifest sync wrote the
  column.
- `perun_update_project` writes instructions via the shared helper and no longer
  touches a phantom registry field.
- **Password persistence is encrypted at rest.** `secrets.persist_password()` (the
  dashboard's "Remember on this PC") no longer writes `PERUN_PASSWORD` in plaintext
  to `HKCU\Environment`; it DPAPI-encrypts the password into `<PERUN_HOME>/secret`
  (bound to the current Windows user). `secrets._password()` still prefers the
  `PERUN_PASSWORD` environment variable and only then falls back to the file.

### Fixed

- **Manifest `instructions` from HTTP/MCP is routed to a file too.** `PUT
  /api/projects/{slug}/manifest` and the `perun_set_manifest` MCP tool now go
  through `projects.sync_manifest()`, so declared `instructions` are seeded to
  `.perun/instructions/default.md` instead of being silently dropped.
- **The dashboard's OpenCode button works again on `cmd`/Windows Terminal.**
  `/api/run-in-terminal` wrapped the frontend-built command text in
  `subprocess.list2cmdline([exe, "/k", cmd_text])`, which escapes inner quotes as
  `\"` — a C-runtime convention `cmd.exe` does not understand. Every launch died
  with *"The filename, directory name, or volume label syntax is incorrect."* The
  `cmd` and `wt` branches build the command line as raw text again (`... /k ` +
  `cmd_text`), so the inner quotes reach cmd intact; both branches are covered by
  regression tests.
- **`db._migrate()` tolerates a failed `DROP COLUMN`.** The retired-column drop is
  wrapped, so SQLite < 3.35 (which has no `DROP COLUMN`) or a concurrent drop can
  no longer abort `init_db()`.
- **`perun global-instructions --clear` re-seeds the default block.** Custom files
  are deleted *before* the default guidance is re-seeded, so global guidance is
  never left absent.
- **`search.unified_search()` no longer blocks on a hung backend.** The executor
  shuts down without waiting, making the per-future timeout a real bound.
- **`grep.py` no longer passes the unsupported `--files-from` flag.** ripgrep and
  ast-grep have no path-list flag, so the "long command line" branch always failed
  and silently degraded to the pure-Python fallback. Global searches now always
  pass project paths positionally (an oversized command line still degrades safely),
  and the dead `_path_list_file` helper is gone.
- **A "no matches" exit code is no longer treated as a search failure.**
  ripgrep/ast-grep exit `1` when a pattern simply matches nothing; `perun/grep.py`
  now returns a successful empty result instead of re-running the whole search with
  the Python fallback (previously a full directory walk on every zero-hit query).
- **Docs & cleanup.** Documented `cmd_global_instructions` (plus `cmd_run` and
  `cmd_encrypt_config`) in `docs/CODEMAP.md` and the `global-instructions` CLI in
  `docs/TECHNICAL_DOCUMENTATION.md`; added `perun_list_instruction_files` to the
  global `INSTRUCTIONS.md` tool table; removed the orphaned `.migrated` marker and
  unused imports; renamed a stale test.
- **MCP discovery no longer hides a broken config, and lists nothing twice.**
  `perun/mcp_servers.py` now returns unreadable/unparseable config files as
  `parse_errors` (shown in the dashboard as a warning card) instead of treating
  them as "no servers", tolerates JSONC trailing commas as well as comments, and
  collapses duplicate `(scope, project, name)` entries — e.g. a name declared in
  both `opencode.json` and `opencode.jsonc` — to a single row.
- **MCP probing can no longer pin the server.** Concurrent probes are capped by a
  module semaphore (a saturated gate returns `status:"busy"` instead of blocking a
  worker), the timeout is clamped to `MAX_PROBE_TIMEOUT` (60 s), the HTTP probe
  shares one deadline across `initialize` + `tools/list` (≈timeout, not 2×), the
  stdio stderr buffer is bounded, non-`.exe` shims (`.cmd`/`.bat`/`.ps1`) are
  launched through the right interpreter, and a force-killed child is reaped.
- **The MCP view explains a stale backend.** Because `web/` is served live from
  disk, a browser can load the new page while the running service is still the old
  process; the view now detects the endpoint's 404 and says to restart Perun,
  rather than rendering a misleading empty list.

## v0.6.3 - 2026-10-16

### Added

- **Project-local multi-file instruction system.** Projects now keep their
  custom instructions as Markdown files under `.perun/instructions/`. Each file
  has YAML front matter (`name`, `enabled`). Enabled files are injected into
  every new agent session in sorted filename order.
- **HTTP CRUD for project instruction files.** New endpoints:
  `GET /api/projects/{slug}/instruction-files`,
  `GET /api/projects/{slug}/instruction-files/{filename}`,
  `POST /api/projects/{slug}/instruction-files/{filename}`,
  `PATCH /api/projects/{slug}/instruction-files/{filename}/toggle`, and
  `DELETE /api/projects/{slug}/instruction-files/{filename}`. Filenames are
  validated (no `/`, `\`, `..`) and normalized to `.md`; resolved paths are checked
  to stay within the project root.
- **MCP tool updates for project instruction files.** `perun_get_instructions`
  now returns the effective combined instructions by default and can read a
  specific file when `filename` is given. `perun_set_instructions` writes to
  `.perun/instructions/<filename>` when `filename` is provided. New tool
  `perun_list_instruction_files` lists project-local instruction files. New tool
  `perun_migrate_project_instructions` triggers the legacy migration explicitly.
- **Lazy migration from legacy instructions.** `_migrate_legacy_project_instructions`
  in `perun/orchestrator.py` moves the old inline `instructions` column or the
  old `instructions_path` file into `.perun/instructions/legacy.md` and clears the
  registry columns. It runs automatically on first read.
- **Dashboard: clickable instruction file links.** Global and project instruction
  filenames in the dashboard are now `vscode://file/` links that open the file in
  the user's default editor. Added `GET /api/instructions/dir` to expose the
  absolute global instructions directory, and `safeUrl()` now allows `vscode:` URLs.

### Changed

- `perun/orchestrator.py` now layers `_project_instruction_blocks(project)`
  after global instructions and before the user prompt. The legacy
  `_project_instruction_text()` helper has been removed.
- `PUT /api/projects/{slug}/instructions` now writes to
  `.perun/instructions/default.md` (or the supplied `filename`) and ignores
  `instructions_path`.
- `perun_set_instructions` no longer accepts `instructions_path` and always
  writes to `.perun/instructions/<filename or default.md>`.
- `perun_create_project` and `perun_update_project` write `instructions` to
  `.perun/instructions/default.md` instead of the legacy column.
- Project discovery writes manifest `instructions` to
  `.perun/instructions/default.md` instead of the legacy column.

### Fixed

- **MCP input validation hardening.** Instruction tools now strictly validate
  filenames: empty names, `.`, `..`, `/`, `\\`, non-`.md` extensions, and any
  path escaping the project root are rejected with a clear `[perun]` error.
  Global search tools reject empty patterns/rules, require `projects` to be a
  list (or omitted), and safely coerce numeric params (`max_results`, `max_memory`,
  `k`) before use.
- **Instruction system hardening.** Added `_coerce_enabled()` for robust boolean
  parsing, `MAX_INSTRUCTION_BYTES` / `MAX_TOTAL_INSTRUCTION_BYTES` caps, and
  XML-style `<perun_instructions>` delimiters with escaping to prevent prompt
  injection. Default-seeding and legacy-text migration now happen once at module
  import / server lifespan, keeping `compose_prompt()` read-only. Front matter is
  generated with `yaml.safe_dump` everywhere.
- **Search error mapping.** `perun/grep.py` error responses now include
  `error_kind` (`invalid_input`, `not_found`, `timeout`, `unknown`). HTTP search
  endpoints use this for 400 vs 500 status mapping.
- **Unified search filtering.** `_resolve_projects()` now returns `None` for an
  explicit but empty project filter so `unified_search()` returns empty results
  instead of searching globally.
- **Discovery no longer clobbers instruction edits.** Manifest `instructions`
  seed `.perun/instructions/default.md` only on first discovery; an existing file
  (including dashboard edits) is left untouched on later scans.
- **Memory writes no longer hold a DB connection across a network call.**
  `memory.remember()` computes the Ollama embedding before opening the
  connection.
- **MCP `projects` filter.** `_validate_projects_list()` now keeps `None`
  ("no filter") distinct from `[]` ("no projects"), fixing `perun_unified_search`
  returning empty results when `projects` was omitted.
- **MCP instruction front matter.** `perun_set_instructions` now builds YAML
  front matter with `yaml.safe_dump`, so filenames with YAML-special characters
  cannot corrupt the file.
- **GET endpoints are read-only.** `GET /api/instructions/dir` and
  `GET /api/projects/{slug}/instruction-files` no longer create directories.
- **`unified_search` error kind.** Returns `error_kind` so `POST /api/search/unified`
  maps invalid input to 400 and internal failures to 500.
- **Robustness.** `process_tracker._kill_tree` restores the `taskkill` timeout and
  `CREATE_NO_WINDOW` flag; `projects.register_or_update` translates a slug
  exhaustion `ValueError` into a clean `ProjectError`; the invalid
  `memory.project_id` foreign key (global rows use `project_id=0`) was removed.

### Removed

- **Legacy single-file / inline project instructions are gone.** The
  `projects.instructions` and `projects.instructions_path` SQLite columns are kept
  as migration sources but are no longer read or written by the orchestrator,
  HTTP API, or MCP tools. The dashboard's **Project → Instructions** tab no longer
  shows the "Legacy single-file instructions" editor, and the project-creation
  wizard no longer offers a "Custom instructions" field (it was never submitted).
  Instruction files are managed exclusively through the multi-file list.
- **Obsolete inline global instruction fields are gone.** `Settings.orchestrator_instructions`
  and `Settings.global_agent_instructions` have been removed from the dataclass.
  Legacy values are still read once from `config.json` and migrated to scoped
  Markdown files under `~/.config/opencode/perun/instructions/`, after which
  the keys are stripped from `config.json`. The dashboard settings UI now only
  manages the instruction file list.

## v0.6.2 - 2026-10-15

### Added

- **Bundled code-search tools.** Perun now provides `perun_grep`,
  `perun_ast_grep`, `perun_global_grep`, and `perun_global_ast_grep` directly
  through the global Perun MCP server, with matching HTTP endpoints
  `POST /api/search/grep`, `POST /api/search/ast-grep`,
  `POST /api/search/global-grep`, and `POST /api/search/global/ast-grep`. All
  use the native `rg` / `ast-grep` CLIs when available and fall back to
  pure-Python implementations otherwise, so every project gets structural and
  regex search without installing separate MCP servers. The `global_*` variants
  search every registered project at once and accept an optional `projects` list
  to restrict the scope.
- **Unified cross-project search.** New `perun/search.py` fuses vector memory,
  `perun_global_grep`, and `perun_global_ast_grep` using Reciprocal Rank Fusion,
  exposing a single MCP tool (`perun_unified_search`) and HTTP endpoint
  (`POST /api/search/unified`). Default agent guidance now routes agents through
  CODEMAP → `perun_unified_search` → targeted global code search → built-in grep.
- **RRF fusion for cross-project code search.** `perun/search.py` fuses ranked
  ripgrep and ast-grep results with Reciprocal Rank Fusion, deduplicating by
  project, file, and line/column before truncation.
- **Token-efficient truncation and global match caps.** Grep, ast-grep, and
  unified search cap total result counts and truncate long lines to keep
  responses within token budgets.
- **Per-source error surfacing in unified search.** Each search backend reports
  its own success or failure; partial failures are surfaced in the response
  instead of failing the entire query.

### Fixed

- **Unified search now respects the `projects` filter for vector-memory results.**
  `_search_memory()` queries only the requested projects instead of returning
  memory hits from every registered project.
- **Search endpoints validate `max_results`, `projects`, and `include` types.**
  `POST /api/search/*` handlers reject malformed arguments before invoking the
  underlying search tools.

### Changed

- Default agent guidance now points to Perun's own `perun_grep` and
  `perun_ast_grep` as the preferred search tools, with the built-in `grep` tool
  reserved as the last resort.

## v0.6.1 - 2026-10-15

Security, reliability and performance fixes following a whole-system audit.

### Fixed

- **XSS via project links.** `safeUrl()` in the dashboard now escapes its output
  so malicious project URLs cannot inject HTML/JS.
- **Settings token leak.** `PATCH /api/settings` no longer echoes back the
  Telegram token and validates/coerces numeric fields before saving.
- **Cross-port origin bypass.** The origin guard now compares ports exactly,
  blocking `127.0.0.1:8761` from reaching `127.0.0.1:8760`.
- **Run finish race.** `/api/runs/{uid}/finish` now rejects transitions from
  already-final states.
- **Cancelled-run race.** The orchestrator re-checks cancellation after acquiring
  the concurrency semaphore so a cancelled run never spawns a process.
- **Memory-leak cleanup.** `EventBus.close` clears global subscribers and the
  orchestrator prunes its internal process/reply maps when runs finalize.
- **Database cascade consistency.** Foreign keys and `ON DELETE CASCADE` are now
  declared for `memory`, `activity`, `doc_chunks` and `bridge_links`;
  `registry.delete_project` also explicitly cleans related rows for existing DBs.
- **Race-safe inserts.** Project slug insertion and `memory.remember` now use
  explicit `INSERT OR IGNORE` patterns to avoid check-then-insert races.
- **Ollama health caching.** The memory stats probe caches the Ollama reachability
  result to avoid repeated failed probes.
- **Process tracker descendant-only walk.** Marker lookup now walks descendants of
  the known parent first and only falls back to a full-system scan when the
  parent is gone, dramatically reducing CPU usage.
- **Run wall-clock watchdog.** A dedicated watchdog thread kills runs that exceed
  `run_timeout` even when stdout is blocked.
- **Telegram message deduplication.** The bridge now drops duplicate updates
  (same chat + message id) that can occur during long-polling reconnects.
- **Chunk size cap.** `chunk_markdown` splits sections larger than 8 KB so no
  single embedding row can grow unbounded.
- **Search result caps.** Keyword fact/document queries are limited to 200
  candidates to keep memory search responsive on large indexes.
- **Unified search pooling.** `unified_search` fetches more candidates from each
  source, pools them once, and truncates to `k` so a high-scoring document is
  not displaced by lower-scoring facts.
- **Prune retention scope.** The retention subquery in `prune()` is now scoped to
  the same project filter so per-project pruning does not touch other projects.
- **Settings staleness.** `PATCH /api/settings` now propagates the updated
  settings object to the orchestrator so `run_timeout`, model fallbacks, etc.
  apply immediately without a restart.
- **Security headers.** The dashboard now serves `X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy` and a restrictive `Content-Security-Policy`
  on every response.

### Frontend

- **SSE reconnection.** Both the per-run stream and the global activity stream
  reconnect automatically with capped exponential backoff when the connection
  drops.
- **Modal close on click.** Modal backdrop dismissal now uses `click` instead of
  `mousedown`, preventing accidental closes during text selection.

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

- **File-based global instructions.** Markdown files under
  `~/.config/opencode/perun/instructions/` carry Perun-wide agent guidance with
  YAML front matter (`name`, `scope: orchestrator|all_projects`, `enabled`).
  New APIs: `GET/POST /api/instructions`, `GET /api/instructions/{filename}`,
  `PATCH /api/instructions/{filename}/toggle`, `DELETE /api/instructions/{filename}`.
- **File-based project instructions.** The existing `PUT /api/projects/{slug}/instructions`
  now accepts an optional `instructions_path` to write a project-local Markdown file
  instead of storing the text inline.
- **Default agent guidance block.** When no custom global instruction files exist,
  Perun seeds one that tells every agent to prefer `docs/CODEMAP.md` anchors first,
  then `ast-grep`/`mcp-grep`, and only fall back to built-in `grep` as a last resort.
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
  - Fixed `POST /api/run-in-terminal` for `cmd.exe` (and Windows Terminal using
    `cmd` tabs): the command is now passed as a raw command-line string so
    Python's `list2cmdline` does not wrap it in outer quotes, which `cmd.exe`
    would strip and corrupt the `cd /d` path and `opencode` argument quoting.
  - Project-card **OpenCode** TUI launch is now equivalent to running `opencode`
    in the project directory: it no longer sends a forced prompt, so OpenCode
    uses its normal session/model resolution (the same as a manual launch)
    instead of starting a fresh chat that triggers provider region errors. The
    Agent Console still passes the composed prompt.
  - `/api/runs/tui` now accepts explicit `model`/`agent` overrides from the
    frontend; without them it falls back to the project's configured defaults.
  - Fixed **Agent Console stop button** reliability: it now uses the same
    `/api/runs/{uid}/cancel` endpoint as the project card (so the run record is
    finalised even when the terminal process was not tracked), passes the
    project's `model`/`agent` through to the TUI launch, and resets the UI if the
    initial run request fails instead of leaving the button stuck disabled.
  - Fixed `PERUN_RUN_UID` marker handling for bash-like terminals: the marker
    is now placed directly on the `opencode` process (`cd ... && PERUN_RUN_UID=…
    opencode ...`) instead of being scoped only to the `cd` command, so the
    process tracker can resolve the TUI tab on macOS/Linux shells.
  - Telegram `/projects` command now sends an inline keyboard with one button
    per project (and the current project marked ▸). Tapping a button switches the
    chat to that project without typing `/project <slug>`.
  - `Perun.cmd` now starts the Telegram bridge **in-process** with
    `perun serve --bridge` instead of relying on the standalone `PerunBridge`
    scheduled task. Before launching it asks `perun status --json` whether the
    bridge mutex is actually held; only a *live* bridge suppresses the in-process
    start. A crashed/stale task no longer leaves Telegram silent. Duplicate
    pollers are prevented by the existing machine-global `PidLock` mutex.
  - `python -m perun status` now has a `--json` flag for machine-readable output
    (the default remains human-readable).
  - Added optional password-based encryption for the Telegram bot token (and
    future secrets) in `config.json`. Set `PERUN_PASSWORD`, then run
    `python -m perun encrypt-config`. `Settings.telegram()` decrypts
    transparently when the password is available; otherwise Telegram degrades
    gracefully to "not configured". Requires `cryptography` (added to
    `requirements.txt`).
  - The Telegram page in the dashboard now has an **Encryption password** field.
    If you enter a password when saving the bot token, it is encrypted in
    `config.json`. You can also encrypt an already-saved plain token by entering
    a password and leaving the token field blank, and a **Change password**
    button lets you re-encrypt the stored token without pasting it again.
  - `POST /api/telegram/bot` now only encrypts when an explicit password is
    supplied in the request; the `PERUN_PASSWORD` environment variable is no
    longer used as an implicit encryption trigger.
  - `POST /api/telegram/bridge/restart` now reloads the in-process bridge token
    when the dashboard owns the bridge, so token/password changes take effect
    without closing the window. It still falls back to restarting the standalone
    `PerunBridge` task when that task owns the lock.
  - Manifest tab file and doc paths are now rendered as clickable links that
    open via `vscode://file/...` / `file://...`. Paths are normalised so they
    cannot escape the project root.
  - Project Memory form now defaults to **This project** scope when opened
    inside a project, so facts count toward the project instead of becoming
    global by default.
  - Added missing `perun/secrets.py` entry to `docs/CODEMAP.md`; restored
    regression tests for config-token-overrides-env and settings source
    reporting.
  - Added `tools/migrate_global_facts.py` to move Perun-specific global facts
    onto the `perun` control-plane project. It is non-destructive by default and
    only deletes duplicate global rows when passed `--delete-duplicates`.
  - **Encrypted Telegram tokens no longer block the launcher.** `Perun.cmd`
    starts the dashboard even when `config.json` contains an encrypted token and
    `PERUN_PASSWORD` is not set. The dashboard shows an unlock modal on startup,
    validates the password via `POST /api/telegram/unlock`, and can persist it
    to the Windows user environment with the "Remember on this PC" option.
    `perun/secrets.py` gained `persist_password()` for the registry write.
- **Project cards now show the Stop button immediately when a run starts.**
  Previously `applyCardStates()` only updated the pip/class visuals; the running
  banner and **■ Stop** button were only rendered on the next full page render.
  `applyCardStates()` now syncs the banner and stop button against the current
  active runs, so launching OpenCode from a card updates the card live.

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
---
## [0.7.42] - 2026-10-24
- PerunChat bridge: memory integration (facts tagged by room/sender, injected into prompts, heuristic extraction)
- Dashboard: private room management UI (create multi-party rooms, invite/remove members)
---
## [0.7.43] - 2026-10-24
- Server: proxy PerunChat private room management endpoints
- Dashboard: validate room ids before adding/creating; quieter private-rooms load
- PerunChat memory: typed `[FACT(type=...)]` markers, heuristic extraction, semantic deduplication, explicit user triggers
---
## [0.7.50] - 2026-10-24
- PerunChat: add regression test for typing/reply sequence-number monotonicity.

---
## [0.7.49] - 2026-10-24
- PerunChat: fix typing indicator sequence number collision that caused the following reply to be rejected by the gateway as non-monotonic.

---
## [0.7.48] - 2026-10-24
- PerunChat: re-enable native typing indicator with idle-frame cleanup on every reply outcome (success, no-reply, timeout, failure); typing frames are no longer queued for redelivery.

---
## [0.7.47] - 2026-10-24
- PerunChat: temporarily disable the native typing indicator while the clear/stuck-indicator behavior is hardened; add cache-busting to dashboard JS.

---
## [0.7.46] - 2026-10-24
- PerunChat: shorten reply hard cap and orchestrator wait to 60 seconds; send idle typing frame when generation gives up.

---
## [0.7.45] - 2026-10-24
- PerunChat: clear typing indicator when reply generation times out or fails; add 60-second client-side stale-indicator guard.

---
## [0.7.44] - 2026-10-24
- PerunChat: native typing indicator (application/perun-typing) with 3-minute hard timeout
