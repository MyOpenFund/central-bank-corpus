"""Federal Reserve FEDS + IFDP working papers (D1) from federalreserve.gov.

Listing: ``/econres/{feds,ifdp}/all-years.htm`` links per-year pages
``/econres/{feds,ifdp}/{YYYY}.htm``. Each entry carries a badge
("FEDS 2025-110"), a ``<time datetime="December 2025">`` (MONTH only) and a
slug landing-page link. The exact day and the canonical PDF live on the landing
page:

    <meta name="citation_publication_date" content="MM-DD-YYYY">
    /econres/{feds,ifdp}/files/{YYYYNNN}pap.pdf   (modern)
    /pubs/{feds,ifdp}/{YYYY}/.../...pap.(pdf|ps)  (legacy)

So one landing fetch per paper is needed for the day (WP v3 decision,
2026-06-12: day precision everywhere, backfill included). The landing date is
the CURRENT version's date — for a revised paper it is the revision date,
which can fall in a later month/year than the original. The listing month is
therefore authoritative: we keep the landing DAY
only when its (year, month) equals the listing's; otherwise the paper stays at
month precision (the listing month).

Match key against RePEc / existing manifest rows:
- FEDS: ``(feds, year, seq)`` — seq resets per year.
- IFDP: ``(ifdp, seq)`` — seq is a single global series since 1971, so the year
  is dropped (lets handle-only / FRASER rows that lack a year still match).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import Fetcher
from ..models import DocRecord
from ..taxonomy import DocType

FED = "https://www.federalreserve.gov"
SERIES = {
    "feds": FED + "/econres/feds/all-years.htm",
    "ifdp": FED + "/econres/ifdp/all-years.htm",
}
_YEAR_RE = {s: re.compile(rf"/econres/{s}/(\d{{4}})\.htm") for s in SERIES}

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}

# PDF/number URL shapes -> (series, year, seq). A revised paper carries an `r<N>`
# infix (e.g. files/2025101r1pap.pdf, files/ifdp1429r2.pdf) — the number is the
# same, so the revision marker is allowed and ignored:
#   FEDS modern: /econres/feds/files/{YYYY}{NNN}[r{N}]pap.pdf
#   IFDP modern: /econres/ifdp/files/ifdp{seq}[r{N}].pdf   (seq is global, no year)
#   legacy:      /pubs/{feds,ifdp}/{YYYY}/.../...(pap|abs).(pdf|ps|html)
# `econres/feds/files/…` (modern) and the 2015-16-era `econresdata/feds/{year}/files/…`
# are the same scheme under two path roots — accept both.
_FILES_RE = re.compile(r"/econres(?:data)?/feds/(?:\d{4}/)?files/(\d{4})(\d+)(?:r\d+)?pap\.(?:pdf|ps)", re.I)
_FILES_IFDP_RE = re.compile(r"/econres(?:data)?/ifdp/(?:\d{4}/)?files/ifdp(\d+)(?:r\d+)?\.(?:pdf|ps)", re.I)
_PUBS_RE = re.compile(r"/pubs/(feds|ifdp)/(\d{4})/[^/]+/([a-z]*)(\d+)(?:pap|abs)?\.", re.I)
# A Fed working-paper PDF/PS link on a landing page (modern files/, 2015-16
# econresdata/{year}/files/, or legacy pubs/).
_PDF_HREF = re.compile(
    r"/(?:econres(?:data)?/(?:feds|ifdp)/(?:\d{4}/)?files|pubs/(?:feds|ifdp)/\d{4}/[^/\"']+)"
    r"/[^\"'\s]+\.(?:pdf|ps)", re.I)
# RePEc handle or IDEAS path: fip:fedgfe:2022-82 | /fip/fedgfe/2022-82.html |
# fedgfe:1997-11 | fedgfe:95-24 | fedgif:694 | fedgfe:103343 (bare global id).
_HANDLE_RE = re.compile(r"fedg(fe|if)[:/](\d{2,4})(?:-(\d+))?", re.I)


def _fed_key(series: str, year: Optional[int], seq: int):
    """Join key. IFDP seq is globally unique -> drop the year so handle/FRASER
    rows (which lack a year) still match; FEDS seq resets yearly -> keep year."""
    return ("ifdp", seq) if series == "ifdp" else ("feds", year, seq)


def _month_year(s: str) -> Optional[date]:
    """'December 2025' -> date(2025, 12, 1); None if unparseable."""
    parts = (s or "").strip().lower().split()
    if len(parts) == 2 and parts[0] in _MONTHS and parts[1].isdigit():
        try:
            return date(int(parts[1]), _MONTHS[parts[0]], 1)
        except ValueError:
            return None
    return None


def _mdy(s: str) -> Optional[date]:
    """'MM-DD-YYYY' -> date; None if unparseable."""
    try:
        return datetime.strptime((s or "").strip(), "%m-%d-%Y").date()
    except ValueError:
        return None


def parse_year_links(html: str, series: str, base_url: str = FED
                     ) -> list[tuple[str, int, int, Optional[date], str, str]]:
    """From a FEDS/IFDP year page, return per paper:
    (series, year, seq, listing_month_date, landing_url, title)."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for badge in soup.select(f"span.badge--{series}"):
        m = re.search(r"(\d{4})-(\d{1,4})", badge.get_text(" ", strip=True))
        cont = badge.parent
        if not m or cont is None:
            continue
        year, seq = int(m.group(1)), int(m.group(2))
        t = cont.find("time")
        mdate = _month_year(t.get("datetime", "") if t else "")
        h = cont.find("h5")
        a = h.find("a", href=True) if h else None
        if not a:
            continue
        out.append((series, year, seq, mdate,
                    urljoin(base_url, a["href"]), a.get_text(" ", strip=True)))
    return out


def parse_landing(html: str, base_url: str = FED) -> tuple[Optional[date], Optional[str]]:
    """(citation_publication_date, pdf_url) from a paper's landing page.

    Prefers a .pdf over a legacy .ps. The PDF URL is read from the page (modern
    files/ or legacy pubs/), never derived.
    """
    soup = BeautifulSoup(html, "lxml")
    cpd = None
    for meta in soup.find_all("meta"):
        if (meta.get("name") or "").lower() == "citation_publication_date":
            cpd = _mdy(meta.get("content", ""))
            break
    pdf = ps = None
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if not _PDF_HREF.search(h):
            continue
        if h.lower().endswith(".pdf"):
            pdf = pdf or h
        else:
            ps = ps or h
    chosen = pdf or ps
    return cpd, (urljoin(base_url, chosen) if chosen else None)


def fed_key_from_url(url: str):
    """(series, year/None, seq) join key from a FEDS/IFDP PDF or abstract URL."""
    u = url or ""
    m = _FILES_RE.search(u)                         # FEDS modern: filesYYYYNNNpap
    if m:
        return _fed_key("feds", int(m.group(1)), int(m.group(2)))
    m = _FILES_IFDP_RE.search(u)                    # IFDP modern: files/ifdp{seq}
    if m:
        return _fed_key("ifdp", None, int(m.group(1)))
    m = _PUBS_RE.search(u)
    if m:
        series, pyear, num = m.group(1).lower(), int(m.group(2)), m.group(4)
        if series == "feds" and len(num) >= 5:   # filename is YYYYNN
            return _fed_key("feds", int(num[:4]), int(num[4:]))
        return _fed_key(series, pyear, int(num))  # ifdpNNN (seq) / short feds num
    return None


def fed_key_from_handle(handle: str):
    """(series, year/None, seq) from a RePEc handle (fip:fedgfe / fip:fedgif)."""
    m = _HANDLE_RE.search(handle or "")
    if not m:
        return None
    series = "feds" if m.group(1).lower() == "fe" else "ifdp"
    a, b = m.group(2), m.group(3)
    if b:                                          # YYYY-NN form
        year = int(a)
        year = (2000 + year if year < 50 else 1900 + year) if year < 100 else year
        return _fed_key(series, year, int(b))
    if series == "ifdp":                           # bare global seq, e.g. 694
        return _fed_key("ifdp", None, int(a))
    return None                                    # bare FEDS global id (103343) -> use the URL


def discover_fed_wp(fetcher: Fetcher, since: Optional[date] = None,
                    years: Optional[set] = None) -> Iterator[DocRecord]:
    """Yield FEDS + IFDP working papers (D1). One landing fetch per paper for the
    exact day; month precision when the landing date is a revision outside the
    listing month (or day is unknown). `years` restricts to those calendar years
    (for validation); `since` skips older years/papers (cheap incremental runs).
    """
    for series, allyears in SERIES.items():
        idx = fetcher.get_text(allyears)
        yrs = sorted({int(y) for y in _YEAR_RE[series].findall(idx)}, reverse=True)
        for y in yrs:
            if years is not None and y not in years:
                continue
            if since and y < since.year:
                continue
            page = fetcher.get_text(f"{FED}/econres/{series}/{y}.htm")
            for _ser, _yr, _seq, mdate, landing, title in parse_year_links(page, series):
                try:
                    land = fetcher.get_text(landing)
                except Exception:                  # one dead landing never aborts the crawl
                    continue
                cpd, pdf = parse_landing(land)
                if not pdf:
                    continue
                if cpd and mdate and (cpd.year, cpd.month) == (mdate.year, mdate.month) and cpd.day != 1:
                    d, prec = cpd, "day"           # landing day confirmed within the listing month
                elif mdate:
                    d, prec = mdate, "month"       # revision / old / day-unknown -> listing month
                elif cpd:
                    d, prec = cpd, ("day" if cpd.day != 1 else "month")
                else:
                    d, prec = None, None     # no date -> no precision
                if since and d and d < since:
                    continue
                yield DocRecord(
                    bank_code="us", doc_type=DocType.D1, title=title,
                    pdf_url=pdf, source_url=landing, date=d, provenance="bank_site",
                    mime_type="application/pdf" if pdf.lower().endswith(".pdf") else "",
                    date_precision=prec, date_source="bank_site",
                )
