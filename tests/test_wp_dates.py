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
        by_bank.setdefault(r["source_code"], []).append(r)
    for bank, rs in by_bank.items():
        p = cfg.manifest_file(bank)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r) + "\n" for r in rs))
    index_path(cfg).write_text("".join(json.dumps(e) + "\n" for e in index))
    return cfg


def _row(doc_id: str, bank: str, date: str, title: str = "Inflation dynamics",
         **extra) -> dict:
    return {"doc_id": doc_id, "corpus": "central-bank", "source_code": bank, "doc_type": "D1", "date": date,
            "year": int(date[:4]), "date_precision": "month", "date_source": "repec",
            "title": title, "document_url": f"https://{bank}.example/{doc_id}.pdf", **extra}


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


def test_year_precision_row_never_passes_the_month_constraint(tmp_path):
    """A year-precision row's 1 January is a placeholder, not an asserted
    January: no candidate passes `month_ok`, a January day included, so
    neither replay nor fresh recovery can stamp a day on it."""
    from datetime import date
    from cb_corpus.wp_dates import month_ok
    row = _row("y1", "us", "2015-01-01", date_precision="year")
    assert month_ok(date(2015, 1, 20), row) is False
    assert month_ok(date(2015, 6, 20), row) is False
    assert month_ok(date(2015, 1, 20), dict(row, date_precision="month")) is True
    cfg = _setup(tmp_path, [row], [_entry(row_key(row), "2015-01-20")])
    counts = wp_dates.run_wp_dates(bank_codes=["us"], write=True, apply_only=True, config=cfg)
    # skipped before the index lookup: not a rejection, not a candidate
    assert counts["year_skipped"] == 1 and counts["index_rejected"] == 0
    assert counts["candidates"] == 0 and counts["unresolved"] == 0
    after = _manifest(cfg, "us")["y1"]
    assert after["date"] == "2015-01-01" and after["date_precision"] == "year"


def test_year_precision_rows_are_skipped_before_any_recovery(tmp_path, monkeypatch):
    """Recovery mode: a year row can never pass `month_ok`, so querying Wayback
    (or reading its PDF) for it is wasted work -- no CDX call is made."""
    calls: list[str] = []
    monkeypatch.setattr(wp_dates, "first_capture", lambda f, u: calls.append(u))
    monkeypatch.setattr(wp_dates, "pdf_creation_date", lambda p: calls.append(p))
    year_row = _row("y1", "us", "2015-01-01", date_precision="year", title="Year only")
    month_row = _row("m1", "us", "2015-03-01", title="Month row")
    cfg = _setup(tmp_path, [year_row, month_row], [])
    counts = wp_dates.run_wp_dates(bank_codes=["us"], write=True, since_year=1900,
                                   config=cfg)
    assert counts["year_skipped"] == 1 and counts["candidates"] == 1
    assert all("y1" not in str(c) for c in calls)            # only the month row tried
    assert any("m1" in str(c) for c in calls)
    assert index_path(cfg).read_text() == ""


def test_rejected_own_key_recovery_is_written_under_a_doc_key_and_rebuilds_stable(
        tmp_path, monkeypatch):
    """Real shape (FEDS 2025-101 and its r1 revision): two papers share one
    RePEc handle. The handle entry holds the original's day; the revision's
    replay is rejected (other month), it is recovered afresh -- and that
    resolution must NOT be written under the shared handle, or it shadows the
    original's entry (last line wins): the next rebuild rejects the original,
    re-recovers it, overwrites the handle again -- a flip-flop that grows the
    index on every run. It goes under `<handle>#<doc_id>`, looked up first
    for that row."""
    handle_src = "https://ideas.repec.org/p/fip/fedgfe/2025-101.html"
    orig = _row("orig", "us", "2025-11-01", source_url=handle_src)
    rev = _row("rev1", "us", "2026-06-01", source_url=handle_src)
    handle = row_key(orig)
    assert handle == row_key(rev) == "RePEc:fip:fedgfe:2025-101"
    cfg = _setup(tmp_path, [orig, rev], [_entry(handle, "2025-11-10")])
    manifest_before = cfg.manifest_file("us").read_text()
    caps = {orig["document_url"]: "20251110000000", rev["document_url"]: "20260612000000"}
    monkeypatch.setattr(wp_dates, "first_capture",
                        lambda f, u: caps.get("https://" + u))

    c1 = wp_dates.run_wp_dates(bank_codes=["us"], write=True, since_year=1900, config=cfg)
    # `wayback` counts both resolutions by source (the replayed one included)
    assert c1["from_index"] == 1 and c1["index_rejected"] == 1 and c1["wayback"] == 2
    lines = [json.loads(ln) for ln in index_path(cfg).read_text().splitlines()]
    assert [e["key"] for e in lines] == [handle, f"{handle}#rev1"]
    after = _manifest(cfg, "us")
    assert (after["orig"]["date"], after["rev1"]["date"]) == ("2025-11-10", "2026-06-12")

    # fresh-clone rebuild: replay only, both papers keep their own day
    cfg.manifest_file("us").write_text(manifest_before)
    c2 = wp_dates.run_wp_dates(bank_codes=["us"], write=True, apply_only=True, config=cfg)
    after = _manifest(cfg, "us")
    assert (after["orig"]["date"], after["rev1"]["date"]) == ("2025-11-10", "2026-06-12")
    assert c2["from_index"] == 2 and c2["index_rejected"] == 0

    # a second recovering run on the reset manifest appends nothing, no network
    size = index_path(cfg).stat().st_size
    cfg.manifest_file("us").write_text(manifest_before)

    def _no_network(f, u):
        raise AssertionError(f"unexpected CDX call for {u}")
    monkeypatch.setattr(wp_dates, "first_capture", _no_network)
    c3 = wp_dates.run_wp_dates(bank_codes=["us"], write=True, since_year=1900, config=cfg)
    assert index_path(cfg).stat().st_size == size
    assert c3["from_index"] == 2 and c3["index_rejected"] == 0


def test_doc_key_entry_is_still_month_checked(tmp_path):
    row = _row("e1", "ecb", "2010-03-01")
    cfg = _setup(tmp_path, [row], [_entry(f"{row_key(row)}#e1", "2010-04-15")])
    counts = wp_dates.run_wp_dates(bank_codes=["ecb"], write=True, apply_only=True, config=cfg)
    assert counts["index_rejected"] == 1 and counts["from_index"] == 0
