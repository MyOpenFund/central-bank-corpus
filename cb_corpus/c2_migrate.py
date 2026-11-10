"""ECB C2 one-shot enrichment from the live foedb source (interviews).

The interim Wayback-based C2 discovery (removed, see sources/ecb_foedb.py /
adapters/ecb.py) produced generic titles (``ECB C2 <date>``) and, once, a
corrupted URL. Now that C2 is discovered live from the foedb publications DB
(exact titles, exact Berlin day), this module joins the existing manifest C2
rows against that live DB by normalized URL and reports/applies the metadata
upgrade:

    title -> foedb's real title, but ONLY when the existing title is generic
             (empty or an "ECB C2 ..." placeholder); a non-generic title that
             differs from foedb's is kept, and the difference is reported
             (never silently overwritten -- a human may have hand-fixed it).
    date  -> foedb's Berlin day always wins (spec 2026-08-25 §5, same rule
             the WP v3 migration established), date_precision="day",
             date_source="bank_site".
    alt_urls -> the live foedb URL is added whenever it differs from the
             row's own pdf_url (double-slash legacy rows, the one corrupted
             row, and the one known ~hash re-issue -- see below).

``doc_id`` / ``sha256`` / ``local_path`` / ``pdf_url`` are never touched --
same invariant as wp_migrate: the file on disk is the same file, this is a
pure metadata correction with zero downloads.

This is the dry-run-by-default report (stdout summary + CSV under
data/reports/c2_migrate.csv). ``--write`` additionally applies the matched
changes to data/manifest/ecb.jsonl via lock-protected keyed updates
(Storage.rewrite_manifest -- non-C2 ecb rows and concurrent appends are
untouched on disk).
"""
from __future__ import annotations

import csv
import re
import sys
from typing import Iterable, Optional

from .config import Config
from .http import Fetcher
from .sources.ecb_foedb import discover_ecb_interviews
from .storage import Storage

_GENERIC_TITLE_RE = re.compile(r"^ecb c2\b", re.I)

# The one corrupted row recon (2026-08-25) found: a concatenation artifact
# where the URL's ``/nter/date/...`` tail got appended a second time after
# the real ``.en.html``. Detecting on this marker (rather than a hardcoded
# single URL) means the fix also covers any other row bearing the same
# artifact, should one turn up -- the clean form is everything up to and
# including the FIRST ``.en.html``.
_CORRUPTED_MARKER = ".en.html/nter/"

# One known URL-churn pair (recon 2026-08-25, spec "Accepted risks" §URL
# churn): ECB re-issued this interview under a new ~hash; the old URL's row
# already lives in the manifest as a separate identity. This is a single
# documented data fact from that reconnaissance, not a general algorithm
# parameter -- closing it here stops the churn from re-surfacing as a
# no-match every run.
_KNOWN_REHASH = {
    "ecb.in191202~fe0bc873b8.en.html": "ecb.in191202~869aa1e5ad.en.html",
}


def match_key_url(u: str) -> str:
    """FUZZY match key for joining a foedb record to a manifest C2 row: drop
    scheme, case, and collapse doubled slashes (legacy rows carry
    ``europa.eu//press/...``). Local to this module -- deliberately not shared
    with ``wp_migrate.match_key_url`` (C2 URLs carry no ``~hash`` the way
    WP/OP URLs do, so there is nothing else to strip).

    Not an identity: never use it for the known-URL indexes or dedup -- that
    is ``storage.normalize_url``, which answers a different question."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"/{2,}", "/", u)
    return u


def _clean_corrupted_url(url: str) -> str:
    """Strip a ``.en.html/nter/...`` concatenation artifact: keep everything
    up to and including the FIRST ``.en.html``."""
    idx = url.lower().find(".en.html")
    return url if idx == -1 else url[:idx + len(".en.html")]


def _is_generic_title(title: str) -> bool:
    t = (title or "").strip()
    return not t or bool(_GENERIC_TITLE_RE.match(t))


def _norm_cmp(s: str) -> str:
    """Loose equality key for the title-diff-kept check (informational only,
    never used for identity/matching)."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _match_native(url: str, native_by_url: dict) -> tuple[Optional[object], str]:
    """(native record or None, match_type) for a manifest C2 row's pdf_url."""
    native = native_by_url.get(match_key_url(url))
    if native is not None:
        return native, "url"
    if _CORRUPTED_MARKER in url:
        native = native_by_url.get(match_key_url(_clean_corrupted_url(url)))
        if native is not None:
            return native, "url-corrupted"
    for old_suffix, new_suffix in _KNOWN_REHASH.items():
        if url.endswith(old_suffix):
            target = url[:-len(old_suffix)] + new_suffix
            native = native_by_url.get(match_key_url(target))
            if native is not None:
                return native, "url-rehash"
    return None, ""


def build_report(fetcher: Fetcher, manifest_rows: Iterable[dict]) -> tuple[dict, list[dict]]:
    """Join live foedb interviews against manifest C2 rows (bank=ecb).

    Returns (summary_counts, change_rows). A change row is emitted for EVERY
    ecb C2 manifest row (matched or not) so the CSV report is a complete
    per-row disposition, not just the ones that would change.
    """
    native_by_url: dict[str, object] = {}
    for rec in discover_ecb_interviews(fetcher):
        native_by_url.setdefault(match_key_url(rec.pdf_url), rec)

    summary = {"native_total": len(native_by_url), "manifest_total": 0,
              "matched": 0, "no_match": 0, "enriched": 0,
              "title_diff_kept": 0, "unchanged": 0}
    changes: list[dict] = []

    for row in manifest_rows:
        if row.get("bank_code") != "ecb" or row.get("doc_type") != "C2":
            continue
        summary["manifest_total"] += 1
        url = row.get("pdf_url") or ""
        native, match_type = _match_native(url, native_by_url)

        if native is None:
            summary["no_match"] += 1
            changes.append({
                "doc_id": row.get("doc_id"), "action": "no-match", "match_type": "",
                "old_title": row.get("title") or "", "new_title": row.get("title") or "",
                "old_date": row.get("date") or "", "new_date": row.get("date") or "",
                "alt_url_added": "",
            })
            continue
        summary["matched"] += 1

        old_title = row.get("title") or ""
        is_generic = _is_generic_title(old_title)
        native_title = (native.title or "").strip()
        final_title = (native_title or old_title) if is_generic else old_title
        title_changed = final_title != old_title
        title_diff_kept = bool(not is_generic and native_title
                               and _norm_cmp(old_title) != _norm_cmp(native_title))

        new_date = native.date.isoformat() if native.date else (row.get("date") or "")
        date_changed = (row.get("date") != new_date
                        or row.get("date_precision") != "day"
                        or row.get("date_source") != "bank_site")

        native_url = native.pdf_url
        alt_url_added = native_url if native_url != url else ""

        if title_diff_kept:
            action = "title-diff-kept"
            summary["title_diff_kept"] += 1
        elif title_changed or date_changed or alt_url_added:
            action = "enriched"
            summary["enriched"] += 1
        else:
            action = "unchanged"
            summary["unchanged"] += 1

        changes.append({
            "doc_id": row.get("doc_id"), "action": action, "match_type": match_type,
            "old_title": old_title, "new_title": final_title,
            "old_date": row.get("date") or "", "new_date": new_date,
            "alt_url_added": alt_url_added,
        })

    return summary, changes


def apply_change(row: dict, change: dict) -> None:
    """Apply one proposed `change` to a manifest `row`, in place.

    Metadata only: title (possibly unchanged), date + precision/source, and
    the native URL registered in alt_urls when it differs from pdf_url.
    doc_id / sha256 / local_path / pdf_url are deliberately left untouched.
    """
    row["title"] = change["new_title"]
    if change.get("new_date"):
        row["date"] = change["new_date"]
        row["year"] = int(change["new_date"][:4])
    row["date_precision"] = "day"
    row["date_source"] = "bank_site"
    alt = change.get("alt_url_added")
    if alt:
        alts = list(row.get("alt_urls") or [])
        if alt not in alts:
            alts.append(alt)
        row["alt_urls"] = alts


_CSV_FIELDS = ["doc_id", "action", "match_type", "old_title", "new_title",
              "old_date", "new_date", "alt_url_added"]


def run_c2_migrate(cfg: Config, fetcher: Fetcher, write: bool = False) -> dict:
    """One-shot enrichment of existing ecb C2 rows from the live foedb source.

    Default (``write=False``) is a dry run: writes nothing to the manifest,
    prints a summary and a CSV of every ecb C2 row's disposition under
    data/reports/c2_migrate.csv. With ``write=True`` it additionally applies
    the matched changes (title/date/alt_urls only) to data/manifest/ecb.jsonl
    via lock-protected keyed updates (Storage.rewrite_manifest -- only the
    changed rows are replaced; non-C2 ecb rows and concurrent appends are
    untouched on disk). Returns the summary dict.
    """
    storage = Storage(cfg, fetcher)
    all_ecb_rows = list(storage.iter_manifest("ecb"))
    summary, changes = build_report(fetcher, all_ecb_rows)
    print(f"ecb C2: {summary}")

    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.reports_dir / "c2_migrate.csv"
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        w.writeheader()
        for c in changes:
            w.writerow({k: c.get(k, "") for k in _CSV_FIELDS})
    print(f"wrote {len(changes)} row(s) -> {out}", file=sys.stderr)

    if write:
        change_by_id = {c["doc_id"]: c for c in changes
                        if c["action"] in ("enriched", "title-diff-kept")}
        updates: dict[str, dict] = {}
        for row in all_ecb_rows:
            c = change_by_id.get(row.get("doc_id"))
            if c is not None:
                apply_change(row, c)
                updates[row["doc_id"]] = row
        n = storage.rewrite_manifest(updates)
        print(f"C2-MIGRATED {n} row(s) in place "
              f"(doc_id/pdf_url/sha256/local_path untouched)", file=sys.stderr)
        summary["applied"] = n
    return summary
