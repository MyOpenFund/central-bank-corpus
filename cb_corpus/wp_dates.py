"""WP v3 phase 4 — recover the publication DAY for legacy month/year-precision
working papers, replicably.

For rows that aren't already at day precision (RePEc-dated legacy, or bank-site
rows the publisher only dates to the month), try in order, stopping at the first
hit that satisfies the **month constraint** (recovered day must fall in the row's
asserted YYYY-MM, and not be the 1st):

  0. committed index  — `data/wp_dates_index.jsonl` short-circuits everything
                        (run-once, replay-forever; no network on a re-build).
                        A replayed hit passes the SAME month constraint against
                        the row it is applied to (issue #22): the index is joined
                        by key, and a key can match a row the entry was not
                        resolved for (a shared title, a stale entry). A hit that
                        fails is counted `index_rejected` and the row falls
                        through to rungs 1-2 like a miss.
  1. PDF `/CreationDate` — read from the on-disk file (zero network)
  2. Wayback first capture of the PDF URL (then the abs/landing variant)

Hits from rungs 1-2 are appended to the committed index with an `evidence_url`,
so a fresh clone replays the exact same dates without re-crawling. Dates that
don't satisfy the month constraint are rejected — the row keeps month precision
rather than being moved to a different month (e.g. a 2009 digitisation snapshot
of a 1975 paper, or a revision-era PDF CreationDate).
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date
from typing import Iterable, Optional

from .config import LEGACY_DATA_PREFIX, Config
from .http import Fetcher
from .models import SOURCE_KEY, URL_KEY
from .sources.wayback import first_capture
from .storage import Storage, apply_row_updates
from .wp_migrate import normalize_title, repec_handle_from_source_url

_LEGACY_BANKS = ("ecb", "us", "jp", "gb", "de")


def index_path(cfg: Config):
    return cfg.data_dir / "wp_dates_index.jsonl"


def load_index(cfg: Config) -> dict:
    """key -> entry (last line wins for a repeated key). Key = RePEc handle when
    known, else a title key (`row_key`: bank-scoped `t:<bank>:<title>` for new
    entries, the legacy global `t:<title>` form for older ones)."""
    p = index_path(cfg)
    idx: dict = {}
    if p.exists():
        with p.open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    e = json.loads(line)
                    idx[e["key"]] = e
    return idx


def row_key(row: dict) -> Optional[str]:
    """Stable join key for the index: the RePEc handle if derivable, else the
    bank-scoped normalized title `t:<bank_code>:<title>`.

    Why scoped (issue #22): the title tier is a fuzzy identity -- generic
    working-paper titles recur across banks, and `normalize_title` strips
    accents and punctuation, widening collisions further. A global title key
    let one bank's resolved day be replayed onto another bank's paper. The
    bank scope cannot collide with the legacy form: a normalized title holds
    no ':' (non-alphanumerics become spaces). Handle keys are unique per
    paper and stay unchanged."""
    h = repec_handle_from_source_url(row.get("source_url") or "") or (row.get("repec_handle") or "")
    if h:
        return h
    t = normalize_title(row.get("title") or "")
    if not t:
        return None
    bank = (row.get(SOURCE_KEY) or "").strip().lower()
    return f"t:{bank}:{t}"


def legacy_title_key(row: dict) -> Optional[str]:
    """The pre-#22 global title key `t:<title>`, for a row that has no handle
    (None otherwise). Only READ: entries written before the bank scope keep
    replaying (the committed index is never rewritten), always month-checked
    against the row; new entries are only ever written under `row_key`."""
    if (repec_handle_from_source_url(row.get("source_url") or "")
            or row.get("repec_handle")):
        return None
    t = normalize_title(row.get("title") or "")
    return "t:" + t if t else None


#: Separator between a shared key and a row's doc_id in a disambiguated key.
#: Neither a RePEc handle nor a normalized title contains it.
DOC_KEY_SEP = "#"


def doc_key(row: dict) -> Optional[str]:
    """`<row_key>#<doc_id>`: the key a row's fresh resolution is written under
    when its own `row_key` already holds an entry for ANOTHER paper (one that
    failed the month check against this row).

    Why (real case on the committed manifests): FEDS 2025-101 and its r1
    revision share one RePEc handle. Writing the revision's day under the
    handle would shadow the original's entry (last line wins): the next
    rebuild rejects the original, re-recovers it, overwrites the handle again
    -- a flip-flop that grows the index on every run. None when the row has
    no `row_key` or no `doc_id`."""
    key, did = row_key(row), row.get("doc_id")
    return f"{key}{DOC_KEY_SEP}{did}" if key and did else None


def lookup_index(idx: dict, row: dict) -> tuple[Optional[dict], bool]:
    """Find the committed-index entry that may be replayed onto `row`.

    Tries `doc_key` (this row's own disambiguated entry), then `row_key`, then
    `legacy_title_key`; the first entry whose date passes `month_ok` against
    THIS row wins. Returns `(entry, rejected)`: `entry` is None when nothing
    usable was found, and `rejected` is True when at least one keyed entry
    existed but every one failed the month check (or carried an unparseable
    date) -- the caller counts it and treats the row as a miss.
    """
    rejected = False
    for key in (doc_key(row), row_key(row), legacy_title_key(row)):
        hit = idx.get(key) if key else None
        if hit is None:
            continue
        if month_ok(_iso_date(hit.get("date")), row):
            return hit, False
        rejected = True
    return None, rejected


def _row_ym(row: dict) -> Optional[tuple[int, int]]:
    d = row.get("date") or ""
    return (int(d[:4]), int(d[5:7])) if len(d) >= 7 and d[:4].isdigit() else None


def month_ok(cand: Optional[date], row: dict) -> bool:
    """Candidate day is in the row's asserted YYYY-MM and not the 1st.

    A row whose `date_precision` is `year` NEVER passes: its 1 January is a
    placeholder (RePEc `citation_year` fallback, E4 bulletins), not an
    asserted January, so reading it as "January" would reject the true day
    and accept a wrong January capture (issue #23 a). There is deliberately
    no year-level check instead: a Wayback first capture or a PDF
    /CreationDate anywhere in the year is too weak to stamp a day on it
    (digitisation snapshots, revision-era PDFs). Such rows keep year
    precision, on fresh recovery and on index replay alike."""
    if row.get("date_precision") == "year":
        return False
    ym = _row_ym(row)
    return bool(cand and ym and (cand.year, cand.month) == ym and cand.day != 1)


def _iso_date(value) -> Optional[date]:
    """An index entry's ISO `date` -> date; None when missing or malformed (a
    hand-edited index line must reject the hit, not crash the replay)."""
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _ts_date(ts: Optional[str]) -> Optional[date]:
    """Wayback timestamp YYYYMMDD... -> date."""
    try:
        return date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
    except (TypeError, ValueError):
        return None


def pdf_creation_date(local_path: Optional[str]) -> Optional[date]:
    """The PDF's /CreationDate day (raw Info dict), else None. No pypdf needed."""
    if not local_path:
        return None
    try:
        with open(local_path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return None
    m = re.search(rb"/CreationDate\s*\(D:(\d{8})", blob)
    if not m:
        return None
    s = m.group(1).decode()
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _file_evidence(stored: Optional[str]) -> str:
    """`evidence_url` of a PDF /CreationDate hit. The committed index spells
    every one `file://data/raw/...` (the path as seen from the app directory,
    the manifests' form before the documents contract); it is evidence text,
    never read back as a path, so new entries keep that single spelling
    whichever form the row stores (`raw/...` or `data/raw/...`)."""
    s = stored or ""
    if s and not s.startswith("/") and not s.startswith(LEGACY_DATA_PREFIX):
        s = LEGACY_DATA_PREFIX + s
    return f"file://{s}"


def _abs_variant(url: Optional[str]) -> Optional[str]:
    """The Fed PDF<->abstract sibling next to `url` (…NNNpap.pdf <-> …NNNabs.html)."""
    if url and url.endswith("pap.pdf"):
        return url[:-len("pap.pdf")] + "abs.html"
    if url and url.endswith("abs.html"):
        return url[:-len("abs.html")] + "pap.pdf"
    return None


def _bare(url: str) -> str:
    return re.sub(r"^https?://", "", url or "")


def _url_variants(row: dict) -> list[str]:
    """Distinct URLs worth querying Wayback for: the document_url, its abs/pdf sibling,
    and any alt_urls (e.g. the native bank URL registered during migration)."""
    out: list[str] = []
    for u in [row.get(URL_KEY), *(row.get("alt_urls") or [])]:
        if not u:
            continue
        out.append(u)
        sib = _abs_variant(u)
        if sib:
            out.append(sib)
    return list(dict.fromkeys(out))


def recover(fetcher: Fetcher, row: dict, use_wayback: bool = True,
            cfg: Optional[Config] = None) -> Optional[dict]:
    """Recover a day for one legacy row → {date, date_source, evidence_url} | None.

    On-disk PDF /CreationDate first (free), then (unless `use_wayback` is False)
    the Wayback first-capture of each URL variant (PDF, its abs/pdf sibling,
    alt_urls). Every candidate must pass the month constraint.
    """
    # the stored path (either form) is relative to the data root, not the CWD
    on_disk = (cfg or Config()).resolve_stored_path(row.get("local_path"))
    cd = pdf_creation_date(str(on_disk) if on_disk else None)
    if month_ok(cd, row):
        return {"date": cd.isoformat(), "date_source": "pdf_meta",
                "evidence_url": _file_evidence(row.get("local_path"))}
    if not use_wayback:
        return None
    for url in _url_variants(row):
        ts = first_capture(fetcher, _bare(url))
        wd = _ts_date(ts)
        if month_ok(wd, row):
            return {"date": wd.isoformat(), "date_source": "wayback",
                    "evidence_url": f"https://web.archive.org/web/{ts}/{url}"}
    return None


def run_wp_dates(bank_codes: Optional[Iterable[str]] = None,
                 write: bool = False, csv_path: Optional[str] = None,
                 since_year: int = 1997, use_wayback: bool = True,
                 apply_only: bool = False, config: Optional[Config] = None) -> dict:
    """Recover days for non-day D1/D2 rows of the requested banks (default: the 5
    natively-covered banks). Default is a dry-run report; `write` rewrites the
    matched manifest rows (date/date_precision=day/date_source) and appends new
    resolutions to the committed index. `since_year` skips Wayback for very old
    rows whose first capture is always a later-era digitisation (still tried via
    PDF meta). Idempotent: day rows are skipped; index entries short-circuit.

    Every index hit is month-checked against the row before it is applied
    (`lookup_index`); a rejected hit is counted in `index_rejected` and the row
    goes on to fresh recovery like a miss (in `apply_only`, it stays
    unresolved). Year-precision rows are skipped before the lookup and any
    recovery (they can never pass `month_ok`) and counted in `year_skipped`,
    outside `candidates`. Returns the counts dict."""
    cfg = config or Config()
    fetcher = Fetcher(cfg)
    storage = Storage(cfg, fetcher)
    codes = set(bank_codes) if bank_codes else set(_LEGACY_BANKS)
    idx = load_index(cfg)
    # apply_only = replay the committed index onto the manifest with no recovery /
    # network (what a fresh clone runs to materialise the dates). Otherwise, in
    # write mode each new resolution is appended to the committed index immediately
    # (resumable: an interrupted crawl keeps progress; a re-run short-circuits).
    idx_fh = index_path(cfg).open("a") if (write and not apply_only) else None

    by_id: dict[str, dict] = {}          # doc_id -> {date, date_source} to apply
    new_count = 0
    counts = {"candidates": 0, "year_skipped": 0, "from_index": 0, "index_rejected": 0,
              "pdf_meta": 0, "wayback": 0, "unresolved": 0}

    try:
        for row in storage.iter_manifest():
            if (row.get(SOURCE_KEY) not in codes or row.get("doc_type") not in ("D1", "D2")
                    or row.get("date_precision") == "day"):
                continue
            if row.get("date_precision") == "year":
                # never passes month_ok (no asserted month): skip before the
                # index lookup and any PDF read / Wayback call -- counted, not a
                # candidate, not a rejection, not unresolved.
                counts["year_skipped"] += 1
                continue
            counts["candidates"] += 1
            key = row_key(row)
            hit, rejected = lookup_index(idx, row)
            if rejected:
                counts["index_rejected"] += 1
            if hit is None and not apply_only:
                yr = (_row_ym(row) or (0, 0))[0]
                # PDF meta is free (any year); Wayback only from ~the online era.
                r = recover(fetcher, row, use_wayback=(use_wayback and yr >= since_year),
                            cfg=cfg)
                if key and key in idx:
                    # this row's own key belongs to another paper (its entry
                    # failed the month check here): never shadow it -- see doc_key
                    key = doc_key(row)
                if r and key:
                    hit = {"key": key, "title_norm": normalize_title(row.get("title") or ""),
                           **r, "date_precision": "day", "resolved_at": date.today().isoformat()}
                    idx[key] = hit
                    new_count += 1
                    if idx_fh is not None:
                        idx_fh.write(json.dumps(hit, ensure_ascii=False) + "\n")
                        idx_fh.flush()
            elif hit is not None:
                counts["from_index"] += 1
            if hit:
                counts[hit["date_source"]] = counts.get(hit["date_source"], 0) + 1
                by_id[row["doc_id"]] = {"date": hit["date"], "date_source": hit["date_source"]}
            else:
                counts["unresolved"] += 1
            if counts["candidates"] % 100 == 0:
                print(f"wp-dates: {counts['candidates']} scanned, {len(by_id)} resolved "
                      f"({new_count} new) …", file=sys.stderr, flush=True)
    finally:
        if idx_fh is not None:
            idx_fh.close()

    print(f"wp-dates: {dict(counts)} (resolved {len(by_id)} / {counts['candidates']} candidates)")

    if csv_path or not write:
        out = csv_path or str(cfg.reports_dir / "wp_dates.csv")
        cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["doc_id", "new_date", "date_source"])
            for did, c in by_id.items():
                w.writerow([did, c["date"], c["date_source"]])
        print(f"wrote {len(by_id)} resolution(s) -> {out}", file=sys.stderr)

    if write and by_id:
        updates: dict[str, dict] = {}
        for row in storage.iter_manifest():
            c = by_id.get(row.get("doc_id"))
            if c is not None:
                row["date"] = c["date"]
                row["year"] = int(c["date"][:4])
                row["date_precision"] = "day"
                row["date_source"] = c["date_source"]
                updates[row["doc_id"]] = row
        applied = apply_row_updates(cfg, updates)
        print(f"wp-dates: applied {applied} day-precision date(s) to the manifest; "
              f"{new_count} new index entr(y/ies) appended to {index_path(cfg)}",
              file=sys.stderr)
    return counts
