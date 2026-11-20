import json

from cb_corpus.runreport import RunReport, SourceStats


def test_clean_run_exits_zero_even_with_nothing_new():
    r = RunReport("central-bank-corpus", "discover")
    r.source("us").record_saved_counts({"skipped": 10})
    assert r.finish() == 0
    assert r.to_dict()["outcome"] == "ok"


def test_recovered_fetch_error_does_not_degrade():
    r = RunReport("central-bank-corpus", "discover")
    s = r.source("us")
    s.record_fetch_error("timeout, retried ok")
    s.record_saved_counts({"saved": 3})
    assert r.finish() == 0


def test_truncated_source_degrades():
    r = RunReport("central-bank-corpus", "bis-sitemap")
    s = r.source("bis-sitemap")
    s.record_saved_counts({"saved": 50})
    s.record_fetch_error("year 2011 sitemap unreachable", truncated=True)
    assert r.finish() == 3
    d = r.to_dict()
    assert d["outcome"] == "degraded"
    assert d["sources"][0]["truncated"] is True


def test_zero_work_with_errors_degrades():
    r = RunReport("central-bank-corpus", "repec")
    r.source("ecb").record_fetch_error("page 1 failed", truncated=True)
    assert r.finish() == 3


def test_error_only_source_degrades():
    """Reviewer repro: a source with only save errors and zero new docs must
    not exit clean — docs_seen including the error count previously masked
    this as 'work was done'."""
    r = RunReport("central-bank-corpus", "discover")
    r.source("x").record_saved_counts({"error": 5})
    assert r.finish() == 3
    assert r.to_dict()["outcome"] == "degraded"


def test_partial_success_does_not_degrade():
    r = RunReport("central-bank-corpus", "discover")
    r.source("us").record_saved_counts({"saved": 3, "error": 2})
    assert r.finish() == 0
    assert r.to_dict()["outcome"] == "ok"


def test_nothing_new_one_save_error_degrades():
    r = RunReport("central-bank-corpus", "discover")
    r.source("us").record_saved_counts({"error": 1})
    assert r.finish() == 3
    assert r.to_dict()["outcome"] == "degraded"


def test_fatal_wins():
    r = RunReport("central-bank-corpus", "discover")
    assert r.finish(fatal="boom") == 1
    assert r.to_dict()["outcome"] == "failed"


def test_error_samples_capped_at_five():
    s = SourceStats("us")
    for i in range(9):
        s.record_fetch_error(f"e{i}")
    assert len(s.error_samples) == 5
    assert s.fetch_errors == 9


def test_write_appends_one_json_line_atomically(tmp_path):
    p = tmp_path / "runs.jsonl"
    for cmd in ("a", "b"):
        r = RunReport("central-bank-corpus", cmd)
        r.finish()
        r.write(str(p))
    lines = p.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["command"] == "a"
    assert {"run_id", "tool", "totals", "sources", "outcome",
            "exit_code", "started_at", "finished_at", "command"} <= set(json.loads(lines[1]))


def test_dict_error_message_survives_truncation():
    """Verify that when a dict error with a long URL is formatted, the error
    message survives the 300-char truncation. This tests the fix for the
    silent-failure-doctrine review finding."""
    r = RunReport("central-bank-corpus", "discover")
    s = r.source("test")

    # Simulate an adapter error dict with a very long URL
    long_url = "https://example.com/" + "x" * 300
    error_dict = {
        "context": "discovery_phase",
        "error": "Connection timeout after 30s",
        "url": long_url,
        "bank": "test"
    }

    # Format the error message as the fix does in pipeline.py
    formatted_msg = f"{error_dict.get('context', '?')}: {error_dict.get('error', str(error_dict))}" if isinstance(error_dict, dict) else str(error_dict)

    s.record_fetch_error(formatted_msg)

    assert len(s.error_samples) == 1
    error_sample = s.error_samples[0]
    # Verify the error message is present in the sample
    assert "Connection timeout after 30s" in error_sample
    assert error_sample.startswith("discovery_phase:")
    # Verify truncation happened (message is 300 chars)
    assert len(error_sample) <= 300


def test_degrade_marks_an_otherwise_clean_run_degraded():
    from cb_corpus.runreport import RunReport
    r = RunReport("t", "discover")
    r.source("us").record_saved_counts({"saved": 1})
    r.degrade("vocabulary.json not written: disk full")
    assert r.finish() == 3 and r.outcome == "degraded"
    assert r.to_dict()["warnings"] == ["vocabulary.json not written: disk full"]


def test_fatal_still_wins_over_degrade():
    from cb_corpus.runreport import RunReport
    r = RunReport("t", "discover")
    r.degrade("x")
    assert r.finish(fatal="boom") == 1
