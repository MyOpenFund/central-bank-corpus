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
