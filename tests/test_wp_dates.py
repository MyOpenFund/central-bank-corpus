"""wp-dates index replay integrity (issue #22).

A committed-index hit used to be applied to any row whose key matched, with
no month check against THAT row, and title keys were global across banks: two
banks' papers sharing a generic title could swap exact days. These tests pin
the two guards: every replayed hit is month-checked against the row it is
applied to (a rejected hit falls through to fresh recovery like a miss), and
new title keys are scoped by bank while the legacy global form is still read.

No network: Wayback is either disabled or `first_capture` is monkeypatched.
"""
from __future__ import annotations

import json

from cb_corpus import wp_dates
from cb_corpus.config import Config
from cb_corpus.storage import Storage
from cb_corpus.wp_dates import index_path, legacy_title_key, row_key


def _setup(tmp_path, rows: list[dict], index: list[dict]) -> Config:
    cfg = Config(data_dir=tmp_path)
    Storage(cfg)
    by_bank: dict[str, list[dict]] = {}
    for r in rows:
        by_bank.setdefault(r["bank_code"], []).append(r)
    for bank, rs in by_bank.items():
        p = cfg.manifest_file(bank)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r) + "\n" for r in rs))
    index_path(cfg).write_text("".join(json.dumps(e) + "\n" for e in index))
    return cfg


def _row(doc_id: str, bank: str, date: str, title: str = "Inflation dynamics",
         **extra) -> dict:
    return {"doc_id": doc_id, "bank_code": bank, "doc_type": "D1", "date": date,
            "year": int(date[:4]), "date_precision": "month", "date_source": "repec",
            "title": title, "pdf_url": f"https://{bank}.example/{doc_id}.pdf", **extra}


def _manifest(cfg: Config, bank: str) -> dict[str, dict]:
    return {r["doc_id"]: r for r in map(json.loads,
                                        cfg.manifest_file(bank).read_text().splitlines())}


def _entry(key: str, date: str) -> dict:
    return {"key": key, "date": date, "date_source": "wayback", "date_precision": "day",
            "evidence_url": "https://web.archive.org/web/x"}


def test_title_key_is_scoped_by_bank_and_legacy_key_is_the_old_form():
    row = _row("a", "ecb", "2010-03-01", title="Inflation: Dynamics!")
    assert row_key(row) == "t:ecb:inflation dynamics"
    assert legacy_title_key(row) == "t:inflation dynamics"
    # a handle-keyed row is unchanged and has no title fallback at all
    h = _row("b", "ecb", "2010-03-01",
             source_url="https://ideas.repec.org/p/ecb/ecbwps/2010100.html")
    assert row_key(h) == "RePEc:ecb:ecbwps:2010100"
    assert legacy_title_key(h) is None


def test_two_banks_sharing_a_title_do_not_cross_on_replay(tmp_path):
    """Same normalized title, same month: the month check alone would let the
    ECB day land on the Fed paper; the bank scope must stop it."""
    ecb = _row("e1", "ecb", "2010-03-01")
    us = _row("u1", "us", "2010-03-01")
    cfg = _setup(tmp_path, [ecb, us], [_entry(row_key(ecb), "2010-03-15")])

    counts = wp_dates.run_wp_dates(bank_codes=["ecb", "us"], write=True,
                                   apply_only=True, config=cfg)
    assert _manifest(cfg, "ecb")["e1"]["date"] == "2010-03-15"
    after_us = _manifest(cfg, "us")["u1"]
    assert after_us["date"] == "2010-03-01" and after_us["date_precision"] == "month"
    assert counts["from_index"] == 1 and counts["unresolved"] == 1


def test_legacy_global_key_hit_in_month_still_replays(tmp_path):
    row = _row("e1", "ecb", "2010-03-01")
    cfg = _setup(tmp_path, [row], [_entry(legacy_title_key(row), "2010-03-15")])
    counts = wp_dates.run_wp_dates(bank_codes=["ecb"], write=True, apply_only=True, config=cfg)
    assert _manifest(cfg, "ecb")["e1"]["date"] == "2010-03-15"
    assert counts["from_index"] == 1 and counts["index_rejected"] == 0


def test_legacy_global_key_hit_outside_the_rows_month_is_rejected(tmp_path):
    """The legacy key was written for another bank's paper (another month):
    applying it would stamp a confidently wrong day."""
    row = _row("u1", "us", "2011-07-01")
    cfg = _setup(tmp_path, [row], [_entry(legacy_title_key(row), "2010-03-15")])
    counts = wp_dates.run_wp_dates(bank_codes=["us"], write=True, apply_only=True, config=cfg)
    after = _manifest(cfg, "us")["u1"]
    assert after["date"] == "2011-07-01" and after["date_precision"] == "month"
    assert counts["index_rejected"] == 1 and counts["from_index"] == 0
    assert counts["unresolved"] == 1


def test_handle_hit_outside_the_rows_month_is_rejected_too(tmp_path):
    """B1 is not limited to title keys: whatever the key, a replayed day must
    fall in the row's own asserted month (and not be the 1st)."""
    row = _row("e1", "ecb", "2010-03-01",
               source_url="https://ideas.repec.org/p/ecb/ecbwps/2010100.html")
    cfg = _setup(tmp_path, [row], [_entry(row_key(row), "2010-04-15")])
    counts = wp_dates.run_wp_dates(bank_codes=["ecb"], write=True, apply_only=True, config=cfg)
    assert _manifest(cfg, "ecb")["e1"]["date"] == "2010-03-01"
    assert counts["index_rejected"] == 1


def test_replayed_first_of_month_is_rejected(tmp_path):
    row = _row("e1", "ecb", "2010-03-01")
    cfg = _setup(tmp_path, [row], [_entry(row_key(row), "2010-03-01")])
    counts = wp_dates.run_wp_dates(bank_codes=["ecb"], write=True, apply_only=True, config=cfg)
    assert counts["index_rejected"] == 1 and counts["from_index"] == 0


def test_malformed_index_date_is_rejected_not_a_crash(tmp_path):
    row = _row("e1", "ecb", "2010-03-01")
    cfg = _setup(tmp_path, [row], [_entry(row_key(row), "2010-03-xx")])
    counts = wp_dates.run_wp_dates(bank_codes=["ecb"], write=True, apply_only=True, config=cfg)
    assert counts["index_rejected"] == 1


def test_scoped_hit_rejected_then_legacy_hit_accepted(tmp_path):
    """Lookup order is scoped key, then legacy key; each is month-checked, the
    first one that passes wins."""
    row = _row("e1", "ecb", "2010-03-01")
    cfg = _setup(tmp_path, [row], [_entry(row_key(row), "2012-01-20"),
                                   _entry(legacy_title_key(row), "2010-03-22")])
    counts = wp_dates.run_wp_dates(bank_codes=["ecb"], write=True, apply_only=True, config=cfg)
    assert _manifest(cfg, "ecb")["e1"]["date"] == "2010-03-22"
    assert counts["from_index"] == 1 and counts["index_rejected"] == 0


def test_rejected_hit_goes_on_to_fresh_recovery_and_is_indexed_scoped(tmp_path, monkeypatch):
    """Outside apply-only, a rejected hit is treated like a miss: the row is
    recovered afresh, and the new entry is written under the bank-scoped key."""
    row = _row("u1", "us", "2011-07-01")
    cfg = _setup(tmp_path, [row], [_entry(legacy_title_key(row), "2010-03-15")])
    monkeypatch.setattr(wp_dates, "first_capture", lambda f, u: "20110712093000")
    counts = wp_dates.run_wp_dates(bank_codes=["us"], write=True, since_year=1900, config=cfg)
    assert _manifest(cfg, "us")["u1"]["date"] == "2011-07-12"
    assert counts["index_rejected"] == 1 and counts["wayback"] == 1
    assert counts["unresolved"] == 0
    lines = [json.loads(ln) for ln in index_path(cfg).read_text().splitlines()]
    assert lines[-1]["key"] == "t:us:inflation dynamics"
    assert lines[-1]["date"] == "2011-07-12"
    # the legacy entry is left as committed (the index is never rewritten)
    assert lines[0]["key"] == "t:inflation dynamics"


def test_dry_run_counts_the_rejection_without_writing(tmp_path):
    row = _row("u1", "us", "2011-07-01")
    cfg = _setup(tmp_path, [row], [_entry(legacy_title_key(row), "2010-03-15")])
    before = index_path(cfg).read_text()
    counts = wp_dates.run_wp_dates(bank_codes=["us"], write=False, use_wayback=False,
                                   config=cfg)
    assert counts["index_rejected"] == 1 and counts["unresolved"] == 1
    assert index_path(cfg).read_text() == before
