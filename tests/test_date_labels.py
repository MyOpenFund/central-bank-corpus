"""B2: honest `date_precision` / `date_source` at the recovery and sitemap
call sites (grit wave 2026-11). New rows only — no backfill. One test per
site, on the emitted DocRecord. No network: every source is a fake."""
from __future__ import annotations

import json
from datetime import date

import pytest

from cb_corpus import pipeline
from cb_corpus.config import Config
from cb_corpus.models import DocRecord
from cb_corpus.storage import Storage
from cb_corpus.taxonomy import DocType


@pytest.fixture
def captured(monkeypatch):
    """Capture what a pipeline recovery hands to save_many, without saving."""
    out: list[DocRecord] = []

    def fake_save_many(self, recs, **kw):
        out.extend(recs)
        return {}
    monkeypatch.setattr(Storage, "save_many", fake_save_many)
    return out


def test_docrecord_accepts_none_precision_and_serializes_null():
    rec = DocRecord(bank_code="de", doc_type=DocType.D1, title="t",
                    pdf_url="https://x/a.pdf", date=None, date_precision=None)
    assert rec.to_row()["date_precision"] is None


# ---- BoE (pipeline) ---------------------------------------------------

def test_boe_wp_recovery_is_year_precision(tmp_path, monkeypatch, captured):
    import cb_corpus.sources.boe_wp as boe_wp
    monkeypatch.setattr(boe_wp, "sitemap_pages", lambda f, years: [
        (date(2006, 1, 1), "https://boe/wp/2006/x", "https://boe/media/2006/x.pdf")])
    monkeypatch.setattr(boe_wp, "paper_pdf", lambda f, page: ("T", "https://boe/real/x.pdf"))
    pipeline.run_boe_wp_recovery(dry_run=True, config=Config(data_dir=tmp_path))
    [rec] = captured
    assert rec.date == date(2006, 1, 1)
    assert (rec.date_precision, rec.date_source) == ("year", "bank_site")


def test_boe_recovery_is_year_precision(tmp_path, monkeypatch, captured):
    import cb_corpus.sources.boe_wp as boe_wp
    monkeypatch.setattr(boe_wp, "doc_pages", lambda f, sp, hf, years: [
        (date(2010, 1, 1), "https://boe/minutes/2010/x")])
    monkeypatch.setattr(boe_wp, "page_doc", lambda f, page: ("T", None))
    pipeline.run_boe_recovery("minutes", DocType.A3, "x", dry_run=True,
                              config=Config(data_dir=tmp_path))
    [rec] = captured
    assert (rec.date_precision, rec.date_source) == ("year", "bank_site")


# ---- Wayback source ---------------------------------------------------

class _Cdx:
    def __init__(self, rows):
        self.rows = rows

    def get_text(self, url):
        return json.dumps([["original", "timestamp"], *self.rows])


def test_wayback_source_is_year_wayback():
    from cb_corpus.sources.wayback import WaybackSource
    src = WaybackSource("se", "riksbank.se/wp", DocType.D1)
    recs = list(src.items(_Cdx([["http://riksbank.se/wp/1999/a.pdf", "2017"],
                                ["http://riksbank.se/wp/undated/b.pdf", "2017"]]), None))
    assert recs[0].date == date(1999, 1, 1)
    assert (recs[0].date_precision, recs[0].date_source) == ("year", "wayback")
    assert recs[1].date is None
    assert (recs[1].date_precision, recs[1].date_source) == (None, "wayback")


# ---- ECB publication recovery ----------------------------------------

@pytest.mark.parametrize("fmt,fn,expected_date,prec", [
    ("yyyymm", "ecb.fsr200612~h.en.pdf", date(2006, 12, 1), "month"),
    ("yymmdd", "ecb.in200612~h.en.pdf", date(2020, 6, 12), "day"),
    ("yyyymmdd", "ecb.x20200612~h.en.pdf", date(2020, 6, 12), "day"),
    ("yyyy", "ecb.ar2019~h.en.pdf", date(2019, 1, 1), "year"),
    ("auto", "ecb.x20200612~h.en.pdf", date(2020, 6, 12), "day"),
    ("auto", "ecb.fsr200612~h.en.pdf", date(2006, 12, 1), "month"),
    ("auto", "ecb.in240115~h.en.pdf", date(2024, 1, 15), "day"),
    ("auto", "ecb.ar2019en~h.en.pdf", date(2019, 1, 1), "year"),
])
def test_ecb_pub_recovery_precision_by_url_format(tmp_path, monkeypatch, captured,
                                                  fmt, fn, expected_date, prec):
    import cb_corpus.sources.ecb_pub as ecb_pub
    url = f"https://www.ecb.europa.eu/pub/{fn}"
    monkeypatch.setattr(ecb_pub, "section_include_docs",
                        lambda f, section, year, exts=None: [url] if year == 2020 else [])
    pipeline.run_ecb_pub_recovery("fsr", DocType.E2, since_year=2020, date_fmt=fmt,
                                  dry_run=True, config=Config(data_dir=tmp_path))
    [rec] = captured
    assert rec.date == expected_date
    assert (rec.date_precision, rec.date_source) == (prec, "bank_site")


def test_ecb_date_from_url_unchanged():
    from cb_corpus.sources.ecb_pub import date_from_url
    assert date_from_url("x/ecb.fsr200612~h.en.pdf", "yyyymm") == date(2006, 12, 1)
    assert date_from_url("x/nothing.en.pdf") is None


# ---- generic sitemap (declarative adapters) ---------------------------

def test_generic_sitemap_lastmod_labelled_as_such():
    from cb_corpus.adapters.generic_sitemap import GenericSitemapAdapter
    from cb_corpus.banks import get_bank
    xml = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://bank.example/pub/a.pdf</loc><lastmod>2021-03-04</lastmod></url>
  <url><loc>https://bank.example/pub/2020-05-06-b.pdf</loc></url>
  <url><loc>https://bank.example/pub/c.pdf</loc></url>
</urlset>"""

    class F:
        def get_text(self, url):
            return xml
    ad = GenericSitemapAdapter(get_bank("ch"), F(), sitemap_url="https://bank.example/sm.xml",
                               patterns={DocType.E1: r"/pub/"})
    recs = {r.pdf_url.rsplit("/", 1)[-1]: r for r in ad._discover_native(DocType.E1, None)}
    a, b, c = recs["a.pdf"], recs["2020-05-06-b.pdf"], recs["c.pdf"]
    assert (a.date, a.date_precision, a.date_source) == (date(2021, 3, 4), "day", "sitemap_lastmod")
    assert (b.date, b.date_precision, b.date_source) == (date(2020, 5, 6), "day", "bank_site")
    assert (c.date, c.date_precision, c.date_source) == (None, None, "bank_site")


# ---- None-date sites in the native WP sources -------------------------

def test_buba_wp_none_date_has_no_precision(monkeypatch, fetcher_factory):
    import cb_corpus.sources.buba_wp as buba_wp
    monkeypatch.setattr(buba_wp, "parse_listing_page", lambda html: [
        ("T", "https://www.bundesbank.de/resource/blob/1/nodate.pdf", None)])
    f = fetcher_factory({"pageNumString=0": "<html></html>"})
    [rec] = list(buba_wp.discover_buba_wp(f, max_pages=1))
    assert rec.date is None and rec.date_precision is None


def test_riksbank_wp_none_date_has_no_precision(monkeypatch, fetcher_factory):
    import cb_corpus.sources.riksbank_wp as rb
    monkeypatch.setattr(rb, "parse_wp_listing", lambda html: [
        ("2001:1", None, "T", "https://www.riksbank.se/a.pdf")])
    f = fetcher_factory({"": "<html></html>"})
    [rec] = list(rb.discover_riksbank_wp(f, max_pages=1))
    assert rec.date is None and rec.date_precision is None


def test_boj_wp_none_date_has_no_precision():
    from cb_corpus.sources.boj_wp import parse_wp_table
    html = ('<table><tr><td>not a date</td>'
            '<td><a href="/en/research/wps_rev/wps_2026/data/wp26e01.pdf">PDF</a></td></tr>'
            '<tr><td>Apr. 2002</td>'
            '<td><a href="/en/research/wps_rev/wps_2026/data/wp02e01.pdf">PDF</a></td></tr>'
            '</table>')
    rows = parse_wp_table(html, "https://www.boj.or.jp/en/research/wps_rev/wps_2026/index.htm")
    assert rows[0][0] is None and rows[0][1] is None
    assert rows[1][0] == date(2002, 4, 1) and rows[1][1] == "month"


def test_fed_wp_none_date_has_no_precision(monkeypatch, fetcher_factory):
    import cb_corpus.sources.fed_wp as fed_wp
    monkeypatch.setattr(fed_wp, "SERIES", {"feds": "https://fed/feds/all-years.htm"})
    monkeypatch.setattr(fed_wp, "parse_year_links", lambda page, series: [
        ("feds", 2020, 1, None, "https://fed/landing", "T")])
    monkeypatch.setattr(fed_wp, "parse_landing", lambda html: (None, "https://fed/x.pdf"))
    f = fetcher_factory({"all-years.htm": "/econres/feds/2020.htm",
                         "2020.htm": "<html></html>", "landing": "<html></html>"})
    [rec] = list(fed_wp.discover_fed_wp(f))
    assert rec.date is None and rec.date_precision is None


def test_wp_migrate_undated_native_keeps_the_rows_own_precision(monkeypatch):
    """B2 knock-on: an undated native record now carries date_precision=None;
    the WP migration keeps the row's date then, so it must keep the row's own
    precision too -- never fall back to a fake "day"."""
    from cb_corpus import wp_migrate
    native = [DocRecord(bank_code="fr", doc_type=DocType.D1, title="P",
                        pdf_url="https://www.banque-france.fr/system/files/2006-03/WP155.pdf",
                        date=None, date_precision=None)]
    monkeypatch.setitem(wp_migrate._NATIVE, "fr", lambda fetcher: iter(native))
    row = {"doc_id": "fr155", "bank_code": "fr", "doc_type": "D1", "date": "2006-03-01",
           "date_precision": "month", "date_source": "repec",
           "pdf_url": "https://www.banque-france.fr/system/files/2006-03/WP155.pdf"}
    _summary, [c] = wp_migrate.build_report("fr", fetcher=None, manifest_rows=[row])
    assert c["new_date"] == "2006-03-01"
    assert c["date_precision"] == "month"
    wp_migrate.apply_change(row, c)
    assert row["date_precision"] == "month"
