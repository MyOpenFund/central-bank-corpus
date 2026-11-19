"""Bank of England Staff Working Papers from the BoE's OWN sitemap.

The live official source. IDEAS (boe:boeewp) keeps pre-2017 URLs that 404 after
the BoE website migration; the BoE re-published every paper at
`/working-paper/<year>/<slug>`. Scraping the BoE's own sitemap gives the full
1992-present set from the issuing bank's live domain.

We resolve each paper's real PDF by reading its page (the slug-derived PDF URL is
right ~80% of the time; fetching the page is robust), falling back to the derived
URL when the page can't be read.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import Fetcher
from ..models import DocRecord
from ..taxonomy import DocType

BOE = "https://www.bankofengland.co.uk"
WP_SITEMAP = BOE + "/sitemap/staff-working-paper"
_WP_RE = re.compile(r"/working-paper/(\d{4})/([a-z0-9][a-z0-9\-]+)$", re.I)
_PDF_RE = re.compile(r"/-/media/boe/files/working-paper/\d{4}/[^\"'>\s]+?\.pdf", re.I)
# Any BoE media PDF (used for non-working-paper docs: minutes, reports, ...).
_ANY_PDF_RE = re.compile(r"/-/media/boe/files/[^\"'>\s]+?\.pdf", re.I)
_YEAR_IN_URL = re.compile(r"/(\d{4})/")
# Slug join key: from a paper page (/working-paper/2025/slug) or its media PDF
# (/-/media/boe/files/working-paper/2025/slug.pdf) — same slug either way.
_SLUG_RE = re.compile(r"/working-paper/\d{4}/([a-z0-9][a-z0-9\-]*?)(?:\.pdf)?$", re.I)
# "Published on 30 May 2025" on the paper page = the exact publication day.
_PUBLISHED_RE = re.compile(r"Published on\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", re.I)
_MON = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def boe_slug(url: str) -> Optional[str]:
    """Normalised staff-working-paper slug from a page or media-PDF URL."""
    m = _SLUG_RE.search(url or "")
    return m.group(1).lower() if m else None


def _published_date(html: str) -> Optional[date]:
    m = _PUBLISHED_RE.search(html or "")
    if not m or m.group(2)[:3].lower() not in _MON:
        return None
    try:
        return date(int(m.group(3)), _MON[m.group(2)[:3].lower()], int(m.group(1)))
    except ValueError:
        return None


def paper_meta(fetcher: Fetcher, page_url: str
               ) -> Optional[tuple[Optional[date], str, str]]:
    """Fetch a staff-WP page; return (published_date | None, title, real_pdf_url).

    The day comes from the "Published on …" line; None when absent (older pages)
    so the caller falls back to year precision. PDF/title via :func:`paper_pdf`.
    """
    try:
        html = fetcher.get_text(page_url)
    except Exception:
        return None
    soup = _soup(html)
    if soup is None:
        return None
    pdf = None
    for a in soup.find_all("a", href=True):
        if _PDF_RE.search(a["href"]):
            pdf = urljoin(BOE, a["href"])
            break
    if pdf is None:
        m = _PDF_RE.search(html)
        pdf = urljoin(BOE, m.group(0)) if m else None
    if pdf is None:
        return None
    title = ""
    for m in soup.find_all("meta"):
        if (m.get("property") or "").lower() == "og:title":
            title = (m.get("content") or "").strip()
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    return _published_date(html), (title or page_url.rsplit("/", 1)[-1]), pdf


def discover_boe_wp(fetcher: Fetcher, since: Optional[date] = None,
                    years: Optional[set] = None) -> Iterator[DocRecord]:
    """Yield BoE Staff Working Papers (D1) with the exact day from each paper page.

    Walks the staff-WP sitemap (optionally restricted to `years` / `since` year),
    then reads each paper page for its "Published on" day + real PDF. Falls back
    to year precision when a page has no published date.
    """
    yrs = set(years) if years is not None else (
        set(range(since.year, date.today().year + 1)) if since else None)
    for d_year, page, derived in sitemap_pages(fetcher, yrs):
        got = paper_meta(fetcher, page)
        if got is None:
            continue
        d, title, pdf = got
        prec = "day" if d else "year"
        d = d or d_year                      # d_year is date(y, 1, 1) -> year precision
        if since and d and d < since:
            continue
        yield DocRecord(
            bank_code="gb", doc_type=DocType.D1, title=title,
            document_url=pdf or derived, source_url=page, date=d, provenance="bank_site",
            mime_type="application/pdf", date_precision=prec, date_source="bank_site",
        )


def _soup(html: str):
    """Parse HTML robustly: lxml, falling back to the lenient stdlib parser
    (some BoE pages have malformed namespaces that crash lxml's bs4 builder)."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        try:
            return BeautifulSoup(html, "html.parser")
        except Exception:
            return None


def doc_pages(fetcher: Fetcher, sitemap_path: str, href_filter: str,
              years: Optional[Iterable[int]] = None) -> list[tuple[date, str]]:
    """Generic: (date, page_url) for content pages in any BoE sitemap section.

    `sitemap_path` e.g. "minutes"; `href_filter` is a regex the href must match
    (e.g. r"/minutes/\\d{4}/monetary-policy-committee"). Date is the year in the URL.
    """
    flt = re.compile(href_filter, re.I)
    soup = _soup(fetcher.get_text(f"{BOE}/sitemap/{sitemap_path}"))
    if soup is None:
        return []
    yrs = set(years) if years is not None else None
    out: list[tuple[date, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not flt.search(href):
            continue
        ym = _YEAR_IN_URL.search(href)
        if not ym:
            continue
        y = int(ym.group(1))
        if yrs is not None and y not in yrs:
            continue
        url = urljoin(BOE, href)
        if url in seen:
            continue
        seen.add(url)
        out.append((date(y, 1, 1), url))
    return out


def page_doc(fetcher: Fetcher, page_url: str) -> Optional[tuple[str, Optional[str]]]:
    """Fetch a BoE content page; return (title, pdf_url_or_None). When no PDF is
    linked the page itself is the artifact (HTML, to be rendered)."""
    try:
        html = fetcher.get_text(page_url)
    except Exception:
        return None
    soup = _soup(html)
    if soup is None:
        return None
    pdf = None
    for a in soup.find_all("a", href=True):
        if _ANY_PDF_RE.search(a["href"]):
            pdf = urljoin(BOE, a["href"])
            break
    if pdf is None:
        m = _ANY_PDF_RE.search(html)
        pdf = urljoin(BOE, m.group(0)) if m else None
    title = ""
    for m in soup.find_all("meta"):
        if (m.get("property") or "").lower() == "og:title":
            title = (m.get("content") or "").strip()
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    return (title or page_url.rsplit("/", 1)[-1], pdf)


def sitemap_pages(fetcher: Fetcher,
                  years: Optional[Iterable[int]] = None) -> list[tuple[date, str, str]]:
    """Return (date, page_url, derived_pdf_url) for staff working papers."""
    soup = _soup(fetcher.get_text(WP_SITEMAP))
    if soup is None:
        return []
    yrs = set(years) if years is not None else None
    out: list[tuple[date, str, str]] = []
    seen: set[tuple[int, str]] = set()
    for a in soup.find_all("a", href=True):
        m = _WP_RE.search(a["href"])
        if not m:
            continue
        y, slug = int(m.group(1)), m.group(2)
        if yrs is not None and y not in yrs:
            continue
        if (y, slug) in seen:
            continue
        seen.add((y, slug))
        page = urljoin(BOE, a["href"])
        derived = f"{BOE}/-/media/boe/files/working-paper/{y}/{slug}.pdf"
        out.append((date(y, 1, 1), page, derived))
    return out


# Back-compat: derived-URL-only listing (date, title, document_url).
def sitemap_papers(fetcher: Fetcher,
                   years: Optional[Iterable[int]] = None) -> list[tuple[date, str, str]]:
    out = []
    for d, page, derived in sitemap_pages(fetcher, years):
        slug = page.rstrip("/").rsplit("/", 1)[-1]
        out.append((d, slug.replace("-", " ").strip().capitalize(), derived))
    return out


def paper_pdf(fetcher: Fetcher, page_url: str) -> Optional[tuple[str, str]]:
    """Fetch a working-paper page and return (title, real_pdf_url), or None."""
    try:
        html = fetcher.get_text(page_url)
    except Exception:
        return None
    soup = _soup(html)
    if soup is None:
        return None
    pdf = None
    for a in soup.find_all("a", href=True):
        if _PDF_RE.search(a["href"]):
            pdf = a["href"]
            break
    if pdf is None:
        m = _PDF_RE.search(html)
        pdf = m.group(0) if m else None
    if pdf is None:
        return None
    title = ""
    for m in soup.find_all("meta"):
        if (m.get("property") or "").lower() == "og:title":
            title = (m.get("content") or "").strip()
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    return (title or page_url.rsplit("/", 1)[-1], urljoin(BOE, pdf))
