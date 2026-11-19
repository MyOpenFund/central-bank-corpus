# NAS Deployment (Dockge) — runbook

Design history (specs and plans) is kept outside this repository; this
runbook is the operational reference.
Absolute rule: **no real infra value** (IP, hostname, real /mnt paths,
UID) must ever be committed — values live in Dockge and in untracked
local notes (`*.local.md`).

## 0. Prerequisites (one-time)

1. **Deploy key** (on the Mac):
   `ssh-keygen -t ed25519 -f nas_deploy_key -N "" -C "cb-corpus-nas-state"`
   GitHub → repo → Settings → Deploy keys → "Add deploy key", paste
   `nas_deploy_key.pub`, **check "Allow write access"**.
2. **GHCR visibility**: after the first CI build, GitHub → profile → Packages →
   `central-bank-corpus` → Package settings → Change visibility → **Public**
   (otherwise the NAS can't pull without authentication).

## 1. Path/UID discovery (disposable stack)

Dockge → new stack `cb-probe` → paste `compose.discover-ids.example.yml`
→ Deploy → read the logs: note the `/mnt/<pool>/<dataset>` path of the
SMB share and the owning UID/GID (files created via the SMB share). Delete the stack.

## 2. Initial seed (MANDATORY before the first run)

Without a seed, the first run would re-download ~38,000 documents and the
non-recrawlable Wayback state would be lost. From the Mac, with the SMB share mounted:

```bash
# adjust the destination to the share mounted in Finder
DST="/Volumes/<share>/<dataset_path>"
rsync -rt --progress "data/manifest" "$DST/"
rsync -rt --progress "data/wp_dates_index.jsonl" "$DST/"
rsync -rt --progress "data/raw" "$DST/"      # 8.2 GB — several hours

# integrity check (counts must match)
find data/raw -type f | wc -l
find "$DST/raw" -type f | wc -l
ls data/manifest/*.jsonl | wc -l
ls "$DST/manifest/"*.jsonl | wc -l
```

After the seed: the Mac **stops crawling**; its `data/` becomes an archive
(do not delete without an explicit decision).

The container **refuses** to run on a volume without manifests (status
`REFUSED` in `nas_runs.log`) — this is the missing-seed protection;
`CB_ALLOW_EMPTY_DATA=1` for a deliberately empty bootstrap.

## 3. `cb-refresh` stack

Dockge → new stack `cb-refresh` → paste `compose.refresh.example.yml` →
replace `POOL/DATASET/PUID/PGID` → drop the private key `nas_deploy_key`
into the stack's folder under the name `deploy_key` (Dockge file editor)
→ Deploy.

The `deploy_key` file must be **readable** by the PUID user:
autocommit copies the key as 0600 into a private temp directory (0700, removed at the end of the run), so a 0644 key works;
a root:0600 key will fail with a clear message in `nas_runs.log`.

Schedule: bounded sync nightly at 01:00 (Paris) Monday–Saturday, unbounded
full sync at 01:00 Sunday. Each sync catalogs once for all banks (bis-sitemap,
repec), then proceeds to parallel native bank-site discovery. Scope for the
native phase is controlled by env vars (Dockge, no rebuild needed):
`DISCOVER_BANKS` (`all` or comma list — required, the job refuses to run
without it), `DISCOVER_TYPES` (`full` = whole A–F scope, or comma list),
`DISCOVER_ROUNDS` (1 = incremental), `DISCOVER_WORKERS` (parallel banks,
default 6 — the single knob bounding CPU/RAM/bandwidth),
`DISCOVER_BANK_TIMEOUT` (seconds per bank before the crawl is killed and the
bank is counted as failed, default 10800).

`SYNC_WINDOW_DAYS` (e.g. `"90"`) bounds the nightly catalog phase to a
freshness window: it caps the BIS sitemap walk to `--years <y0>-<y1>`
(computed from `now - SYNC_WINDOW_DAYS`) and switches RePEc to
`--incremental` (skip papers already known by their IDEAS page, stop each
series at the first fully-known listing page). Unset or empty
means every night runs the full, unbounded catalog walk (the pre-2026-07-16
behavior). The `sync full` job argument (used by the Sunday cron line) always
runs the unbounded walk regardless of `SYNC_WINDOW_DAYS`, so late backfills
and corrections on either catalog are never permanently missed. Log line
`[sync] START (window <n>d)` vs `[sync] START (full)` in `nas_runs.log`
records which mode ran.

**Count-semantics change:** on a windowed (incremental) night, the RePEc
phase logs only *new* work discovered per series, not full per-series counts
— a low number on a bounded night is expected, not a regression. Those
per-series counts appear in the container/Dockge logs, not in
`nas_runs.log`, which records only the run's START mode marker, `[sync]
catalogs OK`, and the native discovery summary line. The Sunday full sweep
is the one that re-walks everything and produces full audit counts; use it
(not the nightly numbers) to judge whether a series is actually stalled.

**Quarantine:** URLs that fail to download for N distinct nights are quarantined
and stop being retried in nightly syncs, freeing bandwidth for healthy URLs. The
quarantine state lives in `data/download_quarantine.jsonl` (operational state,
never committed — alongside `download_errors.jsonl`). Quarantined URLs are retried
during the Sunday full sweep (via `QUARANTINE_RETRY=1`, set automatically by the
`sync full` job). Tune the quarantine threshold with `QUARANTINE_AFTER_NIGHTS`
(default: 5 nights of failure before quarantine). A "night" is the **local** date
(the container's `TZ`, Europe/Paris), taken once per `Storage` instance, i.e. once per
process run of a command, so a run that crosses midnight counts as one night. Monday–Saturday bounded syncs
do not bypass quarantine, keeping them nimble. For docs confirmed unrecoverable
after a manual `recover-downloads --candidates` hunt, `recover-downloads
--seed-quarantine <file>` is a one-time operator action that seeds them as
already-quarantined immediately (no need to accumulate synthetic failed
nights) — run it against (or copy its resulting `download_quarantine.jsonl`
into) the same data directory the NAS deployment actually uses, otherwise the
seeded state never reaches the running container. A `duplicate` verdict from
`recover-downloads` (the dead URL's bytes or doc_id match a document already
in the corpus) now self-heals: the dead URL is stamped onto the matching
row's `alt_urls` and its quarantine entry is released, so it stops
reappearing as a fresh failure on every future nightly/recover pass instead
of being rediscovered as `duplicate` forever — but only when that row is a
real PDF (no `html_path`; `Config.stamp_duplicate_html` relaxes this). With an
HTML owner (including a page rendered to PDF) the dead URL is neither stamped
nor released: it stays quarantined and the next recover pass re-evaluates it.
The parallel discover workers (`DISCOVER_WORKERS`) all write this one file:
every write takes its sidecar lock (`.download_quarantine.jsonl.lock`, never
delete it while a job runs) and first replays what the other workers appended,
so their night counts and releases never overwrite each other. Each process
still decides what to skip from the state it read at startup.

**Content check (bad-content):** a download that answers HTTP 200 with the
wrong bytes — a consent wall or error page served as HTML for a PDF URL, an
HTML page labelled `application/pdf`, a stub under `Config.min_pdf_bytes`
(1,024), a PDF cut off before its `%%EOF` (`Config.pdf_require_eof`) — is not
stored: the next fallback URL is tried, and when none passes the document is
a download error (`ContentCheckFailed` in `download_errors.jsonl`, the short
reasons first), counted in the quarantine like a dead URL, with one
`[<label>] bad-content: N` line per batch in the logs and a `bad_content`
counter (a sub-count of `docs_failed`) per source and in the totals of the
night's `runs.jsonl` line; the exit code is unchanged. A burst of
`bad-content` on one bank usually means the site put a wall in front of its
PDFs. Like any dead URL, a rejected URL is quarantined after
`QUARANTINE_AFTER_NIGHTS` nights and then re-alerts on every Sunday full
sweep (`QUARANTINE_RETRY=1`) until the site serves the real document. A body
that starts with `%PDF` is judged as a PDF even when the server labels it
`text/html`: stored as a PDF when it passes the check, refused for its PDF
reason (truncated, too small) when it does not — never stored as HTML.

The three thresholds are set through the stack's `environment:` (Dockge, no
rebuild; a commented example is in `compose.refresh.example.yml`):

| Variable | Field | Default | Values |
|---|---|---|---|
| `CB_MIN_PDF_BYTES` | `Config.min_pdf_bytes` | `1024` | an int >= 0 (`0` disables the size floor) |
| `CB_PDF_REQUIRE_EOF` | `Config.pdf_require_eof` | `1` | `1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off` |
| `CB_PDF_URL_SERVED_HTML` | `Config.pdf_url_served_html` | `reject` | `reject`, or `accept` (store the HTML a PDF URL served, the old behaviour) |

An unset or empty variable means the default. An invalid value never stops
a night: it prints one `[config] WARNING: invalid <VAR>=...` line on stderr
and the default applies — grep the logs for it after changing one.

`reindex-from-disk` reports an on-disk PDF that fails the same check as
`skip:bad-content` (`bad_content=N`), never indexes it and leaves the file in
place for inspection. Rows indexed before this check (2026-11) are not
re-verified. Artifacts are written to a hidden `.<name>.<random>.part` temp
in the same directory, then renamed into place (they keep the umask's mode);
a temp left by a killed process is reported by `sweep-orphans` as a stale
temp once older than `--temp-min-age-hours` (default 24).

**Migration:** stacks created before 2026-07-15 used the refresh/discover job
pair — the crontab and job names changed; recreate the stack after re-pulling
the image. Stacks created before 2026-07-16 ran a single unbounded sync every
night — after re-pulling the image, add `SYNC_WINDOW_DAYS` to the compose
environment (or leave it unset to keep the old full-nightly behavior; the
crontab image update still switches Sunday to `sync full`). Stacks created
before this image must also manually add `stop_grace_period: 60s` to BOTH
stacks' compose in Dockge — re-pulling the image does not update the compose
files.

## 4. `cb-campaign` stack (on demand)

Dockge → stack `cb-campaign` → paste `compose.campaign.example.yml` →
replace the placeholders and the `command:` line → drop the private key `nas_deploy_key`
into the stack's folder under the name `deploy_key` → Deploy. The container
waits for any running sync or campaign to finish (lock), runs, pushes the state, stops.
To launch another campaign: re-edit `command:` + Deploy.

Maintenance example — orphan sweep (see README § Maintenance): first
`command: ["/app/deploy/run-job.sh", "campaign", "sweep-orphans"]` (dry-run; read
`reports/orphan_sweep_<ts>.summary.json`), then the same with `"--move"`.
`temps_stale` there counts `.part` temps older than `--temp-min-age-hours`
(default 24) left by a killed job; `--move` moves them to `raw_orphans/` too,
counted in `temps_moved` / `temps_move_failed` (`moved` / `move_failed` stay
the duplicates').

Measurement example — stored PDFs against the content check (issue #21; read-only, no
network): `command: ["/app/deploy/run-job.sh", "campaign", "audit-pdfs"]`. Read the JSON
summary in the container log (`by_reason`, `by_series`, and `eof_from_end`: where the last
`%%EOF` of each `no-eof-in-window` file sits) and `reports/pdf_audit.csv` (one line per
failing row). `no-eof-in-window` rows are the files `CB_PDF_REQUIRE_EOF=1` would refuse
at their next download or reindex; `not-pdf` and `too-small` rows are stored documents
that are not the document (error pages, truncated files) and need a re-download
(or a re-render when the CSV's `rendered` column says `yes`); `unreadable` rows name the
error (e.g. a permission the container's PUID lacks). The exit code is 0 whatever the
findings (it is a measure). Set `AUTOCOMMIT=0` in the one-shot's environment if nothing at
all may be pushed: reading the manifests repairs a torn final line in place, like every
command that reads them.

Repair example — fix what `audit-pdfs` found (README § Maintenance: replace or retire a bad
artifact). The plan is shipped in the image under `/app/deploy/plans/` (the Dockerfile's
`COPY deploy /app/deploy`; `.dockerignore` only drops `*.md`), so the image must be the one
built from the commit that ships the plan (§7). First the dry run (it fetches the candidates,
including Wayback, which the NAS reaches; writes only `reports/replace_artifact.csv`):
`command: ["/app/deploy/run-job.sh", "campaign", "replace-artifact", "--plan", "/app/deploy/plans/2026-11-19-audit-pdfs.csv"]`.
Read the JSON summary in the container log: each row's `outcome` (`would-replace`,
`would-retire`, `unresolved`, `duplicate-of:<doc_id>`, `error`), `winning_url`, sizes,
sha256s and `tried` (every candidate with its reason). Then the same `command:` with
`"--write"` appended: the files are replaced in place, the retired file goes to
`raw_orphans/`, and the autocommit pushes the manifest changes. A third run (dry or
`--write`) must report only `already-fixed` / `already-retired` (plus whatever stayed
`unresolved`); `already-fixed` assumes the winning URL still serves the same bytes, so a
`would-replace` there means the live file changed, not that the write was lost. Finally
re-run `audit-pdfs`: it should report only the rows still unresolved (in this plan, Banque
de France WP 101 and Riksbank WP 397 when no Wayback capture passes; WP 397 then goes to a
later plan as `retire,duplicate-of:c8d8705ae2aaa19d`).

Maintenance example — policy-dates backfill (see README § Maintenance: policy-dates
backfill): first `command: ["/app/deploy/run-job.sh", "campaign", "policy-dates"]`
(dry-run; read `reports/policy_dates.csv` on the volume), then the same with `"--write"`
(manifest date keys only; the autocommit pushes them), then the dry-run once more: it must
report `unchanged` only. The image must be the one built from the commit that ships the
command (§7) before the stack is deployed.

Migration example — the `pdf_url` -> `document_url` field rename (README § Maintenance:
migrate-document-url), run once, **after** `cb-refresh` runs the image that ships the command
(that image reads both names; the previous one reads only `pdf_url` and, given a migrated
manifest, would see no known URL at all and re-fetch everything), and after the vault was
deployed with its own `document_url` change (the previous vault NULLs the column on migrated
rows). In order: `cb-refresh` → Mettre à jour; then `cb-campain` with
`command: ["/app/deploy/run-job.sh", "campaign", "migrate-document-url"]` and "Mettre à jour"
(pulls the same image; dry-run: per-file counts in the container log, nothing written; expect
`both=0`), then the same with `"--write"` (every manifest and `download_errors.jsonl` rewritten
under its sidecar lock; the autocommit pushes the manifests), then the dry-run once more:
`renamed=0` everywhere.

Exit 1 has two causes, both visible in the log: a row carrying both names (left untouched, its
`doc_id` is printed) or a malformed line before the end of a manifest (the run stops there;
files sorted before it are already rewritten). On exit 1 `run-job.sh` marks the run FAILED and
skips the autocommit, so the files already rewritten are pushed by the next run's autocommit;
fix the cause and run `--write` again (it is safe to repeat). A manifest whose last line is torn
is rewritten too (the torn line saved to `.torn`, as every rewrite does), even with no rename.

## 4a. Weekly cadence watchdog (automated)

The cadence watchdog detects silent series — banks/doc-types with no new
publications for longer than their median publication interval. It runs
Sundays at 06:30 (after the full sweep completes) via `run-job.sh cadence`.

**Output:** `data/cadence.jsonl` (beside `manifest/`, auto-gitignored) contains
one JSON line per series: `{bank_code, doc_type, last, interval_days,
next_expected, days_until, status, expected_per_year, n_3y}`. Status is
`overdue` (>7d past expected), `soon` (≤60d to expected), or `on-track`.

**Alerting:** State file `data/cadence_state.jsonl` remembers known-overdue
series — the job logs only NEW silences, one line per series per run (no
alert noise on recovery). `run-job.sh` routes the cadence job's stderr into
`nas_runs.log`, so both the per-series `NEW OVERDUE ...` lines and the
summary `cadence: N overdue (M new)` land there — not just the container
console.

**Date precision:** rows whose `date_precision` is `year` are dropped before the
median and `last` — their date is a 1 January placeholder, not a release day (ECB
E4 bulletins, RePEc year-only fallbacks). Month-precision rows are kept: they sit
on the 1st but still carry the cadence at monthly granularity (a quarterly report
dated by its period, monthly-dated minutes). A row without the field counts as
before. The output fields do not change; the table shows the dropped rows per
series in a `coarse` column, lists series left with no judgeable rows as
`unjudged`, and the job writes one stderr line per affected series (`cadence: ecb
E4: 109 year-precision row(s) dropped; series not judged`) into `nas_runs.log`. These lines are a
report, not an alert (no ntfy push). `CADENCE_DROP_PRECISIONS` sets which
precisions are dropped: default `year`; `month,year` judges day rows only (on the
committed manifests it leaves `au E1`, `ca A3`, `ecb E1`, `gb A3` and `gb E1`
unjudged and turns `ca D1` and `nl D1` overdue); `none` drops nothing; an invalid
value falls back to the default. When `year` is dropped, in a series that has at
least one dropped row, rows WITHOUT a precision (field missing or null) dated
1 January are dropped too and counted with them: they are the same placeholders written before the field
existed, and keeping them while the new ones are dropped would freeze `last` (ECB
E4: its new bulletins are labelled `year`, its legacy ones are not, so from 2027
the series would read overdue while publishing). A series with no dropped row
keeps its unlabelled rows, and so does every series when `year` is kept
(`month` or `none`): the unlabelled 1 January rows always share the fate of the
rows labelled `year`. On the committed manifests (2026-11-18) the only change
versus before is `ecb E4`: `unjudged` (109 placeholder rows dropped) instead of
`soon` on a 366-day interval; every other series keeps its status.

**Known coverage gap:** a series with fewer than `CADENCE_MIN_DOCS` dated rows in
the lookback window, or fewer than 2 distinct dates, is not judged at all — no
status, never overdue. That covers the sparsest series and every series whose
recent rows are all of a dropped precision. A silent discovery failure on an
unjudged series is caught only by the completeness report
(`python -m cb_corpus report`) or by a human.

**Muting:** Deliberately-closed series (e.g. a future decision) are silenced
by seeding a `{"bank_code", "doc_type", "muted": true, "reason"}` line in
the state file — muted series are still written to cadence.jsonl (dashboard
transparency) but never alerted.

**After the `policy-dates` backfill** (README § Maintenance: policy-dates backfill) the next
weekly run reads corrected dates. Expect exactly this, and nothing else:

- `gb A3` and `gb E1` get real intervals — about 46 and 92 days instead of 366 (all their rows
  used to sit on 1 January). No alert. (Month precision is kept by the default
  `CADENCE_DROP_PRECISIONS=year`; with `month,year` both series become `unjudged`.)
- **One new series, `jp A3`, is reported overdue** (`NEW OVERDUE jp A3`, logged once). It was
  not judged before (too few dated rows). It is structural, not a silence: BoJ minutes are
  dated at their meeting and released after the following meeting, so the newest row is
  always about one interval old and the series alerts again after each recovery.
  To acknowledge: do nothing — the state file records it as known-overdue and stays quiet
  until it recovers. To mute for good, append this line to `cadence_state.jsonl` at the root of
  the data volume (`/app/data/cadence_state.jsonl` in the container, next to `manifest/`),
  before the run or at any time later. End it with a newline: without one the next
  automatic append is glued to it, the line is skipped as corrupt and the mute is lost.

  ```json
  {"bank_code": "jp", "doc_type": "A3", "muted": true, "reason": "minutes dated at the meeting, released after the following one"}
  ```

**Tuning:** Env vars override thresholds (no hard-coded constants):
`CADENCE_OVERDUE_GRACE_DAYS` (default 7), `CADENCE_SOON_DAYS` (default 60),
`CADENCE_MIN_DOCS` (default 6, minimum to detect a median), `CADENCE_LOOKBACK_YEARS`
(default 3), `CADENCE_DROP_PRECISIONS` (default `year`, see Date precision).

## 5. Sanity checks

- `data/reports/nas_runs.log` and `last_run_status` visible in Finder (SMB).
- A recent PDF appears under `raw/<bank>/...` in Finder.
- A `data: NAS sync <date>` commit appears on GitHub after a useful run, with `[sync] catalogs OK` in the log output.
- Files created by the container belong to you via SMB (otherwise revisit PUID/PGID).
- A Dockge stop/redeploy mid-run kills the current job (status `FAILED` or
  absent) — the lock is released automatically and the next cron tick
  picks up again; this is expected.
- After the nightly sync: per-bank discovery logs under `data/reports/discover/<date>/`
  (`<date>` is the container-local calendar date — `TZ=Europe/Paris` in prod —
  not UTC, so the 01:00-CEST Monday run lands in Monday's directory instead of
  clobbering Sunday's full-sweep logs), one summary line `OK n/n` or
  `PARTIAL k/n FAILED: <codes>` in `nas_runs.log`, and a `data: NAS sync
  <date>` commit on GitHub when discovery changed. The same directory also
  holds `catalogs.log` (tee'd stdout+stderr of the bis-sitemap/RePEc phases —
  survives a Dockge Update mid-run, unlike a traceback that only ever hit the
  terminal) and a `[sync] bis-sitemap OK` heartbeat line in `nas_runs.log`
  between the two catalog phases. Monday–Saturday this is a bounded sync
  (`[sync] START (window <n>d)`, lower RePEc counts — incremental, new work
  only; counts in the container logs, `nas_runs.log` keeps only the mode
  marker, `bis-sitemap OK`, and `catalogs OK`); Sunday it is the full sweep
  (`[sync] START (full)`) with the full audit counts.
- A `PARTIAL` status is not an emergency: failed banks are retried the next
  night. Investigate a bank only when it fails several nights in a row
  (its log under `reports/discover/<date>/<code>.log` has the traceback).
- `data/reports/duplicate_stamps.jsonl`: one JSON line per URL found to
  duplicate a row that already owns its content (`skip:duplicate-content` in the
  nightly sync; in `recover-downloads` also its `skip:already-indexed` self-heals
  and a candidate's final URL, so one recovered record can log two lines; see
  `label`), with `persisted` = whether the URL was
  stamped onto the owner row (`owner_doc_id`, `owner_bank`), plus an `error`
  field when the stamping itself failed. Only a real PDF owner (no `html_path`)
  is stamped by default, so a duplicate of an HTML page — or of a page rendered
  to PDF — is fetched and re-logged (`persisted: false`) every night: expect the
  file to grow by those lines nightly. It is append-only, never read back by the
  crawler, and safe to rotate or truncate (e.g. keep the last N months).
- A `cb-campaign` run that had to queue behind an in-progress sync logs
  `[campaign] WAITING (lock busy)` in `nas_runs.log` before it blocks — if a
  campaign looks stuck, check for that line plus whichever job is currently
  running (its own `START` line without a matching `OK`/`PARTIAL`/`FAILED`
  yet).

## 6. Status semantics

`data/reports/last_run_status` holds exactly one line summarizing the most
recent run — treat it as "freshness", not as history (`nas_runs.log` has the
history):

- `OK n/n [job]` — full success.
- `PARTIAL k/n FAILED: <codes> [job]` — some banks failed; autocommit still
  runs (partial progress is real progress). Not an emergency — see §5.
- `FAILED [job] rc=<n>` — the job errored out (a catalog phase, all banks
  failed, a malformed `SYNC_WINDOW_DAYS`, ...); no autocommit.
- `REFUSED [job]` — the volume had no manifests (missing-seed protection,
  §2); no phase ran.
- `SKIPPED [job]` — the global lock was already held (a previous sync or
  campaign still in progress); this run did nothing. A `SKIPPED` line
  overwrites the previous status on disk, so an operator checking only the
  file's last-write time can no longer mistake a skipped night for a fresh
  `OK` — check `nas_runs.log` for who held the lock.

### Sunday error barrage (transitional)

Until `repec-reconcile` exists (tracked separately, not yet written), the
Sunday full sweep is expected to log on the order of ~400 `gb`/`fr` lines to
`data/download_errors.jsonl` per run — a known RePEc/native reconciliation
gap for those two banks, not a new incident. Don't read a noisy Sunday as a
regression; conversely, don't read a *quiet* Sunday (well under ~400) before
`repec-reconcile` ships as a sign of health either — it more likely means the
sweep aborted early. Re-measure and remove this note once `repec-reconcile`
lands.

### Autocommit: the volume always wins

`autocommit.sh` copies `data/manifest/*.jsonl` and `data/wp_dates_index.jsonl`
from the running container's volume as-is and pushes them as a new commit —
it does not diff or merge against the previously committed state beyond
git's own history (clone, add, commit, rebase, push). The volume is the
single source of truth for every autocommit: if it ever regresses (a bad
restore, a manual edit, a stale seed re-applied), the next successful run
will commit that regression as-is. The only push-side guard is the
per-manifest JSON validation from the H1 hardening pass — a *malformed*
manifest is refused, a *valid-but-stale* one is not detected as such.

## 7. Code updates

Push to master → CI rebuilds `ghcr.io/myopenfund/central-bank-corpus:latest` → in Dockge:
re-pull the image and redeploy the stacks.

After a long gap since the last rebuild, review `deploy/Dockerfile`'s base
image tag before rebuilding — an unpinned/floating tag can jump several
upstream versions between rebuilds, and that drift is easier to catch and
pin deliberately than to debug after the fact.
