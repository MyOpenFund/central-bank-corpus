"""audit-pdfs: read-only measure of the stored PDFs against the content rules (#21)."""
import json
import os

from cb_corpus.cli import main
from cb_corpus.config import Config
from cb_corpus.pdf_audit import audit_file, audit_pdfs
from tests.conftest import pdf_body


def _setup(tmp_path, files):
    """files: doc_id -> (bytes or None for missing, mime, ext). One bank 'xx', type A1."""
    data = tmp_path / "data"
    cfg = Config(data_dir=data)
    rows = []
    for doc_id, (content, mime, ext) in files.items():
        rel = f"raw/xx/A1/2020/{doc_id}.{ext}"
        if content is not None:
            p = data / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        rows.append({"doc_id": doc_id, "corpus": "central-bank", "source_code": "xx", "doc_type": "A1", "title": doc_id,
                     "document_url": f"https://x/{doc_id}", "mime_type": mime,
                     "local_path": rel})
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    (cfg.manifest_dir / "xx.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return cfg


def _snapshot(root):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def test_reasons_and_eof_distance(tmp_path):
    good = pdf_body(b"%PDF-1.4 good")
    junk_tail = pdf_body(b"%PDF-1.4 padded") + b"\0" * 5000          # %%EOF 5,000 bytes from the end
    cfg = _setup(tmp_path, {
        "good": (good, "application/pdf", "pdf"),
        "padded": (junk_tail, "application/pdf", "pdf"),
        "truncated": (pdf_body(b"%PDF-1.4 cut")[:600] + b"0" * 900, "application/pdf", "pdf"),
        "html": (b"<!doctype html>" + b" " * 2000, "application/pdf", "pdf"),
        "small": (b"%PDF-1.4\n%%EOF\n", "application/pdf", "pdf"),
        "gone": (None, "application/pdf", "pdf"),
        "page": (b"<html>not checked</html>", "text/html", "html"),   # HTML artifact: out of scope
    })
    before = _snapshot(tmp_path / "data" / "raw")
    s = audit_pdfs(cfg, progress_every=0)
    assert _snapshot(tmp_path / "data" / "raw") == before                    # read-only
    assert (s.rows_checked, s.ok, s.failing) == (6, 1, 5)
    assert s.by_reason == {"no-eof-in-window": 2, "not-pdf": 1, "too-small": 1, "missing": 1}
    assert s.eof_from_end == {"<= 16384": 1, "none in tail": 1}
    lines = (cfg.reports_dir / "pdf_audit.csv").read_text().splitlines()
    by_id = {line.split(",")[2]: line.split(",") for line in lines[1:]}
    assert set(by_id) == {"padded", "truncated", "html", "small", "gone"}
    assert by_id["padded"][5] == "5001"                               # "\n" + 5,000 zero bytes
    assert by_id["truncated"][5] == ""


def test_audit_file_reads_only_head_and_tail(tmp_path):
    p = tmp_path / "big.pdf"
    p.write_bytes(b"%PDF-1.7\n" + os.urandom(3 * 1024 * 1024) + b"\n%%EOF\n")
    assert audit_file(p, min_bytes=1024, tail_bytes=4096) == (None, p.stat().st_size, None, "")


def test_verdict_is_the_write_path_verdict(tmp_path):
    """audit_file and check_pdf_bytes decide through the same pdf_problem."""
    from cb_corpus.pdfcheck import EOF_WINDOW, check_pdf_bytes, pdf_problem
    straddle = b"%PDF-1.4\n" + b"0" * 3000 + b"%%EOF" + b"0" * (EOF_WINDOW - 3)  # marker cut by the window
    cases = {"ok": pdf_body(b"%PDF-1.4 x"), "html": b"<html>" + b" " * 3000,
             "small": b"%PDF-1.4\n%%EOF\n", "cut": pdf_body(b"%PDF-1.4 y")[:-7] + b"0" * 2100,
             "straddle": straddle}
    for name, data in cases.items():
        p = tmp_path / f"{name}.pdf"
        p.write_bytes(data)
        reason = audit_file(p, min_bytes=1024)[0]
        assert reason == pdf_problem(data, data, len(data), min_bytes=1024, require_eof=True), name
        assert (reason is None) == (check_pdf_bytes(data, min_bytes=1024, require_eof=True) is None), name
    assert audit_file(tmp_path / "straddle.pdf", min_bytes=1024)[0] == "no-eof-in-window"


def test_unreadable_file_is_reported_not_fatal_and_no_temp_left(tmp_path):
    cfg = _setup(tmp_path, {"ok": (pdf_body(b"%PDF-1.4 ok"), "application/pdf", "pdf"),
                            "dir": (None, "application/pdf", "pdf")})
    (tmp_path / "data/raw/xx/A1/2020/dir.pdf").mkdir()          # open() raises IsADirectoryError
    s = audit_pdfs(cfg, progress_every=0)
    assert (s.ok, s.by_reason) == (1, {"unreadable": 1})
    row = (cfg.reports_dir / "pdf_audit.csv").read_text().splitlines()[1].split(",")
    assert row[2] == "dir" and row[7].startswith("IsADirectoryError")
    assert [p.name for p in cfg.reports_dir.iterdir()] == ["pdf_audit.csv"]


def test_temp_file_removed_when_the_audit_fails(tmp_path, monkeypatch):
    import cb_corpus.pdf_audit as pa
    cfg = _setup(tmp_path, {"ok": (pdf_body(b"%PDF-1.4 ok"), "application/pdf", "pdf")})

    def boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(pa, "audit_file", boom)
    try:
        audit_pdfs(cfg, progress_every=0)
    except RuntimeError:
        pass
    assert list(cfg.reports_dir.iterdir()) == []


def test_small_file_outside_window_and_rows_without_a_pdf_artifact(tmp_path):
    small_cut = b"%PDF-1.4\n%%EOF\n" + b"0" * 2100 + b"x" * 400   # 2.5 KB, marker before the window
    cfg = _setup(tmp_path, {"cut": (small_cut, "application/pdf", "pdf"),
                            "page": (b"<html></html>", "application/pdf", "html")})
    rows = (cfg.manifest_dir / "xx.jsonl").read_text() + json.dumps(
        {"doc_id": "nofile", "corpus": "central-bank", "source_code": "xx", "doc_type": "A1", "mime_type": "application/pdf"}) + "\n"
    (cfg.manifest_dir / "xx.jsonl").write_text(rows)
    s = audit_pdfs(cfg, progress_every=0)
    assert (s.rows_checked, s.by_reason) == (1, {"no-eof-in-window": 1})   # the .html and the pathless row are out
    assert s.eof_from_end == {"<= 4096": 1}


def test_banks_filter_and_cli(tmp_path, monkeypatch, capsys):
    cfg = _setup(tmp_path, {"a": (pdf_body(b"%PDF-1.4 a"), "application/pdf", "pdf")})
    monkeypatch.chdir(tmp_path)                                      # Config() reads ./data
    assert audit_pdfs(cfg, banks=["yy"], progress_every=0).rows_checked == 0
    assert main(["audit-pdfs", "--csv", str(tmp_path / "out.csv")]) == 0
    out = capsys.readouterr().out
    assert '"rows_checked": 1' in out and "1 ok, 0 failing" in out
    assert (tmp_path / "out.csv").read_text().count("\n") == 1         # header only


def test_cli_refuses_a_tail_smaller_than_the_window(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, {"a": (pdf_body(b"%PDF-1.4 a"), "application/pdf", "pdf")})
    monkeypatch.chdir(tmp_path)
    assert main(["audit-pdfs", "--tail-bytes", "0"]) == 1
    assert "tail_bytes must be >=" in capsys.readouterr().err
