"""Torn-manifest resilience (audit C1 — task H1; fix wave: H1 review).

A JSONL manifest line torn by SIGKILL/ENOSPC mid-append currently wedges
every subsequent run (strict json.loads at Storage init). The fix in
storage.iter_manifest_rows distinguishes:

  - a malformed FINAL line  -> a lost append, REPAIR: truncate it off
    atomically, save the fragment to `<file>.torn`, warn loudly, continue.
  - a malformed NON-final line -> real corruption, RAISE naming file+line.

"Malformed" covers both a structurally broken JSON line
(json.JSONDecodeError) and a line torn mid multi-byte UTF-8 character
(UnicodeDecodeError — json.loads decodes bytes internally before parsing);
both are torn-append symptoms handled identically.

Blank lines are skipped as before. An intact file must never be rewritten.

The repair path itself takes an exclusive fcntl flock on the file and
RE-VERIFIES the tail is still torn under that lock before truncating — a
concurrent _append() (same lock) may have completed/extended the tail
between the caller's unlocked first read and lock acquisition.
"""
from __future__ import annotations

import json

import pytest

from cb_corpus.config import Config
from cb_corpus.storage import iter_manifest_rows


def _plain(rows):
    """Rows as written: the reader adds `corpus` to a row without it
    (documents-contract transition, models.upgrade_row)."""
    return [{k: v for k, v in r.items() if k != "corpus"} for r in rows]


def _write(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# --- (i) torn FINAL line: repaired, loaded rows = all-but-torn ---
def test_torn_final_line_is_repaired(tmp_path, capsys):
    cfg = Config(data_dir=tmp_path)
    f = cfg.manifest_file("us")
    good1 = json.dumps({"doc_id": "a"}) + "\n"
    good2 = json.dumps({"doc_id": "b"}) + "\n"
    torn = '{"doc_id": "c", "title": "trunc'   # no closing brace/quote, no \n
    _write(f, (good1 + good2 + torn).encode())

    rows = list(iter_manifest_rows(cfg, "us"))

    assert _plain(rows) == [{"doc_id": "a"}, {"doc_id": "b"}]

    # file repaired on disk: only the two intact lines remain
    assert f.read_bytes() == (good1 + good2).encode()

    # fragment preserved for forensics
    torn_path = f.with_name(f.name + ".torn")
    assert torn_path.exists()
    assert torn_path.read_bytes() == torn.encode()

    # one loud stderr warning naming file + byte offset
    err = capsys.readouterr().err
    assert str(f) in err
    assert f"byte offset {len((good1 + good2).encode())}" in err


# --- (ii) torn MIDDLE line: raises, message names file + line ---
def test_torn_middle_line_raises(tmp_path):
    cfg = Config(data_dir=tmp_path)
    f = cfg.manifest_file("us")
    good1 = json.dumps({"doc_id": "a"}) + "\n"
    bad = "{not json at all}\n"
    good2 = json.dumps({"doc_id": "b"}) + "\n"
    _write(f, (good1 + bad + good2).encode())

    with pytest.raises(ValueError) as exc_info:
        list(iter_manifest_rows(cfg, "us"))

    msg = str(exc_info.value)
    assert str(f) in msg
    assert ":2:" in msg or "line 2" in msg.lower()

    # non-final corruption must NOT repair/truncate the file
    assert f.read_bytes() == (good1 + bad + good2).encode()
    assert not f.with_name(f.name + ".torn").exists()


# --- (iii) intact file: byte-identical after a load, no .torn written ---
def test_intact_file_is_byte_identical_after_load(tmp_path):
    cfg = Config(data_dir=tmp_path)
    f = cfg.manifest_file("us")
    content = (json.dumps({"doc_id": "a"}) + "\n"
               + json.dumps({"doc_id": "b"}) + "\n"
               + json.dumps({"doc_id": "c"}) + "\n")
    _write(f, content.encode())
    before = f.read_bytes()
    before_mtime_ns = f.stat().st_mtime_ns

    rows = list(iter_manifest_rows(cfg, "us"))

    assert _plain(rows) == [{"doc_id": "a"}, {"doc_id": "b"}, {"doc_id": "c"}]
    assert f.read_bytes() == before
    assert f.stat().st_mtime_ns == before_mtime_ns
    assert not f.with_name(f.name + ".torn").exists()


# --- (iv) empty file / trailing-newline-only file: zero rows, no crash ---
def test_empty_and_blank_only_files_load_as_zero_rows(tmp_path):
    cfg = Config(data_dir=tmp_path)

    f_empty = cfg.manifest_file("us")
    _write(f_empty, b"")
    assert list(iter_manifest_rows(cfg, "us")) == []
    assert f_empty.read_bytes() == b""

    f_blank = cfg.manifest_file("fr")
    _write(f_blank, b"\n\n   \n")
    assert list(iter_manifest_rows(cfg, "fr")) == []
    # blank-only file left untouched (nothing to repair — no content line at all)
    assert f_blank.read_bytes() == b"\n\n   \n"


# --- (v) torn FINAL line cut mid multi-byte UTF-8 char: repaired too ---
# json.loads(bytes) decodes the bytes as UTF-8 BEFORE parsing JSON, so a line
# torn mid-character raises UnicodeDecodeError, not json.JSONDecodeError.
# RED before the fix: this exception escaped iter_manifest_rows uncaught.
def test_torn_final_line_mid_multibyte_char_is_repaired(tmp_path, capsys):
    cfg = Config(data_dir=tmp_path)
    f = cfg.manifest_file("us")
    good = json.dumps({"doc_id": "a"}) + "\n"
    # "Stabilité" -> the trailing 'é' encodes as the 2-byte sequence
    # b"\xc3\xa9"; dropping the last byte leaves a dangling lead byte
    # (0xc3) with no continuation byte -- exactly what a SIGKILL mid-write
    # of a multi-byte character looks like on disk.
    torn = '{"doc_id":"x","title":"Stabilité'.encode()[:-1]
    _write(f, good.encode() + torn)

    rows = list(iter_manifest_rows(cfg, "us"))

    assert _plain(rows) == [{"doc_id": "a"}]

    # file repaired on disk: only the intact line remains
    assert f.read_bytes() == good.encode()

    # fragment preserved for forensics
    torn_path = f.with_name(f.name + ".torn")
    assert torn_path.exists()
    assert torn_path.read_bytes() == torn

    # loud stderr warning naming file + byte offset (same wording as the
    # JSONDecodeError repair case)
    err = capsys.readouterr().err
    assert str(f) in err
    assert f"byte offset {len(good.encode())}" in err


# --- (vi) torn MIDDLE line with invalid UTF-8: raises, names file + line ---
# Same UnicodeDecodeError distinction as (v), but the line is NOT final, so
# it must hard-stop exactly like a mid-file JSONDecodeError does -- the
# exception must not escape unnamed.
def test_torn_middle_line_invalid_utf8_raises_with_file_and_line(tmp_path):
    cfg = Config(data_dir=tmp_path)
    f = cfg.manifest_file("us")
    good1 = json.dumps({"doc_id": "a"}) + "\n"
    # 0xc3 (a UTF-8 lead byte) followed by 'e' (0x65, not a valid
    # continuation byte) is structurally "complete" JSON-shaped but not
    # valid UTF-8 -- real corruption, not a torn tail.
    bad = b'{"doc_id":"x","title":"Stabilit\xc3e"}\n'
    good2 = json.dumps({"doc_id": "b"}) + "\n"
    _write(f, good1.encode() + bad + good2.encode())

    with pytest.raises(ValueError) as exc_info:
        list(iter_manifest_rows(cfg, "us"))

    msg = str(exc_info.value)
    assert str(f) in msg
    assert ":2:" in msg or "line 2" in msg.lower()

    # non-final corruption must NOT repair/truncate the file
    assert f.read_bytes() == good1.encode() + bad + good2.encode()
    assert not f.with_name(f.name + ".torn").exists()


# --- (vii) repair path re-verifies under lock before truncating ---
# Simulate the race the flock exists to close: between the caller's
# UNLOCKED first read (which sees a torn tail) and the repair path's locked
# re-read, a concurrent in-flight _append() finishes writing that very
# line. The re-check under the lock must see the now-complete line and
# must NOT truncate it away.
def test_repair_rechecks_under_lock_before_truncating(tmp_path, monkeypatch):
    import fcntl as _fcntl

    from cb_corpus import storage as storage_mod

    cfg = Config(data_dir=tmp_path)
    f = cfg.manifest_file("us")
    good1 = json.dumps({"doc_id": "a"}) + "\n"
    prefix = '{"doc_id": "c", "title": "trunc'   # no closing brace/quote yet
    _write(f, (good1 + prefix).encode())

    completion = '"}\n'  # what the "other process" finishes the line with
    real_flock = _fcntl.flock
    calls = {"n": 0}

    def fake_flock(fd, op):
        calls["n"] += 1
        if calls["n"] == 1:
            # Fires exactly at the point _repair_torn_tail acquires its
            # lock -- i.e. AFTER the caller's unlocked first read already
            # saw a torn tail, but BEFORE the repair path's own re-read.
            with open(f, "ab") as extra:
                extra.write(completion.encode())
        return real_flock(fd, op)

    monkeypatch.setattr(storage_mod.fcntl, "flock", fake_flock)

    rows = list(iter_manifest_rows(cfg, "us"))

    assert calls["n"] >= 1  # the hook actually fired
    assert _plain(rows) == [{"doc_id": "a"}, {"doc_id": "c", "title": "trunc"}]

    # no truncation: the completed line is intact on disk, verbatim
    assert f.read_bytes() == (good1 + prefix + completion).encode()
    assert not f.with_name(f.name + ".torn").exists()
