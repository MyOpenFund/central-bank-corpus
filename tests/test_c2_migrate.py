"""ECB C2 one-shot enrichment tests (mirrors test_wp_v3.py's wp_migrate fixtures)."""
import csv
import json
from datetime import date
from pathlib import Path

from cb_corpus.config import Config
from cb_corpus.models import DocRecord
from cb_corpus.storage import Storage, iter_manifest_rows
from cb_corpus.taxonomy import DocType
from cb_corpus import c2_migrate
from cb_corpus.c2_migrate import run_c2_migrate, match_key_url


def _write_ecb_manifest(cfg: Config, rows: list[dict]) -> None:
    Storage(cfg)  # ensure dirs exist
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    cfg.manifest_file("ecb").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")


def _read_csv(cfg: Config) -> list[dict]:
    with open(cfg.reports_dir / "c2_migrate.csv", newline="") as fh:
        return list(csv.DictReader(fh))


def test_match_key_url_collapses_double_slash():
    a = "https://www.ecb.europa.eu//press/inter/date/2026/html/ecb.in260715~aa.en.html"
    b = "https://www.ecb.europa.eu/press/inter/date/2026/html/ecb.in260715~aa.en.html"
    assert match_key_url(a) == match_key_url(b)


def test_c2_migrate_stamps_title_and_date(tmp_path, monkeypatch):
    native = [
        DocRecord(bank_code="ecb", doc_type=DocType.C2, title="Interview with Real Outlet",
                  pdf_url="https://www.ecb.europa.eu/press/inter/date/2026/html/"
                          "ecb.in260715~aa.en.html",
                  date=date(2026, 7, 16), provenance="bank_site", mime_type="text/html",
                  date_precision="day", date_source="bank_site"),
    ]
    monkeypatch.setattr(c2_migrate, "discover_ecb_interviews",
                        lambda fetcher, since=None: iter(native))

    cfg = Config(data_dir=tmp_path)
    row = {"doc_id": "id1", "bank_code": "ecb", "doc_type": "C2",
          "title": "ECB C2 2026-07-15",
          "pdf_url": "https://www.ecb.europa.eu/press/inter/date/2026/html/ecb.in260715~aa.en.html",
          "date": "2026-07-16", "date_precision": "month", "date_source": "bank_site"}
    _write_ecb_manifest(cfg, [row])

    run_c2_migrate(cfg, fetcher=object(), write=True)

    after = {r["doc_id"]: r for r in iter_manifest_rows(cfg, "ecb")}
    a = after["id1"]
    assert a["title"] == "Interview with Real Outlet"
    assert a["date"] == "2026-07-16"
    assert a["date_precision"] == "day" and a["date_source"] == "bank_site"
    # identity untouched
    assert a["doc_id"] == "id1" and a["pdf_url"] == row["pdf_url"]

    csv_rows = _read_csv(cfg)
    r1 = next(r for r in csv_rows if r["doc_id"] == "id1")
    assert r1["action"] == "enriched"


def test_c2_migrate_keeps_nongeneric_titles(tmp_path, monkeypatch):
    native = [
        DocRecord(bank_code="ecb", doc_type=DocType.C2, title="Foedb's Own Title",
                  pdf_url="https://www.ecb.europa.eu/press/inter/date/2026/html/"
                          "ecb.in260715~aa.en.html",
                  date=date(2026, 7, 15), provenance="bank_site", mime_type="text/html",
                  date_precision="day", date_source="bank_site"),
    ]
    monkeypatch.setattr(c2_migrate, "discover_ecb_interviews",
                        lambda fetcher, since=None: iter(native))

    cfg = Config(data_dir=tmp_path)
    row = {"doc_id": "id1", "bank_code": "ecb", "doc_type": "C2",
          "title": "A Hand-Picked Non-Generic Title",
          "pdf_url": "https://www.ecb.europa.eu/press/inter/date/2026/html/ecb.in260715~aa.en.html",
          "date": "2026-07-15", "date_precision": "day", "date_source": "bank_site"}
    _write_ecb_manifest(cfg, [row])

    run_c2_migrate(cfg, fetcher=object(), write=True)

    after = {r["doc_id"]: r for r in iter_manifest_rows(cfg, "ecb")}
    a = after["id1"]
    assert a["title"] == "A Hand-Picked Non-Generic Title"   # kept, NOT overwritten

    csv_rows = _read_csv(cfg)
    r1 = next(r for r in csv_rows if r["doc_id"] == "id1")
    assert r1["action"] == "title-diff-kept"
    assert r1["new_title"] == "A Hand-Picked Non-Generic Title"


def test_c2_migrate_matches_double_slash_urls(tmp_path, monkeypatch):
    native = [
        DocRecord(bank_code="ecb", doc_type=DocType.C2, title="Interview X",
                  pdf_url="https://www.ecb.europa.eu/press/inter/date/2019/html/"
                          "ecb.in191216~8014b1bae6.en.html",
                  date=date(2019, 12, 16), provenance="bank_site", mime_type="text/html",
                  date_precision="day", date_source="bank_site"),
    ]
    monkeypatch.setattr(c2_migrate, "discover_ecb_interviews",
                        lambda fetcher, since=None: iter(native))

    cfg = Config(data_dir=tmp_path)
    legacy_url = ("https://www.ecb.europa.eu//press/inter/date/2019/html/"
                 "ecb.in191216~8014b1bae6.en.html")
    row = {"doc_id": "id1", "bank_code": "ecb", "doc_type": "C2",
          "title": "ECB C2 2019-12-16", "pdf_url": legacy_url,
          "date": "2019-12-16", "date_precision": "day", "date_source": "bank_site"}
    _write_ecb_manifest(cfg, [row])

    summary = run_c2_migrate(cfg, fetcher=object(), write=True)
    assert summary["matched"] == 1 and summary["no_match"] == 0

    after = {r["doc_id"]: r for r in iter_manifest_rows(cfg, "ecb")}
    a = after["id1"]
    assert a["title"] == "Interview X"
    assert a["pdf_url"] == legacy_url                          # pdf_url untouched
    normalized_native = "https://www.ecb.europa.eu/press/inter/date/2019/html/ecb.in191216~8014b1bae6.en.html"
    assert normalized_native in a["alt_urls"]                  # clean form registered


def test_c2_migrate_dry_run_touches_nothing(tmp_path, monkeypatch):
    native = [
        DocRecord(bank_code="ecb", doc_type=DocType.C2, title="Real Title",
                  pdf_url="https://www.ecb.europa.eu/press/inter/date/2026/html/"
                          "ecb.in260715~aa.en.html",
                  date=date(2026, 7, 15), provenance="bank_site", mime_type="text/html",
                  date_precision="day", date_source="bank_site"),
    ]
    monkeypatch.setattr(c2_migrate, "discover_ecb_interviews",
                        lambda fetcher, since=None: iter(native))

    cfg = Config(data_dir=tmp_path)
    row = {"doc_id": "id1", "bank_code": "ecb", "doc_type": "C2",
          "title": "ECB C2 2026-07-15",
          "pdf_url": "https://www.ecb.europa.eu/press/inter/date/2026/html/ecb.in260715~aa.en.html",
          "date": "2026-07-15", "date_precision": "month", "date_source": "bank_site"}
    _write_ecb_manifest(cfg, [row])
    before = cfg.manifest_file("ecb").read_text()

    summary = run_c2_migrate(cfg, fetcher=object(), write=False)
    assert summary["enriched"] == 1     # a change WAS proposed...

    after = cfg.manifest_file("ecb").read_text()
    assert after == before              # ...but nothing was written
    # the CSV report is still produced in dry-run mode
    csv_rows = _read_csv(cfg)
    assert csv_rows and csv_rows[0]["doc_id"] == "id1"


def test_c2_migrate_matches_corrupted_url(tmp_path, monkeypatch):
    """A row whose pdf_url carries the ``.en.html/nter/...`` concatenation
    artifact still joins the native foedb record (by the cleaned form) and
    gets the clean URL registered in alt_urls -- pdf_url itself is left
    untouched (same file-on-disk invariant as every other match path)."""
    clean_url = ("https://www.ecb.europa.eu/press/inter/date/2019/html/"
                "ecb.in191216~e43be9798e.en.html")
    native = [
        DocRecord(bank_code="ecb", doc_type=DocType.C2, title="Interview X",
                  pdf_url=clean_url,
                  date=date(2019, 12, 16), provenance="bank_site", mime_type="text/html",
                  date_precision="day", date_source="bank_site"),
    ]
    monkeypatch.setattr(c2_migrate, "discover_ecb_interviews",
                        lambda fetcher, since=None: iter(native))

    cfg = Config(data_dir=tmp_path)
    corrupted_url = clean_url + "/nter/date/2019/html/ecb.in191216~e43be9798e.en.html"
    row = {"doc_id": "id1", "bank_code": "ecb", "doc_type": "C2",
          "title": "ECB C2 2019-12-16", "pdf_url": corrupted_url,
          "date": "2019-12-16", "date_precision": "day", "date_source": "bank_site"}
    _write_ecb_manifest(cfg, [row])

    summary = run_c2_migrate(cfg, fetcher=object(), write=True)
    assert summary["matched"] == 1 and summary["no_match"] == 0

    csv_rows = _read_csv(cfg)
    r1 = next(r for r in csv_rows if r["doc_id"] == "id1")
    assert r1["match_type"] == "url-corrupted"

    after = {r["doc_id"]: r for r in iter_manifest_rows(cfg, "ecb")}
    a = after["id1"]
    assert a["pdf_url"] == corrupted_url                       # pdf_url untouched
    assert clean_url in a["alt_urls"]                          # clean form registered


def test_c2_migrate_matches_known_rehash(tmp_path, monkeypatch):
    """The one documented URL-churn pair: foedb only reports the interview
    under its NEW hash, but the manifest row still carries the OLD hash --
    _KNOWN_REHASH bridges the join and the new-hash URL lands in alt_urls."""
    old_url = ("https://www.ecb.europa.eu/press/inter/date/2019/html/"
              "ecb.in191202~fe0bc873b8.en.html")
    new_url = ("https://www.ecb.europa.eu/press/inter/date/2019/html/"
              "ecb.in191202~869aa1e5ad.en.html")
    native = [
        DocRecord(bank_code="ecb", doc_type=DocType.C2, title="Interview Y",
                  pdf_url=new_url,
                  date=date(2019, 12, 2), provenance="bank_site", mime_type="text/html",
                  date_precision="day", date_source="bank_site"),
    ]
    monkeypatch.setattr(c2_migrate, "discover_ecb_interviews",
                        lambda fetcher, since=None: iter(native))

    cfg = Config(data_dir=tmp_path)
    row = {"doc_id": "id1", "bank_code": "ecb", "doc_type": "C2",
          "title": "ECB C2 2019-12-02", "pdf_url": old_url,
          "date": "2019-12-02", "date_precision": "day", "date_source": "bank_site"}
    _write_ecb_manifest(cfg, [row])

    summary = run_c2_migrate(cfg, fetcher=object(), write=True)
    assert summary["matched"] == 1 and summary["no_match"] == 0

    csv_rows = _read_csv(cfg)
    r1 = next(r for r in csv_rows if r["doc_id"] == "id1")
    assert r1["match_type"] == "url-rehash"

    after = {r["doc_id"]: r for r in iter_manifest_rows(cfg, "ecb")}
    a = after["id1"]
    assert a["pdf_url"] == old_url                              # pdf_url untouched
    assert new_url in a["alt_urls"]                             # new-hash form registered


def test_c2_migrate_non_c2_ecb_rows_pass_through_unchanged(tmp_path, monkeypatch):
    """rewrite_manifest applies keyed updates: only rows whose doc_id is in
    the update set are replaced, every other line is preserved on disk -- so
    a non-C2 ecb row must survive the --write pass byte-for-byte (no schema
    fields injected)."""
    monkeypatch.setattr(c2_migrate, "discover_ecb_interviews",
                        lambda fetcher, since=None: iter([]))

    cfg = Config(data_dir=tmp_path)
    d1_row = {"doc_id": "wpid", "bank_code": "ecb", "doc_type": "D1",
             "title": "Some WP", "pdf_url": "https://www.ecb.europa.eu/pub/pdf/x.pdf",
             "date": "2026-01-01"}
    _write_ecb_manifest(cfg, [d1_row])

    run_c2_migrate(cfg, fetcher=object(), write=True)
    after = {r["doc_id"]: r for r in iter_manifest_rows(cfg, "ecb")}
    assert after["wpid"] == d1_row
