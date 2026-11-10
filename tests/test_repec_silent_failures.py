"""Silent RePEc failures (PR A of "Silent failures and grit", 2026-11).

A1: a paper page that fails to fetch, or that carries no PDF candidate, used
to be dropped by `discover_bank` with a bare `continue` -- re-fetched every
night and visible nowhere. They are now COUNTED in the run-report
(`SourceStats.skipped` / `skip_samples`) but never alert: `finish()` ignores
skips, so the exit-code doctrine is unchanged.
"""
import pytest

from cb_corpus.runreport import _SAMPLE_CAP, RunReport, SourceStats
from cb_corpus.sources.repec import IDEAS
from tests.test_repec_truncation import (
    _ExactURLFetcher,
    _discovery_page_html,
    _fixture,
    _mk_discovery,
    _real_fetcher,
)


# ---------------------------------------------------------------------------
# A1 -- SourceStats skip tally
# ---------------------------------------------------------------------------

def test_record_skip_tallies_per_reason_and_keeps_samples():
    s = SourceStats("se")
    s.record_skip("paper_fetch_failed", "u1: boom")
    s.record_skip("paper_no_pdf", "u2")
    s.record_skip("paper_fetch_failed", "u3: boom")
    assert s.skipped == {"paper_fetch_failed": 2, "paper_no_pdf": 1}
    assert s.skip_samples == ["u1: boom", "u2", "u3: boom"]
    # skips are not errors
    assert s.fetch_errors == 0 and s.error_samples == [] and s.truncated is False


def test_skip_samples_are_capped_and_truncated_but_tally_is_not():
    s = SourceStats("se")
    for i in range(_SAMPLE_CAP + 3):
        s.record_skip("paper_no_pdf", f"u{i}" + "x" * 400)
    assert s.skipped == {"paper_no_pdf": _SAMPLE_CAP + 3}
    assert len(s.skip_samples) == _SAMPLE_CAP
    assert all(len(x) == 300 for x in s.skip_samples)
    assert s.skip_samples[0].startswith("u0x")


def test_to_dict_carries_skips_and_defaults_are_empty():
    s = SourceStats("se")
    d = s.to_dict()
    assert d["skipped"] == {} and d["skip_samples"] == []
    s.record_skip("paper_no_pdf", "u")
    d = s.to_dict()
    assert d["skipped"] == {"paper_no_pdf": 1} and d["skip_samples"] == ["u"]
    # copies, not live references
    d["skipped"]["paper_no_pdf"] = 99
    d["skip_samples"].append("x")
    assert s.skipped == {"paper_no_pdf": 1} and s.skip_samples == ["u"]


def test_two_stats_do_not_share_skip_containers():
    a, b = SourceStats("a"), SourceStats("b")
    a.record_skip("paper_no_pdf", "u")
    assert b.skipped == {} and b.skip_samples == []


def test_skips_never_change_finish_with_a_new_doc():
    r = RunReport("t", "repec")
    s = r.source("se")
    s.record_saved_counts({"saved": 1})
    for _ in range(50):
        s.record_skip("paper_fetch_failed", "u: 404")
    assert r.finish() == 0 and r.outcome == "ok"


def test_skips_never_change_finish_on_a_zero_new_run():
    """Zero new documents with ONLY skips is still clean (doctrine: degraded
    needs errors or truncation, and skips are neither)."""
    r = RunReport("t", "repec")
    s = r.source("se")
    s.record_skip("paper_no_pdf", "u")
    s.record_skip("paper_fetch_failed", "v: 404")
    assert r.finish() == 0


def test_skips_do_not_mask_the_degraded_doctrine():
    r = RunReport("t", "repec")
    s = r.source("se")
    s.record_skip("paper_no_pdf", "u")
    s.record_fetch_error("listing boom")
    assert r.finish() == 3


def test_run_report_to_dict_has_skip_keys_per_source():
    r = RunReport("t", "repec")
    r.source("se").record_skip("paper_no_pdf", "u")
    r.finish()
    src = r.to_dict()["sources"][0]
    assert src["skipped"] == {"paper_no_pdf": 1} and src["skip_samples"] == ["u"]


# ---------------------------------------------------------------------------
# A1 -- discover_bank records the skips
# ---------------------------------------------------------------------------

_BASE = f"{IDEAS}/s/hhs/rbnkwp"
_P = f"{IDEAS}/p/hhs/rbnkwp"
_GOOD_HTML = ('<html><head><title>Good</title></head><body>'
              '<a href="https://www.riksbank.se/wp/good.pdf">pdf</a></body></html>')
_NOPDF_HTML = '<html><head><title>No pdf</title></head><body>abstract only</body></html>'


def _se_series(monkeypatch):
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType
    monkeypatch.setitem(R.SERIES, "se", [("hhs:rbnkwp", DocType.D1)])


def _three_paper_fetcher():
    # 0001 good, 0002 missing (fetch fails), 0003 no PDF; page 2 repeats -> clean end
    return _ExactURLFetcher({
        f"{_BASE}.html": _discovery_page_html(["0001", "0002", "0003"]),
        f"{_BASE}2.html": _discovery_page_html(["0001"]),
        f"{_P}/0001.html": _GOOD_HTML,
        f"{_P}/0003.html": _NOPDF_HTML,
    })


def test_discover_bank_records_fetch_failed_and_no_pdf(monkeypatch):
    _se_series(monkeypatch)
    d = _mk_discovery(_three_paper_fetcher())
    stats = SourceStats("se")
    recs = list(d.discover_bank("se", stats=stats))
    assert [r.source_url for r in recs] == [f"{_P}/0001.html"]
    assert stats.skipped == {"paper_fetch_failed": 1, "paper_no_pdf": 1}
    assert stats.skip_samples[0].startswith(f"{_P}/0002.html: ")
    assert "404" in stats.skip_samples[0]          # the exception text is kept
    assert stats.skip_samples[1] == f"{_P}/0003.html"
    assert stats.fetch_errors == 0 and stats.truncated is False


def test_discover_bank_known_papers_are_not_counted_as_skips(monkeypatch):
    """skip_url hits stay on their own stderr counter, not in stats.skipped."""
    _se_series(monkeypatch)
    d = _mk_discovery(_three_paper_fetcher())
    stats = SourceStats("se")
    list(d.discover_bank("se", skip_url=lambda u: True, stats=stats))
    assert stats.skipped == {} and stats.skip_samples == []


def test_discover_bank_without_stats_is_unchanged(monkeypatch):
    _se_series(monkeypatch)
    d = _mk_discovery(_three_paper_fetcher())
    recs = list(d.discover_bank("se"))
    assert [r.source_url for r in recs] == [f"{_P}/0001.html"]


# ---------------------------------------------------------------------------
# A1 -- end to end: pipeline.run_repec, real Fetcher, real chained 404
# ---------------------------------------------------------------------------

class _KnownExceptStorage:
    def __init__(self, unknown):
        self.unknown = set(unknown)
        self.saved = []

    def is_known_source_url(self, url):
        return url not in self.unknown

    def save_many(self, recs, dry_run=True, label=""):
        self.saved = list(recs)
        return {"saved": len(self.saved)} if self.saved else {}


def _run_repec_skips(monkeypatch, tmp_path, with_good):
    import cb_corpus.pipeline as P
    from cb_corpus.config import Config
    from cb_corpus.sources.repec import parse_series_page

    _se_series(monkeypatch)
    papers = parse_series_page(_fixture("rbnkwp.html"))
    good, gone, nopdf = papers[0], papers[1], papers[2]
    routes = {gone: (404, "<html>not found</html>"), nopdf: (200, _NOPDF_HTML)}
    unknown = [gone, nopdf]
    if with_good:
        routes[good] = (200, _GOOD_HTML)
        unknown.append(good)
    cfg = Config(data_dir=tmp_path / "data", min_delay_seconds=0.0)
    fetcher, session = _real_fetcher(monkeypatch, 404, extra_routes=routes, config=cfg)
    storage = _KnownExceptStorage(unknown)
    monkeypatch.setattr(P, "_make_storage",
                        lambda config=None, html_to_pdf=None: (cfg, fetcher, storage))
    report = RunReport("central-bank-corpus", "repec")
    P.run_repec(["se"], dry_run=True, config=cfg, report=report)
    return report, storage, session, gone, nopdf


@pytest.mark.parametrize("with_good", [True, False])
def test_run_repec_counts_skips_and_exits_0(monkeypatch, tmp_path, with_good):
    report, storage, session, gone, nopdf = _run_repec_skips(monkeypatch, tmp_path, with_good)
    assert session.calls.count(gone) == 1          # real 404: fails fast, once
    assert len(storage.saved) == (1 if with_good else 0)
    rc = report.finish()
    src = report.to_dict()["sources"][0]
    assert src["source_code"] == "se"
    assert src["skipped"] == {"paper_fetch_failed": 1, "paper_no_pdf": 1}
    assert src["skip_samples"][0].startswith(f"{gone}: ")
    assert "HTTP 404" in src["skip_samples"][0]
    assert src["skip_samples"][1] == nopdf
    assert src["fetch_errors"] == 0 and src["truncated"] is False
    assert rc == 0
