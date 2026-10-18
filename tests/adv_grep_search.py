"""Agresywne testy dla perun.grep i perun.search (adv-grep-search).

Zakres: ścieżki (path traversal, dowiązania, NUL), wzorce (ReDoS, nieprawidłowe,
puste, wielkość liter, unicode), pliki (binarne, duże, liczne, bez uprawnień,
zepsute UTF-8, CRLF), katalogi ignorowane (.git, node_modules, venv, .gitignore)
oraz search.py (ranking RRF, limity, puste zapytania).

Testy, które padają, opisują ZACHOWANIE UWAŻANE ZA POPRAWNE. Nie dopasowujemy
asercji do błędnej implementacji — padający test jest znaleziskiem.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

import perun.config as perun_config
import perun.registry as perun_registry
from perun import grep, search

REPO_ROOT = Path(__file__).resolve().parents[1]
RG_AVAILABLE = grep.rg_available()

rg_only = pytest.mark.skipif(not RG_AVAILABLE, reason="ripgrep nie jest zainstalowany")

# --------------------------------------------------------------------------- #
# Pomocnicze
# --------------------------------------------------------------------------- #


def _force_engine(monkeypatch, name: str) -> None:
    """Wymuś silnik: 'rg' lub 'python' (fallback czysto pythonowy)."""
    monkeypatch.setattr(grep, "rg_available", lambda: name == "rg")


def _force_python(monkeypatch) -> None:
    _force_engine(monkeypatch, "python")


def _force_rg(monkeypatch) -> None:
    _force_engine(monkeypatch, "rg")


def _files(result: dict) -> set[str]:
    return {m["file"] for m in result.get("matches", [])}


def _symlink(target, link) -> None:
    """Create a symlink, skipping the test where the OS does not allow it.

    Windows without Developer Mode / admin rights raises WinError 1314 for
    unprivileged symlink creation — an environment limit, not a Perun defect.
    """
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"system nie pozwala na dowiązania symboliczne: {exc}")


def _wire_project(monkeypatch, tmp_path: Path, name: str = "proj"):
    """Udawany wpis rejestru + dozwolony korzeń dla testów search.py."""
    proj_dir = tmp_path / name
    proj_dir.mkdir(parents=True, exist_ok=True)
    proj = {"id": 1, "slug": name, "name": name, "path": str(proj_dir)}
    monkeypatch.setattr(perun_registry, "list_projects", lambda: [proj])
    monkeypatch.setattr(
        perun_config, "settings", lambda: types.SimpleNamespace(roots=[str(tmp_path)])
    )
    return proj, proj_dir


# --------------------------------------------------------------------------- #
# (a) Ścieżki: traversal, dowiązania, ścieżka absolutna, NUL
# --------------------------------------------------------------------------- #


@rg_only
def test_a01_dotdot_root_does_not_escape_on_rg(tmp_path, monkeypatch):
    # Poprawne zachowanie: korzeń "proj/.." nie może rozszerzyć zakresu
    # przeszukiwania poza wskazany katalog (tu: poza proj/).
    _force_rg(monkeypatch)
    base = tmp_path / "base"
    proj = base / "proj"
    proj.mkdir(parents=True)
    (base / "outside.txt").write_text("SECRET\n", encoding="utf-8")
    (proj / "inside.txt").write_text("SECRET\n", encoding="utf-8")

    result = grep.rg_search("SECRET", proj / "..", max_results=10)
    assert _files(result) == {"inside.txt"}, "korzeń z '..' wyszedł poza zadany katalog"


def test_a02_dotdot_root_does_not_escape_python_fallback(tmp_path, monkeypatch):
    # To samo dla fallbacku pythonowego.
    _force_python(monkeypatch)
    base = tmp_path / "base"
    proj = base / "proj"
    proj.mkdir(parents=True)
    (base / "outside.txt").write_text("SECRET\n", encoding="utf-8")
    (proj / "inside.txt").write_text("SECRET\n", encoding="utf-8")

    result = grep.rg_search("SECRET", proj / "..", max_results=10)
    assert _files(result) == {"inside.txt"}, "korzeń z '..' wyszedł poza zadany katalog"


@rg_only
def test_a03_absolute_root_outside_project_roots_refused(tmp_path, monkeypatch):
    # Poprawne zachowanie: ścieżka absolutna spoza skonfigurowanych korzeni
    # projektów powinna zostać odrzucona (tak jak robi to _is_project_path_safe
    # w ścieżce globalnej), a nie przeszukana.
    _force_rg(monkeypatch)
    monkeypatch.setattr(
        perun_config,
        "settings",
        lambda: types.SimpleNamespace(roots=[str(tmp_path / "allowed")]),
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOPSECRET\n", encoding="utf-8")

    result = grep.rg_search("TOPSECRET", outside, max_results=10)
    assert result.get("ok") is False, "przeszukano katalog spoza dozwolonych korzeni"


def test_a04_is_project_path_safe_fails_open_without_roots(monkeypatch):
    # Poprawne zachowanie: brak skonfigurowanych korzeni nie może oznaczać
    # "wszystko wolno" — brama bezpieczeństwa nie powinna przepuszczać /etc.
    monkeypatch.setattr(perun_config, "settings", lambda: types.SimpleNamespace(roots=[]))
    assert grep._is_project_path_safe({"path": "/etc"}) is False


def test_a05_project_path_safe_rejects_symlink_escape(tmp_path):
    # Kontrola pozytywna: dowiązanie symboliczne wychodzące poza korzeń
    # jest (poprawnie) odrzucane.
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x\n", encoding="utf-8")
    _symlink(outside, root / "link")

    assert grep._is_project_path_safe({"path": str(root / "link")}, [str(root)]) is False


def test_a06_symlinked_file_outside_root_not_read_python_fallback(tmp_path, monkeypatch):
    # Poprawne zachowanie: plik-dowiązanie wskazujący poza korzeń nie powinien
    # być czytany (rg domyślnie go pomija). Fallback pythonowy podąża za nim.
    _force_python(monkeypatch)
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRETNEEDLE\n", encoding="utf-8")
    _symlink(outside / "secret.txt", root / "link.txt")

    result = grep.rg_search("SECRETNEEDLE", root, max_results=10)
    assert _files(result) == set(), "przeczytano zawartość pliku spoza korzenia"


def test_a07_symlinked_dir_outside_root_not_followed(tmp_path, monkeypatch):
    # Kontrola pozytywna dla obu silników: katalog-dowiązanie nie jest
    # przechodzony.
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "deep.txt").write_text("SECRETNEEDLE\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "plain.txt").write_text("plain\n", encoding="utf-8")
    _symlink(outside, root / "linkdir")

    for engine in ("rg", "python") if RG_AVAILABLE else ("python",):
        _force_engine(monkeypatch, engine)
        result = grep.rg_search("SECRETNEEDLE", root, max_results=10)
        assert _files(result) == set(), f"silnik {engine} wszedł do dowiązanego katalogu"


def test_a08_nul_in_root_path_returns_error_not_crash(tmp_path, monkeypatch):
    # Kontrola pozytywna: nazwa z "\0" nie może wywrócić funkcji — ma zwrócić
    # błąd wejścia.
    _force_python(monkeypatch)
    result = grep.rg_search("x", str(tmp_path) + "\0evil", max_results=5)
    assert result.get("ok") is False


def test_a09_nul_in_pattern_is_reported_not_silently_downgraded(tmp_path, monkeypatch):
    # Poprawne zachowanie: wzorzec z "\0" jest nieprawidłowy — powinien zostać
    # zgłoszony jako invalid_input. Obserwowane: rg zawodzi, a funkcja po cichu
    # przechodzi na fallback pythonowy (inna semantyka: limity rozmiaru,
    # ignorowanie plików, brak --crlf) i zwraca "ok" z pustym wynikiem.
    _force_rg(monkeypatch)
    (tmp_path / "a.txt").write_text("NEEDLE\n", encoding="utf-8")
    result = grep.rg_search("NEEDLE\0X", tmp_path, max_results=5)
    assert result.get("tool") == "rg" or result.get("ok") is False, (
        f"ciche przejście na inny silnik: {result.get('tool')}"
    )


# --------------------------------------------------------------------------- #
# (b) Wzorce: ReDoS, nieprawidłowy, pusty, wielkość liter, unicode
# --------------------------------------------------------------------------- #


def test_b01_catastrophic_regex_has_no_timeout_in_python_fallback(tmp_path, monkeypatch):
    # Poprawne zachowanie: przeszukiwanie powinno mieć limit czasu/kroków
    # (jak rg), a nie wisieć wykładniczo na (a+)+$.
    _force_python(monkeypatch)
    (tmp_path / "x.txt").write_text("a" * 40 + "!\n", encoding="utf-8")
    child = (
        "import sys;"
        f"sys.path.insert(0, {str(REPO_ROOT)!r});"
        "from perun import grep;"
        "grep.rg_available = lambda: False;"
        f"print(len(grep.rg_search('(a+)+$', {str(tmp_path)!r}, max_results=5)['matches']))"
    )
    start = time.monotonic()
    try:
        subprocess.run(
            [sys.executable, "-c", child],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        pytest.fail(
            f"katastrofalny regex bez limitu czasu: przerwano po {elapsed:.1f}s "
            "(poprawne zachowanie: timeout lub limit kroków silnika)"
        )
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"katastrofalny regex trwał {elapsed:.1f}s (ReDoS)"


@rg_only
def test_b02_catastrophic_regex_rg_engine_is_fast(tmp_path, monkeypatch):
    # Kontrola pozytywna: silnik rg jest liniowy i radzi sobie z (a+)+$.
    _force_rg(monkeypatch)
    (tmp_path / "x.txt").write_text("a" * 40 + "!\n", encoding="utf-8")
    start = time.monotonic()
    result = grep.rg_search("(a+)+$", tmp_path, max_results=5)
    assert time.monotonic() - start < 10.0
    assert result["ok"] is True and result["tool"] == "rg"


def test_b03_invalid_regex_returns_error(tmp_path, monkeypatch):
    _force_python(monkeypatch)
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    assert grep.rg_search("(unclosed", tmp_path, max_results=5).get("ok") is False


def test_b04_empty_pattern_rejected(tmp_path, monkeypatch):
    # Poprawne zachowanie: pusty wzorzec powinien być odrzucony jako
    # invalid_input — tak jak robią to search_all_projects i unified_search.
    # Obserwowane: fallback pythonowy traktuje "" jako regex dopasowujący każdą
    # linię i zwraca "ok" z pełnym zbiorem trafień.
    _force_python(monkeypatch)
    (tmp_path / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    result = grep.rg_search("", tmp_path, max_results=10)
    assert result.get("ok") is False and result.get("error_kind") == "invalid_input"


@rg_only
def test_b05_case_sensitivity_respected(tmp_path, monkeypatch):
    _force_rg(monkeypatch)
    (tmp_path / "a.txt").write_text("Foo\nfoo\n", encoding="utf-8")
    assert len(grep.rg_search("foo", tmp_path, max_results=10)["matches"]) == 1
    assert len(
        grep.rg_search("foo", tmp_path, case_sensitive=False, max_results=10)["matches"]
    ) == 2


def test_b06_unicode_text_file_is_searchable_python_fallback(tmp_path, monkeypatch):
    # Poprawne zachowanie: plik tekstowy UTF-8 z literami spoza ASCII (polskie,
    # cyrylica, emoji) musi być przeszukiwany. Obserwowane: heurystyka
    # _is_binary uznaje bajty kontynuacji UTF-8 z zakresu 0x7f-0x9f za binaria,
    # więc plik w ogóle nie jest czytany, a wynik jest pusty.
    _force_python(monkeypatch)
    (tmp_path / "pl.txt").write_text("żółć\n", encoding="utf-8")
    (tmp_path / "ru.txt").write_text("привет\n", encoding="utf-8")
    assert len(grep.rg_search("żółć", tmp_path, max_results=10)["matches"]) == 1
    assert len(grep.rg_search("привет", tmp_path, max_results=10)["matches"]) == 1


def test_b07_unicode_case_insensitive_python_fallback(tmp_path, monkeypatch):
    # Poprawne zachowanie: IGNORECASE dla liter nie-ASCII (polskie znaki).
    _force_python(monkeypatch)
    (tmp_path / "u.txt").write_text("żółć\nŻÓŁĆ\n", encoding="utf-8")
    result = grep.rg_search("ŻÓŁĆ", tmp_path, case_sensitive=False, max_results=10)
    assert len(result["matches"]) == 2


@rg_only
def test_b08_unicode_case_insensitive_rg(tmp_path, monkeypatch):
    # Kontrola pozytywna dla rg.
    _force_rg(monkeypatch)
    (tmp_path / "u.txt").write_text("żółć\nŻÓŁĆ\n", encoding="utf-8")
    result = grep.rg_search("ŻÓŁĆ", tmp_path, case_sensitive=False, max_results=10)
    assert len(result["matches"]) == 2


def test_b09_whole_word_with_alternation_python_fallback(tmp_path, monkeypatch):
    # Poprawne zachowanie: whole_word musi objąć CAŁY wzorzec, np. \b(?:foo|bar)\b.
    # Obserwowane: sklejenie "\b" + "foo|bar" + "\b" daje \bfoo|bar\b, więc
    # alternatywa wymyka się ograniczeniu słowa i "foobar" pasuje.
    _force_python(monkeypatch)
    (tmp_path / "w.txt").write_text("foobar\n", encoding="utf-8")
    result = grep.rg_search("foo|bar", tmp_path, whole_word=True, max_results=10)
    assert result["matches"] == []


def test_b10_include_is_glob_not_regex_python_fallback(tmp_path, monkeypatch):
    # Poprawne zachowanie: parametr include to glob (tak jak -g w rg), więc
    # "a.py" nie może dopasować pliku "axpy" przez kropkę-regex.
    _force_python(monkeypatch)
    (tmp_path / "a.py").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "axpy").write_text("NEEDLE\n", encoding="utf-8")
    result = grep.rg_search("NEEDLE", tmp_path, include="a.py", max_results=10)
    assert _files(result) == {"a.py"}


def test_b11_include_glob_star_python_fallback(tmp_path, monkeypatch):
    # Kontrola pozytywna: include="*.py" (glob) działa.
    _force_python(monkeypatch)
    (tmp_path / "a.py").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("NEEDLE\n", encoding="utf-8")
    result = grep.rg_search("NEEDLE", tmp_path, include="*.py", max_results=10)
    assert _files(result) == {"a.py"}


# --------------------------------------------------------------------------- #
# (c) Pliki: binarne, duże, liczne, bez uprawnień, zepsute UTF-8, CRLF
# --------------------------------------------------------------------------- #


def test_c01_binary_10mb_skipped(tmp_path, monkeypatch):
    blob = b"\x00" * (10 * 1024 * 1024) + b"\nNEEDLE\n"
    (tmp_path / "bin.dat").write_bytes(blob)
    (tmp_path / "txt.txt").write_text("NEEDLE\n", encoding="utf-8")
    for engine in ("rg", "python") if RG_AVAILABLE else ("python",):
        _force_engine(monkeypatch, engine)
        assert _files(grep.rg_search("NEEDLE", tmp_path, max_results=10)) == {"txt.txt"}


@rg_only
def test_c02_50mb_text_file_searched_in_time(tmp_path, monkeypatch):
    # Poprawne zachowanie: duży plik tekstowy jest przeszukiwany w rozsądnym
    # czasie (rg), bez wieszania się.
    _force_rg(monkeypatch)
    big = tmp_path / "big.txt"
    with open(big, "wb") as fh:
        fh.write(b"x" * (50 * 1024 * 1024))
        fh.write(b"\nNEEDLE\n")
    start = time.monotonic()
    result = grep.rg_search("NEEDLE", tmp_path, max_results=10)
    elapsed = time.monotonic() - start
    assert elapsed < 30.0
    assert _files(result) == {"big.txt"}


def test_c03_large_text_file_not_silently_skipped_python_fallback(tmp_path, monkeypatch):
    # Poprawne zachowanie: przekroczenie MAX_FILE_BYTES (5 MB) powinno być
    # zgłoszone (flaga/pole w wyniku) albo limit powinien być konfigurowalny —
    # nie może po cichu zgubić trafienia, skoro silnik rg je znajduje.
    _force_python(monkeypatch)
    big = tmp_path / "big.txt"
    with open(big, "wb") as fh:
        fh.write(b"x" * (6 * 1024 * 1024))
        fh.write(b"\nNEEDLE\n")
    result = grep.rg_search("NEEDLE", tmp_path, max_results=10)
    assert _files(result) == {"big.txt"}, "6 MB plik tekstowy pominięty bez żadnej informacji"


@rg_only
def test_c04_big_file_engines_agree(tmp_path, monkeypatch):
    # Spójność silników: ten sam zbiór trafień dla pliku 6 MB, który przekracza
    # limit fallbacku pythonowego.
    big = tmp_path / "big.txt"
    with open(big, "wb") as fh:
        fh.write(b"x" * (6 * 1024 * 1024))
        fh.write(b"\nNEEDLE\n")
    _force_rg(monkeypatch)
    rg_files = _files(grep.rg_search("NEEDLE", tmp_path, max_results=10))
    _force_python(monkeypatch)
    py_files = _files(grep.rg_search("NEEDLE", tmp_path, max_results=10))
    assert rg_files == py_files, f"rg={rg_files} python={py_files}"


def test_c05_ten_thousand_files_capped_and_fast(tmp_path, monkeypatch):
    # Poprawne zachowanie: limit liczby wyników działa i wyszukiwanie 10 000
    # plików nie trwa minut.
    for i in range(10000):
        (tmp_path / f"f{i:05d}.txt").write_text("NEEDLE\n", encoding="utf-8")
    for engine in ("rg", "python") if RG_AVAILABLE else ("python",):
        _force_engine(monkeypatch, engine)
        start = time.monotonic()
        result = grep.rg_search("NEEDLE", tmp_path, max_results=50)
        elapsed = time.monotonic() - start
        assert elapsed < 30.0, f"silnik {engine}: {elapsed:.1f}s"
        assert len(result["matches"]) == 50, f"silnik {engine}: limit nie zadziałał"


def test_c06_max_results_zero_returns_nothing_python_fallback(tmp_path, monkeypatch):
    # Poprawne zachowanie: max_results=0 => zero trafień (silnik rg zwraca 0).
    # Obserwowane: fallback pythonowy zwraca 1 trafienie, bo sprawdzenie limitu
    # następuje po dodaniu elementu.
    _force_python(monkeypatch)
    (tmp_path / "a.txt").write_text("NEEDLE\nNEEDLE\n", encoding="utf-8")
    result = grep.rg_search("NEEDLE", tmp_path, max_results=0)
    assert result["matches"] == []


def test_c07_negative_max_results_rejected_python_fallback(tmp_path, monkeypatch):
    # Poprawne zachowanie: ujemny limit to błąd wejścia (albo zero trafień),
    # nie "jedno trafienie" i nie nieograniczony wynik.
    _force_python(monkeypatch)
    (tmp_path / "a.txt").write_text("NEEDLE\nNEEDLE\n", encoding="utf-8")
    result = grep.rg_search("NEEDLE", tmp_path, max_results=-5)
    assert result.get("ok") is False or result["matches"] == []


def test_c08_huge_max_results_capped(tmp_path, monkeypatch):
    # Kontrola pozytywna: olbrzymi limit jest twardo przycinany do MAX_RESULTS.
    (tmp_path / "many.txt").write_text("NEEDLE\n" * 400, encoding="utf-8")
    for engine in ("rg", "python") if RG_AVAILABLE else ("python",):
        _force_engine(monkeypatch, engine)
        result = grep.rg_search("NEEDLE", tmp_path, max_results=10**9)
        assert 0 < len(result["matches"]) <= grep.MAX_RESULTS


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="root ignoruje prawa dostępu (POSIX); brak geteuid na Windows")
def test_c09_unreadable_file_does_not_break_search(tmp_path, monkeypatch):
    # Poprawne zachowanie: brak uprawnień do jednego pliku nie psuje wyników,
    # pozostałe trafienia są zwracane.
    secret = tmp_path / "noperm.txt"
    secret.write_text("NEEDLE\n", encoding="utf-8")
    os.chmod(secret, 0o000)
    (tmp_path / "ok.txt").write_text("NEEDLE\n", encoding="utf-8")
    for engine in ("rg", "python") if RG_AVAILABLE else ("python",):
        _force_engine(monkeypatch, engine)
        result = grep.rg_search("NEEDLE", tmp_path, max_results=10)
        assert result["ok"] is True
        assert "ok.txt" in _files(result)


@rg_only
def test_c10_unreadable_file_keeps_rg_engine(tmp_path, monkeypatch):
    # Poprawne zachowanie: pojedynczy plik bez uprawnień nie powinien powodować
    # cichego przełączenia na fallback pythonowy (inna semantyka: limity
    # rozmiaru, brak respektowania .gitignore, pytanie o timeout). Obserwowane:
    # rg kończy się kodem 2 i cała ścieżka rg jest porzucana bez komunikatu.
    _force_rg(monkeypatch)
    secret = tmp_path / "noperm.txt"
    secret.write_text("NEEDLE\n", encoding="utf-8")
    os.chmod(secret, 0o000)
    (tmp_path / "ok.txt").write_text("NEEDLE\n", encoding="utf-8")
    result = grep.rg_search("NEEDLE", tmp_path, max_results=10)
    assert result["tool"] == "rg", f"cichy fallback na {result['tool']}"


def test_c11_broken_utf8_still_searchable(tmp_path, monkeypatch):
    # Poprawne zachowanie: zepsute bajty UTF-8 nie blokują trafień w resztę pliku.
    (tmp_path / "bad.txt").write_bytes(b"NEEDLE\xff\xfe\n")
    for engine in ("rg", "python") if RG_AVAILABLE else ("python",):
        _force_engine(monkeypatch, engine)
        assert len(grep.rg_search("NEEDLE", tmp_path, max_results=10)["matches"]) == 1


def test_c12_crlf_line_end_anchor_python_fallback(tmp_path, monkeypatch):
    # Kontrola pozytywna: fallback pythonowy (universal newlines) radzi sobie
    # z CRLF i kotwicą $.
    _force_python(monkeypatch)
    (tmp_path / "c.txt").write_bytes(b"foo\r\nbar\r\n")
    assert len(grep.rg_search("foo$", tmp_path, max_results=10)["matches"]) == 1


@rg_only
def test_c13_crlf_line_end_anchor_rg(tmp_path, monkeypatch):
    # Poprawne zachowanie: plik CRLF powinien dać to samo trafienie co w
    # fallbacku; rg wymaga w tym celu flagi --crlf (nie jest przekazywana),
    # więc kotwica $ nie działa. Silniki zwracają różne wyniki.
    _force_rg(monkeypatch)
    (tmp_path / "c.txt").write_bytes(b"foo\r\nbar\r\n")
    result = grep.rg_search("foo$", tmp_path, max_results=10)
    assert len(result["matches"]) == 1, "CRLF: kotwica $ nie działa w silniku rg"


# --------------------------------------------------------------------------- #
# (d) Katalogi ignorowane i .gitignore
# --------------------------------------------------------------------------- #


def test_d01_python_fallback_skips_known_dirs(tmp_path, monkeypatch):
    # Kontrola pozytywna: fallback pythonowy pomija .git, node_modules, venv,
    # .venv, __pycache__, .perun.
    _force_python(monkeypatch)
    for d in (".git", "node_modules", "venv", ".venv", "__pycache__", ".perun"):
        sub = tmp_path / d
        sub.mkdir()
        (sub / "x.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "src.txt").write_text("NEEDLE\n", encoding="utf-8")
    assert _files(grep.rg_search("NEEDLE", tmp_path, max_results=50)) == {"src.txt"}


@rg_only
def test_d02_node_modules_skipped_by_rg(tmp_path, monkeypatch):
    # Poprawne zachowanie: zależności z node_modules nie powinny być zwracane
    # przez domyślne przeszukiwanie projektu. Obserwowane: rg ich nie pomija,
    # więc ten sam projekt daje różne wyniki w zależności od silnika.
    _force_rg(monkeypatch)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "src.txt").write_text("NEEDLE\n", encoding="utf-8")
    assert _files(grep.rg_search("NEEDLE", tmp_path, max_results=50)) == {"src.txt"}


@rg_only
def test_d03_venv_skipped_by_rg(tmp_path, monkeypatch):
    # Poprawne zachowanie: katalog venv (bez kropki) też powinien być pomijany —
    # inaczej wyniki zależą od tego, czy na maszynie jest rg.
    _force_rg(monkeypatch)
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "lib.py").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "src.txt").write_text("NEEDLE\n", encoding="utf-8")
    assert _files(grep.rg_search("NEEDLE", tmp_path, max_results=50)) == {"src.txt"}


@rg_only
def test_d04_gitignore_respected_by_rg(tmp_path, monkeypatch):
    # Kontrola pozytywna: rg (w repo z .git) respektuje .gitignore.
    _force_rg(monkeypatch)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "kept.txt").write_text("NEEDLE\n", encoding="utf-8")
    assert _files(grep.rg_search("NEEDLE", tmp_path, max_results=50)) == {"kept.txt"}


def test_d05_gitignore_respected_by_python_fallback(tmp_path, monkeypatch):
    # Poprawne zachowanie: fallback pythonowy powinien respektować .gitignore
    # tak jak rg. Obserwowane: nie respektuje, więc wyniki zależą od silnika.
    _force_python(monkeypatch)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "kept.txt").write_text("NEEDLE\n", encoding="utf-8")
    assert _files(grep.rg_search("NEEDLE", tmp_path, max_results=50)) == {"kept.txt"}


@rg_only
def test_d06_engines_agree_on_ignored_dirs(tmp_path, monkeypatch):
    # Spójność obu silników dla katalogów zależności (venv/node_modules).
    for d in ("venv", "node_modules"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "x.txt").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "src.txt").write_text("NEEDLE\n", encoding="utf-8")
    _force_rg(monkeypatch)
    rg_files = _files(grep.rg_search("NEEDLE", tmp_path, max_results=50))
    _force_python(monkeypatch)
    py_files = _files(grep.rg_search("NEEDLE", tmp_path, max_results=50))
    assert rg_files == py_files, f"rg={rg_files} python={py_files}"


# --------------------------------------------------------------------------- #
# (e) search.py: ranking RRF, limity, puste zapytanie
# --------------------------------------------------------------------------- #


def _hit(file, line, column=None, project="p"):
    hit = {"project": project, "file": file, "line": line, "text": "t", "match": "t"}
    if column is not None:
        hit["column"] = column
    return hit


def test_e01_rrf_ranks_shared_hits_higher():
    # Kontrola pozytywna: trafienie obecne w obu rankingach wypada wyżej.
    grep_hits = [_hit("a.py", 1, 1), _hit("b.py", 2, 1)]
    ast_hits = [_hit("b.py", 2, 1), _hit("c.py", 3, 1)]
    fused = search._rrf_fuse(grep_hits, ast_hits, max_code=10, rrf_k=60)
    assert len(fused) == 3
    assert fused[0]["file"] == "b.py"
    scores = {m["file"]: m["score"] for m in fused}
    assert scores["b.py"] > scores["a.py"] and scores["b.py"] > scores["c.py"]


def test_e02_rrf_dedupes_same_location_across_engines():
    # Poprawne zachowanie: to samo miejsce w pliku zgłoszone przez rg i ast-grep
    # to jedno trafienie (różnica kolumny: rg liczy kolumnę trafienia, ast-grep
    # podaje 0) — inaczej użytkownik dostaje duplikaty.
    grep_hits = [_hit("f.py", 1, 5)]
    ast_hits = [_hit("f.py", 1, 0)]
    fused = search._rrf_fuse(grep_hits, ast_hits, max_code=10, rrf_k=60)
    assert len(fused) == 1, f"zduplikowane trafienie tej samej linii: {fused}"


def test_e03_rrf_many_hits_respects_max_code():
    # Kontrola pozytywna: ranking przy wielu trafieniach tnie się do max_code.
    grep_hits = [_hit(f"f{i}.py", i, 1) for i in range(100)]
    ast_hits = [_hit(f"g{i}.py", i, 1) for i in range(100)]
    fused = search._rrf_fuse(grep_hits, ast_hits, max_code=20, rrf_k=60)
    assert len(fused) == 20
    assert all(set(m) >= {"source", "file", "line", "text", "match", "score"} for m in fused)


def test_e04_rrf_zero_max_code_returns_nothing():
    assert search._rrf_fuse([_hit("a.py", 1, 1)], [_hit("a.py", 1, 1)], max_code=0) == []


def test_e05_rrf_negative_max_code_returns_nothing():
    # Poprawne zachowanie: ujemny limit => zero wyników (albo błąd wejścia).
    # Obserwowane: cięcie listy przez fused[:negative] zwraca "wszystko oprócz
    # ostatnich n", więc ujemny limit zwraca wyniki.
    hits = [_hit(f"f{i}.py", i, 1) for i in range(3)]
    assert search._rrf_fuse(hits, [], max_code=-2) == []


def test_e06_rrf_text_truncated():
    # Kontrola pozytywna: teksty są przycinane do 160 znaków.
    long = "x" * 400
    fused = search._rrf_fuse([{"project": "p", "file": "a.py", "line": 1, "column": 1,
                               "text": long, "match": long}], [], max_code=5)
    assert len(fused[0]["text"]) <= search._TRUNCATE_LEN
    assert len(fused[0]["match"]) <= search._TRUNCATE_LEN


def test_e07_unified_search_empty_query_rejected():
    # Poprawne zachowanie: brak zapytania/wzorca => invalid_input.
    result = search.unified_search(query="   ")
    assert result["ok"] is False and result["error_kind"] == "invalid_input"


def test_e08_unified_search_unknown_project_returns_empty(tmp_path, monkeypatch):
    _wire_project(monkeypatch, tmp_path)
    _force_rg(monkeypatch)
    result = search.unified_search(pattern="NEEDLE", projects=["nie-ma-takiego"])
    assert result["ok"] is True and result["total"] == 0 and result["projects_searched"] == 0


def test_e09_unified_search_finds_and_ranks_code(tmp_path, monkeypatch):
    # Kontrola pozytywna: integracja grep + RRF na udawanym projekcie.
    _, proj_dir = _wire_project(monkeypatch, tmp_path)
    (proj_dir / "a.py").write_text("def needle():\n", encoding="utf-8")
    _force_rg(monkeypatch)
    result = search.unified_search(pattern="needle", projects=["proj"], max_code=5)
    assert result["ok"] is True
    assert result["code"] and all(m["project"] == "proj" for m in result["code"])


def test_e10_unified_search_zero_max_code_returns_no_code(tmp_path, monkeypatch):
    _, proj_dir = _wire_project(monkeypatch, tmp_path)
    (proj_dir / "a.py").write_text("NEEDLE\n", encoding="utf-8")
    _force_rg(monkeypatch)
    result = search.unified_search(pattern="NEEDLE", projects=["proj"], max_code=0)
    assert result["ok"] is True and result["code"] == []


def test_e11_unified_search_huge_max_code_capped(tmp_path, monkeypatch):
    # Kontrola pozytywna: olbrzymi limit jest przycinany (MAX_RESULTS).
    _, proj_dir = _wire_project(monkeypatch, tmp_path)
    (proj_dir / "a.txt").write_text("NEEDLE\n" * 400, encoding="utf-8")
    _force_rg(monkeypatch)
    result = search.unified_search(pattern="NEEDLE", projects=["proj"], max_code=10**6)
    assert result["ok"] is True
    assert 0 < len(result["code"]) <= grep.MAX_RESULTS + 1


def test_e12_unified_search_negative_max_memory_passed_to_backend(tmp_path, monkeypatch):
    # Poprawne zachowanie: ujemny limit pamięci powinien być odrzucony albo
    # zaciśnięty do 0 — inaczej trafia wprost do backendu (w SQL LIMIT -1
    # oznacza "bez limitu").
    _wire_project(monkeypatch, tmp_path)
    _force_rg(monkeypatch)
    seen: list[int] = []

    def fake_memory(query, project_id=0, **kwargs):
        seen.append(kwargs.get("k"))
        return []

    monkeypatch.setattr(search.memory, "unified_search", fake_memory)
    result = search.unified_search(query="needle", projects=["proj"], max_memory=-3)
    assert result["ok"] is True
    assert seen, "backend pamięci nie został wywołany"
    assert all(k >= 0 for k in seen), f"ujemny limit przekazany do backendu: {seen}"
