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


def test_pack_skips_sensitive_files(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "api.py").write_text("ok")
    (root / ".env").write_text("SECRET=1")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret")
    archive = package_handoff.pack(project, ["."])
    files = package_handoff.unpack(archive)
    assert "api.py" in files
    assert ".env" not in files
    assert ".git/config" not in files


def test_unpack_skips_directory_entries(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("dir/", "")
        zf.writestr("dir/file.txt", "ok")
    files = package_handoff.unpack(buf.getvalue())
    assert "dir/" not in files
    assert files["dir/file.txt"] == b"ok"


def test_pack_enforces_size_cap(tmp_path, monkeypatch):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "big.txt").write_text("0123456789")

    class SmallCaps:
        external_execution_max_package_bytes = 5
        external_execution_max_entry_bytes = 100
        external_execution_max_entries = 10

    monkeypatch.setattr(package_handoff, "_settings", lambda: SmallCaps())
    with pytest.raises(package_handoff.PackageError, match="exceeds max"):
        package_handoff.pack(project, ["big.txt"])


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


def test_derive_package_rejects_sensitive_extra_file(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "api.py").write_text("old", encoding="utf-8")
    proposal = action_proposals.create(
        project, "frambo",
        {"intent": "edit api", "operations": [{"path": "api.py", "op": "edit"}]},
    )

    _manifest, ops, errors = package_handoff.derive_package(
        project, proposal, {"api.py": b"ok", ".env": b"secret"})

    assert ops == []
    assert any(".env" in e for e in errors)


def test_derive_package_rejects_external_dependency_targets(tmp_path):
    project = _make_shared_project(tmp_path)
    dep = tmp_path / "dep"
    dep.mkdir()
    (dep / "lib.py").write_text("x", encoding="utf-8")
    data = manifest.from_project(project)
    data["shared"] = True
    data["participants"] = [{"agent_id": "frambo", "roles": ["rw"]}]
    data["external_deps"] = [{"path": str(dep), "access": "rw"}]
    manifest.save(project["path"], data, body="# p")
    project = registry.get_project(project["slug"])
    proposal = action_proposals.create(
        project, "frambo",
        {"intent": "edit dep", "operations": [{"path": str(dep / "lib.py"), "op": "edit"}]},
    )

    _manifest, ops, errors = package_handoff.derive_package(
        project, proposal, {"lib.py": b"new"})

    assert ops == []
    assert any("external dependency" in e for e in errors)


def test_apply_operations_rolls_back_on_midway_write_failure(tmp_path, monkeypatch):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "a.txt").write_text("old-a", encoding="utf-8")
    (root / "b.txt").write_text("old-b", encoding="utf-8")
    ops = [
        {"path": "a.txt", "op": "edit",
         "pre_sha256": package_handoff.sha256_hex(b"old-a"),
         "post_sha256": package_handoff.sha256_hex(b"new-a"), "size": 5},
        {"path": "b.txt", "op": "edit",
         "pre_sha256": package_handoff.sha256_hex(b"old-b"),
         "post_sha256": package_handoff.sha256_hex(b"new-b"), "size": 5},
    ]
    real_replace = package_handoff.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("boom")
        return real_replace(src, dst)

    monkeypatch.setattr(package_handoff.os, "replace", flaky_replace)
    with pytest.raises(OSError, match="boom"):
        package_handoff.apply_operations(project, ops, {"a.txt": b"new-a", "b.txt": b"new-b"})

    assert (root / "a.txt").read_text() == "old-a"
    assert (root / "b.txt").read_text() == "old-b"
    assert not list(root.glob("*.perun-tmp"))


def test_sha256_helper():
    assert package_handoff.sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

def test_unpack_rejects_case_insensitive_duplicates():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CANARY8.md", "one")
        zf.writestr("canary8.md", "two")
    with pytest.raises(package_handoff.PackageError):
        package_handoff.unpack(buf.getvalue())


@pytest.mark.parametrize("name", ["$HOME/x.md", "%TEMP%\\x.md"])
def test_unpack_rejects_shell_or_backslash_names(name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, "x")
    with pytest.raises(package_handoff.PackageError):
        package_handoff.unpack(buf.getvalue())

# --- typed manifest derivation (external handoff v2) ----------------------

def _proposal(project, action, targets):
    return action_proposals.create(
        project, "frambo",
        {"intent": "x", "operations": [
            {"path": str(path), "op": action} for path in targets
        ]},
    )


def test_derive_package_create_manifest(tmp_path):
    project = _make_shared_project(tmp_path)
    proposal = _proposal(project, "create", ["new.py"])
    files = {"new.py": b"hello"}

    manifest_obj, ops, errors = package_handoff.derive_package(project, proposal, files)

    assert errors == []
    assert manifest_obj["action_type"] == "create"
    assert ops == [{
        "path": "new.py", "op": "create", "pre_sha256": None,
        "post_sha256": package_handoff.sha256_hex(b"hello"), "size": 5,
    }]


def test_derive_package_edit_uses_current_hash(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "api.py").write_text("old", encoding="utf-8")
    proposal = _proposal(project, "edit", ["api.py"])

    _manifest_obj, ops, errors = package_handoff.derive_package(
        project, proposal, {"api.py": b"new"})

    assert errors == []
    assert ops[0]["op"] == "edit"
    assert ops[0]["pre_sha256"] == package_handoff.sha256_hex(b"old")
    assert ops[0]["post_sha256"] == package_handoff.sha256_hex(b"new")


def test_derive_package_delete_has_no_post_hash_and_ignores_bytes(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "gone.py").write_text("bye", encoding="utf-8")
    proposal = _proposal(project, "delete", ["gone.py"])

    _manifest_obj, ops, errors = package_handoff.derive_package(
        project, proposal, {"gone.py": b"ignored"})

    assert errors == []
    assert ops == [{
        "path": "gone.py", "op": "delete",
        "pre_sha256": package_handoff.sha256_hex(b"bye"),
        "post_sha256": None, "size": 3,
    }]


def test_derive_package_rejects_extra_files(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "api.py").write_text("old", encoding="utf-8")
    proposal = _proposal(project, "edit", ["api.py"])

    _manifest_obj, ops, errors = package_handoff.derive_package(
        project, proposal, {"api.py": b"new", "other.py": b"nope"})

    assert ops == []
    assert any("outside the approved targets" in e for e in errors)


def test_derive_package_refuses_edit_on_missing_target(tmp_path):
    project = _make_shared_project(tmp_path)
    proposal = _proposal(project, "edit", ["missing.py"])

    _manifest_obj, ops, errors = package_handoff.derive_package(
        project, proposal, {"missing.py": b"new"})

    assert ops == []
    assert any("does not exist" in e for e in errors)


def test_derive_package_refuses_create_over_existing_target(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "api.py").write_text("old", encoding="utf-8")
    proposal = _proposal(project, "create", ["api.py"])

    _manifest_obj, ops, errors = package_handoff.derive_package(
        project, proposal, {"api.py": b"new"})

    assert ops == []
    assert any("already exists" in e for e in errors)


def test_derive_package_refuses_unknown_operation(tmp_path):
    project = _make_shared_project(tmp_path)
    proposal = {
        "id": "prop_unknown",
        "operations": [{"path": "api.py", "op": "other"}],
    }

    _manifest_obj, ops, errors = package_handoff.derive_package(
        project, proposal, {"api.py": b"new"})

    assert ops == []
    assert any("unsupported operation" in e for e in errors)


def test_derive_package_refuses_a_directory_target(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "src").mkdir()
    proposal = _proposal(project, "edit", ["src"])

    _manifest_obj, ops, errors = package_handoff.derive_package(
        project, proposal, {})

    assert ops == []
    assert any("not a regular file" in e for e in errors)


def test_derive_package_refuses_claim_drift(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "api.py").write_text("claimed", encoding="utf-8")
    proposal = _proposal(project, "edit", ["api.py"])
    claim_hashes = {
        str(root / "api.py"): {
            "kind": "file", "size": 7,
            "sha256": package_handoff.sha256_hex(b"claimed"),
        }
    }
    (root / "api.py").write_text("changed", encoding="utf-8")

    _manifest_obj, ops, errors = package_handoff.derive_package(
        project, proposal, {"api.py": b"new"}, claim_hashes)

    assert ops == []
    assert any("changed since the claim" in e for e in errors)


def test_build_preview_produces_a_bounded_diff(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "api.py").write_text("a\nb\n", encoding="utf-8")
    _manifest_obj, ops, errors = package_handoff.derive_package(
        project, _proposal(project, "edit", ["api.py"]), {"api.py": b"a\nc\n"})
    assert errors == []

    preview = package_handoff.build_preview(project, ops, {"api.py": b"a\nc\n"})

    assert preview[0]["path"] == "api.py"
    assert "+1" in preview[0]["summary"] and "-1" in preview[0]["summary"]
    assert "-b" in preview[0]["diff"] and "+c" in preview[0]["diff"]


# --- typed application ----------------------------------------------------

def test_apply_operations_creates_edits_and_deletes(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "edit.py").write_text("old", encoding="utf-8")
    (root / "delete.py").write_text("bye", encoding="utf-8")
    files = {"create.py": b"created", "edit.py": b"new"}
    ops = [
        {"path": "create.py", "op": "create", "pre_sha256": None,
         "post_sha256": package_handoff.sha256_hex(b"created"), "size": 7},
        {"path": "edit.py", "op": "edit",
         "pre_sha256": package_handoff.sha256_hex(b"old"),
         "post_sha256": package_handoff.sha256_hex(b"new"), "size": 3},
        {"path": "delete.py", "op": "delete",
         "pre_sha256": package_handoff.sha256_hex(b"bye"),
         "post_sha256": None, "size": 3},
    ]

    applied = package_handoff.apply_operations(project, ops, files)

    assert sorted(applied) == ["create.py", "delete.py", "edit.py"]
    assert (root / "create.py").read_text() == "created"
    assert (root / "edit.py").read_text() == "new"
    assert not (root / "delete.py").exists()
    assert not list(root.glob("*.perun-tmp"))


def test_apply_operations_rejects_a_changed_preimage(tmp_path):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    path = root / "api.py"
    path.write_text("old", encoding="utf-8")
    ops = [{
        "path": "api.py", "op": "edit",
        "pre_sha256": package_handoff.sha256_hex(b"old"),
        "post_sha256": package_handoff.sha256_hex(b"new"), "size": 3,
    }]
    path.write_text("someone else", encoding="utf-8")

    with pytest.raises(package_handoff.PackageError, match="changed before apply"):
        package_handoff.apply_operations(project, ops, {"api.py": b"new"})
    assert path.read_text() == "someone else"


def test_apply_operations_rolls_back_when_a_postcondition_fails(
        tmp_path, monkeypatch):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "a.py").write_text("old-a", encoding="utf-8")
    (root / "b.py").write_text("old-b", encoding="utf-8")
    ops = [
        {"path": "a.py", "op": "edit",
         "pre_sha256": package_handoff.sha256_hex(b"old-a"),
         "post_sha256": package_handoff.sha256_hex(b"new-a"), "size": 5},
        {"path": "b.py", "op": "edit",
         "pre_sha256": package_handoff.sha256_hex(b"old-b"),
         "post_sha256": package_handoff.sha256_hex(b"new-b"), "size": 5},
    ]
    real_sha = package_handoff._file_sha256
    state = {"corrupt": False}

    def sha(path):
        digest = real_sha(path)
        if state["corrupt"] and path.name == "b.py":
            return "f" * 64
        return digest

    monkeypatch.setattr(package_handoff, "_file_sha256", sha)
    state["corrupt"] = False
    # Derive the operations normally, then corrupt only the postcondition.
    ops[0]["post_sha256"] = package_handoff.sha256_hex(b"new-a")
    ops[1]["post_sha256"] = package_handoff.sha256_hex(b"new-b")
    orig_apply = package_handoff.apply_operations

    # Flip corruption on after the writes start by wrapping the replace path.
    real_replace = package_handoff.os.replace
    calls = {"n": 0}

    def replace(src, dst):
        result = real_replace(src, dst)
        calls["n"] += 1
        if calls["n"] == 2:
            state["corrupt"] = True
        return result

    monkeypatch.setattr(package_handoff.os, "replace", replace)
    with pytest.raises(package_handoff.PackageError, match="postcondition hash mismatch"):
        orig_apply(project, ops, {"a.py": b"new-a", "b.py": b"new-b"})

    assert (root / "a.py").read_text() == "old-a"
    assert (root / "b.py").read_text() == "old-b"


def test_build_preview_does_not_load_a_huge_old_target(tmp_path, monkeypatch):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    path = root / "big.py"
    path.write_bytes(b"x" * 250_000)
    ops = [{
        "path": "big.py", "op": "edit",
        "pre_sha256": package_handoff.sha256_hex(b"x" * 250_000),
        "post_sha256": package_handoff.sha256_hex(b"new"), "size": 3,
    }]

    real_read = Path.read_bytes

    def guarded_read(self):
        if self.name == "big.py":
            raise AssertionError("preview loaded an oversized target")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    preview = package_handoff.build_preview(project, ops, {"big.py": b"new"})

    assert preview[0]["summary"].startswith("edit · binary")
    assert "250000" in preview[0]["summary"]


def test_apply_operations_rolls_back_a_delete_on_a_later_failure(tmp_path, monkeypatch):
    project = _make_shared_project(tmp_path)
    root = Path(project["path"])
    (root / "gone.txt").write_text("old-gone", encoding="utf-8")
    (root / "edit.txt").write_text("old-edit", encoding="utf-8")
    ops = [
        {"path": "gone.txt", "op": "delete",
         "pre_sha256": package_handoff.sha256_hex(b"old-gone"),
         "post_sha256": None, "size": 8},
        {"path": "edit.txt", "op": "edit",
         "pre_sha256": package_handoff.sha256_hex(b"old-edit"),
         "post_sha256": package_handoff.sha256_hex(b"new-edit"), "size": 8},
    ]
    def flaky_replace(src, dst):
        raise OSError("boom")

    monkeypatch.setattr(package_handoff.os, "replace", flaky_replace)
    with pytest.raises(OSError, match="boom"):
        package_handoff.apply_operations(
            project, ops, {"edit.txt": b"new-edit"})

    assert (root / "gone.txt").read_text() == "old-gone"
    assert (root / "edit.txt").read_text() == "old-edit"
    assert not list(root.glob("*.perun-tmp"))
