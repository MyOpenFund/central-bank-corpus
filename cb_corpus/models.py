"""Canonical record for one official document."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Mapping, MutableMapping, Optional, Union

from .dates import policy_date_kind
from .taxonomy import DocType

#: Name of the canonical document URL key in a manifest row (and in the other
#: row dicts that carry it: download_errors.jsonl entries, recover-downloads
#: candidates as `dead_document_url`, retired_duplicates audit entries).
URL_KEY = "document_url"
#: The same key's name until 2026-11-19 (`pdf_url`). TRANSITION: remove,
#: with LEGACY_URL_KEYS, row_url's fallback and upgrade_row, once the NAS
#: manifests are migrated (`migrate-document-url --write`).
LEGACY_URL_KEY = "pdf_url"
#: new key -> old key, for every row dict that may still carry the old name.
LEGACY_URL_KEYS = {URL_KEY: LEGACY_URL_KEY, "dead_document_url": "dead_pdf_url"}

#: This corpus's code in the `documents` contract (vault-contract 1.0.0):
#: every manifest row carries `corpus: "central-bank"`; the vault's identity
#: of a document is (`corpus`, `doc_id`).
CORPUS = "central-bank"

#: Every `provenance` value the code writes (how a document was obtained):
#: the bank's own site, the BIS speeches index, a RePEc/IDEAS listing, a
#: Wayback snapshot (recovery), a legitimate mirror/co-publication
#: (recover-downloads candidates). Declared in vocabulary.json; the vault
#: refuses a row whose provenance is not declared. A new provenance in the
#: code MUST be added here (tests/test_vocabulary.py scans the package).
PROVENANCES: tuple[str, ...] = ("bank_site", "bis_index", "repec_discovery",
                                "wayback", "mirror")


def row_url(row: Mapping[str, Any], key: str = URL_KEY) -> Any:
    """The document URL of a row dict: `row[key]` (default `document_url`),
    or, when that key is ABSENT, the value under its pre-2026-11-19 name
    (`pdf_url`, `dead_pdf_url`). None when neither key is there.

    TRANSITION, remove once the NAS manifests are migrated: every reader of
    a manifest row (or of another row dict that stores the key) goes through
    this helper, so a row not yet migrated never breaks a run. Writers write
    only `document_url`."""
    if key in row:
        return row[key]
    return row.get(LEGACY_URL_KEYS.get(key, key))


def upgrade_row(row: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Rename a row's `pdf_url` key to `document_url` IN PLACE, keeping the
    key's position and every value; same for the entries of an optional
    `retired_duplicates` audit list. Returns `row` (for chaining). A row that
    already uses the new name is left as is; a row (or entry) carrying BOTH
    names is left untouched too -- never guess which value is right
    (`migrate-document-url` reports it as an error).

    TRANSITION, remove once the NAS manifests are migrated: applied where
    manifest rows are read (storage), so a row read under the old name and
    rewritten by any rewrite path comes out with `document_url` only."""
    _rename_key(row)
    for entry in row.get("retired_duplicates") or []:
        if isinstance(entry, dict):
            _rename_key(entry)
    return row


def _rename_key(d: MutableMapping[str, Any], old: str = LEGACY_URL_KEY,
                new: str = URL_KEY) -> bool:
    """`old` -> `new` in place, position kept. False (nothing done) when `old`
    is absent or `new` is already there."""
    if old not in d or new in d:
        return False
    items = [((new if k == old else k), v) for k, v in d.items()]
    d.clear()
    d.update(items)
    return True


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
    document_url: str                  # canonical document URL (PDF preferred, HTML accepted);
                                       # named `pdf_url` until 2026-11-19 (doc_id unchanged)
    source_url: str = ""               # landing/listing page it was found on
    date: Optional[date] = None
    language: str = "en"
    provenance: str = "bank_site"      # bank_site | bis_index | repec_discovery
    mime_type: str = ""                # "application/pdf" | "text/html" | ""
    sha256: Optional[str] = None       # filled after download
    local_path: Optional[str] = None   # filled after download — the canonical artifact (PDF preferred)
    html_path: Optional[str] = None    # set when source was HTML — the original HTML sibling file
    # Date-quality metadata. `date_precision` is how precise `date` is;
    # `date_source` is where that date came from. A month- or year-precision
    # date is stored as the FIRST DAY of the period (see cb_corpus/dates.py).
    # The default precision is None = "not stated": a producer that reads a
    # day, a month or a year MUST say so explicitly (a native listing with an
    # exact day -> "day", RePEc -> "month", a year-only listing -> "year").
    # A producer that says nothing no longer claims a day it may not have.
    # An undated record (`date` is None) never has a precision: `to_row`
    # writes null whatever the producer set. A producer with no date may also
    # leave `date_source` None (e.g. an IDEAS page without a date) rather
    # than claim a source for nothing.
    # `sitemap_lastmod` = a sitemap <lastmod> (last modification day, not a
    # publication date) used for lack of anything better.
    date_precision: Optional[str] = None  # day | month | year | None (not stated / undated)
    # None for undated records, e.g. RePEc/IDEAS pages without a date.
    date_source: Optional[str] = "bank_site"  # bank_site | repec | wayback | pdf_meta | nep_bound | llm_crawl | sitemap_lastmod
    # What `date` IS: meeting | publication | period (cb_corpus.dates.DATE_KINDS).
    # Leave it None: `to_row` takes it from dates.policy_date_kind (the
    # series' kind and its documented row-level exceptions) for the policy
    # core. A producer sets it only to say something those rules do not. Optional manifest key: written only
    # when there is a date AND a kind, absent otherwise. Never part of doc_id.
    date_kind: Optional[str] = None
    # RePEc handle (e.g. "RePEc:ecb:ecbwps:20253117"), stamped by repec-check /
    # the v2 migration when a manifest row is matched to its IDEAS record. Empty
    # when unknown. Optional metadata only — never part of doc_id.
    repec_handle: str = ""
    # Ordered fallback PDF URLs (e.g. EconStor/SSRN copies, or a native URL found
    # for a paper first ingested via RePEc) tried by Storage when the preferred
    # `document_url` download fails. NOT part of doc_id, so identity/dedup stay stable
    # on the preferred URL regardless of which copy actually downloaded. Persisted
    # (since WP v3) so dedup recognises alternate URLs across restarts.
    alt_urls: list[str] = field(default_factory=list)
    # NOT a field: manifest rows may also carry an optional `alt_source_urls`
    # list, written only by Storage's duplicate stamping (source pages of records
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

        Residual risk (issue #21): the id hashes the RAW `document_url`, while the
        known-URL dedup index normalises it (storage.normalize_url collapses
        duplicate slashes). Two spellings of one URL therefore give two
        doc_ids. For a PDF the sha256 dedup still catches the second copy
        (`skip:duplicate-content`), but an HTML page can serve different
        bytes on every fetch, so nothing stops a second row -- this happened
        once (15 ECB D3 rows under a double-slash variant, fixed by hand with
        `alt_urls`). Hence a rule for every NEW discovery path: call the
        storage skip hooks (`Storage.is_known_url` / `is_known_source_url`,
        wired as an adapter's `_skip_known_url` or a source's `skip_url`
        argument, see pipeline.py) BEFORE building a DocRecord, so a
        URL variant is recognised as known and never mints a second id.
        Normalising inside the id is not an option: every file name and
        manifest key on disk derives from the current formula.
        """
        return compute_doc_id(self.bank_code, self.doc_type, self.document_url)

    def to_row(self) -> dict:
        d = asdict(self)
        d["doc_type"] = self.doc_type.code
        d["date"] = self.date.isoformat() if self.date else None
        if self.date is None:
            d["date_precision"] = None      # no date -> no precision
        kind = self.date_kind or policy_date_kind(
            self.bank_code, self.doc_type, date=self.date, precision=self.date_precision,
            date_source=self.date_source, title=self.title,
            document_url=self.document_url)
        if self.date is not None and kind:
            d["date_kind"] = kind
        else:
            del d["date_kind"]              # optional key: absent, never null
        d["doc_id"] = self.doc_id
        d["year"] = self.year
        return d
