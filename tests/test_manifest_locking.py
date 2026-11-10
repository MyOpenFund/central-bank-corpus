"""Concurrency tests for the per-bank sidecar manifest lock (issue #18)."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cb_corpus.storage import _locked

# Subprocess snippet: hold the sidecar lock of sys.argv[1] for sys.argv[2] seconds.
HOLDER = r"""
import sys, time
from pathlib import Path
from cb_corpus.storage import _locked
with _locked(Path(sys.argv[1])):
    print("HELD", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def _spawn_holder(path: Path, seconds: float) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(path), str(seconds)],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "HELD"   # lock is held before we return
    return p


def test_locked_creates_sidecar_and_excludes(tmp_path):
    data = tmp_path / "xx.jsonl"
    hold = 1.0
    proc = _spawn_holder(data, hold)
    t0 = time.monotonic()
    with _locked(data):
        elapsed = time.monotonic() - t0
    proc.wait()
    assert elapsed >= hold * 0.8, f"lock did not block (elapsed={elapsed:.2f}s)"
    assert (tmp_path / ".xx.jsonl.lock").exists()
    assert not data.exists()          # _locked never creates the data file


def test_append_blocks_on_sidecar(tmp_path):
    from cb_corpus.config import Config
    from cb_corpus.models import DocRecord, DocType
    from cb_corpus.storage import Storage
    st = Storage(Config(data_dir=tmp_path))
    rec = DocRecord(bank_code="xx", doc_type=DocType.D1, title="t",
                    pdf_url="https://example.org/a.pdf")
    data = st.cfg.manifest_file("xx")
    hold = 1.0
    proc = _spawn_holder(data, hold)
    t0 = time.monotonic()
    st._append(rec)
    elapsed = time.monotonic() - t0
    proc.wait()
    assert elapsed >= hold * 0.8, f"_append ignored the sidecar lock ({elapsed:.2f}s)"
    rows = [json.loads(l) for l in data.read_text().splitlines() if l.strip()]
    assert len(rows) == 1 and rows[0]["bank_code"] == "xx"


def _seed(tmp_path, bank="xx", n=3):
    from cb_corpus.config import Config
    cfg = Config(data_dir=tmp_path)
    path = cfg.manifest_file(bank)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"doc_id": f"{bank}-{i}", "bank_code": bank, "title": f"t{i}",
             "pdf_url": f"https://example.org/{bank}/{i}.pdf"} for i in range(n)]
    with path.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return cfg, path, rows


def test_apply_row_updates_replaces_and_preserves(tmp_path):
    from cb_corpus.storage import apply_row_updates
    cfg, path, rows = _seed(tmp_path)
    before_lines = path.read_text().splitlines()
    upd = dict(rows[1]); upd["title"] = "CHANGED"
    n = apply_row_updates(cfg, {upd["doc_id"]: upd})
    assert n == 1
    after_lines = path.read_text().splitlines()
    assert len(after_lines) == 3
    assert after_lines[0] == before_lines[0]          # untouched: byte-identical
    assert after_lines[2] == before_lines[2]
    assert json.loads(after_lines[1])["title"] == "CHANGED"


def test_apply_row_updates_unknown_docid_warns_and_skips(tmp_path, capsys):
    from cb_corpus.storage import apply_row_updates
    cfg, path, rows = _seed(tmp_path)
    before = path.read_text()
    ghost = {"doc_id": "xx-ghost", "bank_code": "xx", "title": "g",
             "pdf_url": "https://example.org/g.pdf"}
    n = apply_row_updates(cfg, {"xx-ghost": ghost})
    assert n == 0
    assert path.read_text() == before                  # file untouched by content
    assert "xx-ghost" in capsys.readouterr().err


def test_apply_row_updates_empty_is_noop(tmp_path):
    from cb_corpus.storage import apply_row_updates
    cfg, path, rows = _seed(tmp_path)
    mtime = path.stat().st_mtime_ns
    assert apply_row_updates(cfg, {}) == 0
    assert path.stat().st_mtime_ns == mtime            # not rewritten at all


def test_apply_row_updates_missing_bank_file_warns(tmp_path, capsys):
    from cb_corpus.storage import apply_row_updates
    from cb_corpus.config import Config
    cfg = Config(data_dir=tmp_path)
    row = {"doc_id": "zz-1", "bank_code": "zz", "title": "t",
           "pdf_url": "https://example.org/z.pdf"}
    assert apply_row_updates(cfg, {"zz-1": row}) == 0
    assert not cfg.manifest_file("zz").exists()        # never conjures a file
    assert "zz-1" in capsys.readouterr().err


def test_apply_row_updates_multibank_touches_only_its_banks(tmp_path):
    from cb_corpus.storage import apply_row_updates
    cfg, path_xx, rows_xx = _seed(tmp_path, bank="xx")
    _,   path_yy, rows_yy = _seed(tmp_path, bank="yy")
    _,   path_zz, _       = _seed(tmp_path, bank="zz")
    zz_before = path_zz.read_text()
    u1 = dict(rows_xx[0]); u1["title"] = "X"
    u2 = dict(rows_yy[2]); u2["title"] = "Y"
    n = apply_row_updates(cfg, {u1["doc_id"]: u1, u2["doc_id"]: u2})
    assert n == 2
    assert json.loads(path_xx.read_text().splitlines()[0])["title"] == "X"
    assert json.loads(path_yy.read_text().splitlines()[2])["title"] == "Y"
    assert path_zz.read_text() == zz_before


def test_rewrite_manifest_refreshes_indexes(tmp_path):
    from cb_corpus.config import Config
    from cb_corpus.storage import Storage
    cfg, path, rows = _seed(tmp_path)
    st = Storage(cfg)
    upd = dict(rows[0]); upd["pdf_url"] = "https://example.org/NEW.pdf"
    assert st.rewrite_manifest({upd["doc_id"]: upd}) == 1
    assert st.is_known_url("https://example.org/NEW.pdf")


# Appender subprocess: sidecar-locked append of one row (what Storage._append
# does, minus DocRecord ceremony) — used to interleave with a rewrite.
APPENDER = r"""
import json, sys
from pathlib import Path
from cb_corpus.storage import _locked
path = Path(sys.argv[1])
with _locked(path):
    with path.open("a") as fh:
        fh.write(json.dumps({"doc_id": sys.argv[2], "bank_code": "xx",
                             "pdf_url": "https://example.org/race.pdf"}) + "\n")
"""


def test_race_appended_row_survives_rewrite(tmp_path):
    """THE issue #18 scenario: snapshot -> long 'expensive work' -> another
    process appends -> write-back. The appended row must survive."""
    from cb_corpus.storage import apply_row_updates
    cfg, path, rows = _seed(tmp_path)
    upd = dict(rows[0]); upd["title"] = "CONVERTED"        # snapshot-based work
    subprocess.run([sys.executable, "-c", APPENDER, str(path), "race-row"],
                   check=True)                              # append lands mid-run
    apply_row_updates(cfg, {upd["doc_id"]: upd})            # write-back
    ids = {json.loads(l)["doc_id"] for l in path.read_text().splitlines() if l.strip()}
    assert "race-row" in ids, "concurrently appended row was erased (issue #18)"
    assert upd["doc_id"] in ids


def test_race_harness_detects_the_old_bug(tmp_path):
    """Mutation-proof the harness: replay the OLD semantics (unlocked full
    write from a stale snapshot) and assert the harness DOES see the loss.
    If this stops failing-the-old-way, the race test above proves nothing."""
    cfg, path, rows = _seed(tmp_path)
    stale = [dict(r) for r in rows]                         # pre-append snapshot
    subprocess.run([sys.executable, "-c", APPENDER, str(path), "race-row"],
                   check=True)
    tmp = path.with_suffix(".jsonl.tmp")                    # old write_per_bank body
    with tmp.open("w") as fh:
        for r in stale:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    import os as _os
    _os.replace(tmp, path)
    ids = {json.loads(l)["doc_id"] for l in path.read_text().splitlines() if l.strip()}
    assert "race-row" not in ids, "old semantics unexpectedly kept the row"


def test_rewrite_blocks_while_appender_holds_lock(tmp_path):
    from cb_corpus.storage import apply_row_updates
    cfg, path, rows = _seed(tmp_path)
    upd = dict(rows[0]); upd["title"] = "LOCKED-OUT"
    hold = 1.0
    proc = _spawn_holder(path, hold)
    t0 = time.monotonic()
    apply_row_updates(cfg, {upd["doc_id"]: upd})
    elapsed = time.monotonic() - t0
    proc.wait()
    assert elapsed >= hold * 0.8, f"rewrite did not wait for the lock ({elapsed:.2f}s)"


def test_rewrite_with_torn_tail_under_lock(tmp_path):
    """Adversarial fixture: bank file ends in a torn line. The rewrite must
    not deadlock (no nested repair lock), must save the fragment, and must
    drop the torn line while applying the update."""
    from cb_corpus.storage import apply_row_updates
    cfg, path, rows = _seed(tmp_path)
    with path.open("a") as fh:
        fh.write('{"doc_id": "torn-')                       # no newline, torn
    upd = dict(rows[2]); upd["title"] = "OK"
    n = apply_row_updates(cfg, {upd["doc_id"]: upd})
    assert n == 1
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 3
    assert path.with_name(path.name + ".torn").exists()


# --- stale-offset guard in _repair_torn_tail (final review, I1) --------------
# An offset computed by the UNLOCKED read in _iter_manifest_file can go stale
# if apply_row_updates os.replace's the file before the repair takes the lock:
# same path, new inode, shifted offsets. Truncating at that stale offset would
# destroy valid rows of the NEW file. The guard must detect both symptoms
# (inode mismatch; offset not on a line boundary) and leave the file alone.

def _seed_with_torn_tail(tmp_path):
    cfg, path, rows = _seed(tmp_path)
    intact_len = len(path.read_bytes())                     # a true line boundary
    midline_offset = len(path.read_bytes().splitlines(keepends=True)[0]) - 2
    with path.open("ab") as fh:
        fh.write(b'{"doc_id": "torn-')                      # a REAL torn tail
    return path, midline_offset, intact_len


def test_repair_wrong_inode_leaves_file_untouched(tmp_path):
    """expected_ino mismatch (file was os.replace'd since the unlocked read)
    -> no truncation, file byte-identical, no .torn fragment. Uses a genuine
    LINE-BOUNDARY offset so the inode guard alone must do the refusing (a
    mid-line offset would let the newline guard mask a missing ino check)."""
    from cb_corpus.storage import _repair_torn_tail
    path, _, boundary_offset = _seed_with_torn_tail(tmp_path)
    before = path.read_bytes()
    st = path.stat()
    got = _repair_torn_tail(path, boundary_offset,
                            expected_ino=st.st_ino + 1,
                            expected_size=st.st_size)
    assert got == []
    assert path.read_bytes() == before, "stale-inode repair truncated the file"
    assert not path.with_name(path.name + ".torn").exists()


def test_repair_midline_offset_leaves_file_untouched(tmp_path):
    """Correct inode but an offset pointing MID-LINE (byte before it is not a
    newline) -> stale offset, no truncation, file byte-identical."""
    from cb_corpus.storage import _repair_torn_tail
    path, midline_offset, _ = _seed_with_torn_tail(tmp_path)
    before = path.read_bytes()
    st = path.stat()
    got = _repair_torn_tail(path, midline_offset,
                            expected_ino=st.st_ino,
                            expected_size=st.st_size)
    assert got == []
    assert path.read_bytes() == before, "mid-line offset repair truncated the file"
    assert not path.with_name(path.name + ".torn").exists()


def test_repair_valid_identity_still_truncates(tmp_path):
    """Positive control: correct inode + line-boundary offset + genuinely torn
    tail -> the repair truncates as before (guard must not block real repairs).
    The end-to-end path is also covered by tests/test_torn_manifest.py."""
    from cb_corpus.storage import _repair_torn_tail
    cfg, path, rows = _seed(tmp_path)
    intact = path.read_bytes()
    with path.open("ab") as fh:
        fh.write(b'{"doc_id": "torn-')                      # torn, no newline
    st = path.stat()
    got = _repair_torn_tail(path, len(intact),              # true line boundary
                            expected_ino=st.st_ino,
                            expected_size=st.st_size)
    assert got == []                                        # torn row dropped
    assert path.read_bytes() == intact                      # truncated back
    torn_path = path.with_name(path.name + ".torn")
    assert torn_path.read_bytes() == b'{"doc_id": "torn-'


def test_convert_existing_preserves_concurrent_append(tmp_path, monkeypatch):
    """convert-html's snapshot/write-back must go through apply_row_updates:
    a row appended during the render loop survives."""
    import cb_corpus.convert as convert_mod
    cfg, path, rows = _seed(tmp_path)
    # Make row 0 an HTML row with an existing file so _convert_one converts it.
    html = tmp_path / "doc.html"; html.write_text("<html/>")
    raws = [json.loads(l) for l in path.read_text().splitlines()]
    raws[0]["mime_type"] = "text/html"; raws[0]["local_path"] = str(html)
    with path.open("w") as fh:
        for r in raws:
            fh.write(json.dumps(r) + "\n")
    monkeypatch.setattr(convert_mod, "find_chrome", lambda: "/usr/bin/true")

    def fake_render(url, pdf_path, user_data_dir=None, **kw):
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")
        # An append lands while "Chrome" renders — the heart of issue #18.
        subprocess.run([sys.executable, "-c", APPENDER, str(path), "mid-render"],
                       check=True)
    monkeypatch.setattr(convert_mod, "render_url_to_pdf", fake_render)

    from tests.conftest import public_resolver
    counts = convert_mod.convert_existing(config=cfg, resolver=public_resolver)
    assert counts.get("converted") == 1
    ids = {json.loads(l)["doc_id"] for l in path.read_text().splitlines() if l.strip()}
    assert "mid-render" in ids, "convert-html erased the row appended mid-render"
    assert json.loads(path.read_text().splitlines()[0])["mime_type"] == "application/pdf"
