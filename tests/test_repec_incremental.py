"""RePEc incremental mode: pre-fetch skip + stop-on-known pagination."""
from cb_corpus.sources.repec import RePEcDiscovery, IDEAS


SERIES_HANDLE = "hhs:rbnkwp"
BASE = f"{IDEAS}/s/hhs/rbnkwp"


def _series_html(paper_ids):
    links = "".join(f'<li><a href="/p/hhs/rbnkwp/{p}.html">P{p}</a></li>'
                    for p in paper_ids)
    return f"<html><body><ul>{links}</ul></body></html>"


def _paper_html(pid):
    return (f'<html><head><title>Paper {pid}</title></head><body>'
            f'<h1>Paper {pid}</h1>'
            f'<a href="https://bank.test/wp/{pid}.pdf">Full text</a>'
            f'</body></html>')


class FakeFetcher:
    """Serves canned series/paper pages and records every URL fetched."""
    def __init__(self, pages):
        # pages: {url: html}; anything else raises (like a 404)
        self.pages = pages
        self.fetched = []

    def get_text(self, url):
        self.fetched.append(url)
        if url not in self.pages:
            raise RuntimeError(f"404 {url}")
        return self.pages[url]


def _mk(pages, max_items=5000):
    d = RePEcDiscovery.__new__(RePEcDiscovery)
    d.fetcher = FakeFetcher(pages)
    d.max_pages = 80
    d.max_items = max_items
    return d


def _paper_url(pid):
    return f"{IDEAS}/p/hhs/rbnkwp/{pid}.html"


def test_skip_url_prevents_paper_page_fetch(monkeypatch):
    from cb_corpus.sources import repec as R
    monkeypatch.setitem(R.SERIES, "se", [(SERIES_HANDLE, __import__("cb_corpus.taxonomy", fromlist=["DocType"]).DocType.D1)])
    pages = {f"{BASE}.html": _series_html(["0001", "0002"]),
             _paper_url("0002"): _paper_html("0002")}
    d = _mk(pages)
    known = {_paper_url("0001")}
    recs = list(d.discover_bank("se", skip_url=lambda u: u in known))
    assert [r.source_url for r in recs] == [_paper_url("0002")]
    assert _paper_url("0001") not in d.fetcher.fetched   # never fetched


def test_stop_on_known_stops_after_all_known_page(monkeypatch):
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType
    monkeypatch.setitem(R.SERIES, "se", [(SERIES_HANDLE, DocType.D1)])
    pages = {f"{BASE}.html": _series_html(["0010", "0011"]),
             f"{BASE}2.html": _series_html(["0001", "0002"])}
    d = _mk(pages)
    known = {_paper_url(p) for p in ("0010", "0011", "0001", "0002")}
    list(d.discover_bank("se", skip_url=lambda u: u in known, stop_on_known=True))
    # page 1 is fully known -> pagination must stop; page 2 never requested
    assert f"{BASE}2.html" not in d.fetcher.fetched


def test_page_with_one_unknown_keeps_paginating(monkeypatch):
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType
    monkeypatch.setitem(R.SERIES, "se", [(SERIES_HANDLE, DocType.D1)])
    pages = {f"{BASE}.html": _series_html(["0010", "0011"]),
             f"{BASE}2.html": _series_html(["0001"]),
             _paper_url("0011"): _paper_html("0011"),
             _paper_url("0001"): _paper_html("0001")}
    d = _mk(pages)
    known = {_paper_url("0010")}
    recs = list(d.discover_bank("se", skip_url=lambda u: u in known, stop_on_known=True))
    assert f"{BASE}2.html" in d.fetcher.fetched          # kept going
    assert {r.source_url for r in recs} == {_paper_url("0011"), _paper_url("0001")}


def test_default_behavior_unchanged(monkeypatch):
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType
    monkeypatch.setitem(R.SERIES, "se", [(SERIES_HANDLE, DocType.D1)])
    pages = {f"{BASE}.html": _series_html(["0001"]),
             _paper_url("0001"): _paper_html("0001")}
    d = _mk(pages)
    recs = list(d.discover_bank("se"))                   # no new kwargs
    assert len(recs) == 1 and recs[0].document_url == "https://bank.test/wp/0001.pdf"


def test_max_items_caps_papers_considered_per_series(monkeypatch):
    """discover_bank must cap paper-page URLs considered per series at
    max_items, counted at listing level -- mirroring the old
    _series_paper_urls flattened-list truncation."""
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType
    monkeypatch.setitem(R.SERIES, "se", [(SERIES_HANDLE, DocType.D1)])
    ids = ["0001", "0002", "0003", "0004", "0005"]
    pages = {f"{BASE}.html": _series_html(ids)}
    pages.update({_paper_url(p): _paper_html(p) for p in ids})
    d = _mk(pages, max_items=3)
    list(d.discover_bank("se"))
    paper_fetches = [u for u in d.fetcher.fetched if u.startswith(f"{IDEAS}/p/")]
    assert len(paper_fetches) == 3


def test_max_items_binding_exactly_on_page_stops_pagination(monkeypatch):
    """When max_items is fully satisfied by the current listing page, the
    next listing page must never be fetched -- the cap must stop pagination
    immediately, not one iteration late (which would waste a real HTTP
    listing-page fetch per capped series)."""
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType
    monkeypatch.setitem(R.SERIES, "se", [(SERIES_HANDLE, DocType.D1)])
    page1_ids = ["0001", "0002", "0003"]
    page2_ids = ["0004", "0005"]
    pages = {f"{BASE}.html": _series_html(page1_ids),
             f"{BASE}2.html": _series_html(page2_ids)}
    pages.update({_paper_url(p): _paper_html(p) for p in page1_ids + page2_ids})
    d = _mk(pages, max_items=3)
    list(d.discover_bank("se"))
    assert f"{BASE}2.html" not in d.fetcher.fetched


def test_storage_is_known_source_url(tmp_path):
    import json
    from cb_corpus.config import Config
    from cb_corpus.storage import Storage
    data = tmp_path / "data"
    (data / "manifest").mkdir(parents=True)
    row = {"corpus": "central-bank", "source_code": "se", "doc_type": "D1", "title": "t",
           "document_url": "https://bank.test/wp/1.pdf",
           "source_url": _paper_url("0001"),
           "date": "2026-01-01", "language": "en", "provenance": "repec_discovery",
           "mime_type": "application/pdf", "sha256": "x", "local_path": "y",
           "doc_id": "abc123", "year": 2026}
    (data / "manifest" / "se.jsonl").write_text(json.dumps(row) + "\n")
    st = Storage(Config(data_dir=data))
    assert st.is_known_source_url(_paper_url("0001"))
    assert not st.is_known_source_url(_paper_url("9999"))
    # deliberately separate semantics: source pages are NOT pdf-url-known
    assert not st.is_known_url(_paper_url("0001"))


def test_run_repec_incremental_wiring(monkeypatch, tmp_path):
    """incremental=True wires storage.is_known_source_url + stop_on_known."""
    import cb_corpus.pipeline as P
    captured = {}

    class _FakeRep:
        def __init__(self, fetcher): pass
        def discover_bank(self, code, skip_url=None, stop_on_known=False):
            captured["skip_url"] = skip_url
            captured["stop_on_known"] = stop_on_known
            return iter(())

    monkeypatch.setattr("cb_corpus.sources.repec.RePEcDiscovery", _FakeRep)
    from cb_corpus.config import Config
    (tmp_path / "manifest").mkdir(parents=True)
    P.run_repec(bank_codes=["se"], dry_run=True,
                config=Config(data_dir=tmp_path), incremental=True)
    assert captured["stop_on_known"] is True
    assert callable(captured["skip_url"])


def test_discover_bank_prints_skipped_known_counter(monkeypatch, capsys):
    """discover_bank must report, at bank end, how many listed paper pages
    were skipped as already-known -- the only visibility channel for
    revision-blindness (spec 2026-07-16 amendment). Spy on stderr via capsys
    against a known/unknown mix."""
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType
    monkeypatch.setitem(R.SERIES, "se", [(SERIES_HANDLE, DocType.D1)])
    ids = ["0001", "0002", "0003"]
    pages = {f"{BASE}.html": _series_html(ids)}
    pages.update({_paper_url(p): _paper_html(p) for p in ids})
    d = _mk(pages)
    known = {_paper_url("0001"), _paper_url("0002")}   # 2 known, 1 unknown
    list(d.discover_bank("se", skip_url=lambda u: u in known))
    err = capsys.readouterr().err
    assert "[repec:se] skipped-known: 2" in err


def test_discover_bank_skipped_known_counter_zero_when_no_skip(monkeypatch, capsys):
    """No skip_url -> no known papers -> counter must still print, at 0
    (not omitted), so the absence of the line is never mistaken for the
    counter not having run."""
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType
    monkeypatch.setitem(R.SERIES, "se", [(SERIES_HANDLE, DocType.D1)])
    pages = {f"{BASE}.html": _series_html(["0001"]),
             _paper_url("0001"): _paper_html("0001")}
    d = _mk(pages)
    list(d.discover_bank("se"))
    err = capsys.readouterr().err
    assert "[repec:se] skipped-known: 0" in err


def test_discover_bank_counter_prints_on_early_close(monkeypatch, capsys):
    """The counter line must print via `finally`, so it still fires when the
    consumer stops iterating early (generator `.close()`) instead of running
    the walk to natural exhaustion -- e.g. a caller that only wants the first
    record, or an early abort."""
    from cb_corpus.sources import repec as R
    from cb_corpus.taxonomy import DocType
    monkeypatch.setitem(R.SERIES, "se", [(SERIES_HANDLE, DocType.D1)])
    ids = ["0001", "0002", "0003"]
    pages = {f"{BASE}.html": _series_html(ids)}
    pages.update({_paper_url(p): _paper_html(p) for p in ids})
    d = _mk(pages)
    known = {_paper_url("0002")}   # one known, rest unknown
    gen = d.discover_bank("se", skip_url=lambda u: u in known)
    next(gen)          # consume only the first yielded record
    gen.close()         # early close -- generator never reaches exhaustion
    err = capsys.readouterr().err
    assert "[repec:se] skipped-known:" in err


def test_run_repec_full_mode_also_passes_skip_url(monkeypatch, tmp_path):
    """Full sweeps (incremental=False) still pass skip_url=storage.is_known_source_url
    -- collision-free by construction since the RePEc source_url IS the per-record
    identity page -- but stop_on_known stays False (full pagination preserved)."""
    import cb_corpus.pipeline as P
    captured = {}

    class _FakeRep:
        def __init__(self, fetcher): pass
        def discover_bank(self, code, skip_url=None, stop_on_known=False):
            captured["skip_url"] = skip_url
            captured["stop_on_known"] = stop_on_known
            return iter(())

    monkeypatch.setattr("cb_corpus.sources.repec.RePEcDiscovery", _FakeRep)
    from cb_corpus.config import Config
    (tmp_path / "manifest").mkdir(parents=True)
    P.run_repec(bank_codes=["se"], dry_run=True,
                config=Config(data_dir=tmp_path), incremental=False)
    assert captured["stop_on_known"] is False
    assert callable(captured["skip_url"])
