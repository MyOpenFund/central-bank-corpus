"""Manifest rows follow the `documents` contract (vault-contract 1.0.0):
`corpus: central-bank`, `source_code` (was `bank_code`), paths relative to the
data root. Every reader and writer expects that form: a row in an older one
is refused loudly (models.ContractRowError, naming migrate-contract), which
rewrites it on the raw JSON."""
from __future__ import annotations

import copy
import json
from datetime import date

import pytest
from vault_contract import check_row, golden, load_vocabulary

from cb_corpus.config import Config
from cb_corpus.contract_migrate import contract_upgrade
from cb_corpus.models import CORPUS, ContractRowError, DocRecord, check_contract_row
from cb_corpus.storage import (Storage, _write_per_bank_unlocked, apply_row_updates,
                               iter_manifest_rows, merge_rows_locked, migrate_legacy_layout)
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
def test_our_readers_refuse_exactly_what_the_vault_refuses_without_transition(case):
    row = CB_GOLDEN[case]["row"]
    vault_ok = _check(row, transition=False).refusals == ()
    try:
        check_contract_row(row)
        ours_ok = True
    except ContractRowError:
        ours_ok = False
    assert ours_ok == vault_ok


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
    assert check_contract_row(row) is row           # and our own readers accept it
    notices = {n.reason for n in checked.notices}
    assert not notices & {"corpus_missing", "legacy_alias", "legacy_data_prefix",
                          "precision_without_date", "kind_without_date"}
    want = dict(g["expected_columns"], corpus=CORPUS)
    assert checked.columns == want


# --- migrate-contract's per-row upgrade ----------------------------------------------------

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


# --- readers: the contract form only, an old form fails loudly ----------------

def _write_legacy(cfg: Config, rows: list[dict], bank: str = "jp") -> None:
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    with cfg.manifest_file(bank).open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _contract(**kw) -> dict:
    row = _rec(**kw).to_row()
    row.update(local_path="raw/ecb/A1/2022/x.pdf")
    return row


def test_the_transition_readers_are_gone():
    import cb_corpus.models as models
    for name in ("row_bank", "row_url", "upgrade_row", "contract_upgrade",
                 "LEGACY_URL_KEY", "LEGACY_URL_KEYS", "LEGACY_SOURCE_KEY"):
        assert not hasattr(models, name), name


def test_check_contract_row_passes_a_written_row():
    row = _contract()
    assert check_contract_row(row) is row
    assert check_contract_row(_rec().to_row()) is not None   # no path at all


@pytest.mark.parametrize("change,what", [
    (lambda r: r.pop("corpus"), "corpus"),
    (lambda r: r.update(corpus="company"), "corpus"),
    (lambda r: r.__setitem__("bank_code", r.pop("source_code")), "bank_code"),
    (lambda r: r.update(bank_code="ecb"), "bank_code"),
    (lambda r: r.pop("source_code"), "source_code"),
    (lambda r: r.update(source_code=""), "source_code"),
    (lambda r: r.__setitem__("pdf_url", r.pop("document_url")), "pdf_url"),
    (lambda r: r.update(retired_duplicates=[{"doc_id": "d", "pdf_url": "u"}]), "pdf_url"),
    (lambda r: r.update(local_path="data/raw/ecb/A1/2022/x.pdf"), "local_path"),
    (lambda r: r.update(html_path="data/raw/ecb/A1/2022/x.html"), "html_path"),
    (lambda r: r.update(local_path="/app/data/raw/ecb/A1/2022/x.pdf"), "local_path"),
])
def test_check_contract_row_refuses_every_old_form_naming_migrate_contract(change, what):
    row = _contract()
    change(row)
    with pytest.raises(ContractRowError) as exc:
        check_contract_row(row, "ecb.jsonl")
    msg = str(exc.value)
    assert what in msg and "migrate-contract" in msg and "ecb.jsonl" in msg
    assert row["doc_id"] in msg
    assert isinstance(exc.value, ValueError)


def test_reading_a_legacy_manifest_fails_loudly(tmp_path):
    cfg = Config(data_dir=tmp_path)
    _write_legacy(cfg, [dict(LEGACY)])
    with pytest.raises(ContractRowError, match="migrate-contract") as exc:
        list(iter_manifest_rows(cfg))
    assert "jp.jsonl" in str(exc.value) and "6349348f8603c010" in str(exc.value)
    with pytest.raises(ContractRowError, match="migrate-contract"):
        Storage(cfg)


def test_a_legacy_row_is_never_filed_under_unknown(tmp_path):
    cfg = Config(data_dir=tmp_path)
    row = _contract()
    _write_legacy(cfg, [row], bank="ecb")
    before = cfg.manifest_file("ecb").read_bytes()
    old = dict(row, title="x")
    old["bank_code"] = old.pop("source_code")
    with pytest.raises(ContractRowError, match="migrate-contract"):
        apply_row_updates(cfg, {row["doc_id"]: old})
    with pytest.raises(ContractRowError, match="migrate-contract"):
        _write_per_bank_unlocked(cfg, [old])
    assert cfg.manifest_file("ecb").read_bytes() == before
    assert not cfg.manifest_file("_unknown").exists()


def test_merge_on_a_legacy_row_on_disk_fails_and_writes_nothing(tmp_path):
    cfg = Config(data_dir=tmp_path)
    _write_legacy(cfg, [dict(LEGACY)])
    before = cfg.manifest_file("jp").read_bytes()
    with pytest.raises(ContractRowError, match="migrate-contract"):
        merge_rows_locked(cfg, {"jp": {"6349348f8603c010": lambda r: True}})
    assert cfg.manifest_file("jp").read_bytes() == before


def test_outside_clone_migrates_then_reads(tmp_path, monkeypatch):
    """An outside clone still holding pre-contract manifests (even under the
    pre-2026-11-19 `pdf_url`): the one-shot migrate-contract works on the raw
    JSON, after which every reader accepts the rows."""
    from cb_corpus import cli
    cfg = Config(data_dir=tmp_path)
    old = {("pdf_url" if k == "document_url" else k): v for k, v in LEGACY.items()}
    _write_legacy(cfg, [old])
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setattr(cli, "Config", lambda: cfg)
    assert cli.main(["migrate-contract", "--write"]) == 0
    [row] = list(iter_manifest_rows(cfg))
    assert row["source_code"] == "jp" and row["document_url"] == LEGACY["document_url"]
    assert Storage(cfg)._bank_by_id == {"6349348f8603c010": "jp"}


def test_legacy_single_manifest_is_split_then_migrated(tmp_path):
    """A pre-split `manifest.jsonl` (pre-contract rows): migrate-contract
    --write splits it per bank first, then upgrades the split files."""
    from cb_corpus.contract_migrate import migrate_contract
    cfg = Config(data_dir=tmp_path)
    cfg.manifest_path.write_text(json.dumps(LEGACY) + "\n"
                                 + json.dumps(dict(LEGACY, bank_code="us", doc_id="u1")) + "\n")
    res = migrate_contract(cfg, write=True)
    assert sorted(p.name for p in cfg.manifest_dir.glob("*.jsonl")) == ["jp.jsonl", "us.jsonl"]
    assert sum(f.upgraded for f in res.files) == 2
    assert {r["source_code"] for r in iter_manifest_rows(cfg)} == {"jp", "us"}


def test_split_refuses_a_row_without_a_bank(tmp_path):
    cfg = Config(data_dir=tmp_path)
    nobank = {k: v for k, v in LEGACY.items() if k != "bank_code"}
    cfg.manifest_path.write_text(json.dumps(LEGACY) + "\n" + json.dumps(nobank) + "\n")
    with pytest.raises(ValueError, match="no source_code"):
        migrate_legacy_layout(cfg)
    assert cfg.manifest_path.exists() and not cfg.manifest_dir.exists()


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


@pytest.mark.parametrize("bad", [dict(bank_code=""), dict(local_path="/abs/raw/ecb/x.pdf"),
                                 dict(html_path="data/raw/ecb/x.html")])
def test_append_never_writes_a_row_the_readers_would_refuse(tmp_path, bad):
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    st._append(_rec())
    path = cfg.manifest_file("ecb")
    before = path.read_bytes()
    rec = _rec(document_url="https://www.ecb.europa.eu/other.pdf", **bad)
    with pytest.raises(ContractRowError):
        st._append(rec)
    assert path.read_bytes() == before
    assert not cfg.manifest_file("").exists() and not cfg.manifest_file("_unknown").exists()


def test_an_append_refusal_names_a_producer_bug_not_the_migration(tmp_path):
    """An in-memory record is not a stored row: migrate-contract cannot fix
    it, so the refusal names the problem and the producer, not the one-shot."""
    st = Storage(Config(data_dir=tmp_path))
    with pytest.raises(ContractRowError) as ei:
        st._append(_rec(bank_code=""))
    msg = str(ei.value)
    assert "no `source_code`" in msg and "producer bug" in msg
    assert "migrate-contract" not in msg


def test_the_readers_message_says_refused_rows_are_fixed_by_hand():
    with pytest.raises(ContractRowError) as ei:
        check_contract_row({"doc_id": "x", "bank_code": "aa"}, "aa.jsonl")
    msg = str(ei.value)
    assert "migrate-contract --write" in msg and "by hand" in msg
