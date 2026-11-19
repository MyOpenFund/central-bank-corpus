"""The 2026-11-19 field rename `pdf_url` -> `document_url`: no doc_id moves,
the transition fallback (models.row_url / models.upgrade_row) reads rows
written under the old name, and every rewrite path writes the new name only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cb_corpus.config import Config
from cb_corpus.models import DocRecord, compute_doc_id, row_url, upgrade_row
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
            assert compute_doc_id(row["bank_code"], row["doc_type"], row_url(row)) == row["doc_id"]
            n += 1
    assert n > 0


def test_to_row_keeps_the_field_position():
    row = DocRecord(bank_code="xx", doc_type=DocType.D1, title="t",
                    document_url="https://x/a.pdf").to_row()
    assert list(row)[:5] == ["bank_code", "doc_type", "title", "document_url", "source_url"]


# -- row_url / upgrade_row ---------------------------------------------------

def test_row_url_reads_new_then_old_key():
    assert row_url({"document_url": "n"}) == "n"
    assert row_url({"pdf_url": "o"}) == "o"
    assert row_url({"document_url": "n", "pdf_url": "o"}) == "n"
    assert row_url({"document_url": None, "pdf_url": "o"}) is None   # new key present wins
    assert row_url({}) is None
    assert row_url({"dead_pdf_url": "d"}, "dead_document_url") == "d"
    assert row_url({"dead_document_url": "n", "dead_pdf_url": "d"}, "dead_document_url") == "n"


def test_upgrade_row_renames_in_place_keeping_position_and_values():
    row = {"bank_code": "xx", "title": "t", "pdf_url": "u", "source_url": "s",
           "retired_duplicates": [{"doc_id": "r1", "pdf_url": "ru", "at": "x"}, "junk"]}
    same = upgrade_row(row)
    assert same is row
    assert list(row) == ["bank_code", "title", "document_url", "source_url", "retired_duplicates"]
    assert row["document_url"] == "u"
    assert list(row["retired_duplicates"][0]) == ["doc_id", "document_url", "at"]
    assert row["retired_duplicates"][1] == "junk"


def test_upgrade_row_leaves_both_names_alone():
    row = {"pdf_url": "o", "document_url": "n"}
    assert upgrade_row(row) == {"pdf_url": "o", "document_url": "n"}
    assert upgrade_row({"document_url": "n"}) == {"document_url": "n"}


# -- storage: read under the old name, rewrite under the new one -------------

def _legacy_row(bank: str, i: int, **extra) -> dict:
    url = f"https://example.org/{bank}/{i}.pdf"
    return {"bank_code": bank, "doc_type": "D1", "title": f"t{i}", "pdf_url": url,
            "source_url": f"https://example.org/{bank}/{i}.html", "date": None,
            "doc_id": compute_doc_id(bank, "D1", url), "year": None, **extra}


def _write_raw(cfg: Config, bank: str, rows: list[dict], tail: str = "") -> Path:
    path = cfg.manifest_file(bank)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows) + tail,
                    encoding="utf-8")
    return path


def _disk_rows(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_legacy_rows_are_read_under_the_new_name(tmp_path):
    cfg = Config(data_dir=tmp_path)
    _write_raw(cfg, "xx", [_legacy_row("xx", 1)])
    rows = list(iter_manifest_rows(cfg))
    assert "pdf_url" not in rows[0] and rows[0]["document_url"] == "https://example.org/xx/1.pdf"
    st = Storage(cfg)
    assert st.is_known_url("https://example.org/xx/1.pdf")


def test_apply_row_updates_from_a_legacy_snapshot_writes_the_new_name_only(tmp_path):
    cfg = Config(data_dir=tmp_path)
    path = _write_raw(cfg, "xx", [_legacy_row("xx", 1), _legacy_row("xx", 2)])
    snap = list(iter_manifest_rows(cfg))
    upd = dict(snap[0]); upd["title"] = "new"
    # a caller handing a raw legacy dict is upgraded too
    raw = _legacy_row("xx", 2, title="raw")
    assert apply_row_updates(cfg, {upd["doc_id"]: upd, raw["doc_id"]: raw}) == 2
    on_disk = _disk_rows(path)
    for row in on_disk:
        assert "pdf_url" not in row and list(row)[3] == "document_url"
    assert [r["title"] for r in on_disk] == ["new", "raw"]


def test_merge_rows_locked_rewrites_a_changed_legacy_row_under_the_new_name(tmp_path):
    cfg = Config(data_dir=tmp_path)
    r1, r2 = _legacy_row("xx", 1), _legacy_row("xx", 2)
    path = _write_raw(cfg, "xx", [r1, r2])
    untouched_line = path.read_text(encoding="utf-8").splitlines()[1]

    def merge(row):
        assert row_url(row) == r1["pdf_url"] and "pdf_url" not in row
        row["title"] = "merged"
        return True

    assert merge_rows_locked(cfg, {"xx": {r1["doc_id"]: merge}}) == 1
    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert "pdf_url" not in first and first["document_url"] == r1["pdf_url"]
    assert lines[1] == untouched_line          # an unchanged row stays verbatim


def test_recover_inventory_reads_legacy_download_errors(tmp_path):
    from cb_corpus.recover import _read_inventory
    cfg = Config(data_dir=tmp_path)
    p = tmp_path / "download_errors.jsonl"
    p.write_text(json.dumps({"bank_code": "fr", "pdf_url": "https://x/old.pdf"}) + "\n"
                 + json.dumps({"bank_code": "fr", "document_url": "https://x/new.pdf"}) + "\n",
                 encoding="utf-8")
    assert sorted(row_url(e) for e in _read_inventory(cfg)) == [
        "https://x/new.pdf", "https://x/old.pdf"]


def test_download_error_log_is_written_under_the_new_name(tmp_path):
    st = Storage(Config(data_dir=tmp_path))
    rec = DocRecord(bank_code="xx", doc_type=DocType.D1, title="t",
                    document_url="https://x/a.pdf")
    st._record_download_error(rec, RuntimeError("boom"), "xx:D1")
    entry = json.loads((tmp_path / "download_errors.jsonl").read_text().splitlines()[0])
    assert entry["document_url"] == "https://x/a.pdf" and "pdf_url" not in entry
    assert (tmp_path / ".download_errors.jsonl.lock").exists()   # appended under the lock
