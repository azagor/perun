"""Git integration tests (require the ``git`` CLI)."""

from __future__ import annotations

import pytest

from perun import git

pytestmark = pytest.mark.skipif(not git.available(), reason="git not installed")


def test_validate_remote_accepts_supported_forms():
    assert git.validate_remote("https://github.com/a/b.git").endswith(".git")
    assert git.validate_remote("git@github.com:a/b.git") == "git@github.com:a/b.git"
    assert git.validate_remote("ssh://git@host/a/b").startswith("ssh://")


def test_validate_remote_rejects_bad_forms():
    for bad in ("", "   ", "-flag", "not a url", "ftp://x/y"):
        with pytest.raises(git.GitError):
            git.validate_remote(bad)


def test_init_commit_log(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    info = git.init(d, "main")
    assert info["repo"] is True
    assert info["branch"] == "main"

    (d / "a.txt").write_text("hello", encoding="utf-8")
    res = git.commit(d, "first commit")
    assert res["committed"] is True
    assert res["sha"]

    commits = git.log(d)
    assert commits and commits[0]["subject"] == "first commit"

    # Nothing changed → no new commit.
    again = git.commit(d, "noop")
    assert again["committed"] is False
    assert "nothing to commit" in again["reason"]


def test_set_remote_and_info(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    git.init(d)
    git.set_remote(d, "https://example.com/x/y.git", "main")
    info = git.info(d)
    assert info["has_remote"] is True
    assert info["remote_url"].endswith("y.git")


def test_push_without_remote_raises(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    git.init(d)
    (d / "a").write_text("x", encoding="utf-8")
    git.commit(d, "c")
    with pytest.raises(git.GitError):
        git.push(d)


def test_info_on_plain_directory(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    info = git.info(d)
    assert info["repo"] is False
    assert info["exists"] is True
    assert info["available"] is True


def test_init_rejects_existing_repo(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    git.init(d)
    with pytest.raises(git.GitError):
        git.init(d)


def test_clone_local_repository(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    git.init(src)
    (src / "file.txt").write_text("data", encoding="utf-8")
    git.commit(src, "init")

    dest = tmp_path / "cloned"
    info = git.clone(str(src), dest)
    assert info["repo"] is True
    assert (dest / "file.txt").read_text(encoding="utf-8") == "data"
