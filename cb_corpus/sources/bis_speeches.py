"""BIS speech discovery via per-year sitemaps  ->  DocRecord(C1).

The old BIS listing pages (/list/cbspeeches/) are now a React SPA — they expose
no data in the static HTML. The yearly XML sitemaps are still public and
contain every speech URL: `/review/r<YYMMDDx>.pdf` for the PDF and
`/review/r<YYMMDDx>.htm` for the detail page.

Strategy:
  1. Fetch the sitemap index at /sitemap.xml -> list of yearly sitemaps.
  2. For each target year, fetch sitemap_documents_<YYYY>.xml.
  3. Filter URLs matching /review/r<YYMMDDx>.pdf  (one DocRecord per speech).
  4. Derive the date from the URL slug (r<YYMMDD>...).
  5. Fetch the .htm detail page to extract og:description -> institution name
     -> bank_code, plus a clean title.

`/review/` is NOT in robots.txt's Disallow list, so this path is clean.
Speeches before ~1996 may not be indexed in sitemaps.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..banks import bank_for_bis_institution
from ..http import Fetcher
from ..models import DocRecord
from ..runreport import SourceStats
from ..taxonomy import DocType

BIS_BASE = "https://www.bis.org"
SITEMAP_INDEX = BIS_BASE + "/sitemap.xml"

_YEAR_SITEMAP_RE = re.compile(r"sitemap_documents_(\d{4})\.xml$")
_REVIEW_PDF_RE = re.compile(r"^https?://[^/]+/review/r(\d{6})[a-z]?\.pdf$", re.I)


@dataclass
class BISSpeechMeta:
    document_url: str
    detail_url: str
    date: Optional[date]


def _parse_slug_date(yymmdd: str) -> Optional[date]:
    """Parse the 6-digit slug from a /review/rYYMMDDx.pdf URL.

    BIS speeches archive starts in 1996, so yy>=90 maps to 19yy and yy<90 to
    20yy. Returns None on malformed input.
    """
    try:
        yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    except ValueError:
        return None
    yyyy = 1900 + yy if yy >= 90 else 2000 + yy
    try:
        return date(yyyy, mm, dd)
    except ValueError:
        return None


def parse_sitemap_index(xml: str) -> list[tuple[int, str]]:
    """Return (year, sitemap_url) pairs from the BIS sitemap index."""
    soup = BeautifulSoup(xml, "xml")
    out: list[tuple[int, str]] = []
    for sm in soup.find_all("sitemap"):
        loc = sm.find("loc")
        if loc is None:
            continue
        url = loc.get_text(strip=True)
        m = _YEAR_SITEMAP_RE.search(url)
        if m:
            out.append((int(m.group(1)), url))
    return sorted(out)


def parse_year_sitemap(xml: str) -> list[BISSpeechMeta]:
    """Extract speech entries (PDF URL + date) from a year's sitemap."""
    soup = BeautifulSoup(xml, "xml")
    out: list[BISSpeechMeta] = []
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if loc is None:
            continue
        pdf = loc.get_text(strip=True)
        m = _REVIEW_PDF_RE.match(pdf)
        if not m:
            continue
        d = _parse_slug_date(m.group(1))
        detail = pdf[:-4] + ".htm"
        out.append(BISSpeechMeta(document_url=pdf, detail_url=detail, date=d))
    return out


# Event-introducer markers that separate the SPEAKER half of a BIS description
# ("Speech by X, Governor of the Bank of Y") from the EVENT half ("at the Z
# conference, organised by ..."). The speaker's institution is always before
# the first such marker.
_SPEAKER_END_RE = re.compile(
    r"\s+(?:at|during|to|before|in|on|hosted by|organised by|organized by"
    r"|sponsored by|via|by videoconference|by video conference)\s+",
    re.I,
)


def _guess_institution(text: str) -> str:
    """Map a free-text description to a known BIS-63 institution label.

    Looks only in the SPEAKER half of the description (the text before the
    first event-introducer like "at" / "organised by"). Picks the LONGEST
    registry label (primary OR historical/renamed alias) that matches, accent-
    and apostrophe-insensitively. Returns the bank's PRIMARY label, or "".
    """
    from ..banks import match_bis_institution
    if not text:
        return ""
    m = _SPEAKER_END_RE.search(text)
    head = text[:m.start()] if m else text
    return match_bis_institution(head)


def parse_detail(html: str) -> tuple[str, str]:
    """Return (title, institution_text) from a speech detail page.

    Uses <meta og:title> for the title and <meta og:description> for the
    institution-bearing free text. Both fields are stable on bis.org.
    """
    soup = BeautifulSoup(html, "lxml")
    title = ""
    desc = ""
    for m in soup.find_all("meta"):
        prop = (m.get("property") or m.get("name") or "").lower()
        if prop == "og:title":
            title = (m.get("content") or "").strip()
        elif prop == "og:description":
            desc = (m.get("content") or "").strip()
    return title, desc


class BISSpeechIndex:
    """Discover C1 speeches from BIS's yearly XML sitemaps."""

    def __init__(self, fetcher: Optional[Fetcher] = None):
        self.fetcher = fetcher or Fetcher()

    def list_years(self) -> list[tuple[int, str]]:
        xml = self.fetcher.get_text(SITEMAP_INDEX)
        return parse_sitemap_index(xml)

    def speeches_for_year(self, year: int,
                          sitemap_url: Optional[str] = None) -> list[BISSpeechMeta]:
        url = sitemap_url or f"{BIS_BASE}/sitemap_documents_{year}.xml"
        xml = self.fetcher.get_text(url)
        return parse_year_sitemap(xml)

    def discover(self, since: Optional[date] = None,
                 until: Optional[date] = None,
                 only_banks: Optional[set[str]] = None,
                 max_per_year: Optional[int] = None,
                 skip_url: Optional[Callable[[str], bool]] = None,
                 stats: Optional[SourceStats] = None,
                 ) -> Iterator[DocRecord]:
        """Yield C1 DocRecords by walking yearly sitemaps.

        `since`/`until` bound by document date. `only_banks` restricts to the
        given bank_code set (still requires the detail-page fetch to know).
        `max_per_year` caps work per year (useful for smoke tests).
        `skip_url(url)` short-circuits BEFORE the per-speech detail fetch — used
        by the pipeline to skip speeches already in the manifest, so re-runs
        don't waste 30k HTML fetches.

        `stats`, when given, isolates fetch failures instead of aborting the
        whole discovery: a dead sitemap index or a dead per-year sitemap is
        recorded via `record_fetch_error(..., truncated=True)` and the walk
        moves on (index failure ends discovery with nothing to walk; a year
        failure just skips that year and continues with the next one); a dead
        detail-page fetch is recorded (not truncated) in place of today's bare
        skip. Without `stats`, all three failure modes raise as before (kept
        for library / backward-compatible callers).
        """
        start_year = since.year if since else 1996
        end_year = until.year if until else date.today().year
        try:
            years = [y for y, _ in self.list_years() if start_year <= y <= end_year]
        except Exception as exc:
            if stats is None:
                raise
            stats.record_fetch_error(f"sitemap index: {exc}", truncated=True)
            return
        for year in years:
            try:
                metas = self.speeches_for_year(year)
            except Exception as exc:
                if stats is None:
                    raise
                stats.record_fetch_error(f"year {year} sitemap: {exc}", truncated=True)
                continue
            if max_per_year:
                metas = metas[:max_per_year]
            for meta in metas:
                if meta.date is None:
                    continue
                if since and meta.date < since:
                    continue
                if until and meta.date > until:
                    continue
                if skip_url is not None and skip_url(meta.document_url):
                    continue
                try:
                    html = self.fetcher.get_text(meta.detail_url)
                except Exception as exc:
                    if stats is not None:
                        stats.record_fetch_error(f"detail {meta.detail_url}: {exc}")
                    continue
                title, desc = parse_detail(html)
                institution = _guess_institution(desc) or _guess_institution(title)
                bank = bank_for_bis_institution(institution) if institution else None
                if bank is None:
                    continue
                if only_banks and bank.code not in only_banks:
                    continue
                yield DocRecord(
                    bank_code=bank.code,
                    doc_type=DocType.C1,
                    title=title or meta.document_url,
                    document_url=meta.document_url,
                    source_url=meta.detail_url,
                    date=meta.date,
                    provenance="bis_index",
                    mime_type="application/pdf",
                    date_precision="day",    # r<YYMMDD> in the BIS review slug
                )


# --- legacy helpers (kept for backward-compatible tests) ----------------
def parse_listing(html: str, base_url: str = BIS_BASE) -> list:
    """Legacy fixture parser kept for the existing unit test only.

    The live BIS listing pages are now a React SPA — see BISSpeechIndex above
    for the real discovery path.
    """
    soup = BeautifulSoup(html, "lxml")
    items: list = []

    @dataclass
    class _LegacyItem:
        date: Optional[date]
        title: str
        institution: str
        detail_url: str
        document_url: Optional[str]

    def _legacy_parse_date(t: str) -> Optional[date]:
        from datetime import datetime as _dt
        for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
            try:
                return _dt.strptime(t.strip(), fmt).date()
            except ValueError:
                continue
        return None

    table = soup.find("table", class_="documentList") or soup.find("table")
    if table is None:
        return items
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        date_cell = row.find("td", class_="item_date") or cells[0]
        title_cell = row.find("td", class_="title") or cells[-1]
        link = title_cell.find("a", href=True)
        if not link:
            continue
        info = title_cell.get_text(" ", strip=True)
        institution = _guess_institution(info)
        pdf_link = title_cell.find("a", href=lambda h: h and h.lower().endswith(".pdf"))
        pdf = urljoin(base_url, pdf_link["href"]) if pdf_link else None
        items.append(_LegacyItem(
            date=_legacy_parse_date(date_cell.get_text(strip=True)),
            title=link.get_text(strip=True),
            institution=institution,
            detail_url=urljoin(base_url, link["href"]),
            document_url=pdf,
        ))
    return items


def extract_pdf_url(detail_html: str, base_url: str = BIS_BASE) -> Optional[str]:
    """Find the original PDF link on a BIS speech detail page (legacy helper)."""
    soup = BeautifulSoup(detail_html, "lxml")
    link = soup.find("a", href=lambda h: h and h.lower().endswith(".pdf"))
    return urljoin(base_url, link["href"]) if link else None
