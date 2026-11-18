"""Listing-page crawler adapter.

For banks that publish doc archives as static HTML listings (e.g. an "Archive
of MPC minutes" page with a list of PDF links), this adapter takes one or more
listing URLs + regexes per doc_type. Configured declaratively from
`banks_sources.toml`.

Each entry is a tuple `(url_template, link_regex)` where `url_template` can
contain `{year}` for year iteration.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import DocRecord
from ..taxonomy import DocType
from .base import BankAdapter

_DATE_RE = re.compile(r"(20\d{2})[-_/]?(0[1-9]|1[0-2])[-_/]?(0[1-9]|[12]\d|3[01])")


def _date_from(s: str) -> Optional[date]:
    m = _DATE_RE.search(s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_links(html: str, base_url: str, pattern: re.Pattern) -> list[tuple[Optional[date], str, str]]:
    """Extract (date, title, abs_url) from a listing page matching `pattern`."""
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[Optional[date], str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not pattern.search(href):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        title = a.get_text(" ", strip=True) or href.rsplit("/", 1)[-1]
        out.append((_date_from(href) or _date_from(title), title, url))
    return out


Entry = tuple[str, str]  # (url_template, link_regex_pattern)


class ListingCrawlerAdapter(BankAdapter):
    """Adapter that scrapes one or more listing pages per doc_type."""

    def __init__(self, bank, fetcher=None, *,
                 entries: dict[DocType, list[Entry]],
                 expected_per_year: Optional[dict[DocType, int]] = None,
                 year_range: Optional[tuple[int, int]] = None):
        super().__init__(bank, fetcher)
        self.entries = {
            dt: [(tpl, re.compile(rx, re.I)) for tpl, rx in lst]
            for dt, lst in entries.items()
        }
        self.native_types = tuple(entries.keys())
        self.expected_per_year = dict(expected_per_year or {})
        self.year_range = year_range or (2000, date.today().year)

    def _urls_for(self, tpl: str) -> Iterator[str]:
        if "{year}" in tpl:
            for y in range(self.year_range[0], self.year_range[1] + 1):
                yield tpl.format(year=y)
        else:
            yield tpl

    def _discover_native(self, doc_type: DocType,
                         since: Optional[date]) -> Iterator[DocRecord]:
        for tpl, regex in self.entries.get(doc_type, []):
            for url in self._urls_for(tpl):
                try:
                    html = self.fetcher.get_text(url)
                except Exception:
                    continue
                for d, title, abs_url in parse_links(html, url, regex):
                    if since and d and d < since:
                        continue
                    mime = ("application/pdf" if abs_url.lower().endswith(".pdf")
                            else "text/html")
                    yield DocRecord(
                        bank_code=self.bank.code, doc_type=doc_type,
                        title=title, pdf_url=abs_url, source_url=url,
                        date=d, provenance="bank_site", mime_type=mime,
                        # _DATE_RE reads a full YYYYMMDD; no date, no precision
                        date_precision="day" if d else None,
                    )
