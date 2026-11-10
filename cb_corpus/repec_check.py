"""WP v3 phase 2 — RePEc/IDEAS completeness audit (never downloads).

Enumerates each wired IDEAS series (``SERIES`` in sources/repec.py) and asks: is
any paper missing from our manifest? Matching a RePEc paper to a manifest row,
in cascade:

  - **handle** — the IDEAS handle (e.g. RePEc:ecb:ecbwps:20253124) appears in a
    manifest row's ``source_url``/``repec_handle`` (exact for RePEc-sourced rows);
  - **key** — the bank-specific number (wp_migrate ``_KEY_FROM_HANDLE``) matches a
    native row's key (covers the 5 banks migrated off RePEc);
  - **title** — exact normalized title equality (last resort, unambiguous only).

Leftovers (RePEc papers no manifest row covers) are reported as `missing`, split
into `missing_recent` (within the last ~45 days → native discovery just hasn't
caught up / re-run it) and `missing_legacy` (older → ingest via RePEc + date
recovery). The reverse — manifest rows with no RePEc paper — is reported too
(`pending_repec` if recent, else `unmatched_old`). Stdout + CSV; no writes.
"""
from __future__ import annotations

import csv
import re
import sys
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from .banks import get_bank
from .config import Config
from .http import Fetcher
from .runreport import SourceStats
from .sources.repec import (IDEAS, SERIES, _paper_meta, cap_truncation,
                            extract_pdf_candidates, listing_error_truncation,
                            resolve_max_pages)
from .storage import Storage
from .wp_migrate import (_KEY_FROM_HANDLE, _KEY_FROM_PDF, match_key_url,
                         normalize_title, repec_handle_from_source_url)

_GRACE_DAYS = 45
_APPROVE_TOKENS = {"x", "yes", "1", "oui"}


def parse_series_listing(html: str, arch: str, series: str) -> list[tuple[str, str]]:
    """[(paper_id, title)] for an IDEAS series listing page. De-duped by id, keeping
    the longest title seen (skips the short 'By citations/downloads' sort links)."""
    soup = BeautifulSoup(html, "lxml")
    pat = re.compile(rf"/p/{re.escape(arch)}/{re.escape(series)}/([^/.]+)\.html$")
    best: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        m = pat.search(a["href"])
        if not m:
            continue
        pid = m.group(1)
        t = a.get_text(" ", strip=True)
        if t.lower().startswith(("by ", "sorted")):   # sort-order links, not titles
            t = ""
        # keep every paper id (completeness); upgrade to the longest real title seen
        if pid not in best or len(t) > len(best[pid]):
            best[pid] = t
    return list(best.items())


def enumerate_series(fetcher: Fetcher, handle: str,
                     max_pages: Optional[int] = None,
                     stats: Optional[SourceStats] = None
                     ) -> list[tuple[str, str]]:
    """All (paper_id, title) for an IDEAS series, following pagination.

    The walk ends cleanly when a page yields nothing new, or on an HTTP 404
    for page k > 1 that page k-1 does not link to
    (IDEAS answers 404 on the page after the last one; see
    `sources.repec.listing_error_truncation`). Any other listing-page fetch
    failure (404 on page 1, 404 on a page that page k-1 advertises, network
    error, 5xx, ...) also ends the walk (skipping a page would lose ordering
    guarantees), but with `stats` given it is recorded via
    `record_fetch_error(..., truncated=True)` first, so it is never
    indistinguishable from a genuinely completed listing. `stats=None` (the
    default) preserves the prior silent-break behavior for callers not yet
    wired to a run-report. `max_pages` defaults to the fetcher's
    `cfg.repec_max_pages` (200 without a cfg); reaching it is a truncation only
    when the last page read still links to the next one, else a clean end.
    """
    max_pages = resolve_max_pages(fetcher, max_pages)
    arch, series = handle.split(":")
    base = f"{IDEAS}/s/{arch}/{series}"
    seen: dict[str, str] = {}
    prev_html: Optional[str] = None
    for page in range(1, max_pages + 1):
        url = f"{base}.html" if page == 1 else f"{base}{page}.html"
        try:
            html = fetcher.get_text(url)
        except Exception as exc:
            msg = listing_error_truncation(exc, page, prev_html, series)
            if msg is not None and stats is not None:
                stats.record_fetch_error(msg, truncated=True)
            break
        prev_html = html
        rows = parse_series_listing(html, arch, series)
        new = [(pid, t) for pid, t in rows if pid not in seen]
        if not new:
            break
        for pid, t in new:
            seen[pid] = t
    else:
        msg = cap_truncation(prev_html, series, max_pages)
        if msg is not None and stats is not None:
            stats.record_fetch_error(msg, truncated=True)
    return list(seen.items())


def _entry_key(key_handle, arch: str, series: str, pid: str):
    """Bank join key for an IDEAS listing entry. Passes the full
    ``arch:series:pid`` form: every handle extractor in ``_KEY_FROM_HANDLE``
    reads it (``se_handle_number`` needs the ``hhs:`` archive prefix; the bare
    ``series:pid`` form gave no key for se, and none for jp before boj_code
    learnt to split on ":")."""
    return key_handle(f"{arch}:{series}:{pid}") if key_handle else None


def _row_date(row: dict) -> Optional[date]:
    """Row date; a month-precision ``YYYY-MM`` reads as its first day."""
    d = (row.get("date") or "").strip()
    if len(d) == 7:
        d += "-01"
    try:
        return date.fromisoformat(d[:10])
    except ValueError:
        return None


def run_repec_check(bank_codes: Optional[Iterable[str]] = None,
                    csv_path: Optional[str] = None,
                    config: Optional[Config] = None,
                    reverse_csv_path: Optional[str] = None,
                    grace_days: int = _GRACE_DAYS) -> dict[str, dict]:
    """Audit RePEc coverage for the requested banks (default: all wired). Never
    downloads, never writes the manifest. Returns {bank: summary}; writes a CSV of
    missing papers (RePEc -> manifest) and a CSV of manifest rows RePEc does not
    list (reverse: `pending_repec` within `grace_days`, `pre_repec` when dated
    before the bank's earliest row RePEc does list, else `unmatched_old`).
    `grace_days` is the one grace window of the design (decision Q4): it also
    splits the forward `missing_recent` / `missing_legacy` buckets.
    The reverse check only covers a bank's doc types that have a RePEc series, and
    is skipped (flagged `reverse_skipped_truncated`) when a listing was truncated
    (including reaching `max_pages` on a page that still advertises a next
    page), or came back empty. Leftover page fetches that fail are
    counted in `pagefetch_errors` (rows they would have matched may then read as
    unmatched)."""
    if grace_days < 0:
        raise ValueError(f"grace_days must be >= 0, got {grace_days}")
    cfg = config or Config()
    fetcher = Fetcher(cfg)
    storage = Storage(cfg, fetcher)
    codes = [c for c in (bank_codes or SERIES.keys()) if c in SERIES]
    cutoff = date.today() - timedelta(days=grace_days)

    results: dict[str, dict] = {}
    missing_rows: list[dict] = []
    reverse_rows: list[dict] = []
    for bank in codes:
        # Manifest coverage sets for this bank: handles, native keys, normalized
        # titles, and normalized PDF/alt URLs (the last catches non-RePEc-sourced
        # rows, e.g. es BdE-repository copies, when we fetch a leftover's page).
        handles: set[str] = set()
        keys: set = set()
        titles: set[str] = set()
        urls: set[str] = set()
        manifest_n = 0
        key_pdf, key_handle = _KEY_FROM_PDF.get(bank), _KEY_FROM_HANDLE.get(bank)
        # Reverse check: the rows RePEc should list (doc types with a series) and
        # each match key -> doc_ids, so every tier that matches marks its rows.
        series_types = {dt.code for _, dt in SERIES[bank]}
        rev_rows: dict[str, dict] = {}
        by_h: dict[str, set] = {}
        by_k: dict = {}
        by_t: dict[str, set] = {}
        by_u: dict[str, set] = {}
        matched: set[str] = set()
        truncated_any = False
        for row in storage.iter_manifest(bank):
            if row.get("doc_type") not in ("D1", "D2"):
                continue
            manifest_n += 1
            did = row.get("doc_id") or ""
            track = row.get("doc_type") in series_types and bool(did)
            if track:
                rev_rows[did] = row
            h = (row.get("repec_handle") or repec_handle_from_source_url(row.get("source_url") or ""))
            if h:
                handles.add(h)
                if track:
                    by_h.setdefault(h, set()).add(did)
            if key_pdf:
                k = key_pdf(row.get("pdf_url") or "") or (key_handle and key_handle(row.get("source_url") or ""))
                if k:
                    keys.add(k)
                    if track:
                        by_k.setdefault(k, set()).add(did)
            t = normalize_title(row.get("title") or "")
            if t:
                titles.add(t)
                if track:
                    by_t.setdefault(t, set()).add(did)
            for u in [row.get("pdf_url"), *(row.get("alt_urls") or [])]:
                if u:
                    urls.add(match_key_url(u))
                    if track:
                        by_u.setdefault(match_key_url(u), set()).add(did)

        summary = {"repec_total": 0, "covered": 0, "recovered_pagefetch": 0,
                   "missing_recent": 0, "missing_legacy": 0, "manifest_total": manifest_n,
                   "pending_repec": 0, "pre_repec": 0, "unmatched_old": 0,
                   "reverse_skipped_truncated": 0, "pagefetch_errors": 0}
        bank_home = get_bank(bank).homepage
        for handle, doc_type in SERIES[bank]:
            arch, series = handle.split(":")
            leftovers: list[tuple[str, str, str]] = []
            stats = SourceStats(f"repec_check:{bank}")
            entries = enumerate_series(fetcher, handle, stats=stats)
            if not entries and not stats.truncated:
                truncated_any = True
                print(f"WARNING: IDEAS listing for {handle} returned no paper",
                      file=sys.stderr)
            if stats.truncated:
                truncated_any = True
                sample = stats.error_samples[-1] if stats.error_samples else ""
                print(f"WARNING: IDEAS listing truncated for {handle}: {sample}",
                     file=sys.stderr)
            for pid, title in entries:
                summary["repec_total"] += 1
                full_handle = f"RePEc:{arch}:{series}:{pid}"
                k = _entry_key(key_handle, arch, series, pid)
                nt = normalize_title(title) if title else ""
                matched |= by_h.get(full_handle, set())
                if k:
                    matched |= by_k.get(k, set())
                if nt:
                    matched |= by_t.get(nt, set())
                covered = full_handle in handles
                if not covered and key_handle:
                    covered = bool(k and k in keys)
                if not covered and title:
                    covered = nt in titles
                if covered:
                    summary["covered"] += 1
                else:
                    leftovers.append((pid, title, full_handle))
            # Second pass (cheap — only the leftovers): fetch each unmatched paper's
            # IDEAS page and re-match by the bank PDF URL or the full canonical
            # title. Catches rows the listing title/handle missed.
            for pid, title, full_handle in leftovers:
                recovered = False
                try:
                    html = fetcher.get_text(f"{IDEAS}/p/{arch}/{series}/{pid}.html")
                    ftitle, _ = _paper_meta(html)
                    cands = extract_pdf_candidates(html, bank_home)
                    nft = normalize_title(ftitle) if ftitle else ""
                    for c in cands:
                        matched |= by_u.get(match_key_url(c), set())
                    if nft:
                        matched |= by_t.get(nft, set())
                    recovered = (any(match_key_url(c) in urls for c in cands)
                                 or bool(nft and nft in titles))
                except Exception:
                    recovered = False
                    summary["pagefetch_errors"] += 1
                if recovered:
                    summary["covered"] += 1
                    summary["recovered_pagefetch"] += 1
                    continue
                yr = int(pid[:4]) if pid[:4].isdigit() and 1990 <= int(pid[:4]) <= cutoff.year + 1 else 0
                recent = yr >= cutoff.year
                summary["missing_recent" if recent else "missing_legacy"] += 1
                missing_rows.append({"bank": bank, "handle": full_handle,
                                     "title": title, "bucket": "recent" if recent else "legacy"})
        if summary["pagefetch_errors"]:
            print(f"WARNING: {bank}: {summary['pagefetch_errors']} IDEAS paper page(s) "
                  f"failed to load; rows they would match may read as unmatched",
                  file=sys.stderr)
        if truncated_any:
            summary["reverse_skipped_truncated"] = 1
        else:
            # RePEc era floor, from the data: the earliest dated row RePEc lists.
            # Older unmatched rows (e.g. de 1995-2011, es 1978-1997 bank-site
            # papers) predate the series on IDEAS: `pre_repec`, not a gap signal.
            era = [d for d in (_row_date(rev_rows[i]) for i in matched if i in rev_rows) if d]
            floor = min(era) if era else None
            for did, row in rev_rows.items():
                if did in matched:
                    continue
                d = _row_date(row)
                if d and d >= cutoff:
                    bucket = "pending_repec"
                elif d and floor and d < floor:
                    bucket = "pre_repec"
                else:
                    bucket = "unmatched_old"
                summary[bucket] += 1
                reverse_rows.append({"bank": bank, "doc_id": did,
                                     "doc_type": row.get("doc_type", ""),
                                     "date": row.get("date", ""),
                                     "title": row.get("title", ""),
                                     "pdf_url": row.get("pdf_url", ""), "bucket": bucket})
        results[bank] = summary
        print(f"{bank}: {summary}")

    out = csv_path or str(cfg.reports_dir / "repec_check.csv")
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["bank", "handle", "title", "bucket"])
        w.writeheader()
        for r in missing_rows:
            w.writerow(r)
    print(f"wrote {len(missing_rows)} missing paper(s) -> {out}", file=sys.stderr)
    rout = reverse_csv_path or str(cfg.reports_dir / "repec_check_reverse.csv")
    with open(rout, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["bank", "doc_id", "doc_type", "date",
                                           "title", "pdf_url", "bucket"])
        w.writeheader()
        for r in reverse_rows:
            w.writerow(r)
    print(f"wrote {len(reverse_rows)} manifest row(s) RePEc does not list -> {rout}",
          file=sys.stderr)
    return results


def _build_indexes(storage: Storage, bank: str) -> dict:
    """Index a bank's manifest rows for the reconcile walk: full RePEc handle,
    bank-specific number key, normalized title, and normalized pdf/alt URL
    (each -> list of rows), plus the set of source_urls already claimed and
    the bank's key_handle helper. Also returns the FULL row list verbatim
    (kept verbatim so _apply_stamps can build its keyed updates from the same
    rows the matching phase saw)."""
    by_handle: dict[str, list[dict]] = {}
    by_key: dict = {}
    by_title: dict[str, list[dict]] = {}
    by_url: dict[str, list[dict]] = {}
    known_sources: set[str] = set()
    all_rows: list[dict] = []
    key_pdf, key_handle = _KEY_FROM_PDF.get(bank), _KEY_FROM_HANDLE.get(bank)

    for row in storage.iter_manifest(bank):
        all_rows.append(row)
        if row.get("doc_type") not in ("D1", "D2"):
            continue
        h = (row.get("repec_handle")
             or repec_handle_from_source_url(row.get("source_url") or ""))
        if h:
            by_handle.setdefault(h, []).append(row)
        if key_pdf:
            k = key_pdf(row.get("pdf_url") or "") or (
                key_handle and key_handle(row.get("source_url") or ""))
            if k:
                by_key.setdefault(k, []).append(row)
        t = normalize_title(row.get("title") or "")
        if t:
            by_title.setdefault(t, []).append(row)
        for u in [row.get("pdf_url"), *(row.get("alt_urls") or [])]:
            if u:
                by_url.setdefault(match_key_url(u), []).append(row)
        src = row.get("source_url")
        if src:
            known_sources.add(src)

    return {"by_handle": by_handle, "by_key": by_key, "by_title": by_title,
            "by_url": by_url, "known_sources": known_sources,
            "all_rows": all_rows, "key_handle": key_handle}


def _walk_entries(bank: str, fetcher: Fetcher, idx: dict, bank_home: str):
    """Yield one classification dict per IDEAS listing entry for ``bank``,
    via the SAME matching cascade documented on ``run_repec_reconcile``
    (never fuzzy, never dates): already-known short-circuit, first pass
    (handle / bank-specific key / exact normalized title, no fetch), second
    pass (only if the first found nothing: fetch the IDEAS paper page,
    re-try PDF-URL candidates + fetched canonical title).

    Each yielded dict: ``{"ideas_url", "title" (fetched canonical title if
    the second pass ran and found one, else the listing title), "action",
    "doc_id", "row"}`` where ``action`` is one of:

      - ``"already"``: the IDEAS URL (or a matched row) already carries a
        source_url; no write is ever possible here.
      - ``"unmatched"``: zero candidate rows found by the cascade.
      - ``"ambiguous"``: more than one candidate row found.
      - ``"candidate"``: EXACTLY one candidate row, with an EMPTY
        source_url -- eligible to be written, but reverse-ambiguity (two
        listing entries independently resolving to the same doc_id) is a
        cross-entry concern the CALLER must still resolve (see
        ``run_repec_reconcile``); this generator does not track run-wide
        claimed doc_ids.
    """
    by_handle, by_key = idx["by_handle"], idx["by_key"]
    by_title, by_url = idx["by_title"], idx["by_url"]
    known_sources, key_handle = idx["known_sources"], idx["key_handle"]

    for handle, doc_type in SERIES[bank]:
        arch, series = handle.split(":")
        stats = SourceStats(f"repec_reconcile:{bank}")
        entries = enumerate_series(fetcher, handle, stats=stats)
        if stats.truncated:
            sample = stats.error_samples[-1] if stats.error_samples else ""
            print(f"WARNING: IDEAS listing truncated for {handle}: {sample}",
                 file=sys.stderr)
        for pid, title in entries:
            ideas_url = f"{IDEAS}/p/{arch}/{series}/{pid}.html"
            full_handle = f"RePEc:{arch}:{series}:{pid}"
            handle_rows = by_handle.get(full_handle) or []

            if ideas_url in known_sources or any(r.get("source_url") for r in handle_rows):
                doc_id = handle_rows[0]["doc_id"] if handle_rows else ""
                yield {"ideas_url": ideas_url, "title": title, "action": "already",
                       "doc_id": doc_id, "row": handle_rows[0] if handle_rows else None}
                continue

            cand_by_id: dict[str, dict] = {r["doc_id"]: r for r in handle_rows}
            key = _entry_key(key_handle, arch, series, pid)
            if key is not None:
                for r in by_key.get(key, []):
                    cand_by_id[r["doc_id"]] = r
            for r in by_title.get(normalize_title(title), []):
                cand_by_id[r["doc_id"]] = r

            fetched_title = ""
            if not cand_by_id:
                # Second pass (cheap -- only unmatched entries): fetch the
                # IDEAS page and re-match by its bank PDF-URL candidates
                # (matched against pdf_url/alt_urls) or its canonical title.
                try:
                    html = fetcher.get_text(ideas_url)
                    fetched_title, _ = _paper_meta(html)
                    cands = extract_pdf_candidates(html, bank_home)
                except Exception:
                    fetched_title, cands = "", []
                for c in cands:
                    for r in by_url.get(match_key_url(c), []):
                        cand_by_id[r["doc_id"]] = r
                if fetched_title:
                    for r in by_title.get(normalize_title(fetched_title), []):
                        cand_by_id[r["doc_id"]] = r

            csv_title = fetched_title or title
            candidates = list(cand_by_id.values())
            if not candidates:
                yield {"ideas_url": ideas_url, "title": csv_title, "action": "unmatched",
                       "doc_id": "", "row": None}
            elif len(candidates) > 1:
                yield {"ideas_url": ideas_url, "title": csv_title, "action": "ambiguous",
                       "doc_id": "", "row": None}
            else:
                row = candidates[0]
                if row.get("source_url"):
                    yield {"ideas_url": ideas_url, "title": csv_title, "action": "already",
                           "doc_id": row["doc_id"], "row": row}
                else:
                    yield {"ideas_url": ideas_url, "title": csv_title, "action": "candidate",
                           "doc_id": row["doc_id"], "row": row}


def _apply_stamps(storage: Storage, all_rows: list[dict], stamps: dict[str, str]) -> None:
    """Stamp ``source_url`` onto every row of ``all_rows`` whose doc_id is a
    key of ``stamps``, then apply exactly those rows via the lock-protected
    keyed-update path (``Storage.rewrite_manifest``). Shared by
    ``run_repec_reconcile`` (phase 1) and ``run_reconcile_apply`` (phase 2,
    human-approved). Rows outside ``stamps`` are untouched on disk, and rows
    appended concurrently survive (issue #18). No-op when ``stamps`` is empty."""
    if not stamps:
        return
    updates: dict[str, dict] = {}
    for row in all_rows:
        did = row.get("doc_id")
        if did in stamps:
            row["source_url"] = stamps[did]
            updates[did] = row
    storage.rewrite_manifest(updates)


def run_repec_reconcile(bank_codes: Optional[Iterable[str]] = None,
                        write: bool = False,
                        csv_path: Optional[str] = None,
                        config: Optional[Config] = None,
                        fetcher: Optional[Fetcher] = None) -> dict[str, dict]:
    """One-shot reconciliation: stamp ``source_url = <IDEAS paper URL>`` onto
    manifest rows that uniquely match an IDEAS listing entry but carry no
    source_url of their own (spec §3 — the gb-style 374 dead-URL rows: native
    bank_site D1 rows under modern slug URLs, with no cross-reference to the
    RePEc entry that reproves them on every catalog pass).

    Matching cascade per listing entry: see ``_walk_entries``'s docstring
    (handle / bank-specific key / exact normalized title, then a second-pass
    page fetch — never fuzzy, never dates).

    Write rule (strict): a listing entry stamps its match ONLY when exactly
    one candidate row was found (across both passes) AND that row's
    source_url is empty (``_walk_entries``' ``"candidate"`` action). Zero
    candidates -> ``unmatched``. More than one -> ``ambiguous``. One
    candidate with a source_url already set -> ``already``. A doc_id already
    claimed by an earlier listing entry IN THIS RUN (reverse ambiguity: two
    distinct listing entries each uniquely resolve to the same row) -> the
    first entry stamps, every later one is ``ambiguous`` too — a doc_id can
    be stamped by at most one entry per run, so ``stamped`` counts, the CSV,
    and rows actually rewritten always agree. Ambiguous/unmatched/already
    rows are reported (CSV + stdout), never written. Default is dry-run
    (``write=False``); writes go through the shared ``_apply_stamps`` (full
    row set, see its docstring), touching ONLY ``source_url`` on the stamped
    doc_ids. Idempotent: once a row is stamped its IDEAS URL is a known
    source_url, so a second run finds it ``already`` and stamps 0.

    Returns {bank: {"stamped": n, "ambiguous": n, "already": n, "unmatched": n}}.
    Always writes a CSV (dry-run included) of every action taken, with rows
    ``{bank, ideas_url, action, doc_id, title}`` (doc_id/title empty when no
    single row was involved, e.g. ambiguous/unmatched).
    """
    cfg = config or Config()
    fetcher = fetcher or Fetcher(cfg)
    storage = Storage(cfg, fetcher)
    codes = [c for c in (bank_codes or SERIES.keys()) if c in SERIES]

    results: dict[str, dict] = {}
    csv_rows: list[dict] = []

    for bank in codes:
        idx = _build_indexes(storage, bank)
        bank_home = get_bank(bank).homepage
        counts = {"stamped": 0, "ambiguous": 0, "already": 0, "unmatched": 0}
        stamps: dict[str, str] = {}   # doc_id -> ideas_url

        for entry in _walk_entries(bank, fetcher, idx, bank_home):
            action = entry["action"]
            ideas_url, title, doc_id = entry["ideas_url"], entry["title"], entry["doc_id"]

            if action == "already":
                counts["already"] += 1
                csv_rows.append({"bank": bank, "ideas_url": ideas_url,
                                 "action": "already", "doc_id": doc_id, "title": title})
            elif action == "unmatched":
                counts["unmatched"] += 1
                csv_rows.append({"bank": bank, "ideas_url": ideas_url,
                                 "action": "unmatched", "doc_id": "", "title": title})
            elif action == "ambiguous":
                counts["ambiguous"] += 1
                csv_rows.append({"bank": bank, "ideas_url": ideas_url,
                                 "action": "ambiguous", "doc_id": "", "title": title})
            else:  # "candidate": exactly one row, empty source_url
                if doc_id in stamps:
                    # Reverse ambiguity: a DIFFERENT listing entry already
                    # claimed this doc_id earlier in this run (e.g. two
                    # listing pids sharing one normalized title, each
                    # otherwise uniquely resolving to the same row). A
                    # doc_id can be stamped by at most one entry, or the
                    # dict write below is last-wins and the CSV would
                    # assert two writes for one actual write. The first
                    # entry keeps its stamp; this later one is reported
                    # (not written) so stamped == len(stamps) always.
                    counts["ambiguous"] += 1
                    csv_rows.append({"bank": bank, "ideas_url": ideas_url,
                                     "action": "ambiguous", "doc_id": "", "title": title})
                else:
                    counts["stamped"] += 1
                    stamps[doc_id] = ideas_url
                    csv_rows.append({"bank": bank, "ideas_url": ideas_url,
                                     "action": "stamp", "doc_id": doc_id, "title": title})

        results[bank] = counts
        print(f"{bank}: {counts}")

        if write:
            _apply_stamps(storage, idx["all_rows"], stamps)

    out = csv_path or str(cfg.reports_dir / "repec_reconcile.csv")
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["bank", "ideas_url", "action", "doc_id", "title"])
        w.writeheader()
        for r in csv_rows:
            w.writerow(r)
    print(f"wrote {len(csv_rows)} row(s) -> {out}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# --propose / --apply-csv: human-approved reconciliation for title drift
# (recovery phase-2 design, 2026-07-16, §B2).
# ---------------------------------------------------------------------------

_PROPOSE_CSV_FIELDS = ("bank", "ideas_url", "repec_title", "candidate_doc_id",
                      "candidate_title", "score", "approve")
_APPLY_CSV_FIELDS = ("bank", "ideas_url", "doc_id", "title", "action", "skip_reason")


def run_reconcile_propose(bank_codes: Optional[Iterable[str]] = None,
                          csv_path: Optional[str] = None,
                          config: Optional[Config] = None,
                          fetcher: Optional[Fetcher] = None) -> dict[str, dict]:
    """``--propose``: the gb-style RePEc/published title-drift cases (spec
    §B2) cannot be matched by any stable key, and the strict cascade in
    ``_walk_entries`` correctly refuses to guess — they surface as
    ``unmatched``. This makes the human the key: for every ``unmatched``
    listing entry, rank up to 3 CANDIDATE manifest rows for the same bank
    (D1/D2, empty source_url) by ``difflib.SequenceMatcher.ratio()`` on
    ``normalize_title`` outputs.

    The similarity score is used ONLY to ORDER the proposal — this function
    holds no ``write`` parameter, never calls ``storage.rewrite_manifest`` or
    ``_apply_stamps``, and touches no manifest bytes: it is architecturally
    incapable of writing. Entries whose candidate pool is empty (no eligible
    row at all for that bank) emit ONE CSV row with empty candidate fields
    rather than being silently dropped.

    Always writes a CSV (columns: bank, ideas_url, repec_title,
    candidate_doc_id, candidate_title, score, approve — ``approve`` left
    EMPTY for Marc to fill in with one of ``x|yes|1|oui``). Returns
    ``{bank: {"unmatched": n}}`` (count of unmatched listing entries seen,
    NOT of CSV rows — an entry can contribute 0..3 rows).
    """
    cfg = config or Config()
    fetcher = fetcher or Fetcher(cfg)
    storage = Storage(cfg, fetcher)
    codes = [c for c in (bank_codes or SERIES.keys()) if c in SERIES]

    results: dict[str, dict] = {}
    csv_rows: list[dict] = []

    for bank in codes:
        idx = _build_indexes(storage, bank)
        bank_home = get_bank(bank).homepage
        pool = [(row, normalize_title(row.get("title") or ""))
                for row in idx["all_rows"]
                if row.get("doc_type") in ("D1", "D2") and not row.get("source_url")]

        n_unmatched = 0
        for entry in _walk_entries(bank, fetcher, idx, bank_home):
            if entry["action"] != "unmatched":
                continue
            n_unmatched += 1
            repec_title = entry["title"]
            norm_repec = normalize_title(repec_title)
            scored = sorted(
                ((SequenceMatcher(None, norm_repec, norm_cand).ratio(), row)
                 for row, norm_cand in pool),
                key=lambda pair: pair[0], reverse=True,
            )
            top = scored[:3]
            if not top:
                csv_rows.append({"bank": bank, "ideas_url": entry["ideas_url"],
                                 "repec_title": repec_title, "candidate_doc_id": "",
                                 "candidate_title": "", "score": "", "approve": ""})
            else:
                for score, row in top:
                    csv_rows.append({"bank": bank, "ideas_url": entry["ideas_url"],
                                     "repec_title": repec_title,
                                     "candidate_doc_id": row["doc_id"],
                                     "candidate_title": row.get("title") or "",
                                     "score": round(score, 3), "approve": ""})
        results[bank] = {"unmatched": n_unmatched}
        print(f"{bank}: unmatched={n_unmatched}")

    out = csv_path or str(cfg.reports_dir / "repec_reconcile_propose.csv")
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(_PROPOSE_CSV_FIELDS))
        w.writeheader()
        for r in csv_rows:
            w.writerow(r)
    print(f"wrote {len(csv_rows)} row(s) -> {out}", file=sys.stderr)
    return results


def _load_bank_state(storage: Storage, bank: str) -> dict:
    """Live per-bank manifest snapshot for ``run_reconcile_apply``: the FULL
    row list (needed verbatim for ``_apply_stamps``'s full-row-set rewrite),
    a doc_id index, the set of source_urls already known BEFORE this apply
    run, and this run's own claim sets (doc_ids stamped, ideas_urls
    claimed) — re-enforced per approved row so every phase-1 invariant holds
    at apply time, not just at propose time."""
    all_rows = list(storage.iter_manifest(bank))
    return {
        "all_rows": all_rows,
        "by_doc_id": {r.get("doc_id"): r for r in all_rows},
        "known_sources": {r.get("source_url") for r in all_rows if r.get("source_url")},
        "stamps": {},
        "claimed_ideas_urls": set(),
    }


def run_reconcile_apply(apply_csv: str, write: bool = False,
                        csv_path: Optional[str] = None,
                        config: Optional[Config] = None,
                        fetcher: Optional[Fetcher] = None) -> dict:
    """``--apply-csv``: reads a ``--propose`` CSV Marc has edited and stamps
    EXACTLY the pairs he approved. The similarity ``score`` column is never
    read here — only ``bank``/``ideas_url``/``candidate_doc_id`` (the human's
    decision) and ``approve`` (``x|yes|1|oui``, case/space-insensitive) drive
    this function, so a stray high-similarity row that Marc did NOT mark can
    never be written.

    This CSV is human-edited by hand, so it is treated as UNTRUSTED input,
    not a trusted machine artifact — every field is guarded before it can
    influence a write:

      - ``bank`` is stripped of surrounding whitespace before use (a stray
        space must not turn a valid bank into an unknown one); if a bank
        has NO manifest rows at all (typo, or genuinely empty), every row
        for it still gets the accurate ``row-gone`` skip reason, but ONE
        stderr warning (``unknown or empty bank '<x>' in apply CSV``) is
        also printed per distinct bad bank, so a systematic mistake is
        loud rather than silently absorbed into a pile of row-gone rows;
      - ``ideas_url`` must start with ``f"{IDEAS}/p/"`` (the only shape a
        real IDEAS paper page URL can take) else ``bad-ideas-url`` —
        this also closes an idempotence hole: an EMPTY ideas_url used to
        pass every other guard and get stamped as ``source_url=""``
        (falsy), so a re-apply of the same CSV saw ``source_url`` still
        "empty" and stamped again, forever — never converging;
      - the target row's ``doc_type`` must be ``D1``/``D2`` else
        ``bad-doc-type`` (a typo'd or hand-edited ``candidate_doc_id``
        must never stamp a row the reconciliation scope excludes).

    Every phase-1 invariant is ALSO re-validated against the LIVE manifest
    at apply time (never trusted from the propose pass, which may be
    stale), per approved row, in CSV order (first line wins ties — same
    rule as phase 1's reverse-ambiguity):

      - the doc_id still exists in the bank's manifest, else ``row-gone``;
      - its ``source_url`` is still empty, else ``source-not-empty`` (also
        what an idempotent re-apply of the same CSV hits: already stamped);
      - the doc_id was not already stamped by an earlier approved row IN
        THIS run, else ``duplicate-doc-id``;
      - the ideas_url was not already claimed by an earlier approved row IN
        THIS run, NOR already a known source_url elsewhere in the manifest
        (a stamp from phase 1 or an earlier apply run counts too), else
        ``duplicate-ideas-url``.

    An unapproved row is reported ``not-approved`` and never touched.

    The CSV is opened with ``encoding="utf-8-sig"`` so a BOM (common when a
    spreadsheet app re-saves the propose CSV) is stripped rather than
    corrupting the first column's header/value.

    Default is dry-run (``write=False``): the report CSV still shows every
    row that WOULD be applied/skipped (so it doubles as a preview), but
    ``_apply_stamps``/``storage.rewrite_manifest`` is never called — the
    manifest is byte-for-byte untouched. With ``write=True``, stamps go
    through the shared ``_apply_stamps`` (full row set, same as phase 1)
    once per bank touched. Returns ``{"applied": n, "skipped": n}``. Always
    writes a report CSV (columns: bank, ideas_url, doc_id, title, action,
    skip_reason) — ``applied`` lines in it equal actual manifest writes
    whenever ``write=True``.
    """
    cfg = config or Config()
    fetcher = fetcher or Fetcher(cfg)
    storage = Storage(cfg, fetcher)

    with open(apply_csv, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    counts = {"applied": 0, "skipped": 0}
    out_rows: list[dict] = []
    bank_states: dict[str, dict] = {}
    warned_banks: set[str] = set()

    for r in rows:
        bank = (r.get("bank") or "").strip()
        ideas_url = r.get("ideas_url") or ""
        doc_id = r.get("candidate_doc_id") or ""
        title = r.get("candidate_title") or ""
        approve = (r.get("approve") or "").strip().lower()

        if approve not in _APPROVE_TOKENS:
            counts["skipped"] += 1
            out_rows.append({"bank": bank, "ideas_url": ideas_url, "doc_id": doc_id,
                             "title": title, "action": "skipped", "skip_reason": "not-approved"})
            continue

        if not ideas_url.startswith(f"{IDEAS}/p/"):
            counts["skipped"] += 1
            out_rows.append({"bank": bank, "ideas_url": ideas_url, "doc_id": doc_id,
                             "title": title, "action": "skipped", "skip_reason": "bad-ideas-url"})
            continue

        state = bank_states.setdefault(bank, _load_bank_state(storage, bank))
        if not state["all_rows"] and bank not in warned_banks:
            warned_banks.add(bank)
            print(f"[repec-reconcile apply] unknown or empty bank '{bank}' in apply CSV",
                 file=sys.stderr)

        row = state["by_doc_id"].get(doc_id) if doc_id else None

        if row is None:
            reason = "row-gone"
        elif row.get("doc_type") not in ("D1", "D2"):
            reason = "bad-doc-type"
        elif row.get("source_url"):
            reason = "source-not-empty"
        elif doc_id in state["stamps"]:
            reason = "duplicate-doc-id"
        elif ideas_url in state["claimed_ideas_urls"] or ideas_url in state["known_sources"]:
            reason = "duplicate-ideas-url"
        else:
            reason = ""

        if reason:
            counts["skipped"] += 1
            out_rows.append({"bank": bank, "ideas_url": ideas_url, "doc_id": doc_id,
                             "title": title, "action": "skipped", "skip_reason": reason})
            continue

        state["stamps"][doc_id] = ideas_url
        state["claimed_ideas_urls"].add(ideas_url)
        counts["applied"] += 1
        out_rows.append({"bank": bank, "ideas_url": ideas_url, "doc_id": doc_id,
                         "title": title, "action": "applied", "skip_reason": ""})

    if write:
        for bank, state in bank_states.items():
            _apply_stamps(storage, state["all_rows"], state["stamps"])

    out = csv_path or str(cfg.reports_dir / "repec_reconcile_apply.csv")
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(_APPLY_CSV_FIELDS))
        w.writeheader()
        for row in out_rows:
            w.writerow(row)
    print(f"[repec-reconcile apply] applied={counts['applied']} skipped={counts['skipped']} -> {out}",
         file=sys.stderr)
    return counts
