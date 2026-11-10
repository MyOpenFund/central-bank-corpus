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

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


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
    from cb_corpus.storage import Storage
    monkeypatch.setattr(Storage, "render_resolver", staticmethod(_no_dns_resolver))
