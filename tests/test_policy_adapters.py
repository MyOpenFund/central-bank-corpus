"""Producers of the policy core write the right date, precision and kind from
the start, so the `policy-dates` backfill is run once and the nightly sync
never disagrees with it: a row a producer yields is a row the backfill leaves
alone. No network: every source is a fake."""
from __future__ import annotations

import re
from datetime import date

import pytest

from cb_corpus import pipeline
from cb_corpus.adapters.base import get_adapter
from cb_corpus.adapters.generic_sitemap import GenericSitemapAdapter
from cb_corpus.adapters.listing_crawler import ListingCrawlerAdapter, parse_links
from cb_corpus.banks import get_bank
from cb_corpus.config import Config
from cb_corpus.dates import BOJ_MINUTES_URL
from cb_corpus.models import DocRecord, compute_doc_id
from cb_corpus.policy_dates import plan_row
from cb_corpus.storage import Storage
from cb_corpus.taxonomy import DocType
from tests.conftest import pdf_body


class _Pages:
    def __init__(self, pages):
        self.pages = pages

    def get_text(self, url):
        for key, body in self.pages.items():
            if key in url:
                return body
        raise RuntimeError(f"fake 404 {url}")


@pytest.fixture
def captured(monkeypatch):
    """Capture what a pipeline recovery hands to save_many, without saving."""
    out: list[DocRecord] = []

    def fake_save_many(self, recs, **kw):
        out.extend(recs)
        return {}
    monkeypatch.setattr(Storage, "save_many", fake_save_many)
    return out


def _dated(rec):
    row = rec.to_row()
    return (row["date"], row["date_precision"], row.get("date_kind"))


def assert_backfill_agrees(recs):
    """The backfill has nothing to write on a row a producer just built."""
    assert recs
    for rec in recs:
        plan = plan_row(rec.to_row())
        assert plan.updates == {}, (rec.document_url, plan)


# ---- RBA --------------------------------------------------------------

RBA_HTML = ('<a href="/publications/smp/2026/feb/">x</a>'
            '<a href="/publications/smp/2025/nov/">y</a>'
            '<a href="/publications/smp/2026/feb/outlook.html">chapter (skip)</a>'
            '<a href="/media-releases/2025/mr-25-33.html">9 December 2025</a>'
            '<a href="/media-releases/2025/mr-25-18.html">1 July 2025</a>'
            '<a href="/monetary-policy/rba-board-minutes/2025/2025-11-04.html">m</a>'
            '<a href="/monetary-policy/rba-board-minutes/2025/2025-04-01.html">m</a>')


def test_rba_statement_is_a_month_not_a_day():
    ad = get_adapter("au", _Pages({"": RBA_HTML}))
    recs = list(ad._discover_native(DocType.E1, None))
    assert sorted(_dated(r) for r in recs) == [("2025-11-01", "month", "period"),
                                               ("2026-02-01", "month", "period")]
    assert_backfill_agrees(recs)


def test_rba_decisions_and_minutes_stay_days_even_on_the_first():
    ad = get_adapter("au", _Pages({"": RBA_HTML}))
    a1 = list(ad._discover_native(DocType.A1, date(2025, 1, 1)))
    a3 = list(ad._discover_native(DocType.A3, date(2025, 1, 1)))
    assert sorted(_dated(r) for r in a1) == [("2025-07-01", "day", "meeting"),
                                             ("2025-12-09", "day", "meeting")]
    assert sorted(_dated(r) for r in a3) == [("2025-04-01", "day", "meeting"),
                                             ("2025-11-04", "day", "meeting")]
    assert_backfill_agrees(a1 + a3)


# ---- generic sitemap (Bank of Canada) ---------------------------------

BOC = "https://www.bankofcanada.ca"
BOC_SITEMAP = f"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{BOC}/publications/mpr/mpr-2026-10-28/tariff-assumptions/</loc></url>
  <url><loc>{BOC}/publications/mpr/mpr-2024-07/global-economy/</loc></url>
  <url><loc>{BOC}/publications/mpr/mpr-2024-07/</loc><lastmod>2024-08-19</lastmod></url>
  <url><loc>{BOC}/publications/mpr/</loc></url>
  <url><loc>{BOC}/publications/mpr/qualitative-research-on-the-mpr/</loc></url>
</urlset>"""


def _boc():
    return GenericSitemapAdapter(get_bank("ca"), _Pages({"": BOC_SITEMAP}),
                                 sitemap_url=f"{BOC}/wp-sitemap.xml",
                                 patterns={DocType.E1: r"/publications/mpr/.*"})


def test_sitemap_reads_a_month_from_the_url():
    recs = {r.document_url[len(BOC):]: r for r in _boc()._discover_native(DocType.E1, None)}
    # a day in the URL is the day the report was released
    assert _dated(recs["/publications/mpr/mpr-2026-10-28/tariff-assumptions/"]) == (
        "2026-10-28", "day", "publication")
    assert _dated(recs["/publications/mpr/mpr-2024-07/global-economy/"]) == (
        "2024-07-01", "month", "period")
    # the bank's own URL beats the sitemap's last-modified day for a policy series
    issue = recs["/publications/mpr/mpr-2024-07/"]
    assert _dated(issue) == ("2024-07-01", "month", "period")
    assert issue.date_source == "bank_site"
    # index pages: undated, no precision, no kind
    for path in ("/publications/mpr/", "/publications/mpr/qualitative-research-on-the-mpr/"):
        row = recs[path].to_row()
        assert (row["date"], row["date_precision"]) == (None, None) and "date_kind" not in row
    assert_backfill_agrees(list(recs.values()))


def test_sitemap_last_modified_day_and_index_pages_carry_no_kind():
    """A <lastmod> is neither an event nor an issue, and an index page is not
    a report: neither gets the series' kind -- exactly as in the backfill."""
    xml = f"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{BOC}/publications/mpr/</loc><lastmod>2024-08-19</lastmod></url>
  <url><loc>{BOC}/publications/mpr/qualitative-research-on-the-mpr/</loc>
       <lastmod>2023-05-02</lastmod></url>
  <url><loc>{BOC}/core-functions/monetary-policy/summary-deliberations-x/</loc>
       <lastmod>2025-06-18</lastmod></url>
</urlset>"""
    ad = GenericSitemapAdapter(get_bank("ca"), _Pages({"": xml}),
                               sitemap_url=f"{BOC}/wp-sitemap.xml",
                               patterns={DocType.E1: r"/publications/mpr/.*",
                                         DocType.A3: r"/core-functions/monetary-policy/.*"})
    recs = (list(ad._discover_native(DocType.E1, None))
            + list(ad._discover_native(DocType.A3, None)))
    assert len(recs) == 3
    for rec in recs:
        row = rec.to_row()
        assert row["date"] and row["date_source"] == "sitemap_lastmod"
        assert "date_kind" not in row
    assert [plan_row(r.to_row()).action for r in recs] == [
        "not-a-report", "not-a-report", "no-evidence"]
    assert_backfill_agrees(recs)


def test_sitemap_month_fallback_for_a_bank_without_a_rule():
    xml = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://bank.example/pub/2023-04/report.pdf</loc></url>
  <url><loc>https://bank.example/pub/2020-05-06-b.pdf</loc></url>
  <url><loc>https://bank.example/pub/a.pdf</loc><lastmod>2021-03-04</lastmod></url>
  <url><loc>https://bank.example/pub/2019-11/c.pdf</loc><lastmod>2021-03-04</lastmod></url>
  <url><loc>https://bank.example/pub/report-2023-045.pdf</loc></url>
</urlset>"""
    ad = GenericSitemapAdapter(get_bank("ch"), _Pages({"": xml}),
                               sitemap_url="https://bank.example/sm.xml",
                               patterns={DocType.E2: r"/pub/"})
    recs = {r.document_url.split("/pub/")[1]: r for r in ad._discover_native(DocType.E2, None)}
    got = {k: (r.date, r.date_precision, r.date_source) for k, r in recs.items()}
    assert got["2023-04/report.pdf"] == (date(2023, 4, 1), "month", "bank_site")
    assert got["2020-05-06-b.pdf"] == (date(2020, 5, 6), "day", "bank_site")
    # unchanged for a series without a rule: the last-modified day comes first
    assert got["a.pdf"] == (date(2021, 3, 4), "day", "sitemap_lastmod")
    assert got["2019-11/c.pdf"] == (date(2021, 3, 4), "day", "sitemap_lastmod")
    # "2023-045" is a number, not a month
    assert got["report-2023-045.pdf"] == (None, None, "bank_site")
    assert all("date_kind" not in r.to_row() for r in recs.values())


def test_sitemap_since_filter_uses_the_month_date():
    recs = list(_boc()._discover_native(DocType.E1, date(2025, 1, 1)))
    urls = {r.document_url[len(BOC):] for r in recs}
    assert "/publications/mpr/mpr-2024-07/global-economy/" not in urls
    assert "/publications/mpr/mpr-2026-10-28/tariff-assumptions/" in urls


# ---- listing crawler (Bank of Japan minutes) --------------------------

BOJ_HTML = """<html><body>
<a href="/en/mopo/mpmsche_minu/minu_2026/g260123.pdf">Minutes [PDF 292KB]</a>
<a href="/en/mopo/mpmsche_minu/minu_2026/g260501.pdf">Meeting on April 30 and May 1, 2026 [PDF 300KB]</a>
<a href="/en/mopo/mpmsche_minu/minu_2026/other.pdf">skip</a>
</body></html>"""


def test_boj_minutes_are_dated_from_the_url():
    ad = get_adapter("jp", _Pages({"minu_2026": BOJ_HTML, "": "<html></html>"}))
    recs = list(ad._discover_native(DocType.A3, None))
    assert [_dated(r) for r in recs] == [("2026-01-23", "day", "meeting"),
                                         ("2026-05-01", "day", "meeting")]
    assert all(r.date_source == "bank_site" for r in recs)
    assert_backfill_agrees(recs)


def test_boj_date_change_keeps_the_identity():
    ad = get_adapter("jp", _Pages({"minu_2026": BOJ_HTML, "": "<html></html>"}))
    rec = next(iter(ad._discover_native(DocType.A3, None)))
    url = "https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2026/g260123.pdf"
    assert rec.document_url == url and rec.doc_id == compute_doc_id("jp", "A3", url)


def test_listing_date_pattern_then_generic_date_then_title():
    html = ('<a href="/m/g260123.pdf">no date in the text</a>'
            '<a href="/m/2024-03-15-minutes.pdf">x</a>'
            '<a href="/m/untitled-a.pdf">Meeting on January 28 and 29, 2016</a>'
            '<a href="/m/untitled-b.pdf">Minutes - June 2021</a>'
            '<a href="/m/untitled-c.pdf">Minutes</a>')
    ad = ListingCrawlerAdapter(get_bank("ch"), _Pages({"": html}),
                               entries={DocType.A3: [("https://x.test/list", r"\.pdf$")]},
                               date_patterns={DocType.A3: BOJ_MINUTES_URL})
    got = [(r.date, r.date_precision) for r in ad._discover_native(DocType.A3, None)]
    assert got == [(date(2026, 1, 23), "day"), (date(2024, 3, 15), "day"),
                   (date(2016, 1, 29), "day"), (date(2021, 6, 1), "month"), (None, None)]


def test_parse_links_keeps_its_three_fields():
    rows = parse_links(BOJ_HTML, "https://www.boj.or.jp/x/", re.compile(r"g\d{6}\.pdf$"))
    assert [len(r) for r in rows] == [3, 3]
    assert rows[0][0] is None                    # no pattern given, no date in the text
    assert rows[1][0] == date(2026, 5, 1)        # the link text names the day


# ---- Bank of England recovery -----------------------------------------

BOE = "https://www.bankofengland.co.uk"


def _boe(monkeypatch, pages):
    import cb_corpus.sources.boe_wp as boe_wp
    monkeypatch.setattr(boe_wp, "doc_pages", lambda f, sp, hf, years: [
        (date(int(re.search(r"/(\d{4})/", p).group(1)), 1, 1), p) for p in pages])
    monkeypatch.setattr(boe_wp, "page_doc", lambda f, page: pages[page])


def test_boe_recovery_dates_minutes_from_the_title(tmp_path, monkeypatch, captured):
    _boe(monkeypatch, {
        f"{BOE}/minutes/1999/monetary-policy-committee-february-1999":
            ("Minutes of the MPC Meeting held on 3 and 4 February 1999",
             f"{BOE}/-/media/boe/files/minutes/1999/minutes-february-1999.pdf"),
        f"{BOE}/minutes/2002/monetary-policy-committee-august-2002":
            ("Minutes of the MPC Meeting held on 31 July and 1 August 2002",
             f"{BOE}/-/media/boe/files/minutes/2002/minutes-august-2002.pdf"),
        f"{BOE}/monetary-policy-summary-and-minutes/2021/june-2021":
            ("Bank Rate maintained at 0.1% - June 2021",
             f"{BOE}/-/media/boe/files/monetary-policy-summary-and-minutes/2021/june-2021.pdf"),
        # the title names nothing: the page URL gives the month
        f"{BOE}/monetary-policy-summary-and-minutes/2018/march-2018":
            ("Bank Rate maintained at 0.5%",
             f"{BOE}/-/media/boe/files/monetary-policy-summary-and-minutes/2018/march-2018.pdf"),
    })
    pipeline.run_boe_recovery("minutes", DocType.A3, "x", dry_run=True,
                              config=Config(data_dir=tmp_path))
    assert [_dated(r) for r in captured] == [
        ("1999-02-04", "day", "meeting"), ("2002-08-01", "day", "meeting"),
        ("2021-06-01", "month", "meeting"), ("2018-03-01", "month", "meeting")]
    assert all(r.date_source == "bank_site" for r in captured)
    assert_backfill_agrees(captured)


def test_boe_recovery_report_is_a_month(tmp_path, monkeypatch, captured):
    _boe(monkeypatch, {
        f"{BOE}/monetary-policy-report/2025/november-2025":
            ("Monetary Policy Report - November 2025",
             f"{BOE}/-/media/boe/files/monetary-policy-report/2025/november/"
             "monetary-policy-report-november-2025.pdf"),
        f"{BOE}/monetary-policy-report/2026/july-2026":
            ("Monetary Policy Report - July 2026 (to be published at 12pm)", None),
    })
    pipeline.run_boe_recovery("monetary-policy-report", DocType.E1, "x", dry_run=True,
                              config=Config(data_dir=tmp_path))
    assert [_dated(r) for r in captured] == [("2025-11-01", "month", "period"),
                                             ("2026-07-01", "month", "period")]
    assert_backfill_agrees(captured)


def test_boe_recovery_falls_back_to_the_year_without_evidence(tmp_path, monkeypatch, captured):
    _boe(monkeypatch, {f"{BOE}/minutes/2010/x": ("T", None)})
    pipeline.run_boe_recovery("minutes", DocType.A3, "x", dry_run=True,
                              config=Config(data_dir=tmp_path))
    [rec] = captured
    assert _dated(rec) == ("2010-01-01", "year", "meeting")
    assert plan_row(rec.to_row()).action == "no-evidence"
    assert plan_row(rec.to_row()).updates == {}


def test_boe_recovery_ignores_title_evidence_outside_the_page_year(tmp_path, monkeypatch,
                                                                    captured):
    """A page filed under 2014 whose title names December 2013: the title is
    not trusted over the URL year (the backfill would call it a mismatch)."""
    _boe(monkeypatch, {f"{BOE}/minutes/2014/mpc": (
        "Minutes of the MPC Meeting held on 17 and 18 December 2013", None)})
    pipeline.run_boe_recovery("minutes", DocType.A3, "x", dry_run=True,
                              config=Config(data_dir=tmp_path))
    [rec] = captured
    assert _dated(rec) == ("2014-01-01", "year", "meeting")
    assert plan_row(rec.to_row()).updates == {}


def test_boe_recovery_other_doc_types_keep_the_year(tmp_path, monkeypatch, captured):
    _boe(monkeypatch, {f"{BOE}/financial-stability-report/2024/june-2024": (
        "Financial Stability Report - June 2024", None)})
    pipeline.run_boe_recovery("financial-stability-report", DocType.E2, "x", dry_run=True,
                              config=Config(data_dir=tmp_path))
    [rec] = captured
    assert (rec.date, rec.date_precision) == (date(2024, 1, 1), "year")
    assert "date_kind" not in rec.to_row()


# ---- ECB publications -------------------------------------------------

def test_ecb_projections_are_a_month_whatever_the_hash(tmp_path, monkeypatch, captured):
    import cb_corpus.sources.ecb_pub as ecb_pub
    urls = ["https://www.ecb.europa.eu/press/projections/html/"
            "ecb.projections202603_eurosystemstaff~da4f97a747.en.html",
            # neither a digit-only hash nor a dated folder before the file
            # name may be read as the date: only `projections<YYYYMM>` is
            "https://www.ecb.europa.eu/press/projections/200712/html/"
            "ecb.projections202509_ecbstaff~2012041588.en.html"]
    monkeypatch.setattr(ecb_pub, "section_include_docs",
                        lambda f, section, year, exts=None: urls if year == 2026 else [])
    pipeline.run_ecb_pub_recovery("projections", DocType.E1, mime="text/html",
                                  since_year=2026, config=Config(data_dir=tmp_path))
    assert [_dated(r) for r in captured] == [("2026-03-01", "month", "period"),
                                             ("2025-09-01", "month", "period")]
    assert_backfill_agrees(captured)


# ---- the other producers of the policy core already agree -------------

def test_fed_and_ecb_native_rows_need_no_backfill():
    fed = ('<a href="/newsevents/pressreleases/monetary20180801a.htm">s</a>'
           '<a href="/monetarypolicy/files/fomcminutes20230201.pdf">m</a>'
           '<a href="/monetarypolicy/files/fomcprojtabl20210317.pdf">p</a>')
    ad = get_adapter("us", _Pages({"fomccalendars": fed, "fomchistorical": "<html/>"}))
    recs = [r for dt in (DocType.A2, DocType.A3, DocType.F1)
            for r in ad._discover_native(dt, date(2015, 1, 1))]
    assert sorted(_dated(r) for r in recs) == [
        ("2018-08-01", "day", "meeting"), ("2021-03-17", "day", "meeting"),
        ("2023-02-01", "day", "meeting")]
    assert_backfill_agrees(recs)

    idx = ('<div id="lazyload-container" '
           'data-snippets="/x/2025/html/index_include.en.html"></div>')
    year = ('<a href="/press/pr/date/2025/html/ecb.mp250501~58b0e415a6.en.html">d</a>'
            '<a href="/press/press_conference/monetary-policy-statement/2025/html/'
            'ecb.is250911~a13675b834.en.html">s</a>'
            '<a href="/press/accounts/2025/html/ecb.mg250710~aa.en.html">a</a>')
    ad = get_adapter("ecb", _Pages({"index_include": year, "": idx}))
    recs = [r for dt in (DocType.A1, DocType.A2, DocType.A3) for r in ad._discover_native(dt, None)]
    assert sorted(_dated(r) for r in recs) == [
        ("2025-05-01", "day", "meeting"), ("2025-07-10", "day", "publication"),
        ("2025-09-11", "day", "meeting")]
    assert_backfill_agrees(recs)


def test_listing_pdf_recovery_rows_need_no_backfill(tmp_path, monkeypatch, captured):
    from cb_corpus.http import Fetcher
    monkeypatch.setattr(Fetcher, "get_text",
                        lambda self, url: "see FOMCpresconf20230201 transcript")
    pipeline.run_listing_pdf_recovery(
        "us", DocType.B1, ["https://www.federalreserve.gov/listing.htm"],
        r"FOMCpresconf(\d{8})", "FOMC press conference transcript",
        url_template="https://www.federalreserve.gov/mediacenter/files/FOMCpresconf{d}.pdf",
        config=Config(data_dir=tmp_path), dry_run=True)
    assert [_dated(r) for r in captured] == [("2023-02-01", "day", "meeting")]
    assert_backfill_agrees(captured)


# ---- the nightly sync neither re-downloads nor rewrites ---------------

def test_sync_after_the_backfill_leaves_known_rows_alone(tmp_path):
    """A BoJ minutes row stored undated (as today), backfilled, then met again
    by the fixed adapter: skipped before any download, the manifest keeps the
    backfilled bytes; only the genuinely new document is saved, already right."""
    import json

    from cb_corpus.policy_dates import run_policy_dates
    url = "https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2026/g260123.pdf"
    row = {"bank_code": "jp", "doc_type": "A3",
           "title": "Minutes [PDF 292KB]", "document_url": url,
           "source_url": "https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2026/index.htm",
           "date": None, "language": "en", "provenance": "bank_site",
           "mime_type": "application/pdf", "sha256": "aa" * 32,
           "local_path": "data/raw/jp/A3/0/x.pdf", "doc_id": compute_doc_id("jp", "A3", url),
           "year": None, "html_path": None, "date_precision": "day",
           "date_source": "bank_site", "repec_handle": "", "alt_urls": []}
    cfg = Config(data_dir=tmp_path, html_to_pdf=False)
    cfg.manifest_dir.mkdir(parents=True)
    cfg.manifest_file("jp").write_text(json.dumps(row, ensure_ascii=False) + "\n")
    assert run_policy_dates(config=cfg, write=True, csv_path=str(tmp_path / "p.csv"))["written"] == 1
    backfilled = cfg.manifest_file("jp").read_text()
    assert json.loads(backfilled)["date"] == "2026-01-23"
    # file not moved; the rewrite stores the path in the contract form
    assert json.loads(backfilled)["local_path"] == "raw/jp/A3/0/x.pdf"

    class Spy(_Pages):
        fetched: list = []

        def get_bytes(self, u):
            assert u != url, "re-downloaded a known document"
            self.fetched.append(u)
            return (pdf_body(b"%PDF-1.4 new"), "application/pdf")

        def throttle(self, u):
            pass

    fetcher = Spy({"minu_2026": BOJ_HTML, "": "<html></html>"})
    storage = Storage(cfg, fetcher)
    adapter = get_adapter("jp", fetcher)
    adapter._skip_known_url = storage.is_known_url        # what pipeline.run() sets
    recs = list(adapter.discover(DocType.A3))
    assert [r.document_url.rsplit("/", 1)[-1] for r in recs] == ["g260501.pdf"]
    # even without the pre-filter, the identity is unchanged: save() skips it
    known = next(iter(get_adapter("jp", fetcher)._discover_native(DocType.A3, None)))
    assert storage.save(known) == "skip:already-indexed"

    assert storage.save_many(recs).get("saved") == 1
    lines = cfg.manifest_file("jp").read_text().splitlines()
    assert lines[0] + "\n" == backfilled                  # the backfilled row: same bytes
    new = json.loads(lines[1])
    assert (new["date"], new["date_precision"], new["date_kind"]) == (
        "2026-05-01", "day", "meeting")
    # and a second backfill run has nothing to do on either row
    again = run_policy_dates(config=cfg, write=True, csv_path=str(tmp_path / "q.csv"))
    assert again["written"] == 0 and again["unchanged"] == 2
