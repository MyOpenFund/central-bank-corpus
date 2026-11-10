"""Sveriges Riksbank Working Paper Series (D1) from riksbank.se.

The paginated, server-rendered listing
``/en-gb/press-and-published/publications/working-paper-series/?&page=N`` lists
every paper newest-first, 10 per page: an ``<a>`` per paper links the PDF
directly under ``/globalassets/media/rapporter/working-papers/<year>/...`` and
carries, INSIDE the anchor, a ``DD/MM/YYYY`` publication-day label and a title
of the form ``"No. <NNN> <Title>"``. No per-paper fetch is needed — number, day
and PDF URL are all on the listing. An out-of-range page returns 200 with an
empty listing (no PDF anchors), which is how discovery finds the end.

Native coverage is bounded to ~2016+ by design (the site's own listing); pre-2016
rows remain RePEc/Wayback-sourced. The nightly/Sunday `cb_corpus repec` catalog
job continues to provide full-history dual-source coverage for se, identically to
the other WP-v3 banks.

PDF filenames are NOT uniform across the site's history — modern papers use
``no.-<NNN>-<slug>.pdf`` (sometimes ``no-<NNN>-`` or ``no.<NNN>-``, and
sometimes with NO ``.pdf`` extension at all — the file is still served as
``application/pdf``; verified live, e.g. WP 317 and WP 355), 2016-2017 papers
use ``wp<NNN>.pdf`` / ``wp_<NNN>.pdf`` / ``rap_wp<NNN>_<date>.pdf``. URLs are
scraped verbatim, never derived or reconstructed. ``se_url_number`` reads the
WP number from any of these forms; it is the join key used by ``wp_migrate``
(paired with ``se_handle_number`` for the RePEc handle/IDEAS-URL side) to
register the current native URL in a legacy manifest row's ``alt_urls`` when
the two point at the same paper under different URLs — the WP-v3
zero-redownload guard.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterator, Optional
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

from ..http import Fetcher
from ..models import DocRecord
from ..taxonomy import DocType

RIKSBANK = "https://www.riksbank.se"
LISTING = RIKSBANK + "/en-gb/press-and-published/publications/working-paper-series/"
_PAGE = LISTING + "?&page={n}"

_WP_HREF_RE = re.compile(r"/globalassets/media/rapporter/working-papers/", re.I)
_NUM_TITLE_RE = re.compile(r"^No\.\s*(\d+)\s+(.+)$")
_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
# URL-based number extraction (for the join key, which must also read historical
# manifest URLs — see module docstring for the filename forms observed).
_URL_NO_RE = re.compile(r"no\.?-?0*(\d+)(?=[-.]|$)", re.I)
_URL_WP_RE = re.compile(r"(?:rap_)?wp_?0*(\d+)", re.I)
_HANDLE_RE = re.compile(r"hhs[:/]rbnkwp[:/]0*(\d+)", re.I)


def se_url_number(url: str) -> Optional[int]:
    """WP number from a Riksbank PDF URL (any filename era), else None."""
    seg = unquote(url or "").rsplit("/", 1)[-1].lower()
    m = _URL_NO_RE.search(seg)
    if m:
        return int(m.group(1))
    m = _URL_WP_RE.search(seg)
    if m:
        return int(m.group(1))
    return None


def se_handle_number(handle_or_url: str) -> Optional[int]:
    """WP number from a RePEc handle (``RePEc:hhs:rbnkwp:0463``) or an IDEAS
    paper URL (``.../p/hhs/rbnkwp/0463.html``), else None."""
    m = _HANDLE_RE.search(handle_or_url or "")
    return int(m.group(1)) if m else None


def _parse_label_date(s: str) -> Optional[date]:
    m = _DATE_RE.match((s or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _split_number_title(raw: str) -> tuple[Optional[int], str]:
    m = _NUM_TITLE_RE.match(raw or "")
    if not m:
        return None, raw
    return int(m.group(1)), m.group(2).strip()


def parse_wp_listing(html: str, base_url: str = RIKSBANK
                     ) -> list[tuple[Optional[int], Optional[date], str, str]]:
    """(number, date, title, pdf_url) per paper on one listing page.

    Content-based: any anchor linking a ``working-papers`` asset is a paper,
    regardless of file extension (some historical rows link the PDF with none —
    see module docstring). The day and "No. NNN Title" text live inside the
    anchor as nested spans.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        if not _WP_HREF_RE.search(a["href"]):
            continue
        pdf_url = urljoin(base_url, a["href"])
        if pdf_url in seen:
            continue
        seen.add(pdf_url)
        title_el = a.select_one(".header--file__title")
        raw_title = title_el.get_text(" ", strip=True) if title_el else ""
        label_el = a.select_one(".label")
        d = _parse_label_date(label_el.get_text(strip=True) if label_el else "")
        num, title = _split_number_title(raw_title)
        if not title:
            title = raw_title or pdf_url.rsplit("/", 1)[-1]
        out.append((num, d, title, pdf_url))
    return out


def discover_riksbank_wp(fetcher: Fetcher, since: Optional[date] = None,
                         max_pages: int = 250) -> Iterator[DocRecord]:
    """Yield Riksbank Working Papers (D1), newest-first, from the paginated
    listing. With `since`, stops once a whole page is older than the cutoff
    (list is newest-first); otherwise walks until a page comes back empty
    (an out-of-range page, or a fetch failure)."""
    for n in range(1, max_pages + 1):
        html = _safe(fetcher, _PAGE.format(n=n))
        if html is None:
            break
        rows = parse_wp_listing(html)
        if not rows:
            break
        page_has_fresh = False
        for _num, d, title, pdf_url in rows:
            if since and d and d < since:
                continue
            page_has_fresh = True
            yield DocRecord(
                bank_code="se", doc_type=DocType.D1,
                title=title, pdf_url=pdf_url, source_url=LISTING,
                date=d, provenance="bank_site", mime_type="application/pdf",
                date_precision="day" if d else None, date_source="bank_site",
            )
        if since and not page_has_fresh:
            break


def _safe(fetcher: Fetcher, url: str) -> Optional[str]:
    try:
        return fetcher.get_text(url)
    except Exception:
        return None
