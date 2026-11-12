"""Bank of Japan Working Paper Series (D1) from boj.or.jp.

Listing: ``/en/research/wps_rev/wps_{YYYY}/index.htm`` — an HTML table with the
exact publication day printed inline, so no per-paper fetch is needed. The column
layout varies by era (5 cols recent: No. | Date | Author | Title | Full Text;
4 cols ~2016; 3 cols ~2002 with no "No." column), so rows are parsed by
*content* — each row's ``data/*.pdf`` link plus whichever cell holds a date —
rather than by column position.

PDF: ``/en/research/wps_rev/wps_{YYYY}/data/{code}.pdf`` where ``code`` is
``wp{YY}e{NN}`` (modern) or ``cwp{YY}e{NN}`` / ``iwp{YY}e{NN}`` (legacy). The
match key is ``(yy, lang, num)``, also parseable from the RePEc handle
(``boj:bojwps:wp25e09`` / ``…:02-e-2r``).
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

BOJ = "https://www.boj.or.jp"
_WPS = BOJ + "/en/research/wps_rev/wps_{year}/index.htm"
_DATA_PDF_RE = re.compile(r"/wps_\d{4}/data/[^\"'\s]+\.pdf$", re.I)
# paper code -> (yy, lang, num): wp25e13 | cwp02e08 | iwp02e02 | 25-E-13 | 02-e-2r
_CODE_RE = re.compile(r"[a-z]*?(\d{2})[-_]?([ej])[-_]?(\d+)", re.I)
_MON = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def boj_code(s: str):
    """(yy, lang, num) join key from a code/URL/handle, else None.

    Reads the last path segment so a year in the URL path can't be mistaken for
    the code (e.g. .../wps_2025/data/wp25e13.pdf -> wp25e13 -> (25, 'e', 13)).
    """
    # Also split on ":" so a RePEc "series:pid" / handle form (what repec-check
    # passes, e.g. "bojwps:wp25e13") reads the paper id, not "bojwps:...".
    s = (s or "").split("?", 1)[0].split("#", 1)[0]
    seg = re.split(r"[/:]", s.rstrip("/"))[-1].split(".")[0]
    m = _CODE_RE.fullmatch(seg) or _CODE_RE.match(seg)
    if not m:
        return None
    return int(m.group(1)), m.group(2).lower(), int(m.group(3))


def _boj_date(s: str) -> tuple[Optional[date], str]:
    """Parse a BoJ date cell. 'Nov. 13, 2025' -> (date, 'day');
    'Apr. 2002' / 'April 2002' -> (date, 'month'); unparseable -> (None, 'month')."""
    s = (s or "").replace("\xa0", " ").strip()
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*((?:19|20)\d{2})", s)
    if m and m.group(1)[:3].lower() in _MON:
        try:
            return date(int(m.group(3)), _MON[m.group(1)[:3].lower()], int(m.group(2))), "day"
        except ValueError:
            pass
    m = re.search(r"([A-Za-z]{3,9})\.?\s+((?:19|20)\d{2})", s)
    if m and m.group(1)[:3].lower() in _MON:
        try:
            return date(int(m.group(2)), _MON[m.group(1)[:3].lower()], 1), "month"
        except ValueError:
            pass
    return None, "month"


def parse_wp_table(html: str, base_url: str
                   ) -> list[tuple[Optional[date], str, str, str]]:
    """From a BoJ WP year page, return (date, precision, title, pdf_url) per paper.

    Content-based: any table row carrying a ``data/*.pdf`` link is a paper; the
    date is whichever cell parses as one; the title is the paper-page link text
    (falling back to the code).
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    seen: set[str] = set()
    for tr in soup.find_all("tr"):
        a_pdf = next((a for a in tr.find_all("a", href=True)
                      if _DATA_PDF_RE.search(a["href"])), None)
        if a_pdf is None:
            continue
        pdf_url = urljoin(base_url, a_pdf["href"])
        if pdf_url in seen:
            continue
        seen.add(pdf_url)
        d = prec = None
        for td in tr.find_all("td"):
            d, prec = _boj_date(td.get_text(" ", strip=True))
            if d is not None:
                break
        # title: the .htm paper-page link (not the PDF), else the code
        title = ""
        for a in tr.find_all("a", href=True):
            if a["href"].lower().endswith(".htm") and a.get_text(strip=True):
                title = a.get_text(" ", strip=True)
                break
        if not title:
            title = pdf_url.rsplit("/", 1)[-1].split(".")[0]
        # No parseable date -> no precision (None), never a fake "month".
        out.append((d, (prec or "month") if d else None, title, pdf_url))
    return out


def discover_boj_wp(fetcher: Fetcher, since: Optional[date] = None,
                    years: Optional[set] = None) -> Iterator[DocRecord]:
    """Yield BoJ Working Papers (D1). Walks year pages newest-first; a year whose
    page is missing/empty is skipped. `years` restricts to those calendar years."""
    start = since.year if since else 1995
    for y in range(date.today().year, start - 1, -1):
        if years is not None and y not in years:
            continue
        url = _WPS.format(year=y)
        try:
            html = fetcher.get_text(url)
        except Exception:
            continue
        for d, prec, title, pdf_url in parse_wp_table(html, url):
            if since and d and d < since:
                continue
            yield DocRecord(
                bank_code="jp", doc_type=DocType.D1, title=title,
                pdf_url=pdf_url, source_url=url, date=d, provenance="bank_site",
                mime_type="application/pdf",
                date_precision=prec, date_source="bank_site",
            )
