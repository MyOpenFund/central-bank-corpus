"""Shared test fixtures.

The suite-wide network guard lives in `pytest.ini` (`--disable-socket`), not
here: pytest-socket re-enables sockets in its per-test teardown hook, so a
session-scoped fixture calling `disable_socket()` would only protect the very
first test. See `tests/test_network_guard.py` for the proof that it holds.

This module carries one shared duck-typed fetcher, and the one-line reader the
fixture-backed tests use, so the newer discovery tests (Bundesbank / BoE / BoJ
/ reindex) don't each grow their own copy. The older test modules keep their
own local fakes on purpose — they are not touched.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# The repo's own data tree. The production default (`Config.data_dir`) is the
# CWD-relative `./data`, and pytest runs from the repo root, so any test that
# reaches a default data root writes HERE. That tree is owned by the corpus
# host (the NAS), never by a test run.
REPO_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# Where a test could plausibly write under data/: its top-level files
# (runs.jsonl, cadence*.jsonl, *_errors.jsonl, quarantine log, ...) plus the
# direct children of these subdirectories. Deliberately NOT recursive and never
# `raw*` / `raw_orphans`: on a corpus host those hold hundreds of thousands of
# downloaded files, and walking them twice per session cost seconds per run.
GUARDED_DATA_SUBDIRS: tuple[str, ...] = ("manifest", "reports")


def _shallow_files(directory: Path, prefix: str, out: dict) -> None:
    """Add `prefix+name -> (size, mtime_ns)` for each regular file directly in
    `directory` (no recursion; symlinks are neither followed nor listed)."""
    try:
        entries = os.scandir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return
    with entries:
        for entry in entries:
            if entry.is_file(follow_symlinks=False):
                st = entry.stat(follow_symlinks=False)
                out[prefix + entry.name] = (st.st_size, st.st_mtime_ns)


def data_tree_snapshot(root: Path,
                       subdirs: tuple[str, ...] = GUARDED_DATA_SUBDIRS
                       ) -> dict[str, tuple[int, int]]:
    """`relative path -> (size, mtime_ns)` for the guarded part of `root`:
    its top-level files and the direct files of each of `subdirs` (empty if
    `root` is absent). Only stats directory entries, never reads a file."""
    out: dict[str, tuple[int, int]] = {}
    if root.is_symlink() or not root.is_dir():
        return out
    _shallow_files(root, "", out)
    for sub in subdirs:
        d = root / sub
        if not d.is_symlink():
            _shallow_files(d, f"{sub}/", out)
    return out


def data_tree_changes(before: dict, after: dict) -> list[str]:
    """Paths that are new, or whose size or mtime changed (an append to a
    pre-existing runs.jsonl counts). Removals are not reported."""
    return sorted(p for p, sig in after.items() if before.get(p) != sig)


def pytest_sessionstart(session):
    session.config._repo_data_before = data_tree_snapshot(REPO_DATA_DIR)
    session.config._repo_data_leaked = []


def pytest_sessionfinish(session, exitstatus):
    """Guard: a test run must not create or modify files under the repo's
    data/ (top level, manifest/, reports/). Runs once per session (cheap: a
    shallow stat of a few directories, no reads), and turns an otherwise
    green run red so a phantom write cannot creep back in."""
    before = getattr(session.config, "_repo_data_before", None)
    if before is None:
        return
    leaked = data_tree_changes(before, data_tree_snapshot(REPO_DATA_DIR))
    session.config._repo_data_leaked = leaked
    if leaked and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    leaked = getattr(config, "_repo_data_leaked", [])
    if leaked:
        terminalreporter.section(
            "files created or modified in the repo's data/", red=True)
        for rel in leaked:
            terminalreporter.line(f"  {REPO_DATA_DIR / rel}")
        terminalreporter.line(
            "A test reached a default data root (CWD-relative ./data or an "
            "unset CB_DATA_DIR). Point it at tmp_path. Clean up the files "
            "above by hand; this guard never deletes or edits anything under "
            "data/. (A crawl writing to data/ during the test run also trips "
            "it.)")


@pytest.fixture(autouse=True)
def _isolated_cb_data_dir(tmp_path, monkeypatch):
    """`cli._runs_path()` appends every reporting command's run-report to
    $CB_DATA_DIR/runs.jsonl, falling back to Config().data_dir (= ./data,
    i.e. the repo's data/ under pytest). Pin it to the test's tmp dir so a
    test that drives `cli.main(["discover", ...])` can never write the real
    run log — also when the developer's shell exports a real CB_DATA_DIR.
    A test that wants a specific dir still sets it itself (last setenv wins)."""
    monkeypatch.setenv("CB_DATA_DIR", str(tmp_path / "cb_data"))


def read_fixture(*parts: str) -> str:
    """Read a recorded page, e.g. `read_fixture("boj", "wps_2026.html")`.

    Always called from inside a test, never at import time: a stale or renamed
    fixture must fail one test with a readable error, not break collection of
    the whole module.
    """
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


class RecordingFetcher:
    """Stand-in for `cb_corpus.http.Fetcher`, serving canned pages off disk.

    `pages` maps a URL *suffix* to the body to return (same matching rule as
    the local fakes in `tests/test_bdf_wp.py`); a body that is an `Exception`
    is raised instead, which is how a dead page/listing is simulated. An
    unmapped URL raises, exactly as a live 404 does. Every requested URL is
    appended to `calls`, so tests can assert on fetch ORDER and on fetches
    that must NOT happen (early stops, dedup, pagination cut-offs).
    """

    def __init__(self, pages: dict[str, object]):
        self.pages = pages
        self.calls: list[str] = []

    def get_text(self, url: str) -> str:
        self.calls.append(url)
        for suffix, body in self.pages.items():
            if url.endswith(suffix):
                if isinstance(body, Exception):
                    raise body
                return body  # type: ignore[return-value]
        raise RuntimeError(f"404: {url}")


@pytest.fixture
def fetcher_factory():
    """Build a `RecordingFetcher` from a {url_suffix: body_or_exception} map."""
    return RecordingFetcher


def _no_dns_resolver(host, port, *args, **kwargs):
    raise OSError(f"real DNS disabled in tests (resolving {host!r})")


@pytest.fixture(autouse=True)
def _storage_render_guard_without_dns(monkeypatch):
    """Storage's render guard (htmlpdf.render_allowed) resolves the host before
    every HTML->PDF render. pytest-socket already blocks socket.getaddrinfo,
    but via a warning-raising error; this default makes every Storage in the
    suite fail closed quietly instead (render refused, HTML kept). A test that
    needs a render to go through sets `storage.render_resolver` to a fake."""
    from cb_corpus import htmlpdf
    from cb_corpus.storage import Storage
    monkeypatch.setattr(Storage, "render_resolver", staticmethod(_no_dns_resolver))
    monkeypatch.setattr(htmlpdf, "DEFAULT_RESOLVER", _no_dns_resolver)


def public_resolver(host, port, *args, **kwargs):
    """Fake getaddrinfo: every name resolves to one public address."""
    return [(2, 1, 6, "", ("93.184.216.34", port or 0))]
