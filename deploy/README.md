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

**Muting:** Deliberately-closed series (e.g. a future decision) are silenced
by seeding a `{"bank_code", "doc_type", "muted": true, "reason"}` line in
the state file — muted series are still written to cadence.jsonl (dashboard
transparency) but never alerted.

**Tuning:** Env vars override thresholds (no hard-coded constants):
`CADENCE_OVERDUE_GRACE_DAYS` (default 7), `CADENCE_SOON_DAYS` (default 60),
`CADENCE_MIN_DOCS` (default 6, minimum to detect a median), `CADENCE_LOOKBACK_YEARS`
(default 3).

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
