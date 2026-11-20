"""Manifest rows follow the `documents` contract (vault-contract 1.0.0):
`corpus: central-bank`, `source_code` (was `bank_code`), paths relative to the
data root. Rows written before it are read in the contract form (transition,
models.upgrade_row) and rewritten in it by every rewrite path."""
from __future__ import annotations

import copy
import json
from datetime import date

import pytest
from vault_contract import check_row, golden, load_vocabulary

from cb_corpus.config import Config
from cb_corpus.models import (CORPUS, DocRecord, contract_upgrade, row_bank,
                              upgrade_row)
from cb_corpus.storage import Storage, apply_row_updates, iter_manifest_rows
from cb_corpus.taxonomy import DocType
from cb_corpus.vocabulary import build_vocabulary

VOCAB = load_vocabulary(build_vocabulary())


def _check(row: dict, *, transition: bool = False):
    return check_row(row, VOCAB, expected_corpus=CORPUS, transition=transition)


def _rec(**kw) -> DocRecord:
    base = dict(bank_code="ecb", doc_type=DocType.A1, title="Monetary policy decisions",
                document_url="https://www.ecb.europa.eu/press/pr/date/2022/html/ecb.mp220721~53e5bdd317.en.html",
                source_url="https://www.ecb.europa.eu/press/pr/html/index.en.html",
                date=date(2022, 7, 21), date_precision="day")
    base.update(kw)
    return DocRecord(**base)


# --- to_row ------------------------------------------------------------------

def test_to_row_writes_corpus_and_source_code_first():
    row = _rec().to_row()
    assert list(row)[:3] == ["corpus", "source_code", "doc_type"]
    assert row["corpus"] == "central-bank" and row["source_code"] == "ecb"
    assert "bank_code" not in row


def test_to_row_doc_id_unchanged():
    rec = _rec()
    assert rec.to_row()["doc_id"] == rec.doc_id == "ceb04c0aa89ed888"   # the vault golden row's id


def test_discovered_row_is_accepted_without_notice_after_the_transition():
    checked = _check(_rec().to_row())
    assert checked.refusals == ()
    assert [n.reason for n in checked.notices] == []
    assert checked.columns["date_kind"] == "meeting"          # policy_date_kind (ECB A1)


def test_downloaded_rendered_row_keeps_the_html_hash():
    """A page rendered to PDF: local_path/mime_type are the PDF's, sha256 is
    the HTML's (the bytes the bank served), html_path an extra."""
    rec = _rec(doc_type=DocType.C1, mime_type="application/pdf", sha256="ab" * 32,
               local_path="raw/ecb/C1/2022/ceb.pdf", html_path="raw/ecb/C1/2022/ceb.html")
    checked = _check(rec.to_row())
    assert checked.refusals == () and checked.notices == ()
    assert checked.columns["sha256"] == "ab" * 32
    assert checked.columns["local_path"] == "raw/ecb/C1/2022/ceb.pdf"
    assert checked.extra["html_path"] == "raw/ecb/C1/2022/ceb.html"


def test_undated_row_has_no_precision_nor_kind():
    row = _rec(date=None, date_precision="day").to_row()
    assert row["date"] is None and row["date_precision"] is None and "date_kind" not in row
    checked = _check(row)
    assert checked.refusals == () and checked.notices == ()


def test_every_bank_and_doc_type_is_in_the_vocabulary():
    from cb_corpus.banks import BIS_63
    for bank in BIS_63:
        for dt in DocType:
            for prov in ("bank_site", "bis_index", "repec_discovery", "wayback", "mirror"):
                row = _rec(bank_code=bank.code, doc_type=dt, provenance=prov).to_row()
                assert _check(row).refusals == (), (bank.code, dt.code, prov)


# --- the vault's golden central-bank rows ------------------------------------

CB_GOLDEN = {k: v for k, v in golden.accepted().items() if v["expected_corpus"] == CORPUS}


def test_golden_central_bank_rows_are_present():
    assert set(CB_GOLDEN) == {"cb_rate_decision", "cb_legacy_form", "cb_undated", "cb_rendered_html"}


@pytest.mark.parametrize("case", sorted(CB_GOLDEN))
def test_golden_rows_pass_our_vocabulary(case):
    assert _check(CB_GOLDEN[case]["row"], transition=True).refusals == ()


@pytest.mark.parametrize("case", sorted(CB_GOLDEN))
def test_upgraded_golden_rows_need_no_transition(case):
    """The one-shot's upgrade turns every golden form, the legacy one
    included, into a row the post-transition contract accepts; the columns
    are those the vault derives from the original row (corpus filled)."""
    g = CB_GOLDEN[case]
    row = copy.deepcopy(g["row"])
    contract_upgrade(row)
    checked = _check(row, transition=False)
    assert checked.refusals == ()
    notices = {n.reason for n in checked.notices}
    assert not notices & {"corpus_missing", "legacy_alias", "legacy_data_prefix",
                          "precision_without_date", "kind_without_date"}
    want = dict(g["expected_columns"], corpus=CORPUS)
    assert checked.columns == want


# --- upgrade (transition) ----------------------------------------------------

LEGACY = {"bank_code": "jp", "doc_type": "D1", "title": "t",
          "document_url": "https://www.boj.or.jp/a.pdf", "source_url": "",
          "date": None, "language": "en", "provenance": "repec_discovery",
          "mime_type": "application/pdf", "sha256": "0" * 64,
          "local_path": "data/raw/jp/D1/0/6349348f8603c010.pdf",
          "html_path": "data/raw/jp/D1/0/6349348f8603c010.html",
          "date_precision": "month", "date_kind": "publication",
          "doc_id": "6349348f8603c010", "year": None}


def test_contract_upgrade_in_place_positions_kept():
    row = dict(LEGACY)
    changes = contract_upgrade(row)
    assert changes == ["corpus", "source_code", "local_path", "html_path",
                       "date_precision", "date_kind"]
    assert list(row)[:3] == ["corpus", "source_code", "doc_type"]
    assert row["local_path"] == "raw/jp/D1/0/6349348f8603c010.pdf"
    assert row["html_path"] == "raw/jp/D1/0/6349348f8603c010.html"
    assert row["date_precision"] is None and "date_kind" not in row
    assert "bank_code" not in row
    # every other key, and its value, untouched
    for k, v in LEGACY.items():
        if k not in {"bank_code", "local_path", "html_path", "date_precision", "date_kind"}:
            assert row[k] == v
    assert contract_upgrade(row) == []                  # idempotent


def test_contract_upgrade_never_guesses_between_both_names():
    """bank_code AND source_code: neither is dropped (never guess which value
    is right; migrate-contract reports it as an error); the rest is upgraded."""
    row = dict(LEGACY, source_code="us")
    changes = contract_upgrade(row)
    assert changes[:2] == ["corpus", "both_source_names"]
    assert (row["bank_code"], row["source_code"]) == ("jp", "us")
    assert row["local_path"].startswith("raw/")


def test_contract_upgrade_renames_pdf_url_too():
    row = dict(LEGACY)
    row = {("pdf_url" if k == "document_url" else k): v for k, v in row.items()}
    assert "document_url" in contract_upgrade(row)
    assert list(row)[4] == "document_url" and "pdf_url" not in row


def test_contract_upgrade_keeps_absolute_paths_and_dated_precision():
    row = dict(LEGACY, local_path="/abs/raw/x.pdf", html_path=None,
               date="2026-08-01", year=2026)
    contract_upgrade(row)
    assert row["local_path"] == "/abs/raw/x.pdf" and row["html_path"] is None
    assert row["date_precision"] == "month" and row["date_kind"] == "publication"


def test_upgrade_row_applies_the_key_level_upgrade_only():
    """Read-time: renames, corpus and paths; the undated-row data fixes stay
    on disk until migrate-contract (policy-dates owns date changes)."""
    row = upgrade_row(dict(LEGACY))
    assert row["corpus"] == CORPUS and row["source_code"] == "jp"
    assert row["local_path"] == "raw/jp/D1/0/6349348f8603c010.pdf"
    assert row["date_precision"] == "month" and row["date_kind"] == "publication"


def test_row_bank_reads_both_names():
    assert row_bank({"source_code": "ecb"}) == "ecb"
    assert row_bank({"bank_code": "us"}) == "us"
    assert row_bank({"source_code": "ecb", "bank_code": "us"}) == "ecb"
    assert row_bank({}) is None


# --- storage: legacy manifests are read in the contract form -----------------

def _write_legacy(cfg: Config, rows: list[dict]) -> None:
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    with cfg.manifest_file("jp").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_legacy_rows_read_in_the_contract_form_and_rewritten_in_it(tmp_path):
    cfg = Config(data_dir=tmp_path)
    other = dict(LEGACY, doc_id="other", local_path="data/raw/jp/D1/0/other.pdf")
    _write_legacy(cfg, [dict(LEGACY), other])
    rows = list(iter_manifest_rows(cfg))
    assert all(r["corpus"] == CORPUS and r["source_code"] == "jp" for r in rows)
    assert rows[0]["local_path"] == "raw/jp/D1/0/6349348f8603c010.pdf"
    rows[0]["title"] = "new"
    assert apply_row_updates(cfg, {rows[0]["doc_id"]: rows[0]}) == 1
    lines = [json.loads(ln) for ln in cfg.manifest_file("jp").read_text().splitlines()]
    assert lines[0]["title"] == "new" and "bank_code" not in lines[0]
    assert lines[0]["corpus"] == CORPUS
    assert lines[1] == other                          # untouched row: verbatim


def test_storage_indexes_legacy_and_new_rows_by_bank(tmp_path):
    cfg = Config(data_dir=tmp_path)
    _write_legacy(cfg, [dict(LEGACY)])
    st = Storage(cfg)
    assert st._bank_by_id["6349348f8603c010"] == "jp"
    # a row given to apply_row_updates under the old name still lands in jp.jsonl
    assert apply_row_updates(cfg, {"6349348f8603c010": dict(LEGACY, title="x")}) == 1
    [row] = list(iter_manifest_rows(cfg))
    assert row["title"] == "x" and row["source_code"] == "jp"


@pytest.mark.parametrize("bad", ["data//raw/x.pdf", "data/data/raw/x.pdf"])
def test_data_strip_is_a_fixed_point(bad):
    """The `data/` strip applies only when what remains is a clean relative
    path: `data//raw` would become absolute, `data/data/raw` would need a
    second pass. Such a row is left as is and reported (`bad_path`)."""
    row = dict(LEGACY, local_path=bad)
    changes = contract_upgrade(row)
    assert "bad_path" in changes and "local_path" not in changes
    assert row["local_path"] == bad
    row2 = dict(LEGACY, html_path=bad)
    assert "bad_path" in contract_upgrade(row2) and row2["html_path"] == bad
