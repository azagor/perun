"""Git integration — one repository (and remote) per project.

Perun never rewrites history or discards work. It offers only the safe,
explicit operations a dashboard needs: inspect status, initialise a repository,
configure the per-project remote, commit the working tree, push a branch, and
read the log. Everything runs through the ``git`` CLI so it uses whatever
credentials/SSH keys the machine already has.

Interactive credential prompts are disabled (``GIT_TERMINAL_PROMPT=0`` etc.) so
a private remote without a stored credential fails fast instead of hanging a
server worker thread.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Remotes we accept. Anything starting with a dash is rejected to prevent
# option injection; local paths are accepted so "point at a folder/nfs/unc".
_REMOTE_RE = re.compile(r"^(https?://|ssh://|git://|file://|git@[^:\s]+:)\S+$", re.I)
_LOCAL_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\|\.{1,2}[\\/])")
_FIELD = "\x1f"  # unit separator for log parsing


class GitError(Exception):
    """An expected git failure whose message is safe to show the user."""


def available() -> bool:
    return shutil.which("git") is not None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env.setdefault("GIT_ASKPASS", "echo")
    env.setdefault("SSH_ASKPASS", "echo")
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _clean(text: str | None) -> str:
    return (text or "").strip()


def _run(path: str | Path, args: list[str], *, timeout: int = 180,
         check: bool = False) -> subprocess.CompletedProcess:
    if not available():
        raise GitError("git is not installed or not on PATH")
    argv = ["git", "-C", str(path), *args]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, env=_env(),
                              creationflags=_creationflags())
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0] if args else ''} timed out after {timeout}s")
    except OSError as exc:
        raise GitError(f"could not run git: {exc}")
    if check and proc.returncode != 0:
        raise GitError(_clean(proc.stderr) or _clean(proc.stdout)
                       or f"git {args[0] if args else ''} failed")
    return proc


def _run_plain(args: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    if not available():
        raise GitError("git is not installed or not on PATH")
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env=_env(), creationflags=_creationflags())
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0] if args else ''} timed out after {timeout}s")


def validate_remote(url: str) -> str:
    """Return a normalised, safe remote URL or raise :class:`GitError`."""
    u = (url or "").strip()
    if not u:
        raise GitError("remote URL is required")
    if any(c in u for c in "\r\n\t") or u.startswith("-"):
        raise GitError("invalid remote URL")
    if _REMOTE_RE.match(u) or _LOCAL_RE.match(u):
        return u
    raise GitError("unsupported remote URL — use https://, ssh://, git@host:path, "
                   "file://, or a local path")


def is_repo(path: str | Path) -> bool:
    if not available():
        return False
    p = Path(path)
    if not p.is_dir():
        return False
    return _run(p, ["rev-parse", "--is-inside-work-tree"]).returncode == 0


def _parse_branch(head_line: str) -> tuple[str, int, int]:
    """Parse ``## main...origin/main [ahead 1, behind 2]``."""
    rest = head_line[3:].strip() if head_line.startswith("## ") else head_line.strip()
    ahead = behind = 0
    if "[" in rest:
        status = rest[rest.find("["):]
        m = re.search(r"ahead (\d+)", status)
        if m:
            ahead = int(m.group(1))
        m = re.search(r"behind (\d+)", status)
        if m:
            behind = int(m.group(1))
        rest = rest[:rest.find("[")].strip()
    if rest.startswith("No commits yet on "):
        return rest[len("No commits yet on "):].strip(), ahead, behind
    if rest.startswith("Initial commit on "):
        return rest[len("Initial commit on "):].strip(), ahead, behind
    branch = rest.split("...")[0].split(" ")[0].strip()
    return branch or "HEAD", ahead, behind


def info(path: str | Path) -> dict[str, Any]:
    """A dashboard-friendly snapshot of the repository state."""
    p = Path(path)
    out: dict[str, Any] = {
        "ok": True, "available": available(), "path": str(p),
        "exists": p.is_dir(), "repo": False,
    }
    if not out["available"] or not out["exists"]:
        return out
    if not is_repo(p):
        return out
    out["repo"] = True

    st = _run(p, ["status", "--porcelain=v1", "-b"])
    lines = st.stdout.splitlines()
    head = lines[0] if lines else "## "
    branch, ahead, behind = _parse_branch(head)
    changes = [ln for ln in lines[1:] if ln.strip()]
    out.update({
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "dirty": bool(changes),
        "change_count": len(changes),
        "changes": changes[:200],
    })

    remotes = _clean(_run(p, ["remote"]).stdout).split()
    out["remotes"] = remotes
    out["has_remote"] = "origin" in remotes
    if out["has_remote"]:
        out["remote_url"] = _clean(_run(p, ["remote", "get-url", "origin"]).stdout)

    out["has_commits"] = _run(p, ["rev-parse", "--verify", "HEAD"]).returncode == 0
    if out["has_commits"]:
        fmt = f"%h{_FIELD}%an{_FIELD}%ar{_FIELD}%s"
        last = _clean(_run(p, ["log", "-1", f"--pretty=format:{fmt}"]).stdout)
        parts = last.split(_FIELD)
        if len(parts) >= 4:
            out["last_commit"] = {"sha": parts[0], "author": parts[1],
                                  "when": parts[2], "subject": parts[3]}
    return out


def init(path: str | Path, branch: str = "main") -> dict[str, Any]:
    p = Path(path)
    if not p.is_dir():
        raise GitError(f"path does not exist: {p}")
    if is_repo(p):
        raise GitError("already a git repository")
    branch = (branch or "main").strip() or "main"
    proc = _run(p, ["init", "-b", branch])
    if proc.returncode != 0:
        # Older git without `-b`.
        _run(p, ["init"], check=True)
        _run(p, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=True)
    return info(p)


def set_remote(path: str | Path, url: str, branch: str = "") -> dict[str, Any]:
    p = Path(path)
    if not is_repo(p):
        raise GitError("not a git repository — initialise it first")
    clean_url = validate_remote(url)
    remotes = _clean(_run(p, ["remote"]).stdout).split()
    if "origin" in remotes:
        _run(p, ["remote", "set-url", "origin", clean_url], check=True)
    else:
        _run(p, ["remote", "add", "origin", clean_url], check=True)
    # If no commits yet and a branch was requested, point HEAD at it.
    if branch and not _run(p, ["rev-parse", "--verify", "HEAD"]).returncode == 0:
        _run(p, ["symbolic-ref", "HEAD", f"refs/heads/{branch}"], check=False)
    return info(p)


def commit(path: str | Path, message: str) -> dict[str, Any]:
    p = Path(path)
    if not is_repo(p):
        raise GitError("not a git repository — initialise it first")
    _run(p, ["add", "-A"], check=True)
    if _run(p, ["diff", "--cached", "--quiet"]).returncode == 0:
        return {"ok": True, "committed": False, "reason": "nothing to commit",
                "git": info(p)}
    msg = (message or "").strip() or "Perun checkpoint"
    extra: list[str] = []
    if not _clean(_run(p, ["config", "user.name"]).stdout):
        extra += ["-c", "user.name=Perun"]
    if not _clean(_run(p, ["config", "user.email"]).stdout):
        extra += ["-c", "user.email=perun@localhost"]
    proc = _run(p, [*extra, "commit", "-m", msg])
    if proc.returncode != 0:
        raise GitError(_clean(proc.stderr) or _clean(proc.stdout)
                       or "git commit failed")
    sha = _clean(_run(p, ["rev-parse", "--short", "HEAD"]).stdout)
    return {"ok": True, "committed": True, "sha": sha, "git": info(p)}


def push(path: str | Path, branch: str = "", remote: str = "origin",
         set_upstream: bool = True) -> dict[str, Any]:
    p = Path(path)
    if not is_repo(p):
        raise GitError("not a git repository — initialise it first")
    remotes = _clean(_run(p, ["remote"]).stdout).split()
    if not remotes:
        raise GitError("no remote configured — set one first")
    # The remote must be one of the names git actually knows: a value like
    # ``--receive-pack=...`` would otherwise be consumed as a *push option* and
    # execute an arbitrary command on the receiving side.
    remote = (remote or "origin").strip()
    if remote not in remotes:
        raise GitError(f"unknown remote: {remote!r} (configured: {', '.join(remotes)})")
    if not _run(p, ["rev-parse", "--verify", "HEAD"]).returncode == 0:
        raise GitError("nothing to push — commit first")
    if not branch:
        branch = _clean(_run(p, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout) or "main"
    branch = branch.strip()
    # Reject option-like and refspec-like branch names (``:`` creates/updates a
    # *different* remote ref than the one shown to the user).
    if (not branch or branch.startswith("-")
            or any(c in branch for c in "\r\n\t :")):
        raise GitError("invalid branch name")
    args = ["push"]
    if set_upstream:
        args.append("-u")
    # ``--`` ends option parsing so neither argument can be taken as a flag.
    args += ["--", remote, branch]
    proc = _run(p, args, timeout=600)
    if proc.returncode != 0:
        raise GitError(_clean(proc.stderr) or _clean(proc.stdout) or "git push failed")
    return {"ok": True, "pushed": True, "branch": branch, "remote": remote,
            "output": _clean(proc.stdout), "git": info(p)}


def log(path: str | Path, limit: int = 30) -> list[dict[str, Any]]:
    p = Path(path)
    if not is_repo(p) or _run(p, ["rev-parse", "--verify", "HEAD"]).returncode != 0:
        return []
    fmt = f"%h{_FIELD}%an{_FIELD}%ar{_FIELD}%s"
    out = _run(p, ["log", f"-n{max(1, min(int(limit), 200))}",
                   f"--pretty=format:{fmt}"]).stdout
    commits = []
    for line in out.splitlines():
        parts = line.split(_FIELD)
        if len(parts) >= 4:
            commits.append({"sha": parts[0], "author": parts[1],
                            "when": parts[2], "subject": parts[3]})
    return commits


def clone(url: str, dest: str | Path) -> dict[str, Any]:
    clean_url = validate_remote(url)
    target = Path(dest).expanduser()
    if target.exists() and any(target.iterdir()):
        raise GitError(f"destination is not empty: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = _run_plain(["clone", clean_url, str(target)])
    if proc.returncode != 0:
        raise GitError(_clean(proc.stderr) or _clean(proc.stdout) or "git clone failed")
    return info(target)
