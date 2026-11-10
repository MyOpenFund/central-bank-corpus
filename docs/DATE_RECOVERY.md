# Date recovery for legacy RePEc-only papers

Some old working papers exist on RePEc but no longer on the bank's website
(site redesigns, dead listings). For these we keep RePEc as the source of the
*document*, but we want a better publication date than `YYYY-MM-01`.

Hard constraint discovered during investigation: **day precision does not
exist anywhere in RePEc** (IDEAS meta tags, EconPapers, API — all carry
`YYYY-MM` only, because that is what the banks put in their ReDIF
`Creation-Date`). So the day must be recovered from *outside* RePEc.

> **Status (2026-11):** shipped as `wp-dates` (`cb_corpus/wp_dates.py`, June 2026).
> **As built**, the waterfall is shorter and in a different order than designed
> below: (0) the committed index, (1) PDF `/CreationDate` from the file on disk,
> (2) Wayback first capture (skipped for papers before 1997) of the PDF URL, then its abstract/landing variant and
> `alt_urls`. Rungs 3 (cover-page text) and 4 (NEP bound) and the LLM-assisted
> tail were **not built**. A recovered day must also not be the 1st of the month.
> The committed `data/wp_dates_index.jsonl` held 1,444 entries in the 2026-09-03
> manifest snapshot (1,309 `pdf_meta`, 135 `wayback`). Results by bank:
> [WP_V3_SUMMARY.md](WP_V3_SUMMARY.md).

## The waterfall (June 2026 design)

For each legacy paper, try in order; stop at the first hit. Every recovered
date must satisfy the **month constraint**: RePEc's `YYYY-MM` is authoritative
for the month — a candidate date in a different month is rejected (next rung).

### 1. Wayback Machine CDX — first capture of the PDF (best signal)

```
https://web.archive.org/cdx/search/cdx?url=<pdf_url>&output=json&fl=timestamp&filter=statuscode:200&limit=1
```

- The first 200 snapshot of the official PDF URL is an upper bound on the
  publication date, and for ECB/Fed/BoE it is typically **days** after
  publication (their sites were crawled heavily).
- Also query the paper's landing page on the bank site (when its historical
  URL can be derived) — sometimes captured earlier than the PDF.
- Accept if: snapshot month == RePEc month → use the snapshot day with
  `date_precision="day"`, `date_source="wayback"`.
  If the snapshot is in a *later* month, it's only an upper bound → rung 2.
- Infra note: the codebase already has a Wayback client
  (`cb_corpus/sources/wayback.py`) — extend it with a `first_capture(url)`
  helper instead of writing a new one. Rate-limit ~1 req/s.

### 2. PDF internal metadata (`/CreationDate`)

We already have the PDF on disk (`local_path`) — zero network cost.

- Read the XMP/Info `CreationDate` / `ModDate` with pypdf.
- Banks' publication pipelines typically generate the final PDF hours-to-days
  before release, so CreationDate month usually equals publication month.
- Accept if: CreationDate month == RePEc month → use its day,
  `date_precision="day"`, `date_source="pdf_meta"`.
- Reject obvious garbage: dates before 1990, after the manifest ingest date,
  or epoch defaults (1970-01-01, 1980-01-01).

### 3. Cover-page text of the PDF

Many WP series print the date on the first page ("September 1998",
sometimes "15 September 1998").

- Extract page-1 text (pypdf / pdfminer), regex for
  `(\d{1,2}\s+)?(January|February|...)\s+\d{4}` (+ the bank's local-language
  month names for de/jp).
- A full day+month+year hit consistent with the RePEc month →
  `date_precision="day"`, `date_source="pdf_meta"`.
- Month-year only → no day gain, but it cross-validates the RePEc month.

### 4. NEP announcement date as bounded fallback

The NEP newsletter date (`nep-xxx/YYYY-MM-DD` links on the IDEAS paper page)
is the date the paper was *announced*, 1–4 weeks after publication — an
**upper bound**, never the real date.

- Use only when rungs 1–3 all failed AND the NEP date falls in the same
  month as RePEc's `YYYY-MM`: then the real day is ≤ NEP day, and we keep
  `date = YYYY-MM-01` but record `nep_bound` in `date_source` so the doc is
  at least flagged as "published before the {NEP day}th".
- NEP only exists from ~1998 and only for papers that were announced.

### 5. Give up gracefully

Keep `YYYY-MM-01`, `date_precision="month"`, `date_source="repec"` —
exactly today's behaviour, now explicit instead of silent.

## Committed date index — run once, replay forever

Date recovery is slow (Wayback rate limits, thousands of papers) and partly
non-deterministic (Wayback availability changes, sites die). To make the
corpus **replicable**, the recovered dates are persisted in a versioned index
file committed to the repo:

```
data/wp_dates_index.jsonl     (one line per resolved paper, committed to git)
```

```json
{"key": "ecb:ecbwps:19991001", "title_norm": "some legacy paper title",
 "date": "1999-10-14", "date_precision": "day", "date_source": "wayback",
 "evidence_url": "https://web.archive.org/web/19991014.../pub/pdf/scpwps/ecbwp001.pdf",
 "resolved_at": "2026-06-12"}
```

Only *expensive or non-reproducible* resolutions go in the index
(`wayback`, `pdf_meta`, `llm_crawl`). Dates obtained from live bank-site
listings (`bank_site`) are **never** indexed — they are cheaply
re-derivable by the native scrapers and would bloat the file.

- **`key`** = the RePEc handle (stable, universal join key). For records that
  never had a handle, the key is the normalized title prefixed `t:` (as built,
  `wp_dates.row_key`); `title_norm` is stored alongside. `evidence_url` is a
  `file://` path to the on-disk PDF for `pdf_meta` hits.
- **`evidence_url`** records *where* the date was found, so any entry can be
  re-audited by a human.
- `wp-dates` consults the index **first**: a keyed entry short-circuits the
  whole waterfall (zero network). Fresh resolutions are appended to the index.
- Result: the expensive crawl runs once; every re-build of the corpus from
  scratch (new machine, CI, collaborator) replays dates instantly and
  identically — even if Wayback/bank pages have since changed or died.

### LLM-assisted resolution for the hard tail

For the papers the automated waterfall can't resolve (old, renamed, host
dead), an **LLM agent with web-crawling tools** is an effective last resort:
give it the title + authors + RePEc month and let it search the bank's site,
archives or press releases for the exact publication date.

- Output goes through the **same index file**, with
  `date_source="llm_crawl"` and the mandatory `evidence_url` pointing at the
  page where the date is visible — the LLM's answer is never trusted without
  a human-checkable citation.
- The month constraint still applies (RePEc's `YYYY-MM` is authoritative);
  any LLM-proposed date outside that month is rejected.
- Because results land in the committed index, the LLM step also runs
  **once** — replays are free and deterministic, which is the whole point:
  the manual/agentic effort is capitalized into the repo instead of being
  re-paid at every rebuild.

## Command

```
python -m cb_corpus wp-dates [--banks ...] [--csv path] [--write] [--no-wayback] [--apply-only]
```

- Default banks: `ecb,us,jp,gb,de`; pass `--banks` for the others (the June run
  covered all 13 wired WP banks).
- Default dry-run: prints a summary and writes a CSV (`doc_id, new_date,
  date_source`; default `data/reports/wp_dates.csv`) for human review.
- `--write`: rewrites manifest rows in place (only `date`, `date_precision`,
  `date_source`; never touches `doc_id`/`sha256`/`local_path`) and appends
  new resolutions to `data/wp_dates_index.jsonl`.
- Idempotent: rows already at `date_precision="day"` are skipped; index
  entries short-circuit the waterfall.
- `--no-wayback`: PDF `/CreationDate` only (skips the slow archive.org rung).
- `--apply-only`: replay the committed index onto the manifest, no recovery and
  no network; what a fresh clone runs to materialise dates.

## Expected yield (honest estimate)

- Rung 1 (Wayback): high hit-rate for ECB/Fed/BoE post-2000 URLs; weaker for
  BoJ (URL churn) and pre-1998 anything.
- Rung 2 (PDF meta): decent for 2000s+; old scans often have garbage dates.
- Rung 3 (cover page): mostly month-year — validates but rarely adds the day.
- Net effect: a large majority of post-2000 legacy papers get day precision;
  1990s papers mostly stay at month precision. Month precision is not a
  failure — it is what the publisher itself asserts.
