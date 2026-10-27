"""Tests for the external-execution ZIP handoff module."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from perun import action_proposals, fs_audit, manifest, package_handoff, registry, shared_projects


def _make_shared_project(tmp_path, name="pkg-test", participants=None):
    (tmp_path / name).mkdir(parents=True)
    project = registry.add_project(name, str(tmp_path / name))
    data = manifest.from_project(project)
    data["shared"] = True
    data["participants"] = participants or [{"agent_id": "frambo", "roles": ["rw"]}]
    manifest.save(tmp_path / name, data, body=f"# {name}")
    # Refresh project so path is a Path in tests.
    project = registry.get_project(project["slug"])
    return project


def test_pack_project_root_roundtrip(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "src" / "a.py").parent.mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('a')")
    (root / "README.md").write_text("# hello")

    archive = package_handoff.pack(project, ["src", "README.md"])
    files = package_handoff.unpack(archive)
    assert set(files.keys()) == {"src/a.py", "README.md"}
    assert files["src/a.py"].decode() == "print('a')"


def test_unpack_rejects_traversal(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "bad")
    with pytest.raises(package_handoff.PackageError):
        package_handoff.unpack(buf.getvalue())


def test_unpack_rejects_absolute_path(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/etc/passwd", "bad")
    with pytest.raises(package_handoff.PackageError):
        package_handoff.unpack(buf.getvalue())


def test_unpack_rejects_drive_letter(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("C:evil.txt", "bad")
    with pytest.raises(package_handoff.PackageError):
        package_handoff.unpack(buf.getvalue())


def test_unpack_rejects_symlink(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link")
        import stat as _stat
        info.external_attr = (_stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "target")
    with pytest.raises(package_handoff.PackageError):
        package_handoff.unpack(buf.getvalue())


def test_unpack_rejects_bad_zip(tmp_path):
    with pytest.raises(package_handoff.PackageError):
        package_handoff.unpack(b"not a zip")


def test_unpack_enforces_entry_count_cap(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(3):
            zf.writestr(f"file{i}.txt", "x")
    with pytest.raises(package_handoff.PackageError):
        package_handoff.unpack(buf.getvalue(), max_entries=2)


def test_unpack_enforces_entry_size_cap(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("big.txt", "x" * 20)
    with pytest.raises(package_handoff.PackageError):
        package_handoff.unpack(buf.getvalue(), max_entry_bytes=10)


def test_unpack_enforces_total_size_cap(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "x" * 10)
        zf.writestr("b.txt", "y" * 10)
    with pytest.raises(package_handoff.PackageError):
        package_handoff.unpack(buf.getvalue(), max_package_bytes=15)


def test_unpack_rejects_duplicate_entry_names(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("same.txt", "first")
        zf.writestr("same.txt", "second")
    with pytest.raises(package_handoff.PackageError) as exc_info:
        package_handoff.unpack(buf.getvalue())
    assert "duplicate" in str(exc_info.value).lower()


def test_validate_package_allows_target_path(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "api.py").write_text("old")
    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "edit api", "targets": ["api.py"], "action_type": "edit"},
    )
    proposal = action_proposals.approve(proposal["id"], "owner")
    files = {"api.py": b"new"}
    errors = package_handoff.validate_package(project, proposal, files)
    assert errors == []


def test_validate_package_rejects_sensitive_path(tmp_path):
    project = _make_shared_project(tmp_path)
    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "steal secrets", "targets": ["api.py"], "action_type": "edit"},
    )
    files = {"api.py": b"ok", ".env": b"secret"}
    errors = package_handoff.validate_package(project, proposal, files)
    assert any(".env" in e for e in errors)


def test_validate_package_rejects_outside_targets(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "api.py").write_text("old")
    (root / "other.py").write_text("old")
    proposal = action_proposals.create(
        project,
        "frambo",
        {"intent": "edit api", "targets": ["api.py"], "action_type": "edit"},
    )
    files = {"other.py": b"new"}
    errors = package_handoff.validate_package(project, proposal, files)
    assert any("not under any approved" in e for e in errors)


def test_apply_package_writes_atomically(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "api.py").write_text("old")
    files = {"api.py": b"new", "src/new.py": b"created"}
    applied = package_handoff.apply_package(project, files)
    assert sorted(applied) == ["api.py", "src/new.py"]
    assert (root / "api.py").read_text() == "new"
    assert (root / "src" / "new.py").read_text() == "created"
    # No leftover temp files.
    assert not list(root.glob("*.perun-tmp"))


def test_sha256_helper():
    assert package_handoff.sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
