"""Honest default precision: a record built without a stated precision is
"unknown" (None), never "day"; an undated record has no precision. Every
producer that does read a day states it, so its rows are labelled exactly as
before. No network: every source is a fake."""
from __future__ import annotations

from datetime import date

from cb_corpus import pipeline
from cb_corpus.adapters.base import get_adapter
from cb_corpus.config import Config
from cb_corpus.models import DocRecord
from cb_corpus.storage import iter_manifest_rows
from cb_corpus.taxonomy import DocType


class _Pages:
    def __init__(self, pages):
        self.pages = pages

    def get_text(self, url):
        for key, body in self.pages.items():
            if key in url:
                return body
        raise RuntimeError(f"fake 404 {url}")


def test_a_record_without_a_stated_precision_does_not_claim_a_day():
    rec = DocRecord(bank_code="ch", doc_type=DocType.A2, title="t",
                    pdf_url="https://x/a.pdf", date=date(2020, 5, 1))
    assert rec.date_precision is None
    assert rec.to_row()["date_precision"] is None


def test_an_undated_row_never_carries_a_precision():
    rec = DocRecord(bank_code="ch", doc_type=DocType.A2, title="t",
                    pdf_url="https://x/a.pdf", date=None, date_precision="day")
    assert rec.to_row()["date_precision"] is None
    assert rec.to_row()["date"] is None


def test_a_stated_precision_is_kept():
    rec = DocRecord(bank_code="ch", doc_type=DocType.A2, title="t",
                    pdf_url="https://x/a.pdf", date=date(2020, 5, 1), date_precision="month")
    assert rec.to_row()["date_precision"] == "month"


# ---- producers that read a real day keep saying "day" -----------------

def test_fed_native_types_state_day_precision():
    html = ('<a href="/newsevents/pressreleases/monetary20150128a.htm">s</a>'
            '<a href="/monetarypolicy/files/fomcminutes20150128.pdf">m</a>'
            '<a href="/monetarypolicy/files/fomcprojtabl20210317.pdf">p</a>')
    ad = get_adapter("us", _Pages({"fomccalendars": html, "fomchistorical": "<html/>"}))
    for dt in (DocType.A2, DocType.A3, DocType.F1):
        recs = list(ad._discover_native(dt, date(2015, 1, 1)))
        assert recs and all(r.date_precision == "day" for r in recs), dt


def test_ecb_decisions_statements_accounts_state_day_precision():
    idx = ('<div id="lazyload-container" '
           'data-snippets="/x/2025/html/index_include.en.html"></div>')
    year = ('<a href="/press/pr/date/2025/html/ecb.mp251218~58b0e415a6.en.html">d</a>'
            '<a href="/press/press_conference/monetary-policy-statement/2025/html/'
            'ecb.is250911~a13675b834.en.html">s</a>'
            '<a href="/press/accounts/2025/html/ecb.mg250710~aa.en.html">a</a>')
    from cb_corpus.adapters import ecb
    ad = get_adapter("ecb", _Pages({"index_include": year, "": idx}))
    assert ecb.parse_year_includes(idx), "fixture must expose one year include"
    for dt in (DocType.A1, DocType.A2, DocType.A3):
        recs = list(ad._discover_native(dt, None))
        assert recs and all(r.date_precision == "day" for r in recs), dt


def test_rba_decisions_and_minutes_state_day_precision():
    html = ('<a href="/media-releases/2025/mr-25-33.html">9 December 2025</a>'
            '<a href="/monetary-policy/rba-board-minutes/2025/2025-11-04.html">x</a>')
    ad = get_adapter("au", _Pages({"": html}))
    for dt in (DocType.A1, DocType.A3):
        recs = list(ad._discover_native(dt, date(2025, 1, 1)))
        assert recs and all(r.date_precision == "day" for r in recs), dt


def test_bis_speeches_state_day_precision():
    from cb_corpus.sources.bis_speeches import BISSpeechIndex, parse_year_sitemap
    index = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             '<sitemap><loc>https://www.bis.org/sitemap_documents_2024.xml</loc></sitemap>'
             '</sitemapindex>')
    year = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            '<url><loc>https://www.bis.org/review/r240115a.pdf</loc></url></urlset>')
    assert parse_year_sitemap(year), "fixture must expose one speech"
    detail = ('<html><head><meta property="og:title" content="A speech">'
              '<meta property="og:description" content="Speech by X, Governor of the '
              'Bank of England, in London."></head></html>')
    f = _Pages({"documents_2024": year, "sitemap": index, "r240115a": detail})
    recs = list(BISSpeechIndex(f).discover())
    assert recs and all(r.date_precision == "day" for r in recs)


def test_listing_pdf_recovery_states_day_precision(tmp_path, monkeypatch):
    from cb_corpus.http import Fetcher
    monkeypatch.setattr(Fetcher, "get_text",
                        lambda self, url: "see FOMCpresconf20230322 transcript")
    monkeypatch.setattr(Fetcher, "get_bytes", lambda self, u: (b"%PDF-1.4 x", "application/pdf"))
    monkeypatch.setattr(Fetcher, "throttle", lambda self, u: None)
    cfg = Config(data_dir=tmp_path, html_to_pdf=False)
    pipeline.run_listing_pdf_recovery(
        "us", DocType.B1, ["https://www.federalreserve.gov/listing.htm"],
        r"FOMCpresconf(\d{8})", "Press conf",
        url_template="https://www.federalreserve.gov/mediacenter/files/FOMCpresconf{d}.pdf",
        config=cfg, dry_run=False)
    [row] = list(iter_manifest_rows(cfg))
    assert (row["date"], row["date_precision"]) == ("2023-03-22", "day")
