"""Tests for `recover-downloads` — inventory-driven Wayback recovery
(cb_corpus/recover.py) and the `latest_capture` CDX helper it relies on
(cb_corpus/sources/wayback.py).

The inventory (`data/download_errors.jsonl`) and the Wayback CDX network are
both faked with tiny stub fetchers — no real HTTP in this suite. Fixtures use
adversarial shapes on purpose: a duplicated pdf_url (dedup-by-latest), an
entry converged only by source_url, a bank filter, a missing inventory file,
a source page fetch that raises, and UTF-8 titles.
"""
from __future__ import annotations

import csv
import json
from datetime import date

import pytest

from cb_corpus.config import Config
from cb_corpus.storage import Storage, iter_manifest_rows


# ---------------------------------------------------------------------------
# latest_capture (sources/wayback.py)
# ---------------------------------------------------------------------------

class _CdxFetcher:
    """get_text -> canned CDX JSON; records every query for shape assertions."""

    def __init__(self, rows):
        self.rows = rows
        self.queries: list[str] = []

    def get_text(self, url):
        self.queries.append(url)
        return json.dumps(self.rows)


def test_latest_capture_parses_and_uses_exact_url_no_prefix():
    from cb_corpus.sources.wayback import latest_capture

    f = _CdxFetcher([["timestamp"], ["20230515000000"]])
    ts = latest_capture(f, "https://www.banque-france.fr/dt986.pdf")
    assert ts == "20230515000000"
    assert len(f.queries) == 1
    q = f.queries[0]
    assert "matchType=prefix" not in q
    assert "url=https://www.banque-france.fr/dt986.pdf" in q
    assert "mimetype:application/pdf" in q


def test_latest_capture_empty_cdx_is_none():
    from cb_corpus.sources.wayback import latest_capture

    f = _CdxFetcher([["timestamp"]])
    assert latest_capture(f, "https://x.example/none.pdf") is None


def test_latest_capture_custom_mimetype():
    from cb_corpus.sources.wayback import latest_capture

    f = _CdxFetcher([["timestamp"], ["20200101000000"]])
    latest_capture(f, "https://x.example/page.html", mimetype="text/html")
    assert "mimetype:text/html" in f.queries[0]


def test_wayback_for_url_still_works_after_refactor():
    """wayback_for_url now delegates to latest_capture -- keep its contract."""
    from cb_corpus.sources.wayback import wayback_for_url

    f = _CdxFetcher([["timestamp"], ["20170811213710"]])
    assert wayback_for_url(f, "http://riksbank.com/upload/993/x.pdf") == (
        "https://web.archive.org/web/20170811213710id_/"
        "http://riksbank.com/upload/993/x.pdf")
    assert wayback_for_url(_CdxFetcher([["timestamp"]]), "http://x/none.pdf") is None


# ---------------------------------------------------------------------------
# run_recover_downloads (cb_corpus/recover.py)
# ---------------------------------------------------------------------------

class _StubFetcher:
    """Configurable fetcher for the full recover-downloads flow.

    - `cdx_hits`: {url_substring: timestamp}. Any CDX query whose target url
      contains one of these substrings returns that timestamp; otherwise the
      CDX query returns an empty resultset.
    - `pages`: {url_substring: html} for non-CDX get_text calls (IDEAS pages).
    - `bytes_ok` / `bytes_fail`: substrings of urls that succeed / raise on
      get_bytes.
    """

    def __init__(self, cdx_hits=None, pages=None, bytes_ok=None, bytes_fail=None):
        self.cdx_hits = cdx_hits or {}
        self.pages = pages or {}
        self.bytes_ok = bytes_ok or {}
        self.bytes_fail = bytes_fail or set()
        self.get_bytes_calls: list[str] = []

    def get_text(self, url):
        if "/cdx/search/cdx" in url:
            for sub, ts in self.cdx_hits.items():
                if sub in url:
                    return json.dumps([["timestamp"], [ts]])
            return json.dumps([["timestamp"]])
        for sub, html in self.pages.items():
            if sub in url:
                return html
        raise RuntimeError(f"fake 404 (get_text): {url}")

    def get_bytes(self, url):
        self.get_bytes_calls.append(url)
        # bytes_ok is checked first: a snapshot URL embeds the original (dead)
        # bank URL as a substring, so a broad bytes_fail entry for the bank
        # host must not shadow the (more specific) archive.org success match.
        for sub, payload in self.bytes_ok.items():
            if sub in url:
                return payload
        for sub in self.bytes_fail:
            if sub in url:
                raise RuntimeError(f"blocked: {url}")
        raise RuntimeError(f"fake 404 (get_bytes): {url}")

    def throttle(self, url):
        pass


def _write_inventory(cfg: Config, entries: list[dict]) -> None:
    path = cfg.data_dir / "download_errors.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def _entry(bank="fr", pdf_url="https://www.banque-france.fr/dt986.pdf",
          title="Document de travail 986", source_url="", doc_type="D1",
          alt_urls=None, ts="2026-01-01T00:00:00+00:00"):
    return {
        "ts": ts, "label": f"{bank}:{doc_type}", "bank_code": bank,
        "doc_type": doc_type, "title": title, "pdf_url": pdf_url,
        "alt_urls": alt_urls or [], "source_url": source_url,
        "error": "HTTPError: 403",
    }


def _csv_rows(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def test_missing_inventory_file_is_a_no_crash_empty_run(tmp_path):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    results = run_recover_downloads(config=cfg,
                                    fetcher=_StubFetcher(), csv_path=str(tmp_path / "r.csv"))
    assert results == {}
    # CSV is still written (empty body, header only) -- never crashes.
    rows = _csv_rows(tmp_path / "r.csv")
    assert rows == []


def test_dedup_keeps_latest_entry_by_pdf_url(tmp_path):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    _write_inventory(cfg, [
        _entry(title="Ancien titre (obsolète)", ts="2026-01-01T00:00:00+00:00"),
        _entry(title="Théorie de l'inflation — note récente",
              ts="2026-02-01T00:00:00+00:00"),
    ])
    fetcher = _StubFetcher()  # no CDX hit -> unrecoverable, but dedup is what we assert
    csv_path = tmp_path / "r.csv"
    run_recover_downloads(config=cfg, fetcher=fetcher, csv_path=str(csv_path))
    rows = _csv_rows(csv_path)
    assert len(rows) == 1  # one pdf_url, not two
    assert rows[0]["title"] == "Théorie de l'inflation — note récente"


def test_banks_filter_restricts_inventory(tmp_path):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    _write_inventory(cfg, [
        _entry(bank="fr", pdf_url="https://www.banque-france.fr/a.pdf"),
        _entry(bank="de", pdf_url="https://www.bundesbank.de/b.pdf"),
    ])
    results = run_recover_downloads(bank_codes=["fr"], config=cfg,
                                    fetcher=_StubFetcher(), csv_path=str(tmp_path / "r.csv"))
    assert "fr" in results
    assert "de" not in results
    rows = _csv_rows(tmp_path / "r.csv")
    assert {r["bank"] for r in rows} == {"fr"}


def test_converged_via_known_pdf_url_skips_network(tmp_path):
    from cb_corpus.recover import run_recover_downloads
    from cb_corpus.models import DocRecord
    from cb_corpus.taxonomy import DocType

    cfg = Config(data_dir=tmp_path)
    url = "https://www.banque-france.fr/already-have.pdf"
    storage = Storage(cfg, _StubFetcher(bytes_ok={"already-have": (b"%PDF-1.4 x", "application/pdf")}))
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Already here",
                    pdf_url=url, date=date(2020, 1, 1), mime_type="application/pdf")
    storage.save(rec)

    _write_inventory(cfg, [_entry(bank="fr", pdf_url=url)])
    results = run_recover_downloads(config=cfg, fetcher=_StubFetcher(),
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["converged"] == 1
    assert results["fr"]["recoverable"] == 0
    rows = _csv_rows(tmp_path / "r.csv")
    assert rows[0]["action"] == "converged"
    assert rows[0]["snapshot_ts"] == ""


def test_converged_via_known_source_url(tmp_path):
    from cb_corpus.recover import run_recover_downloads
    from cb_corpus.models import DocRecord
    from cb_corpus.taxonomy import DocType

    cfg = Config(data_dir=tmp_path)
    source = "https://ideas.repec.org/p/bfr/banfra/981.html"
    storage = Storage(cfg, _StubFetcher(bytes_ok={"x.pdf": (b"%PDF-1.4 x", "application/pdf")}))
    rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Native copy",
                    pdf_url="https://www.banque-france.fr/native-x.pdf",
                    source_url=source, date=date(2020, 1, 1), mime_type="application/pdf")
    storage.save(rec)

    # Same source_url, but a DIFFERENT (dead) pdf_url in the audit entry --
    # the reconciliation already happened, this download attempt is stale.
    _write_inventory(cfg, [_entry(bank="fr",
                                  pdf_url="https://www.banque-france.fr/dead-x.pdf",
                                  source_url=source)])
    results = run_recover_downloads(config=cfg, fetcher=_StubFetcher(),
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["converged"] == 1
    assert results["fr"]["recoverable"] == 0


def test_recoverable_dry_run_never_calls_get_bytes(tmp_path):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    url = "https://www.banque-france.fr/dt986.pdf"
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=url)])
    fetcher = _StubFetcher(cdx_hits={"dt986.pdf": "20250207000000"})
    results = run_recover_downloads(config=cfg, fetcher=fetcher,
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recoverable"] == 1
    assert results["fr"]["recovered"] == 0    # dry-run: never counts as recovered
    assert fetcher.get_bytes_calls == []       # spy: nothing downloaded
    rows = _csv_rows(tmp_path / "r.csv")
    assert rows[0]["action"] == "recoverable"
    assert rows[0]["snapshot_ts"] == "20250207000000"
    assert rows[0]["pdf_url"] == url


def test_unrecoverable_when_no_snapshot_anywhere(tmp_path):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    _write_inventory(cfg, [_entry(bank="fr",
                                  pdf_url="https://www.banque-france.fr/gone.pdf",
                                  alt_urls=["https://econstor.eu/also-gone.pdf"])])
    results = run_recover_downloads(config=cfg, fetcher=_StubFetcher(),
                                    csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["unrecoverable"] == 1
    rows = _csv_rows(tmp_path / "r.csv")
    assert rows[0]["action"] == "unrecoverable"
    assert rows[0]["snapshot_ts"] == ""


def test_csv_columns_shape(tmp_path):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    _write_inventory(cfg, [_entry()])
    run_recover_downloads(config=cfg, fetcher=_StubFetcher(), csv_path=str(tmp_path / "r.csv"))
    with open(tmp_path / "r.csv", newline="") as fh:
        header = next(csv.reader(fh))
    assert header == ["bank", "pdf_url", "action", "snapshot_ts", "title",
                      "canonical_doc_id"]


def test_counts_dict_has_all_five_action_keys(tmp_path):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    _write_inventory(cfg, [_entry()])
    results = run_recover_downloads(config=cfg, fetcher=_StubFetcher(),
                                    csv_path=str(tmp_path / "r.csv"))
    assert set(results["fr"]) == {"recoverable", "recovered", "duplicate",
                                  "unrecoverable", "converged"}


# --- metadata refresh from the IDEAS source page --------------------------

_IDEAS_PAGE = """
<html><head>
<meta name="citation_title" content="Théorie de l'inflation importée">
<meta name="citation_publication_date" content="2025/02">
</head><body>
<input name="url" value="https://www.banque-france.fr/dt986.pdf">
<a href="https://econstor.eu/bitstream/dt986.pdf">alt</a>
</body></html>
"""


def test_metadata_refresh_from_ideas_page_sets_month_precision_repec(tmp_path):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    ideas_url = "https://ideas.repec.org/p/bfr/banfra/981.html"
    pdf_url = "https://www.banque-france.fr/dt986.pdf"
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=pdf_url, source_url=ideas_url,
                                  title="stale audit title")])
    fetcher = _StubFetcher(
        pages={ideas_url: _IDEAS_PAGE},
        cdx_hits={"dt986.pdf": "20250207000000"},
        bytes_ok={"dt986.pdf": (b"%PDF-1.4 official", "application/pdf"),
                 "web.archive.org/web/20250207000000id_": (b"%PDF-1.4 snap", "application/pdf")},
    )
    run_recover_downloads(bank_codes=["fr"], download=True, config=cfg, fetcher=fetcher,
                          csv_path=str(tmp_path / "r.csv"))
    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Théorie de l'inflation importée"
    assert row["date"] == "2025-02-01"
    assert row["date_precision"] == "month"
    assert row["date_source"] == "repec"
    assert row["provenance"] == "wayback"
    assert row["pdf_url"] == pdf_url                       # official URL preserved
    assert row["alt_urls"][0].startswith(
        "https://web.archive.org/web/20250207000000id_/")   # raw snapshot FIRST
    assert row["alt_urls"][0].endswith(pdf_url)


def test_metadata_refresh_falls_back_to_audit_fields_on_fetch_failure(tmp_path):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    ideas_url = "https://ideas.repec.org/p/bfr/banfra/999.html"
    pdf_url = "https://www.banque-france.fr/dt999.pdf"
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=pdf_url, source_url=ideas_url,
                                  title="Titre venant de l'audit")])
    # No `pages` entry for ideas_url -> get_text raises -> refresh fails.
    fetcher = _StubFetcher(
        cdx_hits={"dt999.pdf": "20250101000000"},
        bytes_ok={"dt999.pdf": (b"%PDF-1.4 official", "application/pdf"),
                 "web.archive.org/web/20250101000000id_": (b"%PDF-1.4 snap", "application/pdf")},
    )
    run_recover_downloads(bank_codes=["fr"], download=True, config=cfg, fetcher=fetcher,
                          csv_path=str(tmp_path / "r.csv"))
    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Titre venant de l'audit"   # fallback to the audit line
    assert row["date"] is None                          # undated -- mirrors WaybackSource
    # Undated wayback recovery: no precision, and the (absent) date is
    # attributed to the wayback hunt, never the "bank_site" default (B2).
    assert row["date_precision"] is None
    assert row["date_source"] == "wayback"


# --- --download: fallback chain + recovered accounting ---------------------

def test_download_saves_via_snapshot_fallback_when_official_url_blocked(tmp_path):
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    pdf_url = "https://www.banque-france.fr/wp1002.pdf"
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=pdf_url, title="WP 1002 UTF-8 éè")])
    fetcher = _StubFetcher(
        cdx_hits={"wp1002.pdf": "20240601000000"},
        bytes_fail={"www.banque-france.fr"},    # official host still 403s
        bytes_ok={"web.archive.org/web/20240601000000id_": (b"%PDF-1.4 snap", "application/pdf")},
    )
    results = run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                                    fetcher=fetcher, csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recovered"] == 1
    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1
    row = rows[0]
    assert row["pdf_url"] == pdf_url
    assert row["provenance"] == "wayback"
    assert row["local_path"] and row["sha256"]
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "recovered"


def test_download_all_candidates_fail_stays_recoverable_and_reaudits(tmp_path):
    """A snapshot exists (CDX hit -> recoverable) but BOTH the official URL and
    the archive.org snapshot bytes fail -- e.g. the snapshot itself is a dead
    capture. `save()` raises; the failure must be re-audited into
    download_errors.jsonl (label "recover-downloads") and the entry must
    NEVER be counted/recorded as recovered -- the CSV row stays
    'recoverable', not 'recovered'."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    pdf_url = "https://www.banque-france.fr/wp1003.pdf"
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=pdf_url, title="WP 1003")])
    fetcher = _StubFetcher(
        cdx_hits={"wp1003.pdf": "20240601000000"},
        bytes_fail={"www.banque-france.fr"},    # official host still 403s
        # no bytes_ok entry at all (dropped from the fallback test's setup) --
        # the archive.org snapshot has no configured success either, so it
        # falls through to the stub's generic 404 -- no candidate URL can
        # ever succeed.
    )
    results = run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                                    fetcher=fetcher, csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recoverable"] == 1
    assert results["fr"]["recovered"] == 0
    rows = list(iter_manifest_rows(cfg, "fr"))
    assert rows == []   # nothing saved to the manifest
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "recoverable"   # never 'recovered'

    errors_path = cfg.data_dir / "download_errors.jsonl"
    error_lines = [json.loads(l) for l in errors_path.read_text().splitlines() if l.strip()]
    reaudited = [e for e in error_lines if e.get("label") == "recover-downloads"]
    assert len(reaudited) == 1
    assert reaudited[0]["pdf_url"] == pdf_url


# --- --download bypasses the recovery targets' own quarantine ---------------

def _seed_quarantine_state(cfg: Config, url: str, nights: list[str], quarantined: bool = True) -> None:
    path = cfg.data_dir / "download_quarantine.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "nights": nights, "quarantined": quarantined}) + "\n")


def test_download_bypasses_active_quarantine_and_recovers(tmp_path):
    """`download_errors.jsonl` feeds both the quarantine counter and this
    inventory -- by the time recovery runs, its own target is typically
    already quarantined. --download must reach the real fetch anyway (via
    Storage.save(bypass_quarantine=True)) and report honestly, and a
    successful recovery must release the quarantine too."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    pdf_url = "https://www.banque-france.fr/wp2000.pdf"
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=pdf_url, title="WP 2000")])
    _seed_quarantine_state(cfg, pdf_url,
                           ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"])

    fetcher = _StubFetcher(
        cdx_hits={"wp2000.pdf": "20240601000000"},
        bytes_ok={"wp2000.pdf": (b"%PDF-1.4 official", "application/pdf")},
    )
    results = run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                                    fetcher=fetcher, csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recovered"] == 1
    assert pdf_url in fetcher.get_bytes_calls   # bypass reached the real fetch, not short-circuited
    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "recovered"

    q_path = cfg.data_dir / "download_quarantine.jsonl"
    lines = q_path.read_text().splitlines()
    assert json.loads(lines[-1]) == {"url": pdf_url, "released": True}


def test_download_skip_already_indexed_status_is_reported_as_duplicate(tmp_path, monkeypatch):
    """The other EXACT skip status named by the design (skip:already-indexed)
    must also be folded into 'duplicate', same as skip:duplicate-content. In
    the real flow this doc_id would already have been caught by the
    `_is_converged` short-circuit before ever reaching save() (doc_id is
    derived from pdf_url, so a known doc_id implies a known pdf_url) -- so
    this is exercised directly against the exact-status branch, same as the
    unknown-status test below.

    Because the stubbed doc_id was never really indexed, stamping it back
    onto "its own" row is inherently a no-op (the dead URL IS that row's
    pdf_url) -- what's actually observable here is the quarantine release
    and the canonical_doc_id audit trail in the CSV."""
    from cb_corpus.recover import run_recover_downloads
    from cb_corpus.models import DocRecord
    from cb_corpus.taxonomy import DocType
    from cb_corpus import storage as storage_mod

    cfg = Config(data_dir=tmp_path)
    pdf_url = "https://www.banque-france.fr/wp2500.pdf"
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=pdf_url, title="WP 2500")])
    _seed_quarantine_state(cfg, pdf_url, ["2026-08-19", "2026-08-20"])
    fetcher = _StubFetcher(cdx_hits={"wp2500.pdf": "20240601000000"})

    monkeypatch.setattr(storage_mod.Storage, "save",
                        lambda self, rec, **kw: "skip:already-indexed")

    results = run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                                    fetcher=fetcher, csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["duplicate"] == 1
    assert results["fr"]["recovered"] == 0
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "duplicate"
    expected_doc_id = DocRecord(bank_code="fr", doc_type=DocType.D1, title="WP 2500",
                                pdf_url=pdf_url).doc_id
    assert csv_rows[0]["canonical_doc_id"] == expected_doc_id

    q_lines = (cfg.data_dir / "download_quarantine.jsonl").read_text().splitlines()
    assert json.loads(q_lines[-1]) == {"url": pdf_url, "released": True}


def test_download_legacy_bare_duplicate_status_never_crashes_and_does_not_stamp(tmp_path, monkeypatch):
    """A `skip:duplicate-content` status with NO doc_id suffix (a stubbed
    save(), or some future/legacy caller that never adopted the suffix) must
    be tolerated: still classified 'duplicate' honestly, but with nothing to
    stamp (no doc_id to stamp onto) -- never a crash, never a guess."""
    from cb_corpus.recover import run_recover_downloads
    from cb_corpus import storage as storage_mod

    cfg = Config(data_dir=tmp_path)
    pdf_url = "https://www.banque-france.fr/wp2600.pdf"
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=pdf_url, title="WP 2600")])
    fetcher = _StubFetcher(cdx_hits={"wp2600.pdf": "20240601000000"})

    monkeypatch.setattr(storage_mod.Storage, "save",
                        lambda self, rec, **kw: "skip:duplicate-content")

    results = run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                                    fetcher=fetcher, csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["duplicate"] == 1
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "duplicate"
    assert csv_rows[0]["canonical_doc_id"] == ""
    # No manifest row exists at all (save() was stubbed, never really wrote
    # anything) -- confirms stamp_alt_urls was never even attempted to run
    # against a bogus target.
    assert list(iter_manifest_rows(cfg, "fr")) == []


def test_download_unknown_skip_status_is_reported_verbatim_never_mislabeled(tmp_path, monkeypatch):
    """A skip:* status from storage.save() other than the two named exact
    matches (skip:already-indexed / skip:duplicate-content) must be reported
    honestly AS ITSELF in the CSV action -- never silently folded into
    'duplicate', which would misrepresent what actually happened."""
    from cb_corpus.recover import run_recover_downloads
    from cb_corpus import storage as storage_mod

    cfg = Config(data_dir=tmp_path)
    pdf_url = "https://www.banque-france.fr/wp3000.pdf"
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=pdf_url, title="WP 3000")])
    fetcher = _StubFetcher(cdx_hits={"wp3000.pdf": "20240601000000"})

    monkeypatch.setattr(storage_mod.Storage, "save",
                        lambda self, rec, **kw: "skip:weird-status")

    results = run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                                    fetcher=fetcher, csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recovered"] == 0
    assert results["fr"]["duplicate"] == 0
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "skip:weird-status"


def test_download_missing_doc_type_code_is_skipped_gracefully(tmp_path):
    """An audit entry with an unrecognised doc_type must not crash the run --
    it stays 'recoverable' (not 'recovered'), never breaking the whole pass."""
    from cb_corpus.recover import run_recover_downloads

    cfg = Config(data_dir=tmp_path)
    pdf_url = "https://www.banque-france.fr/wp1.pdf"
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=pdf_url, doc_type="ZZ")])
    fetcher = _StubFetcher(cdx_hits={"wp1.pdf": "20240101000000"})
    results = run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                                    fetcher=fetcher, csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["recoverable"] == 1
    assert results["fr"]["recovered"] == 0


def test_download_skip_status_is_reported_as_duplicate_not_recoverable(tmp_path, capsys):
    """`storage.save()` can come back `skip:duplicate-content` when the
    snapshot's bytes hash-match a document already in the corpus (a real,
    pre-seeded manifest row here) -- nothing is left to recover. Leaving the
    CSV action as 'recoverable' would be a lie (there is nothing recoverable
    left) and would make the entry re-download the full PDF every run just
    to rediscover the same duplicate. The honest action is 'duplicate',
    counted separately from 'recovered'.

    Self-healing (the actual point of this fix): the dead alias URL is
    stamped onto the canonical (seeded) row's alt_urls and its quarantine is
    released, so a re-run of the exact same inventory converges instead of
    rediscovering the same 'duplicate' forever."""
    from cb_corpus.recover import run_recover_downloads
    from cb_corpus.models import DocRecord
    from cb_corpus.taxonomy import DocType

    cfg = Config(data_dir=tmp_path)
    dup_bytes = b"%PDF-1.4 duplicate-body"

    # Pre-seed the corpus with the document under its OWN (still-live) URL.
    seed_storage = Storage(cfg, _StubFetcher(
        bytes_ok={"existing.pdf": (dup_bytes, "application/pdf")}))
    seed_rec = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Existing copy",
                        pdf_url="https://www.banque-france.fr/existing.pdf",
                        date=date(2020, 1, 1), mime_type="application/pdf")
    assert seed_storage.save(seed_rec) == "saved"

    # A DIFFERENT (dead) URL for the exact same content -- not converged by
    # URL or source_url, but its Wayback snapshot's bytes hash-match the doc
    # already saved above (official host still 403s; snapshot succeeds).
    pdf_url = "https://www.banque-france.fr/dt-alias.pdf"
    _write_inventory(cfg, [_entry(bank="fr", pdf_url=pdf_url, title="Alias copy")])
    _seed_quarantine_state(cfg, pdf_url,
                           ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23"])
    fetcher = _StubFetcher(
        cdx_hits={"dt-alias.pdf": "20240101000000"},
        bytes_fail={"www.banque-france.fr"},
        bytes_ok={"web.archive.org/web/20240101000000id_": (dup_bytes, "application/pdf")},
    )
    results = run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                                    fetcher=fetcher, csv_path=str(tmp_path / "r.csv"))
    assert results["fr"]["duplicate"] == 1
    assert results["fr"]["recovered"] == 0
    rows = list(iter_manifest_rows(cfg, "fr"))
    assert len(rows) == 1   # only the seeded row -- nothing new appended
    csv_rows = _csv_rows(tmp_path / "r.csv")
    assert csv_rows[0]["action"] == "duplicate"
    assert csv_rows[0]["canonical_doc_id"] == seed_rec.doc_id

    # The dead alias URL is now stamped onto the canonical (seeded) row.
    assert rows[0]["doc_id"] == seed_rec.doc_id
    assert rows[0]["alt_urls"] == [pdf_url]

    # Quarantine on the dead alias URL is released.
    q_lines = (cfg.data_dir / "download_quarantine.jsonl").read_text().splitlines()
    assert json.loads(q_lines[-1]) == {"url": pdf_url, "released": True}

    stderr = capsys.readouterr().err
    assert "[recover] stamped 1 alt_url(s) on 1 row(s)" in stderr

    # A re-run of the SAME inventory must now converge -- is_known_url(alias)
    # is true via the stamped alt_url -- never rediscover it as 'duplicate'.
    results2 = run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                                     fetcher=fetcher, csv_path=str(tmp_path / "r2.csv"))
    assert results2["fr"]["converged"] == 1
    assert results2["fr"].get("duplicate", 0) == 0
    csv_rows2 = _csv_rows(tmp_path / "r2.csv")
    assert csv_rows2[0]["action"] == "converged"


# ---------------------------------------------------------------------------
# PR #13 final review, Minor 1 -- CDX-walk crash-survival (mirrors
# test_stamps_applied_even_if_pass_dies_mid_loop in test_recover_candidates.py)
# ---------------------------------------------------------------------------

def test_stamps_applied_even_if_cdx_pass_dies_mid_loop(tmp_path, monkeypatch):
    """A SIGKILL/ENOSPC mid-pass would otherwise leave the first entry's
    self-heal stamp collected in memory but never persisted -- the
    try/finally around the CDX-walk loop (recover.py:625-738) shrinks that
    window to the entry actually being processed when the crash hits, same
    as the --candidates pass's try/finally already covers.

    Note on the injection point: unlike --candidates' `Storage.reindex`
    probe, `storage.save()` in THIS loop is deliberately wrapped in its own
    `try/except Exception` (recover.py:672-687, "audited below, never aborts
    the pass") -- a raise from `storage.save` is swallowed into
    status="error" and never escapes the entry, so it cannot exercise a
    genuine mid-loop crash here. `Storage.is_known_url` (called once per
    entry, unguarded, via `_is_converged` at the very top of the loop body)
    is the equivalent unguarded per-entry call for this pass, so that is
    what is monkeypatched to raise on the second entry. `Storage.save` is
    separately monkeypatched (not to raise) so the first entry takes the
    real self-heal/duplicate path and its stamp is actually collected before
    the crash."""
    from cb_corpus.recover import run_recover_downloads
    from cb_corpus.models import DocRecord
    from cb_corpus.taxonomy import DocType
    from cb_corpus import storage as storage_mod

    cfg = Config(data_dir=tmp_path)
    dead_url_1 = "https://www.banque-france.fr/dt-crash-1.pdf"
    dead_url_2 = "https://www.banque-france.fr/dt-crash-2.pdf"
    canonical_url_1 = "https://www.banque-france.fr/existing-crash-1.pdf"

    # Pre-seed the row that entry 1's dead URL will be self-heal-stamped onto.
    seed_storage = Storage(cfg, _StubFetcher(
        bytes_ok={"existing-crash-1.pdf": (b"%PDF-1.4 crash-1", "application/pdf")}))
    seed_rec_1 = DocRecord(bank_code="fr", doc_type=DocType.D1, title="Existing crash 1",
                          pdf_url=canonical_url_1, date=date(2020, 1, 1),
                          mime_type="application/pdf")
    assert seed_storage.save(seed_rec_1) == "saved"

    _write_inventory(cfg, [
        _entry(bank="fr", pdf_url=dead_url_1, title="Crash WP 1"),
        _entry(bank="fr", pdf_url=dead_url_2, title="Crash WP 2"),
    ])
    fetcher = _StubFetcher(cdx_hits={
        "dt-crash-1.pdf": "20240101000000",
        "dt-crash-2.pdf": "20240101000000",
    })

    monkeypatch.setattr(storage_mod.Storage, "save",
                        lambda self, rec, **kw: f"skip:duplicate-content:{seed_rec_1.doc_id}")

    real_is_known_url = storage_mod.Storage.is_known_url
    calls = {"n": 0}

    def _flaky_is_known_url(self, url):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash mid-loop")
        return real_is_known_url(self, url)

    monkeypatch.setattr(storage_mod.Storage, "is_known_url", _flaky_is_known_url)

    with pytest.raises(RuntimeError, match="simulated crash mid-loop"):
        run_recover_downloads(bank_codes=["fr"], download=True, config=cfg,
                              fetcher=fetcher, csv_path=str(tmp_path / "r.csv"))

    # The first entry's self-heal stamp must already be persisted despite the
    # crash while resolving the second entry's `_is_converged` check.
    rows = {r["pdf_url"]: r for r in iter_manifest_rows(cfg, "fr")}
    assert dead_url_1 in rows[canonical_url_1]["alt_urls"]
