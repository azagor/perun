"""Integrated code-search layer for Perun.

Perun bundles three levels of search so agents never need to ask the user to
install extra MCP servers:

1. **perun_ast_grep** — structural/AST search via the ``ast-grep`` CLI when
   available, with a Python-AST fallback.
2. **perun_grep** — fast regex content search via ``ripgrep`` when available,
   with a pure-Python fallback.
3. The OpenCode built-in ``grep`` tool is explicitly the last resort.

Results are normalised to a simple JSON shape so callers (MCP tools, HTTP
endpoints, or other Perun modules) can consume them uniformly.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Iterator


def _creationflags() -> int:
    """Hide console windows for background search subprocesses on Windows."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


MAX_RESULTS = 200
MAX_LINE_LEN = 160
# The Python fallback reads files whole/line-by-line; rg streams and needs no
# cap (it must keep up with e.g. 50 MB logs). 8 MB keeps the fallback useful
# while bounding worst-case memory.
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RG_BYTES = 16 * 1024 * 1024
MAX_PATTERN_LEN = 512
# rg -g exclusions shared with the Python fallback's SKIP_DIRS so both
# engines return the same result set (audit 2026-10-18, d02/d03/d06).
_RG_IGNORE_GLOBS = [
    "-g", "!node_modules/", "-g", "!venv/", "-g", "!__pycache__/",
    "-g", "!.venv/", "-g", "!.git/", "-g", "!.perun/",
]
# Directories both engines always skip.
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".perun"}


def _is_windows() -> bool:
    return os.name == "nt"


def _which(name: str) -> str | None:
    return shutil.which(name)


def _is_binary(path: Path) -> bool:
    """Heuristic binary check: a NUL byte in the first 8 KB.

    Deliberately NOT a control-byte range check — UTF-8 continuation bytes
    (e.g. 0x82 in „ł") live in \\x7f-\\x9f and would mark every non-English
    text file as binary. ripgrep uses the same NUL heuristic.
    """
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(8192)
    except Exception:
        return True
    return b"\x00" in chunk


def _glob_to_regex(glob: str) -> str:
    """Translate simple glob patterns to a Python regex for filename matching.

    Handles ``*``, ``**`` (treated as ``*``), and ``?``.
    """
    parts = []
    i = 0
    n = len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":
                parts.append(".*")
                i += 2
                if i < n and glob[i] == "/":
                    i += 1
            else:
                parts.append(r"[^/\\]*")
                i += 1
        elif c == "?":
            parts.append(".")
            i += 1
        elif c == ".":
            parts.append(r"\.")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1
    return "".join(parts)


def _safe_compile_regex(pattern: str, flags: int = 0) -> re.Pattern | None:
    """Compile a regex, returning None on error without logging."""
    try:
        return re.compile(pattern, flags)
    except re.error:
        return None


def _resolve_abs(path: str | Path) -> Path:
    """Safe resolve that catches OSError/ValueError (e.g. embedded NUL)."""
    try:
        return Path(path).resolve()
    except (OSError, ValueError):
        try:
            return Path(path).absolute()
        except (OSError, ValueError):
            return Path(".")


def _sanitize_root(root: str | Path) -> Path | None:
    """Normalise a search root.

    ``..`` components are *dropped*, not resolved: a root of ``proj/..`` must
    not widen the search beyond the directory it points at (audit 2026-10-18,
    a01/a02).  Returns ``None`` for unusable paths (embedded NUL, ..) so the
    caller can report ``invalid_input`` instead of crashing.
    """
    try:
        raw = str(root)
        if "\x00" in raw:
            return None
        parts = Path(raw).parts
        kept = tuple(p for p in parts if p != "..") or (".",)
        return Path(*kept).resolve()
    except (OSError, ValueError):
        return None


def _validate_pattern(pattern: str) -> str | None:
    """Return an error reason when *pattern* is unusable by both engines."""
    if not isinstance(pattern, str) or not pattern.strip():
        return "empty pattern"
    if "\x00" in pattern:
        return "pattern contains a NUL byte"
    if len(pattern) > MAX_PATTERN_LEN:
        return f"pattern too long ({len(pattern)} > {MAX_PATTERN_LEN} chars)"
    return None


def _looks_catastrophic(pattern: str) -> bool:
    """Detect the classic ReDoS shape: a quantifier applied to a group whose
    body contains another quantifier — ``(a+)+``, ``((a+)+)*``, ``(\\d+)*``.

    Python's backtracking engine takes exponential time on such patterns
    (ripgrep's automaton does not), so the pure-Python fallback refuses them
    instead of hanging (audit 2026-10-18, b01).
    """

    def _parse(i: int) -> tuple[int, bool, bool]:
        """Parse a sequence starting at *i*.

        Returns ``(next_index, has_quantifier, is_catastrophic)``.
        Stops at an unmatched closing parenthesis or end of string.
        """
        has_qt = False
        while i < len(pattern):
            c = pattern[i]
            if c == "\\":
                i += 2
                continue
            if c == "[":
                j = i + 1
                while j < len(pattern) and pattern[j] != "]":
                    if pattern[j] == "\\":
                        j += 1
                    j += 1
                i = j + 1
                continue
            if c == "(":
                j, body_has_qt, body_cat = _parse(i + 1)
                if j >= len(pattern) or pattern[j] != ")":
                    return j, has_qt, False
                i = j + 1
                group_quantified = i < len(pattern) and pattern[i] in "*+{"
                if group_quantified:
                    i += 1
                    has_qt = True
                    if body_has_qt:
                        return i, True, True
                else:
                    if body_has_qt:
                        has_qt = True
                if body_cat:
                    return i, True, True
                continue
            if c == ")":
                return i, has_qt, False
            if c in "*+":
                has_qt = True
            i += 1
        return i, has_qt, False

    _, _, catastrophic = _parse(0)
    return catastrophic


# --------------------------------------------------------------------------- #
# .gitignore support for the Python fallback (so both engines agree)
# --------------------------------------------------------------------------- #
def _glob_match_simple(pat: str, text: str) -> bool:
    """gitignore-style glob: ``*`` stays inside a path segment, ``**`` spans."""
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < n and pat[i] == "/":
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                out.append(pat[i:j + 1])
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    try:
        return re.fullmatch("".join(out), text) is not None
    except re.error:  # pragma: no cover - defensive (odd character classes)
        return False


def _gitignore_rule_matches(rule: str, rel: str) -> bool:
    """Does a single (already de-negated) gitignore rule match *rel*?"""
    is_dir_rule = rule.endswith("/")
    rule = rule.rstrip("/")
    if not rule:
        return False
    anchored = rule.startswith("/") or "/" in rule
    rule = rule.lstrip("/")
    if anchored:
        if _glob_match_simple(rule, rel):
            return True
        return bool(is_dir_rule) and (rel == rule or rel.startswith(rule + "/"))
    return any(_glob_match_simple(rule, seg) for seg in rel.split("/"))


def _gitignored(rel: str, rules: list[tuple[str, str]]) -> bool:
    """Ordered-rule evaluation; the last matching rule wins (incl. ``!``)."""
    rel = rel.replace("\\", "/").strip("/")
    ignored = False
    for base, rule in rules:
        if base:
            if not (rel == base or rel.startswith(base + "/")):
                continue
            sub = rel[len(base):].lstrip("/")
        else:
            sub = rel
        negated = rule.startswith("!")
        pat = rule[1:] if negated else rule
        if not pat:
            continue
        if _gitignore_rule_matches(pat, sub):
            ignored = not negated
    return ignored


def _read_gitignore(path: Path) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return [line.strip() for line in fh
                    if line.strip() and not line.lstrip().startswith("#")]
    except OSError:
        return []


def _run(argv: list[str], cwd: str | Path | None = None,
         timeout: float = 30.0) -> tuple[bool, str, int]:
    """Run a subprocess, stream stdout line-by-line, and return (ok, stdout_or_stderr, returncode).

    ``ok`` is True when the process exits with code 0.  Reading stops after
    ``MAX_TOTAL_RG_BYTES`` to protect against runaway output.
    """
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_creationflags(),
        )
    except FileNotFoundError:
        return False, f"command not found: {argv[0]}", -1
    except Exception as exc:  # pragma: no cover - defensive
        return False, str(exc), -1

    ok, lines, returncode = _run_streamed(
        proc, timeout=timeout, max_bytes=MAX_TOTAL_RG_BYTES,
    )
    stdout = "".join(lines)
    if not ok:
        stderr = ""
        if proc.stderr is not None:
            try:
                stderr = proc.stderr.read()
            except Exception:
                stderr = ""
            try:
                proc.stderr.close()
            except Exception:
                pass
        return False, stdout or stderr, returncode
    if proc.stderr is not None:
        try:
            proc.stderr.close()
        except Exception:
            pass
    return True, stdout, returncode


def _run_streamed(proc: subprocess.Popen, *, timeout: float,
                  max_bytes: int) -> tuple[bool, list[str], int]:
    """Stream stdout line by line, stop after *max_bytes*, and wait for exit.

    Returns ``(ok, lines, returncode)``.  ``ok`` is True when the process was not
    killed by us and exited successfully.
    """
    lines: list[str] = []
    total_bytes = 0
    killed = False
    try:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            total_bytes += len(line.encode("utf-8", errors="replace"))
            lines.append(line)
            if total_bytes >= max_bytes:
                proc.kill()
                killed = True
                break
    finally:
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                returncode = proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:  # pragma: no cover
                returncode = -1
            killed = True
        # Close stdout, which this helper consumed.  Leave stderr open so the
        # caller can read error messages if desired.
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass

    if killed:
        return False, lines, returncode
    return returncode == 0, lines, returncode


def _truncate_field(value: str, max_len: int = MAX_LINE_LEN) -> str:
    """Return a token-efficient copy of *value*, adding ``…`` when truncated."""
    if not isinstance(value, str):
        value = str(value)
    value = value.rstrip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


def _dedupe_matches(matches: list[dict[str, Any]], max_total: int = 20,
                    max_text_len: int = MAX_LINE_LEN) -> list[dict[str, Any]]:
    """Deduplicate and truncate a list of matches.

    Matches are keyed by ``(project, file, line, text)`` where ``project`` may be
    absent.  The returned list contains at most *max_total* items, and each
    ``text``/``match`` field is truncated to *max_text_len*.
    """
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for m in matches:
        key = (m.get("project"), m.get("file"), m.get("line"),
               m.get("text"), m.get("match"))
        if key in seen:
            continue
        seen.add(key)
        truncated = dict(m)
        truncated["text"] = _truncate_field(m.get("text", ""), max_text_len)
        truncated["match"] = _truncate_field(m.get("match", ""), max_text_len)
        out.append(truncated)
        if len(out) >= max_total:
            break
    return out


# --------------------------------------------------------------------------- #
# Path-list helpers
# --------------------------------------------------------------------------- #
def _is_project_path_safe(project: dict[str, Any],
                          configured_roots: list[str] | None = None) -> bool:
    """Check whether a project path lives under one of the configured roots.

    Fail-closed: a missing/empty root list (or an unusable path) refuses the
    path — with no roots configured there is nothing to be safe *within*, so
    "everything is allowed" would turn the gate into a no-op (audit 2026-10-18,
    a04).
    """
    if configured_roots is None:
        try:
            from perun.config import settings
            configured_roots = settings().roots
        except Exception:
            configured_roots = []
    ppath = _project_path(project)
    if not ppath or "\x00" in str(ppath):
        return False
    target = _resolve_abs(ppath)
    if not configured_roots:
        return False
    for root in configured_roots:
        try:
            root_path = _resolve_abs(root)
            if target == root_path or root_path in target.parents:
                return True
        except Exception:
            continue
    return False


# --------------------------------------------------------------------------- #
# ripgrep / fallback grep
# --------------------------------------------------------------------------- #
def rg_available() -> bool:
    return _which("rg") is not None


def _python_grep(pattern: str, root: Path, *, case_sensitive: bool = True,
                 whole_word: bool = False, include: str | None = None,
                 max_results: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """Pure-Python regex fallback when ripgrep is not installed."""
    if max_results <= 0:
        return []
    flags = 0 if case_sensitive else re.IGNORECASE
    if whole_word:
        # Group the WHOLE pattern so alternation stays inside the boundary
        # (\bfoo|bar\b would let "foobar" match — audit b09).
        pattern = r"\b(?:" + pattern + r")\b"
    regex = _safe_compile_regex(pattern, flags)
    if regex is None:
        return []

    # include is a glob first (ripgrep -g semantics). The regex reading of the
    # file name is only a fallback for patterns that match nothing as a glob,
    # so "a.py" cannot match "axpy" (audit b10) while the legacy ".*\\.py"
    # regex form keeps working.
    include_glob: str | None = None
    include_re: re.Pattern | None = None
    if include:
        include_glob = include
        include_re = _safe_compile_regex(include, flags)
        if include_re is None:
            include_re = _safe_compile_regex(_glob_to_regex(include), flags)

    results: list[dict[str, Any]] = []
    gitignore_rules: list[tuple[str, str]] = []
    root_abs = _resolve_abs(root)
    candidates: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        # Collect this directory's .gitignore so nested files/dirs are pruned
        # the same way ripgrep would (audit d05).
        gitignore_path = Path(dirpath) / ".gitignore"
        if gitignore_path.is_file():
            for rule in _read_gitignore(gitignore_path):
                gitignore_rules.append((rel_dir, rule))
        if gitignore_rules:
            dirnames[:] = [d for d in dirnames
                           if not _gitignored(f"{rel_dir}/{d}" if rel_dir else d,
                                              gitignore_rules)]
        for fname in filenames:
            rel = f"{rel_dir}/{fname}" if rel_dir else fname
            if gitignore_rules and _gitignored(rel, gitignore_rules):
                continue
            candidates.append((rel.replace(os.sep, "/"), Path(dirpath) / fname))

    if include_glob is not None:
        glob_hits = {rel for rel, _ in candidates
                     if fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], include_glob)
                     or fnmatch.fnmatchcase(rel, include_glob)}
        if glob_hits:
            candidates = [(rel, p) for rel, p in candidates if rel in glob_hits]
        elif include_re is not None:
            candidates = [(rel, p) for rel, p in candidates
                          if include_re.search(rel.rsplit("/", 1)[-1])]
        else:
            candidates = []

    for rel, path in candidates:
        # Never read through a symlink that leaves the search root
        # (ripgrep does not follow symlinks — audit a06).
        if path.is_symlink():
            try:
                resolved = path.resolve()
                resolved.relative_to(root_abs)
            except (OSError, ValueError, RuntimeError):
                continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except Exception:
            continue
        if _is_binary(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    m = regex.search(line)
                    if m:
                        results.append({
                            "file": rel,
                            "line": i,
                            "column": m.start(),
                            "text": line.rstrip(),
                            "match": m.group(0),
                            "source": "python-grep",
                        })
                        if len(results) >= max_results:
                            return results
        except Exception:
            continue
    return results


def rg_search(pattern: str, root: str | Path, *, case_sensitive: bool = True,
              whole_word: bool = False, include: str | None = None,
              max_results: int = 50) -> dict[str, Any]:
    """Search file contents with ripgrep (or Python fallback).

    Args:
        pattern: POSIX/ripgrep regex.
        root: directory to search (``..`` components never widen the scope).
        case_sensitive: ripgrep ``--case-sensitive`` / ``--ignore-case``.
        whole_word: ripgrep ``--word-regexp``.
        include: ripgrep ``-g`` glob (a regex reading is the Python fallback's
            last resort when the glob matches nothing).
        max_results: cap on returned matches (hard-capped at ``MAX_RESULTS``).

    Returns:
        ``{"ok": true, "matches": [...], "tool": "rg"|"python-grep"}`` or
        ``{"ok": false, "error_kind": "invalid_input", "error": "..."}``.
        ``warnings`` is present when ripgrep hit errors or the Python fallback
        was used after an rg failure (audit 2026-10-18, c10).
    """
    root_path = _sanitize_root(root)
    if root_path is None or not root_path.is_dir():
        return {"ok": False, "error_kind": "invalid_input",
                "error": f"not a directory: {root}"}
    if not _is_project_path_safe({"path": str(root_path)}):
        return {"ok": False, "error_kind": "invalid_input",
                "error": f"path outside the configured Perun roots: {root_path} "
                         "(add it to roots in Settings to search it)"}
    bad = _validate_pattern(pattern)
    if bad is not None:
        return {"ok": False, "error_kind": "invalid_input", "error": bad}
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            return {"ok": False, "error_kind": "invalid_input",
                    "error": "max_results must be an integer"}
    if max_results < 0:
        return {"ok": False, "error_kind": "invalid_input",
                "error": "max_results must not be negative"}
    if max_results == 0:
        return {"ok": True, "matches": [],
                "tool": "rg" if rg_available() else "python-grep"}
    max_results = min(max_results, MAX_RESULTS)

    warnings: list[str] = []
    rg_failed = False
    if rg_available():
        argv = ["rg", "--json", "-n", "--max-columns", "500", "--crlf",
                *_RG_IGNORE_GLOBS]
        if not case_sensitive:
            argv.append("--ignore-case")
        if whole_word:
            argv.append("--word-regexp")
        if include:
            argv.extend(["-g", include])
        argv.extend(["-m", str(max_results), "--", pattern, str(root_path)])
        ok, stdout, returncode = _run(argv, timeout=60.0)
        if ok:
            matches = _parse_rg_json(stdout.splitlines(), root_path,
                                     max_results=max_results)
            matches = _dedupe_matches(matches, max_total=max_results, max_text_len=MAX_LINE_LEN)
            return {"ok": True, "matches": matches, "tool": "rg"}
        if returncode == 1:
            # ripgrep exit code 1 means "no matches" — a successful empty result.
            return {"ok": True, "matches": [], "tool": "rg"}
        # Exit code 2 (e.g. one unreadable file): rg still produced usable
        # matches for the rest — keep the engine instead of silently falling
        # back to a different one (audit c10).
        if stdout:
            matches = _parse_rg_json(stdout.splitlines(), root_path,
                                     max_results=max_results)
            if matches:
                matches = _dedupe_matches(matches, max_total=max_results,
                                          max_text_len=MAX_LINE_LEN)
                return {"ok": True, "matches": matches, "tool": "rg",
                        "warnings": [f"rg reported errors (exit {returncode}); "
                                     "results may be incomplete"]}
        rg_failed = True
        warnings.append(f"rg failed (exit {returncode}); used python-grep fallback")

    # Validate the regex before running the Python fallback.
    if _safe_compile_regex(pattern) is None:
        return {"ok": False, "error_kind": "invalid_input", "error": "invalid regex"}
    if _looks_catastrophic(pattern):
        return {"ok": False, "error_kind": "invalid_input",
                "error": "refusing catastrophic regex (nested quantifier like "
                         "(a+)+) — rewrite with a linear pattern"}

    matches = _python_grep(
        pattern, root_path, case_sensitive=case_sensitive,
        whole_word=whole_word, include=include, max_results=max_results,
    )
    matches = _dedupe_matches(matches, max_total=max_results, max_text_len=MAX_LINE_LEN)
    resp: dict[str, Any] = {"ok": True, "matches": matches, "tool": "python-grep"}
    if rg_failed:
        resp["warnings"] = warnings
    return resp


def _parse_rg_json(lines: Iterable[str] | str, root: Path | None = None,
                   max_results: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """Parse ripgrep's --json output.

    Accepts a generator/iterator of lines (or a single multi-line string) and
    stops early once *max_results* matches have been collected.  When *root* is
    ``None`` the original file path is kept, which is useful for global searches
    that attribute matches to projects afterwards.
    """
    if isinstance(lines, str):
        lines = lines.splitlines()
    matches: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj.get("data", {})
        path_data = data.get("path", {})
        path_text = path_data.get("text", "")
        if root is None:
            rel = path_text
        else:
            try:
                rel = str(Path(path_text).relative_to(root))
            except ValueError:
                rel = path_text
        line_no = data.get("line_number", 0)
        sublines = data.get("submatches", [])
        for sm in sublines:
            match_text = sm.get("match", {}).get("text", "")
            start = sm.get("start", 0)
            matches.append({
                "file": rel,
                "line": line_no,
                "column": start,
                "text": data.get("lines", {}).get("text", "").rstrip(),
                "match": match_text,
                "source": "rg",
            })
            if len(matches) >= max_results:
                return matches
    return matches


# --------------------------------------------------------------------------- #
# ast-grep / fallback ast search
# --------------------------------------------------------------------------- #
def ast_grep_available() -> bool:
    return _which("ast-grep") is not None or _which("sg") is not None


def _ast_grep_cmd() -> str | None:
    return _which("ast-grep") or _which("sg")


def _python_ast_search(pattern: str, root: Path, max_results: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """Very basic Python AST fallback for structural searches.

    Parses every ``.py`` file and returns nodes whose string representation
    matches the supplied pattern (which can be a regex over the AST dump).
    This is intentionally simple — it gives agents a structural signal even
    when ``ast-grep`` is missing.
    """
    import ast

    regex = _safe_compile_regex(pattern)
    if regex is None or max_results <= 0:
        return []

    results: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = Path(dirpath) / fname
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except Exception:
                continue
            if _is_binary(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                tree = ast.parse(text)
            except Exception:
                continue
            source_lines = text.splitlines()
            for node in ast.walk(tree):
                dump = ast.dump(node, include_attributes=False)
                m = regex.search(dump)
                if m:
                    lineno = getattr(node, "lineno", 0)
                    col_offset = getattr(node, "col_offset", 0)
                    source_line = source_lines[lineno - 1] if lineno and lineno <= len(source_lines) else ""
                    results.append({
                        "file": str(path.relative_to(root)),
                        "line": lineno,
                        "column": col_offset,
                        "text": source_line.rstrip(),
                        "match": type(node).__name__,
                        "source": "python-ast",
                    })
                    if len(results) >= max_results:
                        return results
    return results


def ast_grep_search(rule: str, root: str | Path,
                    max_results: int = 50) -> dict[str, Any]:
    """Structural code search with ast-grep (or Python-AST fallback).

    Args:
        rule: An ast-grep rule string, e.g. ``$F($$$ARGS)`` or a YAML rule.
        root: directory to search.
        max_results: cap on returned matches (hard-capped at ``MAX_RESULTS``).

    Returns:
        ``{"ok": true, "matches": [...], "tool": "ast-grep"|"python-ast"}`` or
        ``{"ok": false, "error": "..."}``.
    """
    root_path = _sanitize_root(root)
    if root_path is None or not root_path.is_dir():
        return {"ok": False, "error_kind": "invalid_input",
                "error": f"not a directory: {root}"}
    if not _is_project_path_safe({"path": str(root_path)}):
        return {"ok": False, "error_kind": "invalid_input",
                "error": f"path outside the configured Perun roots: {root_path} "
                         "(add it to roots in Settings to search it)"}
    if not str(rule or "").strip():
        return {"ok": False, "error_kind": "invalid_input", "error": "empty rule"}

    max_results = min(max(0, max_results), MAX_RESULTS)
    if max_results == 0:
        return {"ok": True, "matches": [],
                "tool": "ast-grep" if ast_grep_available() else "python-ast"}

    if ast_grep_available():
        cmd = _ast_grep_cmd()
        assert cmd is not None
        rule = str(rule or "").strip()
        is_yaml = "\n" in rule or rule.startswith(("id:", "language:", "rule:"))
        if is_yaml:
            argv = [
                cmd, "scan", "--json=stream",
                "--inline-rules", rule,
                str(root_path),
            ]
        else:
            # No --lang: let ast-grep auto-detect per file, so non-Python
            # projects are searchable too (audit 2026-10-18, drobne).
            argv = [
                cmd, "run", "--pattern", rule,
                "--json=stream",
                str(root_path),
            ]
        ok, stdout, returncode = _run(argv, timeout=60.0)
        if ok:
            matches = _parse_ast_grep_json(stdout.splitlines(), root_path,
                                           max_results=max_results)
            matches = _dedupe_matches(matches, max_total=max_results, max_text_len=MAX_LINE_LEN)
            return {"ok": True, "matches": matches, "tool": "ast-grep"}
        if returncode == 1:
            # ast-grep exit code 1 means "no matches" — a successful empty result.
            return {"ok": True, "matches": [], "tool": "ast-grep"}
        # Distinguish an invalid rule from other failures.
        if "invalid" in (stdout or "").lower():
            return {"ok": False, "error_kind": "invalid_input",
                    "error": "invalid ast-grep rule"}

    if _safe_compile_regex(rule) is None:
        return {"ok": False, "error_kind": "invalid_input",
                "error": "invalid ast-grep rule"}

    matches = _python_ast_search(rule, root_path, max_results=max_results)
    matches = _dedupe_matches(matches, max_total=max_results, max_text_len=MAX_LINE_LEN)
    return {"ok": True, "matches": matches, "tool": "python-ast"}


def _parse_ast_grep_json(lines: Iterable[str] | str, root: Path | None = None,
                         max_results: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """Parse ast-grep ``--json=stream`` output.

    Supports both the per-match stream objects emitted by current ast-grep and
    the older per-file envelope objects used by earlier versions.

    Accepts a generator/iterator of lines (or a single multi-line string) and
    stops early once *max_results* matches have been collected.  When *root* is
    ``None`` the original file path is kept, which is useful for global searches
    that attribute matches to projects afterwards.
    """
    if isinstance(lines, str):
        lines = lines.splitlines()
    matches: list[dict[str, Any]] = []

    def _rel(file_path: str) -> str:
        if root is None:
            return file_path
        try:
            return str(Path(file_path).relative_to(root))
        except ValueError:
            return file_path

    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "matches" in obj:
            # Legacy envelope format: {"file": "...", "matches": [...]}
            rel = _rel(obj.get("file", ""))
            for m in obj.get("matches", []):
                rng = m.get("range", {})
                text = m.get("text", "")
                matches.append({
                    "file": rel,
                    "line": rng.get("start", {}).get("line", 0),
                    "column": rng.get("start", {}).get("column", 0),
                    "text": text.splitlines()[0].rstrip() if text else "",
                    "match": text,
                    "source": "ast-grep",
                })
                if len(matches) >= max_results:
                    return matches
        else:
            # Modern stream format: one JSON object per match.
            rng = obj.get("range", {})
            text = obj.get("text", "")
            matches.append({
                "file": _rel(obj.get("file", "")),
                "line": rng.get("start", {}).get("line", 0),
                "column": rng.get("start", {}).get("column", 0),
                "text": text.splitlines()[0].rstrip() if text else "",
                "match": text,
                "source": "ast-grep",
            })
            if len(matches) >= max_results:
                return matches
    return matches


# --------------------------------------------------------------------------- #
# Multi-project / global search
# --------------------------------------------------------------------------- #
def _project_path(project: dict[str, Any]) -> str | None:
    return project.get("path") or project.get("root") or None


def _project_for_path(abs_path: str | Path, projects: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the most specific project that owns *abs_path*."""
    target = _resolve_abs(abs_path)
    best: dict[str, Any] | None = None
    best_len = -1
    for p in projects:
        ppath = _project_path(p)
        if not ppath:
            continue
        try:
            ptarget = _resolve_abs(ppath)
        except Exception:
            continue
        if target == ptarget or ptarget in target.parents:
            if len(str(ptarget)) > best_len:
                best_len = len(str(ptarget))
                best = p
    return best


def _tag_matches_with_project(matches: list[dict[str, Any]],
                              projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attribute absolute-path matches to their owning project."""
    for m in matches:
        abs_file = m.get("file", "")
        if not Path(abs_file).is_absolute():
            continue
        proj = _project_for_path(abs_file, projects)
        if proj:
            m["project"] = proj.get("slug") or proj.get("name") or proj.get("path")
            try:
                proj_path = _project_path(proj)
                if proj_path:
                    m["file"] = str(Path(abs_file).relative_to(proj_path))
            except ValueError:
                pass
    return matches


def rg_search_global(pattern: str, projects: list[dict], *, case_sensitive: bool = True,
                     whole_word: bool = False, include: str | None = None,
                     max_results: int = 20) -> dict[str, Any]:
    """Run a single ripgrep query across multiple projects.

    Matches are attributed to their owning project and deduplicated before
    being returned.
    """
    if not projects:
        return {"ok": True, "matches": [], "tool": "rg-global", "projects_searched": 0}

    roots = []
    try:
        from perun.config import settings
        roots = settings().roots
    except Exception:
        roots = []

    bad = _validate_pattern(pattern)
    if bad is not None:
        return {"ok": False, "error_kind": "invalid_input", "error": bad}
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            return {"ok": False, "error_kind": "invalid_input",
                    "error": "max_results must be an integer"}
    if max_results < 0:
        return {"ok": False, "error_kind": "invalid_input",
                "error": "max_results must not be negative"}
    if max_results == 0:
        return {"ok": True, "matches": [], "tool": "rg-global",
                "projects_searched": 0}

    paths: list[str] = []
    projects_searched = 0
    for p in projects:
        ppath = _project_path(p)
        if not ppath:
            continue
        if not _is_project_path_safe(p, roots):
            continue
        resolved = _resolve_abs(ppath)
        if resolved.is_dir():
            paths.append(str(resolved))
            projects_searched += 1
    if not paths:
        return {"ok": False, "error_kind": "not_found",
                "error": "no valid project paths"}

    max_results = min(max_results, MAX_RESULTS)

    warnings: list[str] = []
    rg_failed = False
    if rg_available():
        argv = ["rg", "--json", "-n", "--max-columns", "500", "--crlf",
                *_RG_IGNORE_GLOBS]
        if not case_sensitive:
            argv.append("--ignore-case")
        if whole_word:
            argv.append("--word-regexp")
        if include:
            argv.extend(["-g", include])

        argv.extend(["-m", str(max(1, max_results)), "--", pattern, *paths])

        ok, stdout, returncode = _run(argv, timeout=120.0)
        if ok:
            matches = _parse_rg_json(stdout.splitlines(), root=None,
                                     max_results=max_results)
            matches = _tag_matches_with_project(matches, projects)
            matches = _dedupe_matches(matches, max_total=max_results, max_text_len=MAX_LINE_LEN)
            return {"ok": True, "matches": matches, "tool": "rg-global",
                    "projects_searched": projects_searched}
        if returncode == 1:
            # ripgrep exit code 1 means "no matches" — a successful empty result.
            return {"ok": True, "matches": [], "tool": "rg-global",
                    "projects_searched": projects_searched}
        # Exit code 2 with usable output (e.g. one unreadable file): keep rg's
        # results instead of silently switching engine (audit c10).
        if stdout:
            matches = _parse_rg_json(stdout.splitlines(), root=None,
                                     max_results=max_results)
            if matches:
                matches = _tag_matches_with_project(matches, projects)
                matches = _dedupe_matches(matches, max_total=max_results,
                                          max_text_len=MAX_LINE_LEN)
                return {"ok": True, "matches": matches, "tool": "rg-global",
                        "projects_searched": projects_searched,
                        "warnings": [f"rg reported errors (exit {returncode}); "
                                     "results may be incomplete"]}
        rg_failed = True
        warnings.append(f"rg failed (exit {returncode}); used python-grep fallback")
        # fall through to the Python fallback below (e.g. a command line so
        # long that Popen refused it).

    # Validate regex before Python fallback.
    if _safe_compile_regex(pattern) is None:
        return {"ok": False, "error_kind": "invalid_input", "error": "invalid regex"}
    if _looks_catastrophic(pattern):
        return {"ok": False, "error_kind": "invalid_input",
                "error": "refusing catastrophic regex (nested quantifier like "
                         "(a+)+) — rewrite with a linear pattern"}

    all_matches: list[dict[str, Any]] = []
    for p in projects:
        ppath = _project_path(p)
        if not ppath or not _is_project_path_safe(p, roots):
            continue
        resolved = _resolve_abs(ppath)
        if not resolved.is_dir():
            continue
        submatches = _python_grep(
            pattern, resolved, case_sensitive=case_sensitive,
            whole_word=whole_word, include=include, max_results=max_results,
        )
        for m in submatches:
            m["project"] = p.get("slug") or p.get("name") or ppath
        all_matches.extend(submatches)
    matches = _dedupe_matches(all_matches, max_total=max_results, max_text_len=MAX_LINE_LEN)
    resp: dict[str, Any] = {"ok": True, "matches": matches,
                            "tool": "python-grep-global",
                            "projects_searched": projects_searched}
    if rg_failed:
        resp["warnings"] = warnings
    return resp


def ast_grep_search_global(rule: str, projects: list[dict], *, max_results: int = 20) -> dict[str, Any]:
    """Run a single ast-grep query across multiple projects.

    Matches are attributed to their owning project and deduplicated before
    being returned.
    """
    if not projects:
        return {"ok": True, "matches": [], "tool": "ast-grep-global", "projects_searched": 0}

    if not str(rule or "").strip():
        return {"ok": False, "error_kind": "invalid_input", "error": "empty rule"}
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        return {"ok": False, "error_kind": "invalid_input",
                "error": "max_results must be an integer"}
    if max_results < 0:
        return {"ok": False, "error_kind": "invalid_input",
                "error": "max_results must not be negative"}
    if max_results == 0:
        return {"ok": True, "matches": [], "tool": "ast-grep-global",
                "projects_searched": 0}

    roots = []
    try:
        from perun.config import settings
        roots = settings().roots
    except Exception:
        roots = []

    paths: list[str] = []
    projects_searched = 0
    for p in projects:
        ppath = _project_path(p)
        if not ppath:
            continue
        if not _is_project_path_safe(p, roots):
            continue
        resolved = _resolve_abs(ppath)
        if resolved.is_dir():
            paths.append(str(resolved))
            projects_searched += 1
    if not paths:
        return {"ok": False, "error_kind": "not_found",
                "error": "no valid project paths"}

    max_results = min(max_results, MAX_RESULTS)

    if ast_grep_available():
        cmd = _ast_grep_cmd()
        assert cmd is not None
        rule = str(rule or "").strip()
        is_yaml = "\n" in rule or rule.startswith(("id:", "language:", "rule:"))
        argv: list[str]
        if is_yaml:
            argv = [
                cmd, "scan", "--json=stream",
                "--inline-rules", rule,
            ]
        else:
            # No --lang: ast-grep auto-detects per file (any project language).
            argv = [
                cmd, "run", "--pattern", rule,
                "--json=stream",
            ]

        argv.extend(paths)

        ok, stdout, returncode = _run(argv, timeout=120.0)
        if ok:
            matches = _parse_ast_grep_json(stdout.splitlines(), root=None,
                                           max_results=max_results)
            matches = _tag_matches_with_project(matches, projects)
            matches = _dedupe_matches(matches, max_total=max_results, max_text_len=MAX_LINE_LEN)
            return {"ok": True, "matches": matches, "tool": "ast-grep-global",
                    "projects_searched": projects_searched}
        if returncode == 1:
            # ast-grep exit code 1 means "no matches" — a successful empty result.
            return {"ok": True, "matches": [], "tool": "ast-grep-global",
                    "projects_searched": projects_searched}
        # An invalid rule is reported directly; other failures fall through to
        # the Python fallback below.
        if "invalid" in (stdout or "").lower():
            return {"ok": False, "error_kind": "invalid_input",
                    "error": "invalid ast-grep rule"}

    if _safe_compile_regex(rule) is None:
        return {"ok": False, "error_kind": "invalid_input",
                "error": "invalid ast-grep rule"}

    all_matches: list[dict[str, Any]] = []
    for p in projects:
        ppath = _project_path(p)
        if not ppath or not _is_project_path_safe(p, roots):
            continue
        resolved = _resolve_abs(ppath)
        if not resolved.is_dir():
            continue
        submatches = _python_ast_search(rule, resolved, max_results=max_results)
        for m in submatches:
            m["project"] = p.get("slug") or p.get("name") or ppath
        all_matches.extend(submatches)
    matches = _dedupe_matches(all_matches, max_total=max_results, max_text_len=MAX_LINE_LEN)
    return {"ok": True, "matches": matches, "tool": "python-ast-global",
            "projects_searched": projects_searched}


def search_all_projects(pattern=None, rule=None, projects=None, max_results=20,
                        **kwargs) -> dict[str, Any]:
    """Search all registered projects with ripgrep or ast-grep.

    Exactly one of *pattern* (regex grep) or *rule* (ast-grep) must be
    supplied.  When *projects* is ``None`` the full project registry is loaded.
    """
    if projects is None:
        from perun import registry
        projects = registry.list_projects()

    roots = []
    try:
        from perun.config import settings
        roots = settings().roots
    except Exception:
        roots = []

    safe_projects = [p for p in projects if _is_project_path_safe(p, roots)]

    if bool(pattern) == bool(rule):
        return {"ok": False, "error_kind": "invalid_input",
                "error": "pass pattern (grep) or rule (ast-grep)"}

    if pattern:
        return rg_search_global(
            pattern, safe_projects, max_results=max_results,
            case_sensitive=kwargs.get("case_sensitive", True),
            whole_word=kwargs.get("whole_word", False),
            include=kwargs.get("include"),
        )
    return ast_grep_search_global(rule, safe_projects, max_results=max_results)


# --------------------------------------------------------------------------- #
# Capability summary
# --------------------------------------------------------------------------- #
def search_capabilities() -> dict[str, Any]:
    return {
        "rg": rg_available(),
        "ast_grep": ast_grep_available(),
    }
