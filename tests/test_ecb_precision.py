"""ECB date honesty at the source (issue #23 c, E4 `since`).

- The foedb producers stamped `date_precision="day"` even when the record's
  `pub_timestamp` was missing or garbage (date None). `DocRecord.to_row`
  already nulls it on the way to disk (#37); the source now says so itself.
- Economic Bulletin (E4) rows are dated 1 January of their year (precision
  `year`): a `since` later than 1 January compared against that placeholder
  dropped every bulletin of the current year. A year-precision record is
  compared by year.

No network: the bulletin index is served by a duck-typed fetcher.
"""
from __future__ import annotations

from datetime import date

from cb_corpus.adapters.base import get_adapter
from cb_corpus.adapters.ecb import BULLETIN_INDEX
from cb_corpus.sources.ecb_foedb import _interview_to_doc, _record_to_doc
from cb_corpus.taxonomy import DocType


def _wp_rec(ts):
    return {"pub_timestamp": ts, "publicationProperties": {"Title": "WP"},
            "documentTypes": ["/pub/pdf/scpwps/ecbwp1000.pdf"]}


def test_foedb_wp_without_a_date_has_no_precision():
    for ts in (None, "garbage", ""):
        rec = _record_to_doc(_wp_rec(ts))
        assert rec.date is None and rec.date_precision is None, ts


def test_foedb_wp_with_a_date_stays_day():
    rec = _record_to_doc(_wp_rec(1579042800))
    assert rec.date is not None and rec.date_precision == "day"


def test_foedb_interview_precision_follows_the_date():
    assert _interview_to_doc("I", None, "https://x/i.en.html").date_precision is None
    rec = _interview_to_doc("I", date(2024, 3, 5), "https://x/i.en.html")
    assert (rec.doc_type, rec.date_precision) == (DocType.C2, "day")
    assert _interview_to_doc("", None, "https://x/i.en.html").title == "ECB interview"


class _Fetcher:
    def __init__(self, html):
        self.html = html

    def get_text(self, url, *a, **kw):
        assert url == BULLETIN_INDEX
        return self.html


_INDEX = """<html><body>
<a href="/pub/pdf/ecbu/eb202602.en.pdf">EB 2026/2</a>
<a href="/pub/pdf/ecbu/eb202601.en.pdf">EB 2026/1</a>
<a href="/pub/pdf/ecbu/eb202508.en.pdf">EB 2025/8</a>
<a href="/pub/pdf/ecbu/eb202401.en.pdf">EB 2024/1</a>
</body></html>"""


def _bulletins(since):
    ad = get_adapter("ecb", fetcher=_Fetcher(_INDEX))
    return [r.pdf_url.rsplit("/", 1)[-1] for r in ad._discover_bulletin(since)]


def test_e4_since_in_march_keeps_the_current_years_bulletins():
    assert _bulletins(date(2026, 3, 15)) == ["eb202602.en.pdf", "eb202601.en.pdf"]


def test_e4_since_on_the_last_day_of_the_year_keeps_that_year():
    assert _bulletins(date(2025, 12, 31)) == ["eb202602.en.pdf", "eb202601.en.pdf",
                                              "eb202508.en.pdf"]


def test_e4_since_first_of_january_and_none():
    assert _bulletins(date(2025, 1, 1)) == ["eb202602.en.pdf", "eb202601.en.pdf",
                                            "eb202508.en.pdf"]
    assert len(_bulletins(None)) == 4


def test_e4_records_are_year_precision():
    ad = get_adapter("ecb", fetcher=_Fetcher(_INDEX))
    assert {r.date_precision for r in ad._discover_bulletin(None)} == {"year"}
