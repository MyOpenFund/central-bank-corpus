"""Tests for `recover-downloads --candidates` (external-recovery injection)
and `--seed-quarantine` (cb_corpus/recover.py, cb_corpus/quarantine.py).

No network in this suite: `_refresh_metadata` is monkeypatched to a stub so
candidate rows never actually reach the fake IDEAS fetch. Fixtures use
adversarial shapes on purpose: a candidate whose dead_document_url matches nothing
in the audit trail, a too-small "PDF" (truncated/HTML-error-page-as-pdf), and
a candidate whose bytes hash-match a document already in the corpus.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from cb_corpus.config import Config
from cb_corpus.models import DocRecord
from cb_corpus.quarantine import Quarantine
from cb_corpus.storage import Storage, iter_manifest_rows
from cb_corpus.taxonomy import DocType
from tests.conftest import pdf_body


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

class _NullFetcher:
    """No candidate test in this suite should ever reach the network --
    `_refresh_metadata` is monkeypatched below. This fetcher raises if
    anything does try to use it, so a leak is caught loudly."""

    def get_text(self, url):
        raise AssertionError(f"unexpected network call: {url}")

    def get_bytes(self, url):
        raise AssertionError(f"unexpected network call: {url}")

    def throttle(self, url):
        pass


def _stub_refresh_metadata(monkeypatch, title="Refreshed Title", rec_date=None,
                           date_precision=None, date_source=None):
    """Monkeypatch `_refresh_metadata` (used by both the CDX-walk path and the
    --candidates path) to a deterministic, network-free stub."""
    import cb_corpus.recover as recover_mod

    def _fake(fetcher, entry):
        return (title, rec_date, date_precision, date_source, [])

    monkeypatch.setattr(recover_mod, "_refresh_metadata", _fake)


def _write_inventory(cfg: Config, entries: list[dict]) -> None:
    path = cfg.data_dir / "download_errors.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def _entry(bank="fr", document_url="https://www.banque-france.fr/dt986.pdf",
          title="Document de travail 986", source_url="", doc_type="D1",
          alt_urls=None, ts="2026-01-01T00:00:00+00:00"):
    return {
        "ts": ts, "label": f"{bank}:{doc_type}", "bank_code": bank,
        "doc_type": doc_type, "title": title, "document_url": document_url,
        "alt_urls": alt_urls or [], "source_url": source_url,
        "error": "HTTPError: 403",
    }


def _write_candidates(tmp_path, candidates: list[dict]) -> Path:
    path = tmp_path / "candidates.jsonl"
    with path.open("w") as fh:
        for c in candidates:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    return path


def _make_local_pdf(tmp_path, name="recovered.pdf", size=25 * 1024) -> Path:
    """A verified-shape local PDF: %PDF magic, bigger than the 20KB floor."""
    path = tmp_path / name
    body = pdf_body(b"%PDF-1.4 " + b"x" * size)
    path.write_bytes(body)
    return path


def _csv_rows(path):
    import csv
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# provenance rules (spec decision 2)
# ---------------------------------------------------------------------------

def test_wayback_provenance_dead_url_stays_document_url(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/dt986.pdf"
    snapshot_url = "https://web.archive.org/web/20250101000000id_/https://www.banque-france.fr/dt986.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path, "wayback.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": snapshot_url,
    }])
    _stub_refresh_metadata(monkeypatch, title="WP 986", rec_date=None,
                           date_precision=None, date_source=None)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recovered"] == 1

    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1
    row = rows[0]
    assert row["document_url"] == dead_url               # official (dead) URL preserved as identity
    assert row["alt_urls"] == [snapshot_url]         # snapshot as the fallback
    assert row["provenance"] == "wayback"
    assert row["date_source"] == "wayback"           # no repec date -> honestly wayback
    assert row["local_path"] and (tmp_path / row["local_path"]).is_file()

    # File landed exactly at Storage.target_path(rec).
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="WP 986",
                    document_url=dead_url, mime_type="application/pdf")
    storage = Storage(cfg, _NullFetcher())
    assert (tmp_path / row["local_path"]) == storage.target_path(rec)

    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "recovered"


def test_bank_site_provenance_final_url_becomes_document_url(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/old-path/dt987.pdf"
    final_url = "https://www.banque-france.fr/new-path/dt987.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path, "banksite.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "bank_site", "final_url": final_url,
    }])
    _stub_refresh_metadata(monkeypatch, title="WP 987")

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recovered"] == 1

    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1
    row = rows[0]
    assert row["document_url"] == final_url               # live URL points at reality
    assert row["alt_urls"] == [dead_url]              # dead URL kept for history
    assert row["provenance"] == "bank_site"
    assert row["date_source"] == "bank_site"          # DocRecord default, untouched

    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="WP 987",
                    document_url=final_url, mime_type="application/pdf")
    storage = Storage(cfg, _NullFetcher())
    assert (tmp_path / row["local_path"]) == storage.target_path(rec)


def test_mirror_provenance_dead_url_stays_document_url(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/dt988.pdf"
    mirror_url = "https://www.econstor.eu/bitstream/dt988.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path, "mirror.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "mirror", "final_url": mirror_url,
    }])
    _stub_refresh_metadata(monkeypatch, title="WP 988")

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recovered"] == 1

    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1
    row = rows[0]
    assert row["document_url"] == dead_url                 # official URL is the WP's identity
    assert row["alt_urls"] == [mirror_url]
    assert row["provenance"] == "mirror"

    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="WP 988",
                    document_url=dead_url, mime_type="application/pdf")
    storage = Storage(cfg, _NullFetcher())
    assert (tmp_path / row["local_path"]) == storage.target_path(rec)


# ---------------------------------------------------------------------------
# bad-file / unknown-entry
# ---------------------------------------------------------------------------

def test_too_small_file_is_bad_file_never_registered(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/truncated.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    tiny = tmp_path / "tiny.pdf"
    tiny.write_bytes(b"%PDF-1.4 too small")   # well under 20KB
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(tiny),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/x",
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["bad-file"] == 1
    assert list(iter_manifest_rows(cfg, "fr")) == []
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "bad-file"


def test_missing_pdf_magic_is_bad_file(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/notreally.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"<html>404 not found</html>" + b"x" * (25 * 1024))  # big but no magic
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(fake),
        "recovered_from": "mirror", "final_url": "https://econstor.eu/x.pdf",
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["bad-file"] == 1
    assert list(iter_manifest_rows(cfg, "fr")) == []


def test_unknown_dead_url_is_unknown_entry(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    _write_inventory(cfg, [_entry(bank="fr", document_url="https://www.banque-france.fr/known.pdf")])
    local_pdf = _make_local_pdf(tmp_path)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": "https://www.banque-france.fr/never-in-inventory.pdf",
        "file_path": str(local_pdf), "recovered_from": "wayback",
        "final_url": "https://web.archive.org/x",
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["_unknown"]["unknown-entry"] == 1
    assert list(iter_manifest_rows(cfg, "fr")) == []
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "unknown-entry"
    assert csv_rows[0]["document_url"] == "https://www.banque-france.fr/never-in-inventory.pdf"


# ---------------------------------------------------------------------------
# duplicate-content dedup + orphan-file removal
# ---------------------------------------------------------------------------

def test_duplicate_content_is_deduped_and_orphan_file_removed(tmp_path, monkeypatch, capsys):
    """Self-healing (the actual point of this fix): the dead alias URL AND
    the candidate's own final_url (its Wayback snapshot) are both stamped
    onto the canonical (seeded) row's alt_urls, and the dead URL's
    quarantine is released -- so a future recover pass over this same dead
    URL sees it as already known."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dup_bytes = pdf_body(b"%PDF-1.4 duplicate-body " + b"y" * (25 * 1024))

    # Pre-seed the corpus with this exact content under a different, live URL.
    seed_storage = Storage(cfg, _NullFetcher())
    seed_rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Existing copy",
                        document_url="https://www.banque-france.fr/existing.pdf",
                        date=date(2020, 1, 1), mime_type="application/pdf")
    seed_path = cfg.data_dir / "seed-source.pdf"
    seed_path.write_bytes(dup_bytes)
    assert seed_storage.reindex(seed_rec, seed_path) == "reindexed"

    dead_url = "https://www.banque-france.fr/dt-alias.pdf"
    final_url = "https://web.archive.org/alias"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url, title="Alias copy")])
    q_path = cfg.data_dir / "download_quarantine.jsonl"
    q_path.parent.mkdir(parents=True, exist_ok=True)
    q_path.write_text(json.dumps({"url": dead_url,
                                  "nights": ["2026-08-19", "2026-08-20"],
                                  "quarantined": False}) + "\n")
    local_pdf = tmp_path / "alias.pdf"
    local_pdf.write_bytes(dup_bytes)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": final_url,
    }])
    _stub_refresh_metadata(monkeypatch, title="Alias copy")

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["duplicate"] == 1
    assert results["fr"]["recovered"] == 0

    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1   # only the pre-seeded row -- nothing new appended
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "duplicate"
    assert csv_rows[0]["canonical_doc_id"] == seed_rec.doc_id

    # No orphan bytes: the copy made into the corpus layout for the alias
    # doc_id must have been removed again since it was never indexed.
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Alias copy",
                    document_url=dead_url, mime_type="application/pdf")
    storage = Storage(cfg, _NullFetcher())
    assert not storage.target_path(rec).exists()

    # Both the dead URL and its Wayback snapshot are stamped onto the
    # canonical row (order not asserted -- a set internally).
    assert rows[0]["doc_id"] == seed_rec.doc_id
    assert set(rows[0]["alt_urls"]) == {dead_url, final_url}

    q_lines = q_path.read_text().splitlines()
    assert json.loads(q_lines[-1]) == {"url": dead_url, "released": True}

    stderr = capsys.readouterr().err
    assert "[recover] stamped 2 alt_url(s) on 1 row(s)" in stderr


def test_candidates_duplicate_stamp_order_dead_then_final(tmp_path, monkeypatch):
    """Deterministic alt_urls append order (spec §4): the duplicate-content
    site stamps [dead_url, final_url] onto the canonical row in that exact
    order -- a set here would make append order vary across runs (hash
    randomization), churning autocommitted manifest diffs for nothing."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dup_bytes = pdf_body(b"%PDF-1.4 duplicate-body-order " + b"y" * (25 * 1024))

    seed_storage = Storage(cfg, _NullFetcher())
    seed_rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Existing copy",
                        document_url="https://www.banque-france.fr/existing-order.pdf",
                        date=date(2020, 1, 1), mime_type="application/pdf")
    seed_path = cfg.data_dir / "seed-source-order.pdf"
    seed_path.write_bytes(dup_bytes)
    assert seed_storage.reindex(seed_rec, seed_path) == "reindexed"

    dead_url = "https://www.banque-france.fr/dt-alias-order.pdf"
    final_url = "https://web.archive.org/alias-order"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url, title="Alias copy order")])
    local_pdf = tmp_path / "alias-order.pdf"
    local_pdf.write_bytes(dup_bytes)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": final_url,
    }])
    _stub_refresh_metadata(monkeypatch, title="Alias copy order")

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["duplicate"] == 1

    rows = list(iter_manifest_rows(cfg, "fr"))
    assert rows[0]["doc_id"] == seed_rec.doc_id
    assert rows[0]["alt_urls"][-2:] == [dead_url, final_url]


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/dryrun.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/dryrun",
    }])
    _stub_refresh_metadata(monkeypatch, title="Dry run WP")

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=False,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recoverable"] == 1
    assert results["fr"]["recovered"] == 0
    assert list(iter_manifest_rows(cfg, "fr")) == []

    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Dry run WP",
                    document_url=dead_url, mime_type="application/pdf")
    storage = Storage(cfg, _NullFetcher())
    assert not storage.target_path(rec).exists()

    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "recoverable"


def test_candidates_converged_dry_run_does_not_release_quarantine(tmp_path, monkeypatch):
    """A row already indexed under the entry's own `document_url` (wayback
    provenance keeps the candidate's `document_url == dead_url`) makes the entry
    `_is_converged` from the start -- the short-circuit fires before the
    probe is even reached, in EITHER mode (dry-run or --download). The
    dry-run contract must still hold: no quarantine release, no manifest
    write, ever."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/probe-dup.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url, title="Probe dup WP")])

    # Pre-seed a row already indexed under the EXACT identity the candidate's
    # own doc_id resolves to (wayback provenance keeps document_url == dead_url)
    # -- makes the entry `_is_converged` honestly, before ever reaching the
    # probe.
    seed_storage = Storage(cfg, _NullFetcher())
    seed_rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Already here",
                        document_url=dead_url, date=date(2020, 1, 1),
                        mime_type="application/pdf")
    seed_path = tmp_path / "seed.pdf"
    seed_path.write_bytes(pdf_body(b"%PDF-1.4 " + b"z" * (25 * 1024)))
    assert seed_storage.reindex(seed_rec, seed_path) == "reindexed"

    q_path = cfg.data_dir / "download_quarantine.jsonl"
    q_path.parent.mkdir(parents=True, exist_ok=True)
    q_path.write_text(json.dumps({"url": dead_url, "nights": ["2026-08-19"],
                                  "quarantined": False}) + "\n")

    local_pdf = _make_local_pdf(tmp_path, "probe-dup-local.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/probe-dup",
    }])
    _stub_refresh_metadata(monkeypatch, title="Probe dup WP")

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=False,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["converged"] == 1

    # No quarantine release: still exactly the one seeded line, no
    # "released" tombstone appended.
    q_lines = q_path.read_text().splitlines()
    assert len(q_lines) == 1
    assert json.loads(q_lines[0]).get("released") is not True

    # No manifest write beyond the pre-seeded row.
    assert len(list(iter_manifest_rows(cfg, "fr"))) == 1


def test_bank_site_probe_duplicate_stamps_dead_url_on_alt_urls(tmp_path, monkeypatch):
    """The flagship self-heal scenario for `bank_site` provenance: the probe
    (`storage.reindex(..., dry_run=True)`) recognises the candidate's doc_id
    as already indexed under its live final_url, so nothing is (re)downloaded
    -- but unlike wayback/mirror (where rec.document_url == dead_url, making the
    stamp inherently a no-op), here rec.document_url == final_url, so the
    genuinely different dead_url must land in the pre-existing row's
    alt_urls, its own quarantine must be released, and the CSV row must
    carry the canonical (pre-seeded) doc_id."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/old-path/probe-bs.pdf"
    final_url = "https://www.banque-france.fr/new-path/probe-bs.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url, title="Probe bank_site WP")])

    # Pre-seed a row already indexed under the LIVE final_url -- exactly what
    # a prior bank_site recovery of this same doc would have produced.
    seed_storage = Storage(cfg, _NullFetcher())
    seed_rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Already here",
                        document_url=final_url, date=date(2020, 1, 1),
                        mime_type="application/pdf")
    seed_path = tmp_path / "seed-bs.pdf"
    seed_path.write_bytes(pdf_body(b"%PDF-1.4 " + b"w" * (25 * 1024)))
    assert seed_storage.reindex(seed_rec, seed_path) == "reindexed"

    q_path = cfg.data_dir / "download_quarantine.jsonl"
    q_path.parent.mkdir(parents=True, exist_ok=True)
    q_path.write_text(json.dumps({"url": dead_url, "nights": ["2026-08-19"],
                                  "quarantined": False}) + "\n")

    local_pdf = _make_local_pdf(tmp_path, "probe-bs-local.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "bank_site", "final_url": final_url,
    }])
    _stub_refresh_metadata(monkeypatch, title="Probe bank_site WP")

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["duplicate"] == 1

    # (a) dead_url lands in the canonical row's alt_urls; document_url unchanged.
    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1
    assert rows[0]["doc_id"] == seed_rec.doc_id
    assert rows[0]["document_url"] == final_url
    assert dead_url in rows[0]["alt_urls"]

    # (b) the dead URL's quarantine is released.
    q_lines = q_path.read_text().splitlines()
    assert json.loads(q_lines[-1]) == {"url": dead_url, "released": True}

    # (c) the CSV row carries the canonical doc_id.
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "duplicate"
    assert csv_rows[0]["canonical_doc_id"] == seed_rec.doc_id


# ---------------------------------------------------------------------------
# --seed-quarantine
# ---------------------------------------------------------------------------

def test_seed_quarantine_flag_applies_seed_before_the_pass(tmp_path):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    url = "https://www.banque-france.fr/unrecoverable.pdf"
    seed_path = tmp_path / "unrecoverable.jsonl"
    seed_path.write_text(json.dumps({"url": url, "reason": "hunt exhausted"}) + "\n")

    run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                          seed_quarantine=str(seed_path),
                          csv_path=str(tmp_path / "r.csv"))

    q = Quarantine(cfg)
    assert q.is_quarantined(url) is True
    lines = (cfg.data_dir / "download_quarantine.jsonl").read_text().splitlines()
    assert json.loads(lines[-1]) == {"url": url, "seeded": "hunt exhausted", "quarantined": True}


def test_apply_seed_quarantine_validates_whole_file_before_applying_any_seed(tmp_path):
    """A malformed line ANYWHERE in the seed file must abort the whole
    application with NO partial effect -- not seed the good lines that came
    before it and then blow up on the bad one."""
    from cb_corpus.recover import _apply_seed_quarantine

    cfg = Config(data_dir=tmp_path)
    q = Quarantine(cfg)
    good_url = "https://www.banque-france.fr/good.pdf"
    seed_path = tmp_path / "seed.jsonl"
    seed_path.write_text(
        json.dumps({"url": good_url, "reason": "ok"}) + "\n"
        '{"url": "https://bad.test/x.pdf", "reason": '  # malformed JSON (no closing)
    )

    with pytest.raises(Exception):
        _apply_seed_quarantine(q, str(seed_path))

    # No partial application: the good line above the bad one must not have
    # been seeded either.
    assert q.is_quarantined(good_url) is False


# ---------------------------------------------------------------------------
# CRITICAL — orphan unlink must never delete a canonical corpus file
# ---------------------------------------------------------------------------

def test_rerunning_same_candidates_file_twice_does_not_delete_corpus_file(tmp_path, monkeypatch):
    """The exact data-loss scenario: a --candidates pass is re-run (e.g. a
    retry) after the corpus already converged on these docs. The second run
    must recognise that via the `_is_converged` short-circuit BEFORE ever
    touching the local file (the check runs even before the file/doc-type
    validations), never copy2-then-unlink the real corpus file.

    Converged means "the index already knows every URL" (spec §2): no
    stamping, no quarantine call -- so a quarantine entry set after the
    first run (e.g. an independent nightly-sync race) is left exactly as
    is, never released again by a converged verdict."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/rerun.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url, title="Rerun WP")])
    local_pdf = _make_local_pdf(tmp_path, "rerun.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/rerun",
    }])
    _stub_refresh_metadata(monkeypatch, title="Rerun WP")

    results1 = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                     candidates=str(cand_path), download=True,
                                     csv_path=str(tmp_path / "r1.csv"))
    assert results1["fr"]["recovered"] == 1

    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1
    local_path = (tmp_path / rows[0]["local_path"])
    assert local_path.is_file()

    # Simulate the nightly sync having independently re-quarantined the dead
    # URL since the first run -- a converged verdict must leave this exactly
    # as is (no quarantine call at all).
    q_path = cfg.data_dir / "download_quarantine.jsonl"
    with q_path.open("a") as fh:
        fh.write(json.dumps({"url": dead_url,
                             "nights": ["2026-08-19", "2026-08-20", "2026-08-21",
                                       "2026-08-22", "2026-08-23"],
                             "quarantined": True}) + "\n")

    # Re-run the exact same candidates file against the now-converged corpus.
    results2 = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                     candidates=str(cand_path), download=True,
                                     csv_path=str(tmp_path / "r2.csv"))
    assert results2["fr"]["converged"] == 1
    assert results2["fr"].get("duplicate", 0) == 0
    assert results2["fr"].get("recovered", 0) == 0

    # The real corpus file must still be present -- the exact bug this fix
    # prevents (copy2 -> reindex -> skip:already-indexed -> unlink(dest)).
    assert local_path.is_file()
    rows_after = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows_after) == 1

    csv_rows = _csv_rows(tmp_path / "r2.csv")
    assert csv_rows[0]["action"] == "converged"

    # No quarantine call for a converged verdict: the manually re-quarantined
    # line is untouched, no "released" tombstone appended.
    q_lines = q_path.read_text().splitlines()
    assert len(q_lines) == 1
    assert json.loads(q_lines[-1]).get("released") is not True


def test_duplicated_candidate_line_within_one_run_is_deduped_without_deleting_file(tmp_path, monkeypatch):
    """Same scenario as above but within a SINGLE run: the same candidate
    line appears twice in the file (an operator mistake / a hand-hunted
    list built by concatenating sources). The first occurrence recovers and
    indexes the doc in-memory immediately (`Storage.reindex` updates `_ids`/
    `_urls` without needing a manifest reload) -- so the second occurrence
    is already `_is_converged` by the time its turn comes, deduped even
    earlier than the probe (no file/doc-type validation at all)."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/dupline.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url, title="Dup line WP")])
    local_pdf = _make_local_pdf(tmp_path, "dupline.pdf")
    cand = {
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/dupline",
    }
    cand_path = _write_candidates(tmp_path, [cand, cand])
    _stub_refresh_metadata(monkeypatch, title="Dup line WP")

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recovered"] == 1
    assert results["fr"]["converged"] == 1

    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1
    assert (tmp_path / rows[0]["local_path"]).is_file()

    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert [r["action"] for r in csv_rows] == ["recovered", "converged"]


# ---------------------------------------------------------------------------
# IMPORTANT A — empty/missing final_url must be rejected before provenance
# ---------------------------------------------------------------------------

def test_wayback_empty_final_url_is_bad_candidate(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/nofinalurl-wb.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": "",
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["bad-candidate"] == 1
    assert list(iter_manifest_rows(cfg, "fr")) == []
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "bad-candidate"


def test_bank_site_missing_final_url_key_is_bad_candidate(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/nofinalurl-bs.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "bank_site",   # no "final_url" key at all
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["bad-candidate"] == 1
    assert list(iter_manifest_rows(cfg, "fr")) == []
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "bad-candidate"


def test_mirror_empty_final_url_is_bad_candidate(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/nofinalurl-mirror.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "mirror", "final_url": "",
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["bad-candidate"] == 1
    assert list(iter_manifest_rows(cfg, "fr")) == []
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "bad-candidate"


# ---------------------------------------------------------------------------
# IMPORTANT 2 — final_url honesty: reject a candidate whose byte origin is
# not honestly distinguishable from the dead URL, or (for wayback) not
# actually an archive.org snapshot. This is exactly the failure mode that
# produced a "wayback" manifest row whose alt_urls[0] silently equalled its
# own dead document_url (final-review finding 1: no real snapshot trail at all).
# ---------------------------------------------------------------------------

def test_final_url_equal_to_dead_url_is_bad_candidate_for_wayback(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/samething.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": dead_url,  # dishonest: no real snapshot
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["bad-candidate"] == 1
    assert list(iter_manifest_rows(cfg, "fr")) == []
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "bad-candidate"


def test_final_url_equal_to_dead_url_is_bad_candidate_for_bank_site(tmp_path, monkeypatch):
    """Rule (a) is not wayback-specific: ANY recovered_from with final_url ==
    dead_document_url is rejected -- a "moved" doc that resolves to its own dead
    URL never actually moved."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/samething-bs.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "bank_site", "final_url": dead_url,
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["bad-candidate"] == 1
    assert list(iter_manifest_rows(cfg, "fr")) == []


def test_wayback_final_url_not_archive_org_is_bad_candidate(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/notarchive.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path)
    # A different, live-looking URL -- NOT web.archive.org -- claiming to be
    # a wayback recovery. Not honestly a snapshot.
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback",
        "final_url": "https://www.banque-france.fr/moved/notarchive.pdf",
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["bad-candidate"] == 1
    assert list(iter_manifest_rows(cfg, "fr")) == []
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "bad-candidate"


def test_wayback_final_url_on_archive_org_is_accepted(tmp_path, monkeypatch):
    """Sanity: a genuine web.archive.org snapshot host still passes."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/realsnapshot.pdf"
    snapshot_url = "https://web.archive.org/web/20250101000000id_/https://www.banque-france.fr/realsnapshot.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": snapshot_url,
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recovered"] == 1
    assert results["fr"].get("bad-candidate", 0) == 0


# ---------------------------------------------------------------------------
# MINOR 1 — copy2/reindex exceptions stay recoverable + audited, never crash
# ---------------------------------------------------------------------------

def test_copy_exception_stays_recoverable_and_is_audited(tmp_path, monkeypatch):
    from cb_corpus import recover as recover_mod
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/copyfail.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url, title="Copy fail WP")])
    local_pdf = _make_local_pdf(tmp_path, "copyfail.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/copyfail",
    }])
    _stub_refresh_metadata(monkeypatch, title="Copy fail WP")

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(recover_mod.shutil, "copy2", _boom)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recoverable"] == 1
    assert results["fr"].get("recovered", 0) == 0
    assert list(iter_manifest_rows(cfg, "fr")) == []

    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "recoverable"

    errors_path = cfg.data_dir / "download_errors.jsonl"
    error_lines = [json.loads(l) for l in errors_path.read_text().splitlines() if l.strip()]
    reaudited = [e for e in error_lines if e.get("label") == "recover-downloads-candidates"]
    assert len(reaudited) == 1
    assert reaudited[0]["document_url"] == dead_url


# ---------------------------------------------------------------------------
# MINOR 2 — _read_candidates tolerates a malformed JSON line
# ---------------------------------------------------------------------------

def test_read_candidates_skips_malformed_line_with_one_warning(tmp_path, capsys):
    from cb_corpus.recover import _read_candidates

    path = tmp_path / "candidates.jsonl"
    good1 = json.dumps({"dead_document_url": "https://x.test/a.pdf", "file_path": "a.pdf",
                        "recovered_from": "wayback", "final_url": "https://web.archive.org/a"})
    bad = '{"dead_document_url": "https://x.test/b.pdf", '  # malformed JSON
    good2 = json.dumps({"dead_document_url": "https://x.test/c.pdf", "file_path": "c.pdf",
                        "recovered_from": "wayback", "final_url": "https://web.archive.org/c"})
    path.write_text(good1 + "\n" + bad + "\n" + good2 + "\n")

    rows = _read_candidates(str(path))

    assert [r["dead_document_url"] for r in rows] == ["https://x.test/a.pdf", "https://x.test/c.pdf"]
    err = capsys.readouterr().err
    assert err.count("WARNING") == 1


# ---------------------------------------------------------------------------
# MINOR 4 — --banks filter + candidates: out-of-scope match is "filtered"
# ---------------------------------------------------------------------------

def test_out_of_scope_candidate_is_filtered_not_unknown_entry(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.bundesbank.de/oos.pdf"
    _write_inventory(cfg, [_entry(bank="de", document_url=dead_url, title="DE WP")])
    local_pdf = _make_local_pdf(tmp_path)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/oos",
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(bank_codes=["fr"], config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["de"]["filtered"] == 1
    assert results.get("_unknown", {}).get("unknown-entry", 0) == 0
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "filtered"
    assert list(iter_manifest_rows(cfg, "de")) == []


# ---------------------------------------------------------------------------
# MINOR 5 — coverage for the existing bad-doc-type / bad-provenance actions
# ---------------------------------------------------------------------------

def test_bad_doc_type_action(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/baddoctype.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url, doc_type="ZZZ")])
    local_pdf = _make_local_pdf(tmp_path)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/baddoctype",
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["bad-doc-type"] == 1
    assert list(iter_manifest_rows(cfg, "fr")) == []
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "bad-doc-type"


# ---------------------------------------------------------------------------
# MINOR 5 — bank_site recovery also tombstones the OLD dead URL's quarantine
# ---------------------------------------------------------------------------

def test_bank_site_recovery_releases_the_dead_urls_quarantine(tmp_path, monkeypatch):
    """reindex() releases rec.document_url on success, which for bank_site IS the
    NEW final_url -- the OLD dead_url would otherwise stay quarantined
    forever even though the corpus now has the doc under its new address."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/old-path/dt989.pdf"
    final_url = "https://www.banque-france.fr/new-path/dt989.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])

    q_path = cfg.data_dir / "download_quarantine.jsonl"
    q_path.parent.mkdir(parents=True, exist_ok=True)
    q_path.write_text(json.dumps({
        "url": dead_url,
        "nights": ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"],
        "quarantined": True,
    }) + "\n")

    local_pdf = _make_local_pdf(tmp_path, "banksite989.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "bank_site", "final_url": final_url,
    }])
    _stub_refresh_metadata(monkeypatch, title="WP 989")

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recovered"] == 1

    q_after = Quarantine(cfg)
    assert q_after.is_quarantined(dead_url) is False


def test_wayback_recovery_dead_url_release_stays_a_noop(tmp_path, monkeypatch):
    """For wayback/mirror, rec.document_url IS dead_url, so reindex() already
    released it -- the extra record_success(dead_url) call must be a
    harmless no-op, never a crash or a double-release error."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/dt990.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path, "wayback990.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback",
        "final_url": "https://web.archive.org/web/20250101000000id_/https://www.banque-france.fr/dt990.pdf",
    }])
    _stub_refresh_metadata(monkeypatch, title="WP 990")

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recovered"] == 1
    q_after = Quarantine(cfg)
    assert q_after.is_quarantined(dead_url) is False


# ---------------------------------------------------------------------------
# MINOR 7 — dry-run reports "duplicate" for an already-indexed candidate too
# (mode parity with --download's dry-run probe)
# ---------------------------------------------------------------------------

def test_dry_run_reports_duplicate_for_already_indexed_doc_id(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/dryrun-dup.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url, title="Dry-run dup WP")])
    local_pdf = _make_local_pdf(tmp_path, "dryrundup.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/dryrundup",
    }])
    _stub_refresh_metadata(monkeypatch, title="Dry-run dup WP")

    # First pass, --download: actually recovers and indexes the doc.
    results1 = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                     candidates=str(cand_path), download=True,
                                     csv_path=str(tmp_path / "r1.csv"))
    assert results1["fr"]["recovered"] == 1

    # Second pass, dry-run (download=False) against the now-converged corpus:
    # must report "converged", not "recoverable" -- the `_is_converged`
    # short-circuit fires before the probe is ever reached, dry-run or not
    # (spec §2: "the corpus already has it" is a different truth than
    # "nothing left to recover").
    results2 = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                     candidates=str(cand_path), download=False,
                                     csv_path=str(tmp_path / "r2.csv"))
    assert results2["fr"]["converged"] == 1
    assert results2["fr"].get("recoverable", 0) == 0

    csv_rows = _csv_rows(tmp_path / "r2.csv")
    assert csv_rows[0]["action"] == "converged"


def test_dry_run_still_reports_recoverable_for_a_new_doc(tmp_path, monkeypatch):
    """Sanity: the dry-run probe doesn't turn EVERY dry-run into 'duplicate'
    -- a genuinely new doc_id still reports 'recoverable'."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/dryrun-new.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url, title="Dry-run new WP")])
    local_pdf = _make_local_pdf(tmp_path, "dryrunnew.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "wayback", "final_url": "https://web.archive.org/dryrunnew",
    }])
    _stub_refresh_metadata(monkeypatch, title="Dry-run new WP")

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=False,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recoverable"] == 1
    assert results["fr"].get("duplicate", 0) == 0
    assert list(iter_manifest_rows(cfg, "fr")) == []


def test_bad_provenance_action(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/badprov.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url)])
    local_pdf = _make_local_pdf(tmp_path)
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "some_unrecognised_source", "final_url": "https://example.org/x",
    }])
    _stub_refresh_metadata(monkeypatch)

    results = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                    candidates=str(cand_path), download=True,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["bad-provenance"] == 1
    assert list(iter_manifest_rows(cfg, "fr")) == []
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "bad-provenance"


# ---------------------------------------------------------------------------
# PR #13 final review, Minor 1 — stamps survive a mid-pass crash
# ---------------------------------------------------------------------------

def test_stamps_applied_even_if_pass_dies_mid_loop(tmp_path, monkeypatch):
    """A SIGKILL/ENOSPC mid-pass would otherwise leave the first candidate's
    stamp collected in memory but never persisted -- try/finally shrinks
    that window to the entry actually being processed when the crash hits.
    Two bank_site duplicate candidates; `Storage.reindex` (the probe call)
    is monkeypatched to raise on the SECOND candidate's probe -- the pass
    propagates that error, but the FIRST candidate's self-heal stamp must
    already be persisted in the manifest."""
    from cb_corpus.recover import run_recover_downloads
    from cb_corpus import storage as storage_mod

    cfg = Config(data_dir=tmp_path)
    dead_url_1 = "https://www.banque-france.fr/old-path/crash-1.pdf"
    final_url_1 = "https://www.banque-france.fr/new-path/crash-1.pdf"
    dead_url_2 = "https://www.banque-france.fr/old-path/crash-2.pdf"
    final_url_2 = "https://www.banque-france.fr/new-path/crash-2.pdf"
    _write_inventory(cfg, [
        _entry(bank="fr", document_url=dead_url_1, title="Crash WP 1"),
        _entry(bank="fr", document_url=dead_url_2, title="Crash WP 2"),
    ])

    # Pre-seed both rows already indexed under their LIVE final_urls -- what
    # a prior bank_site recovery of each doc would have produced.
    seed_storage = Storage(cfg, _NullFetcher())
    seed_rec_1 = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Already here 1",
                           document_url=final_url_1, date=date(2020, 1, 1),
                           mime_type="application/pdf")
    seed_rec_2 = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Already here 2",
                           document_url=final_url_2, date=date(2020, 1, 1),
                           mime_type="application/pdf")
    seed_path_1 = tmp_path / "seed-crash-1.pdf"
    seed_path_1.write_bytes(pdf_body(b"%PDF-1.4 " + b"c" * (25 * 1024)))
    seed_path_2 = tmp_path / "seed-crash-2.pdf"
    seed_path_2.write_bytes(pdf_body(b"%PDF-1.4 " + b"d" * (25 * 1024)))
    assert seed_storage.reindex(seed_rec_1, seed_path_1) == "reindexed"
    assert seed_storage.reindex(seed_rec_2, seed_path_2) == "reindexed"

    local_pdf_1 = _make_local_pdf(tmp_path, "crash-1-local.pdf")
    local_pdf_2 = _make_local_pdf(tmp_path, "crash-2-local.pdf")
    cand_path = _write_candidates(tmp_path, [
        {"dead_document_url": dead_url_1, "file_path": str(local_pdf_1),
         "recovered_from": "bank_site", "final_url": final_url_1},
        {"dead_document_url": dead_url_2, "file_path": str(local_pdf_2),
         "recovered_from": "bank_site", "final_url": final_url_2},
    ])
    _stub_refresh_metadata(monkeypatch, title="Crash WP")

    real_reindex = storage_mod.Storage.reindex
    calls = {"n": 0}

    def _flaky_reindex(self, rec, path, *, dry_run=False):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash mid-loop")
        return real_reindex(self, rec, path, dry_run=dry_run)

    monkeypatch.setattr(storage_mod.Storage, "reindex", _flaky_reindex)

    with pytest.raises(RuntimeError, match="simulated crash mid-loop"):
        run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                              candidates=str(cand_path), download=True,
                              csv_path=str(tmp_path / "r.csv"))

    # The first candidate's stamp must already be persisted despite the
    # crash on the second candidate's probe.
    rows = {r["document_url"]: r for r in iter_manifest_rows(cfg, "fr")}
    assert dead_url_1 in rows[final_url_1]["alt_urls"]
    assert dead_url_2 not in rows[final_url_2]["alt_urls"]


# ---------------------------------------------------------------------------
# PR #13 final review, Minor 1 (prior) / spec §2 -- candidates re-run reports
# converged, matching the CDX-walk pass's vocabulary
# ---------------------------------------------------------------------------

def test_candidates_rerun_over_healed_corpus_reports_converged(tmp_path, monkeypatch):
    """Re-running a --candidates file after a prior pass already self-healed
    it (stamped the dead URL onto the canonical row) must report
    `converged`, not `duplicate` -- "the corpus already has it" (converged)
    is a different truth than "nothing left to recover" (duplicate), and the
    CDX-walk pass already reports the former as such."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    dead_url = "https://www.banque-france.fr/old-path/converge.pdf"
    final_url = "https://www.banque-france.fr/new-path/converge.pdf"
    _write_inventory(cfg, [_entry(bank="fr", document_url=dead_url, title="Converge WP")])

    seed_storage = Storage(cfg, _NullFetcher())
    seed_rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Already here",
                        document_url=final_url, date=date(2020, 1, 1),
                        mime_type="application/pdf")
    seed_path = tmp_path / "seed-converge.pdf"
    seed_path.write_bytes(pdf_body(b"%PDF-1.4 " + b"v" * (25 * 1024)))
    assert seed_storage.reindex(seed_rec, seed_path) == "reindexed"

    q_path = cfg.data_dir / "download_quarantine.jsonl"
    q_path.parent.mkdir(parents=True, exist_ok=True)
    q_path.write_text(json.dumps({"url": dead_url, "nights": ["2026-08-19"],
                                  "quarantined": False}) + "\n")

    local_pdf = _make_local_pdf(tmp_path, "converge-local.pdf")
    cand_path = _write_candidates(tmp_path, [{
        "dead_document_url": dead_url, "file_path": str(local_pdf),
        "recovered_from": "bank_site", "final_url": final_url,
    }])
    _stub_refresh_metadata(monkeypatch, title="Converge WP")

    # First pass: self-heals via the duplicate path (stamps dead_url onto
    # the pre-seeded canonical row, releases quarantine).
    results1 = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                     candidates=str(cand_path), download=True,
                                     csv_path=str(tmp_path / "r1.csv"))
    assert results1["fr"]["duplicate"] == 1
    q_lines_after_first = q_path.read_text().splitlines()
    assert len(q_lines_after_first) == 2   # seeded line + released tombstone

    # Second pass over the SAME file, now-healed corpus: must report
    # converged, not duplicate -- no new stamping, no new quarantine call.
    results2 = run_recover_downloads(config=cfg, fetcher=_NullFetcher(),
                                     candidates=str(cand_path), download=True,
                                     csv_path=str(tmp_path / "r2.csv"))
    assert results2["fr"]["converged"] == 1
    assert results2["fr"].get("duplicate", 0) == 0

    csv_rows2 = _csv_rows(tmp_path / "r2.csv")
    assert csv_rows2[0]["action"] == "converged"

    # No new quarantine line: the release call never happened again.
    q_lines_after_second = q_path.read_text().splitlines()
    assert q_lines_after_second == q_lines_after_first
