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
    return DocRecord(bank_code="xx", doc_type=DocType.C1, title=title, document_url=url,
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
    assert row["document_url"] == U and row["local_path"].endswith(".pdf")


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
    # "bad-content" is a sub-count of "error", not a status of its own.
    assert counts == {"error": 1, "saved": 1, "bad-content": 1}
    err = capsys.readouterr().err
    assert "[xx:C1] bad-content: 1" in err
    [line] = (tmp_path / "download_errors.jsonl").read_text().splitlines()
    entry = json.loads(line)
    assert entry["label"] == "xx:C1" and entry["document_url"] == U
    assert entry["error"].startswith("ContentCheckFailed:")
    q = (tmp_path / "download_quarantine.jsonl").read_text()
    assert U in q                                    # fed like any failed download


def test_save_many_without_bad_content_prints_no_line(tmp_path, capsys):
    st = _storage(tmp_path, {U: (make_pdf(), "application/pdf")})
    st.save_many([_rec(U)], label="xx:C1")
    assert "bad-content" not in capsys.readouterr().err


# -- A3: atomic artifact writes and the Chrome render ------------------------

from cb_corpus import storage as storage_mod                     # noqa: E402
from cb_corpus.storage import _atomic_write_bytes                 # noqa: E402
from tests.conftest import public_resolver                        # noqa: E402


def test_atomic_write_writes_and_replaces(tmp_path):
    p = tmp_path / "d" / "a.pdf"
    p.parent.mkdir()
    _atomic_write_bytes(p, b"one")
    _atomic_write_bytes(p, b"two")
    assert p.read_bytes() == b"two"
    assert sorted(x.name for x in p.parent.iterdir()) == ["a.pdf"]


def test_atomic_write_killed_midway_leaves_no_final_file(tmp_path, monkeypatch):
    p = tmp_path / "a.pdf"

    def dying_fsync(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(storage_mod.os, "fsync", dying_fsync)
    with pytest.raises(OSError):
        _atomic_write_bytes(p, make_pdf())
    assert list(tmp_path.iterdir()) == []            # no final file, no temp


def test_atomic_write_failure_keeps_the_previous_file(tmp_path, monkeypatch):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"previous")
    monkeypatch.setattr(storage_mod.os, "replace",
                        lambda *a: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(OSError):
        _atomic_write_bytes(p, b"new")
    assert p.read_bytes() == b"previous"
    assert [x.name for x in tmp_path.iterdir()] == ["a.pdf"]


def test_save_killed_mid_write_leaves_no_file_and_no_row(tmp_path, monkeypatch):
    st = _storage(tmp_path, {U: (make_pdf(), "application/pdf")})
    rec = _rec(U)
    target = st.target_path(rec)

    def dying_fsync(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(storage_mod.os, "fsync", dying_fsync)
    with pytest.raises(OSError):
        st.save(rec)
    assert not target.exists()
    assert _raw_files(tmp_path) == [] and _manifest_rows(tmp_path) == []
    monkeypatch.undo()
    assert st.save(_rec(U)) == "saved"               # retried cleanly next time


def test_html_artifact_written_atomically(tmp_path, monkeypatch):
    page = b"<html><body>account</body></html>"
    st = _storage(tmp_path, {U: (page, "text/html")})
    rec = _rec(U, mime="text/html")
    html_target = st.target_path(rec)

    def dying_fsync(fd):
        raise OSError("killed")
    monkeypatch.setattr(storage_mod.os, "fsync", dying_fsync)
    with pytest.raises(OSError):
        st.save(rec)
    assert not html_target.exists() and _raw_files(tmp_path) == []


def _render_storage(tmp_path, monkeypatch, write_output):
    """A Storage that fetches an HTML page and 'renders' it with a fake
    Chrome writing `write_output(output_path)`. Returns (storage, calls)."""
    page = b"<html><body>Monetary policy account</body></html>"
    st = _storage(tmp_path, {U: (page, "text/html")}, html_to_pdf=True)
    st.render_resolver = staticmethod(public_resolver)
    calls: list[Path] = []

    def fake_render(url, output, **kw):
        calls.append(output)
        write_output(output)
    monkeypatch.setattr(storage_mod, "render_url_to_pdf", fake_render)
    return st, calls


def test_chrome_renders_to_a_temp_path_then_moves_into_place(tmp_path, monkeypatch):
    rendered = make_pdf("rendered")
    st, calls = _render_storage(tmp_path, monkeypatch,
                                lambda out: out.write_bytes(rendered))
    rec = _rec(U, mime="text/html")
    assert st.save(rec) == "saved"
    final = Path(rec.local_path)
    assert rec.mime_type == "application/pdf" and final.suffix == ".pdf"
    assert final.read_bytes() == rendered
    [tmp_out] = calls
    assert tmp_out != final and tmp_out.parent == final.parent
    assert not tmp_out.exists()
    assert sorted(p.name for p in final.parent.iterdir()) == sorted(
        [final.name, Path(rec.html_path).name])


@pytest.mark.parametrize("output", [
    b"<html><body>Chrome error page</body></html>" * 50,    # no %PDF
    b"%PDF-1.4\n" + b"x" * 100,                              # stub
    b"%PDF-1.4\n" + b"x" * 5000,                             # cut off, no %%EOF
])
def test_bad_chrome_output_keeps_the_html_as_artifact(tmp_path, monkeypatch, output, capsys):
    st, calls = _render_storage(tmp_path, monkeypatch, lambda out: out.write_bytes(output))
    rec = _rec(U, mime="text/html")
    assert st.save(rec) == "saved"
    assert rec.mime_type == "text/html" and rec.local_path == rec.html_path
    assert rec.local_path.endswith(".html")
    d = Path(rec.local_path).parent
    assert [p.name for p in d.iterdir()] == [Path(rec.local_path).name]  # no .pdf, no temp
    assert "render output rejected" in capsys.readouterr().err


def test_chrome_failure_removes_its_temp_output(tmp_path, monkeypatch):
    def half_written_then_crash(out):
        out.write_bytes(b"%PDF-1.4 partial")
        raise RuntimeError("Chrome timed out")
    st, _ = _render_storage(tmp_path, monkeypatch, half_written_then_crash)
    rec = _rec(U, mime="text/html")
    assert st.save(rec) == "saved"
    assert rec.local_path.endswith(".html")
    d = Path(rec.local_path).parent
    assert [p.name for p in d.iterdir()] == [Path(rec.local_path).name]


# -- A2: Storage.reindex() ------------------------------------------------

def _on_disk(st: Storage, rec: DocRecord, data: bytes, ext: str = "pdf") -> Path:
    path = st.target_path(rec).with_suffix("." + ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("data", [
    make_pdf()[:-20],                    # truncated: killed process / ENOSPC
    b"%PDF-1.4\n" + b"x" * 200 + b"\n%%EOF\n",   # a 300-byte stub
    CONSENT_PAGE,                        # an error page saved as .pdf
    b"",                                 # empty file
])
def test_reindex_bad_pdf_is_skipped_never_indexed(tmp_path, data, capsys):
    st = _storage(tmp_path, {})
    st.quarantine.record_failure(U, night="2026-11-01")
    rec = _rec(U)
    path = _on_disk(st, rec, data)
    assert st.reindex(rec, path) == "skip:bad-content"
    assert st._ids == set() and st._hash_docid == {} and st._pending_dups == []
    assert _manifest_rows(tmp_path) == []
    assert path.exists()                              # evidence left in place
    assert U in st.quarantine._state                  # not released
    err = capsys.readouterr().err
    assert len([l for l in err.splitlines() if "bad-content" in l]) == 1
    assert str(path) in err


def test_reindex_bad_pdf_reported_in_dry_run_too(tmp_path):
    st = _storage(tmp_path, {})
    rec = _rec(U)
    path = _on_disk(st, rec, make_pdf()[:-20])
    assert st.reindex(rec, path, dry_run=True) == "skip:bad-content"
    good = _rec("https://bank.example/good.pdf")
    gpath = _on_disk(st, good, make_pdf("good"))
    assert st.reindex(good, gpath, dry_run=True) == "dry-run:would-reindex"


def test_reindex_bad_file_never_becomes_a_dedup_owner(tmp_path):
    st = _storage(tmp_path, {})
    a, b = _rec("https://bank.example/a.pdf"), _rec("https://bank.example/b.pdf")
    pa, pb = _on_disk(st, a, CONSENT_PAGE), _on_disk(st, b, CONSENT_PAGE)
    assert st.reindex(a, pa) == "skip:bad-content"
    assert st.reindex(b, pb) == "skip:bad-content"     # not duplicate-content:a


def test_reindex_valid_pdf_and_html_still_indexed(tmp_path):
    st = _storage(tmp_path, {})
    rec = _rec(U)
    assert st.reindex(rec, _on_disk(st, rec, b"\n " + make_pdf())) == "reindexed"
    h = _rec("https://bank.example/page.html", mime="text/html")
    # An HTML artifact is not a PDF: no PDF check on a .html path.
    assert st.reindex(h, _on_disk(st, h, b"<html>tiny</html>", ext="html")) == "reindexed"


def test_reindex_honours_relaxed_config(tmp_path):
    st = _storage(tmp_path, {}, min_pdf_bytes=0, pdf_require_eof=False)
    rec = _rec(U)
    assert st.reindex(rec, _on_disk(st, rec, b"%PDF-1.4 stub")) == "reindexed"


def test_reindex_bis_from_disk_tallies_bad_content(tmp_path, monkeypatch,
                                                   fetcher_factory, capsys):
    from cb_corpus import pipeline
    from tests.test_reindex_from_disk import SPEECH_URL, _doc_id, _wire_sitemaps
    path = tmp_path / "raw" / "de" / "C1" / "2020" / f"{_doc_id('de', SPEECH_URL)}.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(make_pdf()[:-20])                  # truncated on disk
    _wire_sitemaps(monkeypatch, fetcher_factory)
    counts = pipeline.reindex_bis_from_disk(dry_run=False, config=Config(data_dir=tmp_path))
    assert counts["matched"] == 1 and counts["reindexed"] == 0
    assert counts["bad-content"] == 1 and counts["skip"] == 0
    assert "bad_content=1" in capsys.readouterr().err
    assert not (tmp_path / "manifest" / "de.jsonl").exists()


def test_apply_reindex_tallies_bad_content(tmp_path):
    from cb_corpus.pipeline import _apply_reindex
    st = _storage(tmp_path, {})
    bad, good = _rec("https://bank.example/bad.pdf"), _rec("https://bank.example/good.pdf")
    missing = {bad.doc_id: _on_disk(st, bad, CONSENT_PAGE),
               good.doc_id: _on_disk(st, good, make_pdf("good"))}
    counts = _apply_reindex([bad, good], st, missing, dry_run=False)
    assert counts == {"matched": 2, "reindexed": 1, "skip": 0, "bad-content": 1}


@pytest.mark.parametrize("download", [True, False])
def test_recover_candidate_truncated_is_bad_file_and_never_copied(tmp_path, monkeypatch,
                                                                  download):
    # Passes _verify_local_pdf (magic, > 20 KB) but has no %%EOF: reindex
    # would refuse it, so recover reports bad-file before copying into raw/.
    from cb_corpus.recover import run_recover_downloads
    from tests.test_recover_candidates import (_NullFetcher, _csv_rows, _entry,
                                               _stub_refresh_metadata,
                                               _write_candidates, _write_inventory)
    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/cut.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    cut = tmp_path / "cut.pdf"
    cut.write_bytes(b"%PDF-1.4 " + b"x" * (25 * 1024))
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(cut),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/x",
    }])
    _stub_refresh_metadata(monkeypatch)
    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=download,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["bad-file"] == 1 and results["fr"]["recoverable"] == 0
    assert _csv_rows(tmp_path / "r.csv")[0]["action"] == "bad-file"
    assert not (tmp_path / "manifest" / "fr.jsonl").exists()
    raw = tmp_path / "raw"
    assert not raw.exists() or not [p for p in raw.rglob("*") if p.is_file()]
