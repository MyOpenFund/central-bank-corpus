"""WP v3 — migrate v2 RePEc-dated rows to native bank-site dates (dry-run report).

The v2 manifest dates every D1/D2 working paper ``YYYY-MM-01`` (RePEc's
Creation-Date is month-only). The native bank-site scrapers know the exact day.
This module joins native discovery against the existing manifest rows by a match
cascade and reports the proposed date upgrade per row:

    date -> exact bank-site day, date_precision="day", date_source="bank_site",
    + stamp repec_handle, + register the native URL in alt_urls.

``doc_id`` / ``sha256`` / ``local_path`` are never touched — the PDF on disk is
the same file; this is a pure metadata correction with zero downloads.

This is the **dry-run report** (stdout summary + CSV). It writes nothing to the
manifest. Applying the migration (``--write``) and flipping a bank to native-first
discovery are deliberate follow-up steps (see docs/IMPLEMENTATION_PLAN.md phase 3),
because the native scraper must not run in download mode before the join has
registered native URLs in ``alt_urls``.

Currently wired for ECB (foedb). Other banks plug in as their native scrapers land.
"""
from __future__ import annotations

import csv
import re
import sys
from typing import Iterable, Optional

from .config import Config
from .http import Fetcher
from .sources.ecb_foedb import discover_ecb_wp, ecb_wp_number, repec_ecb_number
from .sources.fed_wp import discover_fed_wp, fed_key_from_url, fed_key_from_handle
from .sources.boj_wp import discover_boj_wp, boj_code
from .sources.boe_wp import discover_boe_wp, boe_slug
from .sources.buba_wp import discover_buba_wp, de_blob_key, de_handle_key
from .sources.bdf_wp import discover_fr_wp, fr_wp_number
from .sources.riksbank_wp import discover_riksbank_wp, se_url_number, se_handle_number
from .storage import Storage

# Native discovery per bank. doc_type ∈ {D1, D2}; returns DocRecords with day dates.
_NATIVE = {
    "ecb": discover_ecb_wp,
    "us": discover_fed_wp,
    "jp": discover_boj_wp,
    "gb": discover_boe_wp,
    "de": discover_buba_wp,
    "fr": discover_fr_wp,
    "se": discover_riksbank_wp,
}
# Per-bank join-key extractors: (pdf_url -> key) and (source_url/handle -> key).
# Native records and manifest rows are matched on equality of these keys. Banks
# whose manifest rows carry neither a usable number nor URL (de = EconStor copies)
# lean on the exact-normalized-title tier in build_report().
_KEY_FROM_PDF = {
    "ecb": ecb_wp_number,
    "us": fed_key_from_url,
    "jp": boj_code,                # paper code works from both the PDF URL and the handle
    "gb": boe_slug,                # BoE WP numbers aren't in the URL — match on the slug
    "de": de_blob_key,             # DP number from the Bundesbank blob filename
    "fr": fr_wp_number,            # WP number is the first digit run in the filename
    "se": se_url_number,           # WP number from any Riksbank filename era
}
_KEY_FROM_HANDLE = {
    "ecb": repec_ecb_number,
    "us": fed_key_from_handle,
    "jp": boj_code,
    "gb": lambda h: None,          # RePEc handle (boe:boeewp:NNNN) has no slug
    "de": de_handle_key,           # DP number from zbw:bubdps:{NN}{YYYY} (None if global id)
    "fr": fr_wp_number,            # same rule reads the IDEAS URL's basename (.../{NUM}.html)
    "se": se_handle_number,        # WP number from hhs:rbnkwp:NNNN / the IDEAS URL
}

_IDEAS_PATH = re.compile(r"/p/([^/]+)/([^/]+)/([^/.]+)")


def repec_handle_from_source_url(url: str) -> str:
    """IDEAS paper URL -> RePEc handle, e.g.
    https://ideas.repec.org/p/ecb/ecbwps/20253124.html -> RePEc:ecb:ecbwps:20253124
    """
    m = _IDEAS_PATH.search(url or "")
    return f"RePEc:{m.group(1)}:{m.group(2)}:{m.group(3)}" if m else ""


def normalize_url(u: str) -> str:
    """Canonicalise a PDF URL for equality: drop scheme, the ECB ``~hash`` segment,
    and collapse doubled slashes (manifest rows carry ``//pub`` from RePEc/RSS)."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"~[0-9a-z]+", "", u)
    u = re.sub(r"/{2,}", "/", u)
    return u


def normalize_title(s: str) -> str:
    """Canonicalise a title for an EXACT dedup match (the rule in
    docs/REPEC_AS_CHECK.md): lowercase, strip accents, non-alphanumeric runs ->
    single space, trimmed. Only EXACT normalized equality is ever auto-acted."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _row_key(bank: str, row: dict):
    """(doc_type, number) join key for a manifest row: from pdf_url, else handle."""
    return (_KEY_FROM_PDF[bank](row.get("pdf_url") or "")
            or _KEY_FROM_HANDLE[bank](row.get("source_url") or ""))


def build_report(bank: str, fetcher: Fetcher,
                 manifest_rows: Iterable[dict]) -> tuple[dict, list[dict]]:
    """Join native records against manifest D1/D2 rows for `bank`.

    Returns (summary_counts, change_rows). Each change row describes one matched
    manifest document and the metadata rewrite that the migration would apply.
    """
    # Index native discovery by join key, by normalized URL, and by normalized
    # title (the title index keeps only UNAMBIGUOUS titles — see below).
    key_from_pdf = _KEY_FROM_PDF[bank]
    native_by_key: dict = {}
    native_by_url: dict = {}
    title_recs: dict = {}
    for rec in _NATIVE[bank](fetcher):
        key = key_from_pdf(rec.pdf_url)
        if key is not None:
            native_by_key.setdefault(key, rec)
        native_by_url.setdefault(normalize_url(rec.pdf_url), rec)
        t = normalize_title(rec.title)
        if t:
            title_recs.setdefault(t, []).append(rec)
    # An exact normalized title is safe to match ONLY when it is unique in the
    # native set (otherwise we can't tell which paper it is).
    native_by_title = {t: recs[0] for t, recs in title_recs.items() if len(recs) == 1}

    summary = {"native_total": len(native_by_url), "manifest_total": 0,
               "matched_key": 0, "matched_url": 0, "matched_title": 0,
               "already_day": 0, "unmatched_manifest": 0}
    matched_native_urls: set = set()
    changes: list[dict] = []

    for row in manifest_rows:
        if row.get("bank_code") != bank or row.get("doc_type") not in ("D1", "D2"):
            continue
        summary["manifest_total"] += 1
        key = _row_key(bank, row)
        native = native_by_key.get(key) if key is not None else None
        match_type = "key" if native is not None else ""
        if native is None:
            native = native_by_url.get(normalize_url(row.get("pdf_url") or ""))
            match_type = "url" if native is not None else ""
        if native is None:
            native = native_by_title.get(normalize_title(row.get("title") or ""))
            match_type = "title" if native is not None else ""
        if native is None:
            summary["unmatched_manifest"] += 1
            continue
        summary[f"matched_{match_type}"] += 1
        matched_native_urls.add(normalize_url(native.pdf_url))

        # Already migrated? (idempotent — once a row's date came from the bank
        # site we don't touch it again, whatever its precision: a Fed paper can
        # legitimately settle at month precision.)
        if row.get("date_source") == "bank_site":
            summary["already_day"] += 1
            continue

        native_url = native.pdf_url
        new_date = native.date.isoformat() if native.date else row.get("date")
        changes.append({
            "doc_id": row.get("doc_id"),
            "doc_type": row.get("doc_type"),
            "old_date": row.get("date"),
            "new_date": new_date,
            # the native source's own precision (e.g. Fed papers without a confirmed
            # day, or revisions outside the listing month, stay "month"); an
            # undated native record keeps the row's date, so its precision too
            "date_precision": (native.date_precision if native.date
                               else row.get("date_precision")),
            "match_type": match_type,
            "old_url": row.get("pdf_url"),
            "native_url": native_url,
            "repec_handle": repec_handle_from_source_url(row.get("source_url") or ""),
            "alt_url_added": native_url if native_url != (row.get("pdf_url") or "") else "",
        })

    summary["native_only"] = sum(1 for u in native_by_url if u not in matched_native_urls)
    return summary, changes


def apply_change(row: dict, change: dict) -> None:
    """Apply one proposed migration `change` to a manifest `row`, in place.

    Metadata only: date + precision/source, repec_handle, and the native URL
    registered in alt_urls (so dedup recognises it). doc_id / sha256 / local_path
    / pdf_url are deliberately left untouched — the file on disk is the same.
    """
    row["date"] = change["new_date"]
    # keep the derived `year` field consistent with the (possibly year-shifted) date
    if change.get("new_date"):
        row["year"] = int(change["new_date"][:4])
    row["date_precision"] = change.get("date_precision") or "day"
    row["date_source"] = "bank_site"
    if change.get("repec_handle"):
        row["repec_handle"] = change["repec_handle"]
    alt = change.get("alt_url_added")
    if alt and alt != row.get("pdf_url"):
        alts = list(row.get("alt_urls") or [])
        if alt not in alts:
            alts.append(alt)
        row["alt_urls"] = alts


def run_wp_migrate(bank_codes: Optional[Iterable[str]] = None,
                   csv_path: Optional[str] = None,
                   write: bool = False,
                   config: Optional[Config] = None) -> dict[str, dict]:
    """Migration report for the requested banks (default: all wired).

    Default (``write=False``) is a dry run: writes nothing to the manifest, prints
    a per-bank summary and a CSV of proposed rewrites under data/reports/.

    With ``write=True`` it additionally applies the matched changes and atomically
    rewrites the manifest (metadata only — see :func:`apply_change`). Safety rails:
    the row count must be unchanged and the set of doc_ids identical before/after,
    or it raises before swapping the file in. Idempotent: a second run finds the
    rows already at day/bank_site and changes nothing. Returns {bank: summary}.
    """
    cfg = config or Config()
    fetcher = Fetcher(cfg)
    storage = Storage(cfg, fetcher)
    codes = [c for c in (bank_codes or _NATIVE.keys()) if c in _NATIVE]

    results: dict[str, dict] = {}
    all_changes: list[dict] = []
    for bank in codes:
        # Re-read the manifest per bank (cheap; keeps the join self-contained).
        summary, changes = build_report(bank, fetcher, storage.iter_manifest())
        for c in changes:
            c["bank"] = bank
        all_changes.extend(changes)
        results[bank] = summary
        print(f"{bank}: {summary} (proposed date fixes: {len(changes)})")

    if all_changes:
        out = csv_path or str(cfg.reports_dir / "wp_migrate.csv")
        cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        fields = ["bank", "doc_id", "doc_type", "old_date", "new_date", "date_precision",
                  "match_type", "old_url", "native_url", "repec_handle", "alt_url_added"]
        with open(out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for c in all_changes:
                w.writerow({k: c.get(k, "") for k in fields})
        print(f"wrote {len(all_changes)} proposed change(s) -> {out}", file=sys.stderr)
    else:
        print("no proposed changes (nothing matched, or all already migrated)",
              file=sys.stderr)

    if write and all_changes:
        change_by_id = {c["doc_id"]: c for c in all_changes}
        updates: dict[str, dict] = {}
        for row in storage.iter_manifest():
            c = change_by_id.get(row.get("doc_id"))
            if c is not None:
                apply_change(row, c)
                updates[row["doc_id"]] = row
        # Safety rails: every change matched a row; every update landed.
        assert len(updates) == len(change_by_id), (
            f"matched {len(updates)} != {len(change_by_id)} changes (doc_id mismatch?)")
        n = storage.rewrite_manifest(updates)
        assert n >= len(updates), (
            f"replaced {n} < {len(updates)} updates (>= because a bank file "
            f"with historical duplicate doc_id lines replaces/counts every "
            f"matching line)")
        print(f"MIGRATED {n} row(s) in place "
              f"(doc_id/sha256/local_path untouched)", file=sys.stderr)
    return results
