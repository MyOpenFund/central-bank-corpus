"""Canonical record for one official document."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional, Union

from .taxonomy import DocType


def compute_doc_id(bank_code: str, doc_type: Union[DocType, str], url: str) -> str:
    """THE doc_id formula: sha1("<bank>|<doc_type code>|<url>")[:16].

    Single source of truth for DocRecord.doc_id and for every caller that has
    to recompute an id without building a record (e.g.
    pipeline.reindex_bis_from_disk matching on-disk files). Never change it:
    every file name and manifest key on disk derives from it. `doc_type` may
    be a DocType or its code ("C1")."""
    code = doc_type.code if isinstance(doc_type, DocType) else doc_type
    return hashlib.sha1(f"{bank_code}|{code}|{url}".encode("utf-8")).hexdigest()[:16]


@dataclass
class DocRecord:
    bank_code: str
    doc_type: DocType
    title: str
    pdf_url: str                       # canonical document URL (PDF preferred, HTML accepted)
    source_url: str = ""               # landing/listing page it was found on
    date: Optional[date] = None
    language: str = "en"
    provenance: str = "bank_site"      # bank_site | bis_index | repec_discovery
    mime_type: str = ""                # "application/pdf" | "text/html" | ""
    sha256: Optional[str] = None       # filled after download
    local_path: Optional[str] = None   # filled after download — the canonical artifact (PDF preferred)
    html_path: Optional[str] = None    # set when source was HTML — the original HTML sibling file
    # Date-quality metadata (WP v3). `date_precision` is how precise `date` is;
    # `date_source` is where that date came from. Defaults describe the common
    # case — a native bank-site listing with an exact day. Producers that yield a
    # coarser/different date MUST set these explicitly (e.g. RePEc → month/repec,
    # a year-only listing → year). This makes date quality auditable per row.
    # `date_precision` is None when there is no date at all (`date` is None);
    # a producer with no date may also leave `date_source` None (e.g. an
    # IDEAS page without a date) rather than claim a source for nothing.
    # `sitemap_lastmod` = a sitemap <lastmod> (last modification day, not a
    # publication date) used for lack of anything better.
    date_precision: Optional[str] = "day"  # day | month | year | None (undated)
    # None for undated records, e.g. RePEc/IDEAS pages without a date.
    date_source: Optional[str] = "bank_site"  # bank_site | repec | wayback | pdf_meta | nep_bound | llm_crawl | sitemap_lastmod
    # RePEc handle (e.g. "RePEc:ecb:ecbwps:20253117"), stamped by repec-check /
    # the v2 migration when a manifest row is matched to its IDEAS record. Empty
    # when unknown. Optional metadata only — never part of doc_id.
    repec_handle: str = ""
    # Ordered fallback PDF URLs (e.g. EconStor/SSRN copies, or a native URL found
    # for a paper first ingested via RePEc) tried by Storage when the preferred
    # `pdf_url` download fails. NOT part of doc_id, so identity/dedup stay stable
    # on the preferred URL regardless of which copy actually downloaded. Persisted
    # (since WP v3) so dedup recognises alternate URLs across restarts.
    alt_urls: list[str] = field(default_factory=list)
    # NOT a field: manifest rows may also carry an optional `alt_source_urls`
    # list, written only by Storage.stamp_alt_urls (source pages of records
    # whose content duplicated this row) and indexed by Storage._load_existing
    # into the known-source-URL index. Absent on most rows.

    @property
    def year(self) -> Optional[int]:
        return self.date.year if self.date else None

    @property
    def doc_id(self) -> str:
        """Stable id used for filenames and dedup.

        Derived from IMMUTABLE identity only (bank, type, url) — deliberately NOT
        the date. Date is mutable metadata (it gets corrected/enriched); binding
        it into the id used to mean a date fix changed the id, creating a
        duplicate + a file rename. With the date out, enriching a date is a pure
        metadata update (no id churn). The url already uniquely identifies a doc.
        """
        return compute_doc_id(self.bank_code, self.doc_type, self.pdf_url)

    def to_row(self) -> dict:
        d = asdict(self)
        d["doc_type"] = self.doc_type.code
        d["date"] = self.date.isoformat() if self.date else None
        d["doc_id"] = self.doc_id
        d["year"] = self.year
        return d
