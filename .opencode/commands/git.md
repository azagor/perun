---
description: Safe git workflow for the Perun project. Use for status checks, commits, pushes, and lightweight history inspection while respecting AGENTS.md constraints.
---

# Perun Git Assistant

Project-specific git helper for `C:\my_apps\perun`. This command is **read-only by default** and avoids destructive operations.

## When to use

- User asks to commit current changes.
- User asks to push to GitHub.
- User wants to see what changed (`git status`, `git diff`, `git log`).
- User asks about branches or wants to create a feature branch.
- User asks to pull or sync — prefer safe fetch/merge, never force.

## Safety rules (read AGENTS.md first)

- **Never run destructive git mutations unless explicitly asked.** Forbidden by default:
  - `git reset --hard`
  - `git rebase -i`, `git rebase --hard`
  - `git push -f` / `git push --force`
  - `git clean -fd`
  - `git checkout -f`
  - `git branch -D` on unmerged branches
- **No force-push to `master`.** `master` is the protected main branch on `https://github.com/azagor/perun`.
- **Always run checks before committing.** If the working tree contains code changes, run:
  - `python -c "import py_compile,glob; [py_compile.compile(f,doraise=True) for f in glob.glob('perun/**/*.py',recursive=True)+glob.glob('mcp/*.py')]"`
  - `node --check web/js/perun.js`
  - `python -m pytest tests/ -q`
  - If CSS changed, verify brace balance.
- **Update documentation for notable changes.** Any change that affects public endpoints, the DB schema, the dashboard UI, or behavior must update:
  - `docs/CHANGELOG.md` (single source of truth for release notes)
  - `docs/CODEMAP.md` if new files or identifiers were added
  - `docs/TECHNICAL_DOCUMENTATION.md` if endpoints or commands changed
  - `AGENTS.md` if agent-facing constraints or commands changed

## Standard workflows

### 1. Inspect current state

Always start here unless the user already told you what they want.

```bash
git status --short
git diff --stat
git log --oneline -5
```

### 2. Commit current changes

1. Run the checks above.
2. Review `docs/CHANGELOG.md`; add an entry if the change is user-visible or architectural.
3. Stage and commit:

```bash
git add -A
git commit -m "<concise imperative summary>

<body if needed>"
```

4. If the user asked to push, run:

```bash
git push origin master
```

### 3. Push an existing commit

```bash
git status --short
git log --oneline -3
git push origin master
```

If `master` has moved ahead, do **not** force-push. Instead:

```bash
git fetch origin
git log --oneline HEAD..origin/master  # incoming commits
git merge origin/master --no-edit      # or rebase if user explicitly asks
# re-run checks after merge/rebase
python -m pytest tests/ -q
git push origin master
```

### 4. Create a feature branch

```bash
git checkout -b feature/<short-name>
# ... make changes, update docs, run checks ...
git add -A
git commit -m "feature/<short-name>: summary"
git push -u origin feature/<short-name>
```

Do not merge the branch yourself unless the user explicitly asks. Prefer opening/mentioning a PR.

### 5. Inspect history

```bash
git log --oneline --graph -15
git log --oneline --all --graph -15  # include remote branches
```

## What to avoid

- Do not suggest `git stash` unless the user explicitly wants to set aside work.
- Do not rewrite published history on `master`.
- Do not commit large binary files, secrets, or personal paths (e.g., `C:\Users\...`).
- Do not update `perun.md` manifests or `opencode.json` unless the user asks.

## Report format

After any git operation, return a concise summary:

- **Commands run**
- **Files changed** (from `git status --short` or `git diff --stat`)
- **Checks passed/failed**
- **Remaining work** (e.g., push needed, conflicts, docs missing)
