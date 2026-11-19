"""Bank of Japan Working Paper Series (jp D1) — `discover_boj_wp` year walk.

Saved-fixture style (mirrors tests/test_bdf_wp.py). The fixtures under
tests/fixtures/boj/ are the real year listings captured 2026-09-02 from
`https://www.boj.or.jp/en/research/wps_rev/wps_{YYYY}/index.htm` — the exact
URLs the code builds — saved whole and unedited: wps_2026.html (14 papers),
wps_2025.html (13) and wps_2024.html (24).

`discover_boj_wp` was previously reached by no test (only `parse_wp_table`
was, on 3-line inline HTML): the year-by-year walk it wraps around the parser
— its order, its tolerance of a dead year, its `since` bound — was unverified.
No live network in these tests.
"""
from __future__ import annotations

from datetime import date

from cb_corpus.sources.boj_wp import discover_boj_wp
from cb_corpus.taxonomy import DocType
from tests.conftest import read_fixture

YEARS = {2024, 2025, 2026}


def _pages(*years: int) -> dict[str, object]:
    """The recorded year listings, keyed by the URL suffix the walker builds."""
    return {f"wps_{y}/index.htm": read_fixture("boj", f"wps_{y}.html") for y in years}


def _codes(records) -> list[str]:
    return [r.document_url.rsplit("/", 1)[-1].split(".")[0] for r in records]


def test_discover_walks_years_newest_first(fetcher_factory):
    """Years are walked newest-first so an incremental run meets the papers it
    is most likely to still need at the top, and so a `since` bound can stop
    early instead of crawling back to 1995 every night."""
    f = fetcher_factory(_pages(2024, 2025, 2026))
    recs = list(discover_boj_wp(f, years=YEARS))

    assert [c.rsplit("/", 2)[-2] for c in f.calls] == ["wps_2026", "wps_2025", "wps_2024"]
    assert len(recs) == 14 + 13 + 24
    assert _codes(recs)[0] == "wp26e12"                 # newest year, newest paper first
    assert _codes(recs)[-1] == "wp24e01"                # oldest year, last row


def test_discover_yields_records_with_the_day_printed_in_the_listing(fetcher_factory):
    """The BoJ prints the exact day inline in the listing table, so no
    per-paper fetch is needed: one request per year must yield full
    day-precision records."""
    f = fetcher_factory(_pages(2026))
    recs = list(discover_boj_wp(f, years={2026}))

    assert len(f.calls) == 1
    newest = recs[0]
    assert newest.date == date(2026, 8, 14) and newest.date_precision == "day"
    assert newest.title.startswith("What Prevents Productivity Gains")
    assert newest.document_url == ("https://www.boj.or.jp/en/research/wps_rev/wps_2026/"
                              "data/wp26e12.pdf")
    assert newest.source_url.endswith("/wps_rev/wps_2026/index.htm")
    assert newest.bank_code == "jp" and newest.doc_type == DocType.D1
    assert newest.provenance == "bank_site" and newest.date_source == "bank_site"
    assert newest.mime_type == "application/pdf"
    assert all(r.date_precision == "day" for r in recs)


def test_one_dead_year_does_not_stop_the_other_years(fetcher_factory):
    """A year page that 404s (or times out) is skipped: the BoJ has renamed
    and moved these listings before, and one dead year must cost that year's
    papers, not the entire back-catalogue."""
    pages = _pages(2024, 2026)
    pages["wps_2025/index.htm"] = RuntimeError("503 Service Unavailable")
    f = fetcher_factory(pages)

    recs = list(discover_boj_wp(f, years=YEARS))

    assert any("wps_2025" in c for c in f.calls)        # it WAS attempted
    assert len(recs) == 14 + 24                         # and only its papers are missing
    assert not any(c.startswith("wp25") for c in _codes(recs))


def test_since_bounds_the_years_walked_and_filters_within_the_boundary_year(fetcher_factory):
    """`since` does double duty: it stops the walk from descending past its
    year (2024 is never even requested) and drops the papers published
    earlier in the boundary year itself."""
    f = fetcher_factory(_pages(2024, 2025, 2026))
    recs = list(discover_boj_wp(f, since=date(2025, 10, 1)))

    assert not any("wps_2024" in c for c in f.calls)
    assert len(recs) == 14 + 3
    assert _codes(recs)[-3:] == ["wp25e13", "wp25e12", "wp25e11"]
    assert all(r.date >= date(2025, 10, 1) for r in recs)
