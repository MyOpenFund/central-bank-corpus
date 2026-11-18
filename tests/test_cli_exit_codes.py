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


def test_data_tree_listing_sees_new_nested_files(tmp_path):
    """The session guard in conftest.py diffs this listing before/after."""
    from tests.conftest import data_tree_listing

    assert data_tree_listing(tmp_path / "absent") == set()
    (tmp_path / "manifest").mkdir()
    (tmp_path / "manifest" / "us.jsonl").write_text("{}\n")
    before = data_tree_listing(tmp_path)
    (tmp_path / "runs.jsonl").write_text("{}\n")
    (tmp_path / "raw" / "us").mkdir(parents=True)
    (tmp_path / "raw" / "us" / "x.pdf").write_bytes(b"%PDF")
    assert data_tree_listing(tmp_path) - before == {"runs.jsonl", "raw/us/x.pdf"}
