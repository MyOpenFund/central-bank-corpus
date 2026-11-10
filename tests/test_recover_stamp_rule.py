"""recover-downloads follows the storage duplicate-stamping rule (final-review
fix wave, grit 2026-11): a duplicate's dead URL is stamped onto its owner row
only when the owner is a real PDF (storage.is_stampable_owner, relaxed by
Config.stamp_duplicate_html), every stamp attempt is logged to
reports/duplicate_stamps.jsonl (label "recover"), and the dead URL's
quarantine is released only once its stamp is persisted -- released <=>
known to a fresh Storage <=> converged on the next pass."""
from __future__ import annotations

import json
from datetime import date

from cb_corpus.config import Config
from cb_corpus.models import DocRecord
from cb_corpus.recover import run_recover_downloads
from cb_corpus.storage import Storage, apply_row_updates, iter_manifest_rows
from cb_corpus.taxonomy import DocType
from tests.test_recover_candidates import (_NullFetcher, _entry,
                                           _stub_refresh_metadata, _write_candidates,
                                           _write_inventory)
from tests.test_recover_downloads import _StubFetcher

DUP = b"%PDF-1.4 shared-body " + b"z" * (25 * 1024)
OWNER_URL = "https://www.banque-france.fr/existing.pdf"
DEAD_URL = "https://www.banque-france.fr/dt-alias.pdf"
SOURCE = "https://ideas.repec.org/p/bfr/banfra/986.html"


def _seed_owner(cfg: Config, tmp_path, *, rendered: bool) -> DocRecord:
    """Index the owner row. `rendered`: flip it like a page rendered from
    HTML (mime_type application/pdf, html_path set)."""
    st = Storage(cfg, _NullFetcher())
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Existing copy",
                    pdf_url=OWNER_URL, date=date(2020, 1, 1), mime_type="application/pdf")
    src = tmp_path / "seed.pdf"
    src.write_bytes(DUP)
    assert st.reindex(rec, src) == "reindexed"
    if rendered:
        row = next(r for r in iter_manifest_rows(cfg) if r["doc_id"] == rec.doc_id)
        row["html_path"] = str(tmp_path / "seed.html")
        apply_row_updates(cfg, {rec.doc_id: row})
    return rec


def _quarantine(cfg: Config, url: str) -> list[dict]:
    p = cfg.data_dir / "download_quarantine.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(json.dumps({"url": url, "nights": ["2026-08-19", "2026-08-20"],
                                 "quarantined": True}) + "\n")
    return [json.loads(ln) for ln in p.read_text().splitlines()]


def _released(cfg: Config, url: str) -> bool:
    return {"url": url, "released": True} in _quarantine(cfg, url)


def _log(cfg: Config) -> list[dict]:
    p = cfg.reports_dir / "duplicate_stamps.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def _owner_row(cfg: Config, doc_id: str) -> dict:
    return next(r for r in iter_manifest_rows(cfg) if r["doc_id"] == doc_id)


# -- candidates pass ------------------------------------------------------

def _run_candidates(cfg, tmp_path, monkeypatch, csv="r.csv"):
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=DEAD_URL, title="Alias",
                                  source_url=SOURCE)])
    local = tmp_path / "alias.pdf"
    local.write_bytes(DUP)
    cand = _write_candidates(tmp_path, [{
        "dead_pdf_url": DEAD_URL, "file_path": str(local),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/alias"}])
    _stub_refresh_metadata(monkeypatch, title="Alias")
    return run_recover_downloads(config=cfg, fetcher=_NullFetcher(), candidates=str(cand),
                                 download=True, csv_path=str(tmp_path / csv))


def test_candidates_html_owner_not_stamped_logged_not_released(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    owner = _seed_owner(cfg, tmp_path, rendered=True)
    _quarantine(cfg, DEAD_URL)
    results = _run_candidates(cfg, tmp_path, monkeypatch)
    assert results["fr"]["duplicate"] == 1
    assert not _owner_row(cfg, owner.doc_id).get("alt_urls")
    log = _log(cfg)
    assert [e["url"] for e in log] == [DEAD_URL, "https://web.archive.org/alias"]
    assert all(e["persisted"] is False and e["label"] == "recover"
               and e["owner_doc_id"] == owner.doc_id and e["source_url"] == SOURCE
               for e in log)
    assert not _released(cfg, DEAD_URL)
    # Not converged: the next pass re-evaluates it (duplicate again).
    again = _run_candidates(cfg, tmp_path, monkeypatch, csv="r2.csv")
    assert again["fr"]["duplicate"] == 1 and again["fr"]["converged"] == 0


def test_candidates_pdf_owner_stamped_logged_released(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    owner = _seed_owner(cfg, tmp_path, rendered=False)
    _quarantine(cfg, DEAD_URL)
    _run_candidates(cfg, tmp_path, monkeypatch)
    row = _owner_row(cfg, owner.doc_id)
    assert row["alt_urls"] == [DEAD_URL, "https://web.archive.org/alias"]
    # recover stamps alt_urls only: its source pages never go to alt_source_urls.
    assert not row.get("alt_source_urls")
    log = _log(cfg)
    assert len(log) == 2 and all(e["persisted"] is True and e["label"] == "recover"
                                 and e["owner_bank"] == "fr" for e in log)
    assert _released(cfg, DEAD_URL)
    again = _run_candidates(cfg, tmp_path, monkeypatch, csv="r2.csv")
    assert again["fr"]["converged"] == 1


def test_candidates_html_owner_stamped_when_relaxed(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path, stamp_duplicate_html=True)
    owner = _seed_owner(cfg, tmp_path, rendered=True)
    _quarantine(cfg, DEAD_URL)
    _run_candidates(cfg, tmp_path, monkeypatch)
    assert DEAD_URL in _owner_row(cfg, owner.doc_id)["alt_urls"]
    assert _released(cfg, DEAD_URL)


def test_candidates_pass_discards_storage_queue(tmp_path, monkeypatch):
    """reindex() queues its own duplicate-content stamp; the candidates pass
    stamps duplicates itself and must drop that queue (else it would leak
    into a later flush on the same Storage)."""
    seen: list[Storage] = []
    real_init = Storage.__init__

    def capture(self, *a, **k):
        real_init(self, *a, **k)
        seen.append(self)
    cfg = Config(data_dir=tmp_path)
    _seed_owner(cfg, tmp_path, rendered=False)
    monkeypatch.setattr(Storage, "__init__", capture)
    _run_candidates(cfg, tmp_path, monkeypatch)
    [st] = seen
    assert st._pending_dups == []


# -- CDX-walk pass --------------------------------------------------------

def _run_cdx(cfg, tmp_path, csv="r.csv"):
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=DEAD_URL, title="Alias",
                                  source_url="")])
    fetcher = _StubFetcher(
        cdx_hits={"dt-alias.pdf": "20240101000000"},
        bytes_fail={"www.banque-france.fr"},
        bytes_ok={"web.archive.org/web/20240101000000id_": (DUP, "application/pdf")})
    return run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                                 fetcher=fetcher, csv_path=str(tmp_path / csv))


def test_download_html_owner_not_stamped_logged_not_released(tmp_path):
    cfg = Config(data_dir=tmp_path)
    owner = _seed_owner(cfg, tmp_path, rendered=True)
    _quarantine(cfg, DEAD_URL)
    results = _run_cdx(cfg, tmp_path)
    assert results["fr"]["duplicate"] == 1
    assert not _owner_row(cfg, owner.doc_id).get("alt_urls")
    [e] = _log(cfg)
    assert e["persisted"] is False and e["label"] == "recover" and e["url"] == DEAD_URL
    assert not _released(cfg, DEAD_URL)
    again = _run_cdx(cfg, tmp_path, csv="r2.csv")
    assert again["fr"]["duplicate"] == 1 and again["fr"]["converged"] == 0


def test_download_pdf_owner_stamped_logged_released(tmp_path):
    cfg = Config(data_dir=tmp_path)
    owner = _seed_owner(cfg, tmp_path, rendered=False)
    _quarantine(cfg, DEAD_URL)
    _run_cdx(cfg, tmp_path)
    assert _owner_row(cfg, owner.doc_id)["alt_urls"] == [DEAD_URL]
    [e] = _log(cfg)
    assert e["persisted"] is True and e["label"] == "recover"
    assert e["owner_doc_id"] == owner.doc_id
    assert _released(cfg, DEAD_URL)
    assert _run_cdx(cfg, tmp_path, csv="r2.csv")["fr"]["converged"] == 1


def test_download_pass_logs_source_url(tmp_path):
    cfg = Config(data_dir=tmp_path)
    _seed_owner(cfg, tmp_path, rendered=False)
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=DEAD_URL, title="Alias",
                                  source_url=SOURCE)])
    fetcher = _StubFetcher(
        cdx_hits={"dt-alias.pdf": "20240101000000"},
        pages={},
        bytes_fail={"www.banque-france.fr"},
        bytes_ok={"web.archive.org/web/20240101000000id_": (DUP, "application/pdf")})
    run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                          fetcher=fetcher, csv_path=str(tmp_path / "r.csv"))
    [e] = _log(cfg)
    assert e["source_url"] == SOURCE


def test_unwritable_log_never_fails_a_persisted_stamp(tmp_path, monkeypatch, capsys):
    """The merge is authoritative: a log write failure after a persisted stamp
    only warns, so recover still releases the quarantine."""
    from cb_corpus.storage import Storage
    cfg = Config(data_dir=tmp_path)
    owner = _seed_owner(cfg, tmp_path, rendered=False)
    _quarantine(cfg, DEAD_URL)

    def boom(self, *a, **k):
        raise OSError("reports dir read-only")
    monkeypatch.setattr(Storage, "_log_duplicates", boom)
    _run_candidates(cfg, tmp_path, monkeypatch)
    assert DEAD_URL in _owner_row(cfg, owner.doc_id)["alt_urls"]
    assert _released(cfg, DEAD_URL)
    assert "duplicate log not written" in capsys.readouterr().err
