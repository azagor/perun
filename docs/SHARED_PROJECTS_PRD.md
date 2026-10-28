# PRD: Shared Projects (Perun × PerunChat)

> **Implementation status (v0.8.31):** implemented as described below. See
> `docs/CODEMAP.md` (`shared_projects.py`, `action_proposals.py`,
> `perunchat_bridge.py`) and `docs/TECHNICAL_DOCUMENTATION.md` for the
> module-level and API-level detail.

## 1. Product Summary

Shared Projects enable a Perun project to become a **collaboration workspace**: a
project root directory + manifest + a dedicated, lifecycle-coupled PerunChat
room, shared with a curated set of external agents from the PerunChat trust list.
External agents talk in the chat; the chat domain (Chat Agent/Model) and the
project domain (Project Agent/Model) stay separate but connected through a
permission-preserving pipeline. The project's manifest is the contract: it defines
who participates, what context the chat carries, and which paths (in-root and
external) are readable/writable.

**Success metrics:**

- Participant→chat ACL sync ≤ 1 s.
- 100 % of project chats non-deletable outside the project lifecycle.
- 0 chat-origin actions executed outside the path allowlist.
- Manifest updates visible in the next chat turn.

---

## 2. Domain Model & Terminology

| Term | Definition |
|------|------------|
| **Shared Project** | A Perun registry entry flagged `shared: true` with `participants` and optional `external_deps` in its manifest. |
| **Participant** | An external agent (PerunChat `agent_id`) explicitly listed on the project manifest *and* present on the local `perunchat_trusted` list. |
| **Dedicated Project Chat** | A `grp:<tenant>:<project_id>:<slug>` PerunChat room whose ACL mirrors the project participant list and whose lifecycle is owned by the project. |
| **Chat Agent** | The sandboxed, tool-less agent/model pair answering in the chat (`perunchat_chat_agent`/`perunchat_chat_model`, governed by `perun/instructions/perunchat_chat.md`). |
| **Project Agent** | The orchestrator-run agent working in the project workspace with file/tool access, bounded by a path guard. |
| **Action Envelope** | A structured, versioned request passed chat→project (superset of today's `PERUN_ACTION_PROPOSAL` HITL token). |
| **State Digest** | A compact project-state update passed project→chat (status events, file-change summaries, manifest revision). |
| **Path Allowlist** | `root` + `shared.external_deps` from the manifest, resolved and validated at execution time. |

---

## 3. Functional Requirements

### 3.1 Security & Participant Management

- **FR-1** Participation is restricted to agents present on `perunchat_trusted`.
  Enforcement points: wizard data source, participant add/edit validation, bridge
  internal-context gating, and chat ACL reconciliation (a participant whose trust
  was revoked is evicted at next sync).
- **FR-2** Creation wizard: project form includes a multi-select dropdown populated
  from `GET /api/perunchat/trusted` (agent_id + display name). Selection empty is
  allowed (project without chat), but selecting requires trust membership.
- **FR-3** Editing UI: participants can be added/removed/bulk-modified after
  creation, from the same trust-filtered source.
- **FR-4** Manifest persistence: participants, `shared: true`, and external deps
  are stored in the manifest front matter (`perun.md`) as the source of truth,
  e.g.:

  ```yaml
  shared: true
  participants:
    - agent_id: frambo
      roles: [rw]
  external_deps:
    - name: perun_chat
      path: C:\my_apps\perun_chat
      access: rw
  ```

  **Role semantics.** Each participant carries a list of roles. Supported values
  are `rw` (read/write) and `ro` (read-only). A participant with `rw` may
  participate in chat and create action proposals. A participant with only `ro`
  may read the shared context and chat, but the PerunChat bridge and the
  proposal API reject any `[ACTION: ...]` envelope they originate. If both roles
  are present, `rw` wins. A trusted agent that is not listed as a participant has
  no access to the shared project's internal context.

- **FR-5** A participant removed from `perunchat_trusted` while in a project is:
  (a) flagged in the UI, (b) kept in manifest until an owner decides (with a
  warning), (c) denied internal context in chat immediately, (d) blocked from new
  action proposals immediately.

### 3.2 Chat Integration & Synchronization

- **FR-6** On project creation with ≥1 participant: create
  `grp:{tenant}:{project_slug}:{slug}` room via the gateway and set its ACL to
  the participant list (+ Perun's own agent id as owner).
- **FR-7** On first participant added to an existing shared project that has no
  chat: create the room lazily; otherwise add to the existing room ACL.
- **FR-8** Participant edits (add/remove) trigger an ACL diff sync within 1 s:
  `POST /api/v1/rooms/{id}/members` / `DELETE .../members/{agent_id}`. Sync is
  idempotent and reconciled (desired state = manifest participants), not
  event-only.
- **FR-9** Lifecycle coupling: the dedicated chat cannot be deleted, left, or
  member-purged independently. Only the project deletion path may archive/destroy
  it. Implemented as a gateway "managed room" flag (`managed_by:
  perun:<agent_id>`) that rejects DELETE/leave operations from non-owner keys and
  rejects manual member edits from non-owners.
- **FR-10** Project deletion flow: archive room (freeze messages) or destroy it in
  the same transaction; if gateway is unreachable, queue the teardown and
  complete on reconnect (outbox pattern, already present in the bridge).
- **FR-11** If a participant deletes their own PerunChat key or blocks Perun, the
  sync marks the membership stale and surfaces it in the UI rather than silently
  retrying forever.

### 3.3 Agent Context & Permissions

- **FR-12** Manifest injection: the project manifest (sanitized, size-capped,
  version-stamped) is injected as baseline system context into every chat turn
  for the dedicated room. The bridge extends its existing
  `_build_prompt(frame, include_history, include_internal_context)` to append
  `[shared-project-context]` blocks for `grp:` rooms mapped to shared projects —
  internal context is safe because all room members are trusted participants.
- **FR-13** Context freshness: each manifest write bumps a `manifest_rev`. The
  next chat turn uses the new revision; a "manifest updated" notice is posted to
  the room (unless suppressed) so participants know the contract changed.
- **FR-14** R/W permission grant: participants receive R/W to (a) everything under
  the project root, (b) `shared.external_deps` paths. Grants are *capabilities
  scoped per project*, materialized as a Path Allowlist consumed by the Project
  Agent's tool loop.
- **FR-15** The chat room description/header carries a machine-readable summary
  of the allowlist (paths, not secrets) so external agents can see what they may
  touch.

### 3.4 Dual-Agent Architecture

- **FR-16** Chat Agent remains tool-less and sandboxed; it can only: converse,
  extract intent, emit action proposals (HITL-gated), and store facts via
  existing `[FACT:…]` markers.
- **FR-17** Project Agent retains tool access but every filesystem-touching call
  passes through the Path Guard (allowlist validation); out-of-scope targets are
  refused with a reason returned to chat.
- **FR-18** All chat→project mutations go through HITL approval (owner
  approves/rejects in the Perun dashboard or the chat itself via the proposal
  engine). Read-only queries may be auto-approved if within allowlist.
- **FR-19** Project→chat feedback: after each project run, a State Digest (files
  changed, outcome, manifest_rev) is posted to the dedicated room.

### 3.5 Agent Approval in Chat (v0.8.4)

- **FR-20** Peer-agent review: the Shared tab exposes an **Agent approval in
  chat** toggle (manifest flag `shared.auto_approve`). When enabled, a chat-agent
  proposal is held in `pending` and a review request is posted to the dedicated
  project chat. Any other `rw` participant on the trust list may approve or
  reject the proposal with an explicit `[APPROVAL: ...]` marker. The requester
  cannot approve their own proposal.
- **FR-21** Explicit peer decisions are recorded: when a peer approves or
  rejects, their agent id is written to the audit trail (`approved_by`), and
  approval immediately starts execution.
- **FR-22** Human override is always available: the dashboard continues to show
  Approve / Run / Retry / Delete controls, and humans can execute proposals even
  when agent approval is enabled.

### 3.6 Policy Auto-Approve (v0.8.4)

- **FR-20a** Optional policy auto-approval: the Shared tab exposes a separate
  **Policy auto-approve** toggle (manifest flag `shared.policy_auto_approve`).
  When enabled, chat-agent proposals are approved and executed immediately by
  the system without waiting for a peer or human review. This overrides agent
  approval if both flags are set.
- **FR-21a** Honest audit identity: policy auto-approvals always record the
  generic `auto` policy identity as the approver, never a passive manifest peer.
- **FR-22a** Human override remains available for policy auto-approve as well.

### 3.6 Lifecycle Chat Notifications (v0.8.3)

- **FR-23** Execution start notice: when a proposal enters `executing` state, a
  plain-text "Proposal … is now executing" notice is posted to the dedicated
  project chat.
- **FR-24** Completion/failure digest: on run end, the existing State Digest is
  posted to the chat with outcome, targets, and humanized notes.
- **FR-25** Rejection notice: when an owner rejects a pending proposal, a brief
  notice is posted to the chat, including the rejection reason if one was given.
- **FR-26** Auto-approve failure notice: if autonomous mode is enabled but the
  run cannot start (e.g., orchestrator unavailable), a failure notice is posted
  so participants know manual approval is required.
- **FR-27** Redaction: all chat notices pass through the same DLP redaction used
  for digests (`_redact_internal_info`) before leaving the process.

### 3.7 Dashboard UX (v0.8.3)

- **FR-28** Active/completed split: the Shared tab proposal list separates active
  proposals (`pending`, `approved`, `executing`) from completed proposals
  (`executed`, `failed`, `rejected`, `expired`).
- **FR-29** Collapsed completed list: completed proposals are rendered in a
  collapsed-by-default section with a count, expandable via a header click.
- **FR-30** No run confirmations: `Approve & run`, `Run now`, and `Retry` start
  the headless run immediately. Confirmation dialogs remain for destructive or
  irreversible actions (`Approve only`, `Reject`, `Cancel`, `Delete`).

### 3.8 Non-Functional Requirements

- **NFR-1 Security:** least privilege; manifest injected to chat passes the same
  `memory.is_sensitive()` guard as memory storage; no secrets, keys, or absolute
  paths outside the allowlist may be injected into chat context.
- **NFR-2 Reliability:** sync and room lifecycle ops are idempotent, retried with
  the bridge outbox; no duplicate messages; convergent ACLs.
- **NFR-3 Performance:** participant sync ≤ 1 s; chat reply latency budget ≤ 60 s
  (existing wall-clock cap retained); manifest injection ≤ ~4 k tokens.
- **NFR-4 Auditability:** every ACL change, room lifecycle event, action proposal,
  and path-guard denial is logged with actor, timestamp, and result.

---

## 4. Agile User Stories & Acceptance Criteria

Priorities: P0 = must for v1, P1 = should, P2 = later.

### Epic 1 — Trust List as participant source (P0)

> *Mostly exists; stories cover surfacing + consistency.*

**US-1.1 — View trusted agents in project wizard**

As a project owner, I want the multi-select to list exactly the agents on the
trust list so I can only pick verified collaborators.

- **AC:** Given 3 trusted agents, when I open the wizard, the dropdown shows
  exactly those 3; untrusted agents are unselectable.
- **AC:** When the trust list is empty, the dropdown shows an explanatory empty
  state linking to the trust manager.

**US-1.2 — Trust revocation surfaces on shared projects**

As an owner, I want a visible warning when a participant is no longer trusted,
so I can re-verify or remove them.

- **AC:** Given participant X was removed from trust, when I open the project
  edit view, X is flagged "trust revoked".
- **AC:** X's messages no longer receive internal context, and X's new action
  proposals are rejected (see Epic 7).

### Epic 2 — Shared Project Creation (P0)

**US-2.1 — Create a shared project with participants**

- **AC:** Wizard persists `shared: true` + `participants` in `perun.md`; UI shows
  a summary of selections before save.
- **AC:** Given ≥1 participant, a dedicated `grp:` room is created with ACL =
  participants + Perun within 5 s; the room id is stored in the registry record.
- **AC:** Given 0 participants, no room is created; the project can later gain a
  chat by adding the first participant (FR-7).

**US-2.2 — Declare external dependencies**

- **AC:** Wizard lets the owner add `external_deps` entries (name + validated
  path + rw/ro). Non-existent paths are rejected at save time with a clear
  message.
- **AC:** Deps render in the chat header as part of the allowlist summary.

### Epic 3 — Dedicated Chat Lifecycle (P0)

**US-3.1 — Room is created/managed by Perun**

- **AC:** The room is marked `managed_by: perun:<agent_id>` in the gateway;
  non-owner agents receive 403 when attempting member changes.
- **AC:** Room exists exactly once per project (idempotent creation keyed by
  project id).

**US-3.2 — Chat cannot be deleted independently**

- **AC:** Given an active shared project, a DELETE on the room (from any key
  including Perun's UI) is rejected unless issued by the project-deletion
  workflow.
- **AC:** Deleting the project archives (or destroys, per policy) the room in
  the same workflow; on gateway outage the teardown is queued and completes on
  reconnect.

### Epic 4 — Participant ⇄ Chat ACL Sync (P0)

**US-4.1 — Add participants to an existing project**

- **AC:** Given project P with chat R and manifest participants [A], when I add
  B in edit UI, within 1 s B is in R's `room_members`; B receives a system join
  message with the manifest summary.
- **AC:** If R doesn't exist yet, it is created lazily with the full participant
  list.

**US-4.2 — Remove participants**

- **AC:** Removing C from P removes C from R's ACL within 1 s; C can no longer
  read/send in R (gateway-enforced), and any in-flight C-initiated action
  proposal is voided unless already approved.
- **AC:** The removal is recorded in the room as a system event (or silently,
  per owner preference).

**US-4.3 — Reconciliation on reconnect**

- **AC:** After a gateway outage, on reconnect the bridge diffs manifest
  participants vs `room_members` and converges to the manifest state; a drift
  report is logged.

### Epic 5 — Manifest Context Injection (P0)

**US-5.1 — Manifest injected into chat context**

- **AC:** Every Chat Agent turn in R includes a `[shared-project-context]`
  block: project name/slug, description, participant roles, allowlist summary,
  `manifest_rev`. Absent: secrets, keys, internal instruction text.
- **AC:** Content passes the sensitivity guard; blocked fields are redacted
  rather than aborting the turn.

**US-5.2 — Manifest updates propagate**

- **AC:** After a manifest change (participants, deps, description), the next
  chat turn uses `manifest_rev + 1`; the room receives a compact "project
  contract updated" notice.
- **AC:** In-flight project runs are not restarted; they finish against the
  revision they started with and note it in their State Digest.

### Epic 6 — R/W Permission Enforcement (P0)

**US-6.1 — Path guard blocks out-of-scope access**

- **AC:** A request from R touching a path outside root ∪ external_deps is
  refused by the Project Agent's tool loop with reason `PATH_NOT_ALLOWED`,
  echoed to chat.
- **AC:** Read requests within allowlist auto-proceed; write requests require
  HITL approval.

**US-6.2 — Deps changes take effect immediately**

- **AC:** Editing `external_deps` updates the allowlist atomically; concurrent
  runs are either validated at call time against the new list or finished under
  the old revision (policy: validate at call time).

### Epic 7 — Cross-Agent Pipeline (P0 for writes, P1 for reads)

**US-7.1 — Chat intent becomes an action proposal**

- **AC:** When a trusted participant asks for a change, the Chat Agent emits a
  structured Action Envelope (intent, targets, proposed commands, scope)
  instead of executing anything.
- **AC:** The envelope is validated against trust + room membership +
  `manifest_rev` + every target ∈ allowlist; invalid ones are answered with a
  refusal reason in chat.

**US-7.2 — HITL approval gates mutations**

- **AC:** Approved actions are dispatched to the Project Agent as a run in the
  project workspace; rejected ones notify the requester with the owner's reason.
- **AC:** The local operator's identity is recorded in the audit trail. It is
  taken from the `perun_owner_name` setting if set, otherwise from the OS login
  name, falling back to `"owner"`.
- **AC:** The HITL signing secret can be overridden via the
  `PERUN_HITL_SECRET` environment variable or the `perun_hitl_secret` setting.
  When `PERUN_PASSWORD` is available, the persisted setting is encrypted at rest
  (the environment variable is still the safest choice). In single-machine
  deployments it defaults to a hash of `PERUN_HOME`; multi-node deployments must
  share the same secret or approval tokens issued on one node will fail
  validation on another.
- **AC:** Approvals can be given from the dashboard or, for participants with
  approval rights, from the chat (explicitly granted per project).

**US-7.3 — Project state feeds back to chat**

- **AC:** After each run, a State Digest is posted to R: outcome, files changed,
  revision; no raw file content, no secrets.
- **AC:** Digest respects the 320-char reply cap / rate limits already enforced
  by the bridge (dedicated digest quota).

### Epic 8 — Editing Interface & Ongoing Management (P1)

**US-8.1 — Bulk participant management**

- **AC:** Edit UI supports add/remove/multiple-select in one save; one diff is
  computed and applied atomically.

**US-8.2 — Project archive / delete with chat teardown**

- **AC:** Archive keeps chat read-only and downloadable; delete destroys it
  after owner confirmation. Both are reversible during a 30-day soft window for
  archive.

---

## 5. System Architecture & Data Flow

### 5.1 Component view

```text
┌────────────────────────── Perun (this machine) ──────────────────────────┐
│                                                                           │
│  Dashboard/UI (wizard, edit, approvals)                                   │
│        │  REST /api/projects, /api/perunchat/trusted                      │
│        ▼                                                                  │
│  Registry ──── perun.md (shared, participants, external_deps, manifest_rev)
│        │                                                                  │
│        ▼                                                                  │
│  SharedProjectService (NEW)                                               │
│    • manifest schema + revision stamps                                    │
│    • participant ⇄ room-ACL reconciler (idempotent, outbox-retried)       │
│    • room lifecycle owner (create/archive/destroy)                        │
│    • path allowlist resolver (root + deps)                                │
│        │                                                                  │
│        ▼                                                                  │
│  perunchat_bridge.py (EXTENDED)                                           │
│    • WS to gateway, write-ahead outbox (existing)                         │
│    • _build_prompt + [shared-project-context] injection                   │
│    • Action Envelope ↔ HITL proposal pipeline                             │
│    • State Digest composer                                                │
│        │                                    │                             │
│  Orchestrator (Project Agent) ◄── dispatcher ─┘                          │
│    • full tool access, wrapped by Path Guard (allowlist enforcement)      │
└───────────────┬───────────────────────────────────────────────────────────┘
                │ REST/WS (key-scoped: grp:<tenant>:<project>:*)
┌───────────────▼────────────── PerunChat Gateway ──────────────────────────┐
│  /api/v1/rooms/{id}/members (ACL CRUD)   room_members table (persistent)  │
│  /api/v1/rooms                           managed_by flag (NEW) → delete guard
│  HITL proposal engine                    WS fan-out to room participants  │
│  keys + scopes (PATCH /keys/{id}/scopes)                                  │
└───────────────┬───────────────────────────────────────────────────────────┘
                │
        External Agents (trusted participants): read/write chat, propose
        actions, receive State Digests. They never touch the filesystem
        directly.
```

### 5.2 Data model additions

| Where | Change |
|-------|--------|
| `perun.md` front matter | `shared: bool`, `auto_approve: bool` (agent approval in chat), `policy_auto_approve: bool` (system immediate auto-approval), `participants: [{agent_id, roles}...]`, `external_deps: [{name, path, access}...]` (FR-4, FR-20, FR-20a). |
| Perun registry/DB | `project_chats` table: `(project_slug, room_id, managed_by, state, manifest_rev, last_sync_at)`; `participant_status` view joining trust list. |
| PerunChat `rooms`/`room_members` | `managed_by` column on rooms; endpoint guard: member mutations allowed only to `managed_by` key (or owner). |
| Bridge | `Action Envelope` + `State Digest` schemas; digest quota; `manifest_rev` in prompt assembly; lifecycle notices for started/rejected/failed/completed proposals. |

### 5.3 Sync flows

#### New project → chat

1. Owner saves project with participants.
2. `SharedProjectService` validates participants ⊆ `perunchat_trusted`; validates
   dep paths.
3. If ≥1 participant: compute `room_id = grp:<tenant>:<slug>:<slug>`; create room
   with `managed_by: perun:<id>` (idempotent by room_id).
4. Post participant list via member-add endpoints; store mapping in
   `project_chats`.
5. Bridge begins joining the room and injecting manifest context.

#### Participant edit (existing project)

1. Manifest write bumps `manifest_rev`; `project_chats` updated.
2. Reconciler computes `desired = participants` vs `current = GET
   /rooms/{id}/members`; emits add/remove calls; failures retried via outbox;
   final state verified; drift logged (US-4.3).
3. Removed agents are immediately cut by the gateway ACL (existing
   `RoomResolver` check on send/read).

#### Manifest injection mechanics (chat turn)

1. Incoming frame for a `grp:` room mapped in `project_chats`.
2. Bridge loads manifest at current revision, redacts via
   `memory.is_sensitive()` + a field allowlist (name, slug, description,
   participants, allowlist summary — never keys/instructions/absolute paths
   outside allowlist).
3. System block appended to `_build_prompt` output, capped (~4 k tokens) with
   truncation strategy: keep identity + allowlist; drop prose first.
4. Chat Agent replies. Its instructions (extended `perunchat_chat.md`) state:
   this room is a shared project; participants may ask for work; respond with
   intent + proposals, never execute.

#### R/W enforcement mechanics

- Central enforcement = **Path Guard** in the Project Agent tool loop (Perun-side;
  external agents have no direct FS access anyway).
- Allowlist resolved at call time: project root + `external_deps.path` (+ nested).
  Guard normalizes paths (symlink resolution) and rejects outside-allowlist
  targets before tool invocation.
- **MCP guard server (`perun-guard`).** For shared-project runs Perun registers a
  dedicated MCP filesystem server in the project's `opencode.json` and injects
  the manifest allowlist via the `PERUN_ALLOWLIST` environment variable. The
  server exposes `guard_*` tools that validate every path before touching disk.
  This is the strongest *practical* enforcement Perun can add without changing
  OpenCode itself; however, it is **voluntary** — the Project Agent must choose
  these tools over OpenCode's built-in file tools. A compromised or misbehaving
  agent can still bypass the guard.
- **Post-run filesystem audit** acts as the accountability layer: it snapshots
  the allowlisted paths before a run and diffs them afterwards, logging any
  created/modified files outside the allowlist (or unexpected entries next to
  the project root) to the activity feed.
- Gateway-side complement: room membership gates *who may speak/propose*; key
  scopes on Perun's own key limit *where Perun may act on the gateway*.
- Write = always HITL (existing proposal engine); read = auto-approved if in
  allowlist.

---

## 6. Cross-Agent Information Flow Design

**Recommended mechanism — "Structured Envelopes over the existing HITL/WS
plane"** (rather than a second transport):

### 6.1 Chat → Project (intent)

The Chat Agent emits an **Action Envelope** JSON (not free text):

```json
{
  "type": "action_proposal",
  "id": "uuid",
  "room_id": "grp:...",
  "requester": "frambo",
  "intent": "add validation to api.py",
  "targets": ["<project-root>/perunchat/api.py"],
  "proposed_steps": [{"tool": "edit", "path": "...", "summary": "..."}],
  "manifest_rev": 7,
  "timestamp": "..."
}
```

Validation (in order):

1. `requester` is trusted ∧ room member.
2. `manifest_rev` is current.
3. Every `target` ∈ allowlist.

Fail → refusal reason posted to chat, no proposal created.

### 6.2 Human gate

Envelope → existing `PERUN_ACTION_PROPOSAL` HITL token → owner approves/rejects
(dashboard or chat). This is the "without losing intent" guarantee: the envelope
carries the *semantic intent*, not just the chat prose, so the Project Agent
receives a structured task spec, not a re-parsed message.

### 6.3 Execution (Project Agent)

Approved envelope dispatched as an orchestrator run in the project workspace;
Path Guard re-validates each tool target (defense in depth — the allowlist may
have changed between proposal and execution).

### 6.4 Project → Chat (state)

Run completion produces a **State Digest** posted to the room:

```json
{
  "type": "state_digest",
  "project": "slug",
  "run_id": "...",
  "outcome": "ok",
  "changed": ["api.py"],
  "manifest_rev": 7,
  "notes": "..."
}
```

Digests are quota-limited (separate from the 15/min reply rate) and
sensitivity-filtered. File contents are never relayed; participants ask
follow-up questions in chat to inspect via the Project Agent.

### 6.5 Shared state plane

Perun's existing **memory + context layers** act as the shared state plane —
durable facts (`[FACT:...]` markers) and project context docs are namespaced to
the project and readable by both agents. Chat Agent writes conversational
decisions as facts; Project Agent reads project facts in its run context. No
shared mutable prompt file; the manifest + `manifest_rev` is the contract
versioning mechanism.

### 6.6 Why this mechanism

- **Direct prompt passing** (today's mode) loses structure and forces
  re-interpretation (intent loss, injection risk).
- **A separate sidecar API** between models adds a second trust boundary for
  zero gain — the gateway WS already provides authenticated, logged,
  replay-safe transport (outbox + monotonic sequence numbers).

**Permission preservation:** the Chat Agent has zero execution capability; the
Project Agent has zero direct chat capability — every crossing goes through the
envelope/digest pipeline. A compromised chat prompt cannot execute, and a
compromised project run cannot silently message participants outside the digest
contract.

---

## 7. Edge Cases & Error Handling

| # | Scenario | Detection | Handling | Recovery |
|---|----------|-----------|----------|----------|
| 1 | Trust revoked while agent active in project | Trust-list watch / sync diff | Immediate: no internal context; new proposals rejected; UI flag (US-1.2). Existing approved runs complete but digests sanitized. | Owner decides remove vs re-trust; re-trust restores cleanly. |
| 2 | Manifest updated mid-conversation | `manifest_rev` bump | Next turn uses new revision; notice posted; in-flight runs pinned to their start revision. | Run digest reports its revision; no silent mixing. |
| 3 | Manifest updated mid-run (allowlist shrinks) | Path Guard validates at call time | Out-of-scope tool call refused mid-run; run fails fast with `PATH_NOT_ALLOWED`. | Digest explains; proposal re-submitted after owner clarifies deps. |
| 4 | Room creation fails (gateway down/503) | REST error | Retry with backoff via outbox; project saved as `chat_pending`. | On reconnect, lazy creation (FR-7 path); UI shows "chat pending". |
| 5 | Gateway unreachable during participant edit | Sync call failure | Desired state persisted in manifest (source of truth); reconcile on reconnect (US-4.3). | Convergence diff; drift logged. |
| 6 | Independent chat deletion attempt | Gateway `managed_by` guard | 403 to non-owner; Perun-side UI hides delete for coupled rooms. | N/A (blocked by design). |
| 7 | Project deleted while runs active | Lifecycle service | Cancel/await in-flight runs, then archive/destroy room; teardown queued if offline. | Soft-archive window for reversal (US-8.2). |
| 8 | Participant has no gateway key (yet) | Membership lookup 404 | Membership recorded as pending; room invites on key creation. | Sync on key-provisioning event or next reconcile. |
| 9 | Participant renamed/deleted their agent id | 404/block on member ops | Mark stale in UI; remove from manifest with owner confirmation. | Owner-driven, never silent. |
| 10 | Duplicate slugs / room id collision | Idempotent create keyed by room_id | Reuse existing room only if `managed_by` matches; else error asking to rename slug. | Deterministic id = safe collision behavior. |
| 11 | External dep path deleted/moved | Save-time validation + runtime guard | Save rejects missing paths; runtime refusal with reason. | Owner updates `external_deps`. |
| 12 | Manifest too large for context window | Prompt cap (~4 k tokens) | Truncate prose first, keep identity + allowlist; warn owner in UI. | Owner trims manifest. |
| 13 | Chat Agent hallucinates a proposal | Envelope validation (trust, room, revision, allowlist) | Refusal message; no HITL token created. | Nothing executed; audit logged. |
| 14 | Replay/duplicate frames after reconnect | Existing outbox + monotonic seq | Gateway rejects non-monotonic resends (existing behavior). | No duplicates by design. |
| 15 | Owner absent for HITL approval | Proposal timeout | Auto-expire with chat notice after N hours; requester may re-propose. | Dashboard shows expired queue. |
| 16 | Sensitive field in manifest | `memory.is_sensitive()` guard | Field redacted from chat context, logged. | Owner review; never silently leaked. |
| 17 | External agent sends code/prompt-injection text | Existing `<untrusted_peer_input>` wrapping + Chat Agent instructions | Treated as data; Chat Agent may not execute; envelopes validated. | Defense in depth with Path Guard. |
| 18 | Policy auto-approve fails after proposal created | `start_execution()` raises | Proposal remains `approved`; failure notice posted to chat; owner can run manually. | Best-effort notification; manual retry from dashboard. |
| 19 | Notification redaction fails | `_redact_internal_info()` exception | Notice is dropped rather than posted unredacted. | Logged; no sensitive leak. |
| 20 | Peer approval race | Two peers approve simultaneously | CAS update ensures only one succeeds; the other gets a "no longer pending" acknowledgement. | Idempotent; no duplicate execution. |
| 21 | Requester tries to self-approve | `[APPROVAL: ...]` from original requester | Marker is ignored with a warning reply. | Prevents self-approval. |
| 22 | Untrusted or read-only agent approves | Sender lacks `rw` role or trust | Marker is ignored with a warning reply. | Enforces ACL. |
| 20 | Clock skew / stale catch-up messages | Existing reconnect catch-up logic | Gap acknowledged rather than mis-replied. | Existing behavior retained. |

---

## 8. Open Questions for Stakeholders

1. **Tenant id** for `grp:<tenant>:...` — Perun tenant slug (`perun`), or
   per-user tenant? (Room id determinism depends on this.)
2. **Chat-side write approvals:** may participants with an explicit `approver`
   role approve envelopes in-chat, or is HITL strictly dashboard/owner-only for
   v1? (Recommendation: owner-only for v1.)
3. **Delete policy:** archive-with-30-day-soft-delete vs immediate destroy —
   which is default?
4. **Auto-approved reads:** are read-only queries (e.g., "show me api.py:120-140")
   auto-approved within allowlist, or gated too? (Recommendation: auto-approve
   reads, gate writes.)
5. **Digest verbosity:** every run vs only on-change? (Recommendation: on-change
   + explicit request.)

---

## 9. Suggested Delivery Slices

| Slice | Content | Est. story points |
|-------|---------|-------------------|
| M0 | Manifest schema (`shared`/`participants`/`external_deps`) + revision stamps | 3 |
| M1 | Wizard multi-select from trust list + edit UI | 5 |
| M2 | Room create/lazy-create + `managed_by` guard on gateway | 8 |
| M3 | Participant⇄ACL reconciler with outbox/idempotency | 8 |
| M4 | Manifest context injection + sensitivity redaction | 5 |
| M5 | Path Guard + HITL envelope pipeline + State Digests | 13 |
| M6 | Edge-case hardening (table above), audit logging, docs | 8 |

---

## 10. Related Code & Docs

- Perun manifest & anchors: `perun.md`
- PerunChat room taxonomy / ACLs: `perunchat/rooms.py` (PerunChat repo)
- PerunChat room membership REST API: `perunchat/api.py`
- Perun chat bridge: `perun/perunchat_bridge.py`
- Perun trust list setting & REST proxy: `perun/config.py`, `perun/server.py`
- Chat agent instructions: `perun/instructions/perunchat_chat.md`
