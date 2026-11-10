# central-bank-corpus

Builder for a corpus of **official central-bank documents** — first-hand sources only,
for downstream RAG / analysis. Part of [MyOpenFund](https://github.com/MyOpenFund).

**39,400+ documents** from **61 of the 63 BIS member central banks** — policy decisions,
minutes, speeches, working papers, reports, statistics — with day-precision dates where the
bank publishes them, honest provenance on every row, and a self-healing nightly pipeline
(dead-URL quarantine, publication-cadence watchdog, automated recovery).

**Scope:** document families A–F (see `taxonomy.py`). **Targets:** the 63 BIS member central banks.
**Rule:** official primary sources only — every document comes from the issuing bank's own
domain, or `*.bis.org` (the official re-host for speeches). No academic-curated datasets,
no machine-translated or model-generated text.

> 📊 Counts: `python -m cb_corpus report` (bank × type × year completeness matrix). Full inventory:
> `python gen_corpus.py` → **`CORPUS.md`** (generated from the per-bank manifests, not committed).
> 🤖 RAG handoff contract (schema, filters, citation): **`INGESTION_RAG.md`**.

## What's wired up

- **BIS speech index** (`sources/bis_speeches.py`) → speeches (C1) for all 63 banks from the
  BIS yearly sitemaps; keeps the **original PDF**. Institution attribution is **alias-aware**
  (`banks.py::BIS_ALIASES`, accent/apostrophe-insensitive) so renamed / historical / native
  BIS labels (e.g. "Bank of Latvia" → `lv`, pre-2010 "…US Federal Reserve System" → `us`)
  still map to the right bank instead of being silently dropped.
- **RePEc / IDEAS discovery** (`sources/repec.py`) → working papers (D1/D2) for every bank in
  `SERIES` (13), including those with a native source (second source, deduped).
  **Paginates** the full series back-catalogue and reads the publication month
  from the IDEAS `citation_*` metadata (RePEc is month-only → `date_precision="month"`).
- **Working Papers v3 — native bank-site discovery** (the 5 big banks: ecb, us, jp, gb, de;
  fr and se added 2026-08). Each gets a native scraper with the **exact publication day** from
  the bank's own site (`sources/{ecb_foedb,fed_wp,boj_wp,boe_wp,buba_wp,bdf_wp,riksbank_wp}.py`),
  RePEc stays a second source. Rule: migrate a bank's rows before switching it to native download
  ([docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)).
  `wp-migrate` rewrites existing rows' dates in place (key→URL→exact-title join, `doc_id`/
  `sha256`/`local_path` untouched, zero re-downloads); `wp-dates` recovers the day for legacy
  rows via PDF `/CreationDate` + Wayback into the **committed** `data/wp_dates_index.jsonl`
  (run-once, replay-forever). See [docs/WP_V3_SUMMARY.md](docs/WP_V3_SUMMARY.md).
- **Per-bank adapters** (`adapters/`) → site-specific listings (A/B/E/F + native D1/D2). Bespoke
  `ECBAdapter`, `FedAdapter`, `BoJAdapter` (jp), `BoEAdapter` (gb), `BubaAdapter` (de),
  `BdfAdapter` (fr), `RiksbankAdapter` (se), `RBAAdapter` (au); TOML-declarative adapters
  (`banks_sources.toml`: ch/ca; a missing/empty/malformed file, an unknown bank code or an
  unknown section prints an `[adapters] WARNING` on stderr — a malformed file only skips the
  declarative adapters, it never breaks the CLI). `GenericAdapter`
  (C1 + RePEc D) is the default. **Rate decisions, policy statements & minutes (A1/A2/A3) are
  wired for the majors.**
- **Bank-of-England official recovery** (`sources/boe_wp.py` + `pipeline.run_boe_wp_recovery` /
  `run_boe_recovery`) → BoE working papers and MPC minutes from the bank's **own sitemaps**
  (`/sitemap/staff-working-paper`, `/sitemap/minutes`), since IDEAS only carries the pre-2017
  dead URLs. Generic over any BoE sitemap section (reports/FSR are a one-line addition).
- **Wayback recovery** (`sources/wayback.py`) → official PDFs a bank has taken offline, fetched
  from archive.org's raw snapshot (`provenance="wayback"`, fully audited). Used for the Riksbank.
- **HTML→PDF** (`htmlpdf.py`) → HTML-only documents (e.g. ECB monetary-policy accounts) are
  rendered to PDF via headless Chrome; the **raw HTML is also kept** (`html_path`). Chrome runs
  with `--no-sandbox`, so a render is **refused** when the URL is not http(s) or its host is, or
  resolves to, an address in `Config.render_blocked_networks` (default: loopback, RFC 1918,
  link-local incl. cloud metadata, CGNAT `100.64.0.0/10` = the tailnet, multicast/reserved, and
  the IPv6 equivalents); the HTML then stays the artifact, with a `render refused` stderr line
  (counted in `Storage.render_refused`). Only the top-level URL is checked: Chrome's own
  sub-resource requests are not inspected (that would need an egress proxy).
- **Storage / dedup** (`storage.py`) → **per-bank manifests** `data/manifest/<bank>.jsonl`
  (a legacy single `data/manifest.jsonl` is auto-split on first use); committed to git for
  replayability. Dedup on `doc_id` (stable hash of bank+type+**url** — date-independent, so
  correcting a date is a pure metadata update, no id churn), persisted `alt_urls`, and content
  `sha256`. **No domain guard** — discovery owns URL quality. A record whose bytes another
  row already owns (`skip:duplicate-content`) is remembered on that owning row (its URL into
  `alt_urls`, its source page into the optional `alt_source_urls`), so the next run skips it
  before any fetch instead of re-downloading it every night. Only when the owner row is a
  **PDF**: an HTML owner keeps the URL for the current run only (retried next night), unless
  `Config.stamp_duplicate_html=True`. The stamp is an add-only merge done under the bank's
  manifest lock on a fresh read of the owner row. Every duplicate is logged in
  `data/reports/duplicate_stamps.jsonl` (`persisted` true/false), with one
  `duplicate-content: N (stamped U urls on R rows)` stderr line per batch when N > 0.
- **Reindex from disk** (`pipeline.reindex_native_from_disk` / `reindex_bis_from_disk`, CLI
  `reindex-from-disk`) → rebuild manifest rows for PDFs that are **on disk but missing from the
  manifest** (e.g. the manifest was reset/lost while downloads kept accumulating). Replays
  discovery — native per-bank adapters (`--source native`, default; covers C1/A/B/E/F) or BIS
  sitemaps (`--source bis-sitemap`; C1) — to recover each document's exact **date** and **title**,
  then writes the row **without re-downloading the PDF**, matched to the on-disk file by the
  stable `doc_id`. Dry-run by default (`--write` to persist); idempotent.
- **Completeness matrix** (`completeness.py`) → expected-vs-downloaded per (bank × type × year):
  `ok / partial / missing / unknown`.
- **Fetcher** (`http.py`) → per-host rate limit (0.5s default) + retries with exponential
  backoff (5xx and network errors). A 4xx is retried only if listed in
  `Config.retry_http_4xx` (default 403/408/429); any other 4xx (404, 410, …) fails at once
  without backoff. Deliberately **not** robots-gated; set your own contact via the
  `CRAWLER_CONTACT` env var (defaults to this repo's URL).
- **Reproducible rebuild** (`pipeline.py`) → idempotent re-runs; `run(..., max_rounds=N)`
  re-crawls until a clean round (no new docs, no errors); discovery failures are logged to
  `data/discovery_errors.jsonl` (no silent drops).

## Install / test

```bash
pip install -r requirements-dev.txt      # runtime deps + pytest + pytest-socket
python3.13 -m pytest -q
```
> The suite is network-free by construction: `pytest.ini` sets `--disable-socket`,
> so a test that reaches for the network fails instead of crawling a live bank.
> Use **`python3.13`** — that interpreter has the dependencies in this environment
> (`python3` resolves to 3.14 without them).

## Operations

Every work command (`discover`, `bis-sitemap`, `repec`) appends a structured
run-report line to `data/runs.jsonl` (run id, per-source counters, fetch
errors, an explicit `truncated` flag) and exits honestly: `0` clean, `1`
fatal, `3` degraded — a run that did no useful work never exits 0. An HTTP 404
on an IDEAS listing page after the last one (not linked from the previous page)
ends the RePEc walk cleanly (no truncation; the run can still be degraded for
other reasons); a 404 on page 1, or on a page the previous one links to, stays a
truncation (`3`). IDEAS listings are walked up to `Config.repec_max_pages`
pages per series (default 200): a loop guard, not a data limit (the largest
series had 17 pages in November 2026). Reaching it is a clean end when the last
page read advertises no next page, and a truncation when it does. Each source
in the run-report also carries `skipped` (reason → count) and `skip_samples`
(reason → up to 5 samples) for the RePEc paper pages a walk passes over:
`paper_gone` (HTTP 404/410), `paper_fetch_failed` (any other fetch failure) and
`paper_no_pdf` (no PDF link on the page). They are counted for visibility and
never affect the exit code. The NAS
wrapper pushes an ntfy notification on non-zero exits when `NTFY_URL` is set
(absent = no-op).

### Maintenance: orphan sweep

An *orphan* is a file under `data/raw/` whose name is not a manifest `doc_id`
(left behind by an earlier id scheme, or by a manifest reset). Most orphans are
byte-identical to a document that is indexed under another id.

```bash
python -m cb_corpus sweep-orphans                 # dry-run: classify + report, moves nothing
python -m cb_corpus sweep-orphans --move          # move duplicates to data/raw_orphans/ (mirror tree)
python -m cb_corpus sweep-orphans --banks ecb,us  # restrict the walk
```

Rules: a file whose sha256 is owned by a manifest row is a **duplicate** and is
moved (atomic rename, never overwritten, and only after verifying that the
owning row's own file is still on disk with the same size and sha256 — the
sweep never removes the last good copy of a document); any other orphan is
**unindexed** and stays in place — follow up with `reindex-from-disk` on that
scope. Indexed files are never touched; symlinks are ignored; an unreadable
file is reported (`hash-failed`), never moved. A `kept-owner-missing` row
carries the reason (`error`) and the resolved `owner_path`. Every run writes
`data/reports/orphan_sweep_<ts>.jsonl` (one line per orphan) and
`orphan_sweep_<ts>.summary.json`. Exit 0 when the sweep completed (failed
moves are counted in the summary), 1 on a fatal error.
`data/raw_orphans/` is walked by nothing and pushed by nothing; delete it by hand
once the vault confirms nothing is missing. Run it under the NAS job lock
(`run-job.sh campaign sweep-orphans …`) or outside the nightly sync window.

## Use

```bash
python -m cb_corpus list-banks

# Speeches (C1) — single pass over the BIS yearly sitemaps, all banks at once
python -m cb_corpus bis-sitemap --download

# Working papers (D1/D2) via RePEc (paginated, dated) — every wired bank, native ones included
python -m cb_corpus repec --download                  # all wired series
python -m cb_corpus repec --banks ecb,us --download

# Working Papers v3 — replace RePEc month-dates with native bank-site day-dates
python -m cb_corpus wp-migrate --banks ecb,us,jp,gb,de            # dry-run report + CSV
python -m cb_corpus wp-migrate --banks ecb,us,jp,gb,de --write    # apply (metadata only)
# Recover the day for legacy / month-precision rows -> committed, replayable index
python -m cb_corpus wp-dates --write

# Per-bank native listings (A/B/E/F + inherited C1/D), with convergence retries
python -m cb_corpus discover --banks us,ecb --types A3,E4 --download --rounds 3

# Reindex: rebuild manifest rows for PDFs on disk but missing from the manifest
# (recovers exact dates/titles, no PDF re-download). Dry-run report unless --write.
python -m cb_corpus reindex-from-disk --source native --banks ecb            # report
python -m cb_corpus reindex-from-disk --source native --banks ecb --write    # persist

# Completeness report -> CSV
python -m cb_corpus report --years 2015-2025 --csv data/reports/matrix.csv
```

Full rebuild from Python:

```python
from cb_corpus.pipeline import run_bis_sitemap, run_repec, run
run_bis_sitemap(dry_run=False)      # C1 speeches (single pass, idempotent)
run_repec(dry_run=False)            # D1/D2 working papers (paginated)
run(dry_run=False, max_rounds=3)    # native A/B/E/F per bank (converges)
```

## Before a real run

1. **Set your contact: `export CRAWLER_CONTACT="you@example.com"`.** It goes into the
   User-Agent of every request — it's how central-bank webmasters reach *you* (not the
   project) about *your* crawls; part of being a polite crawler. Without it, the UA points
   to this repository. Throughput is bounded by politeness
   (`min_delay_seconds`, default 0.5 s/domain); the rate limit is **per-host**, so banks on
   different domains crawl in parallel.
2. **Validate on a small window first.** Parsers were written to each site's documented markup,
   not all validated against live HTML. Dry-run one or two banks before a full crawl:
   ```bash
   PYTHONPATH=. python -m cb_corpus discover --banks us,ecb --since 2024-01-01   # no --download
   ```
   If a parser returns nothing / wrong rows: save the live HTML, adjust the pure `parse_*`
   function, and re-run its test in `tests/`.

**Completeness** (`report`) — status per (bank × type × year): `ok` (downloaded ≥ expected),
`partial` (0 < got < expected → re-crawl that bank/type/year), `missing` (expected but 0 → fix the
adapter/selector), `unknown` (no expected count). Add `expected_per_year` to an adapter (from the
bank's published meeting calendar) to turn `unknown` cells into real checks.

## Reproducibility & reliability

- **Idempotent** — re-running skips already-saved docs (`doc_id` + `sha256`), so a full
  rebuild converges instead of duplicating.
- **Convergence** — `--rounds N` (or `run(max_rounds=N)`) re-crawls until a round adds nothing
  new and reports no errors, filling transient-failure gaps.
- **Visible failures** — discovery fetch failures are appended to `data/discovery_errors.jsonl`
  instead of being swallowed, so an incomplete run is detectable.

## Adding a bank adapter

```python
from cb_corpus.adapters.base import BankAdapter, register
from cb_corpus.taxonomy import DocType

@register("gb")                       # overrides GenericAdapter for the Bank of England
class BoEAdapter(BankAdapter):
    native_types = (DocType.A3, DocType.E1, DocType.E2)
    expected_per_year = {DocType.A3: 8}          # MPC minutes/yr
    def _discover_native(self, doc_type, since):
        # fetch the bank's listing page(s) via self._fetch_text (records failures),
        # parse with a pure parse_* function, yield DocRecord(...)
        ...
```
C1 (speeches) and D1/D2 (papers) are inherited — only implement the native listings.
Use `self._fetch_text(url, context=...)` for listing fetches so failures are surfaced.
When a parser misbehaves, save the live HTML and adjust the relevant pure function
(`parse_listing`, `parse_minutes_links`, `parse_index`, `parse_series_page`,
`extract_official_pdf`), then re-run its test in `tests/`.

**Extending coverage** — the adapter build order is in `BANKS.md`; confirm any domain flagged
⚠️ there is reachable before a first crawl. To add working-paper series, extend
`sources/repec.py::SERIES` toward all 63 banks (verify each handle on IDEAS first).

## Layout

```
cb_corpus/
  taxonomy.py        DocType A1..G3, FULL_SCOPE (A–F)
  banks.py           the 63 BIS members + BIS_ALIASES (institution matching)
  models.py          DocRecord
  config.py          settings (UA, throttle, html_to_pdf, render_blocked_networks)
  http.py            fetcher (per-host throttle + retry/backoff; throttle() for Chrome)
  storage.py         manifest, dedup (doc_id + sha256), HTML→PDF render, html_path
  htmlpdf.py         headless-Chrome HTML→PDF (shared profile)
  completeness.py    expected-vs-downloaded matrix
  pipeline.py        run() (convergence), run_bis_sitemap(), run_repec(),
                     reindex_*_from_disk() (manifest recovery, no re-download), run_*_recovery()
  cli.py             command line (`python -m cb_corpus --help` lists every subcommand)
  runreport.py       run reports (data/runs.jsonl) + honest exit codes
  cadence.py         median-gap silence watchdog (cadence-watch)
  quarantine.py      per-URL download quarantine (dead-letter state; one local night per Storage instance = per process run of a command)
  recover.py         recover-downloads (inventory-driven Wayback recovery)
  orphans.py         sweep-orphans (quarantine on-disk duplicates)
  convert.py / retry_html.py   HTML -> PDF migration and retries
  wp_migrate.py      WP v3 date migration (native bank-site dates, zero re-download)
  wp_dates.py        WP v3 day recovery (PDF /CreationDate + Wayback, committed index)
  repec_check.py     RePEc completeness audit + repec-reconcile
  c2_migrate.py      one-shot ECB C2 enrichment from foedb
  sources/
    bis_speeches.py  BIS speech index         -> C1 (alias-aware attribution)
    repec.py         RePEc discovery          -> D1/D2 (paginated, dated)
    wayback.py       Wayback CDX recovery      -> dead-but-official PDFs/HTML
    recovery.py      uniform recovery-source abstraction
    ecb_pub.py       ECB per-section includes  -> A1/B1/C2/E1-E4/G2 (primary -> CDX fallback)
    ecb_foedb.py     ECB foedb JSON DB         -> D1/D2 native
    fed_wp.py / boj_wp.py / boe_wp.py / buba_wp.py / bdf_wp.py / riksbank_wp.py
                     native working papers (us / jp / gb / de / fr / se); boe_wp also MPC minutes
  adapters/
    base.py          BankAdapter ABC + registry + GenericAdapter + _fetch_text
    fed.py / ecb.py / boj.py / boe.py / buba.py / bdf.py / se.py / rba.py  bespoke adapters
    declarative.py + generic_sitemap.py + listing_crawler.py  TOML-driven adapters
tests/               596 tests at 2026-11-10 (taxonomy, registry, parsers, dedup, matrix,
                     reliability, quarantine, recovery, cadence, ...)
```

## Notes

- Official primary sources only; non-English originals are kept as-is (the BIS speech corpus
  is English-language). No machine translation or model-OCR as a substitute for the original
  PDF (local OCR for indexing is fine if kept separate).
- Respect each site's terms — BIS content is noncommercial-use.
- The corpus is **~99 % complete** against each source's authoritative catalogue (BIS speeches,
  IDEAS working-paper series, bank publication calendars).

## License

- **Code**: [MIT](LICENSE).
- **Data** (the manifest files under `data/` — metadata and links only; the documents
  themselves stay on their issuing banks' servers and keep their own terms):
  [CC BY 4.0](DATA_LICENSE) — attribute "MyOpenFund central-bank-corpus".
