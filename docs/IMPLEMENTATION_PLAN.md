# WP v3 — phase 3: migrate before switching a bank to native download

The working-paper v3 plan (June 2026) shipped for `ecb`, `us`, `jp`, `gb` and
`de`: bank-native discovery with migrated dates, RePEc kept as an audit
(`repec-check`) and as the month constraint for day recovery (`wp-dates`).
RePEc is still walked by the nightly sync for every wired bank (dual source).
`fr` and `se` got native D1 sources in August 2026; see the warning below. What shipped is in
[WP_V3_SUMMARY.md](WP_V3_SUMMARY.md). This page keeps the one rule from that
plan that still governs the code: the **phase 3 ordering constraint**, cited
from `cb_corpus/wp_migrate.py`, `cb_corpus/adapters/base.py` and
`cb_corpus/adapters/ecb.py`.

## The rule

Run the date migration for a bank (`python -m cb_corpus wp-migrate --banks <code> --write`)
**before** adding `DocType.D1` / `DocType.D2` to that bank's adapter
`native_types`, that is before its native scraper runs with `discover --download`.

## Why

- `doc_id` is `sha1(bank|type|pdf_url)` (`models.py`), not a function of the
  date. A native scraper often finds a paper already in the corpus under a
  **different URL** than its RePEc-era row (Bundesbank blob instead of
  EconStor copy, ECB redirects, Fed revision infixes).
- A different URL gives a different `doc_id`, so `Storage.save()` (which dedups
  by `doc_id` / `sha256`) would download the paper again; when the bytes differ
  from the stored file (re-rendered or revised PDF), a duplicate is stored.
- The migration joins native records to existing rows (key, then URL, then exact
  normalized title; never fuzzy), rewrites only the date metadata
  (`date`, `year`, `date_precision`, `date_source`), stamps `repec_handle` when
  the row's `source_url` is an IDEAS page, and
  registers the native URL in the row's `alt_urls`. It never changes `doc_id`,
  `pdf_url`, `sha256` or `local_path`.
- After that, the pipeline injects `Storage.is_known_url` into the adapter as
  `_skip_known_url`; `BankAdapter.discover` drops every native record whose URL
  is already known (including through `alt_urls`) before any download. Only
  genuinely new papers enter, keyed on their native URL.

`wp-migrate` without `--write` is a dry run (stdout + CSV, no writes); a native
discovery without `--download` is always safe.

## Adding a bank

1. Write its native scraper and the two key extractors (`_KEY_FROM_PDF`,
   `_KEY_FROM_HANDLE`) and register it in `_NATIVE` (`cb_corpus/wp_migrate.py`).
2. `wp-migrate --banks <code>` (dry run), review the CSV, then `--write`.
3. Only then add D1/D2 to the adapter's `native_types`; check that a recent
   `discover --since <YYYY-MM-DD>` yields only new papers.

> ⚠️ **`fr` and `se` (2026-11 state):** both adapters already declare D1 in
> `native_types` (`adapters/bdf.py`, `adapters/se.py`), but in the committed
> manifests (2026-09-03 snapshot) none of their RePEc-era rows carries a
> `repec_handle` or migrated date: `wp-migrate --write` has not been applied to
> them. Run it (dry run first) before relying on native download for these two
> banks.
