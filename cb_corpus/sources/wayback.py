"""Wayback Machine (archive.org) recovery for documents whose official URL is
dead (HTTP 404 / removed).

The archived copy is the issuing bank's OWN original PDF — retrieved via the
`<timestamp>id_/` raw-snapshot form (returns the original bytes, not the
archive.org HTML wrapper). Used strictly as a last-resort source for official
PDFs the bank has since taken offline (e.g. pre-2008 Bank of England working
papers). Records are marked `provenance="wayback"` so the recovery is auditable.
"""
from __future__ import annotations

import json
from typing import Optional

from ..http import Fetcher
from .recovery import Source

CDX = "http://web.archive.org/cdx/search/cdx"


def cdx_pdfs(fetcher: Fetcher, url_prefix: str,
             mimetype: str = "application/pdf") -> list[tuple[str, str]]:
    """Return [(original_url, timestamp)] for archived docs under `url_prefix`.

    One CDX query, collapsed to the latest unique snapshot per URL (HTTP 200,
    filtered to `mimetype`, e.g. "application/pdf" or "text/html").
    """
    q = (f"{CDX}?url={url_prefix}&matchType=prefix&filter=statuscode:200"
         f"&filter=mimetype:{mimetype}&collapse=urlkey"
         f"&output=json&fl=original,timestamp")
    try:
        rows = json.loads(fetcher.get_text(q))
    except Exception:
        return []
    if not rows or rows[0][:1] == ["original"]:
        rows = rows[1:]  # drop header row
    return [(r[0], r[1]) for r in rows if len(r) >= 2]


def raw_url(original: str, timestamp: str) -> str:
    """The raw-snapshot URL that serves the ORIGINAL bytes (note the `id_`)."""
    return f"https://web.archive.org/web/{timestamp}id_/{original}"


def first_capture(fetcher: Fetcher, url: str) -> Optional[str]:
    """Earliest 200 snapshot timestamp (YYYYMMDDhhmmss) for an EXACT url, else None.

    CDX returns captures in ascending time order, so ``limit=1`` is the first one.
    No mimetype filter (a paper's landing page is HTML). The first archive of a
    document is an upper bound on its publication date — for date recovery
    (DATE_RECOVERY.md rung 1) we accept it only when it lands in the paper's
    RePEc month (the caller enforces that month constraint).
    """
    q = (f"{CDX}?url={url}&filter=statuscode:200&output=json&fl=timestamp&limit=1")
    try:
        rows = json.loads(fetcher.get_text(q))
    except Exception:
        return None
    rows = [r for r in rows if r and r[0] != "timestamp"]
    return rows[0][0] if rows else None


def latest_capture(fetcher: Fetcher, url: str,
                   mimetype: str = "application/pdf") -> Optional[str]:
    """Latest HTTP-200 snapshot timestamp (YYYYMMDDhhmmss) for an EXACT url, else
    None. Mirrors `first_capture`'s exact-url style (no `matchType=prefix` — a
    prefix match would bleed in unrelated documents sharing the path).

    `limit=-1` asks CDX for the tail of the resultset (most recent capture) —
    same convention `wayback_for_url` already relied on. Filtered to `mimetype`
    (default the PDF the recovery flows care about).
    """
    q = (f"{CDX}?url={url}&filter=statuscode:200&filter=mimetype:{mimetype}"
         f"&output=json&fl=timestamp&limit=-1")
    try:
        rows = json.loads(fetcher.get_text(q))
    except Exception:
        return None
    rows = [r for r in rows if r and r[0] != "timestamp"]
    return rows[0][0] if rows else None


def wayback_for_url(fetcher: Fetcher, url: str) -> Optional[str]:
    """Latest archived PDF snapshot (raw-bytes URL) for ONE exact url, or None.

    For sources with opaque paths (e.g. riksbank.com/upload/<id>/...) where a CDX
    prefix can't isolate the right documents, we query the exact url instead.
    """
    ts = latest_capture(fetcher, url)
    return raw_url(url, ts) if ts else None


class WaybackSource(Source):
    """Recover official PDFs a bank took offline — the canonical `Source` example.

    Enumerates archived PDFs under `url_prefix` (CDX); each record's `pdf_url` is
    the original (dead) bank URL (citation + stable id) and `alt_urls` carries the
    Wayback raw snapshot Storage actually downloads. Date is the year in the URL.
    """

    def __init__(self, bank_code, url_prefix, doc_type, title_fn=None):
        self.bank_code, self.url_prefix = bank_code, url_prefix
        self.doc_type, self.title_fn = doc_type, title_fn
        self.label = f"wayback:{bank_code}"

    def items(self, fetcher, storage):
        import re
        from datetime import date
        from ..models import DocRecord
        for original, ts in cdx_pdfs(fetcher, self.url_prefix):
            y = re.search(r"/(19|20)\d{2}/", original)
            d = date(int(original[y.start() + 1:y.start() + 5]), 1, 1) if y else None
            title = self.title_fn(original) if self.title_fn else original.rsplit("/", 1)[-1]
            yield DocRecord(
                bank_code=self.bank_code, doc_type=self.doc_type, title=title,
                pdf_url=original, alt_urls=[raw_url(original, ts)], date=d,
                provenance="wayback", mime_type="application/pdf",
                # Only the year in the archived URL is known (or nothing).
                date_precision="year" if d else None, date_source="wayback",
            )
