"""Fix wave on the integrity storage work (issue #21, review 2026-11):
artifact file modes, env-driven content-check knobs, the bad_content
run-report counter, checked renders in convert-html / retry-html, the
MIME sniff, `.part` temps in sweep-orphans and a read-only quarantine dir."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from cb_corpus import storage as storage_mod
from cb_corpus.storage import _atomic_write_bytes
from tests.conftest import public_resolver
from tests.test_content_integrity import (U, _rec, _render_storage, _storage,
                                          make_pdf)


@pytest.fixture
def umask():
    """Set the process umask for one test, restored afterwards."""
    old = os.umask(0o022)
    os.umask(old)
    yield lambda m: os.umask(m)
    os.umask(old)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# -- M1: artifacts follow the umask ----------------------------------------

@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o002, 0o664)])
def test_atomic_write_follows_the_umask(tmp_path, umask, mask, expected):
    umask(mask)
    p = tmp_path / "a.pdf"
    _atomic_write_bytes(p, b"data")
    assert _mode(p) == expected


@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o002, 0o664)])
def test_saved_pdf_follows_the_umask(tmp_path, umask, mask, expected):
    umask(mask)
    st = _storage(tmp_path, {U: (make_pdf(), "application/pdf")})
    rec = _rec(U)
    assert st.save(rec) == "saved"
    assert _mode(Path(rec.local_path)) == expected


@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o002, 0o664)])
def test_rendered_pdf_follows_the_umask(tmp_path, monkeypatch, umask, mask, expected):
    umask(mask)
    st, _ = _render_storage(tmp_path, monkeypatch,
                            lambda out: out.write_bytes(make_pdf("r")))
    rec = _rec(U, mime="text/html")
    assert st.save(rec) == "saved"
    assert rec.local_path.endswith(".pdf")
    assert _mode(Path(rec.local_path)) == expected
    assert _mode(Path(rec.html_path)) == expected


# -- S4: sniff the bytes before trusting an HTML mime header ---------------

@pytest.mark.parametrize("producer_mime", ["application/pdf", "text/html", ""])
@pytest.mark.parametrize("served", ["text/html", "text/html; charset=utf-8",
                                    "application/xhtml+xml"])
def test_pdf_bytes_served_as_html_are_stored_as_pdf(tmp_path, producer_mime, served):
    body = make_pdf("mislabelled")
    st = _storage(tmp_path, {U: (body, served)})
    rec = _rec(U, mime=producer_mime)
    assert st.save(rec) == "saved"
    assert rec.mime_type == "application/pdf"
    assert rec.local_path.endswith(".pdf") and rec.html_path is None
    assert Path(rec.local_path).read_bytes() == body


def test_html_bytes_served_as_html_still_rejected_for_a_pdf_producer(tmp_path):
    from cb_corpus.storage import ContentCheckFailed
    st = _storage(tmp_path, {U: (b"<html>wall</html>" * 100, "text/html")})
    with pytest.raises(ContentCheckFailed):
        st.save(_rec(U))


# -- S2: bad_content in the run report, reasons survive the 500-char cut --

def test_save_many_reports_bad_content_as_a_subcount_of_error(tmp_path):
    from cb_corpus.runreport import RunReport
    good = "https://bank.example/good.pdf"
    dead = "https://bank.example/dead.pdf"
    st = _storage(tmp_path, {U: (b"<html>wall</html>" * 100, "text/html"),
                             good: (make_pdf("good"), "application/pdf")})
    counts = st.save_many([_rec(U), _rec(good), _rec(dead)], label="xx:C1")
    assert counts == {"error": 2, "saved": 1, "bad-content": 1}
    report = RunReport("cb", "discover")
    s = report.source("xx")
    s.record_saved_counts(counts)
    assert (s.docs_seen, s.docs_new, s.docs_failed, s.bad_content) == (3, 1, 2, 1)
    d = report.to_dict()
    assert d["sources"][0]["bad_content"] == 1
    assert d["totals"]["bad_content"] == 1
    assert d["totals"]["docs_seen"] == 3


def test_save_many_without_rejection_has_no_bad_content_key(tmp_path):
    st = _storage(tmp_path, {U: (make_pdf(), "application/pdf")})
    assert st.save_many([_rec(U)]) == {"saved": 1}


def test_bad_content_does_not_change_the_exit_code():
    from cb_corpus.runreport import RunReport
    for counts, code in (({"saved": 1, "error": 1, "bad-content": 1}, 0),
                         ({"error": 1, "bad-content": 1}, 3)):
        r = RunReport("cb", "discover")
        r.source("xx").record_saved_counts(counts)
        assert r.finish() == code


def test_reasons_come_before_the_long_urls_in_the_error_record(tmp_path):
    import json
    long = "https://bank.example/" + "very-long-path-segment/" * 20
    urls = [long + "a.pdf", long + "b.pdf", long + "c.pdf"]
    st = _storage(tmp_path, {
        urls[0]: (b"<html>wall</html>" * 100, "text/html"),
        urls[1]: (b"%PDF-1.4\n" + b"x" * 5000, "application/pdf"),     # no %%EOF
        urls[2]: (b"%PDF-1.4 tiny", "application/pdf"),
    })
    st.save_many([_rec(urls[0], alt_urls=urls[1:])], label="xx:C1")
    entry = json.loads((tmp_path / "download_errors.jsonl").read_text())
    err = entry["error"]
    assert len(err) <= 500
    assert "the server sent HTML" in err
    assert "truncated PDF" in err
    assert "too small for a PDF" in err
