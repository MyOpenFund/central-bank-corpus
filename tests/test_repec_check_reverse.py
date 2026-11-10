"""repec-check reverse direction: manifest rows that RePEc does not list.

The module docstring always promised it (`pending_repec` if recent, else
`unmatched_old`) but only the RePEc -> manifest direction was implemented.
Fixtures are adversarial on purpose: rows matched by each tier of the cascade
(handle, key, listing title, leftover page-fetch by URL), a recent row, an old
row, an undated row, a D2 row for a bank whose only RePEc series is D1, a
non-WP row, a truncated listing, and a custom grace window.

Bank `jp` (SERIES = boj:bojwps, D1 only; boj_code reads the paper code from a
PDF URL and from a handle alike).
"""
import csv
import json
import re
from datetime import date, timedelta

import pytest

from cb_corpus import repec_check as RC
from cb_corpus.config import Config

LISTING = "https://ideas.repec.org/s/boj/bojwps.html"


def _listing(entries):
    links = "".join(f'<li><a href="/p/boj/bojwps/{pid}.html">{t}</a></li>'
                    for pid, t in entries)
    return f"<html><body><ul>{links}</ul></body></html>"


def _paper(title, pdf):
    return (f'<html><head><meta name="citation_title" content="{title}">'
            f'<meta name="citation_pdf_url" content="{pdf}"></head>'
            f'<body><h1>{title}</h1><a href="{pdf}">Download</a></body></html>')


class FakeFetcher:
    def __init__(self, listing_pages, papers):
        self.listing_pages = listing_pages   # page no -> html | Exception
        self.papers = papers                 # pid -> html

    def get_text(self, url):
        m = re.search(r"/s/boj/bojwps(\d*)\.html$", url)
        if m:
            page = int(m.group(1) or 1)
            val = self.listing_pages.get(page, self.listing_pages[1])
            if isinstance(val, Exception):
                raise val
            return val
        m = re.search(r"/p/boj/bojwps/([^/.]+)\.html$", url)
        if m and m.group(1) in self.papers:
            return self.papers[m.group(1)]
        raise ConnectionError(f"unexpected url {url}")


TODAY = date.today()
RECENT = (TODAY - timedelta(days=10)).isoformat()
SIXTY = (TODAY - timedelta(days=60)).isoformat()


def _rows():
    def r(doc_id, doc_type="D1", date_=None, **kw):
        row = {"doc_id": doc_id, "bank_code": "jp", "doc_type": doc_type,
               "title": kw.pop("title", doc_id), "pdf_url": kw.pop("pdf_url", f"https://x.test/{doc_id}.pdf"),
               "source_url": kw.pop("source_url", "")}
        if date_:
            row["date"] = date_
        row.update(kw)
        return row
    return [
        r("by_handle", date_="2020-05-01", source_url="https://ideas.repec.org/p/boj/bojwps/wp20e01.html"),
        r("by_key", date_="2021-02-03", pdf_url="https://www.boj.or.jp/en/research/wps_rev/wps_2021/data/wp21e02.pdf"),
        r("by_title", date_="2019-01-01", title="A Paper Listed Under Its Exact Title"),
        r("by_pagefetch", date_="2018-07-01", pdf_url="https://mirror.test/legacy/paper-c.pdf"),
        r("recent_orphan", date_=RECENT),
        r("old_orphan", date_="2019-06-01"),
        r("pre_era", date_="2005-03-01"),
        r("month_only", date_="2019-08"),
        r("undated_orphan"),
        r("sixty_days", date_=SIXTY),
        r("d2_row", doc_type="D2", date_="2010-01-01"),
        r("speech", doc_type="C1", date_="2010-01-01"),
    ]


ENTRIES = [("wp20e01", "Handle paper"), ("wp21e02", "Key paper"),
           ("wp19e03", "A paper listed under its exact title!"),
           ("wp18e04", "Listing title that matches nothing")]
PAPERS = {"wp18e04": _paper("Canonical title C", "https://mirror.test/legacy/paper-c.pdf")}


@pytest.fixture
def cfg(tmp_path):
    c = Config(data_dir=tmp_path / "data")
    c.manifest_dir.mkdir(parents=True)
    with open(c.manifest_file("jp"), "w") as fh:
        for row in _rows():
            fh.write(json.dumps(row) + "\n\n")   # blank lines between rows
    return c


def _run(cfg, monkeypatch, listing_pages=None, **kw):
    fetcher = FakeFetcher(listing_pages or {1: _listing(ENTRIES)}, PAPERS)
    monkeypatch.setattr(RC, "Fetcher", lambda c: fetcher)
    rev = cfg.reports_dir / "rev.csv"
    res = RC.run_repec_check(bank_codes=["jp"], config=cfg,
                             csv_path=str(cfg.reports_dir / "fwd.csv"),
                             reverse_csv_path=str(rev), **kw)
    rows = list(csv.DictReader(open(rev))) if rev.exists() else []
    return res["jp"], {r["doc_id"]: r["bucket"] for r in rows}, rows


def test_every_cascade_tier_counts_as_matched(cfg, monkeypatch):
    summary, buckets, _ = _run(cfg, monkeypatch)
    for matched in ("by_handle", "by_key", "by_title", "by_pagefetch"):
        assert matched not in buckets
    # forward direction unchanged: all 4 RePEc papers covered
    assert summary["covered"] == 4 and summary["missing_legacy"] == 0


def test_unmatched_rows_bucketed_by_grace_window(cfg, monkeypatch):
    summary, buckets, _ = _run(cfg, monkeypatch)
    assert buckets == {"recent_orphan": "pending_repec", "old_orphan": "unmatched_old",
                       "undated_orphan": "unmatched_old", "sixty_days": "unmatched_old",
                       "month_only": "unmatched_old", "pre_era": "pre_repec"}
    assert summary["pending_repec"] == 1 and summary["unmatched_old"] == 4
    assert summary["pre_repec"] == 1


def test_doc_types_without_a_series_are_ignored(cfg, monkeypatch):
    _, buckets, _ = _run(cfg, monkeypatch)
    assert "d2_row" not in buckets and "speech" not in buckets


def test_grace_days_is_a_parameter(cfg, monkeypatch):
    summary, buckets, _ = _run(cfg, monkeypatch, grace_days=90)
    assert buckets["sixty_days"] == "pending_repec"
    assert summary["pending_repec"] == 2


def test_truncated_listing_skips_reverse_check(cfg, monkeypatch):
    pages = {1: _listing(ENTRIES[:2]), 2: ConnectionError("boom")}
    summary, buckets, _ = _run(cfg, monkeypatch, listing_pages=pages)
    assert buckets == {}
    assert summary["reverse_skipped_truncated"] == 1
    assert summary["pending_repec"] == 0 and summary["unmatched_old"] == 0


def test_reverse_csv_columns(cfg, monkeypatch):
    _, _, rows = _run(cfg, monkeypatch)
    assert set(rows[0]) == {"bank", "doc_id", "doc_type", "date", "title", "pdf_url", "bucket"}
    old = next(r for r in rows if r["doc_id"] == "old_orphan")
    assert old["bank"] == "jp" and old["date"] == "2019-06-01"


def test_never_writes_the_manifest(cfg, monkeypatch):
    before = cfg.manifest_file("jp").read_bytes()
    _run(cfg, monkeypatch)
    assert cfg.manifest_file("jp").read_bytes() == before


def test_negative_grace_days_rejected(cfg, monkeypatch):
    with pytest.raises(ValueError):
        _run(cfg, monkeypatch, grace_days=-1)


def test_cli_passes_reverse_options(monkeypatch):
    from cb_corpus import cli
    seen = {}
    monkeypatch.setattr(RC, "run_repec_check", lambda **kw: seen.update(kw) or {})
    assert cli.main(["repec-check", "--banks", "jp", "--reverse-csv", "r.csv",
                     "--grace-days", "30"]) == 0
    assert seen["reverse_csv_path"] == "r.csv" and seen["grace_days"] == 30
    assert seen["bank_codes"] == ["jp"]


def test_boj_code_reads_series_pid_and_handle_forms():
    """repec-check calls the handle extractor with "series:pid"; boj_code used to
    split on "/" only, so the key tier never matched a single jp paper."""
    from cb_corpus.sources.boj_wp import boj_code
    assert boj_code("bojwps:wp21e02") == (21, "e", 2)
    assert boj_code("bojwps:02-e-2r") == (2, "e", 2)
    assert boj_code("RePEc:boj:bojwps:wp25e13") == (25, "e", 13)
    # unchanged forms
    assert boj_code("https://www.boj.or.jp/en/research/wps_rev/wps_2025/data/wp25e13.pdf") == (25, "e", 13)
    assert boj_code("https://ideas.repec.org/p/boj/bojwps/10-e-11.html") == (10, "e", 11)
    assert boj_code("") is None and boj_code("bojwps:") is None


def test_pre_repec_floor_comes_from_matched_rows_only(cfg, monkeypatch):
    """Without any matched row there is no era floor: nothing is `pre_repec`."""
    summary, buckets, _ = _run(cfg, monkeypatch, listing_pages={1: _listing([("wp99e99", "zzz")])})
    assert "pre_repec" not in buckets.values()
    assert buckets["pre_era"] == "unmatched_old"


def test_grace_boundary_is_inclusive(cfg, monkeypatch):
    _, buckets, _ = _run(cfg, monkeypatch, grace_days=60)
    assert buckets["sixty_days"] == "pending_repec"
    _, buckets, _ = _run(cfg, monkeypatch, grace_days=59)
    assert buckets["sixty_days"] == "unmatched_old"


def test_month_precision_date_counts_as_recent(tmp_path, monkeypatch):
    c = Config(data_dir=tmp_path / "data")
    c.manifest_dir.mkdir(parents=True)
    ym = TODAY.strftime("%Y-%m")
    c.manifest_file("jp").write_text(json.dumps(
        {"doc_id": "m", "bank_code": "jp", "doc_type": "D1", "title": "m",
         "pdf_url": "https://x.test/m.pdf", "date": ym}) + "\n")
    _, buckets, _ = _run(c, monkeypatch, grace_days=40)
    assert buckets == {"m": "pending_repec"}


def test_pagefetch_failure_is_counted(cfg, monkeypatch, capsys):
    papers = dict(PAPERS); PAPERS.clear()          # leftover page now fails to load
    try:
        summary, buckets, _ = _run(cfg, monkeypatch)
    finally:
        PAPERS.update(papers)
    assert summary["pagefetch_errors"] == 1
    assert buckets["by_pagefetch"] in ("unmatched_old", "pre_repec")
    assert "failed to load" in capsys.readouterr().err


def test_empty_listing_skips_reverse_check(cfg, monkeypatch, capsys):
    summary, buckets, _ = _run(cfg, monkeypatch, listing_pages={1: "<html><body></body></html>"})
    assert buckets == {} and summary["reverse_skipped_truncated"] == 1
    assert "returned no paper" in capsys.readouterr().err


def test_max_pages_reached_is_a_truncation():
    from cb_corpus.runreport import SourceStats

    class Endless:
        def get_text(self, url):
            n = re.search(r"bojwps(\d*)\.html$", url).group(1) or "1"
            return _listing([(f"wp{int(n):02d}e01", f"p{n}")])
    stats = SourceStats("t")
    out = RC.enumerate_series(Endless(), "boj:bojwps", max_pages=3, stats=stats)
    assert len(out) == 3 and stats.truncated


def test_se_key_tier_matches(tmp_path, monkeypatch):
    """se_handle_number needs the `hhs:` archive prefix: the key tier used to get
    "rbnkwp:0463" and never matched a Riksbank paper."""
    c = Config(data_dir=tmp_path / "data")
    c.manifest_dir.mkdir(parents=True)
    c.manifest_file("se").write_text(json.dumps(
        {"doc_id": "s1", "bank_code": "se", "doc_type": "D1", "date": "2019-12-01",
         "title": "Some paper (Revised December 2019)",
         "pdf_url": "https://www.riksbank.se/globalassets/media/rapporter/working-papers/2019/no.-463-some-paper.pdf"}) + "\n")

    class SeFetcher:
        def get_text(self, url):
            if re.search(r"/s/hhs/rbnkwp\d*\.html$", url):
                return ('<html><body><li><a href="/p/hhs/rbnkwp/0463.html">'
                        'Some Paper</a></li></body></html>')
            raise ConnectionError(url)
    monkeypatch.setattr(RC, "Fetcher", lambda cfg: SeFetcher())
    res = RC.run_repec_check(bank_codes=["se"], config=c,
                             csv_path=str(tmp_path / "f.csv"), reverse_csv_path=str(tmp_path / "r.csv"))
    assert res["se"]["covered"] == 1 and res["se"]["pagefetch_errors"] == 0
    assert list(csv.DictReader(open(tmp_path / "r.csv"))) == []


def test_two_series_bank_tracks_both_doc_types(tmp_path, monkeypatch):
    """ecb has D1 (ecbwps) and D2 (ecbops): rows of both types are checked, and a
    D2 row matched by the ops series is not reported."""
    c = Config(data_dir=tmp_path / "data")
    c.manifest_dir.mkdir(parents=True)
    rows = [{"doc_id": "w", "bank_code": "ecb", "doc_type": "D1", "date": "2020-01-10", "title": "w",
             "pdf_url": "https://x.test/w.pdf", "repec_handle": "RePEc:ecb:ecbwps:20202001"},
            {"doc_id": "o", "bank_code": "ecb", "doc_type": "D2", "date": "2020-02-10", "title": "o",
             "pdf_url": "https://x.test/o.pdf", "repec_handle": "RePEc:ecb:ecbops:2020240"},
            {"doc_id": "x", "bank_code": "ecb", "doc_type": "D2", "date": "2021-02-10", "title": "x",
             "pdf_url": "https://x.test/x.pdf"}]
    c.manifest_file("ecb").write_text("".join(json.dumps(r) + "\n" for r in rows))

    class EcbFetcher:
        def get_text(self, url):
            m = re.search(r"/s/ecb/(ecbwps|ecbops)\d*\.html$", url)
            if m:
                pid = "20202001" if m.group(1) == "ecbwps" else "2020240"
                return f'<html><body><li><a href="/p/ecb/{m.group(1)}/{pid}.html">t</a></li></body></html>'
            raise ConnectionError(url)
    monkeypatch.setattr(RC, "Fetcher", lambda cfg: EcbFetcher())
    RC.run_repec_check(bank_codes=["ecb"], config=c,
                       csv_path=str(tmp_path / "f.csv"), reverse_csv_path=str(tmp_path / "r.csv"))
    rev = {r["doc_id"]: r["bucket"] for r in csv.DictReader(open(tmp_path / "r.csv"))}
    assert rev == {"x": "unmatched_old"}


def test_boj_code_ignores_query_and_fragment():
    from cb_corpus.sources.boj_wp import boj_code
    assert boj_code("https://www.boj.or.jp/x/wp25e13.pdf?v=1:2") == (25, "e", 13)
    assert boj_code("https://www.boj.or.jp/x/wp25e13.pdf#a:b") == (25, "e", 13)
