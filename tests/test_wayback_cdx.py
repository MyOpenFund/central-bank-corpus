"""Wayback CDX queries carry the target URL percent-encoded (issue #24).

The target used to be interpolated raw: a Bank of England media URL
(`...pdf?la=en&hash=...`) split into extra top-level CDX parameters at its
`&`, a `#toc3` fragment truncated the request, and the call sites' blanket
`except` turned the garbled query into a silent "not archived". These tests
inspect the query string ACTUALLY sent and parse it back: the `url` parameter
must round-trip to the exact target and no part of it may leak into another
parameter. No network: the fetcher records queries and returns canned JSON.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urldefrag, urlsplit

import pytest

from cb_corpus.sources.wayback import (CDX, cdx_pdfs, first_capture, latest_capture,
                                       wayback_for_url)


class _Rec:
    def __init__(self, rows):
        self.rows = rows
        self.queries: list[str] = []

    def get_text(self, url, *a, **kw):
        self.queries.append(url)
        return json.dumps(self.rows)


TARGETS = [
    "https://www.bankofengland.co.uk/-/media/boe/files/working-paper/2019/x.pdf"
    "?la=en&hash=0A1B2C3D",
    "https://www.ecb.europa.eu/pub/pdf/other/ecb.report.en.pdf#toc3",
    "https://x.example/get?id=12&lang=en",
    "https://x.example/100%25 sure/a b.pdf",
    "x.example/path with space/r&d.pdf",          # wp-dates sends scheme-less URLs
]


def _params(q: str) -> dict[str, list[str]]:
    parts = urlsplit(q)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == CDX
    assert "#" not in q and " " not in q
    return parse_qs(parts.query, keep_blank_values=True)


@pytest.mark.parametrize("target", TARGETS)
def test_first_capture_sends_the_exact_target(target):
    f = _Rec([["timestamp"], ["20190101000000"]])
    assert first_capture(f, target) == "20190101000000"
    p = _params(f.queries[0])
    assert p["url"] == [urldefrag(target).url]      # fragment never sent
    assert set(p) == {"url", "filter", "output", "fl", "limit"}
    assert p["limit"] == ["1"] and p["filter"] == ["statuscode:200"]


@pytest.mark.parametrize("target", TARGETS)
def test_latest_capture_sends_the_exact_target(target):
    f = _Rec([["timestamp"], ["20230515000000"]])
    assert latest_capture(f, target) == "20230515000000"
    p = _params(f.queries[0])
    assert p["url"] == [urldefrag(target).url]      # fragment never sent
    assert set(p) == {"url", "filter", "output", "fl", "limit"}
    assert p["filter"] == ["statuscode:200", "mimetype:application/pdf"]
    assert "matchType" not in p


@pytest.mark.parametrize("target", TARGETS)
def test_cdx_pdfs_sends_the_exact_prefix_as_a_parameter(target):
    f = _Rec([["original", "timestamp"], [target, "2017"]])
    assert cdx_pdfs(f, target, mimetype="text/html") == [(target, "2017")]
    p = _params(f.queries[0])
    assert p["url"] == [urldefrag(target).url]      # fragment never sent
    assert p["matchType"] == ["prefix"]
    assert p["filter"] == ["statuscode:200", "mimetype:text/html"]
    assert p["collapse"] == ["urlkey"]
    assert "*" not in f.queries[0]


def test_ampersand_no_longer_splits_into_a_stray_parameter():
    """The issue's repro: `hash` must not become a CDX parameter."""
    f = _Rec([["timestamp"]])
    latest_capture(f, TARGETS[0])
    q = f.queries[0]
    assert "&hash=" not in q and "&la=" not in q
    assert "url=https%3A%2F%2Fwww.bankofengland.co.uk%2F" in q


def test_wayback_for_url_keeps_the_original_url_in_the_raw_snapshot():
    """Only the CDX query is encoded; the raw-snapshot URL Storage downloads
    embeds the original URL as Wayback expects it."""
    f = _Rec([["timestamp"], ["20170811213710"]])
    assert wayback_for_url(f, TARGETS[0]) == (
        "https://web.archive.org/web/20170811213710id_/" + TARGETS[0])


def test_fragment_is_stripped_from_the_cdx_target_only():
    """A fragment is client-side: archive.org indexes the URL without it, so
    `#toc3` would only make the exact-URL match miss. It is dropped from the
    CDX query (a `#` or `%23` never reaches it); the raw-snapshot URL keeps the
    original as given."""
    target = "https://www.ecb.europa.eu/pub/pdf/other/ecb.report.en.pdf#toc3"
    for fn in (first_capture, latest_capture):
        f = _Rec([["timestamp"], ["20200101000000"]])
        fn(f, target)
        q = f.queries[0]
        assert "toc3" not in q and "%23" not in q
        assert _params(q)["url"] == ["https://www.ecb.europa.eu/pub/pdf/other/ecb.report.en.pdf"]
    f = _Rec([["original", "timestamp"]])
    cdx_pdfs(f, "https://x.example/dir/#frag")
    assert _params(f.queries[0])["url"] == ["https://x.example/dir/"]
    f = _Rec([["timestamp"], ["20200101000000"]])
    assert wayback_for_url(f, target).endswith("id_/" + target)
