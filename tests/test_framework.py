import json
from datetime import date
from pathlib import Path

import pytest

from cb_corpus.taxonomy import DocType, FULL_SCOPE
from cb_corpus.banks import BIS_63, get_bank, bank_for_bis_institution
from cb_corpus.adapters import ADAPTERS, INSTANCE_FACTORIES, get_adapter
from cb_corpus.adapters.generic_sitemap import (
    GenericSitemapAdapter, parse_sitemap, _date_from_url,
)
from cb_corpus.adapters.listing_crawler import (
    ListingCrawlerAdapter, parse_links,
)
from cb_corpus.adapters.fed import FedAdapter, parse_minutes_links
from cb_corpus.adapters.ecb import (
    ECBAdapter, parse_index, parse_year_includes, parse_account_items,
    parse_bulletin_pdfs, parse_blog_items, BLOG_INDEX,
)
import cb_corpus.sources.ecb_foedb as ecb_foedb
from cb_corpus.sources.bis_speeches import (
    parse_listing, parse_sitemap_index, parse_year_sitemap, parse_detail,
    _parse_slug_date, _guess_institution,
)
from cb_corpus.sources.repec import (
    parse_series_page, extract_official_pdf, extract_pdf,
)
from cb_corpus.models import DocRecord
from cb_corpus.config import Config
from cb_corpus.storage import Storage
from cb_corpus.completeness import build_matrix, summarize


# ---- taxonomy --------------------------------------------------------
def test_full_scope_is_a_through_f_only():
    groups = {dt.group for dt in FULL_SCOPE}
    assert groups == {"A", "B", "C", "D", "E", "F"}
    assert all(dt.group != "G" for dt in FULL_SCOPE)
    assert DocType.A3 in FULL_SCOPE and DocType.G1 not in FULL_SCOPE


# ---- banks -----------------------------------------------------------
def test_exactly_63_banks_unique_codes_and_domains():
    assert len(BIS_63) == 63
    assert len({b.code for b in BIS_63}) == 63
    assert all(b.homepage and "." in b.homepage for b in BIS_63)


def test_bis_institution_mapping():
    assert bank_for_bis_institution("Bank of England").code == "gb"
    assert bank_for_bis_institution("not a bank") is None


# ---- adapter registry ------------------------------------------------
def test_all_63_have_an_adapter():
    for b in BIS_63:
        assert b.code in ADAPTERS
    assert isinstance(get_adapter("us"), FedAdapter)
    assert isinstance(get_adapter("ecb"), ECBAdapter)


def test_generic_adapter_supports_speeches_and_papers():
    a = get_adapter("se")  # no bespoke adapter -> generic
    sup = a.supported_types()
    assert DocType.C1 in sup and DocType.D1 in sup


def test_fed_expected_counts():
    a = get_adapter("us")
    assert a.expected_count(DocType.A3, 2024) == 8
    assert a.expected_count(DocType.F1, 2024) == 4


# ---- BIS speech parser ----------------------------------------------
BIS_FIXTURE = """
<table class="documentList">
  <tr>
    <td class="item_date">15 Mar 2024</td>
    <td class="title">
      <a href="/review/r240315a.htm">Inflation outlook</a>
      <div>Andrew Bailey, Governor of the Bank of England, speech</div>
      <a href="/review/r240315a.pdf">PDF</a>
    </td>
  </tr>
</table>
"""


def test_parse_bis_listing():
    items = parse_listing(BIS_FIXTURE)
    assert len(items) == 1
    it = items[0]
    assert it.date == date(2024, 3, 15)
    assert it.title == "Inflation outlook"
    assert it.institution == "Bank of England"
    assert it.pdf_url.endswith("/review/r240315a.pdf")


# ---- BIS sitemap-based discovery (v2) -------------------------------
BIS_SITEMAP_INDEX = """<?xml version="1.0" encoding="utf-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.bis.org/sitemap_documents_2024.xml</loc></sitemap>
  <sitemap><loc>https://www.bis.org/sitemap_documents_2025.xml</loc></sitemap>
  <sitemap><loc>https://www.bis.org/sitemap_other.xml</loc></sitemap>
</sitemapindex>
"""

BIS_YEAR_SITEMAP = """<?xml version="1.0" encoding="utf-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.bis.org/review/r240315a.pdf</loc></url>
  <url><loc>https://www.bis.org/review/r240315a.htm</loc></url>
  <url><loc>https://www.bis.org/review/r240316b.pdf</loc></url>
  <url><loc>https://www.bis.org/publ/work123.pdf</loc></url>
  <url><loc>https://www.bis.org/about/index.htm</loc></url>
</urlset>
"""

BIS_DETAIL = """<!doctype html><html><head>
  <meta property="og:title" content="Andrew Bailey: Inflation outlook"/>
  <meta property="og:description" content="Speech by Mr Andrew Bailey, Governor of the Bank of England, at a dinner, London, 15 March 2024."/>
</head><body></body></html>
"""


def test_parse_sitemap_index_filters_yearly_files():
    pairs = parse_sitemap_index(BIS_SITEMAP_INDEX)
    assert pairs == [(2024, "https://www.bis.org/sitemap_documents_2024.xml"),
                     (2025, "https://www.bis.org/sitemap_documents_2025.xml")]


def test_parse_year_sitemap_keeps_only_review_pdfs():
    metas = parse_year_sitemap(BIS_YEAR_SITEMAP)
    assert {m.pdf_url for m in metas} == {
        "https://www.bis.org/review/r240315a.pdf",
        "https://www.bis.org/review/r240316b.pdf",
    }
    first = next(m for m in metas if "r240315a" in m.pdf_url)
    assert first.date == date(2024, 3, 15)
    assert first.detail_url.endswith("r240315a.htm")


def test_parse_slug_date_century_boundary():
    assert _parse_slug_date("960115") == date(1996, 1, 15)
    assert _parse_slug_date("240315") == date(2024, 3, 15)
    assert _parse_slug_date("999999") is None  # invalid month


def test_parse_detail_extracts_title_and_description():
    title, desc = parse_detail(BIS_DETAIL)
    assert title.startswith("Andrew Bailey")
    assert "Bank of England" in desc


def test_guess_institution_picks_longest_match():
    inst = _guess_institution(
        "Speech by Andrew Bailey, Governor of the Bank of England, in London.")
    assert inst == "Bank of England"
    assert _guess_institution("No central bank mentioned here.") == ""


def test_guess_institution_ignores_host_institution_after_at():
    # The speaker is from Bundesbank; the host is National Bank of Romania.
    # The naive longest-match would wrongly pick the longer Romanian label.
    desc = ("Keynote speech by Prof Claudia Buch, Vice-President of the "
            "Deutsche Bundesbank, at the 14th Seminar on Financial Stability "
            "Issues, organised by the National Bank of Romania and the IMF.")
    assert _guess_institution(desc) == "Deutsche Bundesbank"


def test_guess_institution_recovers_banks_via_aliases():
    """RC1: BIS labels that differ from our primary registry name must still map
    (these whole banks/eras were silently dropped before the alias table)."""
    # BIS writes "Bank of Latvia"; registry primary is "Latvijas Banka".
    inst = _guess_institution("Speech by G. Razans, Governor of the Bank of Latvia, in Riga.")
    assert bank_for_bis_institution(inst).code == "lv"
    # Pre-2010 BIS label for the Fed.
    inst = _guess_institution(
        "Speech by Ben Bernanke, Chairman of the Board of Governors of the US "
        "Federal Reserve System, at a conference.")
    assert bank_for_bis_institution(inst).code == "us"
    # Renamed institutions (Türkiye endonym, North Macedonia).
    assert bank_for_bis_institution(_guess_institution(
        "Remarks by the Governor of the Central Bank of the Republic of Turkiye, in Ankara.")).code == "tr"


def test_guess_institution_is_accent_and_apostrophe_insensitive():
    # Diacritic in the speaker half must not break the match.
    assert bank_for_bis_institution(_guess_institution(
        "Speech by the Governor of the Central Bank of the Republic of Türkiye.")).code == "tr"
    # Curly apostrophe (U+2019) vs the straight one in the registry.
    assert bank_for_bis_institution(_guess_institution(
        "Speech by the Governor of the People’s Bank of China, in Beijing.")).code == "cn"


# ---- RePEc discovery -------------------------------------------------
REPEC_SERIES = """
<html><body>
<a href="/p/boe/boeewp/0001.html">Paper one</a>
<a href="/p/boe/boeewp/0002.html">Paper two</a>
<a href="/cgi-bin/htsearch">search</a>
</body></html>
"""

REPEC_PAPER = """
<html><body><h1>A study of monetary policy</h1>
<a href="https://www.bankofengland.co.uk/-/media/boe/files/wp/0001.pdf">Official PDF</a>
<a href="https://ideas.repec.org/cached/0001.pdf">RePEc cached copy</a>
</body></html>
"""


def test_repec_series_parse_filters_to_paper_pages():
    urls = parse_series_page(REPEC_SERIES)
    assert any("boeewp/0001.html" in u for u in urls)
    assert all("htsearch" not in u for u in urls)


def test_repec_keeps_only_official_domain_pdf():
    pdf = extract_official_pdf(REPEC_PAPER, "bankofengland.co.uk", allow_bis=False)
    assert pdf.startswith("https://www.bankofengland.co.uk")
    # If the bank domain doesn't match, the IDEAS-cached copy must be rejected.
    assert extract_official_pdf(REPEC_PAPER, "example.org", allow_bis=False) is None


def test_repec_extract_pdf_prefers_bank_then_falls_back():
    # Bank homepage matches -> prefer that.
    pdf = extract_pdf(REPEC_PAPER, "bankofengland.co.uk")
    assert pdf.startswith("https://www.bankofengland.co.uk")
    # No bank match and no BIS host -> fall back to the first available PDF.
    only_cached = """<html><body>
    <a href="https://ideas.repec.org/cached/0001.pdf">Cached</a>
    </body></html>"""
    pdf = extract_pdf(only_cached, "example.org")
    assert pdf.startswith("https://ideas.repec.org")
    # No PDF at all -> None.
    assert extract_pdf("<html></html>", "example.org") is None


# The real IDEAS markup: the PDF lives in an <input name="url">, never an
# <a href>. This is the page shape that previously yielded 0 working papers.
REPEC_PAPER_INPUT = """
<html><body><h1>Monetary policy and the term structure</h1>
<INPUT TYPE="radio" NAME="url"
 VALUE="https://www.ecb.europa.eu//pub/pdf/scpwps/ecbwp722.pdf" checked>
<B>File URL:</B> <span style="word-break:break-all">https://www.ecb.europa.eu//pub/pdf/scpwps/ecbwp722.pdf</span>
</body></html>
"""


def test_repec_extract_pdf_reads_ideas_input_form():
    # The canonical download form (no <a href>) must still yield the PDF.
    pdf = extract_pdf(REPEC_PAPER_INPUT, "ecb.europa.eu")
    assert pdf == "https://www.ecb.europa.eu//pub/pdf/scpwps/ecbwp722.pdf"
    # And it is preferred because it sits on the bank's own domain.
    assert extract_pdf(REPEC_PAPER_INPUT, "example.org").endswith("ecbwp722.pdf")


def test_repec_extract_pdf_regex_fallback_on_bare_url():
    # No <a> and no <input>, but a bare absolute .pdf URL in the markup.
    html = '<html><body>see https://www.snb.ch/n/some/working_paper_2020_01.pdf here</body></html>'
    assert extract_pdf(html, "snb.ch").endswith("working_paper_2020_01.pdf")


def test_repec_pagination_walks_pages_until_no_new():
    """Series listings cap at ~200/page; discovery must follow numbered pages
    and stop when a page yields nothing new (here: page 3 repeats page 2).
    The 404-after-the-last-page end is covered in test_repec_truncation.py."""
    from cb_corpus.sources.repec import RePEcDiscovery, IDEAS
    pages = {
        f"{IDEAS}/s/ecb/ecbwps.html":
            '<a href="/p/ecb/ecbwps/0001.html">a</a><a href="/p/ecb/ecbwps/0002.html">b</a>',
        f"{IDEAS}/s/ecb/ecbwps2.html":
            '<a href="/p/ecb/ecbwps/0003.html">c</a>',
        # page 3 repeats page 2 -> 0 new -> walk stops here
        f"{IDEAS}/s/ecb/ecbwps3.html":
            '<a href="/p/ecb/ecbwps/0003.html">c</a>',
    }

    class FakeFetcher:
        def get_text(self, url):
            return pages.get(url, "")  # unknown page -> empty -> 0 new -> stop

    d = RePEcDiscovery(FakeFetcher())
    got = [u.split("/")[-1] for u in d._series_paper_urls("ecb:ecbwps")]
    assert got == ["0001.html", "0002.html", "0003.html"]


def test_repec_paper_meta_extracts_title_and_date():
    """IDEAS citation_* meta tags give a real title + date; without them every
    working paper lands under year 0. The bare `date` meta (02-02 index date)
    is ignored, so year-only metadata falls back to Jan 1, not 2 Feb."""
    from cb_corpus.sources.repec import _paper_meta
    html = ('<html><head>'
            '<meta name="citation_title" content="Shocks and frictions">'
            '<meta name="date" content="2007-02-02">'
            '<meta name="citation_year" content="2007">'
            '</head><body><h1>fallback h1</h1></body></html>')
    title, d = _paper_meta(html)
    assert title == "Shocks and frictions"
    assert (d.year, d.month, d.day) == (2007, 1, 1)        # date meta ignored
    # Year-only fallback + <h1> title fallback.
    t2, d2 = _paper_meta('<meta name="citation_year" content="2015"><h1>Only H1</h1>')
    assert t2 == "Only H1" and d2 == date(2015, 1, 1)
    # No date metadata -> None (caller stores undated rather than crashing).
    assert _paper_meta("<html><h1>x</h1></html>")[1] is None


def test_repec_extract_pdf_candidates_ordered_by_preference():
    """Candidates ordered bank-domain → bis.org → rest, so Storage can fall back
    when the preferred host blocks us (403)."""
    from cb_corpus.sources.repec import extract_pdf_candidates
    html = """<html><body>
    <a href="https://ideas.repec.org/cached/x.pdf">cached</a>
    <a href="https://www.bis.org/review/r1.pdf">bis</a>
    <a href="https://www.bankofengland.co.uk/wp/0001.pdf">bank</a>
    <a href="https://econstor.eu/y.pdf">econstor</a>
    </body></html>"""
    c = extract_pdf_candidates(html, "bankofengland.co.uk")
    assert c[0].startswith("https://www.bankofengland.co.uk")   # bank first
    assert c[1].startswith("https://www.bis.org")               # bis second
    assert set(c[2:]) == {"https://ideas.repec.org/cached/x.pdf",
                          "https://econstor.eu/y.pdf"}           # rest


def test_storage_tries_alt_urls_on_download_failure(tmp_path):
    """When the preferred PDF 403s, Storage downloads a fallback copy — and keeps
    doc_id bound to the preferred URL (idempotence)."""
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    calls = []

    def fake_get_bytes(url):
        calls.append(url)
        if "preferred" in url:
            raise RuntimeError("403 Forbidden")
        return (b"%PDF-1.4 fallback", "application/pdf")

    st.fetcher.get_bytes = fake_get_bytes
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="x",
                    pdf_url="https://blocked.fr/preferred.pdf",
                    alt_urls=["https://econstor.eu/fallback.pdf"],
                    date=date(2024, 1, 1))
    assert st.save(rec) == "saved"
    assert calls == ["https://blocked.fr/preferred.pdf",
                     "https://econstor.eu/fallback.pdf"]        # tried in order
    assert Path(rec.local_path).read_bytes().startswith(b"%PDF")
    assert rec.pdf_url == "https://blocked.fr/preferred.pdf"    # citation/doc_id unchanged
    # alt_urls is persisted (WP v3) so dedup recognises fallback URLs across
    # restarts — but doc_id stays bound to the preferred pdf_url.
    assert rec.to_row()["alt_urls"] == ["https://econstor.eu/fallback.pdf"]


def test_wayback_cdx_parse_and_raw_url():
    """Wayback recovery: CDX rows -> (original, timestamp); raw_url uses `id_`."""
    from cb_corpus.sources.wayback import cdx_pdfs, raw_url

    class FakeF:
        def get_text(self, url):
            return json.dumps([["original", "timestamp"],
                               ["http://boe/1992/wp05.pdf", "20170812133136"],
                               ["http://boe/1993/wp01.pdf", "20160101000000"]])

    got = cdx_pdfs(FakeF(), "boe")
    assert got == [("http://boe/1992/wp05.pdf", "20170812133136"),
                   ("http://boe/1993/wp01.pdf", "20160101000000")]
    assert raw_url("http://boe/a.pdf", "2017") == "https://web.archive.org/web/2017id_/http://boe/a.pdf"


def test_wayback_for_url_latest_or_none():
    """Per-URL recovery (opaque paths): latest snapshot, or None if not archived."""
    from cb_corpus.sources.wayback import wayback_for_url

    class Has:
        def get_text(self, url):
            return json.dumps([["timestamp"], ["20170811213710"]])

    class Empty:
        def get_text(self, url):
            return json.dumps([["timestamp"]])

    assert wayback_for_url(Has(), "http://riksbank.com/upload/993/x.pdf") == \
        "https://web.archive.org/web/20170811213710id_/http://riksbank.com/upload/993/x.pdf"
    assert wayback_for_url(Empty(), "http://riksbank.com/upload/none.pdf") is None


def test_boe_wp_sitemap_parser_and_pdf_url():
    """BoE working papers from the bank's own sitemap -> live PDF urls."""
    from cb_corpus.sources.boe_wp import sitemap_papers

    class FakeF:
        def get_text(self, url):
            return ('<a href="/working-paper/2006/uk-monetary-regimes">x</a>'
                    '<a href="/working-paper/2007/a-state-space-approach">y</a>'
                    '<a href="/news/2020/not-a-paper">skip</a>')

    got = sitemap_papers(FakeF())
    assert len(got) == 2
    d, title, pdf = got[0]
    assert d.year == 2006
    assert pdf == ("https://www.bankofengland.co.uk/-/media/boe/files/"
                   "working-paper/2006/uk-monetary-regimes.pdf")
    assert len(sitemap_papers(FakeF(), years={2007})) == 1   # year filter


def test_boe_generic_doc_pages_and_page_doc():
    """Generic BoE recovery: sitemap filter + page->PDF (or HTML-only)."""
    from cb_corpus.sources.boe_wp import doc_pages, page_doc

    class FakeF:
        def __init__(self, pages):
            self.pages = pages

        def get_text(self, url):
            return self.pages[url]

    sm = ('<a href="/minutes/2010/monetary-policy-committee-january-2010">a</a>'
          '<a href="/minutes/2010/financial-policy-committee-x">fpc skip</a>'
          '<a href="/minutes/1998/monetary-policy-committee-may-1998">b</a>')
    f = FakeF({"https://www.bankofengland.co.uk/sitemap/minutes": sm})
    pages = doc_pages(f, "minutes", r"/minutes/\d{4}/monetary-policy-committee")
    assert len(pages) == 2 and {d.year for d, _ in pages} == {2010, 1998}  # MPC only

    pdf_page = ('<html><head><meta property="og:title" content="MPC minutes"></head>'
                '<body><a href="/-/media/boe/files/minutes/2010/jan.pdf">PDF</a></body></html>')
    f2 = FakeF({"https://www.bankofengland.co.uk/p": pdf_page})
    title, pdf = page_doc(f2, "https://www.bankofengland.co.uk/p")
    assert title == "MPC minutes" and pdf.endswith("/minutes/2010/jan.pdf")

    f3 = FakeF({"https://www.bankofengland.co.uk/h": "<html><title>T</title>no pdf</html>"})
    assert page_doc(f3, "https://www.bankofengland.co.uk/h")[1] is None   # HTML-only


def test_ecb_pub_section_include_and_date_formats():
    """ECB primary source = per-section/year static include; None signals fallback."""
    from cb_corpus.sources.ecb_pub import section_include_docs, date_from_url

    class FakeF:
        def get_text(self, url):
            if "/inter/date/2024/" in url:
                return ('<a href="/press/inter/date/2024/html/ecb.in240115~ab.en.html">x</a>'
                        '<a href="/press/inter/date/2024/html/sp240220_content.en.html">y</a>'
                        '<a href="/x.fr.html">non-en skip</a>')
            raise RuntimeError("404")

    f = FakeF()
    docs = section_include_docs(f, "inter", 2024, exts=(".en.html",))
    assert len(docs) == 2 and docs[0].startswith("https://www.ecb.europa.eu/press/inter/")
    assert section_include_docs(f, "fsr", 2024) is None       # not served -> triggers fallback
    # section-aware date parsing resolves the YYYYMM/YYMMDD ambiguity ("200612")
    assert date_from_url("x/ecb.fsr200612~h.en.pdf", "yyyymm").isoformat() == "2006-12-01"
    assert date_from_url("x/ecb.in200612~h.en.html", "yymmdd").isoformat() == "2020-06-12"


def test_repec_paper_meta_uses_publication_date_not_record_date():
    """Real pub date = citation_publication_date (YYYY/MM); the bare `date` meta
    (RePEc index date, uniformly YYYY-02-02) must be ignored."""
    from cb_corpus.sources.repec import _paper_meta
    html = ('<html><head>'
            '<meta name="date" content="2007-02-02">'
            '<meta name="citation_publication_date" content="2007/09">'
            '<meta name="citation_year" content="2007">'
            '<meta name="citation_title" content="A paper">'
            '</head></html>')
    title, d = _paper_meta(html)
    assert title == "A paper"
    assert d.isoformat() == "2007-09-01"          # Sept 2007, NOT 2 Feb
    # year-only fallback must not fall back to the 02-02 record date
    _, d2 = _paper_meta('<meta name="citation_year" content="2010">'
                        '<meta name="date" content="2010-02-02">')
    assert d2.isoformat() == "2010-01-01"


# ECB accounts use two URL conventions; the parser must accept BOTH, else the
# 2015-2017 backfill (legacy `mg...`) is silently dropped (only `ecb.mg...` kept).
ECB_ACCOUNTS_MIXED = """
<html><body>
<a href="/press/accounts/2015/html/mg150219.en.html">19 Feb 2015 (legacy form)</a>
<a href="/press/accounts/2017/html/ecb.mg171123.en.html">23 Nov 2017 (modern form)</a>
<a href="/press/accounts/2021/html/ecb.mg210729~b83737e3b5.en.html">29 Jul 2021 (modern + hash)</a>
</body></html>
"""


def test_ecb_account_parser_accepts_legacy_and_modern_urls():
    from cb_corpus.adapters.ecb import parse_account_items
    items = parse_account_items(ECB_ACCOUNTS_MIXED)
    got = {d.isoformat() for d, _ in items}
    # All three conventions must be discovered, not just the `ecb.` prefixed ones.
    assert got == {"2015-02-19", "2017-11-23", "2021-07-29"}


def test_ecb_decision_parser_keeps_decisions_only():
    """A1 = monetary-policy decisions (modern mp / legacy pr) — NOT accounts (mg,
    A3) nor statements (is, A2) that share the MOPO index."""
    from cb_corpus.adapters.ecb import parse_decision_items
    html = ('<html><body>'
            '<a href="/press/pr/date/2025/html/ecb.mp251218~58b0e415a6.en.html">modern</a>'
            '<a href="/press/pr/date/2015/html/pr151203.en.html">legacy</a>'
            '<a href="/press/accounts/2026/html/ecb.mg260122~x.en.html">account A3</a>'
            '<a href="/press/pr/date/2025/html/ecb.is251030~y.en.html">statement A2</a>'
            '</body></html>')
    got = {d.isoformat() for d, _ in parse_decision_items(html)}
    assert got == {"2025-12-18", "2015-12-03"}


def test_ecb_statement_parser_keeps_is_only():
    """A2 = monetary-policy statements (is prefix, EN only) — not decisions/accounts."""
    from cb_corpus.adapters.ecb import parse_statement_items
    P = "/press/press_conference/monetary-policy-statement/2025/html"
    html = ('<html><body>'
            f'<a href="{P}/ecb.is250911~a13675b834.en.html">EN stmt</a>'
            f'<a href="{P}/ecb.is250911~a13675b834.bg.html">BG (skip)</a>'
            '<a href="/press/pr/date/2025/html/ecb.mp251218~x.en.html">decision (skip)</a>'
            '</body></html>')
    got = [(d.isoformat(), u) for d, u in parse_statement_items(html)]
    assert len(got) == 1 and got[0][0] == "2025-09-11" and got[0][1].endswith(".en.html")


# ---- Fed / ECB native parsers ---------------------------------------
def test_fed_minutes_parser():
    html = '<a href="/monetarypolicy/files/fomcminutes20240131.pdf">Minutes</a>'
    got = parse_minutes_links(html)
    assert got and got[0][0] == date(2024, 1, 31)
    assert got[0][1].endswith("fomcminutes20240131.pdf")


def test_fed_statement_parser_modern_and_legacy_paths():
    """A2 FOMC statements: modern + legacy paths, excluding a1 (impl note) / b."""
    from cb_corpus.adapters.fed import parse_statement_links
    html = ('<a href="/newsevents/pressreleases/monetary20150128a.htm">modern</a>'
            '<a href="/newsevents/press/monetary/20080130a.htm">legacy path</a>'
            '<a href="/newsevents/pressreleases/monetary20150128a1.htm">impl note skip</a>'
            '<a href="/newsevents/pressreleases/monetary20150128b.htm">discount skip</a>')
    got = sorted(d.isoformat() for d, _ in parse_statement_links(html))
    assert got == ["2008-01-30", "2015-01-28"]


def test_fed_statement_parser_includes_historical_boarddocs():
    """A2: pre-2006 statements live at /boarddocs/press/monetary/<yr>/<date>/ -> default.htm."""
    from cb_corpus.adapters.fed import parse_statement_links
    html = ('<a href="/newsevents/pressreleases/monetary20150128a.htm">modern</a>'
            '<a href="/boarddocs/press/monetary/2003/20030129/default.htm">historical</a>'
            '<a href="/boarddocs/press/monetary/2003/20030129/">same, dir (dedup)</a>')
    pairs = parse_statement_links(html)
    assert sorted(set(d.isoformat() for d, _ in pairs)) == ["2003-01-29", "2015-01-28"]
    # historical entry resolves to the default.htm page
    assert any(u.endswith("/boarddocs/press/monetary/2003/20030129/default.htm")
               for _, u in pairs)


def test_fed_sep_parser_modern_and_historical():
    """F1: modern `fomcprojtabl<date>.pdf` (2021+) + historical `FOMC<date>SEPcompilation.pdf`."""
    from cb_corpus.adapters.fed import parse_sep_links
    html = ('<a href="/monetarypolicy/files/fomcprojtabl20210317.pdf">x</a>'
            '<a href="/monetarypolicy/files/FOMC20150318SEPcompilation.pdf">y</a>'
            '<a href="/monetarypolicy/files/FOMC20150318SEPkey.pdf">z</a>'        # key -> skip
            '<a href="/monetarypolicy/files/FOMC20030624gbpt20030618.pdf">g</a>')  # greenbook -> skip
    got = sorted(d.isoformat() for d, _ in parse_sep_links(html))
    assert got == ["2015-03-18", "2021-03-17"]


def test_fed_minutes_parser_modern_and_historical():
    """A3: modern `fomcminutes<date>.pdf` (PDF) + historical `/fomc/minutes/<date>.htm`."""
    from cb_corpus.adapters.fed import parse_minutes_links
    html = ('<a href="/monetarypolicy/files/fomcminutes20150128.pdf">modern</a>'
            '<a href="/fomc/minutes/20020130.htm">historical html</a>')
    got = sorted(d.isoformat() for d, _ in parse_minutes_links(html))
    assert got == ["2002-01-30", "2015-01-28"]


def test_rba_decision_parser_reads_date_from_link_text():
    """RBA A1: decision date is in the link TEXT ('9 December 2025'), not the URL."""
    from cb_corpus.adapters.rba import parse_rba_decisions
    html = ('<a href="/media-releases/2025/mr-25-33.html">9 December 2025</a>'
            '<a href="/media-releases/2025/mr-25-03.html">18 February 2025</a>'
            '<a href="/media-releases/2025/mr-25-99.html">Quarterly bulletin</a>'  # bad date -> skip
            '<a href="/speeches/2025/sp-gov-2025.html">9 December 2025</a>')        # not a decision -> skip
    got = sorted(d.isoformat() for d, _ in parse_rba_decisions(html))
    assert got == ["2025-02-18", "2025-12-09"]


def test_rba_adapter_overrides_au():
    from cb_corpus.adapters.rba import RBAAdapter
    from cb_corpus.taxonomy import DocType
    au = get_adapter("au")
    assert isinstance(au, RBAAdapter)
    # A3 (minutes) + E1 (SMP) are covered natively now — not via the (removed,
    # frozen-2017) TOML sitemap.
    assert DocType.A3 in au.native_types and DocType.E1 in au.native_types


def test_rba_minutes_parser_handles_both_url_date_formats():
    """A3 date is in the URL: modern YYYY-MM-DD and legacy DDMMYYYY."""
    from cb_corpus.adapters.rba import parse_rba_minutes
    html = ('<a href="/monetary-policy/rba-board-minutes/2025/2025-11-04.html">x</a>'
            '<a href="/monetary-policy/rba-board-minutes/2010/05102010.html">y</a>'   # 5 Oct 2010
            '<a href="/monetary-policy/rba-board-minutes/2010/index.html">skip</a>')
    got = sorted(d.isoformat() for d, _ in parse_rba_minutes(html))
    assert got == ["2010-10-05", "2025-11-04"]


def test_rba_smp_parser_keeps_only_quarterly_issues():
    """E1: only the issue overview /smp/<year>/<feb|may|aug|nov>/ — not sub-pages."""
    from cb_corpus.adapters.rba import parse_rba_smp
    html = ('<a href="/publications/smp/2026/feb/">x</a>'
            '<a href="/publications/smp/2025/nov/">y</a>'
            '<a href="/publications/smp/2026/boxes.html">skip sub-page</a>'
            '<a href="/publications/smp/2026/feb/outlook.html">skip chapter</a>')
    got = sorted((d.isoformat()) for d, _ in parse_rba_smp(html))
    assert got == ["2025-11-01", "2026-02-01"]


def test_ecb_index_parser():
    html = '<a href="/press/accounts/2024/html/ecb.mg240404.en.pdf">Account</a>'
    rows = parse_index(html, href_must_contain="accounts")
    assert rows and rows[0][2].endswith(".pdf")


# ---- declarative adapters from TOML ---------------------------------
def test_toml_factories_loaded_for_majors():
    # banks_sources.toml ships with at least these.
    for code in ("ch", "ca"):
        assert code in INSTANCE_FACTORIES
    # Hand-written ADAPTERS override TOML for us/ecb/au/jp/fr.
    from cb_corpus.adapters.fed import FedAdapter
    from cb_corpus.adapters.rba import RBAAdapter
    from cb_corpus.adapters.boj import BoJAdapter
    from cb_corpus.adapters.bdf import BdfAdapter
    assert isinstance(get_adapter("us"), FedAdapter)
    # `au` moved from a (frozen-2017) TOML sitemap to the RBAAdapter class.
    assert isinstance(get_adapter("au"), RBAAdapter)
    assert "au" not in INSTANCE_FACTORIES
    # `jp` moved from a TOML listing to the BoJAdapter class (A3 + native D1 WPs).
    assert isinstance(get_adapter("jp"), BoJAdapter)
    assert "jp" not in INSTANCE_FACTORIES
    # `fr` moved from a TOML sitemap (E2 only) to the BdfAdapter class, which
    # reproduces the same E2 sitemap config inline AND adds native D1 WPs
    # (merged legacy + new-system walk).
    fr = get_adapter("fr")
    assert isinstance(fr, BdfAdapter)
    assert "fr" not in INSTANCE_FACTORIES
    assert DocType.D1 in fr.native_types and DocType.E2 in fr.native_types


def test_sitemap_parser_handles_urlset_and_index():
    urlset = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://x.test/a.pdf</loc><lastmod>2024-03-15</lastmod></url>
      <url><loc>https://x.test/b.html</loc></url>
    </urlset>"""
    urls, children = parse_sitemap(urlset)
    assert len(urls) == 2 and not children
    assert urls[0][1] == date(2024, 3, 15)

    index = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://x.test/sm1.xml</loc></sitemap>
    </sitemapindex>"""
    urls, children = parse_sitemap(index)
    assert children == ["https://x.test/sm1.xml"] and not urls


def test_date_from_url_picks_first_iso_like_date():
    assert _date_from_url("https://x/2024/03/15/foo.pdf") == date(2024, 3, 15)
    assert _date_from_url("https://x/foo-20240315.pdf") == date(2024, 3, 15)
    assert _date_from_url("https://x/no-date.pdf") is None


def test_listing_parse_links_extracts_pdfs_with_dates():
    html = """<html><body>
    <a href="/minutes/2024/g240115.pdf">January</a>
    <a href="/minutes/2024/g240228.pdf">February</a>
    <a href="/other.html">skip</a>
    </body></html>"""
    import re as _re
    rows = parse_links(html, "https://x.test/jp/",
                       _re.compile(r"g\d{6}\.pdf$"))
    assert len(rows) == 2
    assert rows[0][2].startswith("https://x.test/")


# ---- ECB v2 parsers --------------------------------------------------
def test_ecb_year_includes():
    html = """<html><body>
    <dl id="lazyload-container" data-snippets="../2026/html/index_include.en.html,../2025/html/index_include.en.html"></dl>
    </body></html>"""
    inc = parse_year_includes(html)
    assert inc == ["../2026/html/index_include.en.html",
                   "../2025/html/index_include.en.html"]


def test_ecb_account_items():
    html = """<html><body>
    <a href="/press/accounts/2026/html/ecb.mg260528~a93230dc4b.en.html">Acc 1</a>
    <a href="/press/accounts/2026/html/ecb.mg260416~6a27b0c258.en.html">Acc 2</a>
    <a href="/press/pr/date/2026/html/ecb.mp260430~xx.en.html">unrelated</a>
    </body></html>"""
    items = parse_account_items(html)
    assert len(items) == 2
    assert items[0][0] == date(2026, 5, 28)
    assert items[1][0] == date(2026, 4, 16)


def test_ecb_bulletin_pdfs_keep_english_only():
    html = """<html><body>
    <a href="/pub/pdf/ecbu/eb202603.en.pdf">EN</a>
    <a href="/pub/pdf/ecbu/eb202603.fr.pdf">FR (skipped)</a>
    <a href="/pub/pdf/ecbu/eb202602.en.pdf">EN</a>
    </body></html>"""
    rows = parse_bulletin_pdfs(html)
    assert len(rows) == 2
    assert all(u.endswith(".en.pdf") for _, _, u in rows)


# ---- ECB D3 blog discovery (master listing) ---------------------------
_ECB_BLOG_FIXTURE = (Path(__file__).parent / "fixtures" / "ecb_blog_index.html").read_text()


def test_ecb_blog_parser_dedups_by_url_excludes_non_english_and_falls_back_to_label_date():
    """Parser-level: content-box/arrow duplicates collapse to one row per URL,
    non-English siblings (.es.html) are excluded, and a URL with no parseable
    date (the real 'ecb.blog0227~...' filename) still gets a day-precision
    date from its <h5> label. The synthetic no-date-anywhere row is returned
    with date=None (the caller's job to skip + warn)."""
    rows = parse_blog_items(_ECB_BLOG_FIXTURE, BLOG_INDEX)
    urls = [u for _, _, u in rows]
    assert len(urls) == len(set(urls)) == 4          # deduped, incl. the malformed one
    assert not any(u.endswith(".es.html") for u in urls)
    by_url = {u: (d, t) for d, t, u in rows}
    cccee = next(u for u in urls if "cccee738e1" in u)
    assert by_url[cccee][0] == date(2024, 11, 19)
    assert "resilient" in by_url[cccee][1].lower()
    legacy = next(u for u in urls if "241114" in u)   # legacy 6-digit URL date
    assert by_url[legacy][0] == date(2024, 11, 14)
    labeled = next(u for u in urls if "blog0227" in u)   # URL has no full date
    assert by_url[labeled][0] == date(2026, 2, 27)       # recovered from <h5> label
    malformed = next(u for u in urls if "blog-untitled" in u)
    assert by_url[malformed][0] is None


def test_ecb_blog_discovery_yields_docrecords_day_precision_and_skips_malformed(capsys):
    """Adapter-level: D3 discovery turns the listing into DocRecords (day
    precision, bank_site provenance), and the anchor with no date anywhere is
    skipped with a warning rather than crashing or yielding a bad row."""
    class FakeFetcher:
        def get_text(self, url):
            assert url == BLOG_INDEX
            return _ECB_BLOG_FIXTURE

    a = ECBAdapter(get_bank("ecb"), FakeFetcher())
    recs = list(a.discover(DocType.D3))
    assert len(recs) == 3
    assert all(r.doc_type == DocType.D3 for r in recs)
    assert all(r.bank_code == "ecb" for r in recs)
    assert all(r.provenance == "bank_site" for r in recs)
    assert all(r.date_precision == "day" for r in recs)
    dates = sorted(r.date for r in recs)
    assert dates == [date(2024, 11, 14), date(2024, 11, 19), date(2026, 2, 27)]
    assert not any("blog-untitled" in r.pdf_url for r in recs)
    err = capsys.readouterr().err
    assert "blog-untitled" in err and "D3" in err


def test_ecb_d3_native_and_reached_by_discover_all():
    """Registry wiring: D3 must be declared native AND actually reachable
    through the generic discover_all() path (supported_types() gate)."""
    assert DocType.D3 in ECBAdapter.native_types

    class FakeFetcher:
        def get_text(self, url):
            return _ECB_BLOG_FIXTURE

    a = ECBAdapter(get_bank("ecb"), FakeFetcher())
    assert DocType.D3 in a.supported_types()
    recs = list(a.discover_all(scope=(DocType.D3,)))
    assert len(recs) == 3
    assert all(r.doc_type == DocType.D3 for r in recs)


def test_ecb_d3_double_slash_legacy_row_deduped_via_alt_url(tmp_path):
    """15 of the 212 historical D3 rows were written by the old one-off
    scraper with a double-slash pdf_url (`europa.eu//press/...`); the new
    `_discover_blog` always normalizes to a single slash. Without indexing the
    normalized form, is_known_url() would miss on every nightly run and
    silently re-fetch those 15 posts. The data-fix registers the normalized
    URL in alt_urls (same convention as the WP v3 migration) — this locks
    that is_known_url() then recognises the normalized form."""
    legacy_url = ("https://www.ecb.europa.eu//press/blog/date/2023/html/"
                  "ecb.blog230824~362178a805.en.html")
    normalized_url = ("https://www.ecb.europa.eu/press/blog/date/2023/html/"
                       "ecb.blog230824~362178a805.en.html")
    row = {
        "bank_code": "ecb", "doc_type": "D3", "title": "ECB D3 2023-08-24",
        "pdf_url": legacy_url, "source_url": "", "date": "2023-08-24",
        "language": "en", "provenance": "bank_site", "mime_type": "text/html",
        "sha256": "f8ee5df6f1402ceee7af11b4e8e9706638c8b213bfe844b1e265c6b4160e7b86",
        "local_path": "data/raw/ecb/D3/2023/f0d212497f8b1289.html",
        "doc_id": "f0d212497f8b1289", "year": 2023,
        "alt_urls": [normalized_url],
    }
    cfg = Config(data_dir=tmp_path)
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    cfg.manifest_file("ecb").write_text(json.dumps(row, ensure_ascii=False) + "\n")
    st = Storage(cfg)
    assert st.is_known_url(normalized_url)   # what _discover_blog would yield
    assert st.is_known_url(legacy_url)       # the legacy form itself still matches too


def test_ecb_d3_known_legacy_row_filtered_before_any_fetch_through_discover_flow(tmp_path):
    """Flow-level lock for the silent-series bug: indexing the normalized alt_url
    (previous test) is only useful if something on the D3 discovery path actually
    consults it. Wire `_skip_known_url` exactly as `pipeline.run()` does
    (`adapter._skip_known_url = storage.is_known_url`) and drive the SAME already-known
    legacy post through `discover(D3)` -> `save_many()`. The record must be filtered
    by `discover()` itself — it must never reach `Storage.save()`'s per-item fetch."""
    legacy_url = ("https://www.ecb.europa.eu//press/blog/date/2023/html/"
                  "ecb.blog230824~362178a805.en.html")
    normalized_url = ("https://www.ecb.europa.eu/press/blog/date/2023/html/"
                       "ecb.blog230824~362178a805.en.html")
    row = {
        "bank_code": "ecb", "doc_type": "D3", "title": "ECB D3 2023-08-24",
        "pdf_url": legacy_url, "source_url": "", "date": "2023-08-24",
        "language": "en", "provenance": "bank_site", "mime_type": "text/html",
        "sha256": "f8ee5df6f1402ceee7af11b4e8e9706638c8b213bfe844b1e265c6b4160e7b86",
        "local_path": "data/raw/ecb/D3/2023/f0d212497f8b1289.html",
        "doc_id": "f0d212497f8b1289", "year": 2023,
        "alt_urls": [normalized_url],
    }
    cfg = Config(data_dir=tmp_path)
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    cfg.manifest_file("ecb").write_text(json.dumps(row, ensure_ascii=False) + "\n")

    # The live listing re-lists this same post (normalized single-slash URL, as
    # `_discover_blog` always yields) alongside one genuinely new post — a real
    # nightly re-crawl would see the already-known post on every pass since blog
    # listings don't expire old entries.
    blog_html = """<html><body><div class="box">
      <a class="content-box" href="/press/blog/date/2023/html/ecb.blog230824~362178a805.en.html">
        <h5>24 August 2023</h5><h3>Already-known legacy post</h3>
      </a>
    </div><div class="box">
      <a class="content-box" href="/press/blog/date/2026/html/ecb.blog20260301~abc123def0.en.html">
        <h5>1 March 2026</h5><h3>Genuinely new post</h3>
      </a>
    </div></body></html>"""

    class SpyFetcher:
        """Records every URL asked for; get_bytes fails loudly on the known
        legacy post so a regression (re-fetching it) fails the test instead of
        silently succeeding."""
        def __init__(self):
            self.get_bytes_calls: list[str] = []

        def get_text(self, url):
            assert url == BLOG_INDEX
            return blog_html

        def get_bytes(self, url):
            self.get_bytes_calls.append(url)
            if url in (legacy_url, normalized_url):
                raise AssertionError(f"fetched already-known D3 URL: {url}")
            return (b"<html>new post</html>", "text/html")

    fetcher = SpyFetcher()
    storage = Storage(cfg, fetcher)
    adapter = ECBAdapter(get_bank("ecb"), fetcher)
    adapter._skip_known_url = storage.is_known_url   # what pipeline.run() sets

    recs = list(adapter.discover(DocType.D3))
    yielded_urls = [r.pdf_url for r in recs]
    assert normalized_url not in yielded_urls   # filtered by discover(), not by save()
    assert any("20260301" in u for u in yielded_urls)   # the new post still comes through

    counts = storage.save_many(recs)
    assert normalized_url not in fetcher.get_bytes_calls   # never asked for
    assert counts.get("saved", 0) == 1                     # only the new post was saved


def test_discovery_slash_variant_of_indexed_row_skipped_before_fetch(tmp_path):
    """Spec 'Consequence' (2026-08-25-url-normalization-index-design): a
    discovery source that re-lists a KNOWN document under a slash-variant URL
    is skipped before download by `_skip_known_url` -- no re-fetch, no
    duplicate row, no alt_urls data amendment needed. Mirrors the test above,
    but reversed: the manifest row is indexed under the CLEAN url and it's the
    discovery source that carries the double-slash variant this time. Passes
    already via Task 2's index-time normalize_url() -- pins the end-to-end
    behavior regardless."""
    clean_url = ("https://www.ecb.europa.eu/press/blog/date/2023/html/"
                 "ecb.blog230824~362178a805.en.html")
    row = {
        "bank_code": "ecb", "doc_type": "D3", "title": "ECB D3 2023-08-24",
        "pdf_url": clean_url, "source_url": "", "date": "2023-08-24",
        "language": "en", "provenance": "bank_site", "mime_type": "text/html",
        "sha256": "f8ee5df6f1402ceee7af11b4e8e9706638c8b213bfe844b1e265c6b4160e7b86",
        "local_path": "data/raw/ecb/D3/2023/f0d212497f8b1289.html",
        "doc_id": "f0d212497f8b1289", "year": 2023,
    }
    cfg = Config(data_dir=tmp_path)
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    cfg.manifest_file("ecb").write_text(json.dumps(row, ensure_ascii=False) + "\n")

    # The live listing re-lists this same post under a double-slash variant of
    # its URL (a discovery-source hiccup), alongside one genuinely new post.
    variant_url = ("https://www.ecb.europa.eu/press//blog/date/2023/html/"
                   "ecb.blog230824~362178a805.en.html")
    blog_html = """<html><body><div class="box">
      <a class="content-box" href="/press//blog/date/2023/html/ecb.blog230824~362178a805.en.html">
        <h5>24 August 2023</h5><h3>Already-known post (slash variant)</h3>
      </a>
    </div><div class="box">
      <a class="content-box" href="/press/blog/date/2026/html/ecb.blog20260301~abc123def0.en.html">
        <h5>1 March 2026</h5><h3>Genuinely new post</h3>
      </a>
    </div></body></html>"""

    class SpyFetcher:
        """Records every URL asked for; get_bytes fails loudly on the known
        post so a regression (re-fetching it) fails the test instead of
        silently succeeding."""
        def __init__(self):
            self.get_bytes_calls: list[str] = []

        def get_text(self, url):
            assert url == BLOG_INDEX
            return blog_html

        def get_bytes(self, url):
            self.get_bytes_calls.append(url)
            if url in (clean_url, variant_url):
                raise AssertionError(f"fetched already-known D3 URL: {url}")
            return (b"<html>new post</html>", "text/html")

    fetcher = SpyFetcher()
    storage = Storage(cfg, fetcher)
    adapter = ECBAdapter(get_bank("ecb"), fetcher)
    adapter._skip_known_url = storage.is_known_url   # what pipeline.run() sets

    recs = list(adapter.discover(DocType.D3))
    yielded_urls = [r.pdf_url for r in recs]
    assert variant_url not in yielded_urls   # filtered by discover(), not by save()
    assert any("20260301" in u for u in yielded_urls)   # the new post still comes through

    counts = storage.save_many(recs)
    assert variant_url not in fetcher.get_bytes_calls   # never asked for
    assert counts.get("saved", 0) == 1                  # only the new post was saved


# ---- ECB C2 interviews (live foedb source, see sources/ecb_foedb.py) ----
def test_ecb_c2_native_and_reached_by_discover_all(monkeypatch):
    """Registry wiring: C2 must be declared native AND actually reachable
    through the generic discover_all() path (supported_types() gate), routed
    to the live foedb source (see sources/ecb_foedb.discover_ecb_interviews;
    stubbed here the same way the D1/D2 wiring test stubs discover_ecb_wp)."""
    assert DocType.C2 in ECBAdapter.native_types

    recs = [
        DocRecord(bank_code="ecb", doc_type=DocType.C2, title="Interview A",
                  pdf_url="https://www.ecb.europa.eu/press/inter/date/2024/html/"
                          "ecb.in240103~aa.en.html",
                  date=date(2024, 1, 3), provenance="bank_site", mime_type="text/html"),
        DocRecord(bank_code="ecb", doc_type=DocType.C2, title="Interview B",
                  pdf_url="https://www.ecb.europa.eu/press/inter/date/2024/html/"
                          "ecb.in240319~bb.en.html",
                  date=date(2024, 3, 19), provenance="bank_site", mime_type="text/html"),
    ]
    monkeypatch.setattr(ecb_foedb, "discover_ecb_interviews",
                        lambda fetcher, since=None: iter(recs))

    a = ECBAdapter(get_bank("ecb"), fetcher=object())   # fetcher unused (patched)
    assert DocType.C2 in a.supported_types()
    got = list(a.discover_all(scope=(DocType.C2,), since=date(2024, 1, 1)))
    assert len(got) == 2
    assert all(r.doc_type == DocType.C2 for r in got)
    assert all(r.bank_code == "ecb" for r in got)
    assert all(r.provenance == "bank_site" for r in got)
    assert all(r.date_precision == "day" for r in got)


def test_ecb_c2_double_slash_legacy_row_deduped_via_alt_url(tmp_path):
    """54 of the 637 pre-existing C2 rows carry a double-slash pdf_url
    (`europa.eu//press/...`), the same one-off scraper artifact as 15 of
    D3's 212 rows. The live foedb source (sources/ecb_foedb.discover_ecb_interviews)
    always yields the normalized single-slash form -- the data-fix registers
    the normalized URL in alt_urls (same convention as the D3 fix) so
    is_known_url() recognises it and nightly discovery skips these before
    any fetch."""
    legacy_url = ("https://www.ecb.europa.eu//press/inter/date/2019/html/"
                  "ecb.in191216~8014b1bae6.en.html")
    normalized_url = ("https://www.ecb.europa.eu/press/inter/date/2019/html/"
                       "ecb.in191216~8014b1bae6.en.html")
    row = {
        "bank_code": "ecb", "doc_type": "C2", "title": "Interview with NOS Nieuwsuur",
        "pdf_url": legacy_url, "source_url": "", "date": "2019-12-16",
        "language": "en", "provenance": "bank_site", "mime_type": "text/html",
        "sha256": "9d67b7c417f25253b3b81fe698bea2b0263f0cef430f2f2bdb239192eb2e7fb6",
        "local_path": "data/raw/ecb/C2/2019/18f03b281e9ab1ba.html",
        "doc_id": "18f03b281e9ab1ba", "year": 2019,
        "alt_urls": [normalized_url],
    }
    cfg = Config(data_dir=tmp_path)
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    cfg.manifest_file("ecb").write_text(json.dumps(row, ensure_ascii=False) + "\n")
    st = Storage(cfg)
    assert st.is_known_url(normalized_url)   # what discover_ecb_interviews would yield
    assert st.is_known_url(legacy_url)       # the legacy form itself still matches too


def test_ecb_d1_double_slash_legacy_row_deduped_via_alt_url(tmp_path):
    """17 live D1/D2 rows (repec_discovery, 2026-07/08 vintage) carry the same
    double-slash pdf_url artifact (`europa.eu//pub/pdf/...`) as the D3/C2
    legacy rows above -- repec discovery always yields the normalized
    single-slash form, so without indexing it these rows would silently
    re-download on every nightly run. Same amendment, same convention:
    normalized URL appended to alt_urls."""
    legacy_url = ("https://www.ecb.europa.eu//pub/pdf/scpwps/"
                  "ecb.wp3252~91761af52b.en.pdf")
    normalized_url = ("https://www.ecb.europa.eu/pub/pdf/scpwps/"
                       "ecb.wp3252~91761af52b.en.pdf")
    row = {
        "bank_code": "ecb", "doc_type": "D1",
        "title": "A theory of bank liquidity requirements",
        "pdf_url": legacy_url,
        "source_url": "https://ideas.repec.org/p/ecb/ecbwps/20263252.html",
        "date": "2026-07-01", "language": "en", "provenance": "repec_discovery",
        "mime_type": "application/pdf",
        "sha256": "f0aa3f6773d68af8026f8c6beb33ca67df3698d46c5d5667410c20f82f579248",
        "local_path": "data/raw/ecb/D1/2026/c58765e17500b626.pdf",
        "doc_id": "c58765e17500b626", "year": 2026,
        "alt_urls": [normalized_url],
    }
    cfg = Config(data_dir=tmp_path)
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    cfg.manifest_file("ecb").write_text(json.dumps(row, ensure_ascii=False) + "\n")
    st = Storage(cfg)
    assert st.is_known_url(normalized_url)   # what repec discovery would yield
    assert st.is_known_url(legacy_url)       # the legacy form itself still matches too


# ---- storage (no domain guard in v2 — discovery layer owns URL quality) ----
def test_storage_indexes_any_url_in_dry_run(tmp_path):
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    rec = DocRecord(bank_code="gb", doc_type=DocType.C1, title="x",
                    pdf_url="https://www.bankofengland.co.uk/a.pdf")
    assert st.save(rec, dry_run=True) == "dry-run:indexed"
    # second time should be deduped on doc_id
    assert st.save(rec, dry_run=True) == "skip:already-indexed"


def test_storage_tracks_known_urls_for_rerun_dedup(tmp_path):
    """Re-runs must short-circuit before the BIS detail-page fetch — populated
    by a REAL save (dry-run must not persist; see the next test)."""
    cfg = Config(data_dir=tmp_path)
    st1 = Storage(cfg)
    st1.fetcher.get_bytes = lambda url: (b"%PDF-1.4 data", "application/pdf")
    rec = DocRecord(bank_code="de", doc_type=DocType.C1, title="x",
                    pdf_url="https://www.bis.org/review/r240315a.pdf",
                    date=date(2024, 3, 15))
    assert st1.save(rec) == "saved"
    # Reopen — _urls is re-populated from the manifest.
    st2 = Storage(cfg)
    assert st2.is_known_url("https://www.bis.org/review/r240315a.pdf")
    assert not st2.is_known_url("https://www.bis.org/review/r240316b.pdf")


def test_dry_run_does_not_persist_and_never_blocks_real_save(tmp_path):
    """R1 regression: a dry-run must NOT write a placeholder manifest row, or a
    later real run would skip it as 'already-indexed' and never download it."""
    cfg = Config(data_dir=tmp_path)
    rec = DocRecord(bank_code="us", doc_type=DocType.A2, title="x",
                    pdf_url="https://x/a.pdf", date=date(2020, 1, 1),
                    mime_type="application/pdf")
    from cb_corpus.storage import iter_manifest_rows
    st = Storage(cfg)
    assert st.save(rec, dry_run=True) == "dry-run:indexed"
    # Manifest stays empty — nothing persisted.
    assert list(iter_manifest_rows(cfg)) == []
    # A fresh Storage (new process) still downloads it.
    st2 = Storage(cfg)
    st2.fetcher.get_bytes = lambda url: (b"%PDF-1.4 data", "application/pdf")
    assert st2.save(rec) == "saved"
    assert len(list(iter_manifest_rows(cfg))) == 1  # exactly 1 row


def test_duplicate_content_status_carries_matched_doc_id(tmp_path):
    """A same-bytes save under a DIFFERENT doc_id must report which existing
    doc_id the content matches (`skip:duplicate-content:<doc_id>`) -- the
    recover.py duplicate-stamping design needs this to know which manifest
    row to stamp the dead URL onto."""
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    st.fetcher.get_bytes = lambda url: (b"%PDF-1.4 same body", "application/pdf")
    rec_a = DocRecord(bank_code="fr", doc_type=DocType.D1, title="A",
                      pdf_url="https://www.banque-france.fr/a.pdf",
                      date=date(2020, 1, 1))
    assert st.save(rec_a) == "saved"
    rec_b = DocRecord(bank_code="fr", doc_type=DocType.D1, title="B",
                      pdf_url="https://www.banque-france.fr/b.pdf",
                      date=date(2020, 1, 1))
    assert st.save(rec_b) == f"skip:duplicate-content:{rec_a.doc_id}"

    # Same for reindex() (no-fetch path).
    st2 = Storage(cfg)
    local_pdf = tmp_path / "reindex-src.pdf"
    local_pdf.write_bytes(b"%PDF-1.4 same body")
    rec_c = DocRecord(bank_code="fr", doc_type=DocType.D1, title="C",
                      pdf_url="https://www.banque-france.fr/c.pdf",
                      date=date(2020, 1, 1))
    assert st2.reindex(rec_c, local_pdf) == f"skip:duplicate-content:{rec_a.doc_id}"


def test_duplicate_content_map_survives_reload(tmp_path):
    """The sha256 -> doc_id map must be rebuilt on `_load_existing` (a fresh
    Storage over the same data dir), not just maintained in-memory at the
    write sites."""
    cfg = Config(data_dir=tmp_path)
    st1 = Storage(cfg)
    st1.fetcher.get_bytes = lambda url: (b"%PDF-1.4 reload body", "application/pdf")
    rec_a = DocRecord(bank_code="fr", doc_type=DocType.D1, title="A",
                      pdf_url="https://www.banque-france.fr/reload-a.pdf",
                      date=date(2020, 1, 1))
    assert st1.save(rec_a) == "saved"

    st2 = Storage(cfg)   # fresh instance, re-reads the manifest
    st2.fetcher.get_bytes = lambda url: (b"%PDF-1.4 reload body", "application/pdf")
    rec_b = DocRecord(bank_code="fr", doc_type=DocType.D1, title="B",
                      pdf_url="https://www.banque-france.fr/reload-b.pdf",
                      date=date(2020, 1, 1))
    assert st2.save(rec_b) == f"skip:duplicate-content:{rec_a.doc_id}"


def test_stamp_alt_urls_adds_missing_url_and_persists(tmp_path):
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    st.fetcher.get_bytes = lambda url: (b"%PDF-1.4 stamp body", "application/pdf")
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="A",
                    pdf_url="https://www.banque-france.fr/stamp-a.pdf",
                    date=date(2020, 1, 1))
    assert st.save(rec) == "saved"

    n = st.stamp_alt_urls({rec.doc_id: ["https://dead.example/stamp-a.pdf"]})
    assert n == (1, 1)

    # Persisted -- a fresh Storage sees the stamped alt_url.
    st2 = Storage(cfg)
    row = next(r for r in st2.iter_manifest("fr") if r["doc_id"] == rec.doc_id)
    assert row["alt_urls"] == ["https://dead.example/stamp-a.pdf"]


def test_stamp_alt_urls_skips_url_equal_to_pdf_url(tmp_path):
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    st.fetcher.get_bytes = lambda url: (b"%PDF-1.4 stamp body 2", "application/pdf")
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="A",
                    pdf_url="https://www.banque-france.fr/stamp-b.pdf",
                    date=date(2020, 1, 1))
    assert st.save(rec) == "saved"

    n = st.stamp_alt_urls({rec.doc_id: [rec.pdf_url]})
    assert n == (0, 0)
    row = next(r for r in st.iter_manifest("fr") if r["doc_id"] == rec.doc_id)
    assert row["alt_urls"] == []


def test_stamp_alt_urls_skips_already_present_alt(tmp_path):
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    st.fetcher.get_bytes = lambda url: (b"%PDF-1.4 stamp body 3", "application/pdf")
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="A",
                    pdf_url="https://www.banque-france.fr/stamp-c.pdf",
                    alt_urls=["https://mirror.example/stamp-c.pdf"],
                    date=date(2020, 1, 1))
    assert st.save(rec) == "saved"

    n = st.stamp_alt_urls({rec.doc_id: ["https://mirror.example/stamp-c.pdf"]})
    assert n == (0, 0)
    row = next(r for r in st.iter_manifest("fr") if r["doc_id"] == rec.doc_id)
    assert row["alt_urls"] == ["https://mirror.example/stamp-c.pdf"]


def test_stamp_alt_urls_no_op_returns_zero_and_does_not_rewrite(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    st.fetcher.get_bytes = lambda url: (b"%PDF-1.4 stamp body 4", "application/pdf")
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="A",
                    pdf_url="https://www.banque-france.fr/stamp-d.pdf",
                    date=date(2020, 1, 1))
    assert st.save(rec) == "saved"

    calls = []
    monkeypatch.setattr(st, "rewrite_manifest", lambda rows: calls.append(rows))
    assert st.stamp_alt_urls({}) == (0, 0)
    assert st.stamp_alt_urls({rec.doc_id: []}) == (0, 0)
    assert calls == []   # never rewritten on a no-op


def test_stamp_alt_urls_ignores_unknown_doc_id(tmp_path):
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    st.fetcher.get_bytes = lambda url: (b"%PDF-1.4 stamp body 5", "application/pdf")
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="A",
                    pdf_url="https://www.banque-france.fr/stamp-e.pdf",
                    date=date(2020, 1, 1))
    assert st.save(rec) == "saved"

    n = st.stamp_alt_urls({"not-a-real-doc-id": ["https://dead.example/x.pdf"]})
    assert n == (0, 0)
    row = next(r for r in st.iter_manifest("fr") if r["doc_id"] == rec.doc_id)
    assert row["alt_urls"] == []


def test_stamp_alt_urls_returns_urls_and_rows_modified(tmp_path):
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    st.fetcher.get_bytes = lambda url: (b"%PDF-1.4 stamp body order", "application/pdf")
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="A",
                    pdf_url="https://www.banque-france.fr/stamp-order.pdf",
                    date=date(2020, 1, 1))
    assert st.save(rec) == "saved"

    u1 = "https://dead.example/stamp-order-1.pdf"
    u2 = "https://mirror.example/stamp-order-2.pdf"
    assert st.stamp_alt_urls({rec.doc_id: [u1, u2]}) == (2, 1)
    # Idempotent re-run: nothing left to add, no row touched.
    assert st.stamp_alt_urls({rec.doc_id: [u1, u2]}) == (0, 0)


def test_sweep_chrome_profiles_removes_dead_pids(tmp_path):
    """R3: stale `.chrome-profile-<pid>` dirs of dead processes are swept; the
    live one (ours + current PID) is kept."""
    import os
    from cb_corpus.storage import _sweep_chrome_profiles
    dead = tmp_path / ".chrome-profile-999999"; dead.mkdir()
    alive = tmp_path / f".chrome-profile-{os.getpid()}"; alive.mkdir()
    keep = tmp_path / ".chrome-profile-keepme"; keep.mkdir()
    _sweep_chrome_profiles(tmp_path, keep=keep)
    assert not dead.exists()        # PID 999999 not alive -> removed
    assert alive.exists()           # current PID alive -> kept
    assert keep.exists()            # explicitly kept


def test_storage_html_to_pdf_invokes_renderer(tmp_path, monkeypatch):
    """When fetched response is HTML and html_to_pdf=True, Storage renders to PDF."""
    from cb_corpus import storage as storage_mod

    cfg = Config(data_dir=tmp_path, html_to_pdf=True)
    st = Storage(cfg)
    # Stub fetcher to return HTML bytes.
    st.fetcher.get_bytes = lambda url: (b"<html><body>hi</body></html>", "text/html")
    # Stub Chrome renderer to just write a fake PDF.
    rendered: list = []
    def fake_render(url, output, **kw):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF-1.4 stub")
        rendered.append((url, output))
    monkeypatch.setattr(storage_mod, "render_url_to_pdf", fake_render)
    rec = DocRecord(bank_code="ecb", doc_type=DocType.A3, title="x",
                    pdf_url="https://www.ecb.europa.eu/x.en.html",
                    date=date(2024, 1, 1))
    assert st.save(rec) == "saved"
    assert rec.mime_type == "application/pdf"
    assert rec.local_path.endswith(".pdf")
    assert Path(rec.local_path).read_bytes().startswith(b"%PDF")
    assert rendered and rendered[0][0] == rec.pdf_url


def test_storage_html_to_pdf_falls_back_when_chrome_fails(tmp_path, monkeypatch):
    """If Chrome rendering raises, Storage falls back to saving the raw HTML."""
    from cb_corpus import storage as storage_mod
    cfg = Config(data_dir=tmp_path, html_to_pdf=True)
    st = Storage(cfg)
    st.fetcher.get_bytes = lambda url: (b"<html>raw</html>", "text/html")
    def boom(url, output, **kw):
        raise RuntimeError("chrome down")
    monkeypatch.setattr(storage_mod, "render_url_to_pdf", boom)
    rec = DocRecord(bank_code="ecb", doc_type=DocType.A3, title="x",
                    pdf_url="https://e.test/x.html", date=date(2024, 1, 1))
    assert st.save(rec) == "saved"
    assert rec.mime_type == "text/html"
    assert rec.local_path.endswith(".html")


def test_storage_html_to_pdf_disabled_keeps_html(tmp_path):
    """html_to_pdf=False -> HTML stays HTML on disk."""
    cfg = Config(data_dir=tmp_path, html_to_pdf=False)
    st = Storage(cfg)
    st.fetcher.get_bytes = lambda url: (b"<html>raw</html>", "text/html")
    rec = DocRecord(bank_code="ecb", doc_type=DocType.A3, title="x",
                    pdf_url="https://e.test/x.html", date=date(2024, 1, 1))
    assert st.save(rec) == "saved"
    assert rec.mime_type == "text/html"
    assert rec.local_path.endswith(".html")


def test_retry_html_recovers_and_records_failures(tmp_path, monkeypatch):
    """retry_failed retries leftover HTML rows; recovered rows update manifest,
    irrecoverable URLs go to data/failed_urls.txt."""
    import json as _json
    from cb_corpus import retry_html as retry_mod

    cfg = Config(data_dir=tmp_path)
    # Seed manifest: one row still text/html (will recover on first try),
    # one row still text/html (all strategies will fail), one already-pdf row.
    paths = []
    for code, mime, suffix in [("a", "text/html", ".html"),
                               ("b", "text/html", ".html"),
                               ("c", "application/pdf", ".pdf")]:
        p = tmp_path / "raw" / "ecb" / "A3" / "2024" / f"{code}{suffix}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"<seed>")
        paths.append(p)
    cfg.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg.manifest_path.open("w") as fh:
        for p, mime, code in zip(paths, ["text/html", "text/html", "application/pdf"], ["a", "b", "c"]):
            fh.write(_json.dumps({
                "bank_code": "ecb", "doc_type": "A3", "title": "x",
                "pdf_url": f"https://e.test/{code}.en.html",
                "date": "2024-01-01", "year": 2024, "doc_id": code,
                "mime_type": mime, "local_path": str(p),
                "sha256": None, "language": "en",
            }) + "\n")

    monkeypatch.setattr(retry_mod, "find_chrome", lambda: "/fake/chrome")

    # First URL recovers on attempt; second URL fails every attempt.
    def fake_try(chrome, url, output, *, timeout, virtual_time_budget_ms, user_data_dir):
        if "a.en.html" in url:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"%PDF-1.4 recovered")
            return True
        return False
    monkeypatch.setattr(retry_mod, "_try_strategy", fake_try)

    counts = retry_mod.retry_failed(cfg)
    assert counts.get("recovered") == 1
    assert counts.get("still-fail") == 1
    assert counts.get("skip") == 1

    # Manifest updated correctly.
    from cb_corpus.storage import iter_manifest_rows
    rows = list(iter_manifest_rows(cfg))
    a_row = next(r for r in rows if r["doc_id"] == "a")
    b_row = next(r for r in rows if r["doc_id"] == "b")
    c_row = next(r for r in rows if r["doc_id"] == "c")
    assert a_row["mime_type"] == "application/pdf"
    assert a_row["local_path"].endswith(".pdf")
    assert b_row["mime_type"] == "text/html"  # still failed
    assert c_row["mime_type"] == "application/pdf"  # untouched

    # Failed URLs file written.
    failed = (cfg.data_dir / "failed_urls.txt").read_text()
    assert "b.en.html" in failed
    assert "a.en.html" not in failed


def test_convert_existing_rewrites_manifest(tmp_path, monkeypatch):
    """convert_existing turns HTML rows into PDF rows and rewrites the manifest."""
    import json as _json
    from cb_corpus import convert as convert_mod
    cfg = Config(data_dir=tmp_path)
    # Seed an HTML file on disk + matching manifest entry.
    html_path = tmp_path / "raw" / "ecb" / "A3" / "2024" / "abc123.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_bytes(b"<html>seed</html>")
    cfg.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg.manifest_path.open("w") as fh:
        fh.write(_json.dumps({
            "bank_code": "ecb", "doc_type": "A3", "title": "x",
            "pdf_url": "https://e.test/abc.en.html",
            "date": "2024-01-01", "year": 2024, "doc_id": "abc123",
            "mime_type": "text/html", "local_path": str(html_path),
            "sha256": None, "language": "en",
        }) + "\n")

    monkeypatch.setattr(convert_mod, "find_chrome", lambda: "/fake/chrome")
    def fake_render(url, output, **kw):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF-1.4 ok")
    monkeypatch.setattr(convert_mod, "render_url_to_pdf", fake_render)

    counts = convert_mod.convert_existing(cfg)
    assert counts == {"converted": 1}
    pdf_path = html_path.with_suffix(".pdf")
    # New policy: keep BOTH the HTML and the rendered PDF.
    assert pdf_path.exists() and html_path.exists()
    from cb_corpus.storage import iter_manifest_rows
    row = list(iter_manifest_rows(cfg))[0]
    assert row["mime_type"] == "application/pdf"
    assert row["local_path"].endswith(".pdf")
    assert row["html_path"].endswith(".html")


def test_storage_target_path_extension(tmp_path):
    from datetime import date as _d
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    pdf_rec = DocRecord(bank_code="us", doc_type=DocType.A3, title="m",
                        pdf_url="https://x/y.pdf", date=_d(2024, 1, 1),
                        mime_type="application/pdf")
    assert st.target_path(pdf_rec).suffix == ".pdf"
    html_rec = DocRecord(bank_code="ecb", doc_type=DocType.A3, title="m",
                         pdf_url="https://x/y.en.html", date=_d(2024, 1, 1),
                         mime_type="text/html")
    assert st.target_path(html_rec).suffix == ".html"


# ---- completeness matrix --------------------------------------------
def test_completeness_matrix_statuses(tmp_path):
    cfg = Config(data_dir=tmp_path)
    # one downloaded FOMC minute in 2024 (expected 8 -> partial)
    rec = DocRecord(bank_code="us", doc_type=DocType.A3, title="m",
                    pdf_url="https://www.federalreserve.gov/x.pdf",
                    date=date(2024, 5, 1))
    cfg.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg.manifest_path.open("w") as fh:
        fh.write(json.dumps(rec.to_row()) + "\n")
    st = Storage(cfg)
    rows = build_matrix([2024], bank_codes=["us"], storage=st)
    cell = next(r for r in rows if r["doc_type"] == "A3" and r["year"] == 2024)
    assert cell["expected"] == 8 and cell["downloaded"] == 1
    assert cell["status"] == "partial"
    # a type with no downloads and known expectation -> missing
    f1 = next(r for r in rows if r["doc_type"] == "F1" and r["year"] == 2024)
    assert f1["status"] == "missing"
    assert "partial" in summarize(rows)


# ---- reliability: surfaced failures, throttle, convergence audit trail ----
class _BoomFetcher:
    """Fetcher stub whose every fetch fails — to test that discovery records
    the failure instead of swallowing it."""
    def get_text(self, url):
        raise RuntimeError("network down")


def test_adapter_fetch_text_records_failure_instead_of_swallowing():
    from cb_corpus.adapters.base import GenericAdapter
    a = GenericAdapter(get_bank("ecb"), _BoomFetcher())
    assert a._fetch_text("https://x.test/listing", context="probe") is None
    assert len(a.errors) == 1
    assert a.errors[0]["url"] == "https://x.test/listing"
    assert a.errors[0]["context"] == "probe"
    assert "RuntimeError" in a.errors[0]["error"]


def test_ecb_discovery_records_error_and_does_not_crash_on_fetch_failure():
    from cb_corpus.adapters.ecb import ECBAdapter
    a = ECBAdapter(get_bank("ecb"), _BoomFetcher())
    # A transient index failure must NOT raise and must NOT silently yield [];
    # it has to leave a breadcrumb so the caller knows to re-run.
    recs = list(a.discover(DocType.A3))
    assert recs == []
    assert any(e["context"] == "A3-index" for e in a.errors)


def test_fetcher_throttle_is_public_and_records_host():
    from cb_corpus.http import Fetcher
    f = Fetcher()
    f.throttle("https://www.ecb.europa.eu/press/accounts/x.html")
    assert "www.ecb.europa.eu" in f._last_hit


def test_pipeline_writes_discovery_errors_audit_trail(tmp_path):
    from cb_corpus.pipeline import _record_discovery_errors
    cfg = Config(data_dir=tmp_path)
    _record_discovery_errors(cfg, [
        {"bank": "ecb", "context": "A3-year", "url": "u1", "error": "RuntimeError: x"},
    ])
    out = tmp_path / "discovery_errors.jsonl"
    assert out.exists()
    row = json.loads(out.read_text().splitlines()[0])
    assert row["bank"] == "ecb" and row["context"] == "A3-year"
    # No errors -> no file churn.
    _record_discovery_errors(cfg, [])
    assert len(out.read_text().splitlines()) == 1


def test_pipeline_run_converges_and_records_errors(tmp_path, monkeypatch):
    """run(max_rounds>1) must re-crawl until a clean round, then stop — and
    persist discovery errors. Covers the reliability convergence loop."""
    from cb_corpus import pipeline as pl

    rounds_seen = {"n": 0}

    class FakeAdapter:
        def __init__(self):
            # error on the first round only -> forces a second round, then clean
            self.errors = ([{"bank": "x", "url": "u", "context": "c", "error": "E"}]
                           if rounds_seen["n"] == 0 else [])
            rounds_seen["n"] += 1

        def discover_all(self, scope, since, native_only=False):
            return iter([])

    class FakeStorage:
        def __init__(self, *a, **k):
            pass

        def is_known_url(self, url):
            return False

        def save_many(self, recs, dry_run=False, label=""):
            list(recs)
            return {"skip": 0}

    monkeypatch.setattr(pl, "Fetcher", lambda cfg: object())
    monkeypatch.setattr(pl, "Storage", FakeStorage)
    monkeypatch.setattr(pl, "get_adapter", lambda code, fetcher: FakeAdapter())

    cfg = Config(data_dir=tmp_path)
    pl.run(bank_codes=["x"], dry_run=False, config=cfg, max_rounds=5)
    # round 1 had an error -> not converged; round 2 clean -> stop. Exactly 2.
    assert rounds_seen["n"] == 2
    assert (tmp_path / "discovery_errors.jsonl").exists()


def test_pipeline_run_retries_through_an_all_bank_crash_round(tmp_path, monkeypatch):
    """A round where EVERY bank's get_adapter/discover_all CRASHES is caught
    per-bank (report-gated except path `continue`s), leaving round_saved==0,
    round_errors==0 and last_disc_errors==[] -- indistinguishable from a
    clean round unless crashes are tracked separately. Without that tracking
    the retry loop would break after round 1 as if nothing was wrong,
    defeating retries exactly on the global blips (e.g. a DNS outage) they
    exist for. rounds=3: get_adapter must be called once per bank per round
    (1 bank x 3 rounds == 3 calls), never stopping early."""
    from cb_corpus import pipeline as pl
    from cb_corpus.runreport import RunReport

    calls = {"n": 0}

    def crashing_get_adapter(code, fetcher):
        calls["n"] += 1
        raise RuntimeError("adapter construction blew up")

    class FakeStorage:
        def __init__(self, *a, **k):
            pass

        def is_known_url(self, url):
            return False

        def save_many(self, recs, dry_run=False, label=""):
            list(recs)
            return {"skip": 0}

    monkeypatch.setattr(pl, "Fetcher", lambda cfg: object())
    monkeypatch.setattr(pl, "Storage", FakeStorage)
    monkeypatch.setattr(pl, "get_adapter", crashing_get_adapter)

    cfg = Config(data_dir=tmp_path)
    report = RunReport("central-bank-corpus", "discover")
    pl.run(bank_codes=["x"], dry_run=False, config=cfg, max_rounds=3, report=report)
    # Every round crashes -> never converges -> all 3 rounds run (the cap),
    # not just 1 (which is what the pre-fix break-as-if-clean bug produced).
    assert calls["n"] == 3
    assert report.source("x").fetch_errors == 3


def test_fetcher_retries_then_raises(monkeypatch):
    """Fetcher.get retries up to max_retries with backoff, then raises — the
    resilience contract the crawl depends on."""
    from cb_corpus import http
    monkeypatch.setattr(http.time, "sleep", lambda s: None)  # no real backoff wait
    f = http.Fetcher()
    calls = {"n": 0}

    class BoomSession:
        def get(self, *a, **k):
            calls["n"] += 1
            raise http.requests.exceptions.ConnectionError("down")

    f.session = BoomSession()
    with pytest.raises(RuntimeError):
        f.get("https://x.test/a")
    assert calls["n"] == f.cfg.max_retries


def test_fetcher_get_bytes_streams_and_enforces_total_deadline(monkeypatch):
    """get_bytes joins streamed chunks, and a slow-trickle body that never trips
    the inactivity timeout is still aborted by the total download deadline."""
    from cb_corpus import http
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    f = http.Fetcher()

    class OkResp:
        headers = {"content-type": "application/pdf; charset=binary"}
        def raise_for_status(self): pass
        def iter_content(self, chunk_size):
            yield b"%PDF-"; yield b"data"

    f.session = type("S", (), {"get": lambda self, u, **k: OkResp()})()
    body, mime = f.get_bytes("https://x.test/a.pdf")
    assert body == b"%PDF-data" and mime == "application/pdf"

    # Infinite trickle + past-deadline -> TimeoutError each attempt -> RuntimeError.
    f.cfg.download_timeout = -1.0

    class Trickle:
        headers = {"content-type": "application/pdf"}
        def raise_for_status(self): pass
        def iter_content(self, chunk_size):
            while True:
                yield b"x"

    f.session = type("S", (), {"get": lambda self, u, **k: Trickle()})()
    with pytest.raises(RuntimeError):
        f.get_bytes("https://x.test/hang.pdf")


def test_cli_dispatches_subcommands(monkeypatch, capsys):
    """list-banks runs offline; discover/repec/bis-sitemap dispatch is wired
    (guards against command-registration regressions like a new subcommand)."""
    from cb_corpus import cli
    assert cli.main(["list-banks"]) == 0
    assert "63 banks" in capsys.readouterr().out

    monkeypatch.setattr(cli, "run", lambda **k: {"us": {"saved": 1}})
    monkeypatch.setattr(cli, "run_repec", lambda **k: {"us": {"saved": 2}})
    monkeypatch.setattr(cli, "run_bis_sitemap", lambda **k: {"saved": 3})
    assert cli.main(["discover", "--banks", "us"]) == 0
    assert cli.main(["repec", "--download"]) == 0
    assert cli.main(["bis-sitemap"]) == 0


# ---- URL normalization (index-only; see 2026-08-25 spec) -------------
def test_normalize_url_collapses_path_slashes_only():
    from cb_corpus.storage import normalize_url
    assert normalize_url("https://a.eu//press//x.pdf") == "https://a.eu/press/x.pdf"
    assert normalize_url("https://a.eu/press/x.pdf") == "https://a.eu/press/x.pdf"
    assert normalize_url("http://a.eu///b////c") == "http://a.eu/b/c"
    # scheme separator untouched, idempotent, total on odd inputs
    assert normalize_url("no-scheme//path") == "no-scheme/path"
    assert normalize_url("") == ""
    assert normalize_url(normalize_url("https://a.eu//x")) == "https://a.eu/x"


def test_normalize_url_does_not_equalize_out_of_scope_variants():
    from cb_corpus.storage import normalize_url
    assert normalize_url("http://a.eu/x.pdf") != normalize_url("https://a.eu/x.pdf")
    assert normalize_url("https://a.eu/x/") != normalize_url("https://a.eu/x")
    assert normalize_url("https://www.a.eu/x") != normalize_url("https://a.eu/x")


def test_is_known_url_matches_slash_variants_both_directions(tmp_path):
    """A row indexed under a double-slash URL is known under the clean form,
    and vice versa: a clean-indexed row matches a double-slash query. Locks
    Task 2's write+read normalization from both directions."""
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    st.fetcher.get_bytes = lambda url: (f"%PDF-1.4 {url}".encode(), "application/pdf")

    rec_a = DocRecord(bank_code="e", doc_type=DocType.D1, title="A",
                      pdf_url="https://e.eu//press//a.pdf", date=date(2024, 1, 1))
    assert st.save(rec_a) == "saved"
    assert st.is_known_url("https://e.eu/press/a.pdf")

    rec_b = DocRecord(bank_code="e", doc_type=DocType.D1, title="B",
                      pdf_url="https://e.eu/press/b.pdf", date=date(2024, 1, 1))
    assert st.save(rec_b) == "saved"
    assert st.is_known_url("https://e.eu//press/b.pdf")


def test_known_url_variants_cover_alt_urls_reload_and_source_urls(tmp_path):
    """A double-slash alt_urls entry matches the clean form after a fresh
    Storage() over the same data dir (exercises _load_existing), and
    is_known_source_url normalizes the same way as is_known_url."""
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    st.fetcher.get_bytes = lambda url: (b"%PDF-1.4 body", "application/pdf")

    rec = DocRecord(bank_code="e", doc_type=DocType.D1, title="A",
                    pdf_url="https://e.eu/press/preferred.pdf",
                    alt_urls=["https://e.eu//press//alt.pdf"],
                    source_url="https://e.eu//press//index.html",
                    date=date(2024, 1, 1))
    assert st.save(rec) == "saved"

    # Fresh Storage over the same data dir -> _load_existing re-populates
    # the indexes from the persisted manifest row.
    st2 = Storage(cfg)
    assert st2.is_known_url("https://e.eu/press/alt.pdf")           # alt_urls, normalized on reload
    assert st2.is_known_source_url("https://e.eu/press/index.html")  # source_url, normalized


def test_user_agent_contact_env_driven(monkeypatch):
    """The crawler contact must identify the OPERATOR, never default to a
    personal address: without CRAWLER_CONTACT the UA points at the public
    repo; with it, the operator's own contact is advertised."""
    from cb_corpus.config import Config
    monkeypatch.delenv("CRAWLER_CONTACT", raising=False)
    assert Config().user_agent == \
        "central-bank-corpus/0.2 (+https://github.com/MyOpenFund/central-bank-corpus)"
    monkeypatch.setenv("CRAWLER_CONTACT", "ops@example.org")
    assert Config().user_agent == "central-bank-corpus/0.2 (+ops@example.org)"
    monkeypatch.setenv("CRAWLER_CONTACT", "   ")   # blank -> falls back, never "(+)"
    assert "myopenfund" in Config().user_agent.lower()
