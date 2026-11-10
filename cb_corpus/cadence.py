"""Cadence watchdog — notices when a `(bank_code, doc_type)` series goes
SILENT (production review: fr D1 unnoticed 213 days, ecb D3 unnoticed 83).
Errors are logged by the sync path; silence was not — this module is the
weekly job that closes that gap.

Method: MEDIAN-GAP ONLY (cadence-watchdog design, 2026-08-24, decision 1). A flat "docs per fixed window" statistic was tried first and
false-alarmed on lumpy quarterly series (ca E1, it/es D1 in the production
review): a series that bursts many documents on (or within a few days of)
one release date every ~90 days, then goes quiet, looks "empty" in any
individual short window even though it is perfectly on schedule.

The median gap is computed over DISTINCT CALENDAR DATES, not over raw
per-document rows. This is a deliberate structural choice, not a detail:
if N documents share (or nearly share) one release date, the doc-to-doc
gaps between them are ~0 and, for a bursty series, EASILY OUTNUMBER the
handful of genuine quarter-to-quarter gaps -- the median would then be
dragged down to ~0 days and next_expected would land the day after the
burst ends, reproducing the exact false-alarm this module exists to kill.
Deduplicating to distinct dates first means a same-day (or tightly
clustered) burst counts as ONE event, so the median reflects the real
release rhythm ("absorbs it naturally", per the design decision) instead
of the burst's internal density.

No network access anywhere in this module: it is pure computation over
`storage.iter_manifest_rows` (already-downloaded manifest data) plus the
`cadence_state.jsonl` state file it owns.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .config import Config
from .storage import iter_manifest_rows

_DEFAULT_OVERDUE_GRACE_DAYS = 7
_DEFAULT_SOON_DAYS = 60
_DEFAULT_MIN_DOCS = 6
_DEFAULT_LOOKBACK_YEARS = 3

_STATUS_ORDER = {"overdue": 0, "soon": 1, "on-track": 2}


# -- env thresholds (re-read on EVERY call, never cached -- same discipline
#    as quarantine.py's _threshold(): a typo in an env var must never crash
#    the weekly job, and thresholds must be tunable between runs/tests
#    without reconstructing anything) -----------------------------------
def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        n = int(raw)
    except ValueError:
        return default
    return n if n > 0 else default


def overdue_grace_days() -> int:
    return _int_env("CADENCE_OVERDUE_GRACE_DAYS", _DEFAULT_OVERDUE_GRACE_DAYS)


def soon_days() -> int:
    return _int_env("CADENCE_SOON_DAYS", _DEFAULT_SOON_DAYS)


def min_docs() -> int:
    return _int_env("CADENCE_MIN_DOCS", _DEFAULT_MIN_DOCS)


def lookback_years() -> int:
    return _int_env("CADENCE_LOOKBACK_YEARS", _DEFAULT_LOOKBACK_YEARS)


# -- pure computation ----------------------------------------------------
def _parse_date(value) -> Optional[date]:
    """Parse a manifest row's `date` field (ISO `%Y-%m-%d`, possibly with a
    time suffix). Missing/None/malformed values return None -- callers
    ignore the row entirely rather than crash (dateless rows are common:
    year/month-precision legacy rows, in-flight discovery rows, etc.)."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def compute_series(cfg: Config, *, today: Optional[date] = None) -> list[dict]:
    """Compute one cadence entry per `(bank_code, doc_type)` series that has
    enough recent history to judge, straight from `iter_manifest_rows` (no
    network, no state file involved).

    A series qualifies when it has at least `min_docs()` dated rows within
    the last `lookback_years()` years (`n_3y` in the output) AND at least 2
    DISTINCT publication dates among them (a single distinct date carries no
    gap information -- skipped, not a crash).

    Output rows are the CONTRACT with the orchestrator dashboard (spec
    design decision 2): lowercase bank_code, doc_type CODE, ISO `%Y-%m-%d`
    dates, and the exact field set
    `{bank_code, doc_type, last, interval_days, next_expected, days_until,
    status, expected_per_year, n_3y}` -- do not add/rename/drop fields
    without updating the spec and the orchestrator's loader together.
    """
    today = today or date.today()
    grace = overdue_grace_days()
    soon = soon_days()
    min_n = min_docs()
    window_start = today - timedelta(days=365 * lookback_years())

    by_series: dict[tuple[str, str], list[date]] = {}
    for row in iter_manifest_rows(cfg):
        d = _parse_date(row.get("date"))
        if d is None:
            continue
        bank = (row.get("bank_code") or "").strip().lower()
        dtype = (row.get("doc_type") or "").strip()
        if not bank or not dtype:
            continue
        by_series.setdefault((bank, dtype), []).append(d)

    entries: list[dict] = []
    for (bank, dtype), dates in sorted(by_series.items()):
        in_window = sorted(d for d in dates if d >= window_start)
        if len(in_window) < min_n:
            continue
        # Distinct calendar dates only -- see module docstring for why.
        distinct = sorted(set(in_window))
        if len(distinct) < 2:
            continue
        gaps = [(b - a).days for a, b in zip(distinct, distinct[1:])]
        interval = round(statistics.median(gaps))
        last = distinct[-1]
        next_expected = last + timedelta(days=interval)
        days_until = (next_expected - today).days
        if today > next_expected + timedelta(days=grace):
            status = "overdue"
        elif days_until <= soon:
            status = "soon"
        else:
            status = "on-track"
        entries.append({
            "bank_code": bank,
            "doc_type": dtype,
            "last": last.isoformat(),
            "interval_days": interval,
            "next_expected": next_expected.isoformat(),
            "days_until": days_until,
            "status": status,
            "expected_per_year": round(365 / interval) if interval else 0,
            "n_3y": len(in_window),
        })
    return entries


def write_cadence_jsonl(cfg: Config, entries: Iterable[dict]) -> Path:
    """Atomically (re)write `data/cadence.jsonl` as the full current snapshot
    (temp file + os.replace, mirroring storage.py's per-bank write pattern,
    `_write_per_bank_unlocked`) -- this
    is a regenerated-weekly report, not an append-only log, so each run
    fully replaces the prior contents."""
    path = cfg.data_dir / "cadence.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return path


# -- state file (new-silence alerting) ------------------------------------
class CadenceState:
    """`data/cadence_state.jsonl` -- append-only, corrupt-line-tolerant, same
    conventions as quarantine.py's state file (see that module's docstring
    for the general house pattern).

    Each row is an EVENT, not a full-row snapshot, because two independent,
    orthogonal facts must survive replay for a given `(bank_code, doc_type)`
    key:
      - the LAST known overdue/recovered status (latest such event wins --
        used to tell a NEW overdue series from an already-known one), and
      - whether the series has EVER been muted (sticky: once muted, stays
        muted -- there is no unmute event in this design; a human edits the
        file directly to lift a mute).
    A quarantine.py-style full-row replace would lose the mute the next time
    an automated overdue/recovery event is appended for that key, defeating
    design decision 5 (mute survives ordinary runs).

    Row shapes:
        {"bank_code", "doc_type", "overdue": true|false}   -- automated
        {"bank_code", "doc_type", "muted": true, "reason": "..."}  -- seeded
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._path = cfg.data_dir / "cadence_state.jsonl"
        self._overdue: dict[tuple[str, str], bool] = {}
        self._muted: dict[tuple[str, str], str] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = self._path.read_bytes()
        warned = False
        for ln in raw.splitlines():
            if not ln.strip():
                continue
            try:
                row = json.loads(ln.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                if not warned:
                    print(f"[cadence] WARNING: corrupt line(s) in {self._path}, "
                          f"skipped", file=sys.stderr, flush=True)
                    warned = True
                continue
            if not isinstance(row, dict):
                if not warned:
                    print(f"[cadence] WARNING: corrupt line(s) in {self._path}, "
                          f"skipped", file=sys.stderr, flush=True)
                    warned = True
                continue
            bank = row.get("bank_code")
            dtype = row.get("doc_type")
            if not bank or not dtype:
                if not warned:
                    print(f"[cadence] WARNING: corrupt line(s) in {self._path}, "
                          f"skipped", file=sys.stderr, flush=True)
                    warned = True
                continue
            key = (bank, dtype)
            if row.get("muted"):
                self._muted[key] = row.get("reason", "")
                continue
            if "overdue" in row:
                self._overdue[key] = bool(row["overdue"])

    def _append(self, row: dict) -> None:
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # -- reads --------------------------------------------------------
    def is_known_overdue(self, bank_code: str, doc_type: str) -> bool:
        return self._overdue.get((bank_code, doc_type), False)

    def is_muted(self, bank_code: str, doc_type: str) -> bool:
        return (bank_code, doc_type) in self._muted

    # -- writes ---------------------------------------------------------
    def record_overdue(self, bank_code: str, doc_type: str, *,
                       today: Optional[date] = None) -> None:
        """Mark `(bank_code, doc_type)` as known-overdue. The file itself is
        append-only (each call appends a new row, never rewrites); the
        resulting in-memory/replayed state is idempotent (repeated
        overdue:true rows for the same key all resolve to the same "known
        overdue" fact). Callers should still only call this for NEWLY
        overdue series -- `apply_state` below only calls it once per
        transition. `today` is recorded on the row purely for operator
        forensics (when the state file is read by a human) -- it is never
        read back by `_load`."""
        self._overdue[(bank_code, doc_type)] = True
        row = {"bank_code": bank_code, "doc_type": doc_type, "overdue": True}
        if today is not None:
            row["date"] = today.isoformat()
        self._append(row)

    def record_recovered(self, bank_code: str, doc_type: str, *,
                         today: Optional[date] = None) -> None:
        self._overdue[(bank_code, doc_type)] = False
        row = {"bank_code": bank_code, "doc_type": doc_type, "overdue": False}
        if today is not None:
            row["date"] = today.isoformat()
        self._append(row)

    def seed_mute(self, bank_code: str, doc_type: str, reason: str = "") -> None:
        """Seed `(bank_code, doc_type)` as muted (design decision 5: a
        known-closed series, e.g. a future decision to retire a report).
        Muted series are still written to cadence.jsonl with their real
        status (dashboard honesty) but never trigger the loud new-overdue
        alert line."""
        self._muted[(bank_code, doc_type)] = reason
        self._append({"bank_code": bank_code, "doc_type": doc_type,
                      "muted": True, "reason": reason})


def apply_state(state: CadenceState, entries: Iterable[dict], *,
                today: Optional[date] = None) -> tuple[int, int, list[str]]:
    """Reconcile freshly computed `entries` against `state`: records newly
    overdue series, releases recovered ones. Returns
    `(overdue_count, new_count, log_lines)`:
      - `overdue_count`: every entry currently `status == "overdue"`
        (matches cadence.jsonl -- dashboard-visible truth, muted or not).
      - `new_count`: newly overdue series that are NOT muted -- a muted
        series' status is still recorded in state (so it behaves correctly
        if ever unmuted) but never counts toward `new_count` and never gets
        a loud line (design decision 5: "never alerted").
      - `log_lines`: one loud line per NEW non-muted overdue series, plus
        one release line per series that recovered (muted or not -- a
        recovery is good news either way, harmless to log).
    Callers are expected to print the summary
    `cadence: N overdue (M new)` themselves and the returned log_lines to
    stderr (CLI concern, kept out of this pure function for testability).
    """
    # Note: a series that ages out of compute_series()'s qualification (e.g.
    # drops below min_docs()/lookback_years() as old rows fall out of the
    # window) simply stops appearing in `entries`. Its last known state
    # (overdue:true or false) is left untouched in `state` -- there is no
    # "series disappeared" event -- so a since-retired series can stay
    # overdue:true in cadence_state.jsonl forever even though it no longer
    # shows up in cadence.jsonl. Accepted bound: harmless (it only suppresses
    # a future NEW OVERDUE line it will never need, since it can't reappear
    # without new qualifying rows), not worth tracking further.
    overdue_count = 0
    new_count = 0
    log_lines: list[str] = []
    for entry in entries:
        key = (entry["bank_code"], entry["doc_type"])
        is_overdue_now = entry["status"] == "overdue"
        was_overdue = state.is_known_overdue(*key)
        if is_overdue_now:
            overdue_count += 1
            if not was_overdue:
                state.record_overdue(*key, today=today)
                if not state.is_muted(*key):
                    new_count += 1
                    log_lines.append(
                        f"cadence: NEW OVERDUE {entry['bank_code']} {entry['doc_type']} "
                        f"(last {entry['last']}, expected {entry['next_expected']}, "
                        f"{-entry['days_until']}d late)"
                    )
        elif was_overdue:
            state.record_recovered(*key, today=today)
            log_lines.append(
                f"cadence: RECOVERED {entry['bank_code']} {entry['doc_type']} "
                f"(last {entry['last']})"
            )
    return overdue_count, new_count, log_lines


# -- CLI entry point --------------------------------------------------------
def _format_table(entries: list[dict]) -> str:
    if not entries:
        return "cadence: no series with enough history to judge"
    ordered = sorted(entries, key=lambda e: (
        _STATUS_ORDER.get(e["status"], 9), e["bank_code"], e["doc_type"]))
    header = (f"{'bank':<6}{'type':<6}{'status':<10}{'last':<12}"
              f"{'next_expected':<16}{'days_until':>11}  {'~/yr':>4}  n_3y")
    lines = [header]
    for e in ordered:
        lines.append(
            f"{e['bank_code']:<6}{e['doc_type']:<6}{e['status']:<10}{e['last']:<12}"
            f"{e['next_expected']:<16}{e['days_until']:>11}  {e['expected_per_year']:>4}  {e['n_3y']}"
        )
    return "\n".join(lines)


def run_cadence_watch(cfg: Config, *, write: bool = False,
                      today: Optional[date] = None) -> list[dict]:
    """Compute the current cadence table, print it, and -- only when
    `write=True` -- persist `data/cadence.jsonl` and update
    `data/cadence_state.jsonl` (new-overdue/recovery alerting). Dry-run
    (default) is read-only: no jsonl, no state mutation, no alert lines --
    it just shows the table (CLI contract per spec design decision 4)."""
    entries = compute_series(cfg, today=today)
    print(_format_table(entries))
    if write:
        write_cadence_jsonl(cfg, entries)
        state = CadenceState(cfg)
        overdue_count, new_count, log_lines = apply_state(state, entries, today=today)
        for line in log_lines:
            print(line, file=sys.stderr, flush=True)
        print(f"cadence: {overdue_count} overdue ({new_count} new)",
              file=sys.stderr, flush=True)
        # Push ntfy notification on NEW overdue series
        if new_count > 0 and (ntfy_url := os.getenv("NTFY_URL")):
            try:
                subprocess.run(
                    ["curl", "-fsS", "-m", "10", "-H", f"Title: cb_corpus cadence {new_count} new overdue",
                     "-d", f"{new_count} new overdue series", ntfy_url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True
                )
            except Exception:
                print("cadence: NTFY FAILED (notification not delivered)", file=sys.stderr, flush=True)
    return entries
