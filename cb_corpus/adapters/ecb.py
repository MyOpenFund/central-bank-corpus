"""European Central Bank adapter -- v2.

Native ECB listings on ecb.europa.eu:
  A3  monetary policy accounts   ~8/yr    (HTML-only since 2024 — stored as .html)
  E4  Economic Bulletin          ~8/yr    (PDF)
  D3  ECB Blog posts             ~2-3/wk  (HTML-only, no PDF version)
  C2  Interviews / op-eds        ~1-2/wk  (HTML-only, live foedb source — see below)

Accounts are now lazy-loaded year-by-year (`<year>/html/index_include.en.html`)
and ECB no longer publishes a PDF version — the HTML is the canonical artifact.
The Economic Bulletin index (`all_releases.en.html`) still inlines all PDF
links for every release across years.

The Blog's old per-section per-year include endpoint
(`/press/blog/date/<year>/html/index_include.en.html`) is dead (404 for every
year, checked 2026-08) — discovery instead parses the human-facing master
listing (`BLOG_INDEX`), a static HTML page that inlines posts across years.

Interviews (C2) are read from the same live foedb JSON DB that already serves
D1/D2 (`sources/ecb_foedb.discover_ecb_interviews`, type == "ECB Interview"),
zero lag and full archive coverage. Non-English-only interviews are excluded
by policy, counted, and reported on stderr (documented exclusion, never a
silent one — see that module's docstring).

Speeches (C1) and WPS/Occasional papers (D1/D2) come from the base class.
"""
from __future__ import annotations

import calendar
import re
import sys
from datetime import date, datetime
from typing import Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import DocRecord
from ..taxonomy import DocType
from .base import BankAdapter, register

ECB = "https://www.ecb.europa.eu"
ACCOUNTS_INDEX = ECB + "/press/accounts/html/index.en.html"
BULLETIN_INDEX = ECB + "/press/economic-bulletin/html/all_releases.en.html"
# ECB Blog (D3) master listing — the per-year include endpoint
# (`/press/blog/date/<year>/html/index_include.en.html`) is dead (404, all
# years) as of 2026-08; this human-facing listing is the live static-HTML
# source (posts inlined across years, no JS needed).
BLOG_INDEX = ECB + "/press/blog/html/index.en.html"
# Monetary-policy DECISIONS index (A1) — same lazy-load year-include mechanism
# as accounts. Each year lists decisions (mp/legacy pr), accounts (mg) and
# statements (is); we keep the decisions.
MOPO_INDEX = ECB + "/press/govcdec/mopo/html/index.en.html"
# Dedicated monetary-policy STATEMENT index (A2) — same lazy-load mechanism,
# full history (the MOPO index only links statements for recent years).
STATEMENT_INDEX = ECB + "/press/press_conference/monetary-policy-statement/html/index.en.html"

# Accounts use two URL conventions over time:
#   legacy (2015-2017): /press/accounts/2015/html/mg151119.en.html
#   modern (2017->)    : /press/accounts/2017/html/ecb.mg171123~<hash>.en.html
# The `ecb.` prefix and the `~hash` suffix are both optional.
_ACCOUNT_HTML_RE = re.compile(r"/press/accounts/\d{4}/html/(?:ecb\.)?mg(\d{6})[~a-z0-9]*\.en\.html$",
                              re.I)
# Monetary-policy decision press releases (A1), same legacy/modern split:
#   legacy (->2017): /press/pr/date/2015/html/pr151203.en.html
#   modern         : /press/pr/date/2025/html/ecb.mp251218~<hash>.en.html
# Matches mp/pr only (NOT mg=accounts/A3, NOT is=statements/A2) within the MOPO index.
_DECISION_HTML_RE = re.compile(
    r"/press/pr/date/\d{4}/html/(?:ecb\.)?(?:mp|pr)(\d{6})[~a-z0-9]*\.en\.html$", re.I)
# Monetary-policy STATEMENT (A2) — the press-conference statement (with Q&A),
# filename `is<YYMMDD>` (optionally `ecb.` prefixed), EN only. Path-agnostic
# (statements live under /press/press_conference/monetary-policy-statement/, not
# /press/pr/date/) so a path change doesn't silently drop them.
_STATEMENT_HTML_RE = re.compile(
    r"/(?:ecb\.)?is(\d{6})[~a-z0-9]*\.en\.html$", re.I)
_BULLETIN_PDF_RE = re.compile(r"/pub/pdf/ecbu/eb(\d{4})(\d{2})\.en\.pdf$", re.I)
_DATE_IN_HREF = re.compile(r"(\d{4})(\d{2})(\d{2})")
# Blog post pages (D3), English only. Two filename eras coexist on the live
# listing: modern `ecb.blog<YYYYMMDD>~<hash>.en.html` and legacy
# `ecb.blog<YYMMDD>~<hash>.en.html` (both handled by `date_from_url(fmt="auto")`
# from sources/ecb_pub.py — the same idiom `run_ecb_pub_recovery` used to
# produce the original 212 rows).
_BLOG_HTML_RE = re.compile(r"/press/blog/date/\d{4}/html/.*\.en\.html$", re.I)
_LABEL_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
_MONTH_NUMBER = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}


def _yymmdd_to_date(s: str) -> Optional[date]:
    try:
        yy, mm, dd = int(s[:2]), int(s[2:4]), int(s[4:6])
    except ValueError:
        return None
    yyyy = 1900 + yy if yy >= 90 else 2000 + yy
    try:
        return date(yyyy, mm, dd)
    except ValueError:
        return None


def parse_year_includes(html: str) -> list[str]:
    """Extract the per-year include URLs from the accounts index page."""
    soup = BeautifulSoup(html, "lxml")
    dl = soup.find(id="lazyload-container")
    if dl is None:
        return []
    snips = dl.get("data-snippets") or ""
    return [s.strip() for s in snips.split(",") if s.strip()]


def parse_account_items(html: str, base_url: str = ACCOUNTS_INDEX) -> list[tuple[date, str]]:
    """From an accounts year-include, return (date, html_url) pairs."""
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[date, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = _ACCOUNT_HTML_RE.search(href)
        if not m:
            continue
        d = _yymmdd_to_date(m.group(1))
        if d is None:
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        out.append((d, url))
    return out


def parse_decision_items(html: str, base_url: str = MOPO_INDEX) -> list[tuple[date, str]]:
    """From a MOPO year-include, return (date, html_url) for decision releases (A1).

    Keeps only monetary-policy DECISIONS (mp/legacy pr); ignores accounts (mg, A3)
    and statements (is, A2) that share the index.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[date, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = _DECISION_HTML_RE.search(href)
        if not m:
            continue
        d = _yymmdd_to_date(m.group(1))
        if d is None:
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        out.append((d, url))
    return out


def parse_statement_items(html: str, base_url: str = MOPO_INDEX) -> list[tuple[date, str]]:
    """From a MOPO year-include, return (date, html_url) for policy statements (A2)."""
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[date, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = _STATEMENT_HTML_RE.search(a["href"])
        if not m:
            continue
        d = _yymmdd_to_date(m.group(1))
        if d is None:
            continue
        url = urljoin(base_url, a["href"])
        if url in seen:
            continue
        seen.add(url)
        out.append((d, url))
    return out


def parse_bulletin_pdfs(html: str, base_url: str = BULLETIN_INDEX
                       ) -> list[tuple[Optional[date], str, str]]:
    """From the Economic Bulletin all-releases page, return (date, title, pdf_url).

    Keeps only the English-language PDFs (`eb<YYYY><II>.en.pdf`).
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[Optional[date], str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = _BULLETIN_PDF_RE.search(href)
        if not m:
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        year, issue = int(m.group(1)), int(m.group(2))
        # Issue number is sequential within the year; we don't have a precise
        # date in the URL, so use Jan 1 of that year as a placeholder.
        d = date(year, 1, 1)
        title = a.get_text(" ", strip=True) or f"Economic Bulletin {year}/{issue}"
        out.append((d, title, url))
    return out


def _date_from_label(text: str) -> Optional[date]:
    """Parse a 'DD Month YYYY' label (the blog listing's <h5> date badge,
    e.g. '27 February 2026') into a date. Returns None if unparseable."""
    m = _LABEL_DATE_RE.search(text or "")
    if not m:
        return None
    day, month_name, year = m.groups()
    month = _MONTH_NUMBER.get(month_name.lower())
    if not month:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def parse_blog_items(html: str, base_url: str = BLOG_INDEX
                     ) -> list[tuple[Optional[date], str, str]]:
    """From the ECB blog master listing, return (date, title, url) for each
    English-language post (`.en.html` only — non-English siblings share the
    same card and must be excluded).

    Each post appears in 1-2 anchors on the page (the card itself, plus a
    duplicate "arrow" language-selector link) — deduped by url, first
    occurrence wins. Date comes primarily from the URL (`date_from_url`,
    handles both the modern 8-digit and legacy 6-digit filename eras);
    when the URL carries no parseable date, falls back to the card's <h5>
    label. A url with NEITHER is returned with date=None — the caller's
    job to skip it (and warn), not this pure parser's.
    """
    from ..sources.ecb_pub import date_from_url
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[Optional[date], str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _BLOG_HTML_RE.search(href):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        d = date_from_url(href, "auto")
        h5 = a.find("h5") or (a.parent.find("h5") if a.parent is not None else None)
        if d is None and h5 is not None:
            d = _date_from_label(h5.get_text(" ", strip=True))
        h3 = a.find("h3") or (a.parent.find("h3") if a.parent is not None else None)
        title = h3.get_text(" ", strip=True) if h3 else ""
        out.append((d, title, url))
    return out


# legacy fixture parser - kept so the existing unit test still runs
def parse_index(html: str, base_url: str = ECB,
                href_must_contain: str = "") -> list[tuple[Optional[date], str, str]]:
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[Optional[date], str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href_must_contain and href_must_contain not in href:
            continue
        if not href.lower().endswith(".pdf"):
            continue
        d = None
        m = _DATE_IN_HREF.search(href)
        if m:
            try:
                d = datetime.strptime("".join(m.groups()), "%Y%m%d").date()
            except ValueError:
                d = None
        out.append((d, a.get_text(" ", strip=True) or href, urljoin(base_url, href)))
    return out


@register("ecb")
class ECBAdapter(BankAdapter):
    # D1/D2 are native (foedb JSON DB, see sources/ecb_foedb.py) rather than RePEc:
    # exact day dates, no indexing lag, full archive. Safe to flip only because the
    # WP v3 migration ran first (registered native URLs in alt_urls → zero
    # re-download; see docs/IMPLEMENTATION_PLAN.md phase 3).
    native_types = (DocType.A1, DocType.A2, DocType.A3, DocType.E4,
                    DocType.D1, DocType.D2, DocType.D3, DocType.C2)
    # Eras measured on the committed manifests (integrity wave, 2026-11-18):
    # the Governing Council decided twice a month in 1999-2001 (A1 ~20-24),
    # about monthly 2002-2014 (11-13), six-weekly since 2015 (8). The press
    # conference (A2) and the Monthly Bulletin (E4, 12 issues; the Economic
    # Bulletin since 2015, 8) follow the same switch. Accounts (A3) exist only
    # since 2015: one era, so earlier years are "unknown", not "missing".
    expected_per_year = {
        DocType.A1: [(1999, 20), (2002, 11), (2015, 8)],
        DocType.A2: [(1999, 11), (2015, 8)],
        DocType.A3: [(2015, 8)],
        DocType.E4: [(1999, 12), (2015, 8)],
    }

    def _discover_native(self, doc_type: DocType,
                         since: Optional[date]) -> Iterator[DocRecord]:
        if doc_type in (DocType.D1, DocType.D2):
            # Native WP/OP via the ECB foedb JSON DB (day precision, full archive).
            # Reached only once D1/D2 are added to `native_types` (post-migration);
            # until then base.py routes D1/D2 to RePEc. The migration command calls
            # discover_ecb_wp directly.
            from ..sources.ecb_foedb import discover_ecb_wp
            yield from (r for r in discover_ecb_wp(self.fetcher, since)
                        if r.doc_type == doc_type)
        elif doc_type == DocType.A1:
            yield from self._discover_index(MOPO_INDEX, since, parse_decision_items,
                                            DocType.A1, "Monetary policy decision")
        elif doc_type == DocType.A2:
            yield from self._discover_index(STATEMENT_INDEX, since, parse_statement_items,
                                            DocType.A2, "Monetary policy statement")
        elif doc_type == DocType.A3:
            yield from self._discover_accounts(since)
        elif doc_type == DocType.E4:
            yield from self._discover_bulletin(since)
        elif doc_type == DocType.D3:
            yield from self._discover_blog(since)
        elif doc_type == DocType.C2:
            from ..sources.ecb_foedb import discover_ecb_interviews
            yield from discover_ecb_interviews(self.fetcher, since)

    def _discover_index(self, index_url, since, parse_fn, doc_type, title_prefix
                        ) -> Iterator[DocRecord]:
        """Walk a lazy-load year-include index and yield the items selected by
        `parse_fn` — decisions (A1, MOPO index) or statements (A2, statement index)."""
        idx = self._fetch_text(index_url, context=f"{doc_type.code}-index")
        if idx is None:
            return
        for snippet in parse_year_includes(idx):
            year_url = urljoin(index_url, snippet)
            year_html = self._fetch_text(year_url, context=f"{doc_type.code}-year")
            if year_html is None:
                continue
            for d, url in parse_fn(year_html, year_url):
                if since and d < since:
                    continue
                yield DocRecord(
                    bank_code="ecb", doc_type=doc_type,
                    title=f"{title_prefix} {d.isoformat()}",
                    pdf_url=url, source_url=year_url, date=d,
                    provenance="bank_site",
                    date_precision="day",    # YYMMDD in the document file name
                )

    def _discover_accounts(self, since: Optional[date]) -> Iterator[DocRecord]:
        idx = self._fetch_text(ACCOUNTS_INDEX, context="A3-index")
        if idx is None:
            return
        includes = parse_year_includes(idx)
        for snippet in includes:
            year_url = urljoin(ACCOUNTS_INDEX, snippet)
            year_html = self._fetch_text(year_url, context="A3-year")
            if year_html is None:
                continue
            for d, url in parse_account_items(year_html, year_url):
                if since and d < since:
                    continue
                yield DocRecord(
                    bank_code="ecb", doc_type=DocType.A3,
                    title=f"Monetary policy account {d.isoformat()}",
                    pdf_url=url,
                    source_url=year_url,
                    date=d,
                    provenance="bank_site",
                    date_precision="day",    # YYMMDD in the document file name
                    # Source page is HTML-only; Storage will render to PDF
                    # via headless Chrome when cfg.html_to_pdf is True.
                )

    def _discover_bulletin(self, since: Optional[date]) -> Iterator[DocRecord]:
        html = self._fetch_text(BULLETIN_INDEX, context="E4-index")
        if html is None:
            return
        for d, title, pdf in parse_bulletin_pdfs(html):
            # `d` is a year placeholder (1 January, precision "year"): compare
            # YEARS against `since` (issue #23). Comparing the placeholder date
            # dropped every current-year bulletin as soon as `since` passed
            # 1 January, e.g. a --since in March skipped last week's issue.
            if since and d and d.year < since.year:
                continue
            yield DocRecord(
                bank_code="ecb", doc_type=DocType.E4,
                title=title, pdf_url=pdf,
                source_url=BULLETIN_INDEX,
                date=d,
                provenance="bank_site",
                mime_type="application/pdf",
                # The all-releases page gives no day; date is Jan 1 of the year.
                date_precision="year",
            )

    def _discover_blog(self, since: Optional[date]) -> Iterator[DocRecord]:
        """D3 — ECB Blog posts, from the live master listing (see BLOG_INDEX
        docstring: the old per-year include endpoint is dead). Blog posts are
        HTML-only artifacts (no PDF version), same convention as the historical
        212 rows recovered via the old one-off `run_ecb_pub_recovery` path.

        This method always yields normalized single-slash pdf_urls. 15 of the
        212 historical rows carry a double-slash pdf_url (`europa.eu//press/...`)
        — a one-off artifact of that old scraper. Rather than special-case them
        here, the manifest rows were amended to also index the normalized form
        in alt_urls (see `data: index normalized URL forms for 15 legacy D3
        rows`), so `Storage.is_known_url()` recognises what this method yields.
        The generic native branch in `BankAdapter.discover()` (adapters/base.py)
        applies that check via the `_skip_known_url` hook the pipeline wires up
        (`pipeline.run()`) BEFORE any record leaves `discover()` — so nightly
        discovery skips these 15 posts before `Storage.save()` ever fetches
        their page, same compat pattern as the WP v3 migration's native-URL
        alt_urls registration."""
        html = self._fetch_text(BLOG_INDEX, context="D3-index")
        if html is None:
            return
        for d, title, url in parse_blog_items(html, BLOG_INDEX):
            if d is None:
                # Neither the URL nor the <h5> label carried a parseable date —
                # a genuinely malformed anchor. Skip it (not re-runnable, so it
                # doesn't belong in self.errors) but leave a visible breadcrumb.
                print(f"!! ecb D3 blog: skipping anchor with no parseable date: {url}",
                      file=sys.stderr, flush=True)
                continue
            if since and d < since:
                continue
            yield DocRecord(
                bank_code="ecb", doc_type=DocType.D3,
                title=title or f"ECB Blog {d.isoformat()}",
                pdf_url=url,
                source_url=BLOG_INDEX,
                date=d,
                provenance="bank_site",
                mime_type="text/html",
                date_precision="day",        # as labelled since the series began
            )
