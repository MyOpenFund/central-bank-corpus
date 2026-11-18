"""cb_corpus/cadence.py — median-gap cadence watchdog.

Pure-computation tests over `iter_manifest_rows` (no network, mirrors
tests/test_wp_v3.py's fixture style). The critical regression case is
`test_lumpy_quarterly_burst_is_not_overdue_during_quiet_period`: the flat
docs-per-window method this design replaces false-alarmed on exactly that
shape (a series that bursts many documents around one release date every
~90 days, then goes quiet) -- median-gap over DISTINCT dates must absorb
that rhythm instead of flagging the quiet stretch as overdue.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from cb_corpus.cadence import (
    CadenceState, apply_state, compute_series, run_cadence_watch,
    write_cadence_jsonl,
)
from cb_corpus.cli import main as cli_main
from cb_corpus.config import Config
from cb_corpus.models import DocRecord
from cb_corpus.storage import _write_per_bank_unlocked
from cb_corpus.taxonomy import DocType


def _mk_cfg(tmp_path) -> Config:
    return Config(data_dir=tmp_path)


def _rec(bank_code, doc_type, d, **kw) -> DocRecord:
    base = dict(bank_code=bank_code, doc_type=doc_type, title="t",
               pdf_url=f"https://x.test/{bank_code}-{doc_type.code}-{d.isoformat()}-{kw.get('_i', 0)}.pdf",
               date=d)
    kw.pop("_i", None)
    base.update(kw)
    return DocRecord(**base)


def _write(cfg, recs) -> int:
    return _write_per_bank_unlocked(cfg, [r.to_row() for r in recs])


# -- the critical regression: lumpy quarterly bursts ----------------------

def test_lumpy_quarterly_burst_is_not_overdue_during_quiet_period(tmp_path):
    """12 quarterly bursts of 10 docs each, all sharing ONE date per burst
    (a report/bulletin release event) -- ~90 days apart. `today` sits 30
    days into the 60-day quiet stretch after the last burst (60 days before
    the NEXT expected burst). The old flat docs-per-window method flagged
    this as overdue (the current window is "empty"); median-gap must not.
    """
    cfg = _mk_cfg(tmp_path)
    start = date(2023, 1, 15)
    burst_dates = [start + timedelta(days=90 * i) for i in range(12)]
    recs = []
    for bd in burst_dates:
        for i in range(10):
            recs.append(_rec("ca", DocType.E1, bd, _i=i))
    _write(cfg, recs)

    today = burst_dates[-1] + timedelta(days=30)
    entries = compute_series(cfg, today=today)
    assert len(entries) == 1
    e = entries[0]
    assert e["bank_code"] == "ca" and e["doc_type"] == "E1"
    assert e["interval_days"] == 90
    assert e["status"] != "overdue"
    assert e["status"] == "soon"  # next_expected is exactly 60 days out
    assert e["days_until"] == 60


def test_lumpy_burst_on_track_earlier_in_the_quiet_period(tmp_path):
    cfg = _mk_cfg(tmp_path)
    start = date(2023, 1, 15)
    burst_dates = [start + timedelta(days=90 * i) for i in range(12)]
    recs = []
    for bd in burst_dates:
        for i in range(8):
            recs.append(_rec("ca", DocType.E1, bd, _i=i))
    _write(cfg, recs)

    today = burst_dates[-1] + timedelta(days=1)
    entries = compute_series(cfg, today=today)
    assert entries[0]["status"] == "on-track"


# -- steady series gone silent ---------------------------------------------

def test_steady_monthly_series_gone_silent_is_overdue(tmp_path):
    cfg = _mk_cfg(tmp_path)
    start = date(2024, 1, 1)
    dates = [start + timedelta(days=30 * i) for i in range(12)]
    recs = [_rec("ecb", DocType.D3, d) for d in dates]
    _write(cfg, recs)

    today = dates[-1] + timedelta(days=200)  # silence far beyond grace
    entries = compute_series(cfg, today=today)
    assert len(entries) == 1
    e = entries[0]
    assert e["interval_days"] == 30
    assert e["status"] == "overdue"
    assert e["next_expected"] == (dates[-1] + timedelta(days=30)).isoformat()


# -- exactly-at-boundary cases ----------------------------------------------

def _six_doc_series_10d_apart(cfg, bank="us", dtype=DocType.D1, start=date(2025, 1, 1)):
    dates = [start + timedelta(days=10 * i) for i in range(6)]
    _write(cfg, [_rec(bank, dtype, d) for d in dates])
    return dates


def test_overdue_grace_boundary_exactly_7d_is_not_yet_overdue(tmp_path):
    cfg = _mk_cfg(tmp_path)
    dates = _six_doc_series_10d_apart(cfg)
    next_expected = dates[-1] + timedelta(days=10)
    today = next_expected + timedelta(days=7)  # exactly the grace boundary
    e = compute_series(cfg, today=today)[0]
    assert e["status"] != "overdue"


def test_overdue_grace_boundary_8d_is_overdue(tmp_path):
    cfg = _mk_cfg(tmp_path)
    dates = _six_doc_series_10d_apart(cfg)
    next_expected = dates[-1] + timedelta(days=10)
    today = next_expected + timedelta(days=8)
    e = compute_series(cfg, today=today)[0]
    assert e["status"] == "overdue"


def test_soon_boundary_exactly_60d_out_is_soon(tmp_path):
    cfg = _mk_cfg(tmp_path)
    dates = _six_doc_series_10d_apart(cfg)
    next_expected = dates[-1] + timedelta(days=10)
    today = next_expected - timedelta(days=60)
    e = compute_series(cfg, today=today)[0]
    assert e["status"] == "soon"
    assert e["days_until"] == 60


def test_soon_boundary_61d_out_is_on_track(tmp_path):
    cfg = _mk_cfg(tmp_path)
    dates = _six_doc_series_10d_apart(cfg)
    next_expected = dates[-1] + timedelta(days=10)
    today = next_expected - timedelta(days=61)
    e = compute_series(cfg, today=today)[0]
    assert e["status"] == "on-track"


# -- dateless rows ignored ---------------------------------------------------

def test_dateless_rows_are_ignored_not_crashed_on(tmp_path):
    cfg = _mk_cfg(tmp_path)
    dates = [date(2025, 1, 1) + timedelta(days=10 * i) for i in range(6)]
    recs = [_rec("us", DocType.D1, d) for d in dates]
    dateless = DocRecord(bank_code="us", doc_type=DocType.D1, title="no date",
                         pdf_url="https://x.test/nodate.pdf", date=None)
    _write(cfg, recs + [dateless])

    entries = compute_series(cfg, today=dates[-1] + timedelta(days=1))
    assert len(entries) == 1
    assert entries[0]["n_3y"] == 6  # the dateless row is not counted


def test_too_few_docs_yields_no_entry(tmp_path):
    cfg = _mk_cfg(tmp_path)
    dates = [date(2025, 1, 1) + timedelta(days=10 * i) for i in range(5)]  # < default min 6
    _write(cfg, [_rec("us", DocType.D1, d) for d in dates])
    assert compute_series(cfg, today=dates[-1]) == []


def test_single_distinct_date_yields_no_entry(tmp_path):
    """>= min_docs rows but they all share ONE date -- no gap to compute."""
    cfg = _mk_cfg(tmp_path)
    d = date(2025, 6, 1)
    recs = [_rec("us", DocType.D1, d, _i=i) for i in range(8)]
    _write(cfg, recs)
    assert compute_series(cfg, today=d + timedelta(days=1)) == []


# -- output contract ----------------------------------------------------

def test_output_contract_fields_and_lowercase_bank_code(tmp_path):
    cfg = _mk_cfg(tmp_path)
    dates = [date(2025, 1, 1) + timedelta(days=10 * i) for i in range(6)]
    _write(cfg, [_rec("GB", DocType.D1, d) for d in dates])  # uppercase in
    e = compute_series(cfg, today=dates[-1])[0]
    assert e["bank_code"] == "gb"
    assert e["doc_type"] == "D1"
    assert set(e.keys()) == {
        "bank_code", "doc_type", "last", "interval_days", "next_expected",
        "days_until", "status", "expected_per_year", "n_3y",
    }
    assert e["last"] == dates[-1].isoformat()
    assert e["status"] in {"overdue", "soon", "on-track"}
    assert e["expected_per_year"] == round(365 / e["interval_days"])


def test_write_cadence_jsonl_writes_one_line_per_series(tmp_path):
    cfg = _mk_cfg(tmp_path)
    dates = [date(2025, 1, 1) + timedelta(days=10 * i) for i in range(6)]
    _write(cfg, [_rec("us", DocType.D1, d) for d in dates])
    entries = compute_series(cfg, today=dates[-1])
    path = write_cadence_jsonl(cfg, entries)
    assert path == cfg.data_dir / "cadence.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["bank_code"] == "us" and row["doc_type"] == "D1"


def test_write_cadence_jsonl_is_a_full_snapshot_not_append(tmp_path):
    cfg = _mk_cfg(tmp_path)
    write_cadence_jsonl(cfg, [{"bank_code": "a", "doc_type": "D1"}])
    write_cadence_jsonl(cfg, [{"bank_code": "b", "doc_type": "D1"}])
    lines = (cfg.data_dir / "cadence.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["bank_code"] == "b"


# -- state file: new vs already-known overdue, recovery, mute ---------------

def _overdue_entry(bank="us", dtype="D1", days_until=-50):
    return {"bank_code": bank, "doc_type": dtype, "last": "2025-01-01",
            "interval_days": 30, "next_expected": "2025-01-31",
            "days_until": days_until, "status": "overdue",
            "expected_per_year": 12, "n_3y": 10}


def _ontrack_entry(bank="us", dtype="D1"):
    return {"bank_code": bank, "doc_type": dtype, "last": "2025-06-01",
            "interval_days": 30, "next_expected": "2025-07-01",
            "days_until": 20, "status": "on-track",
            "expected_per_year": 12, "n_3y": 10}


def test_first_overdue_run_is_new(tmp_path):
    cfg = _mk_cfg(tmp_path)
    state = CadenceState(cfg)
    overdue_count, new_count, lines = apply_state(state, [_overdue_entry()])
    assert (overdue_count, new_count) == (1, 1)
    assert any("NEW OVERDUE" in l for l in lines)
    assert state.is_known_overdue("us", "D1") is True


def test_second_run_same_overdue_series_is_not_new(tmp_path):
    cfg = _mk_cfg(tmp_path)
    state = CadenceState(cfg)
    apply_state(state, [_overdue_entry()])
    # fresh CadenceState instance replaying the same on-disk state, like a
    # separate weekly cron invocation would see
    state2 = CadenceState(cfg)
    overdue_count, new_count, lines = apply_state(state2, [_overdue_entry()])
    assert (overdue_count, new_count) == (1, 0)
    assert lines == []


def test_recovery_writes_release_line(tmp_path):
    cfg = _mk_cfg(tmp_path)
    state = CadenceState(cfg)
    apply_state(state, [_overdue_entry()])

    state2 = CadenceState(cfg)
    overdue_count, new_count, lines = apply_state(state2, [_ontrack_entry()])
    assert overdue_count == 0
    assert any("RECOVERED" in l for l in lines)
    assert state2.is_known_overdue("us", "D1") is False

    state3 = CadenceState(cfg)
    assert state3.is_known_overdue("us", "D1") is False


def test_muted_series_written_but_never_alerted(tmp_path):
    cfg = _mk_cfg(tmp_path)
    state = CadenceState(cfg)
    state.seed_mute("ecb", "G2", reason="decision retired")

    overdue_count, new_count, lines = apply_state(state, [_overdue_entry("ecb", "G2")])
    assert overdue_count == 1          # still counted -- dashboard honesty
    assert new_count == 0              # but never counts as a NEW alert
    assert lines == []                 # and no loud line
    assert state.is_known_overdue("ecb", "G2") is True  # state still tracked


def test_mute_survives_a_later_recovery_event(tmp_path):
    """Mute must be sticky across ordinary automated overdue/recovery
    events for the SAME key (design decision 5) -- not clobbered by a
    full-row replace the way quarantine.py's state file would."""
    cfg = _mk_cfg(tmp_path)
    state = CadenceState(cfg)
    state.seed_mute("ecb", "G2", reason="decision retired")
    apply_state(state, [_overdue_entry("ecb", "G2")])
    apply_state(state, [_ontrack_entry("ecb", "G2")])  # recovers

    state2 = CadenceState(cfg)  # replay from disk
    assert state2.is_muted("ecb", "G2") is True


def test_corrupt_state_line_tolerated(tmp_path):
    cfg = _mk_cfg(tmp_path)
    path = cfg.data_dir / "cadence_state.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"bank_code": "us", "doc_type": "D1", "overdue": true}\n'
        'NOT-JSON-AT-ALL\n'
        '{"bank_code": "gb", "doc_type": "D1", "overdue": true}\n'
    )
    state = CadenceState(cfg)  # must not raise
    assert state.is_known_overdue("us", "D1") is True
    assert state.is_known_overdue("gb", "D1") is True


# -- env threshold overrides --------------------------------------------

def test_env_min_docs_override(tmp_path, monkeypatch):
    cfg = _mk_cfg(tmp_path)
    dates = [date(2025, 1, 1) + timedelta(days=10 * i) for i in range(4)]
    _write(cfg, [_rec("us", DocType.D1, d) for d in dates])
    assert compute_series(cfg, today=dates[-1]) == []  # default min 6 excludes

    monkeypatch.setenv("CADENCE_MIN_DOCS", "4")
    entries = compute_series(cfg, today=dates[-1])
    assert len(entries) == 1
    assert entries[0]["n_3y"] == 4


def test_env_lookback_years_override_excludes_old_docs(tmp_path, monkeypatch):
    cfg = _mk_cfg(tmp_path)
    today = date(2026, 1, 1)
    recent = [today - timedelta(days=10 * i) for i in range(6)]  # within 1y
    _write(cfg, [_rec("us", DocType.D1, d) for d in recent])

    monkeypatch.setenv("CADENCE_LOOKBACK_YEARS", "1")
    assert len(compute_series(cfg, today=today)) == 1

    monkeypatch.setenv("CADENCE_MIN_DOCS", "20")  # now unreachable in 1y
    assert compute_series(cfg, today=today) == []


def test_env_grace_and_soon_overrides_shift_status(tmp_path, monkeypatch):
    cfg = _mk_cfg(tmp_path)
    dates = _six_doc_series_10d_apart(cfg)
    next_expected = dates[-1] + timedelta(days=10)
    today = next_expected + timedelta(days=3)  # overdue only with a tight grace

    assert compute_series(cfg, today=today)[0]["status"] != "overdue"
    monkeypatch.setenv("CADENCE_OVERDUE_GRACE_DAYS", "2")
    assert compute_series(cfg, today=today)[0]["status"] == "overdue"

    monkeypatch.delenv("CADENCE_OVERDUE_GRACE_DAYS")
    far_today = next_expected - timedelta(days=65)
    assert compute_series(cfg, today=far_today)[0]["status"] == "on-track"
    monkeypatch.setenv("CADENCE_SOON_DAYS", "90")
    assert compute_series(cfg, today=far_today)[0]["status"] == "soon"


def test_malformed_env_value_falls_back_to_default(tmp_path, monkeypatch):
    cfg = _mk_cfg(tmp_path)
    dates = _six_doc_series_10d_apart(cfg)
    monkeypatch.setenv("CADENCE_MIN_DOCS", "not-a-number")
    entries = compute_series(cfg, today=dates[-1])
    assert len(entries) == 1  # default min_docs=6 still applies, no crash


# -- CLI ---------------------------------------------------------------

def test_cli_dry_run_prints_table_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = Config()
    dates = [date(2025, 1, 1) + timedelta(days=10 * i) for i in range(6)]
    _write(cfg, [_rec("us", DocType.D1, d) for d in dates])

    rc = cli_main(["cadence-watch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "us" in out and "D1" in out
    assert not (tmp_path / "data" / "cadence.jsonl").exists()
    assert not (tmp_path / "data" / "cadence_state.jsonl").exists()


def test_cli_write_emits_jsonl_and_state(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = Config()
    dates = [date(2025, 1, 1) + timedelta(days=30 * i) for i in range(12)]
    _write(cfg, [_rec("ecb", DocType.D3, d) for d in dates])

    rc = cli_main(["cadence-watch", "--write"])
    assert rc == 0
    assert (tmp_path / "data" / "cadence.jsonl").exists()
    assert (tmp_path / "data" / "cadence_state.jsonl").exists()
    err = capsys.readouterr().err
    assert "cadence:" in err


def test_run_cadence_watch_return_value_matches_compute_series(tmp_path):
    cfg = _mk_cfg(tmp_path)
    dates = [date(2025, 1, 1) + timedelta(days=10 * i) for i in range(6)]
    _write(cfg, [_rec("us", DocType.D1, d) for d in dates])
    entries = run_cadence_watch(cfg, write=False, today=dates[-1])
    assert entries == compute_series(cfg, today=dates[-1])


# -- date precision (issue #23): coarse rows never feed the median ----------

def _day_series(start: date, n: int, step: int) -> list[date]:
    return [start + timedelta(days=step * i) for i in range(n)]


def test_month_and_year_rows_are_dropped_before_the_median_and_last(tmp_path):
    """A day-precision series (every 30 days) with RePEc-style month rows and a
    year-only Jan-1 placeholder mixed in. Kept, the placeholders would drag the
    median down and a later-year Jan-1 would become `last`; dropped, the series
    reads exactly as its day rows."""
    cfg = _mk_cfg(tmp_path)
    days = _day_series(date(2024, 2, 10), 10, 30)
    recs = [_rec("us", DocType.D1, d, _i=i) for i, d in enumerate(days)]
    recs += [_rec("us", DocType.D1, date(2024, m, 1), date_precision="month", _i=100 + m)
             for m in range(3, 12)]
    recs.append(_rec("us", DocType.D1, date(2025, 1, 1), date_precision="year", _i=999))
    _write(cfg, recs)

    today = days[-1] + timedelta(days=5)
    dropped: dict = {}
    entries = compute_series(cfg, today=today, dropped=dropped)
    assert len(entries) == 1
    e = entries[0]
    assert e["interval_days"] == 30
    assert e["last"] == days[-1].isoformat()
    assert e["n_3y"] == 10
    assert dropped == {("us", "D1"): 10}
    # the same day rows alone give the same entry: nothing else moved
    cfg2 = _mk_cfg(tmp_path / "only_days")
    _write(cfg2, [_rec("us", DocType.D1, d, _i=i) for i, d in enumerate(days)])
    assert compute_series(cfg2, today=today) == entries


def test_output_field_set_is_unchanged_by_the_precision_filter(tmp_path):
    cfg = _mk_cfg(tmp_path)
    days = _day_series(date(2025, 1, 5), 8, 20)
    recs = [_rec("ecb", DocType.D3, d, _i=i) for i, d in enumerate(days)]
    recs.append(_rec("ecb", DocType.D3, date(2025, 3, 1), date_precision="month", _i=50))
    _write(cfg, recs)
    e = compute_series(cfg, today=days[-1])[0]
    assert set(e) == {"bank_code", "doc_type", "last", "interval_days", "next_expected",
                      "days_until", "status", "expected_per_year", "n_3y"}


def test_all_year_precision_series_is_not_judged_and_reported(tmp_path, capsys):
    """ECB E4 shape: every row a Jan-1 placeholder (year precision). Judged, it
    would read as a ~365-day cadence; dropped, it is unmonitored -- and the
    table and stderr say so instead of silently omitting it."""
    cfg = _mk_cfg(tmp_path)
    recs = [_rec("ecb", DocType.E4, date(y, 1, 1), date_precision="year", _i=i)
            for y in (2023, 2024, 2025, 2026) for i in range(8)]
    recs += [_rec("us", DocType.D1, d, _i=i)
             for i, d in enumerate(_day_series(date(2025, 6, 1), 8, 14))]
    _write(cfg, recs)
    entries = run_cadence_watch(cfg, write=False, today=date(2026, 2, 1))
    assert [(e["bank_code"], e["doc_type"]) for e in entries] == [("us", "D1")]
    out, err = capsys.readouterr()
    assert any(ln.split()[:2] == ["ecb", "E4"] and "32" in ln for ln in out.splitlines())
    assert "cadence: ecb E4: 32 month/year-precision row(s) dropped" in err
    assert "series not judged" in err


def test_all_coarse_and_no_coarse_tables(tmp_path, capsys):
    """Only coarse rows anywhere: no judged series, the table still lists the
    dropped counts. Nothing coarse: no stderr line at all."""
    cfg = _mk_cfg(tmp_path)
    _write(cfg, [_rec("ecb", DocType.E4, date(2025, 1, 1), date_precision="year", _i=i)
                 for i in range(3)])
    assert run_cadence_watch(cfg, write=False, today=date(2026, 2, 1)) == []
    out, err = capsys.readouterr()
    assert "no series with enough history to judge" in out
    assert "ecb E4: 3" in err

    cfg2 = _mk_cfg(tmp_path / "clean")
    _write(cfg2, [_rec("us", DocType.D1, d, _i=i)
                  for i, d in enumerate(_day_series(date(2025, 6, 1), 8, 14))])
    run_cadence_watch(cfg2, write=False, today=date(2025, 9, 1))
    out, err = capsys.readouterr()
    assert "dropped" not in err


def test_rows_without_precision_key_count_as_before(tmp_path):
    """Legacy rows predate `date_precision`: a missing key (or null) is kept."""
    cfg = _mk_cfg(tmp_path)
    days = _day_series(date(2025, 1, 1), 7, 10)
    rows = []
    for i, d in enumerate(days):
        r = _rec("jp", DocType.D1, d, _i=i).to_row()
        if i % 2:
            r.pop("date_precision", None)
        else:
            r["date_precision"] = None
        rows.append(r)
    _write_per_bank_unlocked(cfg, rows)
    dropped: dict = {}
    e = compute_series(cfg, today=days[-1], dropped=dropped)[0]
    assert e["n_3y"] == 7 and dropped == {}


def test_dropped_count_shown_in_the_table_row(tmp_path, capsys):
    cfg = _mk_cfg(tmp_path)
    days = _day_series(date(2025, 1, 5), 8, 20)
    recs = [_rec("fr", DocType.D1, d, _i=i) for i, d in enumerate(days)]
    recs += [_rec("fr", DocType.D1, date(2025, m, 1), date_precision="month", _i=60 + m)
             for m in (2, 4, 6)]
    _write(cfg, recs)
    run_cadence_watch(cfg, write=False, today=days[-1])
    out, err = capsys.readouterr()
    header, row = out.splitlines()[:2]
    assert header.rstrip().endswith("coarse")
    assert row.split()[0] == "fr" and row.split()[-1] == "3"
    assert "cadence: fr D1: 3 month/year-precision row(s) dropped" in err
