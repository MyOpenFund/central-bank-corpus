# RePEc as a completeness check (no downloads)

In v3, RePEc/IDEAS stops being the *primary* source for the natively-covered
banks (`ecb`, `us`, `jp`, `gb`, `de`, then `fr` and `se`) and also becomes an
**audit oracle**: `repec-check` answers "is the corpus missing any paper?"
without ever downloading a document. (The nightly sync still walks RePEc for
every wired bank, natively-covered ones included, as a second source.)

> **Status (2026-11):** shipped as `repec-check` on 2026-06-14
> (`cb_corpus/repec_check.py`); results in [WP_V3_SUMMARY.md](WP_V3_SUMMARY.md).
> This page was the June design; the sections below are marked where the code
> differs.

## Why RePEc is a good oracle

- The banks themselves maintain their RePEc archives (official ReDIF feeds),
  so series coverage is near-exhaustive.
- Series are flat, numbered, easy to enumerate (`ideas.repec.org/s/<handle>.html`
  with pagination) — one cheap crawl per series.
- Its weaknesses (month-only dates, 1-2 week lag) don't matter for an
  *audit*: we only need the paper's existence, identity and approximate date.

## Command

```
python -m cb_corpus repec-check [--banks us,ecb,...] [--csv path] [--reverse-csv path] [--grace-days 45]
```

**This command never downloads PDFs and never writes the manifest.** It prints a
per-bank summary and writes two CSVs: missing papers (default
`data/reports/repec_check.csv`) and manifest rows RePEc does not list (default
`data/reports/repec_check_reverse.csv`, see step 5).
Default banks: every bank wired in `SERIES` (`cb_corpus/sources/repec.py`, 13
banks; only `ecb` has a D2 series). The `--write` mode designed below was not
built: handle stamping is done by `wp-migrate --write` and `repec-reconcile`
(see "Handle stamping, as built").

## Algorithm

1. **Enumerate RePEc** — for each wired series (`SERIES` in
   `cb_corpus/sources/repec.py`), crawl the IDEAS series listing and collect,
   per paper: handle, number, normalized title, month-date. (Paper pages only
   need fetching for papers that fail to match in step 3 — keeps the crawl
   light and polite.)
2. **Load the manifest** — all D1/D2 rows for the requested banks.
3. **Match** RePEc records against manifest rows. A RePEc record is *covered*
   (→ no need to download anything) if ANY of:
   - **key match**: the bank-specific number (extractors `_KEY_FROM_HANDLE` /
     `_KEY_FROM_PDF` in `cb_corpus/wp_migrate.py`) extracted from the RePEc
     handle equals a number extracted from a manifest row's `pdf_url` /
     `source_url`;
   - **URL match**: the RePEc download URL equals a manifest `pdf_url`
     (after normalization: scheme, doubled slashes, `~hash` suffix stripped);
   - **title match**: normalized titles are equal (see below).

   *As built:* the cascade is handle (the full IDEAS handle found in a row's
   `repec_handle` or `source_url`) → key → exact normalized title, on the
   listing alone. Leftovers then get a **second pass**: the IDEAS paper page is
   fetched and re-matched by the bank PDF URL (normalized) or the full canonical
   title.
4. **Classify the leftovers** (RePEc records with no manifest match):
   *As built:* `missing_recent` means the paper id's year is at least the year
   of today minus 45 days; everything older is `missing_legacy`.

   - `missing_recent` — paper's month is within the native scraper's reach
     (bank site still lists it) → fix = re-run the native scraper; if it
     still doesn't appear, the native scraper has a bug → investigate.
   - `missing_legacy` — paper is old / no longer on the bank site → ingest
     via the RePEc record itself + date recovery
     ([DATE_RECOVERY.md](DATE_RECOVERY.md)).
5. **Reverse check** (manifest rows with no RePEc match). *As built
   (2026-11):* only rows whose doc type has a RePEc series for that bank are
   checked; a row is matched if any tier above (or the leftover page-fetch pass)
   ties it to a RePEc paper. The grace window is `--grace-days` (default 45; it
   also splits `missing_recent` / `missing_legacy`); an undated row counts as
   `unmatched_old`, a `YYYY-MM` date as its first day. Rows dated before the
   bank's earliest row that RePEc does list are `pre_repec` (papers older than
   the series on IDEAS, e.g. Bundesbank 1995–2011, Banco de España 1978–1997),
   not a gap signal. The floor is per bank (not per series) and trusts row
   dates: one wrongly early matched date lowers it. If a listing was truncated, hit its page cap or came back
   empty, the bank's reverse check is skipped and flagged
   (`reverse_skipped_truncated`) rather than reporting every row as unmatched;
   failed leftover page fetches are counted in `pagefetch_errors`. Two cases:
   - **`pending_repec`** (expected, not an anomaly): row's `date` is within
     the last ~45 days → the paper was fetched from the bank site before
     RePEc indexed it (RePEc lags days to ~2 weeks; 45 d gives margin).
     This is the normal v3 flow — native discovery is *ahead* of RePEc by
     design. Reported informationally; auto-resolves at a later check once
     RePEc catches up.
   - **`unmatched_old`**: older rows with no RePEc record. Usually benign
     (paper withdrawn from RePEc, series gap, non-WP stray picked up by a
     scraper), but a systematic excess hints at a scraper bug → review.

## Title normalization (the dedup rule)

Same title ⇒ same paper ⇒ **never download again**. Normalization before
comparison:

```
lowercase
→ unicode NFKD, strip accents/diacritics
→ replace any non-alphanumeric run by a single space
→ strip leading/trailing spaces
→ collapse internal whitespace
```

Match on equality of the normalized strings (`wp_migrate.normalize_title`).
`repec-check` has no fuzzy tier. Fuzzy ranking exists only in
`repec-reconcile --propose`, which writes candidates to a CSV for a human to
approve; nothing fuzzy is ever written automatically. The June design said:
optionally add a fuzzy tier
(e.g. Levenshtein ratio ≥ 0.95) flagged as `match_fuzzy` in the report for
human review — punctuation and subtitle drift between bank sites and RePEc
is common ("ECB-global" vs "ECB-Global", em-dash vs hyphen, etc.).

**Never auto-act on fuzzy matches.** Exact-normalized matches are safe to
treat as duplicates; fuzzy ones go to the report only.

## Handle stamping, as built

- `wp-migrate --write` stamps `repec_handle` on the rows it migrates whose
  `source_url` is an IDEAS page.
- `repec-reconcile [--write]` stamps IDEAS `source_url`s onto uniquely matched
  rows (strict cascade, never fuzzy, never dates).
- `repec-reconcile --propose` then `--apply-csv <csv> --write`: human-approved
  reconciliation for title drift between RePEc and the published paper.

The June design for a `repec-check --write` mode follows; its rationale still
explains why stamping is safe.

For every manifest row matched in step 3 (key / URL / exact-title only,
never fuzzy), `--write` stamps the RePEc handle into a new metadata field:

```json
{"repec_handle": "RePEc:ecb:ecbwps:20253117"}
```

Why this is safe and useful:

- `doc_id = sha1(bank|type|pdf_url)` — adding `repec_handle` changes nothing
  about identity, file, `sha256` or `local_path`. Pure metadata update,
  zero re-downloads.
- **Reruns get faster and stricter**: rows that already carry a handle are
  matched directly by handle on the next check, skipping the title/URL
  cascade entirely. Only un-stamped rows go through the cascade.
- **`pending_repec` self-heals**: a recent paper absent from RePEc today
  gets its handle stamped automatically at a later `--write` run, once
  RePEc has caught up. Each rerun incrementally completes what's missing.
- Rows with no RePEc match keep no field (absent ≠ empty), so the reverse
  check stays meaningful.

Workflow as built: `repec-check` (inspect the report) → `wp-migrate --write` /
`repec-reconcile` to persist handles.

## Migration of v2 rows (one-off)

*Done for `ecb`, `us`, `jp`, `gb`, `de` in June 2026 with `wp-migrate`; `fr` and
`se` are wired but not applied in the committed manifests (see
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)).*

The manifest had thousands of D1/D2 rows with `date = YYYY-MM-01`
sourced from RePEc. When native scrapers land:

1. Run the native scraper in dry-run for the full history.
2. Join native records to manifest rows with the same match cascade
   (key → URL → exact title).
3. For each match: rewrite the row's `date` with the bank-site date, set
   `date_precision="day"`, `date_source="bank_site"`, and stamp
   `repec_handle` while we're at it. If the native scraper found the paper
   under a different URL, register it in `alt_urls` so dedup recognises it
   — **this is what prevents mass re-downloads when native scrapers go
   live** (see the ordering rule in
   [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)). Keep `doc_id`,
   `sha256`, `local_path` untouched — the PDF on disk is the same file.
4. Rows that match nothing on the bank site are `missing_legacy` →
   [DATE_RECOVERY.md](DATE_RECOVERY.md).

This fixes dates in place with **zero re-downloads**.

## Scheduling

- Native scrapers: run inside the nightly `sync` (`deploy/crontab`).
- `repec-check`: designed as weekly, but **not scheduled** in `deploy/crontab`
  today; it runs by hand.
- Treat a non-empty `missing_recent` bucket as a failure signal.
