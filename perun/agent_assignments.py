"""Placement of roster agents: global, per project, or not assigned at all.

Custody — the manifest, hashes, backups, the ceiling — belongs to
:mod:`perun.agent_catalog`. This module owns *where* an agent is placed and
imports the catalogue lazily, so the two can reference each other without a
circular import at load time.

An assignment is not stored separately: it is derived from the directory a copy
lives in, because that is the only truth OpenCode reads.

    ~/.config/opencode/agents/<slug>.md    -> "global"
    <project>/.opencode/agents/<slug>.md   -> "project:<slug>"
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import backups, config


def _catalog():
    from . import agent_catalog

    return agent_catalog


def global_dir() -> Path:
    return Path(config.OPENCODE_CONFIG_DIR) / "agents"


def project_dir(project_path: str | Path) -> Path:
    return Path(project_path) / ".opencode" / "agents"


def project_paths() -> Dict[str, str]:
    """Registered projects by slug (empty when the registry is unavailable)."""
    from . import registry

    try:
        return {str(p["slug"]): str(p["path"])
                for p in registry.list_projects(include_hidden=True)}
    except Exception:  # pragma: no cover - a registry hiccup must not break status
        return {}


def _under(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def assignment_of(path: str | Path, projects: Dict[str, str] | None = None) -> str:
    """``global`` | ``project:<slug>`` | ``external`` for a tracked copy."""
    target = Path(path).resolve()
    if _under(target, global_dir()):
        return "global"
    for slug, root in (projects if projects is not None else project_paths()).items():
        if _under(target, project_dir(root)):
            return f"project:{slug}"
    return "external"


def target_dir(target: str, projects: Dict[str, str] | None = None) -> Path:
    """Resolve an assignment target to the directory OpenCode reads."""
    raw = (target or "").strip()
    if raw == "global":
        return global_dir()
    if raw.startswith("project:"):
        slug = raw.split(":", 1)[1]
        table = projects if projects is not None else project_paths()
        root = table.get(slug)
        if not root:
            raise _catalog().CatalogError(f"unknown project: {slug}")
        return project_dir(root)
    raise _catalog().CatalogError(
        f"unsupported assignment target: {raw or '(empty)'}")


def ensure_gitignore(directory: Path) -> None:
    """Keep materialised project copies out of git: assignments are local-only."""
    marker = directory / ".gitignore"
    if marker.is_file():
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        marker.write_text("*\n", encoding="utf-8", newline="\n")
    except OSError:  # pragma: no cover - a read-only checkout still works
        pass


def owner_meta(cat, source: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    entry = manifest["agents"].get(str(source.resolve())) or {}
    meta = {k: entry[k] for k in ("source", "commit", "division", "mode_intent")
            if k in entry}
    meta.setdefault("source", "agency-agents")
    meta.setdefault("installed_by", "perun")
    return meta


def copy_to(cat, source: Path, target: Path, manifest: Dict[str, Any], *,
            meta: Dict[str, Any]) -> str:
    """Materialise *source* at *target*, refusing to clobber anything edited."""
    key = str(target.resolve())
    entry = manifest["agents"].get(key)
    if target.exists():
        if not entry:
            raise cat.CatalogError("exists and is not tracked by Perun")
        if cat._hash(target) != str(entry.get("sha256") or ""):
            raise cat.CatalogError("edited since it was installed")
        backups.save_copy(target, label="agency")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise cat.CatalogError(f"could not read {source.name}: {exc}") from exc
    tmp = target.with_suffix(f".{os.urandom(4).hex()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort
            pass
        raise cat.CatalogError(f"could not be written: {exc}") from exc
    cat._forget_hash(target)
    data = dict(meta)
    data.update(slug=source.stem, sha256=cat._hash(target),
                adopted_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    manifest["agents"][key] = data
    return data["sha256"]


def check_ceiling(cat, project_path: str | Path | None, slugs: Iterable[str],
                  limit: Optional[int]) -> None:
    """Refuse an assignment that would push the runtime over the ceiling."""
    existing = {p.stem for p in cat._md_files(cat.agent_dirs(project_path))}
    planned = existing | {s for s in slugs if s}
    ceiling = int(cat.configured_limit() if limit is None else limit)
    budget = cat.slots(project_path, measure=True)
    baseline = budget["baseline"]
    projected = len(planned) + (baseline or 0)
    if projected > ceiling:
        detail = (f"{baseline} non-roster + {len(planned)} roster"
                  if baseline is not None
                  else f"{len(planned)} roster; no runtime baseline applied")
        raise cat.CatalogError(
            f"{projected} agents would be registered, over the {ceiling}-agent "
            f"ceiling ({detail}); raise it with `perun agent-catalog limit {projected}`")


def assign(slugs: Iterable[str], target: str, *,
           limit: Optional[int] = None) -> Dict[str, Any]:
    """Materialise agents into a global or per-project agent directory."""
    cat = _catalog()
    projects = project_paths()
    directory = target_dir(target, projects)
    wanted = [s for s in (slugs or []) if s]
    if not wanted:
        raise cat.CatalogError("no agents selected")

    project_path: Optional[Path] = None
    if target.startswith("project:"):
        project_path = Path(projects[target.split(":", 1)[1]])
    check_ceiling(cat, project_path, wanted, limit)

    snapshot = cat._load_manifest()
    global_slugs = {str(entry.get("slug") or Path(path).stem)
                    for path, entry in snapshot["agents"].items()
                    if assignment_of(Path(path), projects) == "global"}

    assigned: List[str] = []
    skipped: List[Dict[str, str]] = []
    with cat._manifest_txn() as manifest:
        for slug in wanted:
            if target.startswith("project:") and slug in global_slugs:
                skipped.append({"path": slug, "reason": "already assigned globally"})
                continue
            try:
                source = cat._target_for(slug)
            except cat.CatalogError as exc:
                skipped.append({"path": slug, "reason": str(exc)})
                continue
            target_path = directory / f"{slug}.md"
            if target_path.resolve() == source.resolve():
                assigned.append(str(target_path.resolve()))
                continue
            meta = owner_meta(cat, source, snapshot)
            try:
                copy_to(cat, source, target_path, manifest, meta=meta)
                assigned.append(str(target_path.resolve()))
            except cat.CatalogError as exc:
                skipped.append({"path": str(target_path), "reason": str(exc)})
            except OSError as exc:
                skipped.append({"path": str(target_path),
                                "reason": f"could not be written: {exc}"})
    if assigned and target.startswith("project:"):
        ensure_gitignore(directory)
    return {"ok": True, "target": target, "assigned": assigned, "skipped": skipped,
            "count": len(assigned), "skipped_count": len(skipped)}


def unassign(slugs: Iterable[str], target: str, *,
             backup_first: bool = True) -> Dict[str, Any]:
    """Remove one assignment; the agent stays wherever else it is assigned."""
    cat = _catalog()
    directory = target_dir(target)
    wanted = [s for s in (slugs or []) if s]
    if not wanted:
        raise cat.CatalogError("no agents selected")

    removed: List[str] = []
    skipped: List[Dict[str, str]] = []
    with cat._manifest_txn() as manifest:
        for slug in wanted:
            path = (directory / f"{slug}.md").resolve()
            try:
                cat._assert_touchable(path)
                entry = cat._entry(manifest, path)
                if not path.is_file():
                    raise cat.CatalogError("not assigned here")
                if cat._hash(path) != str(entry.get("sha256") or ""):
                    raise cat.CatalogError("edited since it was installed")
                if backup_first:
                    backups.save_copy(path, label="agency")
                path.unlink()
                cat._forget_hash(path)
                manifest["agents"].pop(str(path), None)
                removed.append(str(path))
            except cat.CatalogError as exc:
                skipped.append({"path": str(path), "reason": str(exc)})
            except OSError as exc:
                skipped.append({"path": str(path),
                                "reason": f"could not be removed: {exc}"})
    return {"ok": True, "target": target, "removed": removed, "skipped": skipped,
            "count": len(removed), "skipped_count": len(skipped)}


def fan_out(slug: str, *, owner: str | Path = "",
            exclude: str | Path = "") -> Dict[str, Any]:
    """Push *owner*'s bytes to every other copy of *slug* (mode changes).

    The owner is passed explicitly: resolving it by slug can pick any copy (the
    manifest is stored sorted by path), which would fan out the wrong way.
    """
    cat = _catalog()
    source = Path(owner).resolve() if owner else cat._target_for(slug)
    skip = str(Path(exclude).resolve()) if exclude else ""
    snapshot = cat._load_manifest()
    meta = owner_meta(cat, source, snapshot)

    updated: List[str] = []
    skipped: List[Dict[str, str]] = []
    with cat._manifest_txn() as manifest:
        for key, entry in list(manifest["agents"].items()):
            if str(entry.get("slug") or "") != slug:
                continue
            path = Path(key).resolve()
            if str(path) == str(source) or str(path) == skip:
                continue
            try:
                copy_to(cat, source, path, manifest, meta=meta)
                updated.append(str(path))
            except cat.CatalogError as exc:
                skipped.append({"path": str(path), "reason": str(exc)})
            except OSError as exc:
                skipped.append({"path": str(path),
                                "reason": f"could not be written: {exc}"})
    return {"ok": True, "updated": updated, "skipped": skipped,
            "count": len(updated), "skipped_count": len(skipped)}


def remove_slug(slug: str, *, backup_first: bool = True) -> Dict[str, Any]:
    """Uninstall an agent everywhere it is assigned."""
    cat = _catalog()
    wanted = str(slug or "").strip()
    if not wanted:
        raise cat.CatalogError("an agent slug is required")

    removed: List[str] = []
    skipped: List[Dict[str, str]] = []
    with cat._manifest_txn() as manifest:
        for key, entry in list(manifest["agents"].items()):
            if str(entry.get("slug") or "") != wanted:
                continue
            path = Path(key)
            try:
                cat._assert_touchable(path)
                if cat._hash(path) != str(entry.get("sha256") or ""):
                    raise cat.CatalogError(
                        "edited since it was installed; accept or forget it first")
                if backup_first:
                    backups.save_copy(path, label="agency")
                path.unlink(missing_ok=True)
                cat._forget_hash(path)
                manifest["agents"].pop(key, None)
                removed.append(str(path))
            except cat.CatalogError as exc:
                skipped.append({"path": key, "reason": str(exc)})
            except OSError as exc:
                skipped.append({"path": key, "reason": f"could not be removed: {exc}"})
    if not removed and not skipped:
        raise cat.CatalogError(f"not installed: {wanted}")
    return {"ok": True, "slug": wanted, "removed": removed, "skipped": skipped,
            "count": len(removed), "skipped_count": len(skipped)}
