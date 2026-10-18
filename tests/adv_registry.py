"""Aggressive (adversarial) tests for the project registry layer.

Scope: ``perun/registry.py``, ``perun/db.py``, ``perun/projects.py`` and
``perun/discovery.py`` — project matching by cwd, slugs, SQLite behaviour and
directory scanning.

Conventions
-----------
* Tests only use the fixtures from ``tests/conftest.py`` (``PERUN_HOME`` is a
  throwaway dir, the schema is recreated and wiped before every test).
* A FAILING test is a *finding*, not a broken test.  Every failing test carries
  a comment saying what the author considers correct behaviour.  Never adjust an
  assertion to match buggy behaviour.

Run:
    /tmp/perun-exp-venv/bin/python -m pytest tests/adv_registry.py -q -p no:cacheprovider
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from perun import config, db, discovery, projects, registry


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def isolated_db(monkeypatch, tmp_path):
    """Point PERUN_HOME at a brand-new directory for DB-level tests.

    ``perun.config`` reads ``PERUN_HOME`` at import time, so the paths are
    monkeypatched on the module instead of via the environment.
    """
    home = tmp_path / "perun-home"

    def _use(path: Path, *, create: bool = False):
        monkeypatch.setattr(config, "HOME", path)
        monkeypatch.setattr(config, "DB_PATH", path / "perun.db")
        monkeypatch.setattr(config, "CONFIG_PATH", path / "config.json")
        monkeypatch.setattr(config, "LOGS_DIR", path / "logs")
        monkeypatch.setattr(config, "RUNS_DIR", path / "runs")
        if create:
            path.mkdir(parents=True, exist_ok=True)

    _use(home, create=True)
    return home


def _mk(path: Path, *files: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel in files:
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x", encoding="utf-8")
    return path


def _symlink(target: Path, link: Path) -> None:
    """Create a directory symlink; skip on platforms/filesystems without them."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform guard
        pytest.skip("symlinks are not available on this platform")
    if not link.is_symlink():  # pragma: no cover - platform guard
        pytest.skip("symlinks are not available on this platform")


# =========================================================================== #
# (a) matching a project to the current working directory
# =========================================================================== #
def test_lookup_exact_path(workdir):
    proj = _mk(workdir / "exact", "opencode.json")
    p = registry.add_project("Exact", str(proj))
    found = registry.get_project_by_path(str(proj))
    assert found is not None and found["slug"] == p["slug"]


def test_lookup_path_with_trailing_slash(workdir):
    proj = _mk(workdir / "trail", "opencode.json")
    registry.add_project("Trail", str(proj) + "/")
    assert registry.get_project_by_path(str(proj)) is not None
    assert registry.get_project_by_path(str(proj) + "/") is not None


def test_lookup_with_dotdot_segments(workdir):
    proj = _mk(workdir / "dd", "opencode.json")
    _mk(proj / "sub")
    registry.add_project("DD", str(proj))
    assert registry.get_project_by_path(str(proj / "sub" / "..")) is not None


def test_lookup_is_case_sensitive_on_posix_two_distinct_projects(workdir):
    """``Foo`` and ``foo`` are two different directories on Linux.

    ``os.path.normcase`` is the identity on POSIX, so each path must resolve to
    its own project and the case-flipped path must NOT resolve.  On Windows
    (normcase lowercases) the code intentionally folds case — this test
    documents that the Linux behaviour is exact-match only.
    """
    upper = _mk(workdir / "Foo", "opencode.json")
    lower = _mk(workdir / "foo", "opencode.json")
    pu = registry.add_project("Foo", str(upper))
    pl = registry.add_project("foo", str(lower))

    assert pu["path"] != pl["path"]
    assert registry.get_project_by_path(str(upper))["slug"] == pu["slug"]
    assert registry.get_project_by_path(str(lower))["slug"] == pl["slug"]
    if os.name != "nt":
        # On POSIX nothing matches a differently-cased path.
        assert registry.get_project_by_path(str(workdir / "FOO")) is None


def test_lookup_via_windows_style_normcase_is_windows_only():
    """Regression note for tests/test_db_registry.py::test_get_project_by_path_is_normcase.

    That test registers ``C:\\Apps\\Proj`` and looks up ``c:\\apps\\proj``, which
    only works where ``os.path.normcase`` folds case *and* rewrites separators.
    On Linux ``normcase`` is the identity and ``abspath`` merely prepends the
    cwd, so the two strings never match.  After the normcase-only comparison,
    the call must also survive the lookup on the "wrong" platform instead of
    resolving to a bogus project.
    """
    if os.name == "nt":
        pytest.skip("Windows-specific behaviour")
    registry.add_project("Proj", r"C:\Apps\Proj")
    assert os.path.normcase("A") == "A"  # normcase is the identity on POSIX
    found = registry.get_project_by_path(r"c:\apps\proj")
    assert found is None, (
        "on POSIX a differently-cased / Windows-style path must not resolve; "
        "a match would mean the lookup silently mangles the path"
    )


def test_lookup_from_relative_path(workdir, monkeypatch):
    """A relative cwd is resolved against the process cwd before matching."""
    proj = _mk(workdir / "rel", "opencode.json")
    registry.add_project("Rel", str(proj))
    monkeypatch.chdir(proj)
    assert registry.get_project_by_path(".") is not None
    # A relative path pointing at a *different* directory (the parent) must not
    # accidentally match this project.
    assert registry.get_project_by_path("..") is None


def test_cwd_inside_project_resolves_to_the_project(workdir):
    """A session started in a subdirectory belongs to the enclosing project.

    Expected: the registry walks up the path (``<project>/src/foo`` -> project
    registered at ``<project>``), because that is what OpenCode users do — they
    run commands from a subdirectory of the project.  Today only an exact path
    match is implemented, so the lookup fails.
    """
    proj = _mk(workdir / "anc", "opencode.json")
    _mk(proj / "src" / "deep")
    registry.add_project("Anc", str(proj))
    found = registry.get_project_by_path(str(proj / "src" / "deep"))
    assert found is not None, (
        "BŁĄD: cwd w podkatalogu projektu nie znajduje projektu-przodka; "
        "oczekiwane: dopasowanie po najdłuższym zarejestrowanym prefiksie ścieżki"
    )


def test_cwd_reached_through_symlink_resolves_to_project(workdir):
    """Users reach projects through symlinks (``~/work`` -> ``/mnt/data``).

    Expected: the lookup resolves symlinks (``realpath``) before comparing, so
    the same project is found no matter which spelling of the path is used.
    """
    proj = _mk(workdir / "real", "opencode.json")
    link = workdir / "alias"
    _symlink(proj, link)
    registry.add_project("Real", str(proj))
    found = registry.get_project_by_path(str(link))
    assert found is not None, (
        "BŁĄD: ścieżka przez dowiązanie symboliczne nie dopasowuje projektu; "
        "oczekiwane: realpath() przed porównaniem (inaczej ten sam projekt "
        "dostaje dwa wpisy w rejestrze)"
    )


def test_nested_projects_exact_path_wins(workdir):
    """Two registered projects, one inside the other: the exact path wins."""
    outer = _mk(workdir / "outer", "opencode.json")
    inner = _mk(outer / "inner", "opencode.json")
    registry.add_project("Outer", str(outer))
    pi = registry.add_project("Inner", str(inner))

    assert registry.get_project_by_path(str(inner))["slug"] == pi["slug"]
    assert registry.get_project_by_path(str(outer))["slug"] == "outer"


def test_register_or_update_of_subdirectory_does_not_duplicate_project(workdir):
    """``projects.register_or_update`` on a subdir of an existing project.

    Expected: the enclosing project is updated (or the call is rejected), not a
    second project for the same working tree.  Today a new row appears, so one
    project ends up with two slugs and separate session/memory counters.
    """
    outer = _mk(workdir / "proj", "opencode.json")
    _mk(outer / "src")
    projects.register_or_update("Proj", str(outer))
    _, created = projects.register_or_update("Proj Sub", str(outer / "src"))
    assert created is False, (
        "BŁĄD: podkatalog zarejestrowanego projektu tworzy drugi projekt "
        "(duplikat sesji/pamięci); oczekiwane: aktualizacja projektu-przodka"
    )


def test_trailing_slash_registration_updates_instead_of_duplicating(workdir):
    """A trailing slash must not create a second project (abspath normalizes it)."""
    proj = _mk(workdir / "norm", "opencode.json")
    p1, created1 = projects.register_or_update("Norm", str(proj))
    p2, created2 = projects.register_or_update("Norm", str(proj) + "/")
    assert created1 is True and created2 is False
    assert p1["slug"] == p2["slug"]
    assert len(registry.list_projects()) == 1


# =========================================================================== #
# (b) slugs
# =========================================================================== #
def test_slugify_transliterates_polish_diacritics():
    """Polish letters must survive slugification (transliterated).

    Expected: ``Zażółć Gęślą Jaźń`` -> ``zazolc-gesla-jazn``.  The current
    regex keeps only ``[a-z0-9]``, so every Polish letter becomes a separator
    and the slug turns into ``za-g-l-ja`` — unusable for reading and highly
    collision-prone for Polish project names.
    """
    assert registry.slugify("Zażółć Gęślą Jaźń") == "zazolc-gesla-jazn"


def test_slugify_polish_only_name_is_not_near_empty():
    """``Łódź`` must not collapse to a 1-character slug."""
    slug = registry.slugify("Łódź")
    assert len(slug) >= 3, (
        f"BŁĄD: slug dla 'Łódź' = {slug!r}; oczekiwane: transliteracja 'lodz' "
        "(nazwa polska daje nieczytelny, kolizyjny slug)"
    )


def test_slugify_emoji_only_name_is_distinguishable():
    """An emoji-only name must not produce the same slug as an empty name."""
    assert registry.slugify("🚀") != "project", (
        "BŁĄD: nazwa z samych emoji daje slug 'project' — identyczny jak dla "
        "nazwy pustej; oczekiwane: slug wyprowadzony z nazwy (np. transliteracja "
        "lub fallback oparty o nazwę, nie stała)"
    )
    assert registry.slugify("🚀") != registry.slugify("")


def test_slugify_mixed_emoji_and_ascii():
    assert registry.slugify("Rakieta 🚀 2") == "rakieta-2"


def test_slugify_never_returns_path_traversal():
    for raw in ("..", "../../etc/passwd", "///", "/etc/shadow", "..\\..\\win"):
        slug = registry.slugify(raw)
        assert "/" not in slug and "\\" not in slug and ".." not in slug
        assert slug


def test_slugify_empty_name_falls_back_to_placeholder():
    """The documented fallback keeps the DB insert working."""
    assert registry.slugify("") == "project"
    assert registry.slugify("   ") == "project"


def test_slug_collision_between_different_names_gets_suffix(workdir):
    a = registry.add_project("Foo Bar", str(workdir / "a"))
    b = registry.add_project("foo-bar", str(workdir / "b"))
    assert a["slug"] == "foo-bar"
    assert b["slug"] == "foo-bar-2"


def test_two_polish_named_projects_get_meaningful_slugs(workdir):
    """Integration check: Polish project names through the real registry."""
    p1 = registry.add_project("Łódź", str(workdir / "lodz"))
    p2 = registry.add_project("Kraków", str(workdir / "krakow"))
    assert p1["slug"] == "lodz"
    assert p2["slug"] == "krakow"


def test_empty_project_name_should_be_rejected(workdir):
    """A nameless project must not be silently accepted.

    Expected: ``add_project("")`` raises (name is required by the API/wizard).
    Today a project called "" is inserted with the placeholder slug
    ``project``, which then shows up in the dashboard as a blank card.
    """
    with pytest.raises(ValueError):
        registry.add_project("", str(workdir / "anon"))


def test_long_name_round_trips_and_slug_is_bounded(workdir):
    long_name = "X" * 300
    p = registry.add_project(long_name, str(workdir / "long"))
    assert registry.get_project(p["slug"])["name"] == long_name
    assert len(p["slug"]) <= 60, (
        f"BŁĄD: slug ma {len(p['slug'])} znaków — brak limitu długości; "
        "oczekiwane: slug nadający się do URL/nazwy pliku (np. <= 60 znaków)"
    )


def test_slug_candidates_are_capped_at_20(workdir):
    """The 22nd project with the same name cannot be created.

    Expected: registration keeps working (counter unbounded / timestamped
    suffix).  Today ``add_project`` only tries ``base`` + ``base-2..base-21``
    and then raises ``ValueError``, so a legitimate 22nd project is refused.
    """
    slugs = []
    for i in range(22):
        slugs.append(registry.add_project("Same", str(workdir / f"s{i}"))["slug"])
    assert len(set(slugs)) == 22


def test_duplicate_path_raises_a_path_error_not_a_slug_error(workdir):
    """Registering the same path twice must say *that*, not blame slugs.

    ``projects.path`` is UNIQUE, but ``add_project`` only catches
    ``IntegrityError`` in order to try the next slug candidate — so a duplicate
    path burns all 21 candidates and then reports "could not generate a unique
    project slug", which sends the caller hunting for a non-existent slug clash.
    """
    path = str(workdir / "dup")
    registry.add_project("Dup", path)
    with pytest.raises(ValueError) as err:
        registry.add_project("Dup", path)
    assert "path" in str(err.value).lower(), (
        f"BŁĄD: komunikat {str(err.value)!r} obwinia slug, a kolizja dotyczy "
        "ścieżki (path UNIQUE); oczekiwane: błąd o już zarejestrowanej ścieżce"
    )


def test_numeric_slug_is_reachable_through_get_project(workdir):
    """A project named ``123`` gets the slug ``123`` and must be retrievable.

    ``get_project`` treats any all-digit string as a *row id*, so the slug
    ``123`` can never be looked up (and a numeric id lookup may even return an
    unrelated project).  Expecting the slug form to work.
    """
    p = registry.add_project("123", str(workdir / "digits"))
    assert p["slug"] == "123"
    found = registry.get_project("123")
    assert found is not None and found["slug"] == "123", (
        "BŁĄD: get_project('123') nie znajduje projektu o slugu '123' "
        "(cyfrowy slug jest nierozróżnialny od id); oczekiwane: najpierw slug, "
        "potem ewentualnie id"
    )


# =========================================================================== #
# (c) database layer
# =========================================================================== #
def test_eight_threads_write_concurrently(isolated_db):
    """Eight threads inserting at once must all succeed (own connection each)."""
    db.init_db()
    errors: list[str] = []
    barrier = threading.Barrier(8)

    def work(i: int) -> None:
        try:
            barrier.wait(timeout=20)
            registry.add_project(f"Thread {i}", f"/tmp/adv/thread-{i}")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    assert len(registry.list_projects()) == 8


def test_concurrent_same_name_slug_race(isolated_db):
    """The read-then-insert slug race must resolve without losing a project."""
    db.init_db()
    errors: list[str] = []
    barrier = threading.Barrier(8)

    def work(i: int) -> None:
        try:
            barrier.wait(timeout=20)
            registry.add_project("Race", f"/tmp/adv/race-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    slugs = {p["slug"] for p in registry.list_projects()}
    assert len(slugs) == 8


def test_migration_adds_columns_to_old_schema(workdir):
    """An old DB (pre-manifest/pre-git columns) is upgraded in place, keeping data."""
    old = workdir / "old.db"
    conn = sqlite3.connect(str(old))
    conn.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            path TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_uid TEXT UNIQUE NOT NULL,
            project_id INTEGER NOT NULL,
            prompt TEXT NOT NULL,
            status TEXT DEFAULT 'queued'
        );
        """
    )
    conn.execute(
        "INSERT INTO projects (slug,name,path,created_at,updated_at) VALUES (?,?,?,?,?)",
        ("legacy", "Legacy", "/tmp/adv/legacy", "2024-01-01", "2024-01-01"),
    )
    conn.commit()
    conn.close()

    conn = db.connect(old)
    db._migrate(conn)  # type: ignore[arg-type]
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
    conn.commit()
    row = conn.execute("SELECT slug, manifest, git_remote FROM projects").fetchone()
    conn.close()

    assert {"manifest", "manifest_path", "manifest_loaded_at", "git_remote",
            "git_branch", "git_provider"} <= cols
    assert "model_explicit" in run_cols
    assert row["slug"] == "legacy" and row["manifest"] == ""


def test_schema_has_a_version_marker(isolated_db):
    """The schema must be versioned so migrations are unambiguous.

    Expected: ``PRAGMA user_version`` > 0 or a ``schema_version`` table.  Today
    versioning is implicit: ``_migrate`` guesses by inspecting
    ``PRAGMA table_info`` on every start, so a future migration that is not
    "add/drop a column" has no way to know whether it already ran.
    """
    db.init_db()
    conn = db.connect()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert version > 0 or "schema_version" in tables, (
        "BŁĄD: brak wersjonowania schematu (user_version=0, brak tabeli "
        "schema_version) — migracje nie są w stanie rozpoznać, co już wykonano"
    )


def test_migrate_tolerates_duplicate_column_race():
    """Two Perun processes starting at once must not crash on ``ALTER TABLE``.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``: if a second process (or the MCP
    server) added the column between our ``PRAGMA table_info`` and our
    ``ALTER TABLE``, SQLite raises ``OperationalError: duplicate column name``.
    ``_migrate`` only catches that exception for DROP COLUMN, so the whole
    startup fails.  Expected: the same tolerance as for the DROP COLUMN path.
    """

    class _Cursor:
        def __init__(self, rows=()):
            self._rows = rows

        def __iter__(self):
            return iter(self._rows)

        def fetchall(self):
            return self._rows

    class _Conn:
        def execute(self, sql, params=()):
            if "PRAGMA table_info" in sql:
                return _Cursor([{"name": "id"}, {"name": "slug"}])
            if "ADD COLUMN" in sql:
                raise sqlite3.OperationalError("duplicate column name: manifest")
            return _Cursor([])

    db._migrate(_Conn())  # type: ignore[arg-type]  # must not raise


def test_readonly_database_reads_and_refuses_writes(isolated_db):
    """A read-only DB file must still serve reads and fail loudly on writes."""
    db.init_db()
    registry.add_project("RO", "/tmp/adv/ro")

    snapshot = isolated_db / "snapshot.db"
    shutil.copy(config.DB_PATH, snapshot)
    os.chmod(snapshot, 0o444)
    try:
        conn = db.connect(snapshot)
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO projects (slug,name,path,created_at,updated_at) "
                "VALUES ('x','X','/tmp/adv/x','t','t')"
            )
        conn.close()
    finally:
        os.chmod(snapshot, 0o644)


def test_missing_perun_home_directory_is_created(monkeypatch, tmp_path):
    """``db.connect`` must create a missing PERUN_HOME instead of failing."""
    home = tmp_path / "no" / "such" / "perun-home"
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "DB_PATH", home / "perun.db")
    monkeypatch.setattr(config, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(config, "RUNS_DIR", home / "runs")

    db.init_db()
    assert (home / "perun.db").is_file()
    conn = db.connect()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "projects" in names


def test_connect_does_not_commit_open_transactions_silently(isolated_db):
    """``db.db()`` commits on success and rolls back on error (documented contract)."""
    db.init_db()
    with pytest.raises(RuntimeError):
        with db.db() as conn:
            conn.execute(
                "INSERT INTO projects (slug,name,path,created_at,updated_at) "
                "VALUES ('roll','R','/tmp/adv/roll','t','t')"
            )
            raise RuntimeError("boom")
    assert registry.get_project("roll") is None


# =========================================================================== #
# (d) discovery / scanning
# =========================================================================== #
def test_scan_of_2000_directories_is_fast(workdir):
    root = workdir / "many"
    root.mkdir(parents=True)
    for i in range(2000):
        (root / f"d{i:04d}").mkdir()
    (root / "d0000" / "opencode.json").write_text("{}", encoding="utf-8")

    started = time.monotonic()
    found = discovery.scan([str(root)], max_depth=1)
    elapsed = time.monotonic() - started

    assert len(found) == 2000
    assert elapsed < 10, f"skan 2000 katalogów trwał {elapsed:.1f}s"
    assert found[0]["kind"] == "opencode"  # opencode projects sort first


def test_symlink_cycle_does_not_loop(workdir):
    a = workdir / "cycle" / "a"
    b = workdir / "cycle" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _symlink(b, a / "to_b")
    _symlink(a, b / "to_a")

    started = time.monotonic()
    found = discovery.scan([str(workdir / "cycle")], max_depth=50)
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"skan z dowiązaniami cyklicznymi trwał {elapsed:.1f}s"
    assert len(found) < 10  # (dev, ino) dedupe must collapse the cycle


def test_scan_survives_unreadable_subdirectory(workdir):
    """A directory without read permission must be skipped, not abort the scan.

    ``capabilities()`` stats ``<cand>/.git`` etc. without any ``try/except``
    (perun/discovery.py:55) and ``Path.exists()`` propagates ``PermissionError``
    on a dir with mode 000, so ``scan()`` dies and the dashboard's discovery
    returns nothing at all just because *one* directory is unreadable.
    """
    if os.name == "nt" or not hasattr(os, "geteuid"):  # pragma: no cover - POSIX-only
        pytest.skip("POSIX permission bits are not enforced on Windows")
    if os.geteuid() == 0:  # pragma: no cover - root ignores permissions
        pytest.skip("running as root: permissions are not enforced")
    root = workdir / "perm"
    (root / "ok").mkdir(parents=True)
    (root / "ok" / "opencode.json").write_text("{}", encoding="utf-8")
    locked = root / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        found = discovery.scan([str(root)], max_depth=1)
    finally:
        locked.chmod(0o755)

    names = {i["name"] for i in found}
    assert "ok" in names, (
        "BŁĄD: katalog bez uprawnień przerywa cały scan (PermissionError z "
        "capabilities()); oczekiwane: pominięcie takiego katalogu"
    )


def test_discovered_symlinked_project_keeps_the_real_path(workdir):
    """A project reachable only through a symlink must not be stored as the link.

    Expected: the registered path is the resolved target (``realpath``) — the
    symlink path breaks as soon as the link is recreated, and the same project
    can end up registered twice (once per spelling).
    """
    outside = _mk(workdir / "target", "opencode.json")
    root = workdir / "roots"
    root.mkdir()
    _symlink(outside, root / "linked")

    discovery.discover_and_sync([str(root)], max_depth=1)

    stored = [p["path"] for p in registry.list_projects()]
    assert stored, "discovery zarejestrowała projekt przez dowiązanie"
    for path in stored:
        assert os.path.realpath(path) == path, (
            f"BŁĄD: zarejestrowano ścieżkę przez dowiązanie ({path}); "
            "oczekiwane: ścieżka rzeczywista (realpath)"
        )


def test_scan_stays_inside_the_root_through_symlinks(workdir):
    """The documented promise: symlinks under a root do not escape the root."""
    outside = _mk(workdir / "outside" / "proj", "opencode.json")
    root = workdir / "roots2"
    (root / "here").mkdir(parents=True)
    _symlink(outside.parent, root / "escape")

    found = discovery.scan([str(root)], max_depth=3)
    for info in found:
        assert os.path.abspath(info["path"]).startswith(str(root)), (
            f"BŁĄD: skan wyszedł poza root: {info['path']}"
        )


def test_project_deleted_from_disk_is_flagged_or_pruned(workdir):
    """A registry entry whose directory vanished must not look healthy.

    Expected: discovery reports entries whose path no longer exists (so the
    dashboard can mark them, and the user can prune them).  Today the entry
    stays silently, still counted in ``list_projects()`` and offered as a
    target for runs that then fail in the filesystem.
    """
    gone = _mk(workdir / "gone", "opencode.json")
    discovery.discover_and_sync([str(workdir)], max_depth=1)
    assert registry.get_project_by_path(str(gone)) is not None
    shutil.rmtree(gone)

    result = discovery.discover_and_sync([str(workdir)], max_depth=1)
    stale = set(result.get("missing") or result.get("stale") or [])
    assert stale, (
        "BŁĄD: projekt usunięty z dysku pozostaje w rejestrze bez żadnego "
        "oznaczenia; oczekiwane: wynik skanu zawiera listę nieistniejących "
        "wpisów (np. klucz 'missing')"
    )


def test_discover_and_sync_is_idempotent(workdir):
    _mk(workdir / "alpha", "opencode.json")
    _mk(workdir / "beta", "opencode.json")
    first = discovery.discover_and_sync([str(workdir)], max_depth=1)
    second = discovery.discover_and_sync([str(workdir)], max_depth=1)
    assert len(first["created"]) == 2
    assert second["created"] == []
    assert len(registry.list_projects()) == 2


# =========================================================================== #
# (e) projects.py helpers
# =========================================================================== #
def test_name_from_repo_variants():
    assert projects.name_from_repo("https://github.com/a/b.git") == "b"
    assert projects.name_from_repo("git@github.com:a/b.git") == "b"
    assert projects.name_from_repo("https://github.com/a/b/") == "b"
    assert projects.name_from_repo("") == "project"


def test_register_or_update_keeps_existing_values_on_empty_input(workdir):
    proj = _mk(workdir / "keep", "opencode.json")
    first, created = projects.register_or_update(
        "Keep", str(proj), description="opis", model="prov/model")
    assert created is True
    second, created2 = projects.register_or_update("", str(proj))
    assert created2 is False
    assert second["slug"] == first["slug"]
    assert second["description"] == "opis"
    assert second["model"] == "prov/model"
