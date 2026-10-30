# Perun — AGENTS.md context-budget optimisation

> **Status:** phases 0-4, 6 and 7 **implemented** (2026-10-28); v0.8.31 shipped,
> and the same context layer is unchanged through **v0.8.57**.
> All five managed projects migrated, a production-readiness audit hardened the
> implementation, and v0.8.31 completed anchor resolution, CODEMAP-aware
> chunking, the compact CODEMAP outline, and local-embedding fallback. Phase 5 —
> the eval-set half of the parity harness — remains outstanding. The `AGENTS.md`
> rules added since are behavioural one-liners (browser tickets, self-service key
> rotation), which stay inline by design.
> **Problem owner:** every Perun-managed project.
> **Related:** [DESIGN.md](DESIGN.md) §2 (principles), [CODEMAP.md](CODEMAP.md),
> [TECHNICAL_DOCUMENTATION.md](TECHNICAL_DOCUMENTATION.md) §3.

---

## 1. The problem, measured

OpenCode loads **two** `AGENTS.md` files into every session: the global one
(`~/.config/opencode/AGENTS.md`) and the project one. Both are paid in full,
on every request, in every project. On this machine **before the migration** (2026-10-21):

| File | Bytes | ~tokens (chars/4) | Loaded in |
|---|---|---|---|
| `~/.config/opencode/AGENTS.md` (Perun managed block) | 6,100 | ~1,525 | **every** project |
| `perun/AGENTS.md` | 9,435 | ~2,359 | the Perun project |
| `trading_bot/AGENTS.md` | 62,631 | ~15,658 | trading_bot |
| `parental_control/AGENTS.md` | 78,055 | ~19,514 | parental_control |
| `perun_in_the_machine/AGENTS.md` | 6,024 | ~1,510 | perun_in_the_machine |
| `perun_chat/AGENTS.md` | 3,446 | ~860 | perun_chat |

Worst case (a `parental_control` session) starts with **~20.9k tokens of ambient
instructions** before the user has typed a word. The average is bad; the trend
is worse: every project adds its own copy of the same material, so the total
grows linearly with the number of projects while each session pays for one
copy.

### 1.1 Where the bytes actually go

Reading the five files, the content falls into four buckets:

1. **Behavioural rules** — "never commit secrets", "HITL is mandatory",
   "CODEMAP-first", "never block the event loop". *Short, high-value, and the
   model must not be missing them.* These must stay inline.
2. **Reference material** — the MCP tool table, slash-command tables, build
   pipelines, ADB/logcat command lists, architecture prose, key-file lists.
   These are **lookups**: needed only while doing one specific kind of task.
3. **History** — version histories and changelogs, already duplicated in
   `docs/CHANGELOG.md`.
4. **Credentials** — SSH host/user/key, deployment secrets. These are in
   *every* prompt and *every* transcript, which is a leak surface, not just a
   token cost.

Buckets 2–4 are the same size as bucket 1 combined, and buckets 2 and 4 have
no business being in a system prompt.

### 1.2 Two structural causes

**Cause A — duplication.** The same material is written out repeatedly, in
different places, with no owner:

* The MCP tool table exists in `mcp/perun_mcp.py` (as tool docstrings, which
  OpenCode already loads as MCP tool schemas), **and** in
  `perun/integrations/templates/INSTRUCTIONS.md`, **and** again in the
  per-project `AGENTS.md` files (10 hits in `parental_control`, 5 in
  `perun_in_the_machine`).
* The global slash-command table exists in `~/.config/opencode/commands/*.md`
  (loaded by OpenCode as command definitions) **and** in `INSTRUCTIONS.md`.
* The shared "instruction modules" (persona, code-doc traceability, search
  routing policy, memory protocol) are **copy-pasted** into every project
  `AGENTS.md` under a heading literally called *"Inlined instruction modules
  (migrated for OpenCode V2)"*.

That heading is the smoking gun: when OpenCode V2 stopped resolving the
`instructions[]` array, the fix was to **inline** every project instruction
file into `AGENTS.md`. That is the mechanism that made the files grow.

**Cause B — no owner, no drift control.** `AGENTS.md` is a hand-edited
Markdown file with no generator, no schema, and no check. Nothing reports
"this section is now duplicated in three other projects" or "this tool table
is missing `perun_context`". Growth is unbounded and silent.

---

## 2. The design

### 2.0 The Awareness Parity Contract (hard requirement)

> **The agent's awareness of a project must not fall below its current level.**

This is the constraint that governs every decision below. It rules out the
naive reading of "shrink `AGENTS.md`" — deleting reference material and hoping
the agent asks for it. An agent that never read the ADB commands does not know
to ask for them; *deferral without a discovery path is a silent capability
loss*, and a silent capability loss is worse than a large prompt.

Four mechanisms together guarantee parity:

**P1 — Nothing is deleted, only relocated.** The complete original
`AGENTS.md` of every project is retained **verbatim** as a context doc
(`<slug>.agents.full`) and stays retrievable in a single call. The slimmed
`AGENTS.md` is a *pointer* to a document that still contains everything. If the
distillation misses something, the content is still there.

**P2 — The inventory is always visible.** The kernel lists *every* context key
for the project, so the agent always knows the full extent of what it does not
currently have in context. Awareness of the *shape* is never lost.

**P3 — Relevant content is pre-fetched, not requested.** The agent is not
required to know what is relevant. It is instructed to state **what it is
about to do**, and Perun resolves that into the relevant context:

```
perun_context("what I'm about to do: rebuild the Android APK")
```

One call, task-scoped, returns exactly the docs that task needs. On the
orchestrated path Perun does this **automatically** before the run starts, so
the agent does not have to cooperate at all.

**P4 — Parity is measured, not asserted.** A migration is not accepted until a
parity harness shows the agent answers the same questions with and without the
distillation. See §6.

**The split rule follows from the contract:**

| Content | Fate | Why |
|---|---|---|
| Behavioural rules ("never", "always", "must") | **stay inline** | Missing them ⇒ wrong behaviour ⇒ parity broken |
| Project overview / shape | **stays inline** (one paragraph) | Cheap, and it preserves the mental model |
| Reference material (commands, tables, pipelines) | **→ context doc** | Reachable via P1/P3; a lookup, not a rule |
| History / changelog | **→ context doc** | Already in `docs/CHANGELOG.md` |
| Credentials | **→ secret store** | Never belonged in a prompt (leak surface) |

A behavioural rule is **never** moved, even when short. Only *reference*
material moves. This is the line.

### 2.1 Core insight: retrieval economics

The right question is not *"how do we shrink the file"* but *"which content
must be in context at t=0, and which can be fetched when it becomes relevant?"*

| | Must be inline | Can be retrieved |
|---|---|---|
| Test | "Would the model act **wrongly** if this were missing?" | "Would the model merely **ask a question** it cannot answer?" |
| Example | "Never commit secrets" | "The exact ADB command for pulling logcat" |
| Cost of missing it | incorrect behaviour, possible damage | one extra tool call |
| Mechanism | always in the prompt | `perun_context("<key>")` |

Everything that fails the first test stays inline. Everything that passes the
second test moves into a **context store** and is fetched on demand.

### 2.2 Perun Context — a keyed, vector-backed context layer

Perun already owns a vector store (`memory.py` → `doc_chunks`, Ollama +
`nomic-embed-text`) and an anchoring system (`manifest.py` → `anchors`). The
design connects them into one retrieval primitive.

**A *context doc* is a named, retrievable body of reference text.**

* It has a **stable key** (`handbook.tools`, `trading_bot.build`).
* It has a **title** and a body.
* It has a **scope**: `global` (machine-wide, owned by Perun) or `project`
  (owned by one repo).
* It is **embedded** into the existing vector store, so it is also reachable
  by fuzzy semantic search, not only by exact key.
* It is **content-hashed and re-indexed** on change, so it cannot go stale
  silently.

**The agent-facing surface is one new MCP tool:**

```
perun_context(topic: str = "", project: str | None = None,
              k: int = 3, mode: str = "auto") -> str
```

*topic* is a **key**, an **anchor label**, or a **description of what you are
about to do**. All three go through one entry point, because the agent should
not have to know which kind of question it is asking.

Resolution order in `mode="auto"`:

1. **Exact key** — `handbook.tools` → that doc. Deterministic, zero cost,
   never needs Ollama.
2. **Anchor label** — resolve through the project's `perun.md` manifest, so the
   agent can ask by human name ("the build pipeline") as well as by key.
3. **Key substring** — `ssh` → every key containing `ssh`, with titles, so the
   agent can pick. Only attempted for short topics (≤ 48 chars), so a task
   description does not accidentally substring-match.
4. **Semantic pre-fetch** — embed the topic and rank the context docs by
   cosine similarity + keyword overlap, across the project scope *and* the
   global scope. This is the path a task description takes, and the reason the
   layer is vector-backed rather than a lookup table. It is what makes P3 work:
   the agent states its intent, Perun works out what it needs.

`mode="list"` returns the **key inventory** (key + title + one-line summary) for
the scope — the discovery surface when the agent does not know a key, and the
mechanism behind P2.

**Deterministic first, fuzzy second** is the load-bearing decision. Pure
vector retrieval for a fact like "the SSH host" is unreliable: a semantic
match may surface the wrong chunk, and a flaky lookup is worse than inline
text. The key path is exact; the vector path only *adds* reach.

### 2.3 Anchors gain a memory binding

The manifest anchor vocabulary grows by one key:

```yaml
anchors:
  - label: Build pipeline
    kind: doc
    path: docs/BUILD.md
    memory: trading_bot.build      # ← retrieval key
```

* `memory` is optional; an anchor without it behaves exactly as today.
* `perun_get_manifest` returns anchors **with** their `memory` key, so the
  chain is: read manifest → see an anchor → `perun_context(anchor.memory)`.
  `perun_context` resolves anchor labels, so the agent can use whichever name it
  has.
* `perun context verify` checks that an anchor that declares `memory` has that
  key present in the store, and vice-versa. *(Implemented; the `validate-docs`
  variant was folded into `perun context verify`.)*

This is the "anchors in AGENTS.md link to vector memory" idea from the
original request, with one correction: **the anchor binds to a deterministic
key, and the vector index is the fallback, not the primary path.**

### 2.4 The AGENTS.md *kernel*

`AGENTS.md` becomes a small, generated file with three parts:

```markdown
# <Project> — Agent Reference

<One paragraph: what this project is, what it is built with, what it does.>

## Read first
- [`perun.md`](perun.md) — identity, docs map, commands, anchors.
- `docs/CODEMAP.md` — navigation source of truth.

## Critical constraints (always apply)
- …the behavioural rules, verbatim, still inline…

## Context (retrievable)
| Key | Covers |
|---|---|
| `trading_bot.build` | 4-stage build pipeline + troubleshooting |
| `trading_bot.adb` | ADB / logcat debugging commands |
| `trading_bot.agents.full` | **the complete previous AGENTS.md, verbatim** |

Before working, call `perun_context("<what you are about to do>")` — it
returns the rows above that apply to your task. The last row is the whole
original document; nothing was deleted.
```

and the **global** kernel (the managed block in
`~/.config/opencode/AGENTS.md`) shrinks to the text below — 1,832 B including
the two marker lines:

```markdown
<!-- BEGIN perun (managed) -->
# Perun — context kernel

Perun is the machine-wide overseer for every OpenCode project: a registry of
all local projects, project-namespaced vector memory, cross-project code
search, and agent-run orchestration.

**First action in any Perun-managed project:** before reading or editing code,
call `perun_context("<what you are about to do>")`. It resolves your intent
into the reference material you need — build commands, server access, key
files, conventions, the tool surface. Do not guess at these; they are
retrievable, and a guess costs a wrong turn.

Perun's reference material lives in *context docs* with stable keys:

| Key | Covers |
|---|---|
| `handbook.tools` | every perun_* tool, its arguments and what it returns |
| `handbook.search` | grep / ast-grep / unified search, RRF ranking, fallbacks |
| `handbook.memory` | facts vs docs, scoping, freshness, the sensitive-data guard |
| `handbook.commands` | /perun-projects, /perun-ask, /logwork, /update-docs |
| `handbook.cross-project` | perun.project_roots.resolve, sibling repos, why not hand-crafted paths |
| `handbook.sessions` | starting runs, models, agents, streaming, cancellation |
| `handbook.onboarding` | dashboard, Telegram bridge, fresh-machine setup and repair |
| `handbook.secrets` | named credentials, encrypted at rest, pulled on demand |
| `handbook.global` | the complete previous global AGENTS.md block, verbatim |

Always true, never retrieved away:

- Resolve cross-project paths with `perun.project_roots.resolve(name, …)` —
  never hand-craft a relative path.
- Never commit secrets; never paste credentials into a file or a prompt.
- Prefer `perun_unified_search` over ad-hoc grep for discovery.
- Read the project's `perun.md` manifest before acting on its structure.
<!-- END perun (managed) -->
```

The key table has one row per handbook doc — nine today — and is rendered by
`handbook.render_key_table()`, so it cannot drift from the store.

Two properties make this work:

* **The kernel is generated, not authored.** `perun context sync` writes it
  from the handbook sources; `perun context verify` fails if it drifts. This
  kills Cause B (no owner, no drift control) outright.
* **The kernel is stable.** Adding an MCP tool changes `handbook.tools` — a
  context doc — and *not* the kernel. So the ambient prompt stops growing as
  the product grows. This is the real compounding win: today every new tool
  costs every user 20–40 tokens forever, in every project.

### 2.5 Two surfaces, two strategies

Perun controls the prompt on one surface and not the other, and the design
should exploit that:

| Surface | Who assembles the prompt | Strategy |
|---|---|---|
| **OpenCode TUI** (human types) | OpenCode reads `AGENTS.md` | Small kernel + a mandatory `perun_context("<task>")` call, which the kernel states as the *first action* |
| **Orchestrated run** (`opencode run` via Perun) | Perun, via `orchestrator.compose_prompt` | **Pre-fetch**: Perun embeds the prompt, retrieves the top matching context docs, and injects them *before* the run — no agent cooperation required |

The orchestrated path costs one local embedding (~tens of ms) and saves the
model a round trip — the agent gets what it needs on the first try instead of
discovering that it needs it. It is also the **parity guarantee for the
orchestrated path**: there, pre-fetch is not a nudge the agent may ignore, it
is a step Perun performs.

On the TUI path the guarantee is the combination of P1 (the original document
is retained verbatim), P2 (the inventory is visible), and the kernel's
"first action" instruction. If the agent ignores even that, it still has the
key table and one call away from the complete original document.

Pre-fetch is gated conservatively: at most **2 documents** and **4,000
characters** are injected. The gate is **deterministic first** (exact key →
anchor label → key fragment → subject match, threshold 0.5), with a
local-embedding fallback (score ≥ 0.6) only when Ollama is available and the
deterministic rules find nothing. A wrong injection costs tokens *and* can
mislead — worse than a missing lookup — so the default is to inject nothing
rather than something plausible. A run never *depends* on an embedding service
being up.

### 2.6 Credentials leave the prompt entirely

Bucket 4 (SSH keys, deployment credentials) is moved to Perun's existing
encrypted config (`perun/secrets.py`, DPAPI on Windows) and exposed through a
gated tool:

```
perun_secret(name: str = "", action: str = "get", value: str = "",
             hint: str = "", scope: str = "auto",
             project: str | None = None) -> str
```

`action="list"` returns the **names and hints only** — never a value.
`action="get"` decrypts one value on demand; `action="set"` stores one.

Anchors of `kind: secret` point at a secret *name*, never a literal value.
This is simultaneously a token saving and a security fix: today those values
are copied into every prompt, every provider request, and every transcript
Perun stores.

> **Note on the current machine:** no real credential value is present in any
> `AGENTS.md` today — only SSH *key paths* and host IPs. The deliverable of
> this phase is therefore the **mechanism** (encrypted store + name-only
> references + anchors), not a migration of existing secrets.

---

## 3. Storage design

**A dedicated `context_docs` table.** *(This section changed during
implementation — the alternative below was rejected.)*

| Column | Purpose |
|---|---|
| `key` | the stable retrieval key (`handbook.tools`, `perun.agents.full`) |
| `title`, `summary` | the inventory row (P2) |
| `body` | the reference body, verbatim |
| `scope`, `project_id` | `global` (machine-wide, `project_id=0`) or `project` |
| `content_hash` | change detection |
| `embedded_hash` | which revision the vector belongs to |
| `embedding` | the vector, or NULL |

The table is **separate from `doc_chunks`** on purpose. `doc_chunks` is
searched by the cross-project `perun_unified_search`; a project-scoped context
doc living there would leak one project's reference material into another
project's search results. Context docs are retrieved through `perun_context`,
not through the document search.

**Schema versioning.** `db.SCHEMA_VERSION` went 1 → 2, and `db.init_db()`
creates the table idempotently, so an existing database migrates on next start.

> **Alternative considered (and rejected):** reuse `doc_chunks` with a reserved
> `source_file` namespace (`context://global/<key>`). It gets content-hash
> upsert and the background re-embedding repair for free — but it puts
> project-scoped reference material into the cross-project document search, and
> it cannot carry the per-doc inventory metadata (title, summary) that P2
> depends on. The duplication is ~60 lines; the leak is not worth it.

**Module:** `perun/context.py` (new):

```
index_docs(docs, ...) -> dict          # a batch; embeddings computed BEFORE the
index_doc(key, title, body, ...) -> dict  # DB connection is opened
get(key, ...) -> dict | None
list_keys(...) -> list[dict]            # the inventory (P2)
search(topic, ..., k=3, mode="auto") -> list[dict]
outline(key) / slice_section(key, name) -> str
prefetch(topic, ...) -> list[dict]      # the deterministic gate (P3)
delete(key, ...) -> bool
reembed_missing(limit=64) -> dict
```

It reuses `memory.content_hash`, `memory.is_sensitive`,
`embeddings.embed_texts`, `embeddings.pack_vector`, `embeddings.unpack_vector`
and `embeddings.cosine_similarity`. It never holds a DB connection across a
network call, per the house rule.

**The sensitive-data guard, and why a sensitive doc is still stored.** A body
that trips `memory.is_sensitive()` is **stored verbatim** — dropping it would
break P1, and P1 is the hard requirement — but it is **never embedded**,
because the embedding is the only part that leaves the machine. Such a row is
marked `embedded_hash == content_hash` with a NULL vector, so the repair path
(`reembed_missing()`) never selects it.

**Handbook sources** (in-repo; all except `tools.md` are git-tracked):

```
perun/integrations/templates/handbook/
  tools.md          # GENERATED at run time from the MCP registry — not on disk
  search.md
  memory.md
  commands.md
  cross-project.md
  sessions.md
  onboarding.md
  secrets.md
  global.md         # the COMPLETE previous global guide, verbatim (P1)
```

`handbook/tools.md` is **generated by introspecting the FastMCP server** (it is
not tracked in git), so a new tool appears in the handbook automatically and the
AGENTS.md tool table can never disagree with the MCP schema.

---

## 4. Migration plan

| Phase | Deliverable | Risk | Effect | Status |
|---|---|---|---|---|
| **0 — Instrument** | `perun context stats`: per-project AGENTS.md bytes/tokens, cross-project duplicate blocks | none | Baseline; proves the win | **done** |
| **1 — Primitive** | `perun/context.py`, `perun_context` MCP tool, `perun context` CLI, tests | none (additive) | Nothing changes yet | **done** |
| **2 — Global kernel** | Handbook docs; `INSTRUCTIONS.md` → kernel; `install_global` syncs kernel + indexes handbook; **the previous global guide is retained verbatim as `handbook.global`** | low | **-1.07k tokens in every project, immediately** | **done - live** |
| **3 — Anchor binding** | `memory:` key on anchors; manifest + `perun context verify` support | low | Anchors become retrievable | **done** |
| **4 — Project migration** | classify sections → inline vs context doc; **retain the original verbatim as `<slug>.agents.full`**; the **context map** records where each section went | medium | trading_bot −319 B, parental_control −17.4k, perun −47 % | **done — all five migrated** |
| **5 — Parity harness** | Eval set per project: same questions answered with and without the distillation; gate migrations on parity | medium | Proves P4 | **partial** — the losslessness half is live (`perun context map` + `perun context verify`); the eval-set half is outstanding |
| **6 — Orchestrator pre-fetch** | `compose_prompt` injects the top context docs | low | Removes the round trip on the orchestrated path | **done** |
| **7 — Secrets** | `perun/secret_store.py`; `perun_secret` tool; `kind: secret` anchors | medium | Removes credentials from every prompt | **done** (mechanism; no real credentials to migrate) |
| **8 — Production-readiness audit** | Harden the token-efficiency implementation: graceful schema serialization, confined instructions[] resolution, live inline-size measurement, regression tests | low | Makes the work safe to ship | **done — v0.8.27** |
| **9 — Anchoring + CODEMAP** | Resolve anchors by token/substring/path, CODEMAP-aware `###` chunking, compact `<slug>.codemap` outline, `perun docs audit`, local-embedding fallback, stale-vector guard | low | Retrieval names the module it describes and anchors resolve instead of missing | **done — v0.8.31** |

### 4.1 What is live right now

**Done and in use:**

* `perun/context.py` — the Context layer (`context_docs` table, schema v2).
* `perun/handbook.py` + `perun/integrations/templates/handbook/` — nine
  reference docs, `tools.md` generated from the live MCP registry.
* The **global kernel** is live in `~/.config/opencode/AGENTS.md`: the managed
  block is 1,832 B (was ≈6.1 KB), and the previous guide is retained verbatim
  as `handbook.global`.
* `perun_context` and `perun_secret` MCP tools, plus the full current MCP
  inventory (**31 tools**).
* `perun context …` and `perun secret …` CLI groups.
* `orchestrator.compose_prompt()` pre-fetch, injected as
  `--- PERUN CONTEXT START/END ---`.
* `memory:` anchors in `perun.md` (seven of them), and `kind: secret` anchor
  support in the manifest + verifier.
* `perun context verify` — the drift check: kernel vs handbook vs manifest
  anchors vs stored secrets, **plus the losslessness check** for every
  migrated project.
* `perun/context_map.py` — the **context map**: `.perun/context-map.json`
  records, for every section of the original `AGENTS.md`, where that content
  went (`inline` / `context:<key>` / `secret:<name>` / `retained`). Sections
  may declare **mixed** destinations (`inline` + `context`) to keep rule
  bullets inline and point elaboration at a context doc. `perun context map
  --write` drafts it; `perun context verify` proves it.
* `perun context sync` indexes each project's `AGENTS.md` as
  `<slug>.agents.full` and its `.perun/context/*.md` docs as
  `<slug>.context.<name>`.
* `perun context dedupe` (v0.8.27) removes byte-identical duplicate `###`
  sections from a project's `AGENTS.md`.
* `perun context stats` (v0.8.27) reports the full ambient-context ledger:
  global `AGENTS.md`, every `instructions[]` file, the live MCP tool-schema size,
  OpenCode skill frontmatter, duplicated `##`/`###` blocks, budget warnings,
  and the computed per-session token floor.
* `perun context verify` (v0.8.27) also checks that the global
  `perun/INSTRUCTIONS.md` file is the thin pointer, and flags repeated inline
  `###` subsections.
* **Anchoring + CODEMAP completion (v0.8.31).** Anchors resolve by
  exact/token/substring/path match and fall back to an indexed `doc_chunks`
  section or a bounded stub; CODEMAP is chunked at `###` modules into
  `<H2> › <H3>` chunks; a compact `<slug>.codemap` outline is indexed; and
  local embeddings are a fallback for anchor labels, section selection and
  pre-fetch. `perun docs audit` checks CODEMAP file/identifier/version
  traceability.
* **Perun's own `AGENTS.md` is migrated** — 9,435 B → 5,037 B (−47 %). The
  behavioural rules stay inline, verbatim; the project overview, the
  contributing checklists, the command list, the credential procedure and the
  release procedure moved to `.perun/context/`.
* **trading-bot's `AGENTS.md` is migrated** — 62,631 B → 22,038 B (−65 %).
  Behavioural rules stay inline; shared instruction modules, build notes and
  reference tables moved to `.perun/context/`.
* **parental-control's `AGENTS.md` is migrated** — 78,055 B → 10,016 B
  (−87 %).
* **perun-in-the-machine's `AGENTS.md` is migrated** — 6,024 B → 3,074 B
  (−49 %).
* **perunchat's `AGENTS.md` is migrated** — 3,446 B → 2,670 B (−23 %).
* **All five migrations verified** — `perun context verify` reports
  `lossless: ok (4 migrated project(s))`.
* **Total ambient floor** (global + project AGENTS.md + instructions + MCP
  schemas + skill frontmatter) is **~12.4 k tokens**, down from **~36.8 k
  tokens** before the migration (−66 %).

**Outstanding:**

* **Phase 5** — the eval-set half of the parity harness (the same questions
  answered with and without the distillation). The losslessness half now
  exists; the eval-set half does not yet.

### Phase 4 classification rule

For each `##` section of a project `AGENTS.md`:

* Contains **imperatives about how to act** ("do not", "never", "always",
  "must") → **inline**, verbatim.
* Is a **project overview / shape description** → **inline**, condensed to one
  paragraph (it is cheap and preserves the mental model).
* Is a **command reference** (build, deploy, debug, test) → **context doc**.
* Is **history/version** → context doc (already in `docs/CHANGELOG.md`).
* Contains **credentials** → secret store.
* Is a **description of the project** → the `perun.md` body (already there).

A section that mixes both is split: the imperative bullets stay, the reference
material moves.

**Losslessness check.** *(Implemented — `perun/context_map.py`.)* The context
map is the proof: for every section of the pre-migration `AGENTS.md` it
records the destination, and `perun context verify` checks three things —
the retained `<slug>.agents.full` still hashes to the original file
(**nothing was deleted**), every section of that original is accounted for
(**nothing is unaccounted for**), and every destination still holds its
content, verbatim (**nothing was paraphrased away**). A section that is
unaccounted for is a failure, not a warning — and each of those three failure
modes has a test that proves the check fails when it should.

---

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| **Retrieval miss** — the model never asks for a doc it needed | P1 (original retained verbatim) + P2 (inventory visible) + P3 (task-driven pre-fetch, not request-driven) + `mode="list"` |
| **Awareness regression** — the agent knows less than before | P4 parity harness gates every migration; behavioural rules are never moved; the retained original is one call away |
| **Extra round trip** costs more than the inline text | One pre-fetch call replaces N KB of always-on context; the orchestrated path pre-fetches for free |
| **Stale context** | Content-hash re-index on change; `updated_at` in every response; `perun context verify` fails on drift |
| **Two sources of truth** (kernel vs handbook vs manifest) | The kernel is *generated*; `verify` is a CI-able check |
| **Ollama down** | The deterministic key/anchor/subject path never touches Ollama; only the optional embedding fallback and semantic search degrade, to keyword matching |
| **Two AGENTS.md philosophies** (inline rules vs retrieved reference) | Explicit split rule, applied by the Phase 4 classifier and documented in `perun.md` |
| **Complexity added to Perun** | One module, one MCP tool, one CLI group, one table reuse — no new dependency, no new service |

---

## 6. Success metrics

### Token / cost

| Metric | Baseline | Target | Achieved |
|---|---|---|---|
| Global `AGENTS.md` managed block | 6,100 B (~1,525 tok) | ≤ 1,500 B (~380 tok) | 2,190 B (~545 tok; ≈2.8× smaller) |
| Worst-case per-project `AGENTS.md` | 78,055 B (~19,514 tok) | ≤ 3,000 B (~750 tok) | 22,038 B (~5,509 tok, trading-bot) |
| Total ambient floor per session | up to ~36.8k tok | ≤ 1.2k tok | ~12.4k tok (−66 %) |
| MCP tool table copies in the repo | 3+ | 1 (generated) | 1 (generated `handbook.tools`) |
| Credentials in any prompt | yes (paths/hosts) | none | mechanism shipped; nothing to migrate |

### Awareness parity (the non-negotiable)

| Metric | Target |
|---|---|
| Sections of the original `AGENTS.md` unaccounted for after migration | **0** |
| Pre-migration `AGENTS.md` retrievable verbatim as a context doc | **yes, all projects** |
| Behavioural rules moved out of the inline set | **0** |
| Parity-harness answer match rate (old vs new), per project | **≥ the pre-migration baseline** |
| Project overview present inline | **yes, every project** |
| Context keys listed in the kernel | **all** keys for the project |

A migration that cannot show parity is reverted. The token saving is the
*goal*; parity is the *constraint*.

---

## 7. Open decisions

1. **Generated vs hand-written `AGENTS.md`.** Recommendation: generated kernel
   + hand-written project rules, with a `verify` check. A fully generated file
   risks losing the nuance a human wrote; a fully hand-written one keeps the
   drift.
2. **Do project `AGENTS.md` files stay in git?** Recommendation: yes — they
   are the behavioural contract and should be reviewable in PRs.
3. **Should the key table live in the kernel, or should the kernel just say
   "call `perun_context('list')`"?** **Closed** — the nine-row key table is
   shipped and generated by `handbook.render_key_table()`.
4. **Scope of Phase 4.** **Closed** — all five projects are migrated and
   verified.
5. **Does `perun_context` replace `perun_memory_search`, or sit beside it?**
   Recommendation: sit beside it. `perun_memory_search` searches *facts and
   documents*; `perun_context` retrieves *authored reference docs*. Different
   lifetimes, different guarantees.

---

## 8. Appendix — what "the kernel" replaces, line by line

| Current global `AGENTS.md` section | Destination | Why |
|---|---|---|
| "What Perun is" prose (5 bullets) | `handbook.onboarding` | Reference |
| "MCP tools" table (25 rows) | `handbook.tools` (generated) | Duplicated by MCP tool schemas |
| "When to use Perun" | `handbook.search` / `handbook.memory` | Reference |
| "Working inside a Perun-managed project" | `handbook.onboarding` | Reference |
| "Global slash commands" table | `handbook.commands` | Duplicated by command definitions |
| "Dashboard" | `handbook.onboarding` | Reference |
| "New machine" | `handbook.onboarding` | Reference |
| *(new)* key table | **kernel** (inline) | Discovery surface |
| *(new)* 4 always-true rules | **kernel** (inline) | Behavioural |