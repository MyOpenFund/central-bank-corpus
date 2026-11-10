"""gen_corpus.py builds CORPUS.md from the per-bank manifests.

It used to read the pre-split single `data/manifest.jsonl` at import time, so it
crashed (FileNotFoundError) on the per-bank layout the storage layer has used
since the WP v3 split. Fixtures here are deliberately imperfect: blank lines,
rows without a year, a bank code outside BIS-63, an empty bank file, an empty
corpus, a missing raw/ directory, and a legacy-only repo.
"""
import json

import pytest

import gen_corpus
from cb_corpus.config import Config


def _write(path, rows, blank_lines=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for r in rows:
        lines.append(json.dumps(r))
        if blank_lines:
            lines.append("")
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def _row(bank, doc_type, year, **extra):
    r = {"doc_id": f"{bank}-{doc_type}-{year}-{len(extra)}", "bank_code": bank,
         "doc_type": doc_type, "language": "en", "provenance": "bank_site"}
    if year is not None:
        r["year"] = year
        r["date"] = f"{year}-03-01"
    r.update(extra)
    return r


@pytest.fixture
def corpus(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    m = cfg.manifest_dir
    _write(m / "ecb.jsonl", [_row("ecb", "C1", 2020), _row("ecb", "D1", 2021, x=1),
                             _row("ecb", "D1", None, x=2)], blank_lines=True)
    _write(m / "us.jsonl", [_row("us", "A3", 1999), _row("us", "D1", 2024)])
    _write(m / "zz.jsonl", [_row("zz", "C1", 2010)])  # not a BIS-63 code
    _write(m / "jp.jsonl", [])                         # empty file
    return cfg


def test_reads_every_per_bank_file(corpus):
    md = gen_corpus.build_corpus_md(corpus)
    assert "| Documents | **6** |" in md
    # zz is counted and shown with its raw code (no name to map to)
    assert "`zz`" in md
    # the June hard-coded RePEc targets are gone
    assert "4198" not in md and "3658" not in md


def test_blank_lines_and_missing_year_do_not_crash(corpus):
    md = gen_corpus.build_corpus_md(corpus)
    assert "**1999 → 2024**" in md
    # the undated ecb D1 row still counts in the type inventory
    assert "| **D1** | Working papers | 3 |" in md


def test_no_links_to_files_that_never_ship(corpus):
    md = gen_corpus.build_corpus_md(corpus)
    for gone in ("DISCOVERY_AUDIT.md", "PRESENTATION.md", "ARCHITECTURE.md",
                 "data/manifest.jsonl"):
        assert gone not in md
    assert "repec-check" in md
    assert "data/manifest/<bank>.jsonl" in md


def test_missing_raw_dir_reports_na(corpus):
    assert not corpus.raw_dir.exists()
    assert "| Size | **n/a** |" in gen_corpus.build_corpus_md(corpus)


def test_empty_corpus_does_not_crash(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    md = gen_corpus.build_corpus_md(cfg)
    assert "| Documents | **0** |" in md
    assert "| Period | **n/a** |" in md


def test_legacy_single_manifest_still_read(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    _write(cfg.manifest_path, [_row("ca", "D1", 2015), _row("ca", "D1", 2016)])
    assert "| Documents | **2** |" in gen_corpus.build_corpus_md(cfg)


def test_import_has_no_side_effect(tmp_path, monkeypatch):
    """Importing the module must not read data/ or write CORPUS.md."""
    import importlib
    monkeypatch.chdir(tmp_path)
    importlib.reload(gen_corpus)
    assert not (tmp_path / "CORPUS.md").exists()


def test_main_writes_output_path(corpus, tmp_path):
    out = tmp_path / "out" / "CORPUS.md"
    out.parent.mkdir()
    rc = gen_corpus.main(["--data-dir", str(corpus.data_dir), "--out", str(out)])
    assert rc == 0
    assert out.read_text().startswith("# Central bank corpus")
