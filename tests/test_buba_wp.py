"""Bundesbank Discussion Papers (de D1) — `discover_buba_wp` end to end.

Saved-fixture style (mirrors tests/test_bdf_wp.py). The fixtures under
tests/fixtures/buba/ are real `bbksearch` result pages captured 2026-09-02
from the endpoint the code itself requests,
`https://www.bundesbank.de/action/en/732408/bbksearch?pageNumString=N`
(saved whole, nothing edited):
- bbksearch_page0.html   (N=0):   the newest 10 papers. Every item links only a
  `/discussion-papers/<slug>-<id>` page — NO direct blob — so this page is the
  fixture for the "fetch the paper's own page" branch.
- bbksearch_page120.html (N=120): 1999-2000 papers, every item linking a direct
  `…/YYYY-MM-DD-dkp-NN-data.pdf` blob (the no-fetch branch). It also carries
  the German/English pair of DP 05/1999 pointing at the SAME blob — real
  duplicate `(num, year)` keys, so dedup is exercised on real markup.
- bbksearch_page121.html (N=121): the next page (1997-1999), four more
  German/English duplicate pairs. Serving 120 then 121 as pages 0 and 1 gives
  a real two-page walk whose second page is strictly older than the first.
- detail_dkp_22_2026.html: the real paper page for DP 22/2026 "Collateral
  policy surprises" (…/discussion-papers/collateral-policy-surprises-957108),
  carrying the og:title and the blob PDF the listing page does not.

`discover_buba_wp` was previously reached by no test at all (it was only ever
monkeypatched away), so the pagination, branch, cutoff and dedup behaviour it
promises in its docstring was unverified. No live network in these tests.
"""
from __future__ import annotations

from datetime import date

from cb_corpus.sources.buba_wp import discover_buba_wp
from cb_corpus.taxonomy import DocType
from tests.conftest import read_fixture


def _page(name: str) -> str:
    """One recorded bbksearch result page. The 1999-2000 pages (120/121) are
    the only ones in the archive old enough to carry direct blob links, and are
    served to the walker as its pages 0/1."""
    return read_fixture("buba", name)


def _blob_names(records) -> list[str]:
    return [r.document_url.rsplit("/", 1)[-1] for r in records]


# ---- pagination -------------------------------------------------------

def test_discover_walks_every_page_up_to_max_pages(fetcher_factory):
    """The listing renders only 10 papers per page: without following the
    `pageNumString` pagination the crawler would see the newest handful and
    silently declare the back-catalogue complete."""
    f = fetcher_factory({"pageNumString=0": _page("bbksearch_page120.html"),
                         "pageNumString=1": _page("bbksearch_page121.html")})
    recs = list(discover_buba_wp(f, max_pages=2))

    assert [c.rsplit("?", 1)[-1] for c in f.calls] == ["pageNumString=0", "pageNumString=1"]
    names = _blob_names(recs)
    assert "2000-08-31-dkp-04-data.pdf" in names        # first page
    assert "1997-10-02-dkp-05-data.pdf" in names        # second page
    assert len(recs) == 15                              # 9 + 6 after dedup


def test_discover_stops_at_max_pages_even_though_the_site_advertises_more(fetcher_factory):
    """Page 0 advertises `pageNumString=125` as the last page; `max_pages`
    must still bound the walk, or a smoke test would crawl 126 pages."""
    f = fetcher_factory({"pageNumString=0": _page("bbksearch_page120.html")})
    list(discover_buba_wp(f, max_pages=1))
    assert len(f.calls) == 1


# ---- blob-on-the-listing vs fetch-the-paper-page ----------------------

def test_old_papers_take_the_blob_and_its_date_off_the_listing(fetcher_factory):
    """Pre-2001 items link the `…-dkp-NN-data.pdf` blob directly, whose
    filename embeds the ISO date: the walk must read both off the listing and
    NOT spend one HTTP request per paper (10x the traffic for nothing)."""
    f = fetcher_factory({"pageNumString=0": _page("bbksearch_page120.html")})
    recs = list(discover_buba_wp(f, max_pages=1))

    assert len(f.calls) == 1                            # listing only, no paper pages
    newest = recs[0]
    assert newest.document_url.endswith(
        "/472B63F073F071307366337C94F8C870/2000-08-31-dkp-04-data.pdf")
    assert newest.date == date(2000, 8, 31) and newest.date_precision == "day"
    assert newest.bank_code == "de" and newest.doc_type == DocType.D1
    assert newest.provenance == "bank_site" and newest.date_source == "bank_site"
    assert newest.mime_type == "application/pdf"
    assert all(r.date is not None for r in recs)


def test_recent_papers_are_completed_from_their_own_page(fetcher_factory):
    """Recent listing items carry no blob at all — only a slug page. The blob
    URL is opaque (`/resource/blob/<id>/<hash>/…`) and cannot be derived, so
    the page MUST be fetched; the title and day come from it too."""
    f = fetcher_factory({
        "pageNumString=0": _page("bbksearch_page0.html"),
        # Only this one paper page resolves; the nine others 404, as a dead
        # paper page would live — one bad page must not sink the whole walk.
        "collateral-policy-surprises-957108": _page("detail_dkp_22_2026.html"),
    })
    recs = list(discover_buba_wp(f, max_pages=1))

    assert len(f.calls) == 11                           # 1 listing + 10 paper pages
    assert len(recs) == 1                               # the nine dead pages are skipped
    rec = recs[0]
    assert rec.title == "Collateral policy surprises"    # og:title, not the listing blurb
    assert rec.date == date(2026, 8, 18) and rec.date_precision == "day"
    assert rec.document_url.endswith("/2026-08-18-dkp-22-data.pdf")
    assert rec.source_url.endswith("/collateral-policy-surprises-957108")


# ---- since cutoff -----------------------------------------------------

def test_since_cutoff_drops_older_papers_and_stops_paging(fetcher_factory):
    """The list is newest-first, so a page with nothing newer than `since`
    means every later page is older too: the walk must stop there instead of
    paging through 25 years of archive on every nightly run."""
    f = fetcher_factory({"pageNumString=0": _page("bbksearch_page120.html"),
                         "pageNumString=1": _page("bbksearch_page121.html")})
    recs = list(discover_buba_wp(f, since=date(2000, 1, 1), max_pages=3))

    # page 1 (1997-1999) is entirely older -> page 2 is never requested.
    assert [c.rsplit("?", 1)[-1] for c in f.calls] == ["pageNumString=0", "pageNumString=1"]
    assert _blob_names(recs) == [
        "2000-08-31-dkp-04-data.pdf",
        "2000-07-03-dkp-03-data.pdf",
        "2000-05-29-dkp-02-data.pdf",
        "2000-02-01-dkp-01-data.pdf",
    ]


# ---- dedup by (num, year) ---------------------------------------------

def test_duplicate_dp_number_yields_one_record(fetcher_factory):
    """The Bundesbank lists the German and English editions of an early paper
    as two items pointing at the SAME blob (DP 05/1999 here): keyed on
    `(num, year)` they collapse to one record, so the corpus doesn't grow a
    duplicate every time a bilingual paper is re-listed."""
    f = fetcher_factory({"pageNumString=0": _page("bbksearch_page120.html")})
    recs = list(discover_buba_wp(f, max_pages=1))

    names = _blob_names(recs)
    assert names.count("1999-05-01-dkp-05-data.pdf") == 1
    assert len(names) == len(set(names)) == 9           # 10 listed items, 9 distinct DPs
