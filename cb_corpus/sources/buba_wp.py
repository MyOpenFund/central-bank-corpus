"""Deutsche Bundesbank Discussion Papers (D1) from bundesbank.de.

The paginated listing ``/en/publications/research/discussion-papers?page=N``
links paper pages ``/discussion-papers/{slug}-{id}``. Each paper page carries the
title (og:title), the DP number ("No 14/2026"), the publication day, and an
**opaque blob PDF** ``/resource/blob/{id}/.../{YYYY-MM-DD}-dkp-{NN}-data.pdf``
that must be read from the page, never derived. The blob filename conveniently
embeds both the ISO date and the DP number.

Match key: the DP number ``(num, year)`` — parseable from the blob filename and
from the RePEc handle ``zbw:bubdps:{NN}{YYYY}``. Newer handles are bare global
EconStor ids with no DP number; those fall through to the title tier in
wp_migrate. (The manifest's de rows are EconStor copies, so URL/number never
match directly — number-from-handle + title carry the join.)
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import Fetcher
from ..models import DocRecord
from ..taxonomy import DocType

BUBA = "https://www.bundesbank.de"
LISTING = BUBA + "/en/publications/research/discussion-papers"
# The listing page renders only the first 8 papers; the rest are paged via this
# TYPO3 "bbksearch" result endpoint (0-based `pageNumString`), discovered from
# the page's pagination links. If the numeric content id (732408) ever changes,
# discovery returns nothing and fails loudly rather than silently truncating.
_PAGINATE = BUBA + "/action/en/732408/bbksearch?pageNumString={n}"
_PAPER_RE = re.compile(r"/en/publications/research/discussion-papers/[a-z0-9][a-z0-9\-]+-\d+")
_BLOB_RE = re.compile(r"/resource/blob/\d+/[a-f0-9]+/[A-F0-9]+/[^\"'\s]+-data\.pdf", re.I)
_BLOB_DATE_NUM = re.compile(r"/(\d{4})-(\d{2})-(\d{2})-dkp-?(\d+)", re.I)
_DATE_DMY = re.compile(r"(\d{1,2})\.(\d{1,2})\.((?:19|20)\d{2})")


def de_blob_key(url: str):
    """(num, year) from a blob PDF filename (…/YYYY-MM-DD-dkp-NN-data.pdf), else None."""
    m = _BLOB_DATE_NUM.search(url or "")
    return (int(m.group(4)), int(m.group(1))) if m else None


def de_handle_key(handle: str):
    """(num, year) from a zbw:bubdps handle ``{NN}{YYYY}``; None for the bare global
    EconStor ids used for recent papers (year out of range)."""
    m = re.search(r"bubdps[:/](\d{1,3})(\d{4})[a-z]?(?:\.|$|/)", handle or "", re.I)
    if not m:
        return None
    num, year = int(m.group(1)), int(m.group(2))
    return (num, year) if 1990 <= year <= 2035 else None


def parse_de_paper(html: str, base_url: str = BUBA
                   ) -> Optional[tuple[Optional[date], str, str]]:
    """(date, title, blob_pdf_url) from a Bundesbank discussion-paper page, else None.

    Date is taken from the blob filename's ISO prefix (unambiguous); falls back to
    the first DD.MM.YYYY on the page.
    """
    m = _BLOB_RE.search(html or "")
    if not m:
        return None
    pdf = urljoin(base_url, m.group(0))
    d = None
    mb = _BLOB_DATE_NUM.search(pdf)
    if mb:
        try:
            d = date(int(mb.group(1)), int(mb.group(2)), int(mb.group(3)))
        except ValueError:
            d = None
    if d is None:
        md = _DATE_DMY.search(html)
        if md:
            try:
                d = date(int(md.group(3)), int(md.group(2)), int(md.group(1)))
            except ValueError:
                d = None
    soup = BeautifulSoup(html, "lxml")
    title = ""
    for meta in soup.find_all("meta"):
        if (meta.get("property") or "").lower() == "og:title":
            title = (meta.get("content") or "").strip()
            break
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    return d, title, pdf


def parse_listing_page(html: str, base_url: str = BUBA
                       ) -> list[tuple[str, Optional[str], Optional[str]]]:
    """(title, blob_pdf_url|None, paper_page_url|None) per discussion paper on a
    bbksearch result page.

    Old papers link a direct ``…-dkp-NN-data.pdf`` blob (filename embeds the ISO
    date + number — no fetch needed); recent papers link a ``/discussion-papers/``
    slug page (the blob/day live there — one fetch). Items with neither are skipped.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    for item in soup.select(".resultlist__item"):
        blob = next((urljoin(base_url, a["href"]) for a in item.find_all("a", href=True)
                     if _BLOB_RE.search(a["href"]) and de_blob_key(a["href"])), None)
        page = next((urljoin(base_url, a["href"]) for a in item.find_all("a", href=True)
                     if _PAPER_RE.search(a["href"])), None)
        if not blob and not page:
            continue
        t = item.select_one(".teasable__title")
        out.append((t.get_text(" ", strip=True) if t else "", blob, page))
    return out


def discover_buba_wp(fetcher: Fetcher, since: Optional[date] = None,
                     max_pages: int = 300) -> Iterator[DocRecord]:
    """Yield Bundesbank Discussion Papers (D1) with the exact day, from the paged
    bbksearch result list. Old papers come straight off the listing (direct blob);
    recent papers cost one paper-page fetch. Dedups by DP number; with `since`,
    stops once a whole page is older than the cutoff (the list is newest-first)."""
    try:
        first = fetcher.get_text(_PAGINATE.format(n=0))
    except Exception:
        return
    nums = [int(x) for x in re.findall(r"pageNumString=(\d+)", first)]
    last = min(max(nums) if nums else 0, max_pages - 1)
    seen: set = set()
    for n in range(last + 1):
        html = first if n == 0 else None
        if html is None:
            try:
                html = fetcher.get_text(_PAGINATE.format(n=n))
            except Exception:
                continue
        rows = parse_listing_page(html)
        page_has_fresh = False
        for title, blob, page in rows:
            d = None
            pdf = blob
            if blob:                            # old paper: date from the blob filename
                mb = _BLOB_DATE_NUM.search(blob)
                if mb:
                    try:
                        d = date(int(mb.group(1)), int(mb.group(2)), int(mb.group(3)))
                    except ValueError:
                        d = None
            elif page:                          # recent paper: read its page
                page_html = _safe(fetcher, page)
                got = parse_de_paper(page_html, page) if page_html else None
                if got is None:
                    continue
                d, t2, pdf = got
                title = t2 or title
            if not pdf:
                continue
            if since and d and d < since:
                continue
            page_has_fresh = True
            key = de_blob_key(pdf)
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            yield DocRecord(
                bank_code="de", doc_type=DocType.D1,
                title=title or pdf.rsplit("/", 1)[-1], pdf_url=pdf, source_url=page or LISTING,
                date=d, provenance="bank_site", mime_type="application/pdf",
                date_precision="day" if d else None, date_source="bank_site",
            )
        if since and rows and not page_has_fresh:   # newest-first -> rest is older
            break


def _safe(fetcher: Fetcher, url: str) -> Optional[str]:
    try:
        return fetcher.get_text(url)
    except Exception:
        return None
