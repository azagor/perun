"""Tests for perun.grep search layer."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from perun import grep


class _FakePipe:
    def __init__(self, lines=None, err=""):
        self._lines = list(lines or [])
        self._err = err
        self._idx = 0

    def readline(self):
        if self._idx < len(self._lines):
            line = self._lines[self._idx]
            self._idx += 1
            return line
        return ""

    def read(self):
        return self._err

    def close(self):
        pass


class _FakeProc:
    def __init__(self, lines=None, err="", returncode=0, timeout=False):
        self.stdout = _FakePipe(lines, err)
        self.stderr = _FakePipe([], err)
        self._returncode = returncode
        self._timeout = timeout
        self._killed = False
        self._timeout_calls = 0

    def kill(self):
        self._killed = True

    def wait(self, timeout=None):
        if self._timeout and self._timeout_calls == 0:
            self._timeout_calls += 1
            raise subprocess.TimeoutExpired("cmd", timeout)
        return -1 if self._killed else self._returncode


def test_capabilities_returns_dict():
    caps = grep.search_capabilities()
    assert isinstance(caps, dict)
    assert "rg" in caps
    assert "ast_grep" in caps


def test_parse_rg_json(tmp_path: Path) -> None:
    lines = [
        json.dumps({"type": "match", "data": {
            "path": {"text": str(tmp_path / "a.py")},
            "line_number": 7,
            "lines": {"text": "hello world\n"},
            "submatches": [{"match": {"text": "world"}, "start": 6}],
        }}),
        json.dumps({"type": "summary", "data": {"stats": {}}}),
    ]
    matches = grep._parse_rg_json("\n".join(lines), tmp_path)
    assert len(matches) == 1
    assert matches[0]["file"] == "a.py"
    assert matches[0]["line"] == 7
    assert matches[0]["column"] == 6
    assert matches[0]["match"] == "world"


def test_parse_ast_grep_json(tmp_path: Path) -> None:
    lines = [
        json.dumps({"file": str(tmp_path / "a.py"), "matches": [
            {"range": {"start": {"line": 3, "column": 4}}, "text": "foo()"}
        ]}),
    ]
    matches = grep._parse_ast_grep_json("\n".join(lines), tmp_path)
    assert len(matches) == 1
    assert matches[0]["file"] == "a.py"
    assert matches[0]["line"] == 3
    assert matches[0]["column"] == 4


def test_python_grep_finds_pattern(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "rg_available", lambda: False)
    (tmp_path / "src.py").write_text("foo = 1\nbar = 2\nfoo += 3\n")
    result = grep.rg_search("foo", tmp_path, max_results=10)
    assert result["ok"]
    assert result["tool"] == "python-grep"
    assert len(result["matches"]) == 2
    assert all(m["file"] == "src.py" for m in result["matches"])


def test_python_grep_case_insensitive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "rg_available", lambda: False)
    (tmp_path / "src.py").write_text("FOO\nfoo\n")
    result = grep.rg_search("foo", tmp_path, case_sensitive=False, max_results=10)
    assert result["ok"]
    assert len(result["matches"]) == 2


def test_python_grep_whole_word(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "rg_available", lambda: False)
    (tmp_path / "src.py").write_text("foo\nfood\nbar foo\n")
    result = grep.rg_search("foo", tmp_path, whole_word=True, max_results=10)
    assert result["ok"]
    assert len(result["matches"]) == 2


def test_python_grep_include_filter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "rg_available", lambda: False)
    (tmp_path / "a.py").write_text("target\n")
    (tmp_path / "b.js").write_text("target\n")
    result = grep.rg_search("target", tmp_path, include=".*\\.py", max_results=10)
    assert result["ok"]
    assert len(result["matches"]) == 1
    assert result["matches"][0]["file"] == "a.py"


def test_python_grep_respects_max_results(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "rg_available", lambda: False)
    (tmp_path / "src.py").write_text("\n".join(["foo"] * 20))
    result = grep.rg_search("foo", tmp_path, max_results=5)
    assert result["ok"]
    assert len(result["matches"]) == 5


def test_python_grep_invalid_root() -> None:
    result = grep.rg_search("foo", "/nonexistent/path")
    assert not result["ok"]
    assert "not a directory" in result["error"]


def test_python_ast_search_finds_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "ast_grep_available", lambda: False)
    (tmp_path / "a.py").write_text("def f():\n    print('hi')\n")
    result = grep.ast_grep_search("Call", tmp_path, max_results=10)
    assert result["ok"]
    assert result["tool"] == "python-ast"
    assert any(m["match"] == "Call" for m in result["matches"])


def test_python_ast_search_respects_max_results(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "ast_grep_available", lambda: False)
    (tmp_path / "a.py").write_text("\n".join(["x = 1"] * 50))
    result = grep.ast_grep_search("Assign", tmp_path, max_results=5)
    assert result["ok"]
    assert len(result["matches"]) == 5


@pytest.mark.skipif(not grep.rg_available(), reason="ripgrep not installed")
def test_rg_search_live(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hello world\nfoo bar\n")
    result = grep.rg_search("hello", tmp_path, max_results=10)
    assert result["ok"]
    assert result["tool"] == "rg"
    assert any(m["match"] == "hello" for m in result["matches"])


@pytest.mark.skipif(not grep.ast_grep_available(), reason="ast-grep not installed")
def test_ast_grep_search_live(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def foo():\n    pass\n\nfoo()\n")
    result = grep.ast_grep_search("$F($$$ARGS)", tmp_path, max_results=10)
    assert result["ok"]
    assert result["tool"] == "ast-grep"


def test_invalid_regex_returns_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "rg_available", lambda: False)
    result = grep.rg_search("[invalid", tmp_path)
    assert not result["ok"]
    assert "invalid regex" in result["error"]


def test_binary_file_skipped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "rg_available", lambda: False)
    (tmp_path / "bin.dat").write_bytes(b"\x00needle\x00")
    result = grep.rg_search("needle", tmp_path, max_results=10)
    assert result["ok"]
    assert result["matches"] == []


def test_oversized_file_skipped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "rg_available", lambda: False)
    (tmp_path / "big.txt").write_text("needle\n" * (grep.MAX_FILE_BYTES + 1))
    result = grep.rg_search("needle", tmp_path, max_results=10)
    assert result["ok"]
    assert len(result["matches"]) == 0


def test_glob_include_converted_to_regex_in_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "rg_available", lambda: False)
    (tmp_path / "a.py").write_text("needle\n")
    (tmp_path / "b.js").write_text("needle\n")
    result = grep.rg_search("needle", tmp_path, include="*.py", max_results=10)
    assert result["ok"]
    assert len(result["matches"]) == 1
    assert result["matches"][0]["file"] == "a.py"


def test_rg_global_passes_paths_positionally_without_files_from(
    tmp_path: Path, monkeypatch
) -> None:
    """Global search passes every project path as a positional argument.

    ripgrep has no ``--files-from`` flag, so there is no path-file shortcut: an
    oversized command line simply fails and degrades to the Python fallback.
    """
    monkeypatch.setattr(grep, "rg_available", lambda: True)
    monkeypatch.setattr(grep, "_is_project_path_safe", lambda p, roots=None: True)

    captured_argv: list[list[str]] = []

    def fake_run(argv, cwd=None, timeout=30.0):
        captured_argv.append(list(argv))
        return True, "", 0

    monkeypatch.setattr(grep, "_run", fake_run)

    projects = []
    for i in range(100):
        name = f"proj_{i}_{'a' * 100}"
        d = tmp_path / name
        d.mkdir()
        projects.append({"slug": f"p{i}", "path": str(d)})

    result = grep.rg_search_global("needle", projects, max_results=10)
    assert result["ok"]
    assert result["projects_searched"] == 100
    assert len(captured_argv) == 1
    argv = captured_argv[0]
    # No unsupported flag; every project path is passed positionally.
    assert "--files-from" not in argv
    for p in projects:
        assert str(Path(p["path"]).resolve()) in argv


def test_global_search_counts_valid_projects(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "rg_available", lambda: False)
    monkeypatch.setattr(grep, "_is_project_path_safe", lambda p, roots=None: True)

    valid1 = tmp_path / "valid1"
    valid2 = tmp_path / "valid2"
    valid1.mkdir()
    valid2.mkdir()

    projects = [
        {"slug": "a", "path": str(valid1)},
        {"slug": "b", "path": str(valid2)},
        {"slug": "no_path"},
        {"slug": "missing", "path": str(tmp_path / "missing")},
    ]

    result = grep.rg_search_global("needle", projects, max_results=10)
    assert result["ok"]
    assert result["projects_searched"] == 2


def test_run_nonzero_exit_failure(monkeypatch) -> None:
    def fake_popen(*args, **kwargs):
        return _FakeProc(lines=["partial stdout\n"], err="error text", returncode=1)

    monkeypatch.setattr(grep.subprocess, "Popen", fake_popen)
    ok, output, returncode = grep._run(["dummy"])
    assert not ok
    assert returncode == 1
    assert ("partial stdout" in output) or ("error text" in output)


def test_run_timeout(monkeypatch) -> None:
    def fake_popen(*args, **kwargs):
        return _FakeProc(lines=["line\n"], err="timeout", returncode=0, timeout=True)

    monkeypatch.setattr(grep.subprocess, "Popen", fake_popen)
    ok, output, returncode = grep._run(["dummy"], timeout=0.01)
    assert not ok
    assert returncode < 0 or returncode == 0  # killed path returns -1


def test_rg_search_no_matches_is_not_failure(tmp_path: Path, monkeypatch) -> None:
    """ripgrep exit 1 (no matches) returns an empty result without falling back."""
    monkeypatch.setattr(grep, "rg_available", lambda: True)
    monkeypatch.setattr(grep, "_run", lambda *a, **k: (False, "", 1))

    called = {"python": False}
    real = grep._python_grep

    def spy(*a, **k):
        called["python"] = True
        return real(*a, **k)

    monkeypatch.setattr(grep, "_python_grep", spy)
    result = grep.rg_search("needle", tmp_path)
    assert result["ok"] is True
    assert result["matches"] == []
    assert result["tool"] == "rg"
    assert called["python"] is False


def test_rg_global_no_matches_is_not_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "rg_available", lambda: True)
    monkeypatch.setattr(grep, "_is_project_path_safe", lambda p, roots=None: True)
    monkeypatch.setattr(grep, "_run", lambda *a, **k: (False, "", 1))

    d = tmp_path / "proj"
    d.mkdir()
    called = {"python": False}
    real = grep._python_grep

    def spy(*a, **k):
        called["python"] = True
        return real(*a, **k)

    monkeypatch.setattr(grep, "_python_grep", spy)
    result = grep.rg_search_global("needle", [{"slug": "p", "path": str(d)}])
    assert result["ok"] is True
    assert result["matches"] == []
    assert result["tool"] == "rg-global"
    assert called["python"] is False


def test_rg_search_error_falls_back_to_python(tmp_path: Path, monkeypatch) -> None:
    """A genuine ripgrep error (exit 2) still falls back to the Python search."""
    (tmp_path / "a.py").write_text("needle\n")
    monkeypatch.setattr(grep, "rg_available", lambda: True)
    monkeypatch.setattr(grep, "_run", lambda *a, **k: (False, "boom", 2))

    result = grep.rg_search("needle", tmp_path)
    assert result["ok"] is True
    assert result["tool"] == "python-grep"
    assert len(result["matches"]) == 1


def test_ast_grep_search_no_matches_is_not_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "ast_grep_available", lambda: True)
    monkeypatch.setattr(grep, "_ast_grep_cmd", lambda: "sg")
    monkeypatch.setattr(grep, "_run", lambda *a, **k: (False, "", 1))

    called = {"python": False}

    def spy(*a, **k):
        called["python"] = True
        return []

    monkeypatch.setattr(grep, "_python_ast_search", spy)
    result = grep.ast_grep_search("$F($$$A)", tmp_path)
    assert result["ok"] is True
    assert result["matches"] == []
    assert result["tool"] == "ast-grep"
    assert called["python"] is False


def test_ast_grep_global_no_matches_is_not_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "ast_grep_available", lambda: True)
    monkeypatch.setattr(grep, "_ast_grep_cmd", lambda: "sg")
    monkeypatch.setattr(grep, "_is_project_path_safe", lambda p, roots=None: True)
    monkeypatch.setattr(grep, "_run", lambda *a, **k: (False, "", 1))

    d = tmp_path / "proj"
    d.mkdir()
    called = {"python": False}

    def spy(*a, **k):
        called["python"] = True
        return []

    monkeypatch.setattr(grep, "_python_ast_search", spy)
    result = grep.ast_grep_search_global("$F($$$A)", [{"slug": "p", "path": str(d)}])
    assert result["ok"] is True
    assert result["matches"] == []
    assert result["tool"] == "ast-grep-global"
    assert called["python"] is False


def test_ast_grep_global_passes_paths_positionally(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(grep, "ast_grep_available", lambda: True)
    monkeypatch.setattr(grep, "_ast_grep_cmd", lambda: "sg")
    monkeypatch.setattr(grep, "_is_project_path_safe", lambda p, roots=None: True)

    captured: list[list[str]] = []

    def fake_run(argv, cwd=None, timeout=30.0):
        captured.append(list(argv))
        return True, "", 0

    monkeypatch.setattr(grep, "_run", fake_run)

    projects = []
    for i in range(100):
        d = tmp_path / f"proj_{i}_{'a' * 100}"
        d.mkdir()
        projects.append({"slug": f"p{i}", "path": str(d)})

    result = grep.ast_grep_search_global("$F($$$A)", projects, max_results=10)
    assert result["ok"]
    assert len(captured) == 1
    argv = captured[0]
    assert "--files-from" not in argv
    for p in projects:
        assert str(Path(p["path"]).resolve()) in argv
