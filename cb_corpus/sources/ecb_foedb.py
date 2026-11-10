"""ECB native Working Paper / Occasional Paper discovery via the foedb JSON DB.

The ECB "publications by date" listing is NOT scrapable HTML — the page
(``/press/pubbydate/...``) is rendered client-side by the ``foedb`` plugin from a
versioned JSON database. We read that database directly: it is the same data the
website itself serves, covers the full archive (~20k publications) and carries
the exact publication day, so WP/OP discovery needs no HTML parsing and no
per-paper requests.

DB layout (discovered from ``foedb.min.js``):

    /foedb/dbs/foedb/publications.en/versions.json     -> [{"version","hash"}]
    .../<version>/<hash>/metadata.json                 -> {total_records, chunk_size, header, ...}
    .../<version>/<hash>/data/0/chunk_<N>.json         -> flat array; each record is
                                                          len(header) consecutive values.
                                                          Index "0" = all records, sorted
                                                          pub_timestamp DESC.

A record's ``documentTypes`` holds the file URLs. A Working Paper Series PDF is
``/pub/pdf/scpwps/ecb.wp<N>~<hash>.en.pdf`` (D1, modern) or ``/pub/pdf/scpwps/ecbwp<N>.pdf``
(legacy); an Occasional Paper is ``/pub/pdf/scpops/ecb.op<N>.en.pdf`` (modern) or
``/pub/pdf/scpops/ecbocp<N>.pdf`` (legacy). ``<N>`` is the matching key against
RePEc (whose handle ``ecb:ecbwps:<YYYY><N>`` concatenates year and number).
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import date, datetime
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

from ..http import Fetcher
from ..models import DocRecord
from ..taxonomy import DocType

ECB = "https://www.ecb.europa.eu"
FOEDB_DB = ECB + "/foedb/dbs/foedb/publications.en"
# The same foedb DB engine also serves a "publications_types" table mapping
# each numeric `type` value in publications.en to its human name (e.g. 27 ->
# "ECB Interview"). Read at run time by resolve_interview_type_id() below.
TYPES_DB = ECB + "/foedb/dbs/foedb/publications_types"

# WP/OP number from a PDF path. The series comes from the scpwps/scpops folder;
# the number is the first digit run in the filename AFTER the ECB `~hash` is
# stripped. The hash sits in different places across the archive's filename
# conventions, so we remove it wherever it is rather than assume a position:
#   scpwps/ecb.wp3244~0e92afef7d.en.pdf   scpwps/ecbwp722.pdf
#   scpops/ecb.op388.en.pdf               scpops/ecbocp2.pdf
#   scpwps/ecb~44d02b04fd.wp3181en.pdf    scpwps/ecb~0131e2da81.wp2585_en.pdf
#   scpops/ecb~ae799b1df9.op370en.pdf     (hash BEFORE the number)
_SERIES_RE = re.compile(r"/pub/pdf/scp(wps|ops)/", re.I)
_HASH_RE = re.compile(r"~[0-9a-z]+", re.I)
_FIRST_NUM_RE = re.compile(r"\d+")
# WP/OP number from a RePEc handle / IDEAS source_url. The IDEAS number is
# <YYYY><N> (year concatenated with the paper number, N not zero-padded):
#   ecbwps:20253124   ecbwps/2007722   ecbops:20022   ecbops/20001
_REPEC_ECB_RE = re.compile(r"ecb(wps|ops)[:/](\d{4})(\d+)", re.I)

_SERIES_TYPE = {"wps": DocType.D1, "ops": DocType.D2}

# ECB publishes in Central-European time. The foedb `pub_timestamp` is an epoch:
# recent papers carry the real release instant (~11:00 local), older ones store
# the date as local midnight. Both must be read in Europe/Berlin to recover the
# calendar day the bank asserts — reading them in UTC dates every local-midnight
# record (~25% of the archive) to the PREVIOUS day. Verified: foedb-in-Berlin
# equals the bank's own RSS pubDate exactly (14/14 recent papers, June 2026).
_ECB_TZ = ZoneInfo("Europe/Berlin")


def parse_versions(data) -> tuple[str, str]:
    """(version, hash) of the current DB build from ``versions.json`` contents."""
    if not data:
        raise ValueError("ECB foedb versions.json is empty")
    head = data[0]
    return str(head["version"]), str(head["hash"])


def parse_metadata(data: dict) -> tuple[int, int, list[str]]:
    """(total_records, chunk_size, header) from ``metadata.json`` contents."""
    return int(data["total_records"]), int(data["chunk_size"]), list(data["header"])


def chunk_records(flat: list, header: list[str]) -> list[dict]:
    """Slice a foedb flat data array into dict records keyed by ``header``.

    Records are stored as ``len(header)`` consecutive values; a trailing partial
    group (shouldn't happen for a well-formed chunk) is ignored.
    """
    n = len(header)
    return [dict(zip(header, flat[i:i + n]))
            for i in range(0, len(flat) - n + 1, n)]


def _record_date(ts) -> Optional[date]:
    """Unix `pub_timestamp` (seconds) -> publication date in Europe/Berlin.

    Must use the bank's local timezone, not UTC: ~25% of records are timestamped
    at local midnight, which UTC would push to the previous day (see _ECB_TZ).
    Returns None for missing/garbage values.
    """
    try:
        return datetime.fromtimestamp(int(ts), _ECB_TZ).date()
    except (TypeError, ValueError, OSError):
        return None


def _abs_url(path: str) -> str:
    """Make a foedb relative file path absolute on the ECB domain (single slash)."""
    return ECB + ("" if path.startswith("/") else "/") + path


def ecb_wp_number(url: str) -> Optional[tuple[DocType, int]]:
    """(doc_type, number) for an ECB WP/OP PDF URL, else None.

    Used both to read a native foedb URL and to extract the join key from an
    existing manifest ``pdf_url`` during migration.
    """
    ms = _SERIES_RE.search(url or "")
    if not ms:
        return None
    tail = _HASH_RE.sub("", (url or "")[ms.end():])   # drop ~hash wherever it sits
    mn = _FIRST_NUM_RE.search(tail)
    if not mn:
        return None
    return _SERIES_TYPE[ms.group(1).lower()], int(mn.group(0))


def repec_ecb_number(handle_or_url: str) -> Optional[tuple[DocType, int]]:
    """(doc_type, number) from a RePEc handle or IDEAS source_url, else None."""
    m = _REPEC_ECB_RE.search(handle_or_url or "")
    if not m:
        return None
    return _SERIES_TYPE[m.group(1).lower()], int(m.group(3))


def wp_op_from_record(rec: dict) -> Optional[tuple[DocType, int, str, Optional[date], str]]:
    """Extract (doc_type, number, title, date, pdf_url) if `rec` is a WP/OP, else None.

    Scans ``documentTypes`` first (the paper's own files), then ``childrenPublication``
    (language/variant children) for a scpwps/scpops PDF, preferring the English
    (``.en.pdf``) file when several are present.
    """
    sources: list = []
    for key in ("documentTypes", "childrenPublication"):
        val = rec.get(key)
        if isinstance(val, list):
            sources.extend(u for u in val if isinstance(u, str))
    candidates = [u for u in sources
                  if u.lower().endswith(".pdf") and ecb_wp_number(u) is not None]
    if not candidates:
        return None
    url = next((u for u in candidates if u.lower().endswith(".en.pdf")), candidates[0])
    doc_type, number = ecb_wp_number(url)  # type: ignore[misc]
    title = ((rec.get("publicationProperties") or {}).get("Title") or "").strip()
    return doc_type, number, title, _record_date(rec.get("pub_timestamp")), _abs_url(url)


def _record_to_doc(rec: dict) -> Optional[DocRecord]:
    got = wp_op_from_record(rec)
    if got is None:
        return None
    doc_type, number, title, d, pdf_url = got
    return DocRecord(
        bank_code="ecb", doc_type=doc_type,
        title=title or f"ECB {doc_type.name} {number}",
        pdf_url=pdf_url, source_url=FOEDB_DB, date=d,
        provenance="bank_site", mime_type="application/pdf",
        date_precision="day", date_source="bank_site",
    )


def discover_ecb_wp(fetcher: Fetcher,
                    since: Optional[date] = None) -> Iterator[DocRecord]:
    """Yield ECB Working Papers (D1) and Occasional Papers (D2) from the foedb DB.

    Reads the live DB version, then walks every data chunk. Records are globally
    sorted by ``pub_timestamp`` descending, so with ``since`` we stop as soon as we
    reach an older publication (efficient incremental runs); without it the whole
    archive is yielded. Network/schema errors propagate (fail loudly — WP v3 decision
    2026-06-12); there is deliberately no RePEc fallback.
    """
    version, db_hash = parse_versions(json.loads(fetcher.get_text(f"{FOEDB_DB}/versions.json")))
    base = f"{FOEDB_DB}/{version}/{db_hash}"
    total, chunk_size, header = parse_metadata(
        json.loads(fetcher.get_text(f"{base}/metadata.json")))
    n_chunks = math.ceil(total / chunk_size) if chunk_size else 0
    for i in range(n_chunks):
        flat = json.loads(fetcher.get_text(f"{base}/data/0/chunk_{i}.json"))
        for rec in chunk_records(flat, header):
            if since is not None:
                d = _record_date(rec.get("pub_timestamp"))
                if d is not None and d < since:
                    return                       # rest of the archive is older
            doc = _record_to_doc(rec)
            if doc is not None:
                yield doc


_INTERVIEW_TYPE_NAME = "ECB Interview"
_SKIP_NON_EN = object()   # sentinel: a real interview, excluded by language policy


def resolve_interview_type_id(fetcher: Fetcher) -> int:
    """The numeric `type` value meaning "ECB Interview", read from the ECB's own
    publications_types DB at run time. Resolving instead of hard-coding 27 is a
    honesty guard: if the ECB ever renumbers its types, a hard-coded id would
    silently harvest nothing -- this raises instead."""
    version, db_hash = parse_versions(json.loads(fetcher.get_text(f"{TYPES_DB}/versions.json")))
    base = f"{TYPES_DB}/{version}/{db_hash}"
    total, chunk_size, header = parse_metadata(json.loads(fetcher.get_text(f"{base}/metadata.json")))
    n_chunks = math.ceil(total / chunk_size) if chunk_size else 0
    for i in range(n_chunks):
        flat = json.loads(fetcher.get_text(f"{base}/data/0/chunk_{i}.json"))
        for rec in chunk_records(flat, header):
            if rec.get("publication_name") == _INTERVIEW_TYPE_NAME:
                return int(rec["id_publication_type"])
    raise ValueError(f"ECB publications_types DB has no {_INTERVIEW_TYPE_NAME!r} entry"
                     " -- the type map changed; refusing to harvest C2 silently")


def interview_from_record(rec: dict, type_id: int):
    """(title, date, absolute_url) if `rec` is an English interview; the
    `_SKIP_NON_EN` sentinel if it is an interview without an English version
    (9 known records, 2013-2020 -- EN-only policy, spec 2026-08-25, decision
    Marc); None if it is not an interview at all."""
    try:
        if int(rec.get("type")) != type_id:
            return None
    except (TypeError, ValueError):
        return None
    docs = [u for u in (rec.get("documentTypes") or []) if isinstance(u, str)]
    en = [u for u in docs if u.lower().endswith(".en.html")]
    if not en:
        return _SKIP_NON_EN if docs else None
    title = ((rec.get("publicationProperties") or {}).get("Title") or "").strip()
    return title, _record_date(rec.get("pub_timestamp")), _abs_url(en[0])


def discover_ecb_interviews(fetcher: Fetcher,
                            since: Optional[date] = None) -> Iterator[DocRecord]:
    """Yield ECB interviews/op-eds (C2) from the live foedb DB.

    Same walk as `discover_ecb_wp` (global pub_timestamp DESC -> early stop on
    `since`). Non-English-only interviews are excluded by policy, counted, and
    reported on stderr -- a documented exclusion, never a silent one."""
    type_id = resolve_interview_type_id(fetcher)
    version, db_hash = parse_versions(json.loads(fetcher.get_text(f"{FOEDB_DB}/versions.json")))
    base = f"{FOEDB_DB}/{version}/{db_hash}"
    total, chunk_size, header = parse_metadata(
        json.loads(fetcher.get_text(f"{base}/metadata.json")))
    n_chunks = math.ceil(total / chunk_size) if chunk_size else 0
    skipped_non_en = 0
    try:
        for i in range(n_chunks):
            flat = json.loads(fetcher.get_text(f"{base}/data/0/chunk_{i}.json"))
            for rec in chunk_records(flat, header):
                if since is not None:
                    d = _record_date(rec.get("pub_timestamp"))
                    if d is not None and d < since:
                        return
                got = interview_from_record(rec, type_id)
                if got is _SKIP_NON_EN:
                    skipped_non_en += 1
                    continue
                if got is None:
                    continue
                title, d, url = got
                yield DocRecord(
                    bank_code="ecb", doc_type=DocType.C2,
                    title=title or f"ECB interview {d or ''}".strip(),
                    pdf_url=url, source_url=FOEDB_DB, date=d,
                    provenance="bank_site", mime_type="text/html",
                    date_precision="day", date_source="bank_site",
                )
    finally:
        if skipped_non_en:
            print(f"[ecb-c2] {skipped_non_en} non-English-only interview(s) excluded"
                  " (EN-only policy)", file=sys.stderr, flush=True)
