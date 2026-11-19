"""Quarantine with several writers on one state file (issue #25, A5).

Production runs up to 6 parallel per-bank discover processes, each with its
own Storage -> Quarantine on the SAME download_quarantine.jsonl. Replay is
last-line-wins per URL, so a writer that appended from its stale startup
state could regress another writer's night count or resurrect a released
URL. Every write now happens under the sidecar flock, after replaying the
lines other processes appended since this instance last read the file."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from cb_corpus.config import Config
from cb_corpus.quarantine import Quarantine

URL = "https://x.test/dead.pdf"


def _mk(tmp_path) -> Quarantine:
    return Quarantine(Config(data_dir=tmp_path))


def _fresh_nights(tmp_path, url=URL):
    state = _mk(tmp_path)._state.get(url)
    return None if state is None else state["nights"]


def _lines(tmp_path) -> list[str]:
    return (tmp_path / "download_quarantine.jsonl").read_text().splitlines()


def test_b_continues_a_s_count_instead_of_resetting_it(tmp_path):
    a, b = _mk(tmp_path), _mk(tmp_path)          # both start from an empty file
    a.record_failure(URL, "2026-11-01")
    a.record_failure(URL, "2026-11-02")
    b.record_failure(URL, "2026-11-03")
    assert json.loads(_lines(tmp_path)[-1])["nights"] == [
        "2026-11-01", "2026-11-02", "2026-11-03"]
    assert _fresh_nights(tmp_path) == ["2026-11-01", "2026-11-02", "2026-11-03"]
    # A, in turn, sees B's night before its next write.
    a.record_failure(URL, "2026-11-04")
    assert len(_fresh_nights(tmp_path)) == 4


def test_release_by_a_is_seen_by_b_before_b_appends(tmp_path):
    seed = _mk(tmp_path)
    seed.record_failure(URL, "2026-11-01")
    seed.record_failure(URL, "2026-11-02")
    a, b = _mk(tmp_path), _mk(tmp_path)          # both load the 2-night state
    a.record_success(URL)
    b.record_failure(URL, "2026-11-03")
    assert _fresh_nights(tmp_path) == ["2026-11-03"]   # a fresh count, not 3


def test_success_releases_a_url_another_writer_started_failing(tmp_path):
    b = _mk(tmp_path)                            # loaded before the failure
    a = _mk(tmp_path)
    a.record_failure(URL, "2026-11-01")
    b.record_success(URL)
    assert _fresh_nights(tmp_path) is None
    assert json.loads(_lines(tmp_path)[-1]) == {"url": URL, "released": True}


def test_success_on_an_unknown_url_writes_nothing(tmp_path):
    a = _mk(tmp_path)
    a.record_failure("https://x.test/other.pdf", "2026-11-01")
    b = _mk(tmp_path)
    b.record_success(URL)
    assert len(_lines(tmp_path)) == 1


def test_seed_by_a_is_seen_by_b(tmp_path):
    a, b = _mk(tmp_path), _mk(tmp_path)
    a.seed(URL, "confirmed dead")
    b.record_failure(URL, "2026-11-01")          # no-op on a seeded row
    assert len(_lines(tmp_path)) == 1
    assert _mk(tmp_path).is_quarantined(URL)


def test_is_quarantined_reads_the_startup_state(tmp_path, monkeypatch):
    # Documented limit: the skip decision uses what this instance loaded at
    # startup (plus what it replayed while writing), one read per run.
    monkeypatch.setenv("QUARANTINE_AFTER_NIGHTS", "2")
    a, b = _mk(tmp_path), _mk(tmp_path)
    a.record_failure(URL, "2026-11-01")
    a.record_failure(URL, "2026-11-02")
    assert a.is_quarantined(URL) is True
    assert b.is_quarantined(URL) is False
    assert _mk(tmp_path).is_quarantined(URL) is True


def test_torn_tail_from_a_killed_writer_does_not_swallow_the_next_row(tmp_path, capsys):
    a = _mk(tmp_path)
    a.record_failure(URL, "2026-11-01")
    with (tmp_path / "download_quarantine.jsonl").open("ab") as fh:
        fh.write(b'{"url": "https://x.test/oth')     # killed mid-write, no newline
    a.record_failure(URL, "2026-11-02")
    assert _fresh_nights(tmp_path) == ["2026-11-01", "2026-11-02"]
    assert "corrupt line" in capsys.readouterr().err


def test_file_without_final_newline_still_loads_its_last_row(tmp_path):
    p = tmp_path / "download_quarantine.jsonl"
    p.write_text(json.dumps({"url": URL, "nights": ["2026-11-01"], "quarantined": False}))
    q = _mk(tmp_path)
    assert q._state[URL]["nights"] == ["2026-11-01"]
    q.record_failure(URL, "2026-11-02")
    assert _fresh_nights(tmp_path) == ["2026-11-01", "2026-11-02"]


def test_file_replaced_behind_our_back_is_replayed_from_the_start(tmp_path):
    a = _mk(tmp_path)
    for n in ("2026-11-01", "2026-11-02", "2026-11-03"):
        a.record_failure(URL, n)
    # An operator rewrites the file (new inode, shorter): A must not seek
    # to its stale offset.
    p = tmp_path / "download_quarantine.jsonl"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"url": URL, "nights": ["2026-10-01"]}) + "\n")
    tmp.replace(p)
    a.record_failure(URL, "2026-11-04")
    assert _fresh_nights(tmp_path) == ["2026-10-01", "2026-11-04"]


def test_writes_take_the_sidecar_lock(tmp_path, monkeypatch):
    from cb_corpus import quarantine as qmod
    held: list[Path] = []
    real = qmod.locked_sidecar

    def spy(path):
        held.append(path)
        return real(path)
    monkeypatch.setattr(qmod, "locked_sidecar", spy)
    q = _mk(tmp_path)
    q.record_failure(URL, "2026-11-01")
    q.record_success(URL)
    q.seed(URL)
    assert held and all(p == tmp_path / "download_quarantine.jsonl" for p in held)
    assert len(held) >= 3


def test_reading_a_missing_file_creates_nothing(tmp_path):
    d = tmp_path / "data"
    q = Quarantine(Config(data_dir=d))
    assert q.is_quarantined(URL) is False
    q.record_success(URL)                         # unknown url: no write
    assert not d.exists()


# -- real processes ----------------------------------------------------------

WORKER = r"""
import sys, time
from pathlib import Path
from cb_corpus.config import Config
from cb_corpus.quarantine import Quarantine
data_dir, go, night, n = sys.argv[1], Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
q = Quarantine(Config(data_dir=Path(data_dir)))   # startup state: before ANY write
print("READY", flush=True)
while not go.exists():
    time.sleep(0.005)
for i in range(n):
    q.record_failure(f"https://x.test/u{i}.pdf", night)
"""


def test_parallel_processes_never_lose_a_night(tmp_path, monkeypatch):
    # 4 workers load the (empty) file, then all write at once, each its own
    # night for the same 20 URLs: every URL must end with all 4 nights.
    monkeypatch.setenv("QUARANTINE_AFTER_NIGHTS", "10")
    go = tmp_path / "go"
    nights = [f"2026-11-0{i}" for i in range(1, 5)]
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, str(tmp_path), str(go), n, "20"],
                              stdout=subprocess.PIPE, text=True) for n in nights]
    for p in procs:
        assert p.stdout.readline().strip() == "READY"
    go.touch()
    deadline = time.monotonic() + 60
    for p in procs:
        assert p.wait(timeout=max(1, deadline - time.monotonic())) == 0
    final = _mk(tmp_path)
    for i in range(20):
        assert sorted(final._state[f"https://x.test/u{i}.pdf"]["nights"]) == nights
