"""`migrate-contract`: the one-shot that rewrites the stored manifests in the
documents-contract form (corpus, source_code, paths relative to the data
root, undated rows without precision nor kind) and writes vocabulary.json."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from vault_contract import check_row, load_vocabulary

from cb_corpus import cli
from cb_corpus.config import Config
from cb_corpus.contract_migrate import migrate_contract, summary
from cb_corpus.models import compute_doc_id
from cb_corpus.vocabulary import build_vocabulary


def _row(bank: str, i: int, **kw) -> dict:
    url = f"https://example.org/{bank}/{i}.pdf"
    did = compute_doc_id(bank, "D1", url)
    row = {"bank_code": bank, "doc_type": "D1", "title": f"Titre é {i}", "document_url": url,
           "source_url": "", "date": "2020-03-01", "language": "en",
           "provenance": "repec_discovery", "mime_type": "application/pdf",
           "sha256": f"{i:064x}", "local_path": f"data/raw/{bank}/D1/2020/{did}.pdf",
           "html_path": None, "date_precision": "month", "date_source": "repec",
           "doc_id": did, "year": 2020}
    row.update(kw)
    return row


def _new(row: dict) -> dict:
    """The expected contract form of a legacy `_row`."""
    out = {"corpus": "central-bank"}
    for k, v in row.items():
        if k == "bank_code":
            out["source_code"] = v
        elif k in ("local_path", "html_path") and isinstance(v, str) and v.startswith("data/"):
            out[k] = v[5:]
        else:
            out[k] = v
    return out


def _write(path: Path, rows: list, tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows) + tail,
                    encoding="utf-8")
    return path


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


UNDATED = dict(date=None, year=None, date_precision="day", date_kind="publication")


@pytest.fixture
def corpus(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    _write(cfg.manifest_file("aa"), [
        _row("aa", 1),
        _row("aa", 2, html_path="data/raw/aa/D1/2020/x.html", mime_type="application/pdf"),
        _row("aa", 3, **UNDATED),
    ])
    already = _new(_row("bb", 1))
    _write(cfg.manifest_file("bb"), [already, _row("bb", 2)])
    _write(cfg.data_dir / "download_errors.jsonl",
           [{"ts": "t1", "bank_code": "aa", "document_url": "https://x/1.pdf", "error": "e"}])
    return cfg


def test_dry_run_writes_nothing_and_counts(corpus):
    before = _snapshot(corpus.data_dir)
    res = migrate_contract(corpus)
    assert _snapshot(corpus.data_dir) == before          # no lock, no vocabulary, no tmp
    by = {Path(r.path).name: r for r in res.files}
    aa = by["aa.jsonl"]
    assert (aa.rows, aa.upgraded, aa.unchanged) == (3, 3, 0)
    assert (aa.corpus, aa.source_code, aa.local_path, aa.html_path) == (3, 3, 3, 1)
    assert (aa.date_precision, aa.date_kind) == (1, 1)
    bb = by["bb.jsonl"]
    assert (bb.rows, bb.upgraded, bb.unchanged) == (2, 1, 1)
    assert not any(r.written for r in res.files)
    assert res.vocabulary == "would-write"
    t = summary(res)
    assert (t["rows"], t["upgraded"], t["unchanged"], t["files"]) == (5, 4, 1, 2)


def test_write_rewrites_rows_in_the_contract_form(corpus):
    raw_bb_already = corpus.manifest_file("bb").read_text(encoding="utf-8").splitlines()[0]
    res = migrate_contract(corpus, write=True)
    assert all(r.written for r in res.files)
    rows = _lines(corpus.manifest_file("aa"))
    assert rows[0] == _new(_row("aa", 1))
    assert list(rows[0])[:3] == ["corpus", "source_code", "doc_type"]
    assert rows[1]["html_path"] == "raw/aa/D1/2020/x.html"
    assert rows[2]["date_precision"] is None and "date_kind" not in rows[2]
    for r in rows:
        assert r["doc_id"] == compute_doc_id(r["source_code"], r["doc_type"], r["document_url"])
    # a row already in the contract form stays byte-identical
    assert corpus.manifest_file("bb").read_text(encoding="utf-8").splitlines()[0] == raw_bb_already
    # the vocabulary lands at the data root; download_errors.jsonl is not a manifest
    assert json.loads((corpus.data_dir / "vocabulary.json").read_text()) == build_vocabulary()
    assert res.vocabulary == "written"
    assert '"bank_code": "aa"' in (corpus.data_dir / "download_errors.jsonl").read_text()
    assert (corpus.manifest_dir / ".aa.jsonl.lock").exists()


def test_every_migrated_row_passes_the_contract_without_transition(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    _write(cfg.manifest_file("ecb"), [_row("ecb", 1), _row("ecb", 2, **UNDATED),
                                      _row("ecb", 3, html_path="data/raw/ecb/D1/2020/x.html")])
    _write(cfg.manifest_file("jp"), [_new(_row("jp", 1)), _row("jp", 2)])
    corpus = cfg
    migrate_contract(corpus, write=True)
    vocab = load_vocabulary(build_vocabulary())
    for f in sorted(corpus.manifest_dir.glob("*.jsonl")):
        for row in _lines(f):
            checked = check_row(row, vocab, expected_corpus="central-bank", transition=False)
            assert checked.refusals == ()
            assert not {n.reason for n in checked.notices} & {
                "corpus_missing", "legacy_alias", "legacy_data_prefix",
                "precision_without_date", "kind_without_date"}


def test_only_the_contract_keys_change(corpus):
    before = {r["doc_id"]: r for f in sorted(corpus.manifest_dir.glob("*.jsonl")) for r in _lines(f)}
    migrate_contract(corpus, write=True)
    after = {r["doc_id"]: r for f in sorted(corpus.manifest_dir.glob("*.jsonl")) for r in _lines(f)}
    assert list(after) == list(before)                    # same doc_ids, same order
    allowed = {"corpus", "source_code", "bank_code", "local_path", "html_path",
               "date_precision", "date_kind"}
    for did, old in before.items():
        new = after[did]
        diff = {k for k in set(old) | set(new) if old.get(k, "∅") != new.get(k, "∅")}
        assert diff <= allowed, (did, diff)


def test_second_run_does_nothing(corpus):
    migrate_contract(corpus, write=True)
    files = sorted(corpus.manifest_dir.glob("*.jsonl")) + [corpus.data_dir / "vocabulary.json"]
    stat = {f: (os.stat(f).st_ino, f.read_bytes()) for f in files}
    res = migrate_contract(corpus, write=True)
    t = summary(res)
    assert (t["upgraded"], t["files_written"], t["unchanged"]) == (0, 0, 5)
    assert res.vocabulary == "unchanged"
    assert {f: (os.stat(f).st_ino, f.read_bytes()) for f in files} == stat


def test_torn_last_line_is_dropped_like_other_rewrites(tmp_path):
    cfg = Config(data_dir=tmp_path)
    path = _write(cfg.manifest_file("aa"), [_row("aa", 1)], tail='{"bank_code": "aa", "doc')
    before = _snapshot(tmp_path)
    dry = migrate_contract(cfg)
    assert (dry.files[0].upgraded, dry.files[0].torn) == (1, 1)
    assert _snapshot(tmp_path) == before
    res = migrate_contract(cfg, write=True)
    assert (res.files[0].torn, res.files[0].written) == (1, True)
    assert path.with_name("aa.jsonl.torn").read_bytes() == b'{"bank_code": "aa", "doc'
    assert len(_lines(path)) == 1


def test_corrupt_non_final_line_is_a_hard_stop(tmp_path):
    cfg = Config(data_dir=tmp_path)
    path = cfg.manifest_file("aa")
    path.parent.mkdir(parents=True)
    path.write_text('{"broken\n' + json.dumps(_row("aa", 1)) + "\n")
    for write in (False, True):
        with pytest.raises(ValueError, match="corrupt manifest line"):
            migrate_contract(cfg, write=write)
    assert path.read_text().startswith('{"broken')


def test_both_source_names_is_an_error_and_never_guessed(tmp_path, monkeypatch, capsys):
    cfg = Config(data_dir=tmp_path / "data")
    both = _row("aa", 2, source_code="zz")
    path = _write(cfg.manifest_file("aa"), [_row("aa", 1), both])
    both_line = path.read_text(encoding="utf-8").splitlines()[1]
    monkeypatch.chdir(tmp_path)
    assert cli.main(["migrate-contract", "--write"]) == 1
    out = capsys.readouterr().out
    assert "both=1" in out and both["doc_id"] in out
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == both_line                         # left untouched
    assert json.loads(lines[0])["source_code"] == "aa"   # the other row migrated


def test_absolute_path_under_the_root_is_made_relative_other_is_an_error(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    inside = str(cfg.raw_dir / "aa" / "D1" / "2020" / "in.pdf")
    _write(cfg.manifest_file("aa"), [_row("aa", 1, local_path=inside),
                                     _row("aa", 2, local_path="/elsewhere/raw/x.pdf")])
    res = migrate_contract(cfg, write=True)
    f = res.files[0]
    assert (f.local_path, f.bad_path) == (1, 1)
    assert f.bad_path_ids == [_row("aa", 2)["doc_id"]]
    rows = _lines(cfg.manifest_file("aa"))
    assert rows[0]["local_path"] == "raw/aa/D1/2020/in.pdf"
    assert rows[1]["local_path"] == "/elsewhere/raw/x.pdf"


def test_bank_filter_skips_other_files(corpus):
    res = migrate_contract(corpus, write=True, bank_codes=["bb"])
    assert [Path(r.path).name for r in res.files] == ["bb.jsonl"]
    assert '"bank_code"' in corpus.manifest_file("aa").read_text()


def test_cli_dry_run_reports_counts_and_exits_zero(corpus, monkeypatch, capsys):
    monkeypatch.chdir(corpus.data_dir.parent)
    before = _snapshot(corpus.data_dir)
    assert cli.main(["migrate-contract"]) == 0
    out = capsys.readouterr().out
    assert "migrate-contract (dry-run): aa.jsonl: rows=3 upgraded=3 unchanged=0" in out
    assert "TOTAL files=2 rows=5 upgraded=4 unchanged=1" in out
    assert "vocabulary.json: would-write" in out
    assert _snapshot(corpus.data_dir) == before


def test_cli_write_then_rerun_reports_zero(corpus, monkeypatch, capsys):
    monkeypatch.chdir(corpus.data_dir.parent)
    assert cli.main(["migrate-contract", "--write"]) == 0
    capsys.readouterr()
    assert cli.main(["migrate-contract", "--write"]) == 0
    out = capsys.readouterr().out
    assert "TOTAL files=2 rows=5 upgraded=0 unchanged=5" in out and "files_written=0" in out
    assert "vocabulary.json: unchanged" in out


@pytest.mark.parametrize("bad", ["data//raw/aa/x.pdf", "data/data/raw/aa/x.pdf"])
def test_unclean_data_prefix_is_a_bad_path_left_untouched(tmp_path, monkeypatch, capsys, bad):
    cfg = Config(data_dir=tmp_path / "data")
    path = _write(cfg.manifest_file("aa"), [_row("aa", 1), _row("aa", 2, local_path=bad)])
    bad_line = path.read_text(encoding="utf-8").splitlines()[1]
    monkeypatch.chdir(tmp_path)
    assert cli.main(["migrate-contract", "--write"]) == 1
    out = capsys.readouterr().out
    assert "bad_path=1" in out and _row("aa", 2)["doc_id"] in out
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[1] == bad_line                          # the whole row left as is
    assert json.loads(lines[0])["source_code"] == "aa"
    res = migrate_contract(cfg, write=True)              # still reported, never rewritten
    assert (res.files[0].bad_path, res.files[0].upgraded) == (1, 0)


# --- dry run / --write parity on malformed input --------------------------------

_SURROGATE = '{"bank_code": "aa", "doc_type": "D1", "title": "bad \\ud800 title", "doc_id": "s1", "date": null, "local_path": "data/raw/aa/D1/0/s1.pdf"}'


def _counts_of(res):
    keep = ("upgraded", "unchanged", "both", "corpus", "source_code", "local_path",
            "date_precision", "bad_path", "torn")
    return [{k: getattr(f, k) for k in keep} for f in res.files]


@pytest.mark.parametrize("tail", ["[1, 2]\n", '{"bank_code": "aa", "doc', "42\n"])
def test_dry_run_and_write_agree_on_a_malformed_final_line(tmp_path, tail):
    cfg = Config(data_dir=tmp_path)
    path = _write(cfg.manifest_file("aa"), [_row("aa", 1)], tail=tail)
    dry = migrate_contract(cfg)
    res = migrate_contract(cfg, write=True)
    assert _counts_of(dry) == _counts_of(res)
    assert (res.files[0].torn, res.files[0].upgraded) == (1, 1)
    assert len(_lines(path)) == 1


@pytest.mark.parametrize("bad", ["[1, 2]", "42", '"x"'])
def test_dry_run_and_write_agree_on_a_malformed_earlier_line(tmp_path, monkeypatch, capsys, bad):
    cfg = Config(data_dir=tmp_path / "data")
    path = cfg.manifest_file("aa")
    path.parent.mkdir(parents=True)
    content = json.dumps(_row("aa", 1)) + "\n" + bad + "\n" + json.dumps(_row("aa", 2)) + "\n"
    path.write_text(content)
    monkeypatch.chdir(tmp_path)
    for argv in (["migrate-contract"], ["migrate-contract", "--write"]):
        assert cli.main(argv) == 1
        err = capsys.readouterr().err
        assert "corrupt manifest line" in err and "Traceback" not in err
    assert path.read_text() == content                   # file left untouched
    assert not list(path.parent.glob("*.tmp"))


def test_lone_surrogate_escape_is_kept_as_an_escape_in_both_modes(tmp_path):
    """A row whose JSON holds an escaped lone surrogate (`\\ud800`) parses but
    cannot be written as UTF-8: the upgraded row is serialized with ASCII
    escapes (same values, valid UTF-8 file), in --write as planned in the dry
    run; never a crash, never a half-written temp file."""
    cfg = Config(data_dir=tmp_path)
    path = cfg.manifest_file("aa")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_row("aa", 1)) + "\n" + _SURROGATE + "\n", encoding="utf-8")
    dry = migrate_contract(cfg)
    res = migrate_contract(cfg, write=True)
    assert _counts_of(dry) == _counts_of(res)
    assert res.files[0].upgraded == 2
    raw = path.read_bytes()
    raw.decode("utf-8")                                   # strict: a valid UTF-8 file
    rows = _lines(path)
    assert rows[1]["title"] == "bad \ud800 title" and rows[1]["source_code"] == "aa"
    assert "\\ud800" in raw.decode("utf-8").splitlines()[1]
    assert rows[0]["title"] == "Titre é 1"                # other rows keep plain UTF-8
    assert "Titre é 1" in raw.decode("utf-8")
    assert migrate_contract(cfg, write=True).files[0].upgraded == 0


def test_failed_rewrite_leaves_the_file_and_no_temp(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    path = _write(cfg.manifest_file("aa"), [_row("aa", 1)])
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        migrate_contract(cfg, write=True)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == [".aa.jsonl.lock", "aa.jsonl"]


def test_rewrite_is_fsynced_before_the_replace(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace",
                        lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    cfg = Config(data_dir=tmp_path)
    _write(cfg.manifest_file("aa"), [_row("aa", 1)])
    migrate_contract(cfg, write=True)
    assert events[:2] == ["fsync", "replace"]


def test_write_waits_for_a_writer_holding_the_sidecar_lock(tmp_path):
    """An appender (or any locked rewriter) holding the bank's sidecar lock
    blocks migrate-contract --write on that file until it lets go; the
    migration then re-reads the file fresh."""
    from tests.test_manifest_locking import _spawn_holder
    import time
    cfg = Config(data_dir=tmp_path)
    path = _write(cfg.manifest_file("aa"), [_row("aa", 1)])
    hold = 1.0
    proc = _spawn_holder(path, hold)
    t0 = time.monotonic()
    res = migrate_contract(cfg, write=True)
    elapsed = time.monotonic() - t0
    proc.wait()
    assert elapsed >= hold * 0.8, f"migrate-contract ignored the sidecar lock ({elapsed:.2f}s)"
    assert res.files[0].upgraded == 1 and _lines(path)[0]["corpus"] == "central-bank"
