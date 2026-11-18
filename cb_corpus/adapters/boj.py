"""Bank of Japan adapter.

A3 monetary-policy-meeting minutes (static yearly listings) plus native D1
Working Paper Series (see sources/boj_wp.py). Speeches (C1) and any other RePEc
series come from the base class. Hand-written (rather than the declarative TOML
path) because D1 needs a python scraper, not a listing regex.
"""
from __future__ import annotations

from datetime import date
from typing import Iterator, Optional

from ..dates import BOJ_MINUTES_URL
from ..models import DocRecord
from ..taxonomy import DocType
from .base import register
from .listing_crawler import ListingCrawlerAdapter

_MINUTES = ("https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_{year}/index.htm",
            r"g\d{6}\.pdf$")


@register("jp")
class BoJAdapter(ListingCrawlerAdapter):
    def __init__(self, bank, fetcher=None):
        # Minutes are dated by their file name, gYYMMDD.pdf = the last day of
        # the meeting (the generic YYYYMMDD reading finds nothing in it).
        super().__init__(bank, fetcher,
                         entries={DocType.A3: [_MINUTES]},
                         year_range=(2015, date.today().year),
                         date_patterns={DocType.A3: BOJ_MINUTES_URL})
        # D1 = Working Paper Series, native (exact day inline in the year tables).
        self.native_types = self.native_types + (DocType.D1,)

    def _discover_native(self, doc_type: DocType,
                         since: Optional[date]) -> Iterator[DocRecord]:
        if doc_type == DocType.D1:
            from ..sources.boj_wp import discover_boj_wp
            yield from discover_boj_wp(self.fetcher, since)
            return
        yield from super()._discover_native(doc_type, since)
