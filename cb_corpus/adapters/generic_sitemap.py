"""Sitemap-driven generic adapter.

For banks that publish a usable sitemap.xml, we walk it once and apply a regex
per doc_type to pick the relevant URLs. No per-bank class needed — the adapter
is configured declaratively from `banks_sources.toml`.

Sitemap formats supported:
  - <urlset> with <url><loc>...</loc><lastmod>...</lastmod></url>
  - <sitemapindex> with <sitemap><loc>...</loc></sitemap> (recurses one level)
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from ..dates import STATUS_DATED, infer_policy_date, parse_url_date
from ..models import DocRecord
from ..taxonomy import DocType
from .base import BankAdapter

_DATE_IN_URL_RE = re.compile(r"(20\d{2})[-_/]?(0[1-9]|1[0-2])[-_/]?(0[1-9]|[12]\d|3[01])")
#: A year and a month in a URL, separated, with no day after them
#: ("/mpr-2024-07/", "/2023-04/report.pdf"): a month-precision date. The
#: separator is mandatory and a following digit refuses the match, so a
#: reference number ("report-2023-045") is not read as a month.
MONTH_IN_URL = r"(?<!\d)(?P<y>20\d{2})[-_/](?P<m>0[1-9]|1[0-2])(?![-_/]?\d)"


def _parse_iso(s: str) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_sitemap(xml: str) -> tuple[list[tuple[str, Optional[date]]], list[str]]:
    """Return (urls_with_lastmod, child_sitemaps) from one sitemap XML."""
    soup = BeautifulSoup(xml, "xml")
    urls: list[tuple[str, Optional[date]]] = []
    children: list[str] = []
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if loc is None:
            continue
        lm = u.find("lastmod")
        urls.append((loc.get_text(strip=True),
                     _parse_iso(lm.get_text(strip=True)) if lm else None))
    for sm in soup.find_all("sitemap"):
        loc = sm.find("loc")
        if loc is None:
            continue
        children.append(loc.get_text(strip=True))
    return urls, children


def _date_from_url(url: str) -> Optional[date]:
    m = _DATE_IN_URL_RE.search(url)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


class GenericSitemapAdapter(BankAdapter):
    """Adapter built from declarative {DocType: regex} patterns."""

    def __init__(self, bank, fetcher=None, *,
                 sitemap_url: str,
                 patterns: dict[DocType, str],
                 expected_per_year: Optional[dict[DocType, int]] = None,
                 max_sitemap_depth: int = 2):
        super().__init__(bank, fetcher)
        self.sitemap_url = sitemap_url
        self.patterns = {dt: re.compile(p, re.I) for dt, p in patterns.items()}
        self.native_types = tuple(patterns.keys())
        self.expected_per_year = dict(expected_per_year or {})
        self.max_depth = max_sitemap_depth

    def _walk_sitemap(self, url: str, depth: int = 0) -> Iterator[tuple[str, Optional[date]]]:
        if depth > self.max_depth:
            return
        try:
            xml = self.fetcher.get_text(url)
        except Exception:
            return
        urls, children = parse_sitemap(xml)
        yield from urls
        for child in children:
            yield from self._walk_sitemap(child, depth + 1)

    def _discover_native(self, doc_type: DocType,
                         since: Optional[date]) -> Iterator[DocRecord]:
        regex = self.patterns.get(doc_type)
        if regex is None:
            return
        seen: set[str] = set()
        for url, lastmod in self._walk_sitemap(self.sitemap_url):
            if url in seen or not regex.search(url):
                continue
            seen.add(url)
            title = url.rsplit("/", 1)[-1]
            d, prec, source = self._date_of(doc_type, url, title, lastmod)
            if since and d and d < since:
                continue
            mime = "application/pdf" if url.lower().endswith(".pdf") else "text/html"
            yield DocRecord(
                bank_code=self.bank.code, doc_type=doc_type,
                title=title, pdf_url=url,
                source_url=self.sitemap_url, date=d,
                provenance="bank_site", mime_type=mime,
                date_precision=prec, date_source=source,
            )

    def _date_of(self, doc_type: DocType, url: str, title: str, lastmod: Optional[date]
                 ) -> tuple[Optional[date], Optional[str], str]:
        """(date, date_precision, date_source) for one sitemap URL, in order:

        1. the series' own rule (dates.POLICY_DATE_RULES), when it has one:
           the same reading as the `policy-dates` backfill, so both agree;
        2. the sitemap <lastmod> -- the page's last MODIFICATION day, not its
           publication date: labelled `sitemap_lastmod` so it stays auditable;
        3. a full date in the URL (day), then a year-month (MONTH_IN_URL,
           month precision, first day of the month) -- the bank's own.
        A URL that names nothing is undated, with no precision."""
        inf = infer_policy_date(self.bank.code, doc_type, title=title, pdf_url=url)
        if inf.status == STATUS_DATED:
            return inf.match.date, inf.match.precision, "bank_site"
        if lastmod:
            return lastmod, "day", "sitemap_lastmod"
        d = _date_from_url(url)
        if d:
            return d, "day", "bank_site"
        d, prec = parse_url_date(url, MONTH_IN_URL)
        return d, prec, "bank_site"
