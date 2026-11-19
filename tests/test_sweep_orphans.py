"""`cb_corpus sweep-orphans`: quarantine on-disk files that duplicate indexed docs.

Scenario (measured on the production dataset, 2026-09-03): tens of thousands of
files under raw/ have no manifest row because an earlier doc_id scheme named
them differently; nearly all are byte-identical to a document that IS indexed
under another id. The sweep moves those duplicates out of raw/ (mirror tree
under raw_orphans/), reports the rest, and never touches an indexed file.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from cb_corpus import orphans
from cb_corpus.config import Config

PDF_A = b"%PDF-1.4 " + b"a" * (21 * 1024)          # valid: magic + > 20 KiB
PDF_B = b"%PDF-1.4 " + b"b" * (21 * 1024)
TINY = b"%PDF-1.4 tiny"                              # magic but too small
NOT_PDF = b"<html>not a pdf</html>" + b"x" * (21 * 1024)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(data_dir=tmp_path / "data")
    cfg.raw_dir.mkdir(parents=True)
    cfg.manifest_dir.mkdir(parents=True)
    return cfg


def _write(cfg: Config, rel: str, data: bytes) -> Path:
    p = cfg.raw_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _index(cfg: Config, bank: str, *rows: dict, blank_line: bool = False) -> None:
    """Append manifest rows to manifest/<bank>.jsonl (doc_id, sha256, local_path)."""
    f = cfg.manifest_file(bank)
    with f.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
            if blank_line:
                fh.write("\n")


def _row(doc_id: str, data: bytes, bank: str, rel: str) -> dict:
    return {"doc_id": doc_id, "bank_code": bank, "doc_type": rel.split("/")[1],
            "sha256": _sha(data), "local_path": f"data/raw/{rel}", "document_url": f"https://x/{doc_id}.pdf"}


# --- Task 1: index, walk, classify ------------------------------------------

def test_orphans_dir_is_a_sibling_of_raw(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.orphans_dir == cfg.data_dir / "raw_orphans"


def test_manifest_index_maps_ids_and_hashes_across_bank_files(tmp_path):
    """The index is built from every per-bank file, and a blank line (a
    legitimate artefact of the append-only writers) must not break loading."""
    cfg = _cfg(tmp_path)
    _write(cfg, "us/C1/2015/aaaa.pdf", PDF_A)
    _write(cfg, "ecb/C1/2016/bbbb.pdf", PDF_B)
    _index(cfg, "us", _row("aaaa", PDF_A, "us", "us/C1/2015/aaaa.pdf"), blank_line=True)
    _index(cfg, "ecb", _row("bbbb", PDF_B, "ecb", "ecb/C1/2016/bbbb.pdf"))
    index = orphans.load_manifest_index(cfg)
    assert index.doc_ids == {"aaaa", "bbbb"}
    assert index.by_hash == {_sha(PDF_A): "aaaa", _sha(PDF_B): "bbbb"}
    assert index.owner_path == {
        _sha(PDF_A): cfg.raw_dir / "us/C1/2015/aaaa.pdf",
        _sha(PDF_B): cfg.raw_dir / "ecb/C1/2016/bbbb.pdf",
    }


def test_walk_yields_only_files_whose_stem_is_not_indexed(tmp_path):
    """Any regular file under raw/, at any depth and with any extension, is an
    orphan iff its stem is not a manifest doc_id — indexed files never show up."""
    cfg = _cfg(tmp_path)
    _write(cfg, "us/C1/2015/aaaa.pdf", PDF_A)          # indexed
    _write(cfg, "us/C1/2015/orph1.pdf", PDF_A)         # orphan (dup)
    _write(cfg, "us/C1/2015/orph2.html", NOT_PDF)      # orphan html
    _write(cfg, "us/C1/undated/orph3", TINY)           # orphan, no ext, non-numeric year dir
    _write(cfg, "us/.DS_Store", b"junk")               # shallow orphan is still an orphan
    _index(cfg, "us", _row("aaaa", PDF_A, "us", "us/C1/2015/aaaa.pdf"))
    s = orphans.sweep_orphans(cfg)
    rows = [json.loads(l) for l in Path(s.report_path).read_text().splitlines()]
    rels = sorted(r["rel"] for r in rows)
    assert rels == ["us/.DS_Store", "us/C1/2015/orph1.pdf", "us/C1/2015/orph2.html", "us/C1/undated/orph3"]


def test_walk_restricts_to_requested_banks(tmp_path):
    """banks={"ecb"} restricts the walk; both bank dirs exist so H3's
    unknown-bank guard does not trigger."""
    cfg = _cfg(tmp_path)
    _write(cfg, "us/C1/2015/o1.pdf", PDF_A)
    _write(cfg, "ecb/C1/2015/o2.pdf", PDF_B)
    s = orphans.sweep_orphans(cfg, banks={"ecb"})
    rows = [json.loads(l) for l in Path(s.report_path).read_text().splitlines()]
    rels = [r["rel"] for r in rows]
    assert rels == ["ecb/C1/2015/o2.pdf"]


def _mi(by_hash=None, doc_ids=None, owner_path=None) -> "orphans.ManifestIndex":
    return orphans.ManifestIndex(doc_ids=doc_ids or set(), by_hash=by_hash or {},
                                  owner_path=owner_path or {})


def test_classify_marks_duplicates_and_pdf_validity(tmp_path):
    cfg = _cfg(tmp_path)
    dup = _write(cfg, "us/C1/2015/dup.pdf", PDF_A)
    fresh = _write(cfg, "us/C1/2015/fresh.pdf", PDF_B)
    tiny = _write(cfg, "us/C1/undated/tiny", TINY)
    index = _mi(by_hash={_sha(PDF_A): "aaaa"})
    e = orphans.classify(dup, cfg.raw_dir, index)
    assert (e.rel, e.bank, e.doc_type, e.year_dir, e.ext) == ("us/C1/2015/dup.pdf", "us", "C1", "2015", "pdf")
    assert e.size == len(PDF_A) and e.sha256 == _sha(PDF_A) and e.valid_pdf is True
    assert e.matched_doc_id == "aaaa" and e.action == "would-move"
    assert e.dest == "us/C1/2015/dup.pdf"
    f = orphans.classify(fresh, cfg.raw_dir, index)
    assert f.matched_doc_id is None and f.action == "kept-unindexed" and f.dest is None and f.valid_pdf is True
    t = orphans.classify(tiny, cfg.raw_dir, index)
    assert t.ext == "" and t.year_dir == "undated" and t.valid_pdf is False


def test_classify_shallow_path_leaves_missing_parts_empty(tmp_path):
    cfg = _cfg(tmp_path)
    p = _write(cfg, "us/.DS_Store", b"junk")
    e = orphans.classify(p, cfg.raw_dir, _mi())
    assert (e.bank, e.doc_type, e.year_dir, e.ext) == ("us", "", "", "")   # ".DS_Store" has no suffix


# --- Task 2: move + sweep ------------------------------------------------------

def _corpus(tmp_path):
    """Indexed aaaa (PDF_A); orphans: dup.pdf (=PDF_A), fresh.pdf (PDF_B, unknown),
    page.html (=PDF_A bytes, dup), undated/blob (TINY, unknown)."""
    cfg = _cfg(tmp_path)
    _write(cfg, "us/C1/2015/aaaa.pdf", PDF_A)
    _write(cfg, "us/C1/2015/dup.pdf", PDF_A)
    _write(cfg, "us/C1/2015/fresh.pdf", PDF_B)
    _write(cfg, "us/C1/2015/page.html", PDF_A)
    _write(cfg, "us/C1/undated/blob", TINY)
    _index(cfg, "us", _row("aaaa", PDF_A, "us", "us/C1/2015/aaaa.pdf"))
    return cfg


def test_dry_run_reports_but_moves_nothing(tmp_path):
    cfg = _corpus(tmp_path)
    s = orphans.sweep_orphans(cfg)
    assert s.dry_run is True and s.files_seen == 5 and s.orphans == 4
    assert s.duplicates == 2 and s.unindexed == 2 and s.moved == 0
    assert s.bytes_duplicates == 2 * len(PDF_A)
    assert (cfg.raw_dir / "us/C1/2015/dup.pdf").exists()
    assert not cfg.orphans_dir.exists()
    rows = [json.loads(l) for l in Path(s.report_path).read_text().splitlines()]
    assert {r["rel"]: r["action"] for r in rows} == {
        "us/C1/2015/dup.pdf": "would-move", "us/C1/2015/page.html": "would-move",
        "us/C1/2015/fresh.pdf": "kept-unindexed", "us/C1/undated/blob": "kept-unindexed"}
    assert {r["rel"]: r["valid_pdf"] for r in rows}["us/C1/undated/blob"] is False
    assert Path(s.report_path).with_suffix(".summary.json").exists()


def test_move_quarantines_duplicates_in_a_mirror_tree(tmp_path):
    cfg = _corpus(tmp_path)
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.moved == 2 and s.move_failed == 0 and s.dry_run is False
    assert not (cfg.raw_dir / "us/C1/2015/dup.pdf").exists()
    assert (cfg.orphans_dir / "us/C1/2015/dup.pdf").read_bytes() == PDF_A
    assert (cfg.orphans_dir / "us/C1/2015/page.html").read_bytes() == PDF_A
    # never touched: the indexed file and the unindexed orphans
    assert (cfg.raw_dir / "us/C1/2015/aaaa.pdf").exists()
    assert (cfg.raw_dir / "us/C1/2015/fresh.pdf").exists()
    assert (cfg.raw_dir / "us/C1/undated/blob").exists()
    rows = [json.loads(l) for l in Path(s.report_path).read_text().splitlines()]
    assert sorted(r["action"] for r in rows) == ["kept-unindexed", "kept-unindexed", "moved", "moved"]


def test_indexed_file_is_never_moved_even_if_it_duplicates_another_row(tmp_path):
    """Two indexed rows with identical bytes: both files stay, neither is an orphan."""
    cfg = _cfg(tmp_path)
    _write(cfg, "us/C1/2015/aaaa.pdf", PDF_A)
    _write(cfg, "us/C1/2016/cccc.pdf", PDF_A)
    _index(cfg, "us", _row("aaaa", PDF_A, "us", "us/C1/2015/aaaa.pdf"),
           _row("cccc", PDF_A, "us", "us/C1/2016/cccc.pdf"))
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.orphans == 0 and s.moved == 0
    assert (cfg.raw_dir / "us/C1/2015/aaaa.pdf").exists() and (cfg.raw_dir / "us/C1/2016/cccc.pdf").exists()


def test_two_unindexed_orphans_with_the_same_bytes_both_stay(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg, "us/C1/2015/o1.pdf", PDF_B)
    _write(cfg, "us/C1/2015/o2.pdf", PDF_B)
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.orphans == 2 and s.unindexed == 2 and s.moved == 0


def test_second_run_is_a_no_op_and_replay_over_identical_dest_removes_source(tmp_path):
    cfg = _corpus(tmp_path)
    orphans.sweep_orphans(cfg, move=True)
    s2 = orphans.sweep_orphans(cfg, move=True)
    assert s2.duplicates == 0 and s2.moved == 0 and s2.unindexed == 2
    # replay: the same duplicate reappears in raw/ while its copy already sits in raw_orphans/
    _write(cfg, "us/C1/2015/dup.pdf", PDF_A)
    s3 = orphans.sweep_orphans(cfg, move=True)
    assert s3.moved == 1 and not (cfg.raw_dir / "us/C1/2015/dup.pdf").exists()
    assert (cfg.orphans_dir / "us/C1/2015/dup.pdf").read_bytes() == PDF_A


def test_dest_with_different_content_is_a_counted_failure_not_an_overwrite(tmp_path):
    cfg = _corpus(tmp_path)
    clash = cfg.orphans_dir / "us/C1/2015/dup.pdf"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(PDF_B)
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.move_failed == 1 and s.moved == 1
    assert (cfg.raw_dir / "us/C1/2015/dup.pdf").read_bytes() == PDF_A      # source untouched
    assert clash.read_bytes() == PDF_B                                       # dest untouched
    rows = {json.loads(l)["rel"]: json.loads(l) for l in Path(s.report_path).read_text().splitlines()}
    assert rows["us/C1/2015/dup.pdf"]["action"] == "move-failed"
    assert "different content" in rows["us/C1/2015/dup.pdf"]["error"]


def test_report_file_names_use_the_utc_timestamp(tmp_path):
    from datetime import datetime, timezone
    cfg = _corpus(tmp_path)
    s = orphans.sweep_orphans(cfg, now=datetime(2026, 9, 3, 14, 5, 6, tzinfo=timezone.utc))
    assert Path(s.report_path) == cfg.reports_dir / "orphan_sweep_20260903T140506Z.jsonl"
    summary = json.loads((cfg.reports_dir / "orphan_sweep_20260903T140506Z.summary.json").read_text())
    assert summary["orphans"] == 4 and summary["dry_run"] is True and summary["started_at"].startswith("2026-09-03T14:05:06")


def test_progress_lines_go_to_stderr(tmp_path, capsys):
    cfg = _corpus(tmp_path)
    orphans.sweep_orphans(cfg, progress_every=2)
    err = capsys.readouterr().err
    assert "sweep-orphans: examined 2 files" in err and "examined 4 files" in err


# --- Review fixes ------------------------------------------------------------

def test_duplicate_is_kept_when_the_owner_row_file_is_missing_from_disk(tmp_path):
    """The manifest row for aaaa points at data/raw/us/C1/2015/aaaa.pdf, which
    does not exist on disk; the only file on disk with those bytes is
    renamed.pdf. renamed.pdf must NOT be quarantined — moving it would leave
    zero on-disk copies of aaaa."""
    cfg = _cfg(tmp_path)
    renamed = _write(cfg, "us/C1/2015/renamed.pdf", PDF_A)   # NOT at the row's local_path
    _index(cfg, "us", _row("aaaa", PDF_A, "us", "us/C1/2015/aaaa.pdf"))  # missing on disk
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.owner_missing == 1
    assert s.moved == 0
    assert s.bytes_duplicates == 0     # owner missing: not reclaimable, must not be counted
    assert renamed.exists()
    rows = {json.loads(l)["rel"]: json.loads(l) for l in Path(s.report_path).read_text().splitlines()}
    row = rows["us/C1/2015/renamed.pdf"]
    assert row["action"] == "kept-owner-missing" and row["dest"] is None
    assert row["error"].startswith("owner file missing:")


def test_duplicate_is_kept_when_the_owner_path_resolves_to_the_file_itself(tmp_path):
    """The manifest row for aaaa points at data/raw/us/C1/2015/renamed.pdf (the
    file that got renamed away from its indexed doc_id) and renamed.pdf is the
    ONLY on-disk copy of those bytes: the owner path IS the file being
    examined. It must NOT be quarantined — moving it would leave zero on-disk
    copies of aaaa, exactly like the owner-file-missing case."""
    cfg = _cfg(tmp_path)
    renamed = _write(cfg, "us/C1/2015/renamed.pdf", PDF_A)
    _index(cfg, "us", _row("aaaa", PDF_A, "us", "us/C1/2015/renamed.pdf"))
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.owner_missing == 1
    assert s.moved == 0
    assert renamed.exists()
    rows = {json.loads(l)["rel"]: json.loads(l) for l in Path(s.report_path).read_text().splitlines()}
    row = rows["us/C1/2015/renamed.pdf"]
    assert row["action"] == "kept-owner-missing" and row["dest"] is None
    assert row["error"] == "owner is this file"


def test_absolute_local_path_pointing_at_the_owner_file_is_accepted(tmp_path):
    """local_path is absolute and has no `raw/` segment anywhere — this must
    exercise the absolute branch of `_resolve_local_path`, not the marker
    branch. The copy lives outside raw/ but holds the owner's exact bytes so
    H1's verification still passes."""
    cfg = _cfg(tmp_path)
    outside_dir = tmp_path / "outside_owner"
    outside_dir.mkdir()
    owner_copy = outside_dir / "aaaa.pdf"
    owner_copy.write_bytes(PDF_A)
    dup = _write(cfg, "us/C1/2015/dup.pdf", PDF_A)
    row = {"doc_id": "aaaa", "bank_code": "us", "doc_type": "C1", "sha256": _sha(PDF_A),
           "local_path": str(owner_copy), "document_url": "https://x/aaaa.pdf"}
    _index(cfg, "us", row)
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.owner_missing == 0
    assert s.moved == 1
    assert not dup.exists()
    assert (cfg.orphans_dir / "us/C1/2015/dup.pdf").read_bytes() == PDF_A

@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses unix permission checks")
def test_unreadable_file_is_reported_not_fatal_and_others_still_move(tmp_path):
    """A file that cannot be hashed (chmod 0) must not abort the sweep: it gets
    a hash-failed row, is never moved, and the rest of the sweep proceeds."""
    cfg = _corpus(tmp_path)
    bad = _write(cfg, "us/C1/2015/bad.pdf", PDF_B)
    bad.chmod(0)
    try:
        s = orphans.sweep_orphans(cfg, move=True)
    finally:
        bad.chmod(0o644)   # restore so tmp_path cleanup can remove it
    assert s.hash_failed == 1
    assert s.moved == 2                 # dup.pdf and page.html still moved
    assert bad.exists()                 # never moved
    rows = {json.loads(l)["rel"]: json.loads(l) for l in Path(s.report_path).read_text().splitlines()}
    row = rows["us/C1/2015/bad.pdf"]
    assert row["action"] == "hash-failed"
    # stat() still works without read permission (only the parent dir's
    # permissions matter) so the row carries the real size, not a dummy 0.
    assert row["sha256"] == "" and row["size"] == len(PDF_B) and row["valid_pdf"] is False
    assert row["matched_doc_id"] is None and row["dest"] is None
    assert "PermissionError" in row["error"]


def test_symlink_under_raw_is_ignored_by_the_walk(tmp_path):
    cfg = _corpus(tmp_path)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(PDF_A)
    link = cfg.raw_dir / "us/C1/2015/link.pdf"
    link.symlink_to(outside)
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.files_seen == 5           # unchanged: the symlink itself is not counted
    rels = [json.loads(l)["rel"] for l in Path(s.report_path).read_text().splitlines()]
    assert "us/C1/2015/link.pdf" not in rels
    assert link.is_symlink() and outside.exists() and outside.read_bytes() == PDF_A


def test_move_fails_when_destination_is_a_symlink(tmp_path):
    cfg = _corpus(tmp_path)
    dest = cfg.orphans_dir / "us/C1/2015/dup.pdf"
    dest.parent.mkdir(parents=True)
    dest.symlink_to(cfg.raw_dir / "us/C1/2015/aaaa.pdf")
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.move_failed == 1
    assert (cfg.raw_dir / "us/C1/2015/dup.pdf").exists()      # source untouched
    rows = {json.loads(l)["rel"]: json.loads(l) for l in Path(s.report_path).read_text().splitlines()}
    row = rows["us/C1/2015/dup.pdf"]
    assert row["action"] == "move-failed" and row["error"] == "dest is a symlink"


def test_symlinked_bank_directory_is_not_followed(tmp_path):
    """A symlink sitting at the top level of raw/ that points at a directory
    (e.g. an accidental `raw/ext -> /some/tmp/outside_dir`) must not be walked
    as if it were a bank: the files inside it are outside this sweep's scope."""
    cfg = _corpus(tmp_path)
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    outside_dup = outside_dir / "dup.pdf"
    outside_dup.write_bytes(PDF_A)          # duplicate of aaaa, but outside raw/
    link = cfg.raw_dir / "ext"
    link.symlink_to(outside_dir)
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.files_seen == 5               # unchanged: nothing inside the symlinked dir counted
    rows = [json.loads(l)["rel"] for l in Path(s.report_path).read_text().splitlines()]
    assert not any(r.startswith("ext/") for r in rows)
    assert link.is_symlink()
    assert outside_dup.exists() and outside_dup.read_bytes() == PDF_A


def test_move_fails_when_an_intermediate_destination_directory_is_a_symlink(tmp_path):
    """raw_orphans/us -> <somewhere outside raw_orphans/>: the immediate dest
    check (`dest.is_symlink()`) doesn't catch this because the symlink is an
    ancestor, not the leaf. `dest.parent.resolve()` must still land inside
    cfg.orphans_dir, or the move is refused rather than writing outside the
    quarantine tree."""
    cfg = _corpus(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cfg.orphans_dir.mkdir(parents=True)
    (cfg.orphans_dir / "us").symlink_to(elsewhere)
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.move_failed == 2          # dup.pdf and page.html both land under raw_orphans/us/...
    assert (cfg.raw_dir / "us/C1/2015/dup.pdf").exists()      # source untouched
    assert (cfg.raw_dir / "us/C1/2015/page.html").exists()    # source untouched
    assert not any(elsewhere.iterdir())                       # nothing written under elsewhere/
    rows = {json.loads(l)["rel"]: json.loads(l) for l in Path(s.report_path).read_text().splitlines()}
    row = rows["us/C1/2015/dup.pdf"]
    assert row["action"] == "move-failed" and row["error"] == "dest parent escapes raw_orphans/"


def test_loose_files_directly_under_raw_are_swept_only_without_banks_filter(tmp_path):
    cfg = _corpus(tmp_path)
    loose = _write(cfg, "loose.pdf", PDF_A)      # duplicate of aaaa, sitting at raw/ top level
    s = orphans.sweep_orphans(cfg, move=True)
    assert not loose.exists()
    assert (cfg.orphans_dir / "loose.pdf").read_bytes() == PDF_A
    rows = {json.loads(l)["rel"]: json.loads(l) for l in Path(s.report_path).read_text().splitlines()}
    assert rows["loose.pdf"]["bank"] == ""
    # with an explicit banks filter, loose files at the top level belong to no bank
    loose2 = _write(cfg, "loose2.pdf", PDF_A)
    s2 = orphans.sweep_orphans(cfg, move=True, banks={"us"})
    rels2 = [json.loads(l)["rel"] for l in Path(s2.report_path).read_text().splitlines()]
    assert "loose2.pdf" not in rels2
    assert loose2.exists()


def test_summary_records_the_requested_banks_or_none_for_a_full_sweep(tmp_path):
    cfg = _corpus(tmp_path)
    _write(cfg, "ecb/C1/2015/o2.pdf", PDF_B)   # both requested banks must exist (H3)
    s_full = orphans.sweep_orphans(cfg)
    assert s_full.banks is None
    s_banked = orphans.sweep_orphans(cfg, banks={"us", "ecb"})
    assert s_banked.banks == ["ecb", "us"]


def test_report_is_flushed_after_every_written_row(tmp_path, monkeypatch):
    """io.TextIOWrapper is a C type and can't be monkeypatched directly, so
    wrap the file object Path.open('w', ...) hands back for the report."""
    cfg = _corpus(tmp_path)
    calls = []

    class _FlushCountingFile:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return self._inner.__exit__(*exc_info)

        def flush(self):
            calls.append(1)
            return self._inner.flush()

    orig_open = Path.open

    def counting_open(self, *args, **kwargs):
        f = orig_open(self, *args, **kwargs)
        if self.suffix == ".jsonl":
            return _FlushCountingFile(f)
        return f

    monkeypatch.setattr(Path, "open", counting_open)
    s = orphans.sweep_orphans(cfg)
    assert len(calls) >= s.orphans


def test_summary_is_still_written_when_classify_raises_unexpectedly(tmp_path, monkeypatch):
    """An unexpected exception mid-walk (not the contained OSError case) must
    not leave the operator without a summary: sweep_orphans re-raises, but the
    .summary.json file exists so a monitoring job can tell the run happened."""
    cfg = _corpus(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(orphans, "classify", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        orphans.sweep_orphans(cfg)
    summaries = list(cfg.reports_dir.glob("*.summary.json"))
    assert len(summaries) == 1


def test_owner_holds_rejects_owner_with_different_size(tmp_path):
    """The owner path exists (same doc_id row) but the file at it is not the
    same bytes any more — 0 bytes here, a truncated/corrupted owner. The
    duplicate must be kept, not quarantined."""
    cfg = _cfg(tmp_path)
    _write(cfg, "us/C1/2015/aaaa.pdf", b"")            # owner exists but is 0 bytes
    dup = _write(cfg, "us/C1/2015/dup.pdf", PDF_A)
    _index(cfg, "us", _row("aaaa", PDF_A, "us", "us/C1/2015/aaaa.pdf"))
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.owner_missing == 1
    assert s.moved == 0
    assert dup.exists()
    rows = {json.loads(l)["rel"]: json.loads(l) for l in Path(s.report_path).read_text().splitlines()}
    row = rows["us/C1/2015/dup.pdf"]
    assert row["action"] == "kept-owner-missing"
    assert row["error"].startswith("owner size differs:")


def test_owner_holds_rejects_owner_with_same_size_different_bytes(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg, "us/C1/2015/aaaa.pdf", PDF_B)           # same size as PDF_A, different bytes
    dup = _write(cfg, "us/C1/2015/dup.pdf", PDF_A)
    _index(cfg, "us", _row("aaaa", PDF_A, "us", "us/C1/2015/aaaa.pdf"))
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.owner_missing == 1
    assert s.moved == 0
    assert dup.exists()
    rows = {json.loads(l)["rel"]: json.loads(l) for l in Path(s.report_path).read_text().splitlines()}
    row = rows["us/C1/2015/dup.pdf"]
    assert row["action"] == "kept-owner-missing"
    assert row["error"].startswith("owner content differs:")


def test_verified_duplicate_is_moved_and_carries_the_owner_path(tmp_path):
    cfg = _corpus(tmp_path)
    owner = cfg.raw_dir / "us/C1/2015/aaaa.pdf"
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.moved == 2
    rows = {json.loads(l)["rel"]: json.loads(l) for l in Path(s.report_path).read_text().splitlines()}
    assert rows["us/C1/2015/dup.pdf"]["owner_path"] == str(owner)
    assert rows["us/C1/2015/page.html"]["owner_path"] == str(owner)


def test_owner_is_hashed_once_per_owner_path_across_duplicates(tmp_path, monkeypatch):
    """Two duplicates of the same owner hash must reuse one memoised owner
    hash rather than re-hashing the owner file per duplicate."""
    cfg = _cfg(tmp_path)
    owner = _write(cfg, "us/C1/2015/aaaa.pdf", PDF_A)
    _write(cfg, "us/C1/2015/dup1.pdf", PDF_A)
    _write(cfg, "us/C1/2015/dup2.pdf", PDF_A)
    _index(cfg, "us", _row("aaaa", PDF_A, "us", "us/C1/2015/aaaa.pdf"))
    owner_hash_calls = []
    orig = orphans._sha256_of

    def counting(path):
        if Path(path) == owner:
            owner_hash_calls.append(1)
        return orig(path)

    monkeypatch.setattr(orphans, "_sha256_of", counting)
    s = orphans.sweep_orphans(cfg, move=True)
    assert s.moved == 2
    assert len(owner_hash_calls) == 1


def test_sigterm_during_the_sweep_still_writes_the_summary_and_restores_the_handler(tmp_path, monkeypatch):
    import signal
    cfg = _corpus(tmp_path)
    prev_handler = signal.getsignal(signal.SIGTERM)
    orig_classify = orphans.classify
    calls = {"n": 0}

    def _classify(path, raw, index):
        calls["n"] += 1
        if calls["n"] == 2:
            os.kill(os.getpid(), signal.SIGTERM)
        return orig_classify(path, raw, index)

    monkeypatch.setattr(orphans, "classify", _classify)
    try:
        with pytest.raises(SystemExit) as exc_info:
            orphans.sweep_orphans(cfg)
        assert exc_info.value.code == 143
    finally:
        assert signal.getsignal(signal.SIGTERM) == prev_handler
    summaries = list(cfg.reports_dir.glob("*.summary.json"))
    assert len(summaries) == 1


def test_sweep_rejects_unknown_bank_codes(tmp_path):
    cfg = _corpus(tmp_path)
    with pytest.raises(FileNotFoundError, match="unknown bank code"):
        orphans.sweep_orphans(cfg, banks={"ecbb"})


def test_cli_sweep_rejects_unknown_bank_codes(tmp_path, monkeypatch, capsys):
    import cb_corpus.cli as cli
    cfg = _corpus(tmp_path)
    monkeypatch.setattr(cli, "Config", lambda: cfg)
    assert cli.main(["sweep-orphans", "--banks", "ecbb"]) == 1
    assert "error:" in capsys.readouterr().err


def test_raw_dir_present_but_not_a_directory_is_reported_clearly(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True)
    cfg.raw_dir.write_bytes(b"not a directory")
    with pytest.raises(FileNotFoundError, match="corpus raw dir not found or not a directory"):
        orphans.sweep_orphans(cfg)


# --- Task 3: CLI ---------------------------------------------------------------

def test_cli_sweep_orphans_dry_run_and_move_exit_zero(tmp_path, monkeypatch, capsys):
    import cb_corpus.cli as cli
    cfg = _corpus(tmp_path)
    monkeypatch.setattr(cli, "Config", lambda: cfg)           # cli builds Config() itself
    assert cli.main(["sweep-orphans"]) == 0
    out = capsys.readouterr().out
    assert '"dry_run": true' in out and "sweep-orphans:" in out and "(dry-run)" in out
    assert (cfg.raw_dir / "us/C1/2015/dup.pdf").exists()
    assert cli.main(["sweep-orphans", "--move"]) == 0
    assert not (cfg.raw_dir / "us/C1/2015/dup.pdf").exists()


def test_cli_passes_banks_and_progress_through(tmp_path, monkeypatch, capsys):
    import cb_corpus.cli as cli
    seen = {}

    def fake_sweep(cfg, **kw):
        seen.update(kw)
        return orphans.SweepSummary(files_seen=0, orphans=0, duplicates=0, unindexed=0, moved=0,
                                    move_failed=0, bytes_duplicates=0, dry_run=not kw["move"],
                                    report_path="r", started_at="s", finished_at="f",
                                    banks=sorted(kw["banks"]) if kw["banks"] else None)

    monkeypatch.setattr(cli, "Config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(orphans, "sweep_orphans", fake_sweep)
    assert cli.main(["sweep-orphans", "--banks", "ecb,us", "--progress-every", "7"]) == 0
    assert seen["banks"] == {"ecb", "us"} and seen["progress_every"] == 7 and seen["move"] is False
    assert "[banks: ecb,us]" in capsys.readouterr().out


def test_cli_missing_raw_dir_is_a_fatal_error(tmp_path, monkeypatch, capsys):
    import cb_corpus.cli as cli
    cfg = Config(data_dir=tmp_path / "nowhere")
    monkeypatch.setattr(cli, "Config", lambda: cfg)
    assert cli.main(["sweep-orphans"]) == 1
    assert "error:" in capsys.readouterr().err
