"""Reserve Bank of Australia adapter — decisions (A1), board minutes (A3), SMP (E1).

All three come from the RBA's own per-year listing pages (NOT the sitemap.xml,
which was a frozen 2017 snapshot that mis-dated every page to its lastmod and
over-captured SMP sub-pages):
  A1  rate decisions  /monetary-policy/int-rate-decisions/<year>/   (date in LINK TEXT)
  A3  board minutes   /monetary-policy/rba-board-minutes/<year>/    (date in URL: YYYY-MM-DD)
  E1  Statement on MP  /publications/smp/                           (quarterly: feb/may/aug/nov)
Speeches (C1) and working papers (D1) come from the base class.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import DocRecord
from ..taxonomy import DocType
from .base import BankAdapter, register

RBA = "https://www.rba.gov.au"
RATE_INDEX = RBA + "/monetary-policy/int-rate-decisions/{year}/"
MINUTES_INDEX = RBA + "/monetary-policy/rba-board-minutes/{year}/"
SMP_INDEX = RBA + "/publications/smp/"

_DECISION_RE = re.compile(r"/media-releases/\d{4}/mr-\d{2}-\d+\.html$", re.I)
# Two URL conventions: modern `YYYY-MM-DD.html` (2015+) and legacy `DDMMYYYY.html`
# (e.g. 05102010 = 5 Oct 2010, the pre-2015 board minutes).
_MINUTES_RE = re.compile(
    r"/rba-board-minutes/\d{4}/(?:(\d{4})-(\d{2})-(\d{2})|(\d{2})(\d{2})(\d{4}))\.html$", re.I)
_SMP_MONTH = {"feb": 2, "may": 5, "aug": 8, "nov": 11}
_SMP_RE = re.compile(r"/publications/smp/(\d{4})/(feb|may|aug|nov)/?$", re.I)


def parse_rba_decisions(html: str, base_url: str = RBA) -> list[tuple[date, str]]:
    """(decision_date, url) for RBA rate decisions; date is in the LINK TEXT."""
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[date, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        if not _DECISION_RE.search(a["href"]):
            continue
        try:
            d = datetime.strptime(a.get_text(" ", strip=True), "%d %B %Y").date()
        except ValueError:
            continue
        url = urljoin(base_url, a["href"])
        if url not in seen:
            seen.add(url)
            out.append((d, url))
    return out


def parse_rba_minutes(html: str, base_url: str = RBA) -> list[tuple[date, str]]:
    """(meeting_date, url) for RBA board minutes; date is in the URL (YYYY-MM-DD)."""
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[date, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = _MINUTES_RE.search(a["href"].split("?")[0])
        if not m:
            continue
        if m.group(1):                       # modern YYYY-MM-DD
            y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:                                # legacy DDMMYYYY
            dd, mo, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
        try:
            d = date(y, mo, dd)
        except ValueError:
            continue
        url = urljoin(base_url, a["href"])
        if url not in seen:
            seen.add(url)
            out.append((d, url))
    return out


def parse_rba_smp(html: str, base_url: str = RBA) -> list[tuple[date, str]]:
    """(issue_date, url) for RBA Statement on Monetary Policy issues (quarterly).

    The URL names the issue month only: `issue_date` is the FIRST DAY of that
    month and the record is labelled month precision (see RBAAdapter._rec).

    Only the issue overview `/publications/smp/<year>/<feb|may|aug|nov>/` is kept
    — not the per-chapter sub-pages (boxes.html, …) that bloated the old capture.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[date, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = _SMP_RE.search(a["href"].split("?")[0])
        if not m:
            continue
        d = date(int(m.group(1)), _SMP_MONTH[m.group(2).lower()], 1)
        url = urljoin(base_url, a["href"])
        if url not in seen:
            seen.add(url)
            out.append((d, url))
    return out


@register("au")
class RBAAdapter(BankAdapter):
    native_types = (DocType.A1, DocType.A3, DocType.E1)
    # Eras measured on the committed manifests (integrity wave, 2026-11-18):
    # before 2008 a statement was issued only when the rate moved (no
    # expectation: years before the first era are unknown); eleven Board
    # meetings a year 2008-2023, eight since 2024. Minutes (A3) start in late
    # 2006 (first full year 2007). The Statement on Monetary Policy (E1) is
    # quarterly throughout.
    expected_per_year = {
        DocType.A1: [(2008, 11), (2024, 8)],
        DocType.A3: [(2007, 11), (2024, 8)],
        DocType.E1: 4,
    }

    def _rec(self, dt: DocType, label: str, d: date, url: str, src: str,
             precision: str = "day") -> DocRecord:
        """`precision`: decisions (link text) and minutes (URL) name the day;
        a Statement issue is a month (`date` = its first day)."""
        return DocRecord(
            bank_code="au", doc_type=dt, title=f"{label} {d.isoformat()}",
            pdf_url=url, source_url=src, date=d,
            provenance="bank_site", mime_type="text/html",  # rendered to PDF by Storage
            date_precision=precision,
        )

    def _walk_years(self, tmpl, parse_fn, dt, label, since, start) -> Iterator[DocRecord]:
        cur = date.today().year
        seen: set[str] = set()
        for y in range(since.year if since else start, cur + 1):
            src = tmpl.format(year=y)
            html = self._fetch_text(src, context=f"{dt.code}-year")
            if html is None:
                continue
            for d, link in parse_fn(html):
                if link in seen or (since and d < since):
                    continue
                seen.add(link)
                yield self._rec(dt, label, d, link, src)

    def _discover_native(self, doc_type: DocType,
                         since: Optional[date]) -> Iterator[DocRecord]:
        if doc_type == DocType.A1:
            yield from self._walk_years(RATE_INDEX, parse_rba_decisions, DocType.A1,
                                        "RBA monetary policy decision", since, 1997)
        elif doc_type == DocType.A3:
            yield from self._walk_years(MINUTES_INDEX, parse_rba_minutes, DocType.A3,
                                        "RBA board minutes", since, 2006)
        elif doc_type == DocType.E1:
            html = self._fetch_text(SMP_INDEX, context="E1-index")
            if html is None:
                return
            seen: set[str] = set()
            for d, link in parse_rba_smp(html):
                if link in seen or (since and d < since):
                    continue
                seen.add(link)
                yield self._rec(DocType.E1, "RBA Statement on Monetary Policy",
                                d, link, SMP_INDEX, precision="month")
