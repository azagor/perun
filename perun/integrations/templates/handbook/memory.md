# Memory protocol

Perun has one memory system with two kinds of content, plus a third store for
authored reference material.

| Store | What it holds | Lifetime | Retrieved with |
|---|---|---|---|
| **Facts** (`memory` table) | atomic statements *you* wrote | until pruned | `perun_memory_search` |
| **Doc chunks** (`doc_chunks`) | chunks mirrored from a project's `docs/*.md`, `README.md`, `AGENTS.md` | re-indexed from disk | `perun_memory_search` |
| **Context docs** (`context_docs`) | authored reference material with a stable key | re-indexed from source | `perun_context` |

The three are different things with different guarantees. A **fact** is a
decision someone made. A **doc chunk** is a mirror of a file. A **context
doc** is curated reference text addressed by name.

## Facts

```
perun_memory_store(content: str, project: str | None = None,
                   global_scope: bool = False, topic: str = "general")
```

* One atomic statement per call. "The Telegram bridge uses a single poller
  guarded by `telegram.PidLock`" is a fact. "Here is how the bridge works" is
  a document.
* **Scope matters.** Omit `project` and the fact lands in the project
  resolved from your working directory. Pass `global_scope=True` to make it
  visible to every project. Global facts are "rules for ever": they still
  rank below fresher ones, but decay with a 365-day half-life, so they
  effectively never disappear from results.
* **Freshness ranks, it does not filter.** A fact from a year ago is still
  findable; it just sorts below a fresh one.
* Identical content is deduplicated — storing the same fact twice returns
  `duplicate` rather than a second row.

### The sensitive-data guard

`remember()` refuses anything that looks like a credential: private keys,
`api_key=…`, `token: …`, JWTs, AWS keys, GitHub PATs, Slack/Stripe/Anthropic
keys, and connection strings with inline passwords. The guard applies to
doc chunking too — a secret-bearing chunk is neither stored nor embedded, so
it cannot leave the machine through the embedding call.

**If you need to store a real secret, use the encrypted secret store, not
memory.** See `perun_secret`.

## Doc chunks

```
perun_docs_index(project: str | None = None)
```

Indexes a project's standard doc set: `docs/CODEMAP.md`,
`docs/TECHNICAL_DOCUMENTATION.md`, `docs/CHANGELOG.md`, `docs/README.md`,
`AGENTS.md`, `README.md`. Markdown is split at level-2 headings, oversized
sections are split further so no chunk exceeds 8000 characters, and the index
always mirrors the file — renamed or removed headings are deleted, not
orphaned.

## Searching

```
perun_memory_search(query: str, project: str | None = None,
                     k: int = 5, all_projects: bool = False)
```

Hybrid ranking: cosine similarity against the embedding, plus a bonus for
substring and keyword hits, multiplied by the freshness weight. When Ollama
is down, rows are stored without an embedding and the same query falls back
to keyword + recency ranking — degraded, never broken.

## When to store a fact

Store a fact after an **architectural, deployment, or security decision** —
the kind of thing that would be expensive to rediscover and is not obvious
from the code. Do not store what the code already says, and do not store
reference material: that belongs in a context doc (see `perun_context`).

Use `/logwork <one-line summary>` for the quick version: called inside a
project it stores a project-scoped fact; called from the orchestrator it
stores a global one.
