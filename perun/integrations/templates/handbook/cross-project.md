# Cross-project references

## The rule

**Always resolve a cross-project path with `perun.project_roots.resolve()`.**
Never guess, never hand-craft, never build one from a relative path.

```python
from perun.project_roots import resolve

api_path = resolve("perun_chat", "perunchat/api.py")   # absolute, validated
```

## Why it matters

A hand-crafted path like `..\perun_chat\perunchat\api.py` or
`.\my_apps\perun\AGENTS.md` is resolved **relative to the current working
directory**. Change the directory — a different project, a subdirectory, a
test runner — and the same string silently points somewhere else, or at
nothing. It reads as correct, so the failure is not obvious: you edit the
wrong file, or read a stale copy and conclude the code does not exist.

`resolve()` fails loudly instead: it returns an absolute path and raises if
the result would escape the registered root.

## The registry

```bash
python -m perun project-roots --list
python -m perun project-roots --add my_project C:\my_apps\my_project
python -m perun project-roots --remove my_project
```

Resolution order for a root name:

1. the `PERUN_PROJECT_<NAME>` environment variable,
2. `<PERUN_HOME>/project-roots.json`,
3. the built-in defaults (`perun`, `perun_chat`, `perun_in_the_machine`).

Roots are also **auto-populated** when projects are discovered, so a sibling
repository becomes addressable as soon as Perun sees it — you rarely need to
add one by hand.

## Reading another project's files

The MCP tools cover the common cases (`perun_grep`, `perun_global_grep`,
`perun_get_manifest`, `perun_memory_search` with `all_projects`), and they
already know the roots. Reach for `resolve()` when you genuinely need to
touch a file in another repository.

## Sibling projects on this machine

| Project | Root name | Path |
|---|---|---|
| Perun | `perun` | `C:\my_apps\perun` |
| PerunChat | `perun_chat` | `C:\my_apps\perun_chat` |
| Perun In The Machine | `perun_in_the_machine` | `C:\my_apps\perun_in_the_machine` |

PerunChat's gateway, SDK and dashboard live in `perun_chat`; the live
deployment, the company website and the PerunChat public site live in
`perun_in_the_machine`. The two must be kept in sync — a dashboard route
added in one has to exist in the other.
