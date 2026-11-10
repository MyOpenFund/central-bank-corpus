"""repec-reconcile: strict one-shot stamping of IDEAS source_urls onto
uniquely-matched manifest rows (download-failures design,
2026-07-16, §3). Adversarial fixtures on purpose: singleton
vs. duplicated titles, empty vs. non-empty source_url, multi-byte UTF-8
titles (a French accented title, per the house lesson that ASCII-only
fixtures mask bugs)."""
import json

from cb_corpus.config import Config
from cb_corpus.repec_check import (run_reconcile_apply, run_reconcile_propose,
                                   run_repec_reconcile)
from cb_corpus.sources.repec import IDEAS

ARCH, SLUG = "boe", "boeewp"      # real gb series (SERIES["gb"] in sources/repec.py)
BASE = f"{IDEAS}/s/{ARCH}/{SLUG}"


def _series_html(pairs):
    """pairs: [(pid, title)] -> an IDEAS series listing page."""
    links = "".join(
        f'<li><a href="/p/{ARCH}/{SLUG}/{pid}.html">{title}</a></li>'
        for pid, title in pairs
    )
    return f"<html><body><ul>{links}</ul></body></html>"


def _paper_html(title, pdf_url, pub="2001/01"):
    return (
        "<html><head>"
        f'<meta name="citation_title" content="{title}">'
        f'<meta name="citation_publication_date" content="{pub}">'
        "</head><body>"
        f'<a href="{pdf_url}">Download PDF</a>'
        "</body></html>"
    )


def _paper_url(pid):
    return f"{IDEAS}/p/{ARCH}/{SLUG}/{pid}.html"


class FakeFetcher:
    """Serves canned pages; records every URL fetched (spy)."""
    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    def get_text(self, url):
        self.fetched.append(url)
        if url not in self.pages:
            raise RuntimeError(f"404 {url}")
        return self.pages[url]


def _row(doc_id, title, pdf_url, source_url="", alt_urls=None, doc_type="D1"):
    return {
        "bank_code": "gb", "doc_type": doc_type, "title": title,
        "pdf_url": pdf_url, "source_url": source_url,
        "alt_urls": alt_urls or [], "date": "2001-01-01", "year": 2001,
        "language": "en", "provenance": "bank_site",
        "mime_type": "application/pdf", "sha256": "deadbeef",
        "local_path": f"/data/{doc_id}.pdf", "doc_id": doc_id,
        "date_precision": "day", "date_source": "bank_site", "repec_handle": "",
    }


def _write_manifest(cfg, bank, rows):
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.manifest_file(bank)
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _cfg(tmp_path):
    return Config(data_dir=tmp_path / "data")


def _read_rows(cfg, bank):
    path = cfg.manifest_file(bank)
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _read_csv(path):
    import csv
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


# -- Case 1: unique title match, empty source_url ---------------------------

def test_unique_title_match_dry_run_reports_stamp_and_leaves_file_untouched(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("r1", "Stabilité financière",
               "https://www.bankofengland.co.uk/-/media/boe/files/wp/1992/stabilite-financiere.pdf")
    manifest_path = _write_manifest(cfg, "gb", [row])
    before = manifest_path.read_bytes()

    fetcher = FakeFetcher({f"{BASE}.html": _series_html([("0001", "Stabilité financière")])})
    results = run_repec_reconcile(bank_codes=["gb"], write=False,
                                  csv_path=str(tmp_path / "out.csv"),
                                  config=cfg, fetcher=fetcher)

    assert results["gb"] == {"stamped": 1, "ambiguous": 0, "already": 0, "unmatched": 0}
    assert manifest_path.read_bytes() == before   # dry-run: byte-identical


def test_unique_title_match_write_stamps_source_url_and_preserves_other_rows(tmp_path):
    cfg = _cfg(tmp_path)
    row1 = _row("r1", "Stabilité financière",
               "https://www.bankofengland.co.uk/-/media/boe/files/wp/1992/stabilite-financiere.pdf")
    row2 = _row("r2", "Untouched Sibling Row",
               "https://www.bankofengland.co.uk/-/media/boe/files/wp/1993/untouched.pdf",
               source_url="https://www.bankofengland.co.uk/working-paper/1993/untouched")
    _write_manifest(cfg, "gb", [row1, row2])

    fetcher = FakeFetcher({f"{BASE}.html": _series_html([("0001", "Stabilité financière")])})
    results = run_repec_reconcile(bank_codes=["gb"], write=True,
                                  csv_path=str(tmp_path / "out.csv"),
                                  config=cfg, fetcher=fetcher)

    assert results["gb"]["stamped"] == 1
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["r1"]["source_url"] == _paper_url("0001")
    assert rows["r2"] == row2   # OTHER row preserved verbatim


# -- Case 2: two rows share the same normalized title -> ambiguous ----------

def test_duplicated_title_is_ambiguous_and_never_written(tmp_path):
    cfg = _cfg(tmp_path)
    row1 = _row("d1", "Duplicate Study On Growth",
               "https://www.bankofengland.co.uk/-/media/boe/files/wp/1994/dup-a.pdf")
    row2 = _row("d2", "Duplicate Study On Growth",
               "https://www.bankofengland.co.uk/-/media/boe/files/wp/1995/dup-b.pdf")
    _write_manifest(cfg, "gb", [row1, row2])
    before_rows = _read_rows(cfg, "gb")

    fetcher = FakeFetcher({f"{BASE}.html": _series_html([("0002", "Duplicate Study On Growth")])})
    results = run_repec_reconcile(bank_codes=["gb"], write=True,
                                  csv_path=str(tmp_path / "out.csv"),
                                  config=cfg, fetcher=fetcher)

    assert results["gb"] == {"stamped": 0, "ambiguous": 1, "already": 0, "unmatched": 0}
    assert _read_rows(cfg, "gb") == before_rows   # zero writes even with --write


# -- Case 3: matched row already carries a non-empty source_url -------------

def test_matched_row_with_existing_source_url_is_already_and_not_overwritten(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("e1", "Existing Landing Page Paper",
               "https://www.bankofengland.co.uk/-/media/boe/files/wp/1996/existing.pdf",
               source_url="https://www.bankofengland.co.uk/working-paper/1996/existing")
    _write_manifest(cfg, "gb", [row])

    fetcher = FakeFetcher({f"{BASE}.html": _series_html([("0003", "Existing Landing Page Paper")])})
    results = run_repec_reconcile(bank_codes=["gb"], write=True,
                                  csv_path=str(tmp_path / "out.csv"),
                                  config=cfg, fetcher=fetcher)

    assert results["gb"] == {"stamped": 0, "ambiguous": 0, "already": 1, "unmatched": 0}
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["e1"]["source_url"] == "https://www.bankofengland.co.uk/working-paper/1996/existing"


# -- Case 4: listing entry matching nothing -> unmatched, in the CSV --------

def test_no_match_is_unmatched_and_appears_in_csv(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("f1", "Completely Unrelated Row",
               "https://www.bankofengland.co.uk/-/media/boe/files/wp/1997/unrelated.pdf")
    _write_manifest(cfg, "gb", [row])

    pages = {
        f"{BASE}.html": _series_html([("0004", "Nowhere Paper Nobody Owns")]),
        _paper_url("0004"): _paper_html(
            "Nowhere Paper Nobody Owns Full Title",
            "https://www.bankofengland.co.uk/-/media/boe/files/wp/1997/nowhere.pdf"),
    }
    fetcher = FakeFetcher(pages)
    csv_path = str(tmp_path / "out.csv")
    results = run_repec_reconcile(bank_codes=["gb"], write=True,
                                  csv_path=csv_path, config=cfg, fetcher=fetcher)

    assert results["gb"] == {"stamped": 0, "ambiguous": 0, "already": 0, "unmatched": 1}
    csv_rows = _read_csv(csv_path)
    unmatched = [r for r in csv_rows if r["action"] == "unmatched"]
    assert len(unmatched) == 1
    assert unmatched[0]["ideas_url"] == _paper_url("0004")


# -- Case 5: IDEAS URL already a row's source_url -> already, no page fetch -

def test_known_source_url_is_already_with_no_paper_page_fetch(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("g1", "Some Already Reconciled Paper",
               "https://www.bankofengland.co.uk/-/media/boe/files/wp/1998/already.pdf",
               source_url=_paper_url("0005"))
    _write_manifest(cfg, "gb", [row])

    # Deliberately NO page for pid 0005 in the fake fetcher: if the code
    # fetches it despite the known source_url, the fetch raises (404) and
    # the spy assertion below catches it either way.
    fetcher = FakeFetcher({f"{BASE}.html": _series_html([("0005", "Some Title")])})
    results = run_repec_reconcile(bank_codes=["gb"], write=True,
                                  csv_path=str(tmp_path / "out.csv"),
                                  config=cfg, fetcher=fetcher)

    assert results["gb"] == {"stamped": 0, "ambiguous": 0, "already": 1, "unmatched": 0}
    assert _paper_url("0005") not in fetcher.fetched


# -- Case 6: idempotence -----------------------------------------------------

def test_write_twice_second_run_stamps_zero(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("h1", "Idempotence Check Paper",
               "https://www.bankofengland.co.uk/-/media/boe/files/wp/1999/idem.pdf")
    _write_manifest(cfg, "gb", [row])

    pages = {f"{BASE}.html": _series_html([("0006", "Idempotence Check Paper")])}
    first = run_repec_reconcile(bank_codes=["gb"], write=True,
                                csv_path=str(tmp_path / "out1.csv"),
                                config=cfg, fetcher=FakeFetcher(pages))
    assert first["gb"]["stamped"] == 1

    second = run_repec_reconcile(bank_codes=["gb"], write=True,
                                 csv_path=str(tmp_path / "out2.csv"),
                                 config=cfg, fetcher=FakeFetcher(pages))
    assert second["gb"]["stamped"] == 0
    assert second["gb"]["already"] == 1


# -- Case 7: match via second-pass PDF-candidate URL (gb slug-URL banks) ----

def test_second_pass_pdf_candidate_url_match_stamps(tmp_path):
    cfg = _cfg(tmp_path)
    # Title deliberately differs from the listing title (no first-pass match
    # possible for gb: key_handle is always None, so only the second-pass
    # PDF-candidate URL can resolve this).
    row = _row("i1", "Understanding Monetary Transmission In The UK",
               "https://www.bankofengland.co.uk/-/media/boe/files/wp/2000/monetary-transmission.pdf")
    _write_manifest(cfg, "gb", [row])

    pages = {
        f"{BASE}.html": _series_html([("0007", "On Monetary Policy (Old Listing Title)")]),
        _paper_url("0007"): _paper_html(
            "On Monetary Policy (Old Listing Title)",
            "https://www.bankofengland.co.uk/-/media/boe/files/wp/2000/monetary-transmission.pdf"),
    }
    fetcher = FakeFetcher(pages)
    results = run_repec_reconcile(bank_codes=["gb"], write=True,
                                  csv_path=str(tmp_path / "out.csv"),
                                  config=cfg, fetcher=fetcher)

    assert results["gb"] == {"stamped": 1, "ambiguous": 0, "already": 0, "unmatched": 0}
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["i1"]["source_url"] == _paper_url("0007")


# -- Case 8: reverse ambiguity — two listing entries, one manifest row ------

def test_two_listing_entries_matching_one_row_only_first_stamps(tmp_path):
    """Two DIFFERENT listing pids, sharing a normalized title, each uniquely
    resolve (via title) to the SAME single manifest row. Without a claimed-
    doc_id guard, both entries see an empty source_url (the write only
    happens after the whole listing is walked) and both classify as
    ``stamp``, so counts/CSV assert two writes for a doc_id that can only be
    written once (dict last-wins). The reverse-ambiguity rule: the FIRST
    entry stamps, the SECOND is reported ``ambiguous`` (not written)."""
    cfg = _cfg(tmp_path)
    row = _row("j1", "Reverse Ambiguity Paper",
               "https://www.bankofengland.co.uk/-/media/boe/files/wp/2002/reverse.pdf")
    _write_manifest(cfg, "gb", [row])

    pages = {f"{BASE}.html": _series_html([
        ("0008", "Reverse Ambiguity Paper"),
        ("0009", "Reverse Ambiguity Paper"),
    ])}
    fetcher = FakeFetcher(pages)
    csv_path = str(tmp_path / "out.csv")
    results = run_repec_reconcile(bank_codes=["gb"], write=True,
                                  csv_path=csv_path, config=cfg, fetcher=fetcher)

    assert results["gb"] == {"stamped": 1, "ambiguous": 1, "already": 0, "unmatched": 0}

    csv_rows = _read_csv(csv_path)
    stamp_rows = [r for r in csv_rows if r["action"] == "stamp"]
    ambiguous_rows = [r for r in csv_rows if r["action"] == "ambiguous"]
    assert len(stamp_rows) == 1
    assert len(ambiguous_rows) == 1
    assert stamp_rows[0]["ideas_url"] == _paper_url("0008")
    assert ambiguous_rows[0]["ideas_url"] == _paper_url("0009")

    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["j1"]["source_url"] == _paper_url("0008")   # FIRST entry wins


# =============================================================================
# --propose / --apply-csv: human-approved reconciliation for title drift
# (recovery phase-2 design, 2026-07-16, §B2).
# =============================================================================

def _write_propose_csv(path, rows):
    import csv
    fields = ["bank", "ideas_url", "repec_title", "candidate_doc_id",
             "candidate_title", "score", "approve"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({f: r.get(f, "") for f in fields})


# -- propose: ranking, cap at 3, UTF-8 -------------------------------------

def test_propose_ranks_candidates_by_similarity_and_caps_at_three(tmp_path):
    cfg = _cfg(tmp_path)
    repec_title = "La Transmission De La Politique Monétaire"
    rows = [
        _row("c1", "La Transmission De La Politique Monétaire Française",
            "https://www.bankofengland.co.uk/-/media/boe/files/wp/2010/c1.pdf"),
        _row("c2", "La Transmission De La Politique Monétaire En Zone Euro",
            "https://www.bankofengland.co.uk/-/media/boe/files/wp/2010/c2.pdf"),
        _row("c3", "Something About Interest Rates And Growth",
            "https://www.bankofengland.co.uk/-/media/boe/files/wp/2010/c3.pdf"),
        _row("c4", "Completely Different Topic On Housing Markets",
            "https://www.bankofengland.co.uk/-/media/boe/files/wp/2010/c4.pdf"),
    ]
    _write_manifest(cfg, "gb", rows)

    # Listing title doesn't exactly match any manifest row (no cascade match,
    # no key_handle for gb) -> unmatched; no page fetched (no `pages` entry
    # for the paper url) so the second pass fails silently and csv_title
    # falls back to the listing title.
    fetcher = FakeFetcher({f"{BASE}.html": _series_html([("0010", repec_title)])})
    csv_path = str(tmp_path / "propose.csv")
    results = run_reconcile_propose(bank_codes=["gb"], csv_path=csv_path,
                                    config=cfg, fetcher=fetcher)

    assert results["gb"] == {"unmatched": 1}
    proposed = _read_csv(csv_path)
    assert len(proposed) == 3   # capped at 3, c3 (lowest score) excluded
    assert [r["candidate_doc_id"] for r in proposed] == ["c1", "c2", "c4"]
    assert all(r["approve"] == "" for r in proposed)
    assert all(r["repec_title"] == repec_title for r in proposed)
    assert all(r["ideas_url"] == _paper_url("0010") for r in proposed)
    # scores strictly descending, rounded to 3 decimals
    scores = [float(r["score"]) for r in proposed]
    assert scores == sorted(scores, reverse=True)
    assert proposed[0]["score"] == "0.891"
    assert proposed[0]["candidate_title"] == \
        "La Transmission De La Politique Monétaire Française"


def test_propose_zero_candidate_entry_emits_one_row_with_empty_fields(tmp_path):
    cfg = _cfg(tmp_path)
    _write_manifest(cfg, "gb", [])   # empty manifest -> empty candidate pool

    fetcher = FakeFetcher({f"{BASE}.html": _series_html([("0011", "Nobody Home Paper")])})
    csv_path = str(tmp_path / "propose.csv")
    results = run_reconcile_propose(bank_codes=["gb"], csv_path=csv_path,
                                    config=cfg, fetcher=fetcher)

    assert results["gb"] == {"unmatched": 1}
    proposed = _read_csv(csv_path)
    assert len(proposed) == 1
    row = proposed[0]
    assert row["ideas_url"] == _paper_url("0011")
    assert row["repec_title"] == "Nobody Home Paper"
    assert row["candidate_doc_id"] == "" and row["candidate_title"] == "" and row["score"] == ""
    assert row["approve"] == ""


def test_propose_writes_no_manifest_bytes(tmp_path):
    cfg = _cfg(tmp_path)
    rows = [_row("k1", "Some Paper Title",
                "https://www.bankofengland.co.uk/-/media/boe/files/wp/2011/k1.pdf")]
    manifest_path = _write_manifest(cfg, "gb", rows)
    before = manifest_path.read_bytes()

    fetcher = FakeFetcher({f"{BASE}.html": _series_html([("0012", "Totally Unrelated Title")])})
    run_reconcile_propose(bank_codes=["gb"], csv_path=str(tmp_path / "propose.csv"),
                          config=cfg, fetcher=fetcher)

    assert manifest_path.read_bytes() == before   # byte-for-byte: zero writes, ever


# -- apply: stamps exactly the approved pairs ------------------------------

def test_apply_stamps_exactly_the_approved_pairs(tmp_path):
    cfg = _cfg(tmp_path)
    rows = [
        _row("m1", "Approve Me Paper",
            "https://www.bankofengland.co.uk/-/media/boe/files/wp/2012/m1.pdf"),
        _row("m2", "Leave Me Alone Paper",
            "https://www.bankofengland.co.uk/-/media/boe/files/wp/2012/m2.pdf"),
    ]
    manifest_path = _write_manifest(cfg, "gb", rows)

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": _paper_url("0020"), "repec_title": "Approve Me (RePEc)",
         "candidate_doc_id": "m1", "candidate_title": "Approve Me Paper",
         "score": "0.9", "approve": "x"},
        {"bank": "gb", "ideas_url": _paper_url("0021"), "repec_title": "Leave Me (RePEc)",
         "candidate_doc_id": "m2", "candidate_title": "Leave Me Alone Paper",
         "score": "0.9", "approve": ""},
    ])

    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 1, "skipped": 1}

    stamped = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert stamped["m1"]["source_url"] == _paper_url("0020")
    assert stamped["m2"]["source_url"] == ""   # untouched: not approved

    report = _read_csv(str(tmp_path / "report.csv"))
    applied_rows = [r for r in report if r["action"] == "applied"]
    skipped_rows = [r for r in report if r["action"] == "skipped"]
    assert len(applied_rows) == 1 and applied_rows[0]["doc_id"] == "m1"
    assert len(skipped_rows) == 1 and skipped_rows[0]["skip_reason"] == "not-approved"


# -- apply: every skip reason ------------------------------------------------

def test_apply_skip_row_gone(tmp_path):
    cfg = _cfg(tmp_path)
    _write_manifest(cfg, "gb", [])   # doc_id below simply doesn't exist

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": _paper_url("0030"), "repec_title": "Ghost Paper",
         "candidate_doc_id": "ghost1", "candidate_title": "Ghost Paper Copy",
         "score": "0.9", "approve": "x"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 0, "skipped": 1}
    report = _read_csv(str(tmp_path / "report.csv"))
    assert report[0]["skip_reason"] == "row-gone"


def test_apply_skip_source_not_empty(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("n1", "Already Sourced Paper",
              "https://www.bankofengland.co.uk/-/media/boe/files/wp/2013/n1.pdf",
              source_url="https://www.bankofengland.co.uk/working-paper/2013/n1")
    _write_manifest(cfg, "gb", [row])

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": _paper_url("0031"), "repec_title": "Already Sourced (RePEc)",
         "candidate_doc_id": "n1", "candidate_title": "Already Sourced Paper",
         "score": "0.9", "approve": "x"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 0, "skipped": 1}
    report = _read_csv(str(tmp_path / "report.csv"))
    assert report[0]["skip_reason"] == "source-not-empty"
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["n1"]["source_url"] == \
        "https://www.bankofengland.co.uk/working-paper/2013/n1"   # untouched


def test_apply_skip_duplicate_doc_id_first_wins(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("o1", "One Row Two Claims",
              "https://www.bankofengland.co.uk/-/media/boe/files/wp/2014/o1.pdf")
    _write_manifest(cfg, "gb", [row])

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": _paper_url("0040"), "repec_title": "First Claim",
         "candidate_doc_id": "o1", "candidate_title": "One Row Two Claims",
         "score": "0.9", "approve": "x"},
        {"bank": "gb", "ideas_url": _paper_url("0041"), "repec_title": "Second Claim",
         "candidate_doc_id": "o1", "candidate_title": "One Row Two Claims",
         "score": "0.8", "approve": "yes"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 1, "skipped": 1}

    report = _read_csv(str(tmp_path / "report.csv"))
    assert report[0]["action"] == "applied" and report[0]["ideas_url"] == _paper_url("0040")
    assert report[1]["action"] == "skipped" and report[1]["skip_reason"] == "duplicate-doc-id"

    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["o1"]["source_url"] == _paper_url("0040")   # FIRST claim wins


def test_apply_skip_duplicate_ideas_url_first_wins(tmp_path):
    cfg = _cfg(tmp_path)
    rows = [
        _row("p1", "Row P1", "https://www.bankofengland.co.uk/-/media/boe/files/wp/2015/p1.pdf"),
        _row("p2", "Row P2", "https://www.bankofengland.co.uk/-/media/boe/files/wp/2015/p2.pdf"),
    ]
    _write_manifest(cfg, "gb", rows)

    same_ideas_url = _paper_url("0050")
    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": same_ideas_url, "repec_title": "Shared RePEc Entry",
         "candidate_doc_id": "p1", "candidate_title": "Row P1",
         "score": "0.9", "approve": "x"},
        {"bank": "gb", "ideas_url": same_ideas_url, "repec_title": "Shared RePEc Entry",
         "candidate_doc_id": "p2", "candidate_title": "Row P2",
         "score": "0.8", "approve": "x"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 1, "skipped": 1}

    report = _read_csv(str(tmp_path / "report.csv"))
    assert report[0]["action"] == "applied" and report[0]["doc_id"] == "p1"
    assert report[1]["action"] == "skipped" and report[1]["skip_reason"] == "duplicate-ideas-url"

    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["p1"]["source_url"] == same_ideas_url
    assert rows["p2"]["source_url"] == ""   # second claim never written


def test_apply_skip_already_known_ideas_url_from_a_prior_stamp(tmp_path):
    """An ideas_url already used as a DIFFERENT row's source_url from BEFORE
    this apply run (e.g. an earlier phase-1 or apply-csv pass) must also be
    refused -- same accurate reason (duplicate-ideas-url), even though the
    duplication isn't within this CSV."""
    cfg = _cfg(tmp_path)
    already_claimed_url = _paper_url("0060")
    rows = [
        _row("q1", "Already Stamped Elsewhere",
            "https://www.bankofengland.co.uk/-/media/boe/files/wp/2016/q1.pdf",
            source_url=already_claimed_url),
        _row("q2", "New Candidate Row",
            "https://www.bankofengland.co.uk/-/media/boe/files/wp/2016/q2.pdf"),
    ]
    _write_manifest(cfg, "gb", rows)

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": already_claimed_url, "repec_title": "Reused RePEc Entry",
         "candidate_doc_id": "q2", "candidate_title": "New Candidate Row",
         "score": "0.9", "approve": "x"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 0, "skipped": 1}
    report = _read_csv(str(tmp_path / "report.csv"))
    assert report[0]["skip_reason"] == "duplicate-ideas-url"
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["q2"]["source_url"] == ""


def test_apply_not_approved_tokens_case_and_space_insensitive(tmp_path):
    cfg = _cfg(tmp_path)
    rows = [_row(f"r{i}", f"Row {i}",
                f"https://www.bankofengland.co.uk/-/media/boe/files/wp/2017/r{i}.pdf")
           for i in range(1, 6)]
    _write_manifest(cfg, "gb", rows)

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": _paper_url("0070"), "repec_title": "t",
         "candidate_doc_id": "r1", "candidate_title": "Row 1", "score": "0.9", "approve": " X "},
        {"bank": "gb", "ideas_url": _paper_url("0071"), "repec_title": "t",
         "candidate_doc_id": "r2", "candidate_title": "Row 2", "score": "0.9", "approve": "Yes"},
        {"bank": "gb", "ideas_url": _paper_url("0072"), "repec_title": "t",
         "candidate_doc_id": "r3", "candidate_title": "Row 3", "score": "0.9", "approve": "OUI"},
        {"bank": "gb", "ideas_url": _paper_url("0073"), "repec_title": "t",
         "candidate_doc_id": "r4", "candidate_title": "Row 4", "score": "0.9", "approve": "1"},
        {"bank": "gb", "ideas_url": _paper_url("0074"), "repec_title": "t",
         "candidate_doc_id": "r5", "candidate_title": "Row 5", "score": "0.9", "approve": "no"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 4, "skipped": 1}
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    for i in range(1, 5):
        assert rows[f"r{i}"]["source_url"] == _paper_url(f"007{i - 1}")
    assert rows["r5"]["source_url"] == ""


# -- apply: dry-run purity and idempotence -----------------------------------

def test_apply_without_write_touches_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("s1", "Dry Run Candidate",
              "https://www.bankofengland.co.uk/-/media/boe/files/wp/2018/s1.pdf")
    manifest_path = _write_manifest(cfg, "gb", [row])
    before = manifest_path.read_bytes()

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": _paper_url("0080"), "repec_title": "Dry Run RePEc",
         "candidate_doc_id": "s1", "candidate_title": "Dry Run Candidate",
         "score": "0.9", "approve": "x"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=False,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 1, "skipped": 0}   # report says it WOULD apply...
    assert manifest_path.read_bytes() == before      # ...but nothing is touched
    report = _read_csv(str(tmp_path / "report.csv"))
    assert report[0]["action"] == "applied"


def test_apply_idempotent_reapply_is_all_already_ish_skips(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("t1", "Idempotent Apply Candidate",
              "https://www.bankofengland.co.uk/-/media/boe/files/wp/2019/t1.pdf")
    _write_manifest(cfg, "gb", [row])

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": _paper_url("0090"), "repec_title": "Idempotent RePEc",
         "candidate_doc_id": "t1", "candidate_title": "Idempotent Apply Candidate",
         "score": "0.9", "approve": "x"},
    ])
    first = run_reconcile_apply(str(apply_csv), write=True,
                                csv_path=str(tmp_path / "report1.csv"),
                                config=cfg, fetcher=FakeFetcher({}))
    assert first == {"applied": 1, "skipped": 0}

    second = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report2.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert second == {"applied": 0, "skipped": 1}
    report2 = _read_csv(str(tmp_path / "report2.csv"))
    assert report2[0]["skip_reason"] == "source-not-empty"


# =============================================================================
# Fix wave (task review): the human-edited apply CSV is UNTRUSTED input.
# =============================================================================

# -- C1: ideas_url shape guard ------------------------------------------------

def test_apply_skip_bad_ideas_url_arbitrary_string(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("u1", "Row U1", "https://www.bankofengland.co.uk/-/media/boe/files/wp/2020/u1.pdf")
    _write_manifest(cfg, "gb", [row])

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": "not-a-url-at-all", "repec_title": "t",
         "candidate_doc_id": "u1", "candidate_title": "Row U1",
         "score": "0.9", "approve": "x"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 0, "skipped": 1}
    report = _read_csv(str(tmp_path / "report.csv"))
    assert report[0]["skip_reason"] == "bad-ideas-url"
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["u1"]["source_url"] == ""


def test_apply_skip_bad_ideas_url_empty_string(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("v1", "Row V1", "https://www.bankofengland.co.uk/-/media/boe/files/wp/2020/v1.pdf")
    _write_manifest(cfg, "gb", [row])

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": "", "repec_title": "t",
         "candidate_doc_id": "v1", "candidate_title": "Row V1",
         "score": "0.9", "approve": "x"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 0, "skipped": 1}
    report = _read_csv(str(tmp_path / "report.csv"))
    assert report[0]["skip_reason"] == "bad-ideas-url"
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["v1"]["source_url"] == ""


def test_apply_valid_ideas_url_still_applies(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("v2", "Row V2", "https://www.bankofengland.co.uk/-/media/boe/files/wp/2020/v2.pdf")
    _write_manifest(cfg, "gb", [row])

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": _paper_url("0115"), "repec_title": "t",
         "candidate_doc_id": "v2", "candidate_title": "Row V2",
         "score": "0.9", "approve": "x"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 1, "skipped": 0}
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["v2"]["source_url"] == _paper_url("0115")


def test_apply_reapply_of_previously_empty_ideas_url_converges_to_skips(tmp_path):
    """Before the C1 fix, an empty ideas_url passed every guard and got
    stamped as source_url="" (falsy), so a re-apply saw source_url still
    empty and stamped AGAIN -- never converging. The bad-ideas-url guard
    fires identically on every run, so both runs skip the same way."""
    cfg = _cfg(tmp_path)
    row = _row("v3", "Row V3", "https://www.bankofengland.co.uk/-/media/boe/files/wp/2020/v3.pdf")
    _write_manifest(cfg, "gb", [row])

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": "", "repec_title": "t",
         "candidate_doc_id": "v3", "candidate_title": "Row V3",
         "score": "0.9", "approve": "x"},
    ])
    first = run_reconcile_apply(str(apply_csv), write=True,
                                csv_path=str(tmp_path / "report1.csv"),
                                config=cfg, fetcher=FakeFetcher({}))
    second = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report2.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert first == {"applied": 0, "skipped": 1}
    assert second == {"applied": 0, "skipped": 1}
    r1 = _read_csv(str(tmp_path / "report1.csv"))[0]
    r2 = _read_csv(str(tmp_path / "report2.csv"))[0]
    assert r1["skip_reason"] == r2["skip_reason"] == "bad-ideas-url"
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["v3"]["source_url"] == ""


# -- I1: doc_type guard -------------------------------------------------------

def test_apply_skip_bad_doc_type(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("w1", "Row W1", "https://www.bankofengland.co.uk/-/media/boe/files/wp/2020/w1.pdf",
              doc_type="D3")
    _write_manifest(cfg, "gb", [row])

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb", "ideas_url": _paper_url("0140"), "repec_title": "t",
         "candidate_doc_id": "w1", "candidate_title": "Row W1",
         "score": "0.9", "approve": "x"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 0, "skipped": 1}
    report = _read_csv(str(tmp_path / "report.csv"))
    assert report[0]["skip_reason"] == "bad-doc-type"
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["w1"]["source_url"] == ""


# -- I2: BOM -------------------------------------------------------------------

def test_apply_csv_with_bom_applies_normally(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("x1", "Row X1", "https://www.bankofengland.co.uk/-/media/boe/files/wp/2020/x1.pdf")
    _write_manifest(cfg, "gb", [row])

    apply_csv = tmp_path / "apply.csv"
    fields = ["bank", "ideas_url", "repec_title", "candidate_doc_id",
             "candidate_title", "score", "approve"]
    with open(apply_csv, "w", newline="", encoding="utf-8-sig") as fh:
        import csv as _csv
        w = _csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerow({"bank": "gb", "ideas_url": _paper_url("0150"),
                   "repec_title": "t", "candidate_doc_id": "x1",
                   "candidate_title": "Row X1", "score": "0.9", "approve": "x"})
    assert apply_csv.read_bytes().startswith(b"\xef\xbb\xbf")   # sanity: BOM present

    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 1, "skipped": 0}
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["x1"]["source_url"] == _paper_url("0150")


# -- M1: bank hygiene ----------------------------------------------------------

def test_apply_bank_field_whitespace_is_stripped(tmp_path):
    cfg = _cfg(tmp_path)
    row = _row("y1", "Row Y1", "https://www.bankofengland.co.uk/-/media/boe/files/wp/2020/y1.pdf")
    _write_manifest(cfg, "gb", [row])

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "gb ", "ideas_url": _paper_url("0160"), "repec_title": "t",
         "candidate_doc_id": "y1", "candidate_title": "Row Y1",
         "score": "0.9", "approve": "x"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 1, "skipped": 0}
    rows = {r["doc_id"]: r for r in _read_rows(cfg, "gb")}
    assert rows["y1"]["source_url"] == _paper_url("0160")


def test_apply_unknown_bank_warns_once(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _write_manifest(cfg, "gb", [])   # so cfg.manifest_dir exists

    apply_csv = tmp_path / "apply.csv"
    _write_propose_csv(apply_csv, [
        {"bank": "zz", "ideas_url": _paper_url("0170"), "repec_title": "t",
         "candidate_doc_id": "ghost1", "candidate_title": "Ghost1",
         "score": "0.9", "approve": "x"},
        {"bank": "zz", "ideas_url": _paper_url("0171"), "repec_title": "t",
         "candidate_doc_id": "ghost2", "candidate_title": "Ghost2",
         "score": "0.9", "approve": "x"},
    ])
    counts = run_reconcile_apply(str(apply_csv), write=True,
                                 csv_path=str(tmp_path / "report.csv"),
                                 config=cfg, fetcher=FakeFetcher({}))
    assert counts == {"applied": 0, "skipped": 2}
    report = _read_csv(str(tmp_path / "report.csv"))
    assert all(r["skip_reason"] == "row-gone" for r in report)
    err = capsys.readouterr().err
    assert err.count("unknown or empty bank 'zz'") == 1


# -- M2 / M3: CLI guards -------------------------------------------------------

def test_cli_banks_with_apply_csv_is_an_argparse_error(tmp_path, capsys):
    from cb_corpus import cli
    import pytest

    apply_csv = tmp_path / "apply.csv"
    apply_csv.write_text("bank,ideas_url,candidate_doc_id,candidate_title,score,approve\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["repec-reconcile", "--apply-csv", str(apply_csv), "--banks", "gb"])
    assert exc.value.code == 2
    assert "--banks" in capsys.readouterr().err


def test_cli_csv_same_as_apply_csv_is_an_argparse_error(tmp_path, capsys):
    from cb_corpus import cli
    import pytest

    same_path = tmp_path / "same.csv"
    same_path.write_text("bank,ideas_url,candidate_doc_id,candidate_title,score,approve\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["repec-reconcile", "--apply-csv", str(same_path), "--csv", str(same_path)])
    assert exc.value.code == 2
    assert "--csv" in capsys.readouterr().err


def test_cli_csv_symlink_alias_of_apply_csv_is_an_argparse_error(tmp_path, capsys):
    """A --csv path that is a SYMLINK pointing at the --apply-csv file must be
    caught too (realpath resolves the symlink to the same target) -- not just
    a byte-identical path string. Guards against clobbering the decision
    record via an alias rather than the literal same path."""
    import os

    from cb_corpus import cli
    import pytest

    real_path = tmp_path / "real.csv"
    real_path.write_text("bank,ideas_url,candidate_doc_id,candidate_title,score,approve\n")

    link_path = tmp_path / "link.csv"
    try:
        link_path.symlink_to(real_path)
    except (OSError, NotImplementedError):
        import pytest as _pytest
        _pytest.skip("symlinks not supported on this platform")

    with pytest.raises(SystemExit) as exc:
        cli.main(["repec-reconcile", "--apply-csv", str(real_path), "--csv", str(link_path)])
    assert exc.value.code == 2
    assert "--csv" in capsys.readouterr().err

    # samefile sub-assert: the realpath check alone already catches the
    # symlink case above, but confirm the samefile fallback used for
    # case-insensitive-filesystem aliases agrees on the same two paths too.
    if hasattr(os.path, "samefile"):
        assert os.path.samefile(str(link_path), str(real_path))
