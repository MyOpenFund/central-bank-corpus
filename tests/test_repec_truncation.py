"""B4: the three IDEAS/RePEc pagination call sites must record a page-fetch
failure as TRUNCATION, never as a silent end-of-list.

Site 1: cb_corpus/sources/repec.py -- RePEcDiscovery._series_paper_pages
         (discovery, driven through discover_bank).
Site 2: cb_corpus/repec_check.py -- enumerate_series (the completeness audit
         tool).
Site 3: cb_corpus/pipeline.py -- run_repec_wayback_recovery. It has no
         pagination loop of its own (it re-discovers via the SAME
         RePEcDiscovery.discover_bank as site 1) but, before this task, it
         never threaded a SourceStats down into that walk at all -- a page
         failure during Wayback re-discovery was invisible, full stop. This
         is the "third pagination copy" the task brief points at in the
         wayback/recovery path.

Issue #30: IDEAS answers HTTP 404 on the page after the last listing page.
Such a 404 is a CLEAN end of walk iff it hits page k > 1 and page k-1 does not
link to `<series>k.html`; any other failure stays a truncation. The real-markup
fixtures tests/fixtures/repec/rbnkwp.html and rbnkwp2.html are the IDEAS
listing pages of hhs:rbnkwp (Riksbank), captured from
https://ideas.repec.org/s/hhs/rbnkwp.html and .../rbnkwp2.html on 2026-11-10
(200 + 199 papers, 397 distinct; page 1 links to page 2, page 2 links to no
page 3; both carry share-button URLs that mention the page file name).

Synthetic listing markup below mirrors the REAL builders already used
elsewhere in this suite to drive these exact parsers:
  - the `/p/<arch>/<slug>/<pid>.html` <li><a> shape from
    tests/test_repec_incremental.py::_series_html (parse_series_page,
    site 1) and tests/test_repec_reconcile.py::_series_html
    (parse_series_listing, site 2) -- both already verified against the
    production regexes they feed.
"""
from pathlib import Path

import requests

from cb_corpus.repec_check import enumerate_series
from cb_corpus.runreport import RunReport, SourceStats
from cb_corpus.sources.repec import IDEAS, RePEcDiscovery, links_to_listing_page


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages  # page number -> html | Exception
        self.calls = 0

    def get_text(self, url):
        self.calls += 1
        # page 1 is series.html, page N is seriesN.html
        import re
        m = re.search(r"(\d+)\.html$", url)
        page = int(m.group(1)) if m else 1
        val = self.pages.get(page)
        if isinstance(val, Exception):
            raise val
        if val is None:
            raise AssertionError(f"unexpected page {page}")
        return val


def _page_html(ids):
    """An IDEAS series-listing page for repec_check.enumerate_series
    (handle "arch:series"), mirroring test_repec_reconcile.py::_series_html
    (real, already-regex-verified markup for parse_series_listing) --
    templated here to carry arbitrary paper ids.
    """
    links = "".join(
        f'<li><a href="/p/arch/series/{i}.html">Paper {i}</a></li>'
        for i in ids
    )
    return f"<html><body><ul>{links}</ul></body></html>"


def _discovery_page_html(ids):
    """An IDEAS series-listing page for RePEcDiscovery._series_paper_pages
    (handle "hhs:rbnkwp"), mirroring test_repec_incremental.py::_series_html
    (real, already-verified markup for parse_series_page).
    """
    links = "".join(
        f'<li><a href="/p/hhs/rbnkwp/{i}.html">P{i}</a></li>' for i in ids
    )
    return f"<html><body><ul>{links}</ul></body></html>"


def _mk_discovery(fetcher):
    d = RePEcDiscovery.__new__(RePEcDiscovery)
    d.fetcher = fetcher
    d.max_pages = 80
    d.max_items = 5000
    return d


# ---------------------------------------------------------------------------
# Site 2: repec_check.enumerate_series
# ---------------------------------------------------------------------------

def test_page_failure_is_recorded_as_truncation():
    fetcher = FakeFetcher({1: _page_html(["p1", "p2"]), 2: ConnectionError("boom")})
    stats = SourceStats("ecb")
    rows = enumerate_series(fetcher, "arch:series", stats=stats)
    assert len(rows) == 2  # page 1 kept
    assert stats.truncated is True and stats.fetch_errors == 1


def test_true_end_of_pagination_is_not_truncation():
    fetcher = FakeFetcher({1: _page_html(["p1"]), 2: _page_html(["p1"])})  # no new -> stop
    stats = SourceStats("ecb")
    enumerate_series(fetcher, "arch:series", stats=stats)
    assert stats.truncated is False and stats.fetch_errors == 0


def test_enumerate_series_stats_none_preserves_silent_break():
    """Untouched callers (stats=None, the default) keep today's silent-break
    behavior -- no crash, just a shorter-than-expected result."""
    fetcher = FakeFetcher({1: _page_html(["p1"]), 2: ConnectionError("boom")})
    rows = enumerate_series(fetcher, "arch:series")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# I4: the two PRODUCTION callers of enumerate_series (run_repec_check,
# run_repec_reconcile via _walk_entries) used to pass stats=None -- a
# truncated IDEAS listing was silently absorbed into "fewer papers than
# expected", indistinguishable from a genuinely short series. Both now build
# a local SourceStats per series and print a WARNING to stderr when
# stats.truncated, so these audit commands are no longer silent about it
# (they still never write to a report file -- see their docstrings).
# ---------------------------------------------------------------------------

def _boe_page_html(ids):
    """A real gb-series (boe:boeewp, SERIES["gb"] in sources/repec.py) IDEAS
    listing page, mirroring test_repec_reconcile.py::_series_html."""
    links = "".join(
        f'<li><a href="/p/boe/boeewp/{i}.html">Paper {i}</a></li>' for i in ids
    )
    return f"<html><body><ul>{links}</ul></body></html>"


def test_run_repec_check_prints_truncation_warning(tmp_path, monkeypatch, capsys):
    from cb_corpus.config import Config
    from cb_corpus import repec_check as RC

    fetcher = FakeFetcher({1: _boe_page_html(["20250001"]), 2: ConnectionError("boom")})
    monkeypatch.setattr(RC, "Fetcher", lambda cfg: fetcher)
    cfg = Config(data_dir=tmp_path / "data")
    RC.run_repec_check(bank_codes=["gb"], config=cfg)
    err = capsys.readouterr().err
    assert "WARNING: IDEAS listing truncated for boe:boeewp" in err


def test_run_repec_reconcile_prints_truncation_warning(tmp_path, capsys):
    from cb_corpus.config import Config
    from cb_corpus.repec_check import run_repec_reconcile

    fetcher = FakeFetcher({1: _boe_page_html(["20250001"]), 2: ConnectionError("boom")})
    cfg = Config(data_dir=tmp_path / "data")
    run_repec_reconcile(bank_codes=["gb"], config=cfg, fetcher=fetcher)
    err = capsys.readouterr().err
    assert "WARNING: IDEAS listing truncated for boe:boeewp" in err


# ---------------------------------------------------------------------------
# Site 1: sources/repec.py::RePEcDiscovery._series_paper_pages (via discover_bank,
# the production shape -- driven through the class exactly like real callers do)
# ---------------------------------------------------------------------------

def test_discovery_page_failure_is_recorded_as_truncation():
    fetcher = FakeFetcher({1: _discovery_page_html(["0001", "0002"]),
                           2: ConnectionError("boom")})
    d = _mk_discovery(fetcher)
    stats = SourceStats("se")
    pages = list(d._series_paper_pages("hhs:rbnkwp", stats=stats))
    assert pages == [[f"{IDEAS}/p/hhs/rbnkwp/0001.html", f"{IDEAS}/p/hhs/rbnkwp/0002.html"]]
    assert stats.truncated is True and stats.fetch_errors == 1


def test_discovery_true_end_of_pagination_is_not_truncation():
    fetcher = FakeFetcher({1: _discovery_page_html(["0001"]),
                           2: _discovery_page_html(["0001"])})  # no new -> stop
    d = _mk_discovery(fetcher)
    stats = SourceStats("se")
    list(d._series_paper_pages("hhs:rbnkwp", stats=stats))
    assert stats.truncated is False and stats.fetch_errors == 0


# ---------------------------------------------------------------------------
# Site 3: pipeline.run_repec_wayback_recovery -- no pagination loop of its
# own; before this task it never threaded a SourceStats into the shared
# RePEcDiscovery.discover_bank walk at all, so a page failure during
# Wayback re-discovery was silently invisible. Exercised end-to-end through
# the pipeline function (real RePEcDiscovery, real _series_paper_pages) with
# _make_storage monkeypatched to a minimal fake storage/fetcher pair --
# this is mutation-proof against BOTH reverting site 1's fix AND reverting
# the stats-threading this task adds to run_repec_wayback_recovery itself.
# ---------------------------------------------------------------------------

class _ExactURLFetcher:
    """Exact-URL-keyed fake fetcher -- the same shape as the REAL precedent
    tests/test_repec_incremental.py::FakeFetcher, used here because
    run_repec_wayback_recovery drives discover_bank end-to-end, which fetches
    BOTH listing pages and individual paper pages: a page-number/regex fetcher
    (like the module-level FakeFetcher above, built for enumerate_series which
    only ever fetches listing pages) would collide numeric paper ids with page
    numbers. Unlisted URLs 404 (RuntimeError), which discover_bank already
    silently skips per-paper -- unrelated to the pagination truncation this
    test targets.
    """
    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    def get_text(self, url):
        self.fetched.append(url)
        if url not in self.pages:
            raise RuntimeError(f"404 {url}")
        return self.pages[url]


_BASE = f"{IDEAS}/s/hhs/rbnkwp"


class _FakeStorage:
    _ids = set()

    def is_known_url(self, url):
        return False

    def save_many(self, gen, dry_run=True, label=""):
        rows = list(gen)
        return {"saved": len(rows)}


def _patch_make_storage(monkeypatch, fetcher):
    import cb_corpus.pipeline as P
    monkeypatch.setattr(P, "_make_storage",
                        lambda config=None, html_to_pdf=None: (config, fetcher, _FakeStorage()))


def test_wayback_recovery_page_failure_is_recorded_as_truncation(monkeypatch):
    import cb_corpus.pipeline as P
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType

    monkeypatch.setitem(R.SERIES, "se", [("hhs:rbnkwp", DocType.D1)])
    fetcher = _ExactURLFetcher({f"{_BASE}.html": _discovery_page_html(["0001", "0002"])})
    # page 2 (f"{_BASE}2.html") is deliberately absent -> the fetcher raises a
    # bare RuntimeError with no HTTP status -> not a 404 -> truncation
    _patch_make_storage(monkeypatch, fetcher)

    report = RunReport("central-bank-corpus", "repec-wb")
    P.run_repec_wayback_recovery("se", dry_run=True, report=report)

    stats = report.source("repec-wb:se")
    assert stats.truncated is True and stats.fetch_errors == 1


def test_wayback_recovery_true_end_of_pagination_is_not_truncation(monkeypatch):
    import cb_corpus.pipeline as P
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType

    monkeypatch.setitem(R.SERIES, "se", [("hhs:rbnkwp", DocType.D1)])
    fetcher = _ExactURLFetcher({
        f"{_BASE}.html": _discovery_page_html(["0001"]),
        f"{_BASE}2.html": _discovery_page_html(["0001"]),  # no new -> true end
    })
    _patch_make_storage(monkeypatch, fetcher)

    report = RunReport("central-bank-corpus", "repec-wb")
    P.run_repec_wayback_recovery("se", dry_run=True, report=report)

    stats = report.source("repec-wb:se")
    assert stats.truncated is False and stats.fetch_errors == 0


def test_wayback_recovery_report_none_preserves_silent_behavior(monkeypatch):
    """report=None (the default -- unwired callers) must not crash: discover_bank
    gets stats=None, same as before this task."""
    import cb_corpus.pipeline as P
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType

    monkeypatch.setitem(R.SERIES, "se", [("hhs:rbnkwp", DocType.D1)])
    fetcher = _ExactURLFetcher({f"{_BASE}.html": _discovery_page_html(["0001"])})
    _patch_make_storage(monkeypatch, fetcher)

    counts = P.run_repec_wayback_recovery("se", dry_run=True)
    assert counts == {"saved": 0}  # paper page itself 404s -> extract skipped, nothing saved


# ---------------------------------------------------------------------------
# Issue #30: a 404 after the last IDEAS listing page is a clean end of walk
# ---------------------------------------------------------------------------

_FIX = Path(__file__).parent / "fixtures" / "repec"


def _fixture(name):
    return (_FIX / name).read_text(encoding="utf-8")


def _http_exc(status, url="https://ideas.repec.org/s/x/y3.html", msg=None):
    """The exception the real Fetcher raises for an HTTP error: a RuntimeError
    chained (`raise ... from`) from requests' HTTPError carrying the response.
    The default message mirrors Fetcher.get_bytes: a 4xx outside the default
    Config.retry_http_4xx fails fast ("not retried"); anything else is retried
    and ends "failed after retries"."""
    from cb_corpus.config import Config

    r = requests.Response()
    r.status_code = status
    if msg is None:
        fails_fast = 400 <= status < 500 and status not in Config().retry_http_4xx
        msg = (f"GET bytes failed (HTTP {status}, not retried): {url}" if fails_fast
               else f"GET bytes failed after retries: {url}")
    try:
        try:
            raise requests.exceptions.HTTPError(f"{status} error", response=r)
        except requests.exceptions.HTTPError as he:
            raise RuntimeError(msg) from he
    except RuntimeError as exc:
        assert exc.__cause__ is not None
        return exc


def _context_404():
    """404 carried on __context__ only (implicit chaining, no `from`)."""
    r = requests.Response()
    r.status_code = 404
    try:
        try:
            raise requests.exceptions.HTTPError("404", response=r)
        except requests.exceptions.HTTPError:
            raise RuntimeError("GET bytes failed: https://ideas.repec.org/s/x/y2.html")
    except RuntimeError as exc:
        assert exc.__cause__ is None and exc.__context__ is not None
        return exc


def _with_next_link(html, series, page):
    """Append an IDEAS pagination-bar link to `<series><page>.html` (appended
    at the end: the real pages close with an uppercase `</BODY>`)."""
    link = (f'<ul class="pagination"><li class="page-item">'
            f'<a class="page-link" href="{series}{page}.html">{page}</a></li></ul>')
    assert not links_to_listing_page(html, series, page)
    out = html + link
    assert links_to_listing_page(out, series, page)
    return out


def _walk_discovery(pages):
    d = _mk_discovery(FakeFetcher(pages))
    stats = SourceStats("se")
    out = list(d._series_paper_pages("hhs:rbnkwp", stats=stats))
    return out, stats


def _walk_enumerate(pages, handle="arch:series"):
    stats = SourceStats("x")
    rows = enumerate_series(FakeFetcher(pages), handle, stats=stats)
    return rows, stats


# -- unit: links_to_listing_page -------------------------------------------

def test_links_to_listing_page_real_page1_links_to_page2():
    assert links_to_listing_page(_fixture("rbnkwp.html"), "rbnkwp", 2) is True


def test_links_to_listing_page_real_page2_does_not_link_to_page3():
    assert links_to_listing_page(_fixture("rbnkwp2.html"), "rbnkwp", 3) is False


def test_links_to_listing_page_ignores_share_button_urls():
    html = ('<html><body><a href="https://bsky.app/intent/compose?text=Found%20on'
            '%20RePEc/IDEAS:%20https://ideas.repec.org//s/hhs/rbnkwp2.html%20">'
            'share</a></body></html>')
    assert links_to_listing_page(html, "rbnkwp", 2) is False


def test_links_to_listing_page_absolute_href_and_query_fragment():
    html = ('<a href="https://ideas.repec.org/s/hhs/rbnkwp2.html?x=1#top">2</a>')
    assert links_to_listing_page(html, "rbnkwp", 2) is True


def test_links_to_listing_page_exact_basename_only():
    html = '<a class="page-link" href="rbnkwp23.html">23</a>'
    assert links_to_listing_page(html, "rbnkwp", 2) is False
    assert links_to_listing_page('<a href="xrbnkwp2.html">2</a>', "rbnkwp", 2) is False


def test_links_to_listing_page_no_pagination_bar():
    assert links_to_listing_page(_discovery_page_html(["0001"]), "rbnkwp", 2) is False


# -- RePEcDiscovery._series_paper_pages -------------------------------------

def test_discovery_real_fixtures_404_after_last_page_is_clean_end():
    out, stats = _walk_discovery({1: _fixture("rbnkwp.html"),
                                  2: _fixture("rbnkwp2.html"),
                                  3: _http_exc(404)})
    assert len(out) == 2
    assert len({u for page in out for u in page}) == 397
    assert stats.truncated is False and stats.fetch_errors == 0


def test_discovery_404_on_page1_is_truncation():
    out, stats = _walk_discovery({1: _http_exc(404)})
    assert out == []
    assert stats.truncated is True and stats.fetch_errors == 1


def test_discovery_503_on_page3_is_truncation():
    _, stats = _walk_discovery({1: _fixture("rbnkwp.html"),
                                2: _fixture("rbnkwp2.html"),
                                3: _http_exc(503)})
    assert stats.truncated is True and stats.fetch_errors == 1
    assert stats.error_samples[-1].startswith("page 3: ")


def test_discovery_404_on_advertised_page_is_truncation():
    _, stats = _walk_discovery({1: _fixture("rbnkwp.html"),
                                2: _with_next_link(_fixture("rbnkwp2.html"), "rbnkwp", 3),
                                3: _http_exc(404)})
    assert stats.truncated is True and stats.fetch_errors == 1
    assert "page 3: HTTP 404 although page 2 links to it" in stats.error_samples[-1]


def test_discovery_bare_runtime_error_is_truncation():
    _, stats = _walk_discovery({1: _fixture("rbnkwp.html"),
                                2: _fixture("rbnkwp2.html"),
                                3: RuntimeError("404")})
    assert stats.truncated is True and stats.fetch_errors == 1


def test_discovery_404_after_page_without_pagination_bar_is_clean_end():
    out, stats = _walk_discovery({1: _discovery_page_html(["0001"]), 2: _http_exc(404)})
    assert len(out) == 1
    assert stats.truncated is False and stats.fetch_errors == 0


def test_discovery_404_on_context_is_clean_end():
    _, stats = _walk_discovery({1: _discovery_page_html(["0001"]), 2: _context_404()})
    assert stats.truncated is False and stats.fetch_errors == 0


# -- repec_check.enumerate_series -------------------------------------------

def test_enumerate_real_fixtures_404_after_last_page_is_clean_end():
    rows, stats = _walk_enumerate({1: _fixture("rbnkwp.html"),
                                   2: _fixture("rbnkwp2.html"),
                                   3: _http_exc(404)}, handle="hhs:rbnkwp")
    assert len(rows) == 397
    assert stats.truncated is False and stats.fetch_errors == 0


def test_enumerate_404_on_page1_is_truncation():
    rows, stats = _walk_enumerate({1: _http_exc(404)})
    assert rows == []
    assert stats.truncated is True and stats.fetch_errors == 1


def test_enumerate_503_on_page3_is_truncation():
    _, stats = _walk_enumerate({1: _page_html(["p1"]), 2: _page_html(["p2"]),
                                3: _http_exc(503)})
    assert stats.truncated is True and stats.fetch_errors == 1
    assert stats.error_samples[-1].startswith("page 3: ")


def test_enumerate_404_on_advertised_page_is_truncation():
    _, stats = _walk_enumerate({1: _page_html(["p1"]),
                                2: _with_next_link(_page_html(["p2"]), "series", 3),
                                3: _http_exc(404)})
    assert stats.truncated is True and stats.fetch_errors == 1
    assert "page 3: HTTP 404 although page 2 links to it" in stats.error_samples[-1]


def test_enumerate_synthetic_404_after_unlinked_page_is_clean_end():
    rows, stats = _walk_enumerate({1: _page_html(["p1"]), 2: _page_html(["p2"]),
                                   3: _http_exc(404)})
    assert len(rows) == 2
    assert stats.truncated is False and stats.fetch_errors == 0


def test_enumerate_bare_runtime_error_is_truncation():
    _, stats = _walk_enumerate({1: _page_html(["p1"]), 2: _page_html(["p2"]),
                                3: RuntimeError("404")})
    assert stats.truncated is True and stats.fetch_errors == 1


def test_enumerate_404_after_page_without_pagination_bar_is_clean_end():
    rows, stats = _walk_enumerate({1: _page_html(["p1"]), 2: _http_exc(404)})
    assert len(rows) == 1
    assert stats.truncated is False and stats.fetch_errors == 0


def test_enumerate_404_on_context_is_clean_end():
    _, stats = _walk_enumerate({1: _page_html(["p1"]), 2: _context_404()})
    assert stats.truncated is False and stats.fetch_errors == 0


def test_run_repec_check_404_after_last_page_prints_no_warning(tmp_path, monkeypatch, capsys):
    from cb_corpus.config import Config
    from cb_corpus import repec_check as RC

    fetcher = FakeFetcher({1: _boe_page_html(["20250001"]), 2: _http_exc(404)})
    monkeypatch.setattr(RC, "Fetcher", lambda cfg: fetcher)
    cfg = Config(data_dir=tmp_path / "data")
    RC.run_repec_check(bank_codes=["gb"], config=cfg)
    err = capsys.readouterr().err
    assert "WARNING: IDEAS listing truncated" not in err


# -- review fixes: malformed hrefs, series name, end-to-end -----------------

_BAD_HREF = '<a href="http://[bad/x.html">broken</a>'


def test_links_to_listing_page_skips_malformed_href():
    html = _BAD_HREF + '<a class="page-link" href="rbnkwp2.html">2</a>'
    assert links_to_listing_page(html, "rbnkwp", 2) is True
    assert links_to_listing_page(_BAD_HREF, "rbnkwp", 2) is False


def test_discovery_malformed_href_then_404_is_clean_end():
    p1 = _discovery_page_html(["0001"]).replace("</body>", _BAD_HREF + "</body>")
    out, stats = _walk_discovery({1: p1, 2: _http_exc(404)})
    assert len(out) == 1
    assert stats.truncated is False and stats.fetch_errors == 0


def test_enumerate_malformed_href_then_404_is_clean_end():
    p1 = _page_html(["p1"]).replace("</body>", _BAD_HREF + "</body>")
    rows, stats = _walk_enumerate({1: p1, 2: _http_exc(404)})
    assert len(rows) == 1
    assert stats.truncated is False and stats.fetch_errors == 0


class _Chained404Fetcher(_ExactURLFetcher):
    """Like _ExactURLFetcher, but the listing URLs in `http404` raise the real
    Fetcher's chained 404 (RuntimeError from HTTPError)."""
    def __init__(self, pages, http404):
        super().__init__(pages)
        self.http404 = set(http404)

    def get_text(self, url):
        if url in self.http404:
            self.fetched.append(url)
            raise _http_exc(404, url)
        return super().get_text(url)


def test_discovery_series_name_matches_fetched_url():
    """The link check uses the same series name as the fetched page URLs: a
    page 1 linking to `rbnkwp2.html` makes a page-2 404 a truncation."""
    p1 = _with_next_link(_discovery_page_html(["0001"]), "rbnkwp", 2)
    fetcher = _Chained404Fetcher({f"{_BASE}.html": p1}, http404=[f"{_BASE}2.html"])
    d = _mk_discovery(fetcher)
    stats = SourceStats("se")
    list(d._series_paper_pages("hhs:rbnkwp", stats=stats))
    assert fetcher.fetched == [f"{_BASE}.html", f"{_BASE}2.html"]
    assert stats.truncated is True
    assert "page 2: HTTP 404 although page 1 links to it" in stats.error_samples[-1]


def test_wayback_recovery_404_after_last_page_is_clean_end(monkeypatch):
    import cb_corpus.pipeline as P
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType

    monkeypatch.setitem(R.SERIES, "se", [("hhs:rbnkwp", DocType.D1)])
    fetcher = _Chained404Fetcher({f"{_BASE}.html": _discovery_page_html(["0001", "0002"])},
                                 http404=[f"{_BASE}2.html"])
    _patch_make_storage(monkeypatch, fetcher)

    report = RunReport("central-bank-corpus", "repec-wb")
    P.run_repec_wayback_recovery("se", dry_run=True, report=report)

    assert f"{_BASE}2.html" in fetcher.fetched
    stats = report.source("repec-wb:se")
    assert stats.truncated is False and stats.fetch_errors == 0


class _FakeSession:
    """Stands in for requests.Session under a REAL cb_corpus.http.Fetcher:
    url -> (status, body)."""
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def get(self, url, timeout=None, stream=False, allow_redirects=True):
        self.calls.append(url)
        status, body = self.routes[url]
        r = requests.Response()
        r.status_code = status
        r.url = url
        r.reason = "OK" if status == 200 else "Error"
        r.headers["content-type"] = "text/html; charset=utf-8"
        r._content = body.encode("utf-8")
        r._content_consumed = True
        return r


def _real_fetcher(monkeypatch, page3_status, extra_routes=None, config=None):
    import cb_corpus.http as H
    from cb_corpus.config import Config

    monkeypatch.setattr(H.time, "sleep", lambda s: None)
    base = f"{IDEAS}/s/hhs/rbnkwp"
    session = _FakeSession({
        f"{base}.html": (200, _fixture("rbnkwp.html")),
        f"{base}2.html": (200, _fixture("rbnkwp2.html")),
        f"{base}3.html": (page3_status, "<html>error</html>"),
        **(extra_routes or {}),
    })
    f = H.Fetcher(config or Config())
    f.session = session
    return f, session


def test_enumerate_real_fetcher_404_after_last_page_is_clean_end(monkeypatch):
    f, session = _real_fetcher(monkeypatch, 404)
    stats = SourceStats("se")
    rows = enumerate_series(f, "hhs:rbnkwp", stats=stats)
    assert len(rows) == 397
    assert session.calls.count(f"{IDEAS}/s/hhs/rbnkwp3.html") == 1  # 404 not retried
    assert stats.truncated is False and stats.fetch_errors == 0


def test_enumerate_real_fetcher_503_on_page3_is_truncation(monkeypatch):
    f, session = _real_fetcher(monkeypatch, 503)
    stats = SourceStats("se")
    rows = enumerate_series(f, "hhs:rbnkwp", stats=stats)
    assert len(rows) == 397
    assert session.calls.count(f"{IDEAS}/s/hhs/rbnkwp3.html") == f.cfg.max_retries
    assert stats.truncated is True and stats.fetch_errors == 1
    assert stats.error_samples[-1].startswith("page 3: ")


# -- final review: pipeline.run_repec end to end, real Fetcher -> exit code --

class _OneUnknownStorage:
    """Fake storage: every IDEAS paper page is already known except `unknown`,
    so the walk visits all three listing pages but fetches one paper page."""
    def __init__(self, unknown):
        self.unknown = unknown
        self.saved = []

    def is_known_source_url(self, url):
        return url != self.unknown

    def save_many(self, recs, dry_run=True, label=""):
        self.saved = list(recs)
        return {"saved": len(self.saved)}


def _run_repec_se(monkeypatch, tmp_path, page3_status):
    import cb_corpus.pipeline as P
    from cb_corpus.config import Config
    from cb_corpus.sources import repec as R
    from cb_corpus.sources.repec import parse_series_page
    from cb_corpus.taxonomy import DocType

    monkeypatch.setitem(R.SERIES, "se", [("hhs:rbnkwp", DocType.D1)])
    paper = parse_series_page(_fixture("rbnkwp.html"))[0]
    paper_html = ('<html><head><title>A paper</title></head><body>'
                  '<a href="https://www.riksbank.se/wp/a-paper.pdf">pdf</a></body></html>')
    cfg = Config(data_dir=tmp_path / "data", min_delay_seconds=0.0)
    fetcher, session = _real_fetcher(monkeypatch, page3_status,
                                     extra_routes={paper: (200, paper_html)}, config=cfg)
    storage = _OneUnknownStorage(paper)
    monkeypatch.setattr(P, "_make_storage",
                        lambda config=None, html_to_pdf=None: (cfg, fetcher, storage))

    report = RunReport("central-bank-corpus", "repec")
    P.run_repec(["se"], dry_run=True, config=cfg, report=report)
    return report, storage, session


def test_run_repec_404_after_last_page_exits_0(monkeypatch, tmp_path):
    report, storage, session = _run_repec_se(monkeypatch, tmp_path, 404)
    stats = report.source("se")
    assert f"{IDEAS}/s/hhs/rbnkwp3.html" in session.calls
    assert len(storage.saved) == 1 and stats.docs_new == 1 and stats.docs_failed == 0
    assert stats.truncated is False and stats.fetch_errors == 0
    assert report.finish() == 0


def test_run_repec_503_on_page3_exits_3(monkeypatch, tmp_path):
    report, storage, _ = _run_repec_se(monkeypatch, tmp_path, 503)
    stats = report.source("se")
    assert len(storage.saved) == 1 and stats.docs_new == 1
    assert stats.truncated is True and stats.fetch_errors == 1
    assert report.finish() == 3
