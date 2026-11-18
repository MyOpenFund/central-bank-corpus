"""Federal Reserve (Board of Governors) adapter -- worked example.

Adds the FOMC native listings on federalreserve.gov:
  A2  policy statements      ~8/yr
  A3  FOMC minutes           ~8/yr   files/fomcminutes<YYYYMMDD>.pdf
  F1  SEP projections        ~4/yr   files/fomcprojtabl<YYYYMMDD>.pdf

Selectors/URL templates follow the published federalreserve.gov layout and are
isolated in pure helpers (`parse_minutes_links`) for easy validation. Speeches
(C1) and FEDS/IFDP papers (D1) come from the base class automatically.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import DocRecord
from ..taxonomy import DocType
from .base import BankAdapter, register

FED = "https://www.federalreserve.gov"
CAL = FED + "/monetarypolicy/fomccalendars.htm"
HIST = FED + "/monetarypolicy/fomchistorical{year}.htm"

# FOMC minutes (A3). Modern: files/fomcminutes<date>.pdf ; historical (pre-2007):
# /fomc/minutes/<date>.htm (HTML only, rendered to PDF on save).
_MIN_RE = re.compile(
    r"(?:fomcminutes(\d{8})\.pdf|/fomc/minutes/(\d{8})\.htm"
    r"|/fomc/minutes/\d{4}/(\d{8})min\.htm)$", re.I)
# Modern SEP: files/fomcprojtabl<YYYYMMDD>.pdf (2021+) ; historical (2007-2020):
# files/FOMC<YYYYMMDD>SEPcompilation.pdf. One per meeting either way.
_SEP_RE = re.compile(r"(?:fomcprojtabl|FOMC)(\d{8})(?:SEPcompilation)?\.pdf$", re.I)
# FOMC policy statement (A2), HTML. Path-agnostic over the two conventions:
#   modern: /newsevents/pressreleases/monetary20150128a.htm
#   legacy: /newsevents/press/monetary/20080130a.htm
# `a.htm` only (excludes a1.htm implementation note, b.htm discount-rate).
# Modern/legacy: .../monetary<date>a.htm (a.htm only -> excludes a1 note, b discount).
# Historical: /boarddocs/press/{monetary|general}/<year>/<date>/ -> statement at default.htm
# ("general" pre-2002, "monetary" 2002-2005). On the FOMC pages these are all statements.
_STMT_RE = re.compile(
    r"monetary(?:/)?(\d{8})a\.htm$|boarddocs/press/(?:monetary|general)/\d{4}/(\d{8})", re.I)


def _date8(s: str) -> Optional[date]:
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def parse_minutes_links(html: str, base_url: str = FED) -> list[tuple[date, str]]:
    """Return (meeting_date, pdf_url) for FOMC minutes linked on a page."""
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[date, str]] = []
    for a in soup.find_all("a", href=True):
        m = _MIN_RE.search(a["href"])
        if m:
            d = _date8(m.group(1) or m.group(2) or m.group(3))
            if d:
                out.append((d, urljoin(base_url, a["href"])))
    return out


def parse_sep_links(html: str, base_url: str = FED) -> list[tuple[date, str]]:
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[date, str]] = []
    for a in soup.find_all("a", href=True):
        m = _SEP_RE.search(a["href"])
        if m:
            d = _date8(m.group(1))
            if d:
                out.append((d, urljoin(base_url, a["href"])))
    return out


def parse_statement_links(html: str, base_url: str = FED) -> list[tuple[date, str]]:
    """Return (meeting_date, html_url) for FOMC policy statements (A2)."""
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[date, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = _STMT_RE.search(a["href"])
        if not m:
            continue
        if m.group(1):                       # modern/legacy <date>a.htm
            d = _date8(m.group(1))
            url = urljoin(base_url, a["href"])
        else:                                # historical boarddocs (monetary|general) dir
            d = _date8(m.group(2))
            href = a["href"]
            url = urljoin(base_url, href if href.endswith(".htm")
                          else href.rstrip("/") + "/default.htm")
        if d and url not in seen:
            seen.add(url)
            out.append((d, url))
    return out


@register("us")
class FedAdapter(BankAdapter):
    # D1 = FEDS + IFDP working papers, native from federalreserve.gov (exact day
    # via each paper's landing page) rather than RePEc. Flip is safe post-migration
    # (native URLs registered in alt_urls -> zero re-download).
    native_types = (DocType.A2, DocType.A3, DocType.F1, DocType.D1)
    expected_per_year = {DocType.A2: 8, DocType.A3: 8, DocType.F1: 4}

    # parser, title label, mime ("" => HTML, rendered to PDF by Storage)
    _SPECS = {
        DocType.A2: (parse_statement_links, "FOMC statement", ""),
        DocType.A3: (parse_minutes_links, "FOMC minutes", "application/pdf"),
        DocType.F1: (parse_sep_links, "Summary of Economic Projections", "application/pdf"),
    }

    def _year_pages(self, since: Optional[date]) -> Iterator[tuple[int, str]]:
        cur = date.today().year
        start = since.year if since else 1994   # FOMC statements began ~1994
        # The calendars page covers ~the last 5 years + current; per-year
        # historical pages exist only for OLDER years (a year moves to
        # "historical" ~5 years on). Probing recent historical pages just 404s.
        yield (cur, CAL)
        for y in range(start, cur - 5):
            yield (y, HIST.format(year=y))

    def _discover_native(self, doc_type: DocType,
                         since: Optional[date]) -> Iterator[DocRecord]:
        if doc_type == DocType.D1:
            from ..sources.fed_wp import discover_fed_wp
            yield from discover_fed_wp(self.fetcher, since)
            return
        spec = self._SPECS.get(doc_type)
        if spec is None:
            return
        parser, label, mime = spec
        seen: set[str] = set()
        for _year, url in self._year_pages(since):
            html = self._fetch_text(url, context=f"{doc_type.code}-page")
            if html is None:
                continue
            for d, link in parser(html):
                if link in seen or (since and d < since):
                    continue
                seen.add(link)
                yield DocRecord(
                    bank_code="us", doc_type=doc_type,
                    title=f"{label} {d.isoformat()}",
                    pdf_url=link, source_url=url, date=d,
                    provenance="bank_site",
                    mime_type=mime,
                    # every parser reads the full YYYYMMDD of the document URL
                    date_precision="day",
                )
