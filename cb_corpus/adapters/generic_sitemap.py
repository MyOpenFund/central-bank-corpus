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

from ..models import DocRecord
from ..taxonomy import DocType
from .base import BankAdapter

_DATE_IN_URL_RE = re.compile(r"(20\d{2})[-_/]?(0[1-9]|1[0-2])[-_/]?(0[1-9]|[12]\d|3[01])")


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
            d = lastmod or _date_from_url(url)
            if since and d and d < since:
                continue
            mime = "application/pdf" if url.lower().endswith(".pdf") else "text/html"
            # A sitemap <lastmod> is the page's last MODIFICATION day, not its
            # publication date: label it as such so date quality stays
            # auditable. A date read from the URL is the bank's own.
            yield DocRecord(
                bank_code=self.bank.code, doc_type=doc_type,
                title=url.rsplit("/", 1)[-1], pdf_url=url,
                source_url=self.sitemap_url, date=d,
                provenance="bank_site", mime_type=mime,
                date_precision="day" if d else None,
                date_source="sitemap_lastmod" if lastmod else "bank_site",
            )
