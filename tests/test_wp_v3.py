"""WP v3 first PR: schema, ECB foedb scraper, and migration-join tests.

Pure-helper + JSON-fixture style (mirrors tests/test_framework.py). The foedb
fixtures under tests/fixtures/ are trimmed real responses captured June 2026.
"""
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cb_corpus.config import Config
from cb_corpus.models import DocRecord
from cb_corpus.storage import Storage, iter_manifest_rows
from cb_corpus.taxonomy import DocType
from cb_corpus.sources.ecb_foedb import (
    parse_versions, parse_metadata, chunk_records, wp_op_from_record,
    ecb_wp_number, repec_ecb_number, discover_ecb_wp, FOEDB_DB,
    TYPES_DB, resolve_interview_type_id, discover_ecb_interviews,
)
from cb_corpus import wp_migrate
from cb_corpus.wp_migrate import (
    build_report, match_key_url, repec_handle_from_source_url,
)
from cb_corpus.adapters.ecb import ECBAdapter
from cb_corpus.banks import get_bank
import cb_corpus.sources.ecb_foedb as ecb_foedb

FIX = Path(__file__).parent / "fixtures"


# ---- Phase 0 schema --------------------------------------------------
def test_docrecord_date_quality_defaults():
    rec = DocRecord(bank_code="ecb", doc_type=DocType.D1, title="x",
                    pdf_url="https://x/wp.pdf")
    assert (rec.date_precision, rec.date_source, rec.repec_handle) == ("day", "bank_site", "")


def test_to_row_serializes_alt_urls_and_quality_fields():
    rec = DocRecord(bank_code="ecb", doc_type=DocType.D1, title="x",
                    pdf_url="https://x/wp.pdf", alt_urls=["https://alt/wp.pdf"],
                    date_precision="month", date_source="repec",
                    repec_handle="RePEc:ecb:ecbwps:20253117")
    row = rec.to_row()
    assert row["alt_urls"] == ["https://alt/wp.pdf"]
    assert row["date_precision"] == "month"
    assert row["date_source"] == "repec"
    assert row["repec_handle"] == "RePEc:ecb:ecbwps:20253117"


def test_load_existing_indexes_alt_urls(tmp_path):
    cfg = Config(data_dir=tmp_path)
    Storage(cfg)  # ensures dirs
    row = {"doc_id": "d1", "bank_code": "ecb", "doc_type": "D1",
           "pdf_url": "https://x//pub/wp.pdf", "alt_urls": ["https://native/wp.pdf"],
           "sha256": "abc"}
    cfg.manifest_path.write_text(json.dumps(row) + "\n")
    st = Storage(cfg)
    assert st.is_known_url("https://x//pub/wp.pdf")
    assert st.is_known_url("https://native/wp.pdf")     # alt url recognised
    assert not st.is_known_url("https://unknown/wp.pdf")


def test_load_existing_tolerates_null_alt_urls(tmp_path):
    cfg = Config(data_dir=tmp_path)
    Storage(cfg)
    row = {"doc_id": "d1", "bank_code": "ecb", "doc_type": "D1",
           "pdf_url": "https://x/wp.pdf", "alt_urls": None}
    cfg.manifest_path.write_text(json.dumps(row) + "\n")
    st = Storage(cfg)                                    # must not raise on None
    assert st.is_known_url("https://x/wp.pdf")


def test_rewrite_manifest_is_atomic_and_idempotent(tmp_path):
    cfg = Config(data_dir=tmp_path)
    st = Storage(cfg)
    # Under the keyed-update contract, rewrite_manifest only ever REPLACES
    # rows that already exist on disk -- it never conjures new ones (see
    # apply_row_updates) -- so the manifest must be seeded first.
    seed = [
        {"doc_id": "a", "bank_code": "ecb", "doc_type": "D1",
         "pdf_url": "https://x/a.pdf", "alt_urls": [], "date": "2020-01-02"},
        {"doc_id": "b", "bank_code": "ecb", "doc_type": "D2",
         "pdf_url": "https://x/b.pdf", "alt_urls": [], "date": "2021-03-04"},
        {"doc_id": "c", "bank_code": "ecb", "doc_type": "D1",
         "pdf_url": "https://x/c.pdf", "alt_urls": [], "date": "2022-05-06"},
    ]
    ecb_file = cfg.manifest_file("ecb")
    ecb_file.parent.mkdir(parents=True, exist_ok=True)
    with ecb_file.open("w") as fh:
        for r in seed:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    updates = {
        "a": {**seed[0], "alt_urls": ["https://x/a-alt.pdf"]},
        "b": {**seed[1], "alt_urls": ["https://x/b-alt.pdf"]},
    }
    # "c" is deliberately left out of `updates`: the keyed-contract stand-in
    # for the old test's full-row-set replacement semantics (no longer
    # expressible -- rewrite_manifest never replaces the whole file) is that
    # rows NOT in `updates` survive verbatim.
    assert st.rewrite_manifest(updates) == 2
    # rows go to the per-bank file data/manifest/ecb.jsonl
    on_disk = [json.loads(l) for l in ecb_file.read_text().splitlines() if l.strip()]
    assert on_disk == [updates["a"], updates["b"], seed[2]]
    assert not ecb_file.with_suffix(".jsonl.tmp").exists()
    # dedup indexes refreshed from the new content, incl. alt_urls
    assert st.is_known_url("https://x/a-alt.pdf") and st.is_known_url("https://x/b-alt.pdf")
    # idempotent: re-applying the same updates leaves the file byte-identical
    before = ecb_file.read_text()
    assert st.rewrite_manifest(updates) == 2
    assert ecb_file.read_text() == before
    assert not ecb_file.with_suffix(".jsonl.tmp").exists()


# ---- ECB foedb parsers ----------------------------------------------
def test_parse_versions_and_metadata():
    v, h = parse_versions(json.loads((FIX / "ecb_foedb_versions.json").read_text()))
    assert (v, h) == ("1781277750", "SGNKO2Ue")
    total, size, header = parse_metadata(json.loads((FIX / "ecb_foedb_metadata.json").read_text()))
    assert size == 250 and total >= 1 and "documentTypes" in header and "pub_timestamp" in header


def test_chunk_records_and_wp_op_extraction():
    meta = json.loads((FIX / "ecb_foedb_metadata.json").read_text())
    header = meta["header"]
    flat = json.loads((FIX / "ecb_foedb_chunk0.json").read_text())
    recs = chunk_records(flat, header)
    parsed = [wp_op_from_record(r) for r in recs]
    wp = [p for p in parsed if p and p[0] == DocType.D1]
    op = [p for p in parsed if p and p[0] == DocType.D2]
    other = [p for p in parsed if p is None]
    assert len(wp) == 2 and len(op) == 2 and len(other) == 2     # 2 WP + 2 OP + 2 non-WP
    # the known recent WP: number, exact day, absolute single-slash URL, real title
    wp3244 = next(p for p in wp if p[1] == 3244)
    _dt, number, title, d, pdf_url = wp3244
    assert number == 3244 and d == date(2026, 6, 3) and title
    assert pdf_url == "https://www.ecb.europa.eu/pub/pdf/scpwps/ecb.wp3244~0e92afef7d.en.pdf"


def test_record_date_uses_berlin_not_utc():
    """A local-midnight pub_timestamp (how ECB stores older papers' dates) must
    resolve to the Berlin calendar day, NOT the UTC previous day. ~25% of the
    archive is stored this way; a UTC reading would date them one day early."""
    from zoneinfo import ZoneInfo
    # 15 Jan 2020 00:00 Berlin (CET, +01:00) == 14 Jan 2020 23:00 UTC.
    ts = int(datetime(2020, 1, 15, 0, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp())
    rec = {"pub_timestamp": ts, "publicationProperties": {"Title": "Old WP"},
           "documentTypes": ["/pub/pdf/scpwps/ecbwp1000.pdf"]}
    doc_type, number, _title, d, _url = wp_op_from_record(rec)
    assert (doc_type, number) == (DocType.D1, 1000)
    assert d == date(2020, 1, 15)         # Berlin day, not 2020-01-14


def test_ecb_wp_number_handles_all_url_shapes():
    B = "https://www.ecb.europa.eu//pub/pdf/"
    # hash AFTER the number (modern), and no-hash legacy forms
    assert ecb_wp_number(B + "scpwps/ecb.wp3124~475c.en.pdf") == (DocType.D1, 3124)
    assert ecb_wp_number(B + "scpwps/ecbwp722.pdf") == (DocType.D1, 722)
    assert ecb_wp_number(B + "scpops/ecb.op388.en.pdf") == (DocType.D2, 388)
    assert ecb_wp_number(B + "scpops/ecbocp2.pdf") == (DocType.D2, 2)
    # hash BEFORE the number (the variant that broke the first matcher)
    assert ecb_wp_number(B + "scpwps/ecb~44d02b04fd.wp3181en.pdf") == (DocType.D1, 3181)
    assert ecb_wp_number(B + "scpwps/ecb~0131e2da81.wp2585_en.pdf") == (DocType.D1, 2585)
    assert ecb_wp_number(B + "scpops/ecb~ae799b1df9.op370en.pdf") == (DocType.D2, 370)
    assert ecb_wp_number("https://www.ecb.europa.eu/pub/pdf/other/x.pdf") is None


def test_repec_ecb_number_from_handle_and_source_url():
    assert repec_ecb_number("https://ideas.repec.org/p/ecb/ecbwps/20253124.html") == (DocType.D1, 3124)
    assert repec_ecb_number("https://ideas.repec.org/p/ecb/ecbops/20022.html") == (DocType.D2, 2)
    assert repec_ecb_number("RePEc:ecb:ecbwps:20263244") == (DocType.D1, 3244)
    assert repec_ecb_number("https://ideas.repec.org/p/boe/boeewp/1234.html") is None


class _FakeFetcher:
    def __init__(self, mapping):
        self.mapping = mapping

    def get_text(self, url):
        for suffix, content in self.mapping.items():
            if url.endswith(suffix):
                return content
        raise AssertionError(f"unexpected url {url}")


def _foedb_fetcher():
    return _FakeFetcher({
        "versions.json": (FIX / "ecb_foedb_versions.json").read_text(),
        "metadata.json": (FIX / "ecb_foedb_metadata.json").read_text(),
        "chunk_0.json": (FIX / "ecb_foedb_chunk0.json").read_text(),
    })


def test_discover_ecb_wp_yields_d1_and_d2():
    recs = list(discover_ecb_wp(_foedb_fetcher()))
    assert {r.doc_type for r in recs} == {DocType.D1, DocType.D2}
    assert len(recs) == 4
    for r in recs:
        assert r.provenance == "bank_site"
        assert r.date_precision == "day" and r.date_source == "bank_site"
        assert r.pdf_url.startswith("https://www.ecb.europa.eu/pub/pdf/")


def test_discover_ecb_wp_since_early_stops():
    # since after the newest record -> nothing; since before -> all four.
    assert list(discover_ecb_wp(_foedb_fetcher(), since=date(2030, 1, 1))) == []
    assert len(list(discover_ecb_wp(_foedb_fetcher(), since=date(2000, 1, 1)))) == 4


# ---- ECB C2 interviews (foedb publications_types + type==27 filter) --
class _URLFetcher:
    """Maps exact request URL -> canned text; records every URL asked for so
    tests can assert on what was (not) fetched -- e.g. the early-stop tests."""
    def __init__(self, mapping):
        self.mapping = mapping
        self.requested: list[str] = []

    def get_text(self, url):
        self.requested.append(url)
        if url not in self.mapping:
            raise AssertionError(f"unexpected url {url}")
        return self.mapping[url]


def _types_db_entries(chunk0_flat):
    base = f"{TYPES_DB}/1/th"
    return {
        f"{TYPES_DB}/versions.json": json.dumps([{"version": "1", "hash": "th"}]),
        f"{base}/metadata.json": json.dumps({
            "total_records": len(chunk0_flat) // 2, "chunk_size": 250,
            "header": ["id_publication_type", "publication_name"]}),
        f"{base}/data/0/chunk_0.json": json.dumps(chunk0_flat),
    }


_GOOD_TYPES_CHUNK = [2, "WP Series", 27, "ECB Interview", 226, "The ECB Blog"]
_RENAMED_TYPES_CHUNK = [2, "WP Series", 226, "The ECB Blog"]


def test_resolve_interview_type_id_reads_types_db():
    f = _URLFetcher(_types_db_entries(_GOOD_TYPES_CHUNK))
    assert resolve_interview_type_id(f) == 27


def test_resolve_interview_type_id_fails_loudly_when_renamed():
    f = _URLFetcher(_types_db_entries(_RENAMED_TYPES_CHUNK))
    with pytest.raises(ValueError):
        resolve_interview_type_id(f)


_PUB_HEADER = ["pub_timestamp", "type", "documentTypes", "publicationProperties"]


def _pub_ts(y, m, d, hour=11):
    return int(datetime(y, m, d, hour, 0, tzinfo=ZoneInfo("Europe/Berlin")).timestamp())


def _interview_fetcher(pub_base, flat, extra=None):
    entries = _types_db_entries(_GOOD_TYPES_CHUNK)
    entries.update({
        f"{FOEDB_DB}/versions.json": json.dumps([{"version": "1", "hash": "ph"}]),
        f"{FOEDB_DB}/1/ph/metadata.json": json.dumps({
            "total_records": pub_base["total"], "chunk_size": pub_base["chunk_size"],
            "header": _PUB_HEADER}),
        f"{FOEDB_DB}/1/ph/data/0/chunk_0.json": json.dumps(flat),
    })
    if extra:
        entries.update(extra)
    return _URLFetcher(entries)


def test_discover_ecb_interviews_yields_c2_html_rows(capsys):
    en_interview = [_pub_ts(2026, 8, 24), 27,
                    ["/press/inter/date/2026/html/ecb.in260824~x.en.html"],
                    {"Title": "Interview with X"}]
    wp_record = [_pub_ts(2026, 8, 1), 2,
                ["/pub/pdf/scpwps/ecb.wp1.en.pdf"], {"Title": "WP Title"}]
    non_en_interview = [_pub_ts(2020, 1, 1), 27,
                        ["/press/inter/date/2020/html/ecb.in200101~y.it.html"],
                        {"Title": "Intervista"}]
    flat = en_interview + wp_record + non_en_interview
    f = _interview_fetcher({"total": 3, "chunk_size": 250}, flat)

    docs = list(discover_ecb_interviews(f))
    assert len(docs) == 1
    d = docs[0]
    assert (d.doc_type, d.bank_code) == (DocType.C2, "ecb")
    assert d.pdf_url == "https://www.ecb.europa.eu/press/inter/date/2026/html/ecb.in260824~x.en.html"
    assert d.title == "Interview with X"
    assert str(d.date) == "2026-08-24" and d.date_precision == "day"
    assert d.mime_type == "text/html" and d.provenance == "bank_site"
    assert d.date_source == "bank_site" and d.source_url == FOEDB_DB
    # EN-only exclusion is counted and reported on stderr, never silent.
    assert "1 non-English-only interview(s) excluded" in capsys.readouterr().err


def test_discover_ecb_interviews_silent_when_no_non_en_excluded(capsys):
    en_interview = [_pub_ts(2026, 8, 24), 27,
                    ["/press/inter/date/2026/html/ecb.in260824~x.en.html"],
                    {"Title": "Interview with X"}]
    f = _interview_fetcher({"total": 1, "chunk_size": 250}, en_interview)

    docs = list(discover_ecb_interviews(f))
    assert len(docs) == 1
    assert "excluded" not in capsys.readouterr().err


def test_discover_ecb_interviews_since_early_stop():
    newer = [_pub_ts(2026, 8, 24), 27,
             ["/press/inter/date/2026/html/ecb.in260824~x.en.html"],
             {"Title": "Newer"}]
    older = [_pub_ts(2020, 1, 1), 27,
             ["/press/inter/date/2020/html/ecb.in200101~y.en.html"],
             {"Title": "Older"}]
    flat = newer + older     # both land in chunk_0 (chunk_size=2)
    f = _interview_fetcher({"total": 4, "chunk_size": 2}, flat)

    docs = list(discover_ecb_interviews(f, since=date(2025, 1, 1)))
    assert [d.title for d in docs] == ["Newer"]
    # the older record triggered early-stop inside chunk_0 -> chunk_1 never requested
    assert f"{FOEDB_DB}/1/ph/data/0/chunk_1.json" not in f.requested


# ---- migration helpers + join ---------------------------------------
def test_match_key_url_collapses_slash_and_hash():
    a = "https://www.ecb.europa.eu//pub/pdf/scpwps/ecb.wp3244~0e92afef7d.en.pdf"
    b = "https://www.ecb.europa.eu/pub/pdf/scpwps/ecb.wp3244~deadbeef.en.pdf"
    assert match_key_url(a) == match_key_url(b)


def test_repec_handle_from_source_url():
    assert repec_handle_from_source_url(
        "https://ideas.repec.org/p/ecb/ecbwps/20263244.html") == "RePEc:ecb:ecbwps:20263244"
    assert repec_handle_from_source_url("https://example.com/x") == ""


def test_build_report_join_classifies_and_proposes(monkeypatch):
    native = [
        DocRecord(bank_code="ecb", doc_type=DocType.D1, title="WP 3244",
                  pdf_url="https://www.ecb.europa.eu/pub/pdf/scpwps/ecb.wp3244~aa.en.pdf",
                  date=date(2026, 6, 3)),
        DocRecord(bank_code="ecb", doc_type=DocType.D1, title="WP 3243",
                  pdf_url="https://www.ecb.europa.eu/pub/pdf/scpwps/ecb.wp3243~bb.en.pdf",
                  date=date(2026, 6, 3)),
        DocRecord(bank_code="ecb", doc_type=DocType.D2, title="OP 388",
                  pdf_url="https://www.ecb.europa.eu/pub/pdf/scpops/ecb.op388.en.pdf",
                  date=date(2026, 2, 1)),
    ]
    monkeypatch.setitem(wp_migrate._NATIVE, "ecb", lambda fetcher: iter(native))

    manifest = [
        # A: month row, matches 3244 by key -> proposed change
        {"doc_id": "aaa", "bank_code": "ecb", "doc_type": "D1", "date": "2026-06-01",
         "pdf_url": "https://www.ecb.europa.eu//pub/pdf/scpwps/ecb.wp3244~aa.en.pdf",
         "source_url": "https://ideas.repec.org/p/ecb/ecbwps/20263244.html"},
        # C: already migrated (matches 3243) -> counted already_day, no change
        {"doc_id": "ccc", "bank_code": "ecb", "doc_type": "D1", "date": "2026-06-03",
         "date_precision": "day", "date_source": "bank_site",
         "pdf_url": "https://www.ecb.europa.eu//pub/pdf/scpwps/ecb.wp3243~bb.en.pdf",
         "source_url": "https://ideas.repec.org/p/ecb/ecbwps/20263243.html"},
        # B: legacy OP number 2, no native match -> unmatched
        {"doc_id": "bbb", "bank_code": "ecb", "doc_type": "D2", "date": "2002-02-01",
         "pdf_url": "https://www.ecb.europa.eu//pub/pdf/scpops/ecbocp2.pdf",
         "source_url": "https://ideas.repec.org/p/ecb/ecbops/20022.html"},
        # F: month OP 388 matches -> proposed change
        {"doc_id": "fff", "bank_code": "ecb", "doc_type": "D2", "date": "2026-02-01",
         "pdf_url": "https://www.ecb.europa.eu//pub/pdf/scpops/ecb.op388.en.pdf",
         "source_url": "https://ideas.repec.org/p/ecb/ecbops/2026388.html"},
        # E: different bank -> ignored entirely
        {"doc_id": "eee", "bank_code": "us", "doc_type": "D1", "date": "2020-01-01",
         "pdf_url": "https://federalreserve.gov/x.pdf", "source_url": ""},
    ]

    summary, changes = build_report("ecb", fetcher=None, manifest_rows=manifest)
    assert summary["manifest_total"] == 4          # E (us) excluded
    assert summary["matched_key"] == 3             # A, C, F
    assert summary["matched_url"] == 0
    assert summary["already_day"] == 1             # C
    assert summary["unmatched_manifest"] == 1      # B
    assert summary["native_only"] == 0             # all 3 native keys matched

    by_id = {c["doc_id"]: c for c in changes}
    assert set(by_id) == {"aaa", "fff"}            # only the two month rows change
    a = by_id["aaa"]
    assert a["old_date"] == "2026-06-01" and a["new_date"] == "2026-06-03"
    assert a["match_type"] == "key"
    assert a["repec_handle"] == "RePEc:ecb:ecbwps:20263244"
    assert a["alt_url_added"] == "https://www.ecb.europa.eu/pub/pdf/scpwps/ecb.wp3244~aa.en.pdf"
    # invariant #1: doc_id never changes during migration
    assert a["doc_id"] == "aaa"


def test_ecb_d1_d2_are_native_and_skip_known_urls(monkeypatch):
    """After the flip, ECB D1/D2 discover via foedb (not RePEc), the doc_type
    filter holds, and the pipeline's is_known_url hook skips already-known papers
    (zero re-download of the migrated back-catalogue)."""
    assert DocType.D1 in ECBAdapter.native_types and DocType.D2 in ECBAdapter.native_types
    recs = [
        DocRecord(bank_code="ecb", doc_type=DocType.D1, title="a",
                  pdf_url="https://www.ecb.europa.eu/pub/pdf/scpwps/ecb.wp1.en.pdf"),
        DocRecord(bank_code="ecb", doc_type=DocType.D1, title="b",
                  pdf_url="https://www.ecb.europa.eu/pub/pdf/scpwps/ecb.wp2.en.pdf"),
        DocRecord(bank_code="ecb", doc_type=DocType.D2, title="c",
                  pdf_url="https://www.ecb.europa.eu/pub/pdf/scpops/ecb.op1.en.pdf"),
    ]
    monkeypatch.setattr(ecb_foedb, "discover_ecb_wp",
                        lambda fetcher, since=None: iter(recs))
    ad = ECBAdapter(get_bank("ecb"), fetcher=object())   # fetcher unused (patched)

    # routes to native foedb and filters by doc_type (D1 -> a,b ; not the RePEc path)
    assert [r.title for r in ad.discover(DocType.D1)] == ["a", "b"]
    assert [r.title for r in ad.discover(DocType.D2)] == ["c"]

    # is_known_url hook skips known URLs before download
    ad._skip_known_url = lambda u: u.endswith("wp1.en.pdf")
    assert [r.title for r in ad.discover(DocType.D1)] == ["b"]


# ---- Fed (us) FEDS/IFDP scraper -------------------------------------
from cb_corpus.sources.fed_wp import (
    parse_year_links, parse_landing, fed_key_from_url, fed_key_from_handle,
    discover_fed_wp, FED,
)

_FEDS_YEAR_HTML = """
<div class="heading feds-note" id="2025110">
  <span class="badge badge--feds"><strong>FEDS</strong> 2025-110 </span>
  <div><time datetime="December 2025"> December 2025 </time>
    <h5><a href="/econres/feds/slug-a.htm">Paper A</a></h5></div>
</div>
<div class="heading feds-note" id="2025050">
  <span class="badge badge--feds"><strong>FEDS</strong> 2025-050 </span>
  <div><time datetime="March 2025"> March 2025 </time>
    <h5><a href="/econres/feds/slug-b.htm">Paper B (revised)</a></h5></div>
</div>
"""
# A: landing day falls in the listing month -> day precision.
_LANDING_A = ('<meta name="citation_publication_date" content="12-22-2025" />'
              '<a href="/econres/feds/files/2025110pap.pdf">PDF</a>')
# B: landing date is a later-year revision -> month precision (listing month kept).
_LANDING_B = ('<meta name="citation_publication_date" content="01-15-2026" />'
              '<a href="/econres/feds/files/2025050pap.pdf">PDF</a>')


def test_parse_year_links_and_landing():
    entries = parse_year_links(_FEDS_YEAR_HTML, "feds")
    assert [(e[0], e[1], e[2]) for e in entries] == [("feds", 2025, 110), ("feds", 2025, 50)]
    assert entries[0][3] == date(2025, 12, 1) and entries[0][4].endswith("/slug-a.htm")
    cpd, pdf = parse_landing(_LANDING_A)
    assert cpd == date(2025, 12, 22)
    assert pdf == FED + "/econres/feds/files/2025110pap.pdf"


def test_fed_key_extraction_and_consistency():
    # URL forms (FEDS modern uses NNNpap.pdf; IFDP modern uses files/ifdp{seq}.pdf)
    assert fed_key_from_url(FED + "/econres/feds/files/2022086pap.pdf") == ("feds", 2022, 86)
    assert fed_key_from_url(FED + "/econres/ifdp/files/ifdp1429.pdf") == ("ifdp", 1429)
    assert fed_key_from_url(FED + "/pubs/feds/1997/199711/199711abs.html") == ("feds", 1997, 11)
    assert fed_key_from_url(FED + "/pubs/ifdp/2000/694/ifdp694.pdf") == ("ifdp", 694)
    # revised papers carry an r<N> infix but keep the same number
    assert fed_key_from_url(FED + "/econres/feds/files/2025101r1pap.pdf") == ("feds", 2025, 101)
    assert fed_key_from_url(FED + "/econres/ifdp/files/ifdp1429r2.pdf") == ("ifdp", 1429)
    # 2015-16-era econresdata path (was the 2015-16 coverage gap)
    assert fed_key_from_url(FED + "/econresdata/feds/2016/files/2016104pap.pdf") == ("feds", 2016, 104)
    # handle / IDEAS-path forms
    assert fed_key_from_handle("https://ideas.repec.org/p/fip/fedgfe/2022-82.html") == ("feds", 2022, 82)
    assert fed_key_from_handle("RePEc:fip:fedgfe:95-24") == ("feds", 1995, 24)
    assert fed_key_from_handle("https://ideas.repec.org/p/fip/fedgif/694.html") == ("ifdp", 694)
    assert fed_key_from_handle("https://ideas.repec.org/p/fip/fedgfe/103343.html") is None
    # native PDF key == manifest handle key for the same paper (the join works)
    assert (fed_key_from_url(FED + "/econres/feds/files/2022086pap.pdf")
            == fed_key_from_handle("RePEc:fip:fedgfe:2022-86"))
    assert (fed_key_from_url(FED + "/pubs/ifdp/2000/694/ifdp694.pdf")
            == fed_key_from_handle("RePEc:fip:fedgif:694"))


def test_apply_change_preserves_native_precision():
    """A Fed paper with no confirmed day must migrate to month precision, not be
    mislabeled 'day'. apply_change honours the change's date_precision."""
    from cb_corpus.wp_migrate import apply_change
    row = {"date": "2022-12-01", "pdf_url": "https://x/a.pdf", "alt_urls": []}
    apply_change(row, {"new_date": "2022-12-01", "date_precision": "month",
                       "repec_handle": "RePEc:fip:fedgfe:2022-86",
                       "alt_url_added": "https://y/a.pdf"})
    assert row["date_precision"] == "month" and row["date_source"] == "bank_site"
    assert row["repec_handle"] == "RePEc:fip:fedgfe:2022-86"
    assert "https://y/a.pdf" in row["alt_urls"]


def test_discover_fed_wp_month_constraint(monkeypatch):
    pages = {
        "feds/all-years": '<a href="/econres/feds/2025.htm">2025</a>',
        "ifdp/all-years": "",                       # no IFDP years -> skip
        "feds/2025.htm": _FEDS_YEAR_HTML,
        "feds/slug-a.htm": _LANDING_A,
        "feds/slug-b.htm": _LANDING_B,
    }

    class F:
        def get_text(self, url):
            for k, v in pages.items():
                if k in url:
                    return v
            raise AssertionError(f"unexpected {url}")

    recs = list(discover_fed_wp(F()))
    assert len(recs) == 2 and all(r.bank_code == "us" and r.doc_type == DocType.D1 for r in recs)
    a = next(r for r in recs if "2025110" in r.pdf_url)
    b = next(r for r in recs if "2025050" in r.pdf_url)
    assert a.date == date(2025, 12, 22) and a.date_precision == "day"     # day in listing month
    assert b.date == date(2025, 3, 1) and b.date_precision == "month"     # revision -> month kept
    assert a.date_source == "bank_site" and a.provenance == "bank_site"


# ---- BoJ (jp) Working Paper Series scraper --------------------------
from cb_corpus.sources.boj_wp import parse_wp_table, boj_code, discover_boj_wp

# 5-column modern row + 3-column legacy row (no "No." column), &nbsp; in dates.
_BOJ_2025 = """<table><tbody>
<tr><td>25-E-13</td><td>Nov.&nbsp;13,&nbsp;2025</td><td>A. Author</td>
<td><a href="/en/research/wps_rev/wps_2025/wp25e13.htm">Interest Rate Sensitivity</a></td>
<td><a href="/en/research/wps_rev/wps_2025/data/wp25e13.pdf">[PDF 709KB]</a></td></tr>
</tbody></table>"""
_BOJ_2002 = """<table><tbody>
<tr><td>May&nbsp;15,&nbsp;2003</td>
<td><a href="/en/research/wps_rev/wps_2002/cwp02e08.htm">Time-Varying NAIRU</a></td>
<td><a href="/en/research/wps_rev/wps_2002/data/cwp02e08.pdf">[PDF]</a></td></tr>
</tbody></table>"""


def test_boj_parse_table_both_layouts():
    base = "https://www.boj.or.jp/en/research/wps_rev/wps_2025/index.htm"
    r = parse_wp_table(_BOJ_2025, base)
    assert len(r) == 1
    d, prec, title, pdf = r[0]
    assert d == date(2025, 11, 13) and prec == "day"
    assert title == "Interest Rate Sensitivity"
    assert pdf.endswith("/wps_2025/data/wp25e13.pdf")
    # 3-column legacy row still parsed (date found by content, not column index)
    r2 = parse_wp_table(_BOJ_2002, "https://www.boj.or.jp/en/research/wps_rev/wps_2002/index.htm")
    assert r2[0][0] == date(2003, 5, 15) and r2[0][1] == "day"


def test_boj_code_key_from_url_and_handle():
    assert boj_code("https://www.boj.or.jp/.../wps_2025/data/wp25e13.pdf") == (25, "e", 13)
    assert boj_code("https://ideas.repec.org/p/boj/bojwps/wp25e09.html") == (25, "e", 9)
    assert boj_code("https://ideas.repec.org/p/boj/bojwps/02-e-2r.html") == (2, "e", 2)
    assert boj_code("https://www.boj.or.jp/.../wps_2002/data/cwp02e08.pdf") == (2, "e", 8)
    # native PDF key == manifest handle key for the same paper
    assert (boj_code(".../wps_2002/data/cwp02e02.pdf") == boj_code(".../bojwps/02-e-2.html"))


def test_boj_adapter_routes_d1_native(monkeypatch):
    from cb_corpus.adapters.boj import BoJAdapter
    import cb_corpus.sources.boj_wp as boj_mod
    recs = [DocRecord(bank_code="jp", doc_type=DocType.D1, title="x",
                      pdf_url="https://www.boj.or.jp/en/research/wps_rev/wps_2025/data/wp25e13.pdf")]
    monkeypatch.setattr(boj_mod, "discover_boj_wp", lambda fetcher, since=None: iter(recs))
    ad = BoJAdapter(get_bank("jp"), fetcher=object())
    assert DocType.D1 in ad.native_types and DocType.A3 in ad.native_types
    assert [r.title for r in ad.discover(DocType.D1)] == ["x"]


# ---- BoE (gb) staff working papers ----------------------------------
from cb_corpus.sources.boe_wp import boe_slug, _published_date


def test_boe_published_date_and_slug():
    assert _published_date("<p>Published on 30 May 2025</p>") == date(2025, 5, 30)
    assert _published_date("<p>Published on 7 January 2016</p>") == date(2016, 1, 7)
    assert _published_date("<p>no date</p>") is None
    # slug is identical from the paper page and the media PDF (the join key)
    assert boe_slug("https://x/working-paper/2025/some-slug") == "some-slug"
    assert boe_slug("https://x/-/media/boe/files/working-paper/2025/some-slug.pdf") == "some-slug"
    assert boe_slug("https://x/other") is None


def test_build_report_title_tier_matches_slug_drift(monkeypatch):
    """A BoE paper listed under a different slug than its manifest row still
    matches via the exact-normalized-title tier (key/URL both miss)."""
    from cb_corpus.wp_migrate import normalize_title
    assert normalize_title("ECB-Global:  a Study!") == "ecb global a study"
    native = [DocRecord(bank_code="gb", doc_type=DocType.D1, title="Some BoE Paper: A Study",
                        pdf_url="https://x/-/media/boe/files/working-paper/2025/new-slug.pdf",
                        date=date(2025, 5, 30))]
    monkeypatch.setitem(wp_migrate._NATIVE, "gb", lambda fetcher: iter(native))
    manifest = [{"doc_id": "g1", "bank_code": "gb", "doc_type": "D1", "date": "2025-01-01",
                 "pdf_url": "https://x/-/media/boe/files/working-paper/2025/old-slug.pdf",
                 "source_url": "https://ideas.repec.org/p/boe/boeewp/1116.html",
                 "title": "Some BoE Paper: A Study"}]
    summary, changes = build_report("gb", fetcher=None, manifest_rows=manifest)
    assert summary["matched_title"] == 1 and summary["matched_key"] == 0
    assert changes[0]["match_type"] == "title" and changes[0]["new_date"] == "2025-05-30"


def test_boe_adapter_routes_d1_native(monkeypatch):
    from cb_corpus.adapters.boe import BoEAdapter
    import cb_corpus.sources.boe_wp as boe_mod
    recs = [DocRecord(bank_code="gb", doc_type=DocType.D1, title="x",
                      pdf_url="https://x/-/media/boe/files/working-paper/2025/s.pdf")]
    monkeypatch.setattr(boe_mod, "discover_boe_wp", lambda fetcher, since=None: iter(recs))
    ad = BoEAdapter(get_bank("gb"), fetcher=object())
    assert DocType.D1 in ad.native_types
    assert [r.title for r in ad.discover(DocType.D1)] == ["x"]


# ---- Bundesbank (de) discussion papers ------------------------------
from cb_corpus.sources.buba_wp import parse_de_paper, parse_listing_page, de_blob_key, de_handle_key


def test_de_listing_page_blob_and_slug_items():
    html = (
        '<div class="resultlist__item"><span class="teasable__title">Old Paper</span>'
        '<a href="/resource/blob/1/abcdef01/DEF0/2018-06-04-dkp-14-data.pdf">Open file</a></div>'
        '<div class="resultlist__item"><span class="teasable__title">Recent Paper</span>'
        '<a href="/en/publications/research/discussion-papers/recent-paper-998888">link</a></div>')
    rows = parse_listing_page(html)
    assert len(rows) == 2
    (t0, blob0, page0), (t1, blob1, page1) = rows
    assert t0 == "Old Paper" and blob0.endswith("/2018-06-04-dkp-14-data.pdf") and page0 is None
    assert t1 == "Recent Paper" and blob1 is None and page1.endswith("recent-paper-998888")

_DE_PAPER = (
    '<html><head><meta property="og:title" content="Some DP: A Title"/></head>'
    '<body><a href="/resource/blob/961172/abcdef0123456789/DEF456ABC0/'
    '2026-05-06-dkp-14-data.pdf">Download</a></body></html>')


def test_de_parse_paper_and_keys():
    d, title, pdf = parse_de_paper(_DE_PAPER)
    assert d == date(2026, 5, 6) and title == "Some DP: A Title"
    assert pdf.endswith("/2026-05-06-dkp-14-data.pdf")
    assert de_blob_key(pdf) == (14, 2026)
    # handle key: NN+YYYY -> (num, year); global EconStor id -> None (title tier)
    assert de_handle_key("https://ideas.repec.org/p/zbw/bubdps/032012.html") == (3, 2012)
    assert de_handle_key("https://ideas.repec.org/p/zbw/bubdps/012012e.html") == (1, 2012)
    assert de_handle_key("https://ideas.repec.org/p/zbw/bubdps/337465.html") is None
    # native blob key == manifest handle key for the same DP
    assert de_blob_key(pdf) == de_handle_key("RePEc:zbw:bubdps:142026")


def test_buba_adapter_routes_d1_native(monkeypatch):
    from cb_corpus.adapters.buba import BubaAdapter
    import cb_corpus.sources.buba_wp as buba_mod
    recs = [DocRecord(bank_code="de", doc_type=DocType.D1, title="x",
                      pdf_url="https://www.bundesbank.de/resource/blob/1/a/B/2026-05-06-dkp-14-data.pdf")]
    monkeypatch.setattr(buba_mod, "discover_buba_wp", lambda fetcher, since=None: iter(recs))
    ad = BubaAdapter(get_bank("de"), fetcher=object())
    assert DocType.D1 in ad.native_types
    assert [r.title for r in ad.discover(DocType.D1)] == ["x"]


# ---- Banque de France (fr) wp_migrate wiring -------------------------
from cb_corpus.sources.bdf_wp import fr_wp_number, discover_fr_wp


def test_fr_wired_into_wp_migrate_native_and_key_dicts():
    assert wp_migrate._NATIVE["fr"] is discover_fr_wp
    assert wp_migrate._KEY_FROM_PDF["fr"] is fr_wp_number
    assert wp_migrate._KEY_FROM_HANDLE["fr"] is fr_wp_number


def test_build_report_fr_join_stamps_alt_url_and_upgrades_date(monkeypatch):
    """A fake manifest row (v2, RePEc-dated, month precision, legacy filename
    form) joined against a fake native record (day precision, different --
    modern -- filename form) on the WP number: date upgrades to day, the
    native URL lands in alt_urls, doc_id/pdf_url/sha256 untouched."""
    native = [
        DocRecord(bank_code="fr", doc_type=DocType.D1, title="Some BdF Paper",
                  pdf_url="https://www.banque-france.fr/system/files/2006-03/WP155.pdf",
                  date=date(2006, 3, 14), date_precision="day", date_source="bank_site"),
    ]
    monkeypatch.setitem(wp_migrate._NATIVE, "fr", lambda fetcher: iter(native))

    manifest = [
        {"doc_id": "fr155", "bank_code": "fr", "doc_type": "D1", "date": "2006-03-01",
         "pdf_url": ("https://publications.banque-france.fr/sites/default/files/"
                     "medias/documents/working-paper_155_2006.pdf"),
         "source_url": "https://ideas.repec.org/p/bfr/banfra/155.html",
         "sha256": "h155", "local_path": "/raw/155.pdf"},
    ]
    summary, changes = build_report("fr", fetcher=None, manifest_rows=manifest)
    assert summary["matched_key"] == 1
    assert len(changes) == 1
    c = changes[0]
    assert c["doc_id"] == "fr155"
    assert c["old_date"] == "2006-03-01" and c["new_date"] == "2006-03-14"
    assert c["date_precision"] == "day"
    assert c["match_type"] == "key"
    assert c["repec_handle"] == "RePEc:bfr:banfra:155"     # generic IDEAS-path parse, bank-agnostic
    assert c["alt_url_added"] == "https://www.banque-france.fr/system/files/2006-03/WP155.pdf"

    from cb_corpus.wp_migrate import apply_change
    row = dict(manifest[0])
    apply_change(row, c)
    assert row["date"] == "2006-03-14" and row["date_precision"] == "day"
    assert row["date_source"] == "bank_site"
    assert "https://www.banque-france.fr/system/files/2006-03/WP155.pdf" in row["alt_urls"]
    # identity untouched
    assert row["doc_id"] == "fr155" and row["sha256"] == "h155"
    assert row["pdf_url"] == manifest[0]["pdf_url"]
# ---- Sveriges Riksbank (se) working papers ---------------------------
from cb_corpus.sources.riksbank_wp import (
    parse_wp_listing, se_url_number, se_handle_number, discover_riksbank_wp, RIKSBANK,
)

# Trimmed real markup (captured live, Aug 2026): a modern "no.-NNN-slug.pdf"
# row, a real odd row whose href has NO .pdf extension (still served as a PDF
# on the live site — verified by content-type; never filtered on extension),
# a legacy "wpNNN.pdf" row, an unrelated nav anchor (must be skipped), and a
# row with neither a "No. NNN" title prefix nor a parseable date (defensive
# fallback: title/pdf still captured, number/date come back None).
_SE_LISTING = """
<a href="/en-gb/other/page/">Unrelated nav link</a>
<div class="listing-block__body"><ul>
<li>
<a href="/globalassets/media/rapporter/working-papers/2026/no.-465-the-role-of-firm-heterogeneity-for-the-transmission-of-aggregate-shocks.pdf" target="_blank" rel="noopener">
	<span class="label">29/04/2026</span>
	<span class="header">
		<span class="header--file__title">No. 465 The Role of Firm Heterogeneity for the Transmission of Aggregate Shocks</span>
		<span class="header--file__meta"><span class="icon icon--file icon--file-pdf"><span class="sr-only">pdf</span></span>3.2 MB</span>
	</span>
</a>
</li>
<li>
<a href="/globalassets/media/rapporter/working-papers/2016/no.-317-subprime-borrowers-securitization-and-the-transmission-of-business-cycles" target="_blank" rel="noopener">
	<span class="label">03/03/2016</span>
	<span class="header">
		<span class="header--file__title">No. 317 Subprime Borrowers, Securitization and the Transmission of Business Cycles</span>
		<span class="header--file__meta"><span class="icon icon--file icon--file-pdf"><span class="sr-only">pdf</span></span>2.4 MB</span>
	</span>
</a>
</li>
<li>
<a href="/globalassets/media/rapporter/working-papers/2017/wp344.pdf" target="_blank" rel="noopener">
	<span class="label">12/05/2016</span>
	<span class="header">
		<span class="header--file__title">No. 344 Identification and Estimation issues in Exponential Smooth Transition Autoregressive Models</span>
		<span class="header--file__meta"><span class="icon icon--file icon--file-pdf"><span class="sr-only">pdf</span></span>1.1 MB</span>
	</span>
</a>
</li>
<li>
<a href="/globalassets/media/rapporter/working-papers/2015/special-report.pdf" target="_blank" rel="noopener">
	<span class="label">not-a-date</span>
	<span class="header">
		<span class="header--file__title">Special Report Without A Number Prefix</span>
	</span>
</a>
</li>
</ul></div>
<nav class="pagination"><a href="?&page=2">2</a></nav>
"""


def test_se_parse_listing_number_date_title_and_odd_filename():
    rows = parse_wp_listing(_SE_LISTING)
    assert len(rows) == 4                                 # nav link excluded
    num0, d0, t0, u0 = rows[0]
    assert num0 == 465 and d0 == date(2026, 4, 29)
    assert t0 == "The Role of Firm Heterogeneity for the Transmission of Aggregate Shocks"
    assert u0 == RIKSBANK + ("/globalassets/media/rapporter/working-papers/2026/"
                             "no.-465-the-role-of-firm-heterogeneity-for-the-transmission-of-aggregate-shocks.pdf")
    # real odd row: no .pdf extension, still scraped verbatim (never derived/rewritten)
    num1, d1, t1, u1 = rows[1]
    assert num1 == 317 and d1 == date(2016, 3, 3)
    assert t1 == "Subprime Borrowers, Securitization and the Transmission of Business Cycles"
    assert not u1.endswith(".pdf")
    assert u1.endswith("no.-317-subprime-borrowers-securitization-and-the-transmission-of-business-cycles")
    # legacy filename form
    num2, d2, t2, u2 = rows[2]
    assert num2 == 344 and u2.endswith("/2017/wp344.pdf")
    # defensive fallback: no "No. NNN" prefix / unparseable date -> None, not a crash
    num3, d3, t3, u3 = rows[3]
    assert num3 is None and d3 is None
    assert t3 == "Special Report Without A Number Prefix"
    assert u3.endswith("special-report.pdf")


def test_se_url_number_across_filename_eras():
    from cb_corpus.sources.riksbank_wp import RIKSBANK as R
    assert se_url_number(R + "/globalassets/.../no.-465-a-title.pdf") == 465
    assert se_url_number(R + "/globalassets/.../no-358-predictors-of-bank-distress.pdf") == 358
    assert se_url_number(R + "/globalassets/.../no.355-a-shadow-rate-without-a-lower-bound-constraint") == 355
    assert se_url_number(R + "/globalassets/.../wp344.pdf") == 344
    assert se_url_number(R + "/globalassets/.../rap_wp337_170221.pdf") == 337
    assert se_url_number("http://www.riksbank.se/.../WP312%20Updated%20version.pdf") == 312
    assert se_url_number("http://archive.riksbank.se/.../wp_119.pdf") == 119
    assert se_url_number("http://www.riksbank.com/upload/993/98nr75.pdf") is None   # pre-scheme filename


def test_se_handle_number_from_repec_handle_and_ideas_url():
    assert se_handle_number("https://ideas.repec.org/p/hhs/rbnkwp/0465.html") == 465
    assert se_handle_number("RePEc:hhs:rbnkwp:0179") == 179
    assert se_handle_number("https://ideas.repec.org/p/boe/boeewp/1234.html") is None
    # native URL key == manifest handle key for the same paper
    assert se_url_number("https://www.riksbank.se/.../no.-317-subprime.pdf") == se_handle_number(
        "https://ideas.repec.org/p/hhs/rbnkwp/0317.html")


_SE_PAGE1 = """<div class="listing-block__body"><ul>
<li><a href="/globalassets/media/rapporter/working-papers/2026/no.-465-a.pdf">
<span class="label">29/04/2026</span><span class="header">
<span class="header--file__title">No. 465 Paper A</span></span></a></li>
<li><a href="/globalassets/media/rapporter/working-papers/2026/no.-464-b.pdf">
<span class="label">13/03/2026</span><span class="header">
<span class="header--file__title">No. 464 Paper B</span></span></a></li>
</ul></div>"""
_SE_PAGE2 = """<div class="listing-block__body"><ul>
<li><a href="/globalassets/media/rapporter/working-papers/2025/no.-457-c.pdf">
<span class="label">25/11/2025</span><span class="header">
<span class="header--file__title">No. 457 Paper C</span></span></a></li>
<li><a href="/globalassets/media/rapporter/working-papers/2025/no.-452-d.pdf">
<span class="label">08/07/2025</span><span class="header">
<span class="header--file__title">No. 452 Paper D</span></span></a></li>
</ul></div>"""


def test_discover_riksbank_wp_walks_pages_until_empty():
    pages = {"page=1": _SE_PAGE1, "page=2": _SE_PAGE2, "page=3": ""}

    class F:
        def get_text(self, url):
            for k, v in pages.items():
                if url.endswith(k):
                    return v
            raise AssertionError(f"unexpected {url}")

    recs = list(discover_riksbank_wp(F()))
    assert [r.title for r in recs] == ["Paper A", "Paper B", "Paper C", "Paper D"]
    assert all(r.bank_code == "se" and r.doc_type == DocType.D1
               and r.provenance == "bank_site" and r.date_precision == "day"
               for r in recs)


def test_discover_riksbank_wp_since_stops_before_next_page():
    pages = {"page=1": _SE_PAGE1, "page=2": _SE_PAGE2}

    class F:
        def get_text(self, url):
            for k, v in pages.items():
                if url.endswith(k):
                    return v
            raise AssertionError(f"should not fetch beyond page 2: {url}")   # early-stop proof

    recs = list(discover_riksbank_wp(F(), since=date(2026, 1, 1)))
    assert [r.title for r in recs] == ["Paper A", "Paper B"]     # page 2 all older -> stop, never re-fetched


def test_wp_migrate_se_key_match_registers_alt_url(monkeypatch):
    """The real production gap: a legacy RePEc-discovered se row's pdf_url (the
    old riksbank.se/Documents/... form) differs from the current native listing
    URL for the SAME WP number -> matched by key, day date applied, and the
    native URL registered in alt_urls (zero re-download guard)."""
    native = [DocRecord(
        bank_code="se", doc_type=DocType.D1,
        title="Subprime Borrowers, Securitization and the Transmission of Business Cycles",
        pdf_url=("https://www.riksbank.se/globalassets/media/rapporter/working-papers/2016/"
                 "no.-317-subprime-borrowers-securitization-and-the-transmission-of-business-cycles"),
        date=date(2016, 3, 3))]
    monkeypatch.setitem(wp_migrate._NATIVE, "se", lambda fetcher: iter(native))
    manifest = [{
        "doc_id": "s317", "bank_code": "se", "doc_type": "D1", "date": "2016-03-01",
        "pdf_url": "http://www.riksbank.se/Documents/Rapporter/Working_papers/2016/rap_wp317_160303.pdf",
        "source_url": "https://ideas.repec.org/p/hhs/rbnkwp/0317.html",
    }]
    summary, changes = build_report("se", fetcher=None, manifest_rows=manifest)
    assert summary["matched_key"] == 1 and summary["unmatched_manifest"] == 0
    c = changes[0]
    assert c["match_type"] == "key"
    assert c["old_date"] == "2016-03-01" and c["new_date"] == "2016-03-03"
    assert c["date_precision"] == "day"
    assert c["repec_handle"] == "RePEc:hhs:rbnkwp:0317"
    assert c["alt_url_added"] == native[0].pdf_url          # different URL -> registered


def test_riksbank_adapter_routes_d1_native_and_skips_known_urls(monkeypatch):
    from cb_corpus.adapters.se import RiksbankAdapter
    import cb_corpus.sources.riksbank_wp as se_mod
    recs = [
        DocRecord(bank_code="se", doc_type=DocType.D1, title="a",
                  pdf_url="https://www.riksbank.se/globalassets/.../no.-466-a.pdf"),
        DocRecord(bank_code="se", doc_type=DocType.D1, title="b",
                  pdf_url="https://www.riksbank.se/globalassets/.../no.-467-b.pdf"),
    ]
    monkeypatch.setattr(se_mod, "discover_riksbank_wp", lambda fetcher, since=None: iter(recs))
    ad = RiksbankAdapter(get_bank("se"), fetcher=object())
    assert DocType.D1 in ad.native_types
    assert [r.title for r in ad.discover(DocType.D1)] == ["a", "b"]
    # is_known_url hook (the pipeline's zero-redownload guard) skips known URLs before download
    ad._skip_known_url = lambda u: u.endswith("no.-466-a.pdf")
    assert [r.title for r in ad.discover(DocType.D1)] == ["b"]


# ---- phase 4: wp-dates day recovery ---------------------------------
from cb_corpus import wp_dates
from cb_corpus.wp_dates import month_ok, pdf_creation_date, row_key, recover, _abs_variant


def test_wp_dates_month_constraint():
    row = {"date": "2002-01-01"}
    assert month_ok(date(2002, 1, 29), row) is True
    assert month_ok(date(2002, 1, 1), row) is False     # the 1st = no real day
    assert month_ok(date(2002, 2, 3), row) is False     # different month -> reject
    assert month_ok(None, row) is False


def test_wp_dates_row_key_and_abs_variant():
    assert row_key({"source_url": "https://ideas.repec.org/p/fip/fedgfe/2002-1.html"}) == "RePEc:fip:fedgfe:2002-1"
    assert row_key({"title": "Some Paper"}).startswith("t:")
    assert _abs_variant("https://x/2002/200201/200201pap.pdf").endswith("200201abs.html")


def test_wp_dates_pdf_creation_date(tmp_path):
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4 stuff /CreationDate (D:20020115120000-05'00') more")
    assert pdf_creation_date(str(p)) == date(2002, 1, 15)
    assert pdf_creation_date(str(tmp_path / "missing.pdf")) is None


def test_wp_dates_recover_wayback_month_constrained(monkeypatch):
    # same-month first capture -> day recovered (wayback)
    monkeypatch.setattr(wp_dates, "first_capture", lambda f, u: "20020129141357")
    row = {"date": "2002-01-01", "pdf_url": "https://x/200201pap.pdf", "local_path": None}
    r = recover(object(), row)
    assert r["date"] == "2002-01-29" and r["date_source"] == "wayback"
    assert r["evidence_url"].startswith("https://web.archive.org/web/20020129")
    # capture in a later month (e.g. a digitisation snapshot) -> rejected
    monkeypatch.setattr(wp_dates, "first_capture", lambda f, u: "20130205000000")
    assert recover(object(), {"date": "2002-01-01", "pdf_url": "https://x/200201pap.pdf"}) is None


# ---- phase 2: repec-check ------------------------------------------
from cb_corpus.repec_check import parse_series_listing


def test_repec_check_parse_series_listing():
    html = """
    <a href="/p/ecb/ecbwps/2007722.html">By citations</a>
    <a href="/p/ecb/ecbwps/20263244.html">Fiscal policy transmission through production networks</a>
    <a href="/p/ecb/ecbwps/20263243.html">Herding in the foreign exchange market</a>
    <a href="/p/ecb/ecbwps/20263244.html">By downloads</a>
    <a href="/cgi-bin/other.html">unrelated</a>
    """
    rows = dict(parse_series_listing(html, "ecb", "ecbwps"))
    assert set(rows) == {"2007722", "20263244", "20263243"}     # ids, deduped
    # the real title wins over the 'By downloads' sort link for 20263244
    assert rows["20263244"].startswith("Fiscal policy transmission")
    assert rows["20263243"] == "Herding in the foreign exchange market"


def test_wp_dates_apply_only_replays_index(tmp_path):
    """A fresh clone materialises committed dates with no recovery/network:
    apply-only loads the index and rewrites matching manifest rows."""
    from cb_corpus.wp_dates import index_path, row_key
    cfg = Config(data_dir=tmp_path)
    Storage(cfg)
    row = {"doc_id": "d1", "bank_code": "ecb", "doc_type": "D1", "date": "2010-01-01",
           "date_precision": "month", "date_source": "repec",
           "source_url": "https://ideas.repec.org/p/ecb/ecbwps/2010100.html",
           "title": "X", "pdf_url": "https://x/wp.pdf", "year": 2010}
    cfg.manifest_file("ecb").parent.mkdir(parents=True, exist_ok=True)
    cfg.manifest_file("ecb").write_text(json.dumps(row) + "\n")
    index_path(cfg).write_text(json.dumps({
        "key": row_key(row), "date": "2010-03-15", "date_source": "wayback",
        "date_precision": "day", "evidence_url": "https://web.archive.org/x"}) + "\n")

    wp_dates.run_wp_dates(bank_codes=["ecb"], write=True, apply_only=True, config=cfg)
    after = json.loads(cfg.manifest_file("ecb").read_text().splitlines()[0])
    assert after["date"] == "2010-03-15" and after["date_precision"] == "day"
    assert after["date_source"] == "wayback" and after["year"] == 2010
    assert after["doc_id"] == "d1" and after["pdf_url"] == row["pdf_url"]   # identity intact


def test_run_wp_migrate_write_applies_in_place_and_is_idempotent(tmp_path, monkeypatch):
    native = [
        DocRecord(bank_code="ecb", doc_type=DocType.D1, title="WP 3244",
                  pdf_url="https://www.ecb.europa.eu/pub/pdf/scpwps/ecb.wp3244~aa.en.pdf",
                  date=date(2026, 6, 3)),
        DocRecord(bank_code="ecb", doc_type=DocType.D2, title="OP 388",
                  pdf_url="https://www.ecb.europa.eu/pub/pdf/scpops/ecb.op388.en.pdf",
                  date=date(2026, 2, 10)),
    ]
    monkeypatch.setitem(wp_migrate._NATIVE, "ecb", lambda fetcher: iter(native))

    cfg = Config(data_dir=tmp_path)
    Storage(cfg)  # ensure dirs exist
    rows = [
        {"doc_id": "id3244", "bank_code": "ecb", "doc_type": "D1", "date": "2026-06-01",
         "pdf_url": "https://www.ecb.europa.eu//pub/pdf/scpwps/ecb.wp3244~aa.en.pdf",
         "source_url": "https://ideas.repec.org/p/ecb/ecbwps/20263244.html",
         "sha256": "h1", "local_path": "/raw/3244.pdf"},
        {"doc_id": "id388", "bank_code": "ecb", "doc_type": "D2", "date": "2026-02-01",
         "pdf_url": "https://www.ecb.europa.eu//pub/pdf/scpops/ecb.op388.en.pdf",
         "source_url": "https://ideas.repec.org/p/ecb/ecbops/2026388.html",
         "sha256": "h2", "local_path": "/raw/388.pdf"},
        {"doc_id": "idus", "bank_code": "us", "doc_type": "D1", "date": "2020-01-01",
         "pdf_url": "https://federalreserve.gov/x.pdf", "source_url": ""},
    ]
    cfg.manifest_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    wp_migrate.run_wp_migrate(bank_codes=["ecb"], write=True, config=cfg)
    after = {r["doc_id"]: r for r in iter_manifest_rows(cfg)}
    assert len(after) == 3                                   # no rows lost/gained
    a = after["id3244"]
    assert a["date"] == "2026-06-03"
    assert a["date_precision"] == "day" and a["date_source"] == "bank_site"
    assert a["repec_handle"] == "RePEc:ecb:ecbwps:20263244"
    assert "https://www.ecb.europa.eu/pub/pdf/scpwps/ecb.wp3244~aa.en.pdf" in a["alt_urls"]
    # invariant #1: identity/content/file pointers untouched
    assert a["doc_id"] == "id3244" and a["sha256"] == "h1" and a["local_path"] == "/raw/3244.pdf"
    assert a["pdf_url"] == rows[0]["pdf_url"]
    # non-ECB row passes through byte-for-byte (no schema fields injected)
    assert after["idus"] == rows[2]

    # idempotent: a second --write finds them already migrated and rewrites nothing new
    summary2 = wp_migrate.run_wp_migrate(bank_codes=["ecb"], write=True, config=cfg)
    assert summary2["ecb"]["already_day"] == 2
    after2 = {r["doc_id"]: r for r in iter_manifest_rows(cfg)}
    assert after2 == after
