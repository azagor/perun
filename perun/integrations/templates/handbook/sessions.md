# Agent runs

Perun starts headless OpenCode sessions (`opencode run`) in any registered
project and streams the result back. This is how the dashboard console, the
Telegram bridge and the PerunChat bridge all work.

## Starting a run

```
perun_start_session(prompt: str, project: str | None = None,
                    model: str = "", agent: str = "")
```

Returns a **run id** immediately; the run continues in the background.

```
perun_run_status(run_id: str)
```

Returns the current status and the output captured so far. Status is one of
`queued`, `running`, `completed`, `failed`, `cancelled`, `timeout`.

```
perun_ask(prompt: str, project: str | None = None,
          model: str = "", agent: str = "", timeout: float = 0)
```

Same thing, but **waits** for the reply. Use it when you want the answer
now; use `perun_start_session` when the work continues and you want to
observe it later.

## Model and agent resolution

Precedence, highest first:

1. the explicit argument,
2. the project's own override (`perun.md` `model:` / `agent:` front matter,
   or the dashboard's per-project setting),
3. for the control plane only: `orchestrator_model` / `orchestrator_agent`,
4. for the control plane only: the global `default_model` / `default_agent`.

**The global default is never forced onto an ordinary project run.** With no
override, a project run defers to OpenCode's own default model. This keeps
the per-project setting meaningful and avoids silently re-routing work
through a model the user did not choose.

An explicit model is recorded as `model_explicit` and forwarded as `--model`
on the command line. An *empty* resolution is not forwarded — the run then
uses whatever OpenCode would have used anyway.

## Agents

```
perun_list_projects / perun_project_info
```

The agent list for a project is discoverable through the dashboard
(`/api/agents`) and the OpenCode agent discovery layer: the built-ins
(`build`, `plan`, `general`, `explore`), the global ones in
`~/.config/opencode/agents/`, and the project-local ones in
`.opencode/agents/`.

## Streaming and cancellation

The server streams run output over SSE (`/api/runs/{uid}/stream`), parsing
each JSON frame from `opencode run --format json`. Output is preserved even
when a run is cancelled or times out: the final buffered lines are harvested
before the process is stopped, so a partial answer is never lost.

Cancellation is idempotent and also covers queued runs. Per-chat in-flight
runs are tracked separately, so a `/stop` from Telegram only stops that
caller's run.

## Bounds

* `max_concurrent_runs` caps parallelism (a mutable limiter, adjustable at
  runtime).
* `run_timeout` bounds a single run's wall clock.
* A watchdog thread enforces the timeout independently of the process.
* Secret environment variables are stripped from every spawned child, and
  `PERUN_DEPTH` bounds recursive agent spawning.

## Instruction injection

On a **new** session, Perun prepends the global and project instruction
files to the prompt (`orchestrator.compose_prompt`). This is why an
orchestrated run has more context than a bare `opencode run` — and why the
same material does not need to be duplicated into every `AGENTS.md`.
