"""Safe ZIP work-package pack/unpack/validate/apply for external execution.

External rw agents receive a ZIP of the approved targets and upload a modified
ZIP in return. This module guards against traversal, zip-bombs, symlinks,
encrypted entries, path-escape, and writes outside the project allowlist.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from . import config, path_guard, shared_projects


class PackageError(Exception):
    """A package violates size, format, path or security constraints."""


# Defaults are overridden by the active Settings where available.
DEFAULT_MAX_PACKAGE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_ENTRY_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 500


def _settings() -> config.Settings:
    try:
        return config.Settings.load()
    except Exception:
        return config.Settings()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pack(project: dict[str, Any], paths: list[str] | None = None) -> bytes:
    """Build a ZIP archive of *paths* relative to the project root.

    *paths* defaults to the project root. Every entry name is a POSIX-style
    path relative to the project root. Directories are skipped (only files are
    stored). All requested paths are checked against the project allowlist
    first; any violation raises :class:`PackageError`.
    """
    root = Path(project["path"]).resolve()
    cfg = shared_projects.load(root)
    external_deps = cfg.get("external_deps", [])

    targets = paths if paths is not None else ["."]
    if isinstance(targets, str):
        targets = [targets]
    errors: list[str] = []
    for target in targets:
        ok, reason = path_guard.check(root, target, external_deps, require_write=False)
        if not ok:
            errors.append(reason)
    if errors:
        raise PackageError("; ".join(errors))

    # Collect first so size/entry caps can be enforced before building a
    # potentially huge in-memory archive. Overlapping targets are deduplicated.
    selected: list[Path] = []
    seen: set[Path] = set()
    for target in targets:
        target_path = path_guard.resolve_target(root, target)
        if not _path_under(target_path, root):
            raise PackageError(
                "external dependency targets are not supported in external "
                "handoff; use local execution")
        candidates: list[Path] = []
        if target_path.is_file():
            candidates = [target_path]
        elif target_path.is_dir():
            for dirpath, dirnames, filenames in os.walk(target_path):
                dirnames[:] = [
                    d for d in dirnames
                    if not path_guard.is_sensitive_path(Path(dirpath) / d)
                ]
                candidates.extend(Path(dirpath) / name for name in filenames)
        # Non-existent targets are silently omitted; the agent will see the
        # absence and can create the file in the uploaded package.
        for p in candidates:
            if p.is_symlink() or path_guard.is_sensitive_path(p):
                continue
            resolved = p.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            selected.append(p)

    settings = _settings()
    max_package = (settings.external_execution_max_package_bytes
                   or DEFAULT_MAX_PACKAGE_BYTES)
    max_entry = (settings.external_execution_max_entry_bytes
                 or DEFAULT_MAX_ENTRY_BYTES)
    max_count = (settings.external_execution_max_entries
                 or DEFAULT_MAX_ENTRIES)
    if len(selected) > max_count:
        raise PackageError(
            f"package contains {len(selected)} files; max is {max_count}")
    total = 0
    for p in selected:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > max_entry:
            raise PackageError(
                f"{p.name} size {size} exceeds max entry bytes {max_entry}")
        total += size
        if total > max_package:
            raise PackageError(
                f"package uncompressed size {total} exceeds max {max_package}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in selected:
            _add_file(zf, root, p)
    return buf.getvalue()


def _add_file(zf: zipfile.ZipFile, root: Path, path: Path) -> None:
    if path.is_symlink() or path_guard.is_sensitive_path(path):
        return
    try:
        rel = path.resolve().relative_to(root)
    except ValueError:
        raise PackageError(f"path {path} is outside the project root")
    arcname = rel.as_posix()
    if arcname.startswith("../") or "/../" in arcname:
        raise PackageError(f"path escapes the archive root: {arcname}")
    zf.write(path, arcname)


def unpack(
    archive_bytes: bytes,
    *,
    max_package_bytes: int | None = None,
    max_entry_bytes: int | None = None,
    max_entries: int | None = None,
) -> dict[str, bytes]:
    """Safely decode a ZIP archive into ``{relative_path: content}``.

    Raises :class:`PackageError` on any abuse (traversal, symlink, encrypted
    entry, size cap, bad ZIP, etc.). The archive is **not** written to disk.
    """
    settings = _settings()
    max_package = max_package_bytes or settings.external_execution_max_package_bytes or DEFAULT_MAX_PACKAGE_BYTES
    max_entry = max_entry_bytes or settings.external_execution_max_entry_bytes or DEFAULT_MAX_ENTRY_BYTES
    max_count = max_entries or settings.external_execution_max_entries or DEFAULT_MAX_ENTRIES

    if len(archive_bytes) > max_package:
        raise PackageError(
            f"compressed archive size {len(archive_bytes)} exceeds max package bytes {max_package}"
        )

    if not zipfile.is_zipfile(io.BytesIO(archive_bytes)):
        raise PackageError("uploaded data is not a valid ZIP archive")

    files: dict[str, bytes] = {}
    seen_names: set[str] = set()
    total_uncompressed = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as zf:
            infos = zf.infolist()
            if len(infos) > max_count:
                raise PackageError(f"archive contains {len(infos)} entries; max is {max_count}")

            for info in infos:
                if info.filename in seen_names:
                    raise PackageError(f"duplicate entry name not allowed: {info.filename}")
                seen_names.add(info.filename)
                # Reject encrypted entries.
                if info.flag_bits & 0x1:
                    raise PackageError(f"encrypted entry not allowed: {info.filename}")

                # Reject symlinks and other non-regular files.
                mode = (info.external_attr >> 16)
                file_type = stat.S_IFMT(mode)
                if file_type == stat.S_IFLNK:
                    raise PackageError(f"symlink entry not allowed: {info.filename}")

                # Name checks: absolute, drive letters, traversal, null bytes.
                name = info.filename
                if not name or name.startswith("/") or name.startswith("\\"):
                    raise PackageError(f"absolute entry path not allowed: {name}")
                if ":" in name:
                    raise PackageError(f"drive-letter/UNC entry path not allowed: {name}")
                if "\x00" in name:
                    raise PackageError(f"null byte in entry path: {name}")
                parts = name.replace("\\", "/").split("/")
                if any(part == ".." for part in parts):
                    raise PackageError(f"entry escapes archive root: {name}")
                if info.is_dir():
                    # The protocol transfers files only; a directory entry
                    # would otherwise be applied as an empty regular file.
                    continue

                # Size guards before extraction (zip-bomb protection).
                uncompressed = info.file_size
                if uncompressed > max_entry:
                    raise PackageError(
                        f"entry {name} uncompressed size {uncompressed} exceeds max {max_entry}"
                    )
                total_uncompressed += uncompressed
                if total_uncompressed > max_package:
                    raise PackageError(
                        f"total uncompressed size exceeds max package bytes {max_package}"
                    )

                data = zf.read(info)
                if len(data) != uncompressed:
                    raise PackageError(f"entry {name} size mismatch after extraction")
                files[name] = data
    except zipfile.BadZipFile as exc:
        raise PackageError(f"bad ZIP archive: {exc}") from exc

    return files


def validate_package(
    project: dict[str, Any],
    proposal: dict[str, Any],
    files: dict[str, bytes],
) -> list[str]:
    """Return a list of errors for files outside the proposal targets/allowlist."""
    root = project["path"]
    cfg = shared_projects.load(root)
    external_deps = cfg.get("external_deps", [])
    target_paths = [
        path_guard.resolve_target(root, t) for t in (proposal.get("targets") or [])
    ]
    unsupported = [
        t for t in (proposal.get("targets") or [])
        if not _path_under(path_guard.resolve_target(root, t), Path(root).resolve())
    ]
    if unsupported:
        return [
            f"external dependency target not supported in external handoff: {t}"
            for t in unsupported
        ]

    errors: list[str] = []
    for relpath in files:
        resolved = path_guard.resolve_target(root, relpath)

        if path_guard.is_sensitive_path(resolved):
            errors.append(f"{relpath} resolves to a protected or sensitive path")
            continue

        ok, reason = path_guard.check(root, relpath, external_deps, require_write=True)
        if not ok:
            errors.append(reason)
            continue

        if target_paths and not any(_path_under(resolved, t) for t in target_paths):
            errors.append(f"{relpath} is not under any approved proposal target")

    return errors


def apply_package(
    project: dict[str, Any],
    files: dict[str, bytes],
) -> list[str]:
    """Apply a validated package as a unit and return the relative paths applied.

    All files are staged and existing targets are backed up before any replace.
    If a write fails, previously applied files are restored and newly created
    files are removed, so a package failure does not leave a half-applied tree.
    Validation must be done before calling this function.
    """
    root = Path(project["path"]).resolve()
    staging = Path(tempfile.mkdtemp(prefix="perun-apply-"))
    new_dir = staging / "_new"
    backup_dir = staging / "_backup"
    new_dir.mkdir()
    backup_dir.mkdir()
    applied: list[str] = []
    backups: list[tuple[Path, Path | None]] = []
    try:
        for relpath, data in files.items():
            target = _target_for_apply(root, relpath)
            rel = target.relative_to(root)
            staged = new_dir / rel
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)
            if target.is_file():
                backup = backup_dir / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                backups.append((target, backup))
            else:
                backups.append((target, None))

        for relpath in files:
            target = _target_for_apply(root, relpath)
            rel = target.relative_to(root)
            staged = new_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".perun-tmp")
            try:
                shutil.copy2(staged, tmp)
                os.replace(str(tmp), str(target))
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            applied.append(relpath)
    except Exception:
        for target, backup in reversed(backups):
            try:
                if backup is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except Exception:
                pass
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return applied


def _target_for_apply(root: Path, relpath: str) -> Path:
    target = path_guard.resolve_target(root, relpath)
    if not _path_under(target, root):
        raise PackageError(
            "external dependency targets are not supported in external handoff")
    return target


def _path_under(path: Path, parent: Path) -> bool:
    """Return True when *path* is *parent* or lives beneath it."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
