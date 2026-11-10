# Working Papers v3 — implementation summary (2026-06-14)

What shipped against the v3 design (June 2026): bank-native working-paper
discovery for the 5 big banks, with RePEc demoted to an audit/recovery role and
true publication dates everywhere they exist. Two more banks got a native D1
source on 2026-08-24: `fr` (Banque de France, #10) and `se` (Riksbank, #11).

> The June design, plan, per-bank scouting and migration post-mortems were
> retired from the repo in November 2026; the rule that still governs the code
> is in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) (migrate before native
> download). Figures below are the June 2026 run unless dated otherwise.

## Results — all 5 big banks migrated to bank-site dates

| Bank | Native source | D1/D2 migrated | Day | Month | Legacy (RePEc) |
|---|---|---|---|---|---|
| **ecb** | foedb JSON DB | 3630 | 3630 | – | 0 |
| **us** (Fed) | FEDS/IFDP pages + landing | 3831 | 1003 | 2828 | 357 |
| **jp** (BoJ) | wps_rev year tables | 398 | 398 | – | 4 |
| **gb** (BoE) | staff-WP sitemap + pages | 1181 | 1181 | – | 67 |
| **de** (Buba) | bbksearch result list | 617 | 617 | – | 72 |

≈ **6,800 working papers now carry true day-precision dates** (+2,828 Fed
confirmed-month). `doc_id`/`sha256`/`local_path` are never touched — every change
is a metadata rewrite, zero re-downloads (each flip live-verified). Native
discovery also surfaced papers the corpus lacked (de ~462, plus smaller counts
elsewhere) — discoverable via `discover --download`.

## Per-source notes

- **ecb**: the pubbydate page is a client-side **`foedb` JSON database**, not the
  `lazyload` HTML widget the doc assumed. Read `versions.json → metadata.json →
  data/0/chunk_N.json`; filter `scpwps`/`scpops`. Exact day, full archive, no
  per-paper fetch.
- **us**: day comes from each paper's landing `citation_publication_date`
  (MM-DD-YYYY), with a **month-constraint** so a revised paper's later date can't
  overwrite the original month. Only ~2017+ papers have a real day (see below).
  IFDP PDFs are `ifdp{seq}.pdf` (not `…pap.pdf`) and revisions carry an `r<N>`
  infix; both are handled by the key extractors in `wp_migrate.py`.
- **jp**: the wps_rev year tables print the exact day inline (3/4/5-column layouts
  across eras) — no per-paper fetch.
- **gb**: BoE WP numbers aren't in URLs, so the join is by **slug** + an
  exact-title tier (recovered 197 slug-drift rows).
- **de**: JS-only listing cracked via chrome-devtools (`bbksearch` endpoint);
  old papers = direct blob, recent = slug page. The manifest rows were EconStor
  copies, so the join uses the DP number from the RePEc handle, then the exact
  title; the Bundesbank blob goes into `alt_urls`.
- **fr / se** (August 2026): `sources/bdf_wp.py` (both site generations) and
  `sources/riksbank_wp.py`, wired into `wp_migrate._NATIVE`, and both adapters
  declare D1 native. In the committed manifests (2026-09-03 snapshot) their
  back-catalogue is **not migrated** (no `repec_handle`, no migrated date on the
  RePEc-era rows; the `bank_site` rows are new native downloads). See the
  warning in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Architecture

- Hand-written adapter per bank (`adapters/{ecb,fed,boj,boe,buba,bdf,se}.py`);
  jp and fr moved out of `banks_sources.toml` (gb and de were generic). `base.discover` routes D1/D2 native-first
  only when the type is in `native_types`, with a `_skip_known_url` hook
  (injected by the pipeline) so a flipped bank never re-downloads its migrated
  back-catalogue.
- **Per-bank manifests** `data/manifest/<bank>.jsonl` (auto-split from a legacy
  single file; atomic per-bank rewrite). Committed to git for replayability.
- `wp_migrate.py` is generic: `_NATIVE` / `_KEY_FROM_PDF` / `_KEY_FROM_HANDLE`
  dicts + an **exact-normalized-title** match tier (unambiguous-only, never
  fuzzy). Adding a bank = one scraper + two key functions.
- `wp_dates.py` (phase 4): recover the day for legacy/month rows via PDF
  `/CreationDate` + Wayback first-capture (month-constrained), into the committed
  `data/wp_dates_index.jsonl` (run-once, replay-forever, `evidence_url` per entry).

## On Fed pre-2017 dates (the honest part)

The Fed assigns each working paper a **series month** and did not publish a day
before ~2017. Verified across six sources — landing `citation_publication_date`,
RePEc, FRASER, the PDF cover, the PDF body ("This Version: …"), and the PDF
`/CreationDate` — all give only the month. So ~2,800 Fed rows are honestly
month-precision, not a tooling gap. `wp-dates` recovers a day for the ~12% that
were archived (Wayback) in their series month; the rest stay month. This matches
the design's expectation ("post-2000 → day, 1990s → month").

## Commands

```
python -m cb_corpus wp-migrate --banks ecb,us,jp,gb,de [--write]   # dates from bank sites
python -m cb_corpus wp-dates   [--banks …]            [--write]    # recover legacy days -> committed index
python -m cb_corpus discover   --banks de --types D1 --download    # fetch newly-discovered papers
```

## Replicability

`data/manifest/<bank>.jsonl` + `data/wp_dates_index.jsonl` are committed. A fresh
clone replays every date with **no re-crawl**; PDFs are rebuilt from the manifest
(`fetch-from-manifest`, planned in June, **never built**). The expensive Wayback/foedb work runs once.

## Day recovery — results (phase 4, corpus-wide)

`wp-dates` ran across all 13 WP banks (9,589 legacy/month D1/D2 rows):

- **1,328 days recovered (~14%)** from month-constrained PDF `/CreationDate`
  (free, on-disk): es 395, it 228, ca 216, au 151, us 112, nl 102, se 91, fr 33.
  Applied to the manifest + recorded in the committed `wp_dates_index.jsonl`.
- The remaining ~8,261 have no in-month PDF date. A Wayback pass extends the
  index (slow, archive.org-bound; resumable; yield is bank-dependent — high for
  promptly-archived 2000s papers, ~0 for pre-2000/digitised). It was designed as a
  **scheduled job**, but `deploy/crontab` does not run `wp-dates` today (manual
  runs only) — materialise its results anytime with
  `wp-dates --apply-only --write` (the same command a fresh clone runs to replay
  the committed index, no network).
- **Honest ceiling:** many legacy WPs (esp. pre-2000 and Fed pre-2017) have **no
  publication day published anywhere** — they stay month precision by design, not
  by tooling failure.

## Future work (documented, not done)

- **Wayback tail** — run `wp-dates` on a cron (the docs' model) to keep extending
  the committed index; `--apply-only` to materialise.
- **es/gb leftovers** — see the repec-check results below: language-variant
  duplicates (ignore) plus a real legacy tail (ingest via RePEc + `wp-dates`).
- **D2 for the non-five banks** (June 2026 decision: D2 for every bank that has one) — wire each bank's occasional/discussion-paper
  RePEc series handle into `SERIES` (one-off IDEAS research per bank).
- **Missing papers** — the ~462 Bundesbank DPs (and small tails elsewhere) the
  corpus lacks: `discover --download` to fetch.
- **`fetch-from-manifest` (phase 6)** — rebuild `data/raw/` PDFs from a committed
  manifest (fast clone path). Not built.
- **A–F native coverage for all 63 banks** — currently only the hand-coded majors
  have A/B/E/F native listings; extending to every bank is a large per-bank
  investigation project (each like the 5 WP banks), not an automated sweep. The
  shared registries in `wp_migrate.py` (`_NATIVE`, `_KEY_FROM_PDF`,
  `_KEY_FROM_HANDLE`) and `adapters/__init__.py` would need an auto-registration
  refactor first so per-bank work does not collide.

## Status of the v3 phases

- Phase 0 (schema) ✅ · Phase 1 (5 native scrapers, then fr and se) ✅ ·
  Phase 2 (`repec-check`) ✅ · Phase 3 (migration) ✅ for the 5 · Phase 4
  (`wp-dates`) ✅ · Phase 5 (CLI/docs) ✅
- Phase 6 (`fetch-from-manifest`) — not built · D2 handles for non-five banks
  (June 2026 decision: D2 for every bank that has one) — pending.
- RePEc is still walked by the nightly sync (`repec` in `deploy/run-job.sh`) for
  every bank in `SERIES`, natively-covered ones included: dual source, with
  dedup on `source_url` / `doc_id` / `sha256`.

## RePEc completeness audit — results (2026-06-14)

`repec-check` across the wired WP banks (12 with results below; read-only, never downloads; full list
in `data/reports/repec_check.csv`, gitignored). Coverage ≥ 90 % everywhere (es lowest at 90.8 %),
**0 `missing_recent`** for every bank (native discovery keeps up); au, ch and nl
at 100 %.

| Bank | RePEc total | Covered | Missing (legacy) |
|---|---|---|---|
| us | 4203 | 4189 | 14 |
| ca | 1360 | 1355 | 5 |
| it | 1211 | 1205 | 6 |
| gb | 1128 | 1094 | 34 |
| es | 1091 | 991 | 100 |
| fr | 1007 | 992 | 15 |
| de | 691 | 689 | 2 |
| au | 536 | 536 | 0 |
| jp | 405 | 402 | 3 |
| se | 391 | 366 | 25 |
| ch | 318 | 318 | 0 |
| nl | 200 | 200 | 0 |

A paper-page second pass on the es/gb leftovers recovered only +2 each (es 98,
gb 32): the rest are not a matching bug but **language-variant duplicates** (IDEAS
lists e.g. `bde:wpaper:2618` and `2618e` as two papers; cosmetic) and **genuinely
old papers** the corpus lacks (e.g. es 1999 papers, old BoE WPs under dead
pre-2014 URLs). A future refinement could fold `{id}e` / `{id}r` variants onto
their base id.
