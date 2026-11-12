"""Disaster recovery: `pipeline.reindex_bis_from_disk` hash-matching path.

The scenario this function exists for is "the manifest was reset/lost while
tens of thousands of downloaded speech PDFs accumulated on disk". The files
are still there, but every bit of metadata that made them findable — the exact
publication date, the source URL — lived only in the manifest. Re-downloading
is not an option (weeks of crawling); instead the function re-derives the join
by recomputing each speech's stable `doc_id = sha1("<bank>|C1|<pdf_url>")[:16]`
from the BIS sitemaps and looking for a file already named that.

That whole path had no test, so nothing pinned the hash basis, the per-bank
manifest destination, or the dry-run contract. The BIS sitemaps here are
hand-written XML in the real schema (a sitemap index pointing at one yearly
`sitemap_documents_YYYY.xml`) — small enough to read, and it is the *matching*
that is under test, not the parser (covered in tests/test_recovery.py).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cb_corpus import pipeline
from cb_corpus.config import Config

SPEECH_URL = "https://www.bis.org/review/r200115a.pdf"
OTHER_URL = "https://www.bis.org/review/r200220b.pdf"
PDF_BYTES = b"%PDF-1.4 recovered-from-disk\n"

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.bis.org/sitemap_documents_2020.xml</loc></sitemap>
</sitemapindex>
"""

YEAR_SITEMAP = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{SPEECH_URL}</loc></url>
  <url><loc>{OTHER_URL}</loc></url>
</urlset>
"""


def _doc_id(bank: str, url: str) -> str:
    """The stable id reindex must recompute: sha1('<bank>|C1|<url>')[:16]."""
    return hashlib.sha1(f"{bank}|C1|{url}".encode("utf-8")).hexdigest()[:16]


def _seed_pdf(tmp_path: Path, bank: str, year: int, doc_id: str) -> Path:
    """Put an already-downloaded, unindexed speech PDF on disk."""
    path = tmp_path / "raw" / bank / "C1" / str(year) / f"{doc_id}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PDF_BYTES)
    return path


def _wire_sitemaps(monkeypatch, fetcher_factory):
    """Serve the two BIS sitemaps to whatever Fetcher the pipeline builds, and
    hand the test the fetcher so it can assert on what was (not) requested."""
    fetcher = fetcher_factory({"sitemap.xml": SITEMAP_INDEX,
                               "sitemap_documents_2020.xml": YEAR_SITEMAP})
    monkeypatch.setattr(pipeline, "Fetcher", lambda cfg: fetcher)
    return fetcher


def test_on_disk_pdf_is_reindexed_by_recomputed_doc_id(tmp_path, monkeypatch,
                                                       fetcher_factory):
    """A PDF on disk whose doc_id is absent from the manifest is matched to its
    BIS sitemap URL by recomputing that hash, and re-registered with the date
    read from the URL slug — no re-download, which is the entire point."""
    cfg = Config(data_dir=tmp_path)
    doc_id = _doc_id("de", SPEECH_URL)
    path = _seed_pdf(tmp_path, "de", 2020, doc_id)
    fetcher = _wire_sitemaps(monkeypatch, fetcher_factory)

    counts = pipeline.reindex_bis_from_disk(dry_run=False, config=cfg)

    # Two XML fetches, and not one byte of PDF re-downloaded.
    assert [c.rsplit("/", 1)[-1] for c in fetcher.calls] == [
        "sitemap.xml", "sitemap_documents_2020.xml"]
    assert counts["missing_on_disk"] == 1
    assert counts["matched"] == 1
    assert counts["reindexed"] == 1
    assert counts["unmatched"] == 0

    # The row lands in THIS bank's manifest file (storage is per-bank).
    rows = [json.loads(ln) for ln in
            (tmp_path / "manifest" / "de.jsonl").read_text().splitlines() if ln.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["doc_id"] == doc_id
    assert row["pdf_url"] == SPEECH_URL
    assert row["source_url"] == "https://www.bis.org/review/r200115a.htm"
    assert row["date"] == "2020-01-15"                  # from the r<YYMMDD> slug
    assert row["bank_code"] == "de" and row["doc_type"] == "C1"
    assert row["provenance"] == "bis_index"
    # The bytes on disk are hashed and adopted, not re-fetched.
    assert row["sha256"] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert Path(row["local_path"]).name == path.name


def test_dry_run_reports_the_match_without_writing_the_manifest(tmp_path, monkeypatch,
                                                                fetcher_factory):
    """Recovery is run in anger on a damaged corpus, so the default must be
    able to answer "what would this do?" without touching the manifest."""
    cfg = Config(data_dir=tmp_path)
    _seed_pdf(tmp_path, "de", 2020, _doc_id("de", SPEECH_URL))
    _wire_sitemaps(monkeypatch, fetcher_factory)

    counts = pipeline.reindex_bis_from_disk(dry_run=True, config=cfg)

    assert counts["matched"] == 1
    assert counts["dry-run"] == 1
    assert counts["reindexed"] == 0
    assert not (tmp_path / "manifest" / "de.jsonl").exists()


def test_a_file_filed_under_another_bank_is_not_matched(tmp_path, monkeypatch,
                                                        fetcher_factory):
    """The recomputed id is bank-scoped (`<bank>|C1|<url>`), and so is the
    match: a file carrying the German id but sitting in the French folder must
    stay unmatched rather than be adopted with the wrong bank_code — a silent
    corpus corruption that no later step would catch."""
    cfg = Config(data_dir=tmp_path)
    _seed_pdf(tmp_path, "fr", 2020, _doc_id("de", SPEECH_URL))
    _wire_sitemaps(monkeypatch, fetcher_factory)

    counts = pipeline.reindex_bis_from_disk(dry_run=False, config=cfg)

    assert counts["missing_on_disk"] == 1
    assert counts["matched"] == 0
    assert counts["unmatched"] == 1
    assert not (tmp_path / "manifest").exists() or not any(
        (tmp_path / "manifest").glob("*.jsonl"))


def test_a_year_outside_the_sitemaps_is_left_alone(tmp_path, monkeypatch,
                                                   fetcher_factory):
    """BIS sitemaps start in 1996; a pre-1996 speech on disk can never be
    matched. It must be counted as unmatched (so the operator sees it) and
    must not make the run fail or fetch a sitemap that does not exist."""
    cfg = Config(data_dir=tmp_path)
    _seed_pdf(tmp_path, "de", 1994, _doc_id("de", SPEECH_URL))
    fetcher = _wire_sitemaps(monkeypatch, fetcher_factory)

    counts = pipeline.reindex_bis_from_disk(dry_run=False, config=cfg)

    assert not any("1994" in c for c in fetcher.calls)   # no sitemap guessed for it
    assert counts["missing_on_disk"] == 1
    assert counts["matched"] == 0
    assert counts["unmatched"] == 1


def test_duplicate_content_file_url_is_stamped_on_the_owner(tmp_path, monkeypatch,
                                                            fetcher_factory):
    """B1: two on-disk files with the same bytes -> the second is a
    duplicate-content skip, and its URL lands on the first row's alt_urls
    (flushed by reindex_bis_from_disk itself, not only by save_many)."""
    cfg = Config(data_dir=tmp_path)
    _seed_pdf(tmp_path, "de", 2020, _doc_id("de", SPEECH_URL))
    _seed_pdf(tmp_path, "de", 2020, _doc_id("de", OTHER_URL))
    _wire_sitemaps(monkeypatch, fetcher_factory)

    counts = pipeline.reindex_bis_from_disk(dry_run=False, config=cfg)

    assert counts["reindexed"] == 1 and counts["skip"] == 1
    rows = [json.loads(ln) for ln in
            (tmp_path / "manifest" / "de.jsonl").read_text().splitlines() if ln.strip()]
    assert len(rows) == 1
    assert rows[0]["pdf_url"] == SPEECH_URL
    assert rows[0]["alt_urls"] == [OTHER_URL]
    assert rows[0]["alt_source_urls"] == ["https://www.bis.org/review/r200220b.htm"]


def test_native_reindex_flushes_duplicate_stamps(tmp_path, monkeypatch):
    """reindex_native_from_disk drives storage.reindex() directly, so it must
    flush the duplicate-content stamps itself (B1)."""
    from cb_corpus.models import DocRecord
    from cb_corpus.taxonomy import DocType

    cfg = Config(data_dir=tmp_path)
    recs = [DocRecord(bank_code="fr", doc_type=DocType.E1, title=n,
                      pdf_url=f"https://www.banque-france.fr/{n}.pdf",
                      source_url=f"https://www.banque-france.fr/{n}.html")
            for n in ("a", "b")]
    for r in recs:
        p = tmp_path / "raw" / "fr" / "E1" / "0" / f"{r.doc_id}.pdf"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PDF_BYTES)

    class _Adapter:
        errors: list = []

        def discover_all(self, scope=None, since=None):
            return iter(recs)
    monkeypatch.setattr(pipeline, "get_adapter", lambda code, fetcher: _Adapter())

    counts = pipeline.reindex_native_from_disk(bank_codes=["fr"], dry_run=False, config=cfg)
    assert counts["reindexed"] == 1
    rows = [json.loads(ln) for ln in
            (tmp_path / "manifest" / "fr.jsonl").read_text().splitlines() if ln.strip()]
    assert len(rows) == 1 and rows[0]["pdf_url"] == recs[0].pdf_url
    assert rows[0]["alt_urls"] == [recs[1].pdf_url]
    assert rows[0]["alt_source_urls"] == [recs[1].source_url]


def test_reindex_flush_failure_never_replaces_the_original_exception(
        tmp_path, monkeypatch, fetcher_factory, capsys):
    """M4: the finally-flush in reindex_bis_from_disk warns on failure; the
    exception that was already propagating is the one the caller sees."""
    from cb_corpus.storage import Storage

    cfg = Config(data_dir=tmp_path)
    _seed_pdf(tmp_path, "de", 2020, _doc_id("de", SPEECH_URL))
    _wire_sitemaps(monkeypatch, fetcher_factory)

    def reindex(self, rec, path, dry_run=False):
        self._pending_dups.append({"url": rec.pdf_url, "source_url": "",
                                   "owner_doc_id": "x"})
        raise RuntimeError("original failure")

    def flush(self, dups, label):
        raise ValueError("stamping broke")
    monkeypatch.setattr(Storage, "reindex", reindex)
    monkeypatch.setattr(Storage, "_flush", flush)
    import pytest
    with pytest.raises(RuntimeError, match="original failure"):
        pipeline.reindex_bis_from_disk(dry_run=False, config=cfg)
    assert "stamping broke" in capsys.readouterr().err
