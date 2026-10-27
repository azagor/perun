"""Safe ZIP work-package pack/unpack/validate/apply for external execution.

External rw agents receive a ZIP of the approved targets and upload a modified
ZIP in return. This module guards against traversal, zip-bombs, symlinks,
encrypted entries, path-escape, and writes outside the project allowlist.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
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
    errors: list[str] = []
    for target in targets:
        ok, reason = path_guard.check(root, target, external_deps, require_write=False)
        if not ok:
            errors.append(reason)
    if errors:
        raise PackageError("; ".join(errors))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for target in targets:
            target_path = path_guard.resolve_target(root, target)
            if target_path.is_file():
                _add_file(zf, root, target_path)
            elif target_path.is_dir():
                for dirpath, _dirnames, filenames in os.walk(target_path):
                    for name in filenames:
                        p = Path(dirpath) / name
                        if p.is_symlink():
                            continue
                        _add_file(zf, root, p)
            # Non-existent targets are silently omitted; the agent will see the
            # absence and can create the file in the uploaded package.

    return buf.getvalue()


def _add_file(zf: zipfile.ZipFile, root: Path, path: Path) -> None:
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
    """Write all files atomically and return the relative paths applied.

    Validation must be done before calling this function. Each file is written
    to a sibling ``.perun-tmp`` file and renamed into place with ``os.replace``.
    Parent directories are created as needed.
    """
    root = Path(project["path"]).resolve()
    applied: list[str] = []
    for relpath, data in files.items():
        target = path_guard.resolve_target(root, relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".perun-tmp")
        try:
            tmp.write_bytes(data)
            os.replace(str(tmp), str(target))
        except Exception:
            # Best-effort cleanup of the temp file; the real error propagates.
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        applied.append(relpath)
    return applied


def _path_under(path: Path, parent: Path) -> bool:
    """Return True when *path* is *parent* or lives beneath it."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
