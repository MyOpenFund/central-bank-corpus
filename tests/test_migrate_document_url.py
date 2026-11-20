"""`migrate-document-url`: rename the stored key pdf_url -> document_url in
the per-bank manifests and download_errors.jsonl, under the sidecar locks."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cb_corpus import cli
from cb_corpus.config import Config
from cb_corpus.document_url_migrate import migrate_document_url, summary
from cb_corpus.models import compute_doc_id


def _row(bank: str, i: int, *, key: str = "pdf_url", **extra) -> dict:
    url = f"https://example.org/{bank}/{i}.pdf"
    return {"bank_code": bank, "doc_type": "D1", "title": f"Titre é {i}", key: url,
            "source_url": "", "date": "2020-01-01", "alt_urls": [],
            "doc_id": compute_doc_id(bank, "D1", url), "year": 2020, **extra}


def _write(path: Path, rows: list, tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows) + tail,
                    encoding="utf-8")
    return path


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def corpus(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    ret = _row("aa", 9, key="pdf_url")
    _write(cfg.manifest_file("aa"), [
        _row("aa", 1),
        _row("aa", 2, key="document_url"),
        _row("aa", 3, retired_duplicates=[{"doc_id": "r", "pdf_url": ret["pdf_url"],
                                           "at": "2026-11-19"}]),
    ])
    _write(cfg.manifest_file("bb"), [_row("bb", 1), _row("bb", 2)])
    _write(cfg.data_dir / "download_errors.jsonl",
           [{"ts": "t1", "bank_code": "aa", "pdf_url": "https://x/1.pdf", "error": "e"},
            {"ts": "t2", "bank_code": "aa", "document_url": "https://x/2.pdf", "error": "e"}],
           tail='{"ts": "t3", "pdf_u\n')
    return cfg


def test_dry_run_writes_nothing(corpus):
    before = _snapshot(corpus.data_dir)
    res = migrate_document_url(corpus)
    assert _snapshot(corpus.data_dir) == before          # no lock file, no .torn, no tmp
    by = {Path(r.path).name: r for r in res}
    assert (by["aa.jsonl"].renamed, by["aa.jsonl"].already_new) == (2, 1)
    assert (by["bb.jsonl"].renamed, by["bb.jsonl"].already_new) == (2, 0)
    errs = by["download_errors.jsonl"]
    assert (errs.renamed, errs.already_new, errs.unparsed) == (1, 1, 1)
    assert not any(r.written for r in res)
    assert summary(res)["rows"] == 7


def test_write_renames_keeping_position_and_values(corpus):
    raw_before = corpus.manifest_file("aa").read_text(encoding="utf-8").splitlines()
    res = migrate_document_url(corpus, write=True)
    assert all(r.written for r in res)
    raw_after = corpus.manifest_file("aa").read_text(encoding="utf-8").splitlines()
    # byte-for-byte: only the key name changed (top level and nested entries)
    assert raw_after == [ln.replace('"pdf_url":', '"document_url":') for ln in raw_before]
    rows = [json.loads(ln) for ln in raw_after]
    for r in rows:
        assert "pdf_url" not in r and list(r)[3] == "document_url"
        assert r["doc_id"] == compute_doc_id(r["bank_code"], r["doc_type"], r["document_url"])
    assert list(rows[2]["retired_duplicates"][0]) == ["doc_id", "document_url", "at"]
    errs = (corpus.data_dir / "download_errors.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(errs[0]) == {"ts": "t1", "bank_code": "aa",
                                   "document_url": "https://x/1.pdf", "error": "e"}
    assert errs[2] == '{"ts": "t3", "pdf_u'           # unparsed log line kept verbatim
    assert (corpus.manifest_dir / ".aa.jsonl.lock").exists()


def test_second_run_does_nothing(corpus):
    migrate_document_url(corpus, write=True)
    files = [corpus.manifest_file("aa"), corpus.manifest_file("bb"),
             corpus.data_dir / "download_errors.jsonl"]
    stat = {f: (os.stat(f).st_ino, f.read_bytes()) for f in files}
    res = migrate_document_url(corpus, write=True)
    assert summary(res)["renamed"] == 0 and summary(res)["files_written"] == 0
    assert {f: (os.stat(f).st_ino, f.read_bytes()) for f in files} == stat


def test_torn_last_manifest_line_is_handled_like_other_rewrites(tmp_path):
    cfg = Config(data_dir=tmp_path)
    path = _write(cfg.manifest_file("aa"), [_row("aa", 1), _row("aa", 2)],
                  tail='{"bank_code": "aa", "pdf_u')
    before = _snapshot(tmp_path)
    dry = migrate_document_url(cfg)
    assert (dry[0].renamed, dry[0].torn) == (2, 1)
    assert _snapshot(tmp_path) == before
    res = migrate_document_url(cfg, write=True)
    assert (res[0].renamed, res[0].torn, res[0].written) == (2, 1, True)
    assert path.with_name("aa.jsonl.torn").read_bytes() == b'{"bank_code": "aa", "pdf_u'
    assert [json.loads(ln)["document_url"] for ln in path.read_text().splitlines()] == [
        "https://example.org/aa/1.pdf", "https://example.org/aa/2.pdf"]


def test_torn_tail_on_an_already_migrated_file_is_dropped(tmp_path):
    """A migrated file whose tail got torn later: the rewrite drops the tail
    (storage's rule), nothing is renamed."""
    cfg = Config(data_dir=tmp_path)
    path = _write(cfg.manifest_file("aa"), [_row("aa", 1, key="document_url")],
                  tail='{"bank')
    res = migrate_document_url(cfg, write=True)
    assert (res[0].renamed, res[0].already_new, res[0].torn) == (0, 1, 1)
    assert len(path.read_text().splitlines()) == 1


def test_corrupt_non_final_manifest_line_is_a_hard_stop(tmp_path):
    cfg = Config(data_dir=tmp_path)
    path = cfg.manifest_file("aa")
    path.parent.mkdir(parents=True)
    path.write_text('{"broken\n' + json.dumps(_row("aa", 1)) + "\n")
    with pytest.raises(ValueError, match="corrupt manifest line"):
        migrate_document_url(cfg)
    with pytest.raises(ValueError, match="corrupt manifest line"):
        migrate_document_url(cfg, write=True)
    assert path.read_text().startswith('{"broken')


def test_both_names_is_an_error_and_never_guessed(tmp_path, monkeypatch, capsys):
    cfg = Config(data_dir=tmp_path / "data")
    both = _row("aa", 2, document_url="https://other/x.pdf")
    path = _write(cfg.manifest_file("aa"), [_row("aa", 1), both])
    both_line = path.read_text(encoding="utf-8").splitlines()[1]
    monkeypatch.chdir(tmp_path)
    assert cli.main(["migrate-document-url", "--write"]) == 1
    out = capsys.readouterr().out
    assert "both=1" in out and both["doc_id"] in out
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == both_line                       # left untouched
    assert "pdf_url" not in json.loads(lines[0])       # the others are renamed


def test_bank_filter_skips_other_files(corpus):
    res = migrate_document_url(corpus, write=True, bank_codes=["bb"])
    assert [Path(r.path).name for r in res] == ["bb.jsonl"]
    assert '"pdf_url"' in corpus.manifest_file("aa").read_text()
    assert '"pdf_url"' in (corpus.data_dir / "download_errors.jsonl").read_text()


def test_cli_dry_run_reports_counts_and_exits_zero(corpus, monkeypatch, capsys):
    monkeypatch.chdir(corpus.data_dir.parent)
    before = _snapshot(corpus.data_dir)
    assert cli.main(["migrate-document-url"]) == 0
    out = capsys.readouterr().out
    assert "(dry-run): aa.jsonl: rows=3 renamed=2 already_new=1" in out
    assert "TOTAL files=3 rows=7 renamed=5" in out
    assert _snapshot(corpus.data_dir) == before


# --- durability: same rewrite discipline as migrate-contract -------------------

@pytest.mark.parametrize("name", ["aa.jsonl", "download_errors.jsonl"])
def test_failed_rewrite_leaves_the_file_and_no_temp(corpus, monkeypatch, name):
    path = (corpus.manifest_file("aa") if name == "aa.jsonl"
            else corpus.data_dir / "download_errors.jsonl")
    before = path.read_bytes()
    import cb_corpus.document_url_migrate as mdu

    def boom(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(mdu.os, "replace", boom)
    with pytest.raises(OSError):
        migrate_document_url(corpus, write=True,
                             bank_codes=["aa"] if name == "aa.jsonl" else None)
    assert path.read_bytes() == before
    assert not [p for p in corpus.data_dir.rglob("*.tmp")]


def test_rewrite_is_fsynced_before_the_replace(corpus, monkeypatch):
    import cb_corpus.document_url_migrate as mdu
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(mdu.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(mdu.os, "replace",
                        lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    res = migrate_document_url(corpus, write=True)
    assert sum(r.written for r in res) == 3
    assert events == ["fsync", "replace"] * 3
