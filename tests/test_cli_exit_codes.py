import json

import cb_corpus.cli as cli


def _read_report(data_dir):
    lines = (data_dir / "runs.jsonl").read_text().strip().split("\n")
    return json.loads(lines[-1])


def test_discover_clean_run_exits_zero_and_reports(tmp_path, monkeypatch):
    def fake_run(**kwargs):
        report = kwargs.get("report")
        report.source("us").record_saved_counts({"saved": 2})
        return {"us": {"saved": 2}}

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setenv("CB_DATA_DIR", str(tmp_path))
    rc = cli.main(["discover", "--banks", "us"])
    assert rc == 0
    rep = _read_report(tmp_path)
    assert rep["outcome"] == "ok" and rep["command"] == "discover"


def test_discover_truncated_source_exits_three(tmp_path, monkeypatch):
    def fake_run(**kwargs):
        s = kwargs["report"].source("us")
        s.record_fetch_error("listing page unreachable", truncated=True)
        return {"us": {}}

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setenv("CB_DATA_DIR", str(tmp_path))
    assert cli.main(["discover", "--banks", "us"]) == 3
    assert _read_report(tmp_path)["outcome"] == "degraded"


def test_crash_writes_failed_report_and_exits_one(tmp_path, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(cli, "run", boom)
    monkeypatch.setenv("CB_DATA_DIR", str(tmp_path))
    assert cli.main(["discover", "--banks", "us"]) == 1
    rep = _read_report(tmp_path)
    assert rep["outcome"] == "failed" and "adapter exploded" in rep["fatal"]


def test_list_banks_stays_reportless_and_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CB_DATA_DIR", str(tmp_path))
    assert cli.main(["list-banks"]) == 0
    assert not (tmp_path / "runs.jsonl").exists()


def test_suite_pins_runs_log_outside_the_repo(tmp_path, monkeypatch):
    """A test that does NOT set CB_DATA_DIR itself (as test_framework's
    dispatch test and test_seams' --native-only test don't) must still have
    its run-report land under its own tmp dir, never in the repo's data/:
    the autouse fixture in conftest.py pins CB_DATA_DIR per test."""
    from pathlib import Path

    from tests.conftest import REPO_DATA_DIR

    runs = cli._runs_path()
    assert runs.parent.is_relative_to(tmp_path)
    assert not Path(runs).resolve().is_relative_to(REPO_DATA_DIR)

    monkeypatch.setattr(cli, "run", lambda **k: {"us": {}})
    assert cli.main(["discover", "--banks", "us"]) == 0
    assert runs.exists()


def test_guard_catches_append_to_preexisting_runs_log(tmp_path):
    """Every checkout that ran the old suite already has a data/runs.jsonl:
    an append to it (same path, grown file) must trip the session guard in
    conftest.py, as must a new file or an edited manifest."""
    import os

    from tests.conftest import data_tree_changes, data_tree_snapshot

    assert data_tree_snapshot(tmp_path / "absent") == {}
    runs = tmp_path / "runs.jsonl"
    runs.write_text('{"run": 1}\n')
    (tmp_path / "manifest").mkdir()
    man = tmp_path / "manifest" / "us.jsonl"
    man.write_text("{}\n")
    before = data_tree_snapshot(tmp_path)
    assert set(before) == {"runs.jsonl", "manifest/us.jsonl"}
    assert data_tree_changes(before, data_tree_snapshot(tmp_path)) == []

    with runs.open("a") as fh:          # 1 -> 2 lines: same path, new size
        fh.write('{"run": 2}\n')
    assert data_tree_changes(before, data_tree_snapshot(tmp_path)) == ["runs.jsonl"]

    # same-size rewrite: only the mtime moves
    st = man.stat()
    man.write_text("[]\n")
    os.utime(man, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "r.csv").write_text("x\n")
    assert data_tree_changes(before, data_tree_snapshot(tmp_path)) == [
        "manifest/us.jsonl", "reports/r.csv", "runs.jsonl"]


def test_guard_snapshot_never_enters_raw_or_follows_symlinks(tmp_path, monkeypatch):
    """The guard must stay cheap on a corpus host: it never descends into
    data/raw* (tens of GB of downloads), nor below manifest/ and reports/,
    and never follows a symlink."""
    import os

    from tests import conftest

    for sub in ("raw/us/2024", "raw_orphans/us", "manifest/nested"):
        (tmp_path / sub).mkdir(parents=True)
    (tmp_path / "raw" / "us" / "2024" / "x.pdf").write_bytes(b"%PDF")
    (tmp_path / "raw_orphans" / "us" / "y.pdf").write_bytes(b"%PDF")
    (tmp_path / "manifest" / "nested" / "deep.jsonl").write_text("{}\n")
    (tmp_path / "manifest" / "us.jsonl").write_text("{}\n")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "z.jsonl").write_text("{}\n")
    (tmp_path / "reports").symlink_to(outside, target_is_directory=True)
    (tmp_path / "link.jsonl").symlink_to(outside / "z.jsonl")

    scanned = []
    real_scandir = os.scandir

    def spy(path):
        scanned.append(os.path.relpath(path, tmp_path))
        return real_scandir(path)

    monkeypatch.setattr(conftest.os, "scandir", spy)
    assert conftest.data_tree_snapshot(tmp_path).keys() == {"manifest/us.jsonl"}
    assert sorted(scanned) == [".", "manifest"]
