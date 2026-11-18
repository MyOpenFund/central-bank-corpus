"""Tests for the fetch/recovery layer — the `run_*_recovery` pipeline functions
and native adapter discovery — with the network faked (monkeypatched Fetcher).

These target the code that actually issues HTTP requests (previously the least
covered: pipeline.py was at 27%). Each test serves canned HTML / CDX-JSON and
asserts the records that get discovered + saved.
"""
import json
import re
from datetime import date

from cb_corpus import pipeline
from cb_corpus.config import Config
from cb_corpus.taxonomy import DocType
from cb_corpus.adapters.base import get_adapter


def _patch_net(monkeypatch, pages, doc=(b"%PDF-1.4 x", "application/pdf")):
    """Make every Fetcher serve `pages` (url-substring -> text or callable)."""
    from cb_corpus.http import Fetcher

    def get_text(self, url):
        for k, v in pages.items():
            if k in url:
                return v(url) if callable(v) else v
        raise RuntimeError(f"fake 404 {url}")

    monkeypatch.setattr(Fetcher, "get_text", get_text)
    monkeypatch.setattr(Fetcher, "get_bytes", lambda self, u: doc)
    monkeypatch.setattr(Fetcher, "throttle", lambda self, u: None)


class _FakeFetcher:
    """A fetcher instance for adapter tests (adapters take a fetcher directly)."""

    def __init__(self, pages):
        self.pages = pages

    def get_text(self, url):
        for k, v in self.pages.items():
            if k in url:
                return v(url) if callable(v) else v
        raise RuntimeError(f"fake 404 {url}")


def _manifest(cfg):
    from cb_corpus.storage import iter_manifest_rows
    return list(iter_manifest_rows(cfg))


# --- run_ecb_pub_recovery: PRIMARY (per-section/year static include) ---
def test_run_ecb_pub_recovery_uses_include_primary(tmp_path, monkeypatch):
    def inc(url):
        y = re.search(r"/date/(\d{4})/", url).group(1)
        return f'<a href="/press/inter/date/{y}/html/ecb.in{y[2:]}0115~ab.en.html">Itw</a>'

    _patch_net(monkeypatch, {"/press/inter/date/": inc}, doc=(b"<html>i</html>", "text/html"))
    cfg = Config(data_dir=tmp_path, html_to_pdf=False)
    cur = date.today().year
    r = pipeline.run_ecb_pub_recovery("inter", DocType.C2, mime="", since_year=cur,
                                      date_fmt="yymmdd", config=cfg)
    assert r.get("saved") == 1
    rec = _manifest(cfg)[0]
    assert rec["doc_type"] == "C2" and rec["date"].endswith("-01-15")


# --- run_ecb_pub_recovery: FALLBACK to CDX (+ English filter + name_filter) ---
def test_run_ecb_pub_recovery_falls_back_to_cdx(tmp_path, monkeypatch):
    cdx = json.dumps([["original", "timestamp"],
        ["https://www.ecb.europa.eu/pub/fsr/ecb.fsr202405~h.en.pdf", "20240501000000"],
        ["https://www.ecb.europa.eu/pub/fsr/ecb.fsr202405~h.fr.pdf", "20240501000000"]])
    _patch_net(monkeypatch, {"web.archive.org/cdx": cdx})   # includes 404 -> fallback
    cfg = Config(data_dir=tmp_path, html_to_pdf=False)
    r = pipeline.run_ecb_pub_recovery(
        "financial-stability-publications/fsr", DocType.E2,
        cdx_fallback_prefix="ecb.europa.eu/pub/fsr", date_fmt="yyyymm",
        name_filter=r"ecb\.fsr", config=cfg)
    assert r.get("saved") == 1                              # the .fr.pdf is filtered out
    rec = _manifest(cfg)[0]
    assert rec["date"] == "2024-05-01" and rec["pdf_url"].endswith(".en.pdf")


# --- run_wayback_recovery: year derived from URL, wayback provenance ---
def test_run_wayback_recovery_dates_from_url(tmp_path, monkeypatch):
    cdx = json.dumps([["original", "timestamp"],
                      ["https://x.gov/files/2010/paper.pdf", "20150101000000"]])
    _patch_net(monkeypatch, {"web.archive.org/cdx": cdx})
    cfg = Config(data_dir=tmp_path, html_to_pdf=False)
    r = pipeline.run_wayback_recovery("us", "x.gov/files", DocType.D1,
                                      dry_run=False, config=cfg)
    assert r.get("saved") == 1
    rec = _manifest(cfg)[0]
    assert rec["year"] == 2010 and rec["provenance"] == "wayback"


# --- run_listing_pdf_recovery: default mode (group1=path, group2=YYYYMMDD) ---
def test_run_listing_pdf_recovery_default_mode(tmp_path, monkeypatch):
    _patch_net(monkeypatch, {"listing": '<a href="/files/BeigeBook_20230118.pdf">BB</a>'})
    cfg = Config(data_dir=tmp_path, html_to_pdf=False)
    r = pipeline.run_listing_pdf_recovery(
        "us", DocType.E4, ["https://www.federalreserve.gov/listing.htm"],
        r"(/files/BeigeBook_(\d{8})\.pdf)", "Beige Book", config=cfg, dry_run=False)
    assert r.get("saved") == 1
    assert _manifest(cfg)[0]["date"] == "2023-01-18"


# --- run_listing_pdf_recovery: url_template mode (group1=YYYYMMDD, URL built) ---
def test_run_listing_pdf_recovery_url_template_mode(tmp_path, monkeypatch):
    _patch_net(monkeypatch, {"listing": "see FOMCpresconf20230322 transcript"})
    cfg = Config(data_dir=tmp_path, html_to_pdf=False)
    r = pipeline.run_listing_pdf_recovery(
        "us", DocType.B1, ["https://www.federalreserve.gov/listing.htm"],
        r"FOMCpresconf(\d{8})", "Press conf",
        url_template="https://www.federalreserve.gov/mediacenter/files/FOMCpresconf{d}.pdf",
        config=cfg, dry_run=False)
    assert r.get("saved") == 1
    rec = _manifest(cfg)[0]
    assert rec["date"] == "2023-03-22" and rec["pdf_url"].endswith("FOMCpresconf20230322.pdf")


# --- run_boe_wp_recovery: sitemap -> page -> real PDF ---
def test_run_boe_wp_recovery(tmp_path, monkeypatch):
    _patch_net(monkeypatch, {
        "/sitemap/staff-working-paper": '<a href="/working-paper/2020/my-paper">P</a>',
        "/working-paper/2020/my-paper": (
            '<meta property="og:title" content="My Paper">'
            '<a href="/-/media/boe/files/working-paper/2020/my-paper.pdf">PDF</a>'),
    })
    cfg = Config(data_dir=tmp_path, html_to_pdf=False)
    r = pipeline.run_boe_wp_recovery(years=[2020], dry_run=False, config=cfg)
    assert r.get("saved") == 1
    rec = _manifest(cfg)[0]
    assert rec["bank_code"] == "gb" and rec["title"] == "My Paper" and rec["year"] == 2020


# --- run_boe_recovery: generic section (minutes A3) ---
def test_run_boe_recovery_generic_section(tmp_path, monkeypatch):
    _patch_net(monkeypatch, {
        "/sitemap/minutes": '<a href="/minutes/2020/mpc-january">M</a>',
        "/minutes/2020/mpc-january": (
            '<meta property="og:title" content="MPC January 2020">'
            '<a href="/-/media/boe/files/minutes/2020/jan.pdf">PDF</a>'),
    })
    cfg = Config(data_dir=tmp_path, html_to_pdf=False)
    r = pipeline.run_boe_recovery("minutes", DocType.A3, r"/minutes/\d{4}/mpc",
                                  years=[2020], dry_run=False, config=cfg)
    assert r.get("saved") == 1
    row = _manifest(cfg)[0]
    assert row["doc_type"] == "A3"
    # no longer a bare year: the title names the month, and the row says what
    # its date is (the first day of a month-precision date is not a day)
    assert (row["date"], row["date_precision"], row["date_kind"]) == (
        "2020-01-01", "month", "meeting")


# --- boe_wp source units: sitemap_pages dedup + paper_pdf ---
def test_boe_sitemap_pages_and_paper_pdf():
    from cb_corpus.sources.boe_wp import sitemap_pages, paper_pdf
    f = _FakeFetcher({
        "/sitemap/staff-working-paper":
            '<a href="/working-paper/2019/a-paper">a</a>'
            '<a href="/working-paper/2019/a-paper">dup</a>'       # deduped by (year, slug)
            '<a href="/news/x">skip</a>',
        "/working-paper/2019/a-paper":
            '<title>A Paper</title>'
            '<a href="/-/media/boe/files/working-paper/2019/real.pdf">pdf</a>',
    })
    pages = sitemap_pages(f, years=[2019])
    assert len(pages) == 1 and pages[0][0] == date(2019, 1, 1)
    title, pdf = paper_pdf(f, pages[0][1])
    assert title == "A Paper" and pdf.endswith("/working-paper/2019/real.pdf")


# --- RBA adapter discovery: A3 minutes (both URL date formats) + E1 SMP ---
def test_rba_adapter_discovers_minutes_and_smp():
    pages = {
        "/rba-board-minutes/2025/":
            '<a href="/monetary-policy/rba-board-minutes/2025/2025-11-04.html">x</a>',
        "/rba-board-minutes/2010/":
            '<a href="/monetary-policy/rba-board-minutes/2010/05102010.html">y</a>',
        "/publications/smp/":
            '<a href="/publications/smp/2026/feb/">s</a>'
            '<a href="/publications/smp/2026/boxes.html">skip</a>',
    }
    au = get_adapter("au", _FakeFetcher(pages))
    recs = list(au.discover_all(scope=(DocType.A3, DocType.E1)))
    a3 = sorted(r.date.isoformat() for r in recs if r.doc_type is DocType.A3)
    e1 = [r.date.isoformat() for r in recs if r.doc_type is DocType.E1]
    assert a3 == ["2010-10-05", "2025-11-04"]      # legacy DDMMYYYY + modern YYYY-MM-DD
    assert e1 == ["2026-02-01"]                    # quarterly issue only, not boxes.html
    # the minutes name a day; the issue date is the first day of a MONTH
    assert {r.date_precision for r in recs if r.doc_type is DocType.A3} == {"day"}
    assert {r.date_precision for r in recs if r.doc_type is DocType.E1} == {"month"}


# --- Fed adapter discovery: A2 statements + A3 minutes + F1 SEP from CAL page ---
def test_fed_adapter_discovers_statements_minutes_sep():
    cal = ('<a href="/newsevents/pressreleases/monetary20230322a.htm">stmt</a>'
           '<a href="/monetarypolicy/files/fomcminutes20230322.pdf">min</a>'
           '<a href="/monetarypolicy/files/fomcprojtabl20230322.pdf">sep</a>')
    us = get_adapter("us", _FakeFetcher({"fomccalendars": cal}))   # historical pages 404
    recs = list(us.discover_all(scope=(DocType.A2, DocType.A3, DocType.F1)))
    assert {"A2", "A3", "F1"} <= {r.doc_type.code for r in recs}
    assert all(r.date.isoformat() == "2023-03-22" for r in recs)


# --- ECB adapter discovery: A1 decisions + A3 accounts from year-include pages ---
def test_ecb_adapter_discovers_decisions_and_accounts():
    # Two-step lazy-load flow: index `data-snippets` -> per-year include -> docs.
    pages = {
        "/press/govcdec/mopo/html/index.en.html":
            '<div id="lazyload-container" '
            'data-snippets="/press/govcdec/mopo/html/y2024.en.html"></div>',
        "/press/govcdec/mopo/html/y2024.en.html":
            '<a href="/press/pr/date/2024/html/ecb.mp240307~h.en.html">decision</a>',
        "/press/accounts/html/index.en.html":
            '<div id="lazyload-container" '
            'data-snippets="/press/accounts/2024/html/y.en.html"></div>',
        "/press/accounts/2024/html/y.en.html":
            '<a href="/press/accounts/2024/html/ecb.mg240404.en.html">account</a>',
    }
    ecb = get_adapter("ecb", _FakeFetcher(pages))
    recs = list(ecb.discover_all(scope=(DocType.A1, DocType.A3)))
    by = {r.doc_type.code: r for r in recs}
    assert by["A1"].date.isoformat() == "2024-03-07"     # mp240307 -> yymmdd
    assert by["A3"].date.isoformat() == "2024-04-04"     # mg240404 -> yymmdd


# --- GenericSitemapAdapter: walk sitemap.xml, filter URLs by per-type regex ---
def test_generic_sitemap_adapter_discovers_from_sitemap():
    sitemap = (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<url><loc>https://www.snb.ch/x/publikationen/medienmitteilungen/2023/a.de.pdf</loc>'
        '<lastmod>2023-03-01</lastmod></url>'
        '<url><loc>https://www.snb.ch/x/publikationen/finanzstabilitaetsbericht/2023/fsr.pdf</loc></url>'
        '<url><loc>https://www.snb.ch/x/other/skip.html</loc></url>'
        '</urlset>')
    ch = get_adapter("ch", _FakeFetcher({"sitemap.xml": sitemap}))
    got = {r.doc_type.code for r in ch.discover_all(scope=(DocType.A2, DocType.E2))}
    assert "A2" in got and "E2" in got                   # medienmitteilung + FSR matched


# --- ListingCrawlerAdapter: scrape {year} listing pages, filter <a> by regex ---
def test_listing_crawler_adapter_discovers_from_listing():
    page = '<a href="/en/mopo/mpmsche_minu/minu_2020/g200121.pdf">minutes</a>'
    jp = get_adapter("jp", _FakeFetcher({"minu_2020": page}))   # other years 404
    recs = list(jp.discover_all(scope=(DocType.A3,)))
    assert any(r.pdf_url.endswith("g200121.pdf") and r.doc_type is DocType.A3
               for r in recs)


# --- ecb_pub.date_from_url: every explicit format + auto branches ---
def test_ecb_pub_date_from_url_every_branch():
    from cb_corpus.sources.ecb_pub import date_from_url
    assert date_from_url("x/ecb.sp20230118~h.en.pdf", "yyyymmdd").isoformat() == "2023-01-18"
    assert date_from_url("x/ecb.in230118~h.en.html", "yymmdd").isoformat() == "2023-01-18"
    assert date_from_url("x/ecb.fsr202405~h.en.pdf", "yyyymm").isoformat() == "2024-05-01"
    assert date_from_url("x/ar2016en.pdf", "yyyy").isoformat() == "2016-01-01"
    # auto: 8-digit, 6-digit (YYYYMM vs YYMMDD), then year-only, then None
    assert date_from_url("x/fomcminutes20230118.pdf", "auto").isoformat() == "2023-01-18"
    assert date_from_url("x/mb200612en.pdf", "auto").isoformat() == "2006-12-01"   # YYYYMM
    assert date_from_url("x/ar2016.pdf", "auto").isoformat() == "2016-01-01"       # year-only
    assert date_from_url("x/no-date-here.pdf", "auto") is None


# --- BIS speech index: sitemap index -> year sitemap -> detail -> bank attribution ---
def test_bis_speech_index_discovers_and_attributes_bank():
    from cb_corpus.sources.bis_speeches import BISSpeechIndex
    pages = {
        "/sitemap.xml":
            '<sitemapindex><sitemap><loc>'
            'https://www.bis.org/sitemap_documents_2023.xml</loc></sitemap></sitemapindex>',
        "sitemap_documents_2023.xml":
            '<urlset><url><loc>https://www.bis.org/review/r230118a.pdf</loc></url></urlset>',
        "/review/r230118a.htm":
            '<meta property="og:title" content="Monetary policy outlook">'
            '<meta property="og:description" content="Speech by Andrew Bailey, '
            'Governor of the Bank of England, at a conference">',
    }
    idx = BISSpeechIndex(_FakeFetcher(pages))
    recs = list(idx.discover(since=date(2023, 1, 1), until=date(2023, 12, 31)))
    assert len(recs) == 1
    assert recs[0].bank_code == "gb" and recs[0].doc_type is DocType.C1
    assert recs[0].date.isoformat() == "2023-01-18"     # r230118a -> yymmdd
    # skip_url short-circuits BEFORE the per-speech detail fetch
    assert list(idx.discover(since=date(2023, 1, 1), skip_url=lambda u: True)) == []


# --- Source abstraction: run_source drives a custom Source + honours html_to_pdf ---
def test_run_source_drives_a_custom_source(tmp_path, monkeypatch):
    from cb_corpus.models import DocRecord
    from cb_corpus.sources.recovery import Source

    class TwoDocs(Source):
        label = "test"
        html_to_pdf = False            # overrides the config flag below

        def items(self, fetcher, storage):
            for n in ("a", "b", "a"):  # 'a' twice -> dedup within the pass
                u = f"https://x.gov/{n}.pdf"
                if storage.is_known_url(u):
                    continue
                yield DocRecord(bank_code="us", doc_type=DocType.D1, title=n,
                                pdf_url=u, date=date(2020, 1, 1),
                                mime_type="application/pdf")

    from cb_corpus.http import Fetcher
    # url-specific bytes so 'a' and 'b' aren't deduped by content sha256
    monkeypatch.setattr(Fetcher, "get_bytes", lambda self, u: (f"%PDF {u}".encode(), "application/pdf"))
    monkeypatch.setattr(Fetcher, "throttle", lambda self, u: None)
    cfg = Config(data_dir=tmp_path, html_to_pdf=True)
    r = pipeline.run_source(TwoDocs(), dry_run=False, config=cfg)
    assert r.get("saved") == 2         # 'a' and 'b'; the second 'a' was deduped by url
