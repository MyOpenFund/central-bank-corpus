"""CLI:  python -m cb_corpus <command>

  list-banks                       show the 63 target banks
  discover [--banks us,ecb] [--types A3,F1] [--since 2015-01-01] [--download]
  bis-sitemap [--years 2024-2025] [--banks gb,jp] [--download] [--max-per-year N]
  report   [--years 2015-2025] [--banks ...] [--csv path]
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from .banks import BIS_63
from .completeness import build_matrix, export_csv, summarize
from .config import Config
from .convert import convert_existing
from .pipeline import (run, run_bis_sitemap, run_repec,
                       reindex_bis_from_disk, reindex_native_from_disk)
from .retry_html import retry_failed
from .runreport import RunReport
from .taxonomy import FULL_SCOPE, by_code

# Work commands that build a RunReport, feed it through the pipeline, and
# ALWAYS write it (even on crash) before returning the doctrine exit code.
# Non-work commands (list-banks, dry tables, migrations, ...) are unaffected.
REPORTING_CMDS = {"discover", "bis-sitemap", "repec"}


def _runs_path() -> Path:
    """Where the run-report lands: CB_DATA_DIR override (tests), else
    Config's own data_dir. Config itself doesn't honor an env var for
    data_dir today, so CB_DATA_DIR is this CLI's own override, not a
    pre-existing Config mechanism."""
    base = os.environ.get("CB_DATA_DIR")
    data_dir = Path(base) if base else Config().data_dir
    return data_dir / "runs.jsonl"


def _date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _years(s: str) -> list[int]:
    if "-" in s:
        a, b = s.split("-")
        return list(range(int(a), int(b) + 1))
    return [int(s)]


def _types(s: str) -> tuple:
    if not s:
        return FULL_SCOPE
    return tuple(by_code(c.strip()) for c in s.split(",") if c.strip())


def _banks(s: str) -> list[str] | None:
    return [c for c in s.split(",") if c] or None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cb_corpus")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-banks")

    d = sub.add_parser("discover", help="Run per-bank adapter discovery.")
    d.add_argument("--banks", default="")
    d.add_argument("--types", default="", help="e.g. A3,F1 (default: full A-F)")
    d.add_argument("--since", type=_date, default=None)
    d.add_argument("--download", action="store_true",
                   help="actually fetch PDFs (default is dry-run: index only)")
    d.add_argument("--rounds", type=int, default=3,
                   help="re-crawl up to N times until no new docs and no errors "
                        "(idempotent; fills transient-failure gaps). Use a higher "
                        "value for a full rebuild. Ignored for dry-run.")
    d.add_argument("--native-only", action="store_true",
                   help="skip shared catalogs (BIS index, RePEc) — bank-site "
                        "sources only; the sync job's catalog phase owns those")

    b = sub.add_parser("bis-sitemap",
                       help="Single-pass discovery of all C1 speeches via BIS sitemaps.")
    b.add_argument("--years", default="", help="YYYY or YYYY-YYYY")
    b.add_argument("--banks", default="", help="restrict to bank codes")
    b.add_argument("--download", action="store_true")
    b.add_argument("--max-per-year", type=int, default=None,
                   help="cap entries per year (smoke-test only)")

    rp = sub.add_parser("repec",
                        help="Discover/download RePEc working papers (D1/D2) with "
                             "IDEAS pagination, for all SERIES-wired banks.")
    rp.add_argument("--banks", default="", help="restrict to bank codes")
    rp.add_argument("--download", action="store_true",
                    help="actually fetch PDFs (default is dry-run: index only)")
    rp.add_argument("--incremental", action="store_true",
                    help="skip papers already known by their IDEAS page and stop "
                         "each series at the first fully-known listing page "
                         "(nightly mode; the weekly full sweep omits this)")

    rx = sub.add_parser("reindex-from-disk",
                        help="Rebuild manifest rows for on-disk docs missing from "
                             "the manifest, by replaying discovery (recovers exact "
                             "dates/titles; downloads no PDFs).")
    rx.add_argument("--source", choices=["native", "bis-sitemap"], default="native",
                    help="discovery to replay: native per-bank adapters (default, "
                         "covers C1/A/B/E/F from bank sites) or BIS sitemaps (C1)")
    rx.add_argument("--banks", default="", help="restrict to bank codes")
    rx.add_argument("--types", default="", help="native only: e.g. C1,A3 (default full A-F)")
    rx.add_argument("--years", default="", help="YYYY or YYYY-YYYY (restrict)")
    rx.add_argument("--write", action="store_true",
                    help="actually append manifest rows (default: dry-run report)")
    rx.add_argument("--titles", action="store_true",
                    help="bis-sitemap only: fetch each matched speech's detail page "
                         "for a human title (one HTTP request per matched file; slower)")

    so = sub.add_parser("sweep-orphans",
                        help="Quarantine on-disk files that have no manifest row and "
                             "duplicate an indexed document (moved to raw_orphans/, "
                             "mirror tree); report the rest. Dry-run unless --move.")
    so.add_argument("--banks", default="", help="restrict to bank codes")
    so.add_argument("--move", action="store_true",
                    help="actually move duplicates (default: dry-run, report only)")
    so.add_argument("--progress-every", type=int, default=1000,
                    help="stderr progress line every N files examined (default 1000)")

    r = sub.add_parser("report")
    r.add_argument("--banks", default="")
    r.add_argument("--years", type=_years, default=_years("2015-2025"))
    r.add_argument("--csv", default="")

    c = sub.add_parser("convert-html",
                       help="Render every text/html artifact to PDF (Chrome).")
    c.add_argument("--dry-run", action="store_true",
                   help="Show what would be converted without running Chrome.")

    sub.add_parser("retry-html",
                   help="Retry every still-failed text/html with escalating "
                        "strategies; write data/failed_urls.txt at the end.")

    wm = sub.add_parser("wp-migrate",
                        help="WP v3: replace RePEc YYYY-MM-01 dates on existing "
                             "D1/D2 rows with native bank-site day dates. Default "
                             "dry-run (report + CSV, no writes); --write applies "
                             "the metadata fixes in place. Never downloads PDFs.")
    wm.add_argument("--banks", default="", help="restrict to wired banks (e.g. ecb)")
    wm.add_argument("--csv", default="", help="CSV output path (default data/reports/wp_migrate.csv)")
    wm.add_argument("--write", action="store_true",
                    help="apply the date fixes to the manifest (metadata only: "
                         "date/precision/source/handle/alt_urls; doc_id unchanged)")

    cm = sub.add_parser("c2-migrate",
                        help="One-shot enrichment of ecb C2 rows from the foedb DB "
                             "(titles/dates/alt_urls). Dry-run by default.")
    cm.add_argument("--write", action="store_true",
                    help="apply the enrichment to the manifest (metadata only: "
                         "title/date/precision/source/alt_urls; doc_id/pdf_url unchanged)")

    wd = sub.add_parser("wp-dates",
                        help="WP v3 phase 4: recover the publication DAY for legacy "
                             "month/year-precision D1/D2 rows via PDF /CreationDate + "
                             "Wayback (month-constrained), into the committed "
                             "data/wp_dates_index.jsonl. Default dry-run; --write applies.")
    wd.add_argument("--banks", default="", help="restrict to banks (default: ecb,us,jp,gb,de)")
    wd.add_argument("--csv", default="", help="CSV output path (default data/reports/wp_dates.csv)")
    wd.add_argument("--write", action="store_true",
                    help="apply recovered days to the manifest + append to the index")
    wd.add_argument("--no-wayback", action="store_true",
                    help="PDF /CreationDate only — skip the (slow) Wayback rung")
    wd.add_argument("--apply-only", action="store_true",
                    help="replay the committed index onto the manifest (no recovery, "
                         "no network) — what a fresh clone runs to materialise dates")

    rc = sub.add_parser("repec-check",
                        help="WP v3 phase 2: audit RePEc/IDEAS coverage vs the manifest "
                             "(missing papers per bank). Never downloads.")
    rc.add_argument("--banks", default="", help="restrict to wired banks (default: all)")
    rc.add_argument("--csv", default="", help="CSV output path (default data/reports/repec_check.csv)")
    rc.add_argument("--reverse-csv", default="",
                    help="CSV of manifest rows RePEc does not list "
                         "(default data/reports/repec_check_reverse.csv)")
    rc.add_argument("--grace-days", type=int, default=45,
                    help="a manifest row RePEc does not list yet is `pending_repec` "
                         "within this many days of its date, else `unmatched_old` (default 45)")

    rr = sub.add_parser("repec-reconcile",
                        help="Stamp IDEAS source_urls onto uniquely-matched manifest "
                             "rows (dry-run by default; --write applies). "
                             "--propose / --apply-csv: human-approved reconciliation "
                             "for RePEc/published title drift (spec §B2).")
    rr.add_argument("--banks", default="")
    rr.add_argument("--write", action="store_true",
                    help="plain mode: apply the strict-cascade stamps; "
                         "with --apply-csv: apply the human-approved stamps "
                         "(without it, --apply-csv is a dry-run report); "
                         "invalid together with --propose (propose never writes)")
    rr.add_argument("--csv", default="",
                    help="output report CSV path (default data/reports/repec_reconcile.csv, "
                         "or repec_reconcile_propose.csv / repec_reconcile_apply.csv "
                         "for --propose / --apply-csv respectively)")
    rr.add_argument("--propose", action="store_true",
                    help="write a ranked-candidate CSV for every unmatched listing "
                         "entry (up to 3 candidates each, approve column left empty "
                         "for a human to fill in) -- never writes manifests")
    rr.add_argument("--apply-csv", default="",
                    help="path to a --propose CSV a human has edited (approve in "
                         "x|yes|1|oui); stamps exactly the approved pairs, "
                         "re-validated against the live manifest. Guards (the CSV "
                         "is untrusted human input): rows are skipped with "
                         "bad-ideas-url (ideas_url doesn't look like an IDEAS paper "
                         "page) or bad-doc-type (target row isn't D1/D2); --banks "
                         "is invalid together with --apply-csv (the bank comes from "
                         "each CSV row); --csv must not be the same path as "
                         "--apply-csv (would overwrite the decision record you are "
                         "reading)")

    rdl = sub.add_parser("recover-downloads",
                        help="Inventory-driven Wayback recovery for documents that "
                             "failed every prior download attempt "
                             "(data/download_errors.jsonl). Default dry-run (CSV "
                             "classification only); --download saves recoverable ones.")
    rdl.add_argument("--banks", default="")
    rdl.add_argument("--download", action="store_true",
                     help="actually save recoverable documents (default: dry-run report)")
    rdl.add_argument("--csv", default="", help="CSV output path (default data/reports/recover_downloads.csv)")
    rdl.add_argument("--candidates", default="",
                     help="JSONL of externally-recovered docs ({dead_pdf_url, file_path, "
                          "recovered_from: wayback|bank_site|mirror, final_url, title?}) -- "
                          "switches to candidates-only mode, no CDX walk; --download registers "
                          "them into the corpus via Storage.reindex")
    rdl.add_argument("--seed-quarantine", default="",
                     help="JSONL of {url, reason?} lines to seed as already-quarantined "
                          "(for docs confirmed unrecoverable after a manual hunt) -- may be "
                          "combined with --candidates or used alone")

    cw = sub.add_parser("cadence-watch",
                        help="Median-gap publication-cadence watchdog: report "
                             "overdue/soon/on-track per (bank, doc_type) series. "
                             "Default dry-run (prints the table only); --write "
                             "emits data/cadence.jsonl and updates "
                             "data/cadence_state.jsonl (new-silence alerting).")
    cw.add_argument("--write", action="store_true",
                    help="write data/cadence.jsonl + update cadence_state.jsonl "
                         "(default: dry-run table only, no files touched)")

    args = p.parse_args(argv)
    banks = _banks(getattr(args, "banks", ""))

    if args.cmd == "list-banks":
        for b in BIS_63:
            flag = "  (verify domain)" if b.verify else ""
            print(f"{b.code:4} {b.name:48} {b.homepage}{flag}")
        print(f"\n{len(BIS_63)} banks")
        return 0

    if args.cmd in REPORTING_CMDS:
        report = RunReport("central-bank-corpus", args.cmd)
        try:
            if args.cmd == "discover":
                scope = _types(args.types)
                results = run(bank_codes=banks, scope=scope, since=args.since,
                              dry_run=not args.download, max_rounds=args.rounds,
                              native_only=args.native_only, report=report)
                for code, counts in results.items():
                    print(f"{code}: {counts}")

            elif args.cmd == "bis-sitemap":
                years = _years(args.years) if args.years else None
                since = date(min(years), 1, 1) if years else None
                until = date(max(years), 12, 31) if years else None
                only = set(banks) if banks else None
                counts = run_bis_sitemap(
                    since=since, until=until, only_banks=only,
                    dry_run=not args.download, max_per_year=args.max_per_year,
                    report=report,
                )
                print("bis-sitemap:", counts)

            elif args.cmd == "repec":
                results = run_repec(bank_codes=banks, dry_run=not args.download,
                                    incremental=args.incremental, report=report)
                for code, counts in results.items():
                    print(f"{code}: {counts}")
        except Exception as exc:
            rc = report.finish(fatal=f"{type(exc).__name__}: {exc}")
            report.write(str(_runs_path()))
            return rc
        rc = report.finish()
        report.write(str(_runs_path()))
        return rc

    if args.cmd == "reindex-from-disk":
        years = _years(args.years) if args.years else None
        mn = min(years) if years else None
        mx = max(years) if years else None
        if args.source == "bis-sitemap":
            counts = reindex_bis_from_disk(
                only_banks=set(banks) if banks else None,
                dry_run=not args.write, fetch_titles=args.titles,
                min_year=mn, max_year=mx,
            )
        else:
            counts = reindex_native_from_disk(
                bank_codes=banks, scope=_types(args.types),
                dry_run=not args.write, min_year=mn, max_year=mx,
            )
        print(f"reindex-from-disk ({args.source}):", counts)
        return 0

    if args.cmd == "report":
        rows = build_matrix(args.years, bank_codes=banks)
        print("summary:", summarize(rows))
        if args.csv:
            export_csv(rows, args.csv)
            print("wrote", args.csv)
        return 0

    if args.cmd == "convert-html":
        counts = convert_existing(dry_run=args.dry_run)
        print("convert-html:", counts)
        return 0

    if args.cmd == "retry-html":
        counts = retry_failed()
        print("retry-html:", counts)
        return 0

    if args.cmd == "wp-migrate":
        from .wp_migrate import run_wp_migrate
        run_wp_migrate(bank_codes=banks, csv_path=args.csv or None, write=args.write)
        return 0

    if args.cmd == "c2-migrate":
        from .c2_migrate import run_c2_migrate
        from .http import Fetcher
        cfg = Config()
        run_c2_migrate(cfg, Fetcher(cfg), write=args.write)
        return 0

    if args.cmd == "wp-dates":
        from .wp_dates import run_wp_dates
        run_wp_dates(bank_codes=banks, csv_path=args.csv or None, write=args.write,
                     use_wayback=not args.no_wayback, apply_only=args.apply_only)
        return 0

    if args.cmd == "repec-check":
        from .repec_check import run_repec_check
        run_repec_check(bank_codes=banks, csv_path=args.csv or None,
                        reverse_csv_path=args.reverse_csv or None,
                        grace_days=args.grace_days)
        return 0

    if args.cmd == "repec-reconcile":
        if args.propose and args.apply_csv:
            rr.error("--propose and --apply-csv are mutually exclusive")
        if args.propose and args.write:
            rr.error("--propose never writes manifests (drop --write)")
        if args.apply_csv and banks:
            rr.error("--banks has no effect with --apply-csv (the bank comes "
                     "from each CSV row) -- drop --banks")
        if args.apply_csv and args.csv:
            same = os.path.realpath(args.csv) == os.path.realpath(args.apply_csv)
            if not same and os.path.exists(args.csv):
                # realpath resolves symlinks but not case-only aliases (e.g. a
                # case-insensitive APFS volume) -- samefile is stat-based
                # (dev/inode) and catches those too. Both targets must exist
                # for samefile; --apply-csv is always an existing file here.
                try:
                    same = os.path.samefile(args.csv, args.apply_csv)
                except OSError:
                    pass
            if same:
                rr.error("--csv must not be the same path as --apply-csv (would "
                         "overwrite the decision record you are reading)")
        if args.propose:
            from .repec_check import run_reconcile_propose
            run_reconcile_propose(bank_codes=banks, csv_path=args.csv or None)
        elif args.apply_csv:
            from .repec_check import run_reconcile_apply
            run_reconcile_apply(args.apply_csv, write=args.write, csv_path=args.csv or None)
        else:
            from .repec_check import run_repec_reconcile
            run_repec_reconcile(bank_codes=banks, write=args.write, csv_path=args.csv or None)
        return 0

    if args.cmd == "sweep-orphans":
        import json as _json
        from . import orphans
        try:
            summary = orphans.sweep_orphans(
                Config(), banks=set(banks) if banks else None, move=args.move,
                progress_every=args.progress_every,
            )
        except (OSError, ValueError) as exc:   # FileNotFoundError is an OSError
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(_json.dumps(asdict(summary)))
        gib = summary.bytes_duplicates / (1024 ** 3)
        scope = f" [banks: {','.join(summary.banks)}]" if summary.banks else ""
        print(f"sweep-orphans{scope}: {summary.orphans} orphans / {summary.duplicates} duplicates "
              f"({gib:.2f} GiB) / {summary.unindexed} unindexed — moved {summary.moved}"
              f"{' (dry-run)' if summary.dry_run else ''}"
              f"{f', {summary.move_failed} move(s) failed' if summary.move_failed else ''}"
              f"{f', {summary.owner_missing} kept (owner file missing)' if summary.owner_missing else ''}"
              f"{f', {summary.hash_failed} hash failure(s)' if summary.hash_failed else ''}")
        return 0

    if args.cmd == "cadence-watch":
        from .cadence import run_cadence_watch
        run_cadence_watch(Config(), write=args.write)
        return 0

    if args.cmd == "recover-downloads":
        from .recover import run_recover_downloads
        results = run_recover_downloads(bank_codes=banks, download=args.download,
                                        csv_path=args.csv or None,
                                        candidates=args.candidates or None,
                                        seed_quarantine=args.seed_quarantine or None)
        for code, counts in results.items():
            print(f"{code}: {counts}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
