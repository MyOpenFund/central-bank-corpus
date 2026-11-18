"""pdfcheck: the one shape check for bytes that claim to be a PDF (issue #21).

Used by Storage.save (each candidate URL), Storage.reindex (on-disk .pdf),
the Chrome render output, and recover._verify_local_pdf (magic part)."""
from __future__ import annotations

from cb_corpus.pdfcheck import EOF_WINDOW, MAGIC_WINDOW, check_pdf_bytes, has_pdf_magic


def _pdf(body_len: int = 2000, *, lead: bytes = b"", eof: bool = True) -> bytes:
    data = lead + b"%PDF-1.7\n" + b"x" * body_len
    return data + b"\n%%EOF\n" if eof else data


def test_valid_pdf_passes():
    assert check_pdf_bytes(_pdf(), min_bytes=1024, require_eof=True) is None


def test_html_consent_page_is_not_a_pdf():
    html = b"<!DOCTYPE html><html><body>Accept cookies</body></html>" * 40
    reason = check_pdf_bytes(html, min_bytes=1024, require_eof=True)
    assert reason and "%PDF" in reason


def test_magic_after_leading_whitespace_within_window_passes():
    # PDF readers tolerate junk before the header (Acrobat: first 1024 bytes).
    assert check_pdf_bytes(_pdf(lead=b"\r\n \t\n"), min_bytes=1024, require_eof=True) is None


def test_magic_past_window_fails():
    data = _pdf(lead=b" " * MAGIC_WINDOW)
    assert check_pdf_bytes(data, min_bytes=0, require_eof=False) is not None


def test_magic_straddling_window_end_fails():
    # "%PDF" must START within the window AND be complete inside it.
    data = b" " * (MAGIC_WINDOW - 2) + b"%PDF" + b"x" * 3000 + b"%%EOF"
    assert check_pdf_bytes(data, min_bytes=0, require_eof=True) is not None


def test_tiny_pdf_fails_size_floor():
    data = b"%PDF-1.4\n" + b"x" * 200 + b"\n%%EOF"      # ~ 300 bytes
    reason = check_pdf_bytes(data, min_bytes=1024, require_eof=True)
    assert reason and "bytes" in reason
    assert check_pdf_bytes(data, min_bytes=0, require_eof=True) is None


def test_truncated_pdf_fails_eof_check_unless_disabled():
    data = _pdf(eof=False)
    reason = check_pdf_bytes(data, min_bytes=1024, require_eof=True)
    assert reason and "%%EOF" in reason
    assert check_pdf_bytes(data, min_bytes=1024, require_eof=False) is None


def test_eof_must_be_in_the_tail_window():
    # An early %%EOF (incremental update section) followed by a truncated
    # tail longer than the window: still truncated.
    data = b"%PDF-1.7\n" + b"x" * 100 + b"%%EOF\n" + b"y" * (EOF_WINDOW + 10)
    assert check_pdf_bytes(data, min_bytes=0, require_eof=True) is not None


def test_eof_followed_by_trailing_bytes_inside_window_passes():
    data = _pdf() + b"\x00" * 500
    assert check_pdf_bytes(data, min_bytes=0, require_eof=True) is None


def test_empty_bytes_fail():
    assert check_pdf_bytes(b"", min_bytes=0, require_eof=False) is not None


def test_has_pdf_magic():
    assert has_pdf_magic(b"%PDF-1.4")
    assert has_pdf_magic(b"\n\n%PDF-1.4")
    assert not has_pdf_magic(b"<html>")
    assert not has_pdf_magic(b"")


# -- recover._verify_local_pdf shares the magic rule ------------------------

def test_verify_local_pdf_accepts_magic_after_leading_whitespace(tmp_path):
    from cb_corpus.recover import _verify_local_pdf
    p = tmp_path / "lead.pdf"
    p.write_bytes(b"\r\n  " + b"%PDF-1.4 " + b"x" * (25 * 1024))
    assert _verify_local_pdf(p)


def test_verify_local_pdf_still_rejects_html_and_keeps_its_20k_floor(tmp_path):
    from cb_corpus.recover import _verify_local_pdf
    html = tmp_path / "page.pdf"
    html.write_bytes(b"<html>" + b"x" * (25 * 1024))
    small = tmp_path / "small.pdf"
    small.write_bytes(b"%PDF-1.4 " + b"x" * 5000 + b"%%EOF")   # passes pdfcheck, < 20 KB
    assert not _verify_local_pdf(html)
    assert not _verify_local_pdf(small)
