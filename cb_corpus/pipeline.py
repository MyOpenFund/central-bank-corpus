"""Orchestration: discover -> (download) -> manifest, then completeness."""
from __future__ import annotations

import json
import sys
from datetime import date
from typing import Iterable, Iterator, Optional

from .adapters.base import get_adapter
from .banks import BIS_63
from .config import Config
from .http import Fetcher
from .models import DocRecord
from .runreport import RunReport
from .sources.bis_speeches import BISSpeechIndex, parse_detail
from .sources.recovery import Source
from .storage import Storage
from .taxonomy import DocType, FULL_SCOPE


def _make_storage(config: Optional[Config] = None,
                  html_to_pdf: Optional[bool] = None) -> tuple[Config, Fetcher, Storage]:
    """Build the (cfg, fetcher, storage) triplet every entry point needs.

    `html_to_pdf` overrides the config flag (e.g. False to keep HTML-only without
    spawning Chrome). Centralises what used to be ~9 copy-pasted blocks.
    """
    cfg = config or Config()
    if html_to_pdf is not None:
        cfg.html_to_pdf = html_to_pdf
    fetcher = Fetcher(cfg)
    return cfg, fetcher, Storage(cfg, fetcher)


def run_source(source: Source, *, dry_run: bool = False,
               config: Optional[Config] = None) -> dict[str, int]:
    """Drive a `Source`: make storage, enumerate its items, save them.

    The single uniform recovery entry point — a new official source only needs to
    implement `Source.items`, not re-derive the Storage/dedup/save plumbing.
    """
    cfg, fetcher, storage = _make_storage(config, source.html_to_pdf)
    return storage.save_many(source.items(fetcher, storage),
                             dry_run=dry_run, label=source.label)


def _record_discovery_errors(cfg: Config, errors: list[dict]) -> None:
    """Append discovery failures to data/discovery_errors.jsonl (audit trail)."""
    if not errors:
        return
    path = cfg.data_dir / "discovery_errors.jsonl"
    with path.open("a") as fh:
        for e in errors:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def _discover_and_save_bank(code: str, fetcher: Fetcher, storage: Storage,
                            scope: tuple[DocType, ...], since: Optional[date],
                            dry_run: bool, native_only: bool,
                            report: Optional[RunReport]) -> tuple[dict[str, int], list[dict]]:
    """One bank's discover_all -> save_many, optionally folding the outcome
    into `report`'s per-source stats. Raises on adapter/storage failure —
    callers decide whether that's fatal (report is None) or per-bank
    recoverable (report is not None, caller wraps in try/except)."""
    adapter = get_adapter(code, fetcher)
    # Let native discovery skip documents already known by URL (incl.
    # alt_urls registered by migrations/data fixes, e.g. WP v3's native-URL
    # registration or the ECB D3 double-slash legacy rows) so a native-first
    # bank doesn't re-download/re-fetch its back-catalogue. Every native
    # branch reads this hook (see adapters/base.py `discover()`); C1 (shared
    # BIS index) and non-native RePEc D1/D2 are unaffected.
    adapter._skip_known_url = storage.is_known_url
    recs = adapter.discover_all(scope=scope, since=since, native_only=native_only)
    counts = storage.save_many(recs, dry_run=dry_run, label=code)
    if report is not None:
        s = report.source(code)
        s.record_saved_counts(counts)
        for e in adapter.errors:
            s.record_fetch_error(f"{e.get('context', '?')}: {e.get('error', str(e))}" if isinstance(e, dict) else str(e))
    return counts, adapter.errors


def run(bank_codes: Optional[Iterable[str]] = None,
        scope: tuple[DocType, ...] = FULL_SCOPE,
        since: Optional[date] = None,
        dry_run: bool = True,
        config: Optional[Config] = None,
        max_rounds: int = 1,
        native_only: bool = False,
        report: Optional[RunReport] = None) -> dict[str, dict[str, int]]:
    """Crawl + (optionally) download. dry_run=True only indexes URLs.

    With ``max_rounds > 1`` the crawl repeats until a round downloads nothing
    new AND reports no errors (download + discovery) AND no bank crashed, or
    the cap is reached. Because saving is idempotent (dedup on doc_id +
    sha256), re-running only fills gaps left by transient failures — this is
    what makes a full rebuild converge to completeness instead of silently
    stopping one blip short.

    Note: when ``report`` is given, its per-source stats ACCUMULATE across
    every round (a history of everything seen this run), not just the final
    round's outcome — a bank that crashed in round 1 and succeeded in round 2
    still shows round 1's crash in the report, by design (it happened).

    Returns {bank_code: {status: count}} for the last round. Any unresolved
    discovery failures are written to ``data/discovery_errors.jsonl``.
    """
    cfg, fetcher, storage = _make_storage(config)
    codes = list(bank_codes) if bank_codes else [b.code for b in BIS_63]
    results: dict[str, dict[str, int]] = {}
    rounds = 1 if dry_run else max(1, max_rounds)

    round_no = 0
    last_disc_errors: list[dict] = []
    for round_no in range(1, rounds + 1):
        round_saved = round_errors = round_crashes = 0
        last_disc_errors = []
        for code in codes:
            if report is not None:
                try:
                    counts, errs = _discover_and_save_bank(
                        code, fetcher, storage, scope, since, dry_run,
                        native_only, report)
                except Exception as exc:  # noqa: BLE001 — per-bank recoverable
                    report.source(code).record_fetch_error(
                        f"adapter crashed: {exc}", truncated=True)
                    round_crashes += 1
                    continue
            else:
                # report is None: a crash here is NOT caught -- it propagates
                # out of run() entirely, so there is no round to keep clean
                # or retry; only the report-gated branch above needs the
                # crash counter.
                counts, errs = _discover_and_save_bank(
                    code, fetcher, storage, scope, since, dry_run,
                    native_only, report)
            results[code] = counts
            round_saved += counts.get("saved", 0)
            round_errors += counts.get("error", 0)
            last_disc_errors.extend(errs)
        _record_discovery_errors(cfg, last_disc_errors)
        if rounds > 1:
            print(f"[round {round_no}/{rounds}] saved={round_saved} "
                  f"errors={round_errors} discovery_failures={len(last_disc_errors)} "
                  f"crashes={round_crashes}",
                  file=sys.stderr, flush=True)
        if dry_run:
            break
        if (round_saved == 0 and round_errors == 0 and round_crashes == 0
                and not last_disc_errors):
            break

    if last_disc_errors or (not dry_run and round_errors):
        print(f"!! INCOMPLETE after {round_no} round(s): "
              f"{len(last_disc_errors)} discovery failure(s) remain — see "
              f"{cfg.data_dir / 'discovery_errors.jsonl'}. Re-run to fill.",
              file=sys.stderr, flush=True)
    return results


def run_bis_sitemap(since: Optional[date] = None,
                    until: Optional[date] = None,
                    only_banks: Optional[set[str]] = None,
                    dry_run: bool = True,
                    config: Optional[Config] = None,
                    max_per_year: Optional[int] = None,
                    report: Optional[RunReport] = None) -> dict[str, int]:
    """Discover C1 speeches from BIS yearly sitemaps and (optionally) download.

    Single-pass across all 63 banks at once — far faster than per-bank C1
    discovery because we only walk each yearly sitemap once. Returns a global
    {status: count} dict.
    """
    cfg, fetcher, storage = _make_storage(config)
    bis = BISSpeechIndex(fetcher)
    stats = report.source("bis-sitemap") if report is not None else None
    recs: Iterator[DocRecord] = bis.discover(
        since=since, until=until, only_banks=only_banks,
        max_per_year=max_per_year,
        skip_url=storage.is_known_url,
        stats=stats,
    )
    counts = storage.save_many(recs, dry_run=dry_run, label="bis-sitemap")
    if stats is not None: stats.record_saved_counts(counts)
    return counts


def _disk_missing_index(storage: Storage, raw,
                        *, only_banks: Optional[set[str]] = None,
                        doc_types: Optional[set[str]] = None,
                        min_year: Optional[int] = None,
                        max_year: Optional[int] = None) -> dict[str, "object"]:
    """Map ``doc_id -> Path`` for on-disk PDFs not yet in the manifest.

    The on-disk filename stem *is* the cb_corpus ``doc_id``, so this is exactly
    the set of documents whose bytes are present but whose manifest row is gone.
    """
    missing: dict[str, object] = {}
    for bank_dir in sorted(p for p in raw.iterdir() if p.is_dir()):
        if only_banks and bank_dir.name not in only_banks:
            continue
        for dt_dir in sorted(p for p in bank_dir.iterdir() if p.is_dir()):
            if doc_types and dt_dir.name.upper() not in doc_types:
                continue
            for year_dir in dt_dir.iterdir():
                if not year_dir.is_dir() or not year_dir.name.isdigit():
                    continue
                year = int(year_dir.name)
                if (min_year and year < min_year) or (max_year and year > max_year):
                    continue
                for f in year_dir.glob("*.pdf"):
                    if f.stem not in storage._ids:
                        missing[f.stem] = f
    return missing


def _apply_reindex(recs: Iterable[DocRecord], storage: Storage,
                   missing: dict, *, dry_run: bool) -> dict[str, int]:
    """Reindex each discovered record whose ``doc_id`` is an on-disk missing file.

    Mutates ``missing`` (pops matched ids) so the caller can report what stayed
    unmatched. Records not on disk are ignored (we only index files we have).
    """
    counts: dict[str, int] = {"matched": 0, "reindexed": 0, "skip": 0}
    for rec in recs:
        path = missing.get(rec.doc_id)
        if path is None:
            continue
        counts["matched"] += 1
        status = storage.reindex(rec, path, dry_run=dry_run).split(":")[0]
        counts[status] = counts.get(status, 0) + 1
        missing.pop(rec.doc_id, None)
    return counts


def reindex_native_from_disk(bank_codes: Optional[Iterable[str]] = None,
                             scope: tuple[DocType, ...] = FULL_SCOPE,
                             since: Optional[date] = None,
                             dry_run: bool = True,
                             config: Optional[Config] = None,
                             min_year: Optional[int] = None,
                             max_year: Optional[int] = None) -> dict[str, int]:
    """Rebuild manifest rows for on-disk *native-adapter* docs missing from it.

    Re-runs each bank's adapter discovery (listing pages only — **no PDF
    downloads**) and matches every discovered :class:`DocRecord` to an on-disk
    file by ``doc_id``. Recovers the exact publication date AND the real title
    for documents scraped from bank sites (C1 speeches, A1/A2/A3 decisions &
    minutes, E reports, …) — i.e. everything that did NOT come from BIS sitemaps
    or RePEc. Matching requires the bank site to still expose the same URLs the
    files were scraped from; anything that no longer resolves stays unmatched
    (reported, never silently dropped). ``dry_run`` writes nothing.
    """
    cfg, fetcher, storage = _make_storage(config, html_to_pdf=False)
    doc_types = {dt.code for dt in scope}
    missing = _disk_missing_index(
        storage, cfg.raw_dir,
        only_banks=set(bank_codes) if bank_codes else None,
        doc_types=doc_types, min_year=min_year, max_year=max_year,
    )
    n0 = len(missing)
    counts = {"missing_on_disk": n0, "matched": 0, "reindexed": 0,
              "skip": 0, "unmatched": 0}
    if not n0:
        print("reindex(native): no unindexed docs on disk for this scope",
              file=sys.stderr, flush=True)
        return counts

    codes = list(bank_codes) if bank_codes else [b.code for b in BIS_63]
    print(f"reindex(native): {n0} unindexed docs on disk; re-running discovery "
          f"for {len(codes)} bank(s) (listing pages only, no PDF downloads)…",
          file=sys.stderr, flush=True)
    disc_errors: list[dict] = []
    try:
        for code in codes:
            try:
                adapter = get_adapter(code, fetcher)
                c = _apply_reindex(adapter.discover_all(scope=scope, since=since),
                                   storage, missing, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 — one bad bank never aborts the run
                print(f"  {code}: discovery failed: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
                continue
            for k, v in c.items():
                counts[k] = counts.get(k, 0) + v
            disc_errors.extend(getattr(adapter, "errors", []))
    finally:
        # Duplicate-content files: remember their URL on the owning row (B1).
        storage.flush_duplicate_stamps("reindex(native)")
    _record_discovery_errors(cfg, disc_errors)
    counts["unmatched"] = len(missing)
    print(f"reindex(native): matched={counts['matched']} "
          f"reindexed={counts.get('reindexed', 0)} dry_run={dry_run} "
          f"unmatched={len(missing)} (unmatched = URL no longer exposed by the "
          f"bank site, or non-native source)", file=sys.stderr, flush=True)
    return counts


def reindex_bis_from_disk(only_banks: Optional[set[str]] = None,
                          dry_run: bool = True,
                          config: Optional[Config] = None,
                          fetch_titles: bool = False,
                          min_year: Optional[int] = None,
                          max_year: Optional[int] = None) -> dict[str, int]:
    """Rebuild manifest rows for C1 speeches whose PDF is on disk but unindexed.

    Use when the manifest was reset/lost while downloaded PDFs accumulated: the
    files are present but their rich metadata (exact publication date, URL) is
    gone. This recovers it **without re-downloading anything** — it fetches only
    the BIS yearly sitemaps (a few dozen XML files), and matches each sitemap URL
    to an on-disk file by recomputing the stable ``doc_id = sha1(bank|C1|url)``
    (the on-disk folder already tells us the bank). The exact date comes from the
    URL slug (``r<YYMMDD>``).

    ``fetch_titles`` additionally fetches each *matched* speech's detail page for
    a human title (one HTTP request per matched file — slower; off by default).
    ``dry_run`` reports matches without writing to the manifest.
    """
    import hashlib

    cfg, fetcher, storage = _make_storage(config, html_to_pdf=False)
    raw = cfg.raw_dir

    # 1. On-disk C1 PDFs whose doc_id is not in the manifest, bucketed by year.
    missing_by_year: dict[int, dict[str, tuple[str, "object"]]] = {}
    banks_by_year: dict[int, set[str]] = {}
    n_missing = 0
    for bank_dir in sorted(p for p in raw.iterdir() if p.is_dir()):
        bank = bank_dir.name
        if only_banks and bank not in only_banks:
            continue
        c1_dir = bank_dir / "C1"
        if not c1_dir.is_dir():
            continue
        for year_dir in c1_dir.iterdir():
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue
            year = int(year_dir.name)
            if (min_year and year < min_year) or (max_year and year > max_year):
                continue
            for f in year_dir.glob("*.pdf"):
                doc_id = f.stem
                if doc_id in storage._ids:
                    continue
                missing_by_year.setdefault(year, {})[doc_id] = (bank, f)
                banks_by_year.setdefault(year, set()).add(bank)
                n_missing += 1

    counts = {"missing_on_disk": n_missing, "matched": 0, "reindexed": 0,
              "skip": 0, "unmatched": 0}
    if not n_missing:
        print("reindex: no unindexed C1 PDFs on disk — nothing to do",
              file=sys.stderr, flush=True)
        return counts

    print(f"reindex: {n_missing} unindexed C1 PDFs across {len(missing_by_year)} "
          f"year(s); fetching BIS sitemaps (no PDF downloads)…",
          file=sys.stderr, flush=True)

    # 2. Walk the BIS yearly sitemaps for exactly those years; match by hash.
    bis = BISSpeechIndex(fetcher)
    try:
        sitemaps = {y: u for y, u in bis.list_years()}
    except Exception as exc:  # noqa: BLE001
        print(f"reindex: could not fetch sitemap index: {exc}",
              file=sys.stderr, flush=True)
        sitemaps = {}

    try:
        for year in sorted(missing_by_year):
            bucket = missing_by_year[year]
            banks = banks_by_year[year]
            url = sitemaps.get(year)
            if url is None:
                continue  # year not covered by BIS sitemaps (e.g. pre-1996)
            try:
                metas = bis.speeches_for_year(year, url)
            except Exception:  # noqa: BLE001 — one bad year never aborts the run
                continue
            for meta in metas:
                if not bucket:
                    break
                for bank in banks:
                    cand = hashlib.sha1(
                        f"{bank}|C1|{meta.pdf_url}".encode("utf-8")).hexdigest()[:16]
                    hit = bucket.get(cand)
                    if hit is None or hit[0] != bank:
                        continue
                    counts["matched"] += 1
                    title = ""
                    if fetch_titles:
                        try:
                            title, _ = parse_detail(fetcher.get_text(meta.detail_url))
                        except Exception:  # noqa: BLE001
                            title = ""
                    rec = DocRecord(
                        bank_code=bank, doc_type=DocType.C1,
                        title=title or meta.pdf_url, pdf_url=meta.pdf_url,
                        source_url=meta.detail_url, date=meta.date,
                        provenance="bis_index", mime_type="application/pdf",
                    )
                    status = storage.reindex(rec, hit[1], dry_run=dry_run).split(":")[0]
                    counts[status] = counts.get(status, 0) + 1
                    del bucket[cand]
                    break
    finally:
        # Duplicate-content files: remember their URL on the owning row (B1).
        storage.flush_duplicate_stamps("reindex(bis)")

    counts["unmatched"] = sum(len(b) for b in missing_by_year.values())
    print(f"reindex: matched={counts['matched']} "
          f"reindexed={counts.get('reindexed', 0)} dry_run={dry_run} "
          f"unmatched={counts['unmatched']} (unmatched = on disk but not found in "
          f"BIS sitemaps — pre-1996 or non-BIS-sourced)",
          file=sys.stderr, flush=True)
    return counts


def run_repec(bank_codes: Optional[Iterable[str]] = None,
              dry_run: bool = True,
              config: Optional[Config] = None,
              incremental: bool = False,
              report: Optional[RunReport] = None) -> dict[str, dict[str, int]]:
    """Discover + (optionally) download RePEc working papers (D1/D2) for every
    SERIES-wired bank, following IDEAS pagination so the full back-catalogue is
    captured (not just the ~200 newest per series).

    One pass per bank, idempotent (dedup on doc_id + sha256), so re-running only
    fills gaps. Papers already known by their IDEAS source URL are skipped
    BEFORE the per-paper fetch in BOTH modes (collision-free by construction:
    the tested URL IS the record's own source_url). With ``incremental=True``
    (nightly mode), a series' pagination additionally stops at the first
    listing page that is fully known (the weekly full sweep omits this to
    still catch backfills, keeping full pagination for completeness). Returns
    {bank_code: {status: count}}.
    """
    from .sources.repec import RePEcDiscovery, SERIES
    cfg, fetcher, storage = _make_storage(config)
    rep = RePEcDiscovery(fetcher)
    codes = list(bank_codes) if bank_codes else list(SERIES.keys())
    results: dict[str, dict[str, int]] = {}
    for code in codes:
        if code not in SERIES:
            continue
        if report is not None:
            try:
                counts = storage.save_many(
                    rep.discover_bank(
                        code,
                        # Always skip by source_url: in RePEc the tested URL IS the
                        # record's own source_url (one paper page, one record), so
                        # this pre-fetch skip is collision-free by construction, in
                        # both modes. stop_on_known stays incremental-only: full
                        # sweeps keep full pagination for completeness, they just
                        # stop re-fetching pages of papers already owned.
                        skip_url=storage.is_known_source_url,
                        stop_on_known=incremental,
                        # Threaded down to _series_paper_pages: a listing-page
                        # fetch failure is recorded as truncation, never a
                        # silent "last page reached" (see repec.py).
                        stats=report.source(code)),
                    dry_run=dry_run, label=f"repec:{code}")
            except Exception as exc:  # noqa: BLE001 — per-bank recoverable
                report.source(code).record_fetch_error(
                    f"adapter crashed: {exc}", truncated=True)
                continue
            results[code] = counts
            report.source(code).record_saved_counts(counts)
        else:
            results[code] = storage.save_many(
                rep.discover_bank(
                    code,
                    skip_url=storage.is_known_source_url,
                    stop_on_known=incremental),
                dry_run=dry_run, label=f"repec:{code}")
    return results


def run_wayback_recovery(bank_code: str, url_prefix: str, doc_type: DocType,
                         title_fn=None, dry_run: bool = True,
                         config: Optional[Config] = None) -> dict[str, int]:
    """Recover official PDFs the bank took offline, from the Wayback Machine.

    Lists archived PDFs under `url_prefix` (CDX), and saves one record per paper
    with `pdf_url` = the original (dead) bank URL [for citation + stable doc_id]
    and the Wayback raw-snapshot URL as the fallback Storage actually downloads.
    Date is the year embedded in the URL; `provenance="wayback"`.
    """
    from .sources.wayback import WaybackSource
    return run_source(WaybackSource(bank_code, url_prefix, doc_type, title_fn),
                      dry_run=dry_run, config=config)


def run_repec_wayback_recovery(bank_code: str, dry_run: bool = False,
                               config: Optional[Config] = None,
                               report: Optional[RunReport] = None) -> dict[str, int]:
    """Recover RePEc working papers whose official PDF is dead, via the Wayback
    Machine, keying on each paper's EXACT url (for opaque-path sources like the
    Riksbank). Re-discovers the bank, skips papers already saved, and for the
    missing ones adds the archived snapshot as a fallback Storage downloads.
    Dates/titles come from IDEAS; provenance is set to "wayback".

    With `report` given, a `SourceStats` (keyed `repec-wb:<bank_code>`) is
    threaded down into the re-discovery walk so a listing-page fetch failure
    is recorded as truncation, exactly like `run_repec` — this re-discovery
    is IDEAS pagination too, so it must not go silent just because it has no
    `report` wired by default (`report=None` preserves that prior silence).
    """
    from .sources.repec import RePEcDiscovery
    from .sources.wayback import wayback_for_url
    cfg, fetcher, storage = _make_storage(config)
    rep = RePEcDiscovery(fetcher)
    stats = report.source(f"repec-wb:{bank_code}") if report is not None else None

    def _recs() -> Iterator[DocRecord]:
        for rec in rep.discover_bank(bank_code, stats=stats):
            # Skip by URL (not doc_id): already-saved papers — incl. ones recovered
            # earlier with a different date precision — share the same pdf_url, so
            # this avoids both re-pinging Wayback and creating date-mismatch dupes.
            if storage.is_known_url(rec.pdf_url) or rec.doc_id in storage._ids:
                continue
            wb = wayback_for_url(fetcher, rec.pdf_url)
            if wb:
                rec.alt_urls = list(rec.alt_urls) + [wb]
                rec.provenance = "wayback"
            yield rec

    return storage.save_many(_recs(), dry_run=dry_run, label=f"repec-wb:{bank_code}")


def run_boe_wp_recovery(years=None, dry_run: bool = True,
                        config: Optional[Config] = None) -> dict[str, int]:
    """Fetch Bank of England staff working papers (D1) from the BoE's own sitemap
    — the live official source, covering papers IDEAS only has dead URLs for.
    Skips papers already in the manifest (by URL)."""
    from .sources.boe_wp import sitemap_pages, paper_pdf
    cfg, fetcher, storage = _make_storage(config)

    def _recs() -> Iterator[DocRecord]:
        for d, page, derived in sitemap_pages(fetcher, years):
            if storage.is_known_url(derived):
                continue                       # already have it (= the IDEAS media URL)
            got = paper_pdf(fetcher, page)     # read the page for the REAL pdf link
            if got is None:
                continue
            title, pdf = got
            if storage.is_known_url(pdf):
                continue
            yield DocRecord(
                bank_code="gb", doc_type=DocType.D1, title=title,
                pdf_url=pdf, date=d, provenance="bank_site",
                mime_type="application/pdf",
            )

    return storage.save_many(_recs(), dry_run=dry_run, label="boe-wp")


def run_boe_recovery(sitemap_path: str, doc_type: DocType, href_filter: str,
                     years=None, dry_run: bool = True,
                     config: Optional[Config] = None) -> dict[str, int]:
    """Generic Bank of England recovery from a sitemap section (the live official
    source). Works for any doc type — minutes (A3), MP report (E1), FSR (E2)…
    A page's linked PDF is taken when present, else the HTML page is the artifact
    (rendered to PDF by Storage). Skips docs already in the manifest (by URL)."""
    from .sources.boe_wp import doc_pages, page_doc
    cfg, fetcher, storage = _make_storage(config)

    def _recs() -> Iterator[DocRecord]:
        for d, page in doc_pages(fetcher, sitemap_path, href_filter, years):
            got = page_doc(fetcher, page)
            if got is None:
                continue
            title, pdf = got
            url = pdf or page              # PDF if linked, else the HTML page
            if storage.is_known_url(url):
                continue
            yield DocRecord(
                bank_code="gb", doc_type=doc_type, title=title,
                pdf_url=url, source_url=page, date=d, provenance="bank_site",
                mime_type="application/pdf" if pdf else "",
            )

    return storage.save_many(_recs(), dry_run=dry_run, label=f"boe:{doc_type.code}")


def run_listing_pdf_recovery(bank_code: str, doc_type: DocType, listing_urls,
                             path_regex: str, title: str, url_template=None,
                             mime: str = "application/pdf", dry_run: bool = True,
                             config: Optional[Config] = None) -> dict[str, int]:
    """Generic recovery: scrape listing page(s) and save matching PDFs.

    Two modes:
      - default: `path_regex` group 1 = full PDF path, group 2 = YYYYMMDD.
      - `url_template` set: `path_regex` group 1 = YYYYMMDD, and the PDF URL is
        `url_template.format(d=<date8>)` (for date-constructable archives the page
        only lists via JS / a landing page, e.g. press-conference transcripts).
    Matches the RAW HTML (not just <a href>); skips PDFs already in the manifest."""
    import re
    from datetime import date as _date
    from urllib.parse import urljoin
    rx = re.compile(path_regex, re.I)
    cfg, fetcher, storage = _make_storage(config)

    def _recs() -> Iterator[DocRecord]:
        seen: set[str] = set()
        for lu in listing_urls:
            try:
                html = fetcher.get_text(lu)
            except Exception:
                continue
            html = html.replace("\\/", "/")    # unescape JSON-embedded URLs (JS archives)
            for m in rx.finditer(html):
                ds = m.group(1) if url_template else m.group(2)
                try:
                    d = _date(int(ds[:4]), int(ds[4:6]), int(ds[6:8]))
                except (ValueError, IndexError):
                    continue
                url = urljoin(lu, url_template.format(d=ds) if url_template else m.group(1))
                if url in seen or storage.is_known_url(url):
                    continue
                seen.add(url)
                yield DocRecord(
                    bank_code=bank_code, doc_type=doc_type,
                    title=f"{title} {d.isoformat()}", pdf_url=url, date=d,
                    provenance="bank_site", mime_type=mime,
                )

    return storage.save_many(_recs(), dry_run=dry_run, label=f"{bank_code}:{doc_type.code}")


def run_ecb_pub_recovery(section: str, doc_type: DocType, mime: str = "application/pdf",
                         since_year: int = 1997, cdx_fallback_prefix: Optional[str] = None,
                         date_fmt: str = "auto", name_filter: Optional[str] = None,
                         dry_run: bool = False,
                         config: Optional[Config] = None) -> dict[str, int]:
    """Recover an ECB publication section.

    PRIMARY: the bank's per-section/year static includes
    (`/press/<section>/date/<year>/html/index_include.en.html`) — canonical, no JS.
    FALLBACK (kept on purpose for resilience): Wayback CDX enumeration under
    `cdx_fallback_prefix`, used only when the section serves no includes.
    `mime=""` saves HTML as-is (no Chrome render), e.g. for interviews.
    """
    import re
    from .sources.ecb_pub import section_include_docs, date_from_url, ECB
    from .sources.wayback import cdx_pdfs, raw_url
    cfg, fetcher, storage = _make_storage(config, html_to_pdf=(False if not mime else None))
    cur = date.today().year
    exts = (".en.pdf",) if mime else (".en.html", ".en.pdf")

    def _emit(url, d, alt=None):
        return DocRecord(bank_code="ecb", doc_type=doc_type,
                         title=f"ECB {doc_type.name} {d.isoformat()}",
                         pdf_url=url, alt_urls=alt or [], date=d,
                         provenance="bank_site", mime_type=mime)

    def _recs() -> Iterator[DocRecord]:
        seen: set[str] = set()
        # Probe a recent year: does this section serve per-year includes at all?
        # (Avoids hammering ~30 missing-include URLs before falling back.)
        serves_includes = any(section_include_docs(fetcher, section, y, exts=exts) is not None
                              for y in (cur, cur - 1))
        if serves_includes:                                   # PRIMARY
            for year in range(since_year, cur + 1):
                for u in section_include_docs(fetcher, section, year, exts=exts) or []:
                    u = u.split("?")[0]
                    if u in seen or storage.is_known_url(u):
                        continue
                    seen.add(u)
                    d = date_from_url(u, date_fmt)
                    if d:
                        yield _emit(u, d)
        elif cdx_fallback_prefix:                             # FALLBACK: legacy CDX method
            nf = re.compile(name_filter, re.I) if name_filter else None
            cdx_mime = "application/pdf" if mime else "text/html"
            en_ext = "en.pdf" if mime else "en.html"
            for o, t in cdx_pdfs(fetcher, cdx_fallback_prefix, mimetype=cdx_mime):
                o = o.split("?")[0]
                fn = o.rsplit("/", 1)[-1]
                if not fn.lower().endswith(en_ext):            # English-language docs only
                    continue
                if nf and not nf.search(fn):
                    continue
                if o in seen or storage.is_known_url(o):
                    continue
                seen.add(o)
                d = date_from_url(o, date_fmt)
                if d:
                    yield _emit(o, d, alt=[raw_url(o, t)])

    return storage.save_many(_recs(), dry_run=dry_run, label=f"ecb:{doc_type.code}")
