"""The 2026-11-19 field rename `pdf_url` -> `document_url`: no doc_id moves,
every writer writes the new name, and since the transition cleanup
(2026-11-20) a row, download-error entry or recover candidate still under the
old name is refused loudly, naming its one-shot fix."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cb_corpus.config import Config
from cb_corpus.models import ContractRowError, DocRecord, compute_doc_id
from cb_corpus.storage import (Storage, apply_row_updates, iter_manifest_rows,
                               merge_rows_locked)
from cb_corpus.taxonomy import DocType

REPO = Path(__file__).resolve().parent.parent

# doc_ids computed on master caeac6d (before the rename) -- the first two are
# the committed rows ae a5b8ba5e477ad19a and ch e908e3c8f3da77b1.
PINNED = [
    ("ae", DocType.C1, "https://www.bis.org/review/r100205e.pdf", "a5b8ba5e477ad19a"),
    ("ch", DocType.D1,
     "https://www.snb.ch/en/publications/research/working-papers/2026/working_paper_2026_14",
     "e908e3c8f3da77b1"),
    ("ecb", DocType.A1,
     "https://www.ecb.europa.eu/press/pr/date/2024/html/ecb.mp240606~2148ecdb3c.en.html",
     "569958328dad5f26"),
]


@pytest.mark.parametrize("bank,dt,url,expected", PINNED)
def test_doc_id_unchanged_by_the_rename(bank, dt, url, expected):
    rec = DocRecord(bank_code=bank, doc_type=dt, title="t", document_url=url)
    assert rec.doc_id == expected
    assert compute_doc_id(bank, dt.code, url) == expected
    row = rec.to_row()
    assert row["doc_id"] == expected
    assert row["document_url"] == url and "pdf_url" not in row


def test_committed_manifest_doc_ids_hash_the_url_value():
    files = sorted((REPO / "data" / "manifest").glob("*.jsonl"))
    if not files:
        pytest.skip("no committed manifest in this checkout")
    n = 0
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            assert compute_doc_id(row["source_code"], row["doc_type"],
                                  row["document_url"]) == row["doc_id"]
            n += 1
    assert n > 0


def test_to_row_keeps_the_field_position():
    row = DocRecord(bank_code="xx", doc_type=DocType.D1, title="t",
                    document_url="https://x/a.pdf").to_row()
    # documents contract (2026-11-20): corpus + source_code (was bank_code) first
    assert list(row)[:6] == ["corpus", "source_code", "doc_type", "title", "document_url",
                             "source_url"]


# -- an old name is refused, never read as a missing URL ---------------------

def _legacy_row(bank: str, i: int, **extra) -> dict:
    url = f"https://example.org/{bank}/{i}.pdf"
    return {"corpus": "central-bank", "source_code": bank, "doc_type": "D1", "title": f"t{i}",
            "pdf_url": url, "source_url": f"https://example.org/{bank}/{i}.html", "date": None,
            "doc_id": compute_doc_id(bank, "D1", url), "year": None, **extra}


def _write_raw(cfg: Config, bank: str, rows: list[dict], tail: str = "") -> Path:
    path = cfg.manifest_file(bank)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows) + tail,
                    encoding="utf-8")
    return path


def test_a_manifest_row_under_the_old_name_is_refused(tmp_path):
    cfg = Config(data_dir=tmp_path)
    _write_raw(cfg, "xx", [_legacy_row("xx", 1)])
    with pytest.raises(ContractRowError, match="pdf_url.*migrate-contract"):
        list(iter_manifest_rows(cfg))
    with pytest.raises(ContractRowError, match="pdf_url"):
        Storage(cfg)


def test_writers_refuse_a_row_under_the_old_name(tmp_path):
    cfg = Config(data_dir=tmp_path)
    good = _legacy_row("xx", 1)
    good["document_url"] = good.pop("pdf_url")
    path = _write_raw(cfg, "xx", [good])
    before = path.read_bytes()
    with pytest.raises(ContractRowError, match="pdf_url"):
        apply_row_updates(cfg, {good["doc_id"]: _legacy_row("xx", 1, title="raw")})
    dup = dict(good, retired_duplicates=[{"doc_id": "r", "pdf_url": "u"}])
    with pytest.raises(ContractRowError, match="pdf_url"):
        apply_row_updates(cfg, {good["doc_id"]: dup})
    assert path.read_bytes() == before


def test_merge_on_a_row_under_the_old_name_is_refused(tmp_path):
    cfg = Config(data_dir=tmp_path)
    r1 = _legacy_row("xx", 1)
    path = _write_raw(cfg, "xx", [r1])
    before = path.read_bytes()
    with pytest.raises(ContractRowError, match="pdf_url"):
        merge_rows_locked(cfg, {"xx": {r1["doc_id"]: lambda row: True}})
    assert path.read_bytes() == before


def test_recover_inventory_refuses_an_entry_under_the_old_name(tmp_path):
    from cb_corpus.recover import _read_inventory
    cfg = Config(data_dir=tmp_path)
    p = tmp_path / "download_errors.jsonl"
    p.write_text(json.dumps({"bank_code": "fr", "document_url": "https://x/new.pdf"}) + "\n"
                 + json.dumps({"bank_code": "fr", "pdf_url": "https://x/old.pdf"}) + "\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match=r"download_errors.jsonl:2:.*migrate-document-url"):
        _read_inventory(cfg)
    p.write_text(json.dumps({"bank_code": "fr", "document_url": "https://x/new.pdf"}) + "\n",
                 encoding="utf-8")
    assert [e["document_url"] for e in _read_inventory(cfg)] == ["https://x/new.pdf"]


def test_recover_candidates_refuse_the_old_key(tmp_path):
    from cb_corpus.recover import _read_candidates
    p = tmp_path / "cands.jsonl"
    p.write_text(json.dumps({"dead_document_url": "https://x/a.pdf", "file_path": "a"}) + "\n"
                 + json.dumps({"dead_pdf_url": "https://x/b.pdf", "file_path": "b"}) + "\n")
    with pytest.raises(ValueError, match=r"cands.jsonl:2: rename the key `dead_pdf_url`"):
        _read_candidates(str(p))


def test_download_error_log_is_written_under_the_new_name(tmp_path):
    st = Storage(Config(data_dir=tmp_path))
    rec = DocRecord(bank_code="xx", doc_type=DocType.D1, title="t",
                    document_url="https://x/a.pdf")
    st._record_download_error(rec, RuntimeError("boom"), "xx:D1")
    entry = json.loads((tmp_path / "download_errors.jsonl").read_text().splitlines()[0])
    assert entry["document_url"] == "https://x/a.pdf" and "pdf_url" not in entry
    assert (tmp_path / ".download_errors.jsonl.lock").exists()   # appended under the lock
