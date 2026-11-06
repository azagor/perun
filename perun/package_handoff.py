"""Safe ZIP work-package pack/unpack/validate/apply for external execution.

External rw agents receive a ZIP of the approved targets and upload a modified
ZIP in return. This module guards against traversal, zip-bombs, symlinks,
encrypted entries, path-escape, and writes outside the project allowlist.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from . import config, fs_audit, path_guard, shared_projects


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


_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _archive_path(proposal_id: str) -> Path:
    pid = str(proposal_id or "").strip()
    if not _PACKAGE_ID_RE.match(pid):
        raise PackageError("invalid proposal id for a package")
    return config.PACKAGES_DIR / f"{pid}.zip"


def store_archive(proposal_id: str, data: bytes) -> Path:
    """Atomically store a validated package for approval/apply."""
    path = _archive_path(proposal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.stem + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
    return path


def load_archive(proposal_id: str) -> bytes | None:
    path = _archive_path(proposal_id)
    try:
        return path.read_bytes()
    except (OSError, PackageError):
        return None


def discard_archive(proposal_id: str) -> None:
    try:
        _archive_path(proposal_id).unlink(missing_ok=True)
    except (OSError, PackageError):
        pass


def _target_rel(root: Path, target: str) -> tuple[Path, str]:
    resolved = path_guard.resolve_target(str(root), target)
    if not _path_under(resolved, root):
        raise PackageError(
            "external dependency targets are not supported in external handoff; "
            "use local execution")
    return resolved, resolved.relative_to(root).as_posix()


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

    # Never build an archive our own unpacker would reject: a project containing
    # `notes~backup.md` used to produce a package the claimant could not return.
    for p in selected:
        rel = p.relative_to(root).as_posix()
        if "\\" in rel or any(ch in rel for ch in "$%~"):
            raise PackageError(
                f"{rel} contains characters the handoff protocol cannot carry "
                "('$', '%', '~'); rename it or use a local run")

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
    seen_normalized: set[str] = set()
    seen_casefold: set[str] = set()
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
                if info.is_dir():
                    # The protocol transfers files only; a directory entry would
                    # otherwise be applied as an empty regular file. Skip before
                    # the name checks: "dir/" is not a file name.
                    continue

                # Dedupe on the *resolved* name, not the raw one: "api.py" and
                # "./api.py" both land on the same file and used to slip past as
                # two distinct entries (last one wins at apply time).
                normalized = posixpath.normpath(info.filename.replace("\\", "/"))
                if normalized != info.filename.replace("\\", "/"):
                    raise PackageError(f"non-canonical entry name: {info.filename}")
                if normalized in seen_normalized:
                    raise PackageError(f"duplicate entry name not allowed: {info.filename}")
                seen_normalized.add(normalized)
                lowered_name = normalized.casefold()
                if lowered_name in seen_casefold:
                    raise PackageError(
                        f"duplicate entry (case-insensitive): {info.filename}"
                    )
                seen_casefold.add(lowered_name)
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
                if "\\" in name or any(ch in name for ch in "$%~"):
                    raise PackageError(
                        f"entry name contains unsupported characters: {name}"
                    )
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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def derive_package(
    project: dict[str, Any],
    proposal: dict[str, Any],
    files: dict[str, bytes],
    claim_hashes: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Derive the typed operation manifest for an uploaded package.

    The proposal declares a per-target changeset; the external agent uploads
    file content only. Perun derives the pre/post hashes and refuses anything
    the changeset does not declare. At least one error means the package must be
    rejected without touching the project.
    """
    root = Path(project["path"]).resolve()
    raw_ops = proposal.get("operations")
    if isinstance(raw_ops, str):
        try:
            raw_ops = json.loads(raw_ops)
        except (TypeError, ValueError):
            raw_ops = []
    if not isinstance(raw_ops, list) or not raw_ops:
        return {}, [], ["a proposal must declare at least one operation"]
    if len(raw_ops) > 100:
        return {}, [], ["proposal has too many operations (max 100)"]

    cfg = shared_projects.load(root)
    external_deps = cfg.get("external_deps", [])
    plans: list[tuple[str, str, Path, str]] = []
    rels: set[str] = set()
    errors: list[str] = []
    for item in raw_ops:
        if not isinstance(item, dict):
            errors.append("each proposal operation must be an object")
            continue
        target = str(item.get("path") or "").strip()
        action = str(item.get("op") or "").strip().lower()
        if not target:
            errors.append("proposal operation path is required")
            continue
        if action not in ("edit", "create", "delete"):
            errors.append(f"unsupported operation {action!r} for {target}")
            continue
        try:
            resolved, rel = _target_rel(root, target)
        except PackageError as exc:
            errors.append(str(exc))
            continue
        if rel in rels:
            errors.append(f"duplicate target path: {rel}")
            continue
        rels.add(rel)
        if path_guard.is_sensitive_path(resolved):
            errors.append(f"{rel} resolves to a protected or sensitive path")
            continue
        ok, reason = path_guard.check(root, target, external_deps, require_write=True)
        if not ok:
            errors.append(reason)
            continue
        if resolved.is_symlink():
            errors.append(f"{rel} is a symlink; external handoff does not follow links")
            continue
        if resolved.exists() and not resolved.is_file():
            errors.append(
                f"{rel} is not a regular file; use local execution for directories")
            continue
        plans.append((target, rel, resolved, action))
    if errors:
        return {}, [], errors

    targets = [plan[0] for plan in plans]
    try:
        current = fs_audit.target_digests(project, targets)
    except Exception as exc:
        return {}, [], [f"could not fingerprint the targets: {exc}"]
    claims = claim_hashes if isinstance(claim_hashes, dict) else {}
    operations: list[dict[str, Any]] = []
    allowed = {plan[1] for plan in plans}
    extra = sorted(set(files) - allowed)
    if extra:
        errors.append(
            "package contains files outside the approved targets: " + ", ".join(extra[:5])
        )
        return {}, [], errors

    for target, rel, _resolved, action in plans:
        now = current.get(target)
        if claims:
            claimed = claims.get(target, "__missing__")
            if claimed == "__missing__" or now != claimed:
                errors.append(
                    f"{rel} changed since the claim; re-propose or re-claim the proposal")
                continue
        if action == "create":
            if now is not None:
                errors.append(
                    f"create target {rel} already exists; re-propose it as an edit")
                continue
            if rel not in files:
                errors.append(f"package is missing the new file {rel}")
                continue
            data = files[rel]
            operations.append({
                "path": rel, "op": "create", "pre_sha256": None,
                "post_sha256": sha256_hex(data), "size": len(data),
            })
        elif action == "edit":
            if not isinstance(now, dict) or now.get("kind") != "file":
                errors.append(
                    f"edit target {rel} does not exist or is not a regular file; "
                    "re-propose it as a create")
                continue
            if rel not in files:
                errors.append(f"package is missing the edited file {rel}")
                continue
            data = files[rel]
            operations.append({
                "path": rel, "op": "edit", "pre_sha256": now.get("sha256"),
                "post_sha256": sha256_hex(data), "size": len(data),
            })
        else:  # delete
            if not isinstance(now, dict) or now.get("kind") != "file":
                errors.append(f"delete target {rel} does not exist or is not a regular file")
                continue
            operations.append({
                "path": rel, "op": "delete", "pre_sha256": now.get("sha256"),
                "post_sha256": None,
                "size": int(now.get("size") or 0),
            })

    if errors:
        return {}, [], errors
    kinds = {plan[3] for plan in plans}
    manifest = {
        "version": 2,
        "proposal_id": str(proposal.get("id") or ""),
        "action_type": next(iter(kinds)) if len(kinds) == 1 else "mixed",
        "operations": operations,
    }
    return manifest, operations, errors


def build_preview(
    project: dict[str, Any],
    operations: list[dict[str, Any]],
    files: dict[str, bytes],
    *,
    max_chars: int = 120_000,
) -> list[dict[str, Any]]:
    """A bounded, human-readable diff/summary for package approval."""
    root = Path(project["path"]).resolve()
    preview: list[dict[str, Any]] = []
    used = 0
    for op in operations:
        rel = str(op.get("path") or "")
        kind = str(op.get("op") or "")
        try:
            target = path_guard.resolve_target(root, rel)
        except Exception:
            continue
        old = b""
        new = b""
        old_size = 0
        if kind in ("edit", "delete"):
            try:
                old_size = target.stat().st_size
            except OSError:
                old_size = 0
            # Do not load a target larger than the diff budget into memory just
            # to throw it away; the summary only needs its size.
            if old_size <= 200_000:
                try:
                    old = target.read_bytes()
                except OSError:
                    old = b""
        if kind in ("edit", "create"):
            new = files.get(rel, b"")
        entry: dict[str, Any] = {
            "path": rel, "op": kind, "size": len(new), "diff": "", "truncated": False,
        }
        if old_size > 200_000 or len(new) > 200_000 or b"\x00" in old or b"\x00" in new:
            entry["summary"] = (
                f"{kind} · binary · {old_size or len(old)} -> {len(new)} bytes"
            )
        else:
            try:
                old_text = old.decode("utf-8")
                new_text = new.decode("utf-8")
            except UnicodeDecodeError:
                entry["summary"] = f"{kind} · binary · {len(old)} -> {len(new)} bytes"
            else:
                diff_lines = list(difflib.unified_diff(
                    old_text.splitlines(), new_text.splitlines(),
                    fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="",
                ))
                adds = sum(1 for line in diff_lines
                           if line.startswith("+") and not line.startswith("+++"))
                dels = sum(1 for line in diff_lines
                           if line.startswith("-") and not line.startswith("---"))
                entry["summary"] = f"{kind} · +{adds}/-{dels} lines"
                budget = max(0, max_chars - used)
                text = "\n".join(diff_lines)
                if len(text) > min(budget, 20_000) or len(diff_lines) > 400:
                    text = "\n".join(diff_lines[:400])
                    if len(text) > min(budget, 20_000):
                        text = text[:min(budget, 20_000)]
                    entry["truncated"] = True
                entry["diff"] = text
                used += len(text)
        preview.append(entry)
        if used >= max_chars:
            break
    return preview


def apply_operations(
    project: dict[str, Any],
    operations: list[dict[str, Any]],
    files: dict[str, bytes],
) -> list[str]:
    """Apply a validated typed manifest atomically with rollback.

    Every operation re-checks its pre-image and post-image here, immediately
    before writing; a failed postcondition rolls the whole package back.
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
        prepared: list[tuple[dict[str, Any], Path, bytes | None]] = []
        for op in operations:
            rel = str(op.get("path") or "")
            kind = str(op.get("op") or "")
            if kind not in ("create", "edit", "delete"):
                raise PackageError(f"unsupported operation {kind!r} for {rel}")
            target = _target_for_apply(root, rel)
            if target.is_symlink():
                raise PackageError(f"{rel} is a symlink; refusing to write")
            if path_guard.is_sensitive_path(target):
                raise PackageError(f"{rel} resolves to a protected or sensitive path")
            exists = target.is_file()
            if kind == "create":
                if exists:
                    raise PackageError(f"create target {rel} already exists")
                data = files.get(rel)
                if data is None:
                    raise PackageError(f"package is missing {rel}")
                if sha256_hex(data) != str(op.get("post_sha256") or ""):
                    raise PackageError(f"content hash mismatch for {rel}")
            elif kind == "edit":
                if not exists:
                    raise PackageError(f"edit target {rel} no longer exists")
                if _file_sha256(target) != str(op.get("pre_sha256") or ""):
                    raise PackageError(f"{rel} changed before apply")
                data = files.get(rel)
                if data is None:
                    raise PackageError(f"package is missing {rel}")
                if sha256_hex(data) != str(op.get("post_sha256") or ""):
                    raise PackageError(f"content hash mismatch for {rel}")
            else:  # delete
                if not exists:
                    raise PackageError(f"delete target {rel} no longer exists")
                if _file_sha256(target) != str(op.get("pre_sha256") or ""):
                    raise PackageError(f"{rel} changed before delete")
                data = None
            prepared.append((op, target, data))

        for op, target, data in prepared:
            rel = target.relative_to(root).as_posix()
            if data is not None:
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

        for op, target, data in prepared:
            rel = target.relative_to(root).as_posix()
            if str(op.get("op")) == "delete":
                target.unlink()
                applied.append(rel)
                continue
            staged = new_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=target.stem + ".", suffix=".perun-tmp", dir=target.parent
            )
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                shutil.copy2(staged, tmp)
                os.replace(str(tmp), str(target))
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
            applied.append(rel)

        # Postconditions are part of the transaction: a bad result rolls back.
        for op, target, _data in prepared:
            rel = target.relative_to(root).as_posix()
            kind = str(op.get("op"))
            if kind == "delete":
                if target.exists():
                    raise PackageError(f"delete postcondition failed for {rel}")
            elif not target.is_file():
                raise PackageError(f"write postcondition failed for {rel}")
            elif _file_sha256(target) != str(op.get("post_sha256") or ""):
                raise PackageError(f"postcondition hash mismatch for {rel}")
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
