"""Documentation-to-code traceability audit.

Parses a project's ``docs/CODEMAP.md`` for source paths and code identifiers,
then verifies that every referenced file exists and every class/function is
defined in one of the primary Python source files listed on the same CODEMAP
row. Also checks that the current package version appears in CHANGELOG and
CODEMAP so stale version strings are caught.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

FILE_RE = re.compile(
    r"`?([a-zA-Z0-9_][a-zA-Z0-9_./-]*\.[a-zA-Z0-9]+)`?")
IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")
VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")


def _strip_call(ident: str) -> str:
    """Drop a trailing ``()`` from a backticked reference."""
    if ident.endswith("()"):
        return ident[:-2]
    return ident


def _looks_like_path(text: str) -> bool:
    """A backticked token is a file/path if it contains ``/``, a file extension, or ``://``."""
    if "://" in text:
        return False
    if "/" in text or "\\" in text:
        return True
    return bool(re.search(r"\.(py|js|ts|html|css|md|yml|yaml|json|toml|txt|ini|cfg|exe|sha256)$", text, re.I))


def _module_symbols(path: Path) -> tuple[set[str], set[str]]:
    """Return (class_names, function_and_method_names) for a Python module."""
    if path.suffix != ".py":
        return set(), set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set(), set()

    classes: set[str] = set()
    funcs: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            classes.add(node.name)
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs.add(f"{node.name}.{child.name}")
                    funcs.add(child.name)
                targets: list[Any] = []
                if isinstance(child, ast.Assign):
                    targets = child.targets
                elif isinstance(child, ast.AnnAssign):
                    targets = [child.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        funcs.add(f"{node.name}.{target.id}")
                        funcs.add(target.id)
            for child in ast.walk(node):
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for sub in ast.walk(child):
                    targets = []
                    if isinstance(sub, ast.Assign):
                        targets = sub.targets
                    elif isinstance(sub, ast.AnnAssign):
                        targets = [sub.target]
                    for target in targets:
                        if (isinstance(target, ast.Attribute)
                                and isinstance(target.value, ast.Name)
                                and target.value.id == "self"):
                            funcs.add(f"{node.name}.{target.attr}")
                            funcs.add(target.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(node.name)

    for child in ast.iter_child_nodes(tree):
        targets = []
        if isinstance(child, ast.Assign):
            targets = child.targets
        elif isinstance(child, ast.AnnAssign):
            targets = [child.target]
        for target in targets:
            if isinstance(target, ast.Name):
                funcs.add(target.id)
    return classes, funcs


def _project_version(root: Path) -> str | None:
    """Read ``__version__`` from the first package ``__init__.py``."""
    for init in root.glob("*/__init__.py"):
        text = init.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
        if m:
            return m.group(1)
    return None


def _codemap_rows(text: str) -> list[dict[str, Any]]:
    """Parse CODEMAP into rows, one per ``###`` module block."""
    text = text.replace("\r\n", "\n")
    rows: list[dict[str, Any]] = []
    parts = re.split(r"(?m)^## ", text)
    h3_re = re.compile(r"(?m)^### ")
    for part in parts[1:]:
        part_lines = part.splitlines()
        h2 = part_lines[0].strip() if part_lines else "(untitled)"
        body = "\n".join(part_lines[1:])
        h3_parts = h3_re.split(body)
        for sub in h3_parts[1:]:
            sub_lines = sub.splitlines()
            h3 = sub_lines[0].strip() if sub_lines else "(untitled)"
            block_text = "\n".join(sub_lines[1:])
            paths = []
            idents = []
            for m in FILE_RE.findall(f"`{h3}`\n{block_text}"):
                if not _looks_like_path(m):
                    continue
                paths.append(m.strip("/`"))
            for raw in IDENT_RE.findall(block_text):
                ident = _strip_call(raw)
                if _looks_like_path(ident):
                    continue
                # Only check API-surface identifiers: classes, functions/methods,
                # and constants. Skip lowercase local variables and env-style
                # all-caps strings.
                if ident.isupper():
                    continue
                if "." in ident and ident.split(".")[0].islower():
                    continue
                if not (ident[0].isupper() or raw.endswith("()")):
                    continue
                idents.append(ident)
            rows.append({
                "h2": h2,
                "h3": h3,
                "files": list(dict.fromkeys(paths)),
                "idents": list(dict.fromkeys(idents)),
            })
    return rows


def audit_project(project_path: str | Path) -> dict[str, Any]:
    """Audit ``docs/CODEMAP.md`` traceability for *project_path*.

    Returns ``{"ok": bool, "issues": [...], "files": int, "identifiers": int,
    "version": str|None}``.
    """
    root = Path(project_path)
    issues: list[str] = []
    codemap_path = root / "docs" / "CODEMAP.md"
    changelog_path = root / "docs" / "CHANGELOG.md"

    if not codemap_path.is_file():
        return {
            "ok": False,
            "issues": ["docs/CODEMAP.md not found"],
            "files": 0,
            "identifiers": 0,
            "version": None,
        }

    text = codemap_path.read_text(encoding="utf-8", errors="replace")
    rows = _codemap_rows(text)
    symbol_cache: dict[Path, tuple[set[str], set[str]]] = {}
    missing_files: set[str] = set()
    files_checked = 0
    idents_checked = 0

    fallback_dirs = [
        root / "perun",
        root / "perun" / "integrations" / "templates",
        root / "perun" / "integrations" / "templates" / "handbook",
        root / "perun" / "instructions",
        root / "perun" / "integrations",
        root / "web",
        root / ".perun",
        root / "config" / "opencode" / "agents",
    ]

    for row in rows:
        files = row["files"]
        idents = row["idents"]
        resolved: list[Path] = []
        for f in files:
            candidate = root / f
            if not candidate.is_file():
                candidate = root / f.split("/")[-1]
            if not candidate.is_file():
                for d in fallback_dirs:
                    cand = d / f.split("/")[-1]
                    if cand.is_file():
                        candidate = cand
                        break
            if not candidate.is_file():
                if f not in missing_files:
                    issues.append(f"missing file referenced in CODEMAP: {f}")
                    missing_files.add(f)
                continue
            resolved.append(candidate)
            files_checked += 1

        py_files = [p for p in resolved if p.suffix == ".py"]
        for ident in idents:
            idents_checked += 1
            if not py_files:
                continue
            found = False
            for p in py_files:
                if p not in symbol_cache:
                    symbol_cache[p] = _module_symbols(p)
                classes, funcs = symbol_cache[p]
                if ident in classes or ident in funcs:
                    found = True
                    break
            if not found:
                file_list = ", ".join(str(p.relative_to(root)) for p in py_files)
                issues.append(f"unresolved identifier `{ident}` in [{file_list}]")

    version = _project_version(root)
    if version:
        if not changelog_path.is_file():
            issues.append("docs/CHANGELOG.md not found")
        elif version not in changelog_path.read_text(encoding="utf-8", errors="replace"):
            issues.append(f"version {version} not found in docs/CHANGELOG.md")
        if version not in text:
            issues.append(f"version {version} not found in docs/CODEMAP.md")

    return {
        "ok": not issues,
        "issues": issues,
        "files": files_checked,
        "identifiers": idents_checked,
        "version": version,
    }
