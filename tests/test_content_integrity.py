"""Content integrity of stored bytes (issue #21, integrity wave 2026-11, A1-A3).

An HTTP 200 is not a document. Storage.save() checks each candidate URL's
bytes BEFORE the sha256 dedup, Storage.reindex() checks on-disk `.pdf`
files, artifacts are written atomically, and a Chrome render is checked
before it replaces the HTML as the artifact. No network: every fetcher is a
local fake."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from cb_corpus.config import Config
from cb_corpus.models import DocRecord
from cb_corpus.storage import ContentCheckFailed, Storage
from cb_corpus.taxonomy import DocType


def make_pdf(tag: str = "doc", size: int = 2000) -> bytes:
    """Bytes with a PDF's shape: magic, > 1 KB, %%EOF at the end."""
    return b"%PDF-1.7\n% " + tag.encode() + b"\n" + b"x" * size + b"\n%%EOF\n"


CONSENT_PAGE = (b"<!DOCTYPE html><html><head><title>Cookie consent</title></head>"
                b"<body>Please accept cookies to continue.</body></html>") * 20


class FakeFetcher:
    """url -> (bytes, mime) or an Exception to raise. Records every call."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    def get_bytes(self, url: str):
        self.calls.append(url)
        resp = self.responses.get(url)
        if resp is None:
            raise RuntimeError(f"404: {url}")
        if isinstance(resp, Exception):
            raise resp
        return resp

    def throttle(self, url: str) -> None:
        pass


def _storage(tmp_path, responses, **cfg_kw) -> Storage:
    cfg_kw.setdefault("html_to_pdf", False)
    return Storage(Config(data_dir=tmp_path, **cfg_kw), FakeFetcher(responses))


def _rec(url: str, *, mime: str = "application/pdf", alt_urls=None,
         title: str = "t") -> DocRecord:
    return DocRecord(bank_code="xx", doc_type=DocType.C1, title=title, pdf_url=url,
                     date=date(2025, 1, 2), mime_type=mime, date_precision="day",
                     alt_urls=list(alt_urls or []))


def _manifest_rows(tmp_path) -> list[dict]:
    f = tmp_path / "manifest" / "xx.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def _raw_files(tmp_path) -> list[Path]:
    raw = tmp_path / "raw"
    return sorted(p for p in raw.rglob("*") if p.is_file()) if raw.exists() else []


U = "https://bank.example/doc.pdf"
ALT = "https://mirror.example/doc.pdf"


# -- Config ---------------------------------------------------------------

def test_config_defaults():
    cfg = Config()
    assert cfg.min_pdf_bytes == 1024
    assert cfg.pdf_require_eof is True
    assert cfg.pdf_url_served_html == "reject"


@pytest.mark.parametrize("bad", [-1, 1.5, "1024", None, True])
def test_config_rejects_bad_min_pdf_bytes(bad):
    with pytest.raises(ValueError, match="min_pdf_bytes"):
        Config(min_pdf_bytes=bad)


def test_config_accepts_zero_min_pdf_bytes():
    assert Config(min_pdf_bytes=0).min_pdf_bytes == 0


@pytest.mark.parametrize("bad", ["Reject", "keep", "", None, True])
def test_config_rejects_unknown_pdf_url_served_html(bad):
    with pytest.raises(ValueError, match="pdf_url_served_html"):
        Config(pdf_url_served_html=bad)


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_config_rejects_non_bool_pdf_require_eof(bad):
    with pytest.raises(ValueError, match="pdf_require_eof"):
        Config(pdf_require_eof=bad)


# -- A1: Storage.save() ---------------------------------------------------

def test_consent_page_for_expected_pdf_is_rejected_and_fallback_wins(tmp_path):
    good = make_pdf("real")
    st = _storage(tmp_path, {U: (CONSENT_PAGE, "text/html"),
                             ALT: (good, "application/pdf")})
    rec = _rec(U, alt_urls=[ALT])
    assert st.save(rec) == "saved"
    assert st.fetcher.calls == [U, ALT]
    assert Path(rec.local_path).read_bytes() == good
    assert rec.mime_type == "application/pdf" and rec.html_path is None
    [row] = _manifest_rows(tmp_path)
    assert row["pdf_url"] == U and row["local_path"].endswith(".pdf")


def test_consent_page_only_candidate_raises_and_writes_nothing(tmp_path):
    st = _storage(tmp_path, {U: (CONSENT_PAGE, "text/html")})
    with pytest.raises(ContentCheckFailed) as ei:
        st.save(_rec(U))
    assert isinstance(ei.value, RuntimeError)       # the existing error path
    assert U in str(ei.value) and "HTML" in str(ei.value)
    assert _manifest_rows(tmp_path) == [] and _raw_files(tmp_path) == []
    assert st._hash_docid == {} and st._ids == set()


def test_truncated_pdf_is_rejected(tmp_path):
    truncated = make_pdf()[:-20]                     # cut before %%EOF
    st = _storage(tmp_path, {U: (truncated, "application/pdf")})
    with pytest.raises(ContentCheckFailed, match="EOF"):
        st.save(_rec(U))
    assert _raw_files(tmp_path) == []


def test_300_byte_pdf_is_rejected(tmp_path):
    stub = b"%PDF-1.4\n" + b"x" * 280 + b"\n%%EOF\n"
    assert len(stub) < 1024
    st = _storage(tmp_path, {U: (stub, "application/pdf")})
    with pytest.raises(ContentCheckFailed, match="too small"):
        st.save(_rec(U))


def test_relaxed_config_accepts_the_stub(tmp_path):
    stub = b"%PDF-1.4\n" + b"x" * 280             # small AND no %%EOF
    st = _storage(tmp_path, {U: (stub, "application/pdf")},
                  min_pdf_bytes=0, pdf_require_eof=False)
    assert st.save(_rec(U)) == "saved"


def test_valid_pdf_after_leading_whitespace_is_saved(tmp_path):
    body = b"\r\n\t  " + make_pdf()
    st = _storage(tmp_path, {U: (body, "application/pdf")})
    rec = _rec(U)
    assert st.save(rec) == "saved"
    assert Path(rec.local_path).read_bytes() == body


@pytest.mark.parametrize("producer_mime", ["application/pdf", "", "text/html"])
def test_server_pdf_mime_on_html_bytes_is_rejected(tmp_path, producer_mime):
    # The server says PDF, the bytes say HTML: whatever the producer expected,
    # these bytes would be stored as a .pdf with mime application/pdf.
    st = _storage(tmp_path, {U: (CONSENT_PAGE, "application/pdf")})
    with pytest.raises(ContentCheckFailed, match="not a PDF"):
        st.save(_rec(U, mime=producer_mime))


@pytest.mark.parametrize("server_mime", ["", "application/octet-stream", "binary/octet-stream"])
def test_expected_pdf_without_html_mime_is_checked(tmp_path, server_mime):
    st = _storage(tmp_path, {U: (CONSENT_PAGE, server_mime)})
    with pytest.raises(ContentCheckFailed):
        st.save(_rec(U))
    st2 = _storage(tmp_path / "ok", {U: (make_pdf(), server_mime)})
    assert st2.save(_rec(U)) == "saved"


def test_accept_mode_keeps_the_old_behaviour(tmp_path):
    st = _storage(tmp_path, {U: (CONSENT_PAGE, "text/html")},
                  pdf_url_served_html="accept")
    rec = _rec(U)
    assert st.save(rec) == "saved"
    assert rec.mime_type == "text/html" and rec.local_path.endswith(".html")
    assert Path(rec.local_path).read_bytes() == CONSENT_PAGE


@pytest.mark.parametrize("producer_mime", ["text/html", ""])
def test_html_producers_are_untouched(tmp_path, producer_mime):
    page = b"<html><body>Monetary policy account</body></html>"
    st = _storage(tmp_path, {U: (page, "text/html")})
    rec = _rec(U, mime=producer_mime)
    assert st.save(rec) == "saved"
    assert rec.mime_type == "text/html"


def test_error_page_never_becomes_a_dedup_owner(tmp_path):
    # N URLs serving one byte-identical error page: each one FAILS (download
    # error, quarantine), none is filed as skip:duplicate-content of another.
    urls = [f"https://bank.example/wp{i}.pdf" for i in range(3)]
    st = _storage(tmp_path, {u: (CONSENT_PAGE, "application/pdf") for u in urls})
    for u in urls:
        with pytest.raises(ContentCheckFailed):
            st.save(_rec(u))
    assert st._hash_docid == {} and st._pending_dups == []
    # The same URL serving its real PDF the next time saves normally.
    st.fetcher.responses[urls[0]] = (make_pdf(), "application/pdf")
    assert st.save(_rec(urls[0])) == "saved"


def test_fetch_failures_only_keep_the_plain_runtime_error(tmp_path):
    st = _storage(tmp_path, {})
    with pytest.raises(RuntimeError) as ei:
        st.save(_rec(U, alt_urls=[ALT]))
    assert not isinstance(ei.value, ContentCheckFailed)


def test_message_names_every_candidate(tmp_path):
    st = _storage(tmp_path, {U: (CONSENT_PAGE, "text/html")})   # ALT: fetch fails
    with pytest.raises(ContentCheckFailed) as ei:
        st.save(_rec(U, alt_urls=[ALT]))
    msg = str(ei.value)
    assert U in msg and ALT in msg


def test_save_many_counts_records_and_shows_bad_content(tmp_path, capsys):
    good = "https://bank.example/good.pdf"
    st = _storage(tmp_path, {U: (CONSENT_PAGE, "text/html"),
                             good: (make_pdf("good"), "application/pdf")})
    counts = st.save_many([_rec(U), _rec(good)], label="xx:C1")
    assert counts == {"error": 1, "saved": 1}
    err = capsys.readouterr().err
    assert "[xx:C1] bad-content: 1" in err
    [line] = (tmp_path / "download_errors.jsonl").read_text().splitlines()
    entry = json.loads(line)
    assert entry["label"] == "xx:C1" and entry["pdf_url"] == U
    assert entry["error"].startswith("ContentCheckFailed:")
    q = (tmp_path / "download_quarantine.jsonl").read_text()
    assert U in q                                    # fed like any failed download


def test_save_many_without_bad_content_prints_no_line(tmp_path, capsys):
    st = _storage(tmp_path, {U: (make_pdf(), "application/pdf")})
    st.save_many([_rec(U)], label="xx:C1")
    assert "bad-content" not in capsys.readouterr().err
