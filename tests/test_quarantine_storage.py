"""Storage <-> Quarantine integration (recover-quarantine design §3):

- save() consults the quarantine BEFORE any fetch (quarantined URL -> hard
  skip, fetcher never touched).
- a failed download (via _record_download_error) feeds one night of failure
  into the quarantine state.
- a successful download (status "saved") releases the URL from quarantine.
- save_many() prints the quarantine's one-line summary to stderr exactly
  once per run, only when something was actually skipped.

No real HTTP: fetchers are tiny stubs that record their calls, so
"never touched" is directly assertable.
"""
from __future__ import annotations

import datetime as _dt
import json

from cb_corpus.config import Config
from cb_corpus.models import DocRecord
from cb_corpus.storage import Storage
from cb_corpus.taxonomy import DocType


def _rec(**kw):
    base = dict(bank_code="gb", doc_type=DocType.D1, title="t",
                pdf_url="https://x.test/dead.pdf", source_url="https://ideas.test/p/1.html",
                provenance="repec_discovery")
    base.update(kw)
    return DocRecord(**base)


class _BoomFetcher:
    """Always fails; records every URL it was asked to fetch."""

    def __init__(self):
        self.calls: list[str] = []

    def get_bytes(self, url):
        self.calls.append(url)
        raise RuntimeError("HTTP 404: gone")


class _OkFetcher:
    """Always succeeds; records every URL it was asked to fetch."""

    def __init__(self):
        self.calls: list[str] = []

    def get_bytes(self, url):
        self.calls.append(url)
        return b"%PDF-fake", "application/pdf"


def _mk_storage(tmp_path, fetcher):
    (tmp_path / "manifest").mkdir(parents=True, exist_ok=True)
    return Storage(Config(data_dir=tmp_path), fetcher)


def _seed_quarantine(tmp_path, url, nights, quarantined=True):
    """Write a quarantine state line directly, so a freshly-constructed
    Storage (which builds its own Quarantine on __init__) loads it as
    pre-existing state — mirrors a URL that failed on prior nightly runs."""
    path = tmp_path / "download_quarantine.jsonl"
    path.write_text(json.dumps({"url": url, "nights": nights, "quarantined": quarantined}) + "\n")


# --- consult: quarantined URL short-circuits before any fetch ---------------

def test_quarantined_url_skips_without_any_fetch_attempt(tmp_path):
    url = "https://x.test/dead.pdf"
    _seed_quarantine(tmp_path, url,
                      ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"])
    fetcher = _BoomFetcher()
    st = _mk_storage(tmp_path, fetcher)

    status = st.save(_rec(pdf_url=url), dry_run=False)

    assert status == "skip:quarantined"
    assert fetcher.calls == []


def test_below_threshold_url_is_not_short_circuited(tmp_path):
    """Sanity: a URL with failures below the quarantine threshold still goes
    through the normal fetch path — the short-circuit is conditional on
    actually being quarantined, not on any prior-failure state existing."""
    url = "https://x.test/dead.pdf"
    _seed_quarantine(tmp_path, url, ["2026-08-22"], quarantined=False)
    fetcher = _OkFetcher()
    st = _mk_storage(tmp_path, fetcher)

    status = st.save(_rec(pdf_url=url), dry_run=False)

    assert status == "saved"
    assert fetcher.calls == [url]


# --- feed: a failed download counts one night against the URL ---------------

def test_failed_download_feeds_the_quarantine_state(tmp_path):
    url = "https://x.test/dead.pdf"
    st = _mk_storage(tmp_path, _BoomFetcher())
    today = _dt.date.today().isoformat()   # LOCAL night (B7), not UTC

    counts = st.save_many([_rec(pdf_url=url)], dry_run=False, label="repec:gb")

    assert counts == {"error": 1}
    # audit trail still written (existing behaviour, untouched by this feature)
    assert (tmp_path / "download_errors.jsonl").exists()
    # quarantine state fed with today's night for this url
    lines = (tmp_path / "download_quarantine.jsonl").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["url"] == url
    assert row["nights"] == [today]
    assert row["quarantined"] is False  # one night, default threshold is 5


# --- feed: success releases a failing-but-not-yet-quarantined URL -----------

def test_successful_download_releases_a_failing_but_not_yet_quarantined_url(tmp_path):
    url = "https://x.test/dead.pdf"
    _seed_quarantine(tmp_path, url, ["2026-08-20", "2026-08-21", "2026-08-22"], quarantined=False)
    st = _mk_storage(tmp_path, _OkFetcher())

    status = st.save(_rec(pdf_url=url), dry_run=False)

    assert status == "saved"
    lines = (tmp_path / "download_quarantine.jsonl").read_text().splitlines()
    assert json.loads(lines[-1]) == {"url": url, "released": True}

    # A fresh Storage/Quarantine loading this state must see the url as clear.
    st2 = _mk_storage(tmp_path, _OkFetcher())
    assert st2.quarantine.is_quarantined(url) is False


def test_successful_download_via_quarantine_retry_bypass_releases_a_quarantined_url(
        tmp_path, monkeypatch):
    """QUARANTINE_RETRY=1 (the Sunday full-sweep bypass) lets save() proceed
    past an ACTUALLY quarantined URL to the real fetch; on success the
    release still fires."""
    url = "https://x.test/dead.pdf"
    _seed_quarantine(tmp_path, url,
                      ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"])
    monkeypatch.setenv("QUARANTINE_RETRY", "1")
    fetcher = _OkFetcher()
    st = _mk_storage(tmp_path, fetcher)

    status = st.save(_rec(pdf_url=url), dry_run=False)

    assert status == "saved"
    assert fetcher.calls == [url]  # bypass let it through to the real fetch
    lines = (tmp_path / "download_quarantine.jsonl").read_text().splitlines()
    assert json.loads(lines[-1]) == {"url": url, "released": True}


# --- save_many prints the quarantine summary line exactly once --------------

def test_save_many_prints_quarantine_summary_once(tmp_path, capsys):
    quarantined_url = "https://x.test/dead.pdf"
    alive_url = "https://x.test/alive.pdf"
    _seed_quarantine(tmp_path, quarantined_url,
                      ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"])
    st = _mk_storage(tmp_path, _OkFetcher())

    st.save_many([_rec(pdf_url=quarantined_url), _rec(pdf_url=alive_url)],
                 dry_run=False, label="repec:gb")

    err = capsys.readouterr().err
    assert err.count("quarantine: skipped 1 url(s)") == 1


def test_save_many_prints_no_summary_when_nothing_was_skipped(tmp_path, capsys):
    st = _mk_storage(tmp_path, _OkFetcher())

    st.save_many([_rec(pdf_url="https://x.test/alive.pdf")], dry_run=False, label="repec:gb")

    err = capsys.readouterr().err
    assert "quarantine" not in err


def test_two_sequential_save_many_batches_each_report_their_own_skip_count(tmp_path, capsys):
    """IMPORTANT 4: two save_many() batches against the SAME Storage/
    Quarantine instance (e.g. one process discovering several banks in
    turn) must each print their OWN skip count -- not a cumulative total
    that keeps growing across batches."""
    q1_url = "https://x.test/dead-batch1.pdf"
    q2a_url = "https://x.test/dead-batch2-a.pdf"
    q2b_url = "https://x.test/dead-batch2-b.pdf"
    nights = ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"]
    _seed_quarantine(tmp_path, q1_url, nights)
    st = _mk_storage(tmp_path, _OkFetcher())
    # Seed the batch-2 quarantines AFTER Storage init (Storage builds its own
    # Quarantine at __init__, which already loaded state for q1_url) --
    # append directly to the same on-disk state file so st's in-memory
    # Quarantine (which reads it live via is_quarantined -> self._state) sees
    # them too. We instead just seed via the SAME live Quarantine instance
    # storage already holds, by writing failures directly.
    for night in nights:
        st.quarantine.record_failure(q2a_url, night)
        st.quarantine.record_failure(q2b_url, night)

    # Batch 1: exactly one quarantined URL.
    st.save_many([_rec(pdf_url=q1_url), _rec(pdf_url="https://x.test/alive1.pdf")],
                 dry_run=False, label="repec:aa")
    err1 = capsys.readouterr().err
    assert err1.count("quarantine: skipped 1 url(s)") == 1

    # Batch 2: exactly two quarantined URLs -- must NOT report 1 + 2 = 3.
    st.save_many([_rec(pdf_url=q2a_url), _rec(pdf_url=q2b_url),
                 _rec(pdf_url="https://x.test/alive2.pdf")],
                 dry_run=False, label="repec:bb")
    err2 = capsys.readouterr().err
    assert err2.count("quarantine: skipped 2 url(s)") == 1
    assert "skipped 3 url(s)" not in err2


# --- bypass_quarantine: recovery flows must not be blocked by their own -----
# quarantine (task 2b: download_errors.jsonl feeds BOTH quarantine counting
# AND recover-downloads' inventory, so by the time recovery runs its own
# targets are typically quarantined -- save() must offer a way past the gate).

def test_bypass_quarantine_lets_save_proceed_and_leaves_the_gate_state_untouched(tmp_path):
    """bypass_quarantine=True skips the is_quarantined() consult entirely (the
    gate itself is never touched -- skipped_count stays 0), letting the real
    fetch happen even though the URL is actively quarantined. A subsequent
    success still releases the quarantine, same as any other successful save."""
    url = "https://x.test/dead.pdf"
    _seed_quarantine(tmp_path, url,
                      ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"])
    fetcher = _OkFetcher()
    st = _mk_storage(tmp_path, fetcher)

    status = st.save(_rec(pdf_url=url), dry_run=False, bypass_quarantine=True)

    assert status == "saved"
    assert fetcher.calls == [url]              # bypass let it through to the real fetch
    assert st.quarantine.skipped_count() == 0  # the gate itself was never consulted
    lines = (tmp_path / "download_quarantine.jsonl").read_text().splitlines()
    assert json.loads(lines[-1]) == {"url": url, "released": True}


def test_bypass_quarantine_false_by_default_still_short_circuits(tmp_path):
    """Sanity: the new keyword-only parameter defaults to False, so every
    existing call site (the normal nightly save() path) keeps the quarantine
    gate as the default behaviour."""
    url = "https://x.test/dead.pdf"
    _seed_quarantine(tmp_path, url,
                      ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"])
    fetcher = _BoomFetcher()
    st = _mk_storage(tmp_path, fetcher)

    status = st.save(_rec(pdf_url=url), dry_run=False)

    assert status == "skip:quarantined"
    assert fetcher.calls == []


# --- reindex releases quarantine on success ----------------------------------

def test_reindex_success_releases_quarantine_so_a_fresh_load_sees_it_clear(tmp_path):
    """An externally-recovered doc registered via reindex() (no fetch at all)
    must release the URL's quarantine too -- otherwise the nightly sync would
    keep the URL quarantined forever even though the corpus now has it."""
    url = "https://x.test/dead.pdf"
    _seed_quarantine(tmp_path, url,
                      ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"])
    st = _mk_storage(tmp_path, _BoomFetcher())
    file_path = tmp_path / "found.pdf"
    file_path.write_bytes(b"%PDF-fake-content")

    status = st.reindex(_rec(pdf_url=url), file_path, dry_run=False)

    assert status == "reindexed"
    lines = (tmp_path / "download_quarantine.jsonl").read_text().splitlines()
    assert json.loads(lines[-1]) == {"url": url, "released": True}

    # A fresh Storage/Quarantine loading this state must see the url as clear.
    st2 = _mk_storage(tmp_path, _BoomFetcher())
    assert st2.quarantine.is_quarantined(url) is False


def test_reindex_skip_already_indexed_does_not_touch_quarantine(tmp_path):
    """A no-op reindex (doc_id already known) must not call record_success --
    only an ACTUAL 'reindexed' success releases the quarantine."""
    url = "https://x.test/dead.pdf"
    _seed_quarantine(tmp_path, url, ["2026-08-22"], quarantined=False)
    q_path = tmp_path / "download_quarantine.jsonl"
    before = q_path.read_text()
    st = _mk_storage(tmp_path, _BoomFetcher())
    file_path = tmp_path / "found.pdf"
    file_path.write_bytes(b"%PDF-fake-content")
    rec = _rec(pdf_url=url)
    st._ids.add(rec.doc_id)  # simulate already-indexed

    status = st.reindex(rec, file_path, dry_run=False)

    assert status == "skip:already-indexed"
    assert q_path.read_text() == before  # untouched: no release tombstone appended


# --- B7: one LOCAL night per run, fixed at Storage construction -------------

def test_night_is_fixed_at_construction_across_a_fake_midnight(tmp_path, monkeypatch):
    """A run that starts at 23:59 and fails a download at 00:01 counts ONE
    night (the run's), so a single run never spans two quarantine nights."""
    import cb_corpus.storage as storage_mod

    clock = {"today": _dt.date(2026, 11, 10)}

    class _FakeDate(_dt.date):
        @classmethod
        def today(cls):
            return clock["today"]

    monkeypatch.setattr(storage_mod._dt, "date", _FakeDate)
    st = _mk_storage(tmp_path, _BoomFetcher())
    clock["today"] = _dt.date(2026, 11, 11)          # midnight passes mid-run
    st.save_many([_rec(pdf_url="https://x.test/a.pdf")])
    st.save_many([_rec(pdf_url="https://x.test/a.pdf")])
    rows = [json.loads(ln) for ln in
            (tmp_path / "download_quarantine.jsonl").read_text().splitlines()]
    assert {n for r in rows for n in r.get("nights", [])} == {"2026-11-10"}
    assert rows[-1]["nights"] == ["2026-11-10"]


def test_night_uses_local_date_not_utc(tmp_path, monkeypatch):
    """Local date at construction (TZ=Europe/Paris in the stack): at 00:30
    Paris time the UTC date is still yesterday -- the night must be today."""
    import cb_corpus.storage as storage_mod

    class _FakeDate(_dt.date):
        @classmethod
        def today(cls):
            return _dt.date(2026, 11, 11)              # local

    monkeypatch.setattr(storage_mod._dt, "date", _FakeDate)
    st = _mk_storage(tmp_path, _BoomFetcher())
    assert st._night == "2026-11-11"
