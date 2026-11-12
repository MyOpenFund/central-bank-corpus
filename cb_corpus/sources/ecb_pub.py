"""ECB publications via the bank's per-section, per-year STATIC index includes.

Primary source: `/press/<section>/date/<year>/html/index_include.en.html` — a
static HTML file that lists every document of that section/year with its real
(hashed) URL, no JavaScript. This is the canonical ECB listing (cleaner than
scraping the JS-rendered index pages or guessing hashed PDF URLs).

When a section does not serve those includes, the caller falls back to the older
methods (Wayback CDX enumeration in `wayback.py`, constructed URLs, etc.), which
are kept on purpose as a resilience layer.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from bs4 import BeautifulSoup

from ..http import Fetcher

ECB = "https://www.ecb.europa.eu"


def section_include_docs(fetcher: Fetcher, section: str, year: int,
                         exts: tuple = (".en.pdf", ".en.html")) -> Optional[list[str]]:
    """Return the document URLs listed in a section's per-year include, or None
    if that include is not served (signals the caller to fall back)."""
    url = f"{ECB}/press/{section}/date/{year}/html/index_include.en.html"
    try:
        html = fetcher.get_text(url)
    except Exception:
        return None
    out: list[str] = []
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        h = a["href"]
        if h.lower().endswith(exts):
            out.append(h if h.startswith("http") else ECB + h)
    return out


def date_from_url(u: str, fmt: str = "auto") -> Optional[date]:
    """Date from an ECB document filename.

    `fmt`: "yyyymm" (e.g. fsr/projections), "yymmdd" (e.g. interviews/press conf),
    "yyyymmdd", or "auto" (8-digit, else 6-digit read as YYYYMM if the year is
    plausible, else YYMMDD). The section's known format avoids the YYYYMM/YYMMDD
    ambiguity (e.g. "200612" = 2006-12 vs 2020-06-12). See
    date_and_precision_from_url for how precise the returned date is."""
    return date_and_precision_from_url(u, fmt)[0]


def date_and_precision_from_url(u: str, fmt: str = "auto"
                                ) -> tuple[Optional[date], Optional[str]]:
    """(date, date_precision) from an ECB document filename -- same parsing
    as date_from_url, plus the precision the matched format actually carries:
    "day" for YYYYMMDD/YYMMDD, "month" for YYYYMM (day set to 1), "year" for a
    bare YYYY (Jan 1). (None, None) when nothing parses."""
    fn = u.split("/")[-1]
    m8 = re.search(r"(\d{4})(\d{2})(\d{2})", fn)
    m6 = re.search(r"(\d{6})(?!\d)", fn)
    try:
        if fmt == "yyyymmdd" and m8:
            return date(int(m8[1]), int(m8[2]), int(m8[3])), "day"
        if fmt == "yymmdd" and m6:
            s = m6[1]
            return date(2000 + int(s[:2]), int(s[2:4]), int(s[4:6])), "day"
        if fmt == "yyyymm" and m6:
            s = m6[1]
            return date(int(s[:4]), int(s[4:6]), 1), "month"
        if fmt == "yyyy":
            m4 = re.search(r"(19|20)(\d{2})", fn)
            if m4:
                return date(int(m4.group(0)), 1, 1), "year"
        if fmt == "auto":
            if m8:
                try:
                    return date(int(m8[1]), int(m8[2]), int(m8[3])), "day"
                except ValueError:
                    pass
            if m6:
                s = m6[1]
                if 1997 <= int(s[:4]) <= date.today().year and 1 <= int(s[4:6]) <= 12:
                    return date(int(s[:4]), int(s[4:6]), 1), "month"
                return date(2000 + int(s[:2]), int(s[2:4]), int(s[4:6])), "day"
            m4 = re.search(r"(19|20)(\d{2})", fn)          # year-only (e.g. annual report)
            if m4:
                return date(int(m4.group(0)), 1, 1), "year"
    except (ValueError, IndexError):
        return None, None
    return None, None
