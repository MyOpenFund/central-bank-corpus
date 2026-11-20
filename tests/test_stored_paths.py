"""Manifest paths (`local_path`, `html_path`) are relative to the data root
(documents contract); readers still resolve the legacy `data/raw/...` form."""
from __future__ import annotations

from pathlib import Path

import pytest

from cb_corpus.config import Config


@pytest.mark.parametrize("data_dir", [Path("data"), Path("./data")])
def test_stored_path_relative_default_root(data_dir):
    cfg = Config(data_dir=data_dir)
    assert cfg.stored_path(cfg.raw_dir / "ecb" / "C1" / "2023" / "x.pdf") == "raw/ecb/C1/2023/x.pdf"


def test_stored_path_absolute_root(tmp_path):
    cfg = Config(data_dir=tmp_path / "d")
    assert cfg.stored_path(cfg.raw_dir / "jp" / "D1" / "0" / "y.html") == "raw/jp/D1/0/y.html"


def test_stored_path_through_a_differently_spelled_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=Path("data"))
    assert cfg.stored_path(tmp_path / "data" / "raw" / "a.pdf") == "raw/a.pdf"


def test_stored_path_outside_the_root_is_refused(tmp_path):
    cfg = Config(data_dir=tmp_path / "d")
    with pytest.raises(ValueError, match="not under the data root"):
        cfg.stored_path(tmp_path / "elsewhere" / "a.pdf")


def test_resolve_both_forms_and_absolute(tmp_path):
    cfg = Config(data_dir=tmp_path / "d")
    want = cfg.raw_dir / "ecb" / "C1" / "2023" / "x.pdf"
    assert cfg.resolve_stored_path("raw/ecb/C1/2023/x.pdf") == want
    assert cfg.resolve_stored_path("data/raw/ecb/C1/2023/x.pdf") == want      # legacy
    assert cfg.resolve_stored_path(str(want)) == want                         # absolute
    assert cfg.resolve_stored_path("") is None
    assert cfg.resolve_stored_path(None) is None


def test_round_trip(tmp_path):
    cfg = Config(data_dir=tmp_path)
    p = cfg.raw_dir / "us" / "A1" / "2024" / "z.pdf"
    assert cfg.resolve_stored_path(cfg.stored_path(p)) == p


# --- writers and readers ----------------------------------------------------

import json  # noqa: E402

from cb_corpus.models import DocRecord  # noqa: E402
from cb_corpus.storage import Storage, iter_manifest_rows  # noqa: E402
from cb_corpus.taxonomy import DocType  # noqa: E402
from tests.conftest import pdf_body, public_resolver  # noqa: E402

FORMS = ["contract", "legacy"]


def _stored(form: str, rel: str) -> str:
    return rel if form == "contract" else "data/" + rel


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    """CWD outside the data root: a reader that opens a stored path relative
    to the CWD (the pre-contract habit) finds nothing."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return Config(data_dir=tmp_path / "root")


class _Fetcher:
    def __init__(self, body, mime):
        self.body, self.mime = body, mime

    def get_bytes(self, url):
        return self.body, self.mime

    def throttle(self, url):
        pass


def test_save_stores_paths_relative_to_the_data_root(elsewhere):
    st = Storage(elsewhere, _Fetcher(pdf_body(b"%PDF-1.4 x"), "application/pdf"))
    rec = DocRecord(bank_code="ecb", doc_type=DocType.C1, title="t",
                    document_url="https://www.ecb.europa.eu/a.pdf")
    assert st.save(rec) == "saved"
    [row] = list(iter_manifest_rows(elsewhere))
    assert row["local_path"] == f"raw/ecb/C1/0/{rec.doc_id}.pdf"
    assert elsewhere.resolve_stored_path(row["local_path"]).is_file()


def test_saved_html_and_rendered_pdf_both_relative(elsewhere, monkeypatch):
    from cb_corpus import storage as storage_mod
    cfg = Config(data_dir=elsewhere.data_dir, html_to_pdf=True)
    st = Storage(cfg, _Fetcher(b"<html><body>page</body></html>" * 20, "text/html"))
    st.render_resolver = staticmethod(public_resolver)

    def fake_render(url, output, **kw):
        output.write_bytes(pdf_body(b"%PDF-1.4 r"))
    monkeypatch.setattr(storage_mod, "render_url_to_pdf", fake_render)
    rec = DocRecord(bank_code="ecb", doc_type=DocType.C1, title="t",
                    document_url="https://www.ecb.europa.eu/a.en.html")
    assert st.save(rec) == "saved"
    assert rec.local_path == f"raw/ecb/C1/0/{rec.doc_id}.pdf"
    assert rec.html_path == f"raw/ecb/C1/0/{rec.doc_id}.html"


def test_reindex_stores_paths_relative_to_the_data_root(elsewhere):
    st = Storage(elsewhere, _Fetcher(b"", ""))
    rec = DocRecord(bank_code="us", doc_type=DocType.A1, title="t",
                    document_url="https://www.federalreserve.gov/a.html")
    p = st.target_path(DocRecord(bank_code="us", doc_type=DocType.A1, title="t",
                                 document_url=rec.document_url, mime_type="text/html"))
    p.parent.mkdir(parents=True)
    p.write_bytes(b"<html>x</html>")
    assert st.reindex(rec, p) == "reindexed"
    assert rec.local_path == rec.html_path == f"raw/us/A1/0/{rec.doc_id}.html"


def _seed_html_row(cfg: Config, form: str, doc_id: str = "abc123") -> Path:
    rel = f"raw/ecb/A3/2024/{doc_id}.html"
    html = cfg.data_dir / rel
    html.parent.mkdir(parents=True, exist_ok=True)
    html.write_bytes(b"<html>seed</html>")
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    cfg.manifest_file("ecb").write_text(json.dumps({
        "corpus": "central-bank", "source_code": "ecb", "doc_type": "A3", "title": "x",
        "document_url": f"https://e.test/{doc_id}.en.html", "date": "2024-01-01",
        "year": 2024, "doc_id": doc_id, "mime_type": "text/html",
        "local_path": _stored(form, rel), "sha256": None, "language": "en",
    }) + "\n")
    return html


def test_convert_html_reads_and_writes_the_contract_form(elsewhere, monkeypatch):
    from cb_corpus import convert as convert_mod
    html = _seed_html_row(elsewhere, "contract")
    monkeypatch.setattr(convert_mod, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(convert_mod, "render_url_to_pdf",
                        lambda url, output, **kw: output.write_bytes(pdf_body(b"%PDF-1.4 ok")))
    assert convert_mod.convert_existing(elsewhere, resolver=public_resolver) == {
        "converted": 1, "refused": 0}
    assert html.with_suffix(".pdf").is_file()
    [row] = list(iter_manifest_rows(elsewhere))
    assert row["local_path"] == "raw/ecb/A3/2024/abc123.pdf"
    assert row["html_path"] == "raw/ecb/A3/2024/abc123.html"


def test_retry_html_reads_and_writes_the_contract_form(elsewhere, monkeypatch):
    from cb_corpus import retry_html as retry_mod
    html = _seed_html_row(elsewhere, "contract")
    monkeypatch.setattr(retry_mod, "find_chrome", lambda: "/fake/chrome")

    def fake_try(chrome, url, output, **kw):
        output.write_bytes(pdf_body(b"%PDF-1.4 ok"))
        return True
    monkeypatch.setattr(retry_mod, "_try_strategy", fake_try)
    counts = retry_mod.retry_failed(elsewhere, parallelism=1, resolver=public_resolver)
    assert counts.get("recovered") == 1
    assert html.with_suffix(".pdf").is_file()
    [row] = list(iter_manifest_rows(elsewhere))
    assert row["local_path"] == "raw/ecb/A3/2024/abc123.pdf"
    assert row["html_path"] == "raw/ecb/A3/2024/abc123.html"


@pytest.mark.parametrize("cmd", ["convert-html", "retry-html"])
def test_a_legacy_path_stops_the_render_commands_before_any_render(elsewhere, monkeypatch, cmd):
    """A row still storing `data/raw/...` (pre-contract) is refused at read
    time, naming migrate-contract: nothing rendered, the manifest untouched."""
    from cb_corpus import convert as convert_mod
    from cb_corpus import retry_html as retry_mod
    from cb_corpus.models import ContractRowError
    html = _seed_html_row(elsewhere, "legacy")
    before = elsewhere.manifest_file("ecb").read_bytes()
    rendered = []
    monkeypatch.setattr(convert_mod, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(retry_mod, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(convert_mod, "render_url_to_pdf", lambda *a, **kw: rendered.append(a))
    monkeypatch.setattr(retry_mod, "_try_strategy", lambda *a, **kw: rendered.append(a))
    with pytest.raises(ContractRowError, match="migrate-contract"):
        if cmd == "convert-html":
            convert_mod.convert_existing(elsewhere, resolver=public_resolver)
        else:
            retry_mod.retry_failed(elsewhere, parallelism=1, resolver=public_resolver)
    assert rendered == [] and not html.with_suffix(".pdf").exists()
    assert elsewhere.manifest_file("ecb").read_bytes() == before


@pytest.mark.parametrize("form", FORMS)
def test_wp_dates_reads_the_pdf_through_the_data_root(elsewhere, form):
    from datetime import date
    from cb_corpus.wp_dates import recover
    rel = "raw/us/D1/2002/x.pdf"
    p = elsewhere.data_dir / rel
    p.parent.mkdir(parents=True)
    p.write_bytes(b"%PDF-1.4 /CreationDate (D:20020115120000) x")
    row = {"date": "2002-01-01", "document_url": "https://x/200201pap.pdf",
           "local_path": _stored(form, rel)}
    r = recover(object(), row, use_wayback=False, cfg=elsewhere)
    assert r["date"] == date(2002, 1, 15).isoformat() and r["date_source"] == "pdf_meta"


@pytest.mark.parametrize("form", FORMS)
def test_orphan_resolver_reads_either_form(elsewhere, form):
    from cb_corpus.orphans import _resolve_local_path
    assert _resolve_local_path(elsewhere, _stored(form, "raw/us/C1/2015/a.pdf")) == \
        elsewhere.raw_dir / "us" / "C1" / "2015" / "a.pdf"


@pytest.mark.parametrize("form", FORMS)
def test_wp_dates_evidence_keeps_the_committed_index_form(elsewhere, form):
    """The committed wp_dates_index.jsonl spells PDF evidence
    `file://data/raw/...` (1,309 entries on 2026-11-20, the path as seen from
    the app directory). It is evidence text, never read back as a path, so a
    new entry keeps that one spelling whichever form the row stores."""
    from cb_corpus.wp_dates import recover
    rel = "raw/us/D1/2002/x.pdf"
    p = elsewhere.data_dir / rel
    p.parent.mkdir(parents=True)
    p.write_bytes(b"%PDF-1.4 /CreationDate (D:20020115120000) x")
    row = {"date": "2002-01-01", "document_url": "https://x/200201pap.pdf",
           "local_path": _stored(form, rel)}
    r = recover(object(), row, use_wayback=False, cfg=elsewhere)
    assert r["evidence_url"] == "file://data/raw/us/D1/2002/x.pdf"
