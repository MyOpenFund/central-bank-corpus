"""replace-artifact: fix or retire stored artifacts that are not the document.

`audit-pdfs` lists stored PDFs that fail the content check; `Storage.save()`
never re-downloads a known doc_id, so this command is the only way to put the
right bytes behind such a row (or to retire a duplicate ghost). No network: a
fake fetcher serves canned bytes and canned CDX answers.
"""
from __future__ import annotations

import csv
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from cb_corpus.cli import main
from cb_corpus.config import Config
from cb_corpus.replace_artifact import (PlanError, load_plan, replace_artifacts)
from cb_corpus.sources.wayback import CDX, pdf_captures
from cb_corpus.storage import iter_manifest_rows, remove_rows_locked
from tests.conftest import pdf_body

GOOD = pdf_body(b"%PDF-1.7 the real paper")
GOOD2 = pdf_body(b"%PDF-1.7 another good copy")
OWNER = pdf_body(b"%PDF-1.7 the owner speech")
TRUNCATED = GOOD[:700]                     # magic, no %%EOF, too small
HTML = b"<!doctype html><html>error</html>" + b" " * 2000
EMPTY = b""


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class FakeFetcher:
    """get_bytes: url -> bytes | (bytes, mime) | Exception (unmapped = 404).
    get_text: CDX queries answered from `cdx` (exact target url -> rows)."""

    def __init__(self, bodies=None, cdx=None):
        self.bodies = dict(bodies or {})
        self.cdx = dict(cdx or {})
        self.calls: list[str] = []

    def get_bytes(self, url):
        self.calls.append(url)
        body = self.bodies.get(url)
        if body is None:
            raise RuntimeError(f"GET bytes failed (HTTP 404, not retried): {url}")
        if isinstance(body, Exception):
            raise body
        if isinstance(body, tuple):
            return body
        return body, "application/pdf"

    def get_text(self, url):
        self.calls.append(url)
        assert url.startswith(CDX), url
        target = parse_qs(urlsplit(url).query)["url"][0]
        rows = self.cdx.get(target)
        if isinstance(rows, Exception):
            raise rows
        return json.dumps(rows if rows is not None else [])

    def throttle(self, url):
        pass


def _row(doc_id, bank, *, content, ext="pdf", mime="application/pdf", doc_type="D1",
         html_path=None, source_url="", pdf_url=None, alt_urls=None):
    return {"doc_id": doc_id, "bank_code": bank, "doc_type": doc_type, "title": doc_id,
            "pdf_url": pdf_url or f"https://{bank}.example/{doc_id}.pdf",
            "source_url": source_url, "mime_type": mime,
            "sha256": sha(content) if content is not None else None,
            "local_path": f"data/raw/{bank}/{doc_type}/2020/{doc_id}.{ext}",
            "html_path": html_path, "alt_urls": list(alt_urls or [])}


def _setup(tmp_path, rows_and_bytes):
    """rows_and_bytes: list of (row, bytes or None for no file). Returns cfg."""
    data = tmp_path / "data"
    cfg = Config(data_dir=data)
    by_bank: dict[str, list[dict]] = {}
    for row, content in rows_and_bytes:
        if content is not None:
            p = data / row["local_path"][len("data/"):]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        by_bank.setdefault(row["bank_code"], []).append(row)
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    for bank, rows in by_bank.items():
        cfg.manifest_file(bank).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return cfg


def _plan(tmp_path, rows, name="plan.csv"):
    p = tmp_path / name
    with p.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["doc_id", "action", "candidates", "note"])
        for r in rows:
            w.writerow(r)
    return p


def _rows(cfg):
    return {r["doc_id"]: r for r in iter_manifest_rows(cfg)}


def _file(cfg, row):
    return cfg.data_dir / row["local_path"][len("data/"):]


def _tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.name.endswith(".lock")}


def _one(summary, doc_id):
    return next(r for r in summary.rows if r["doc_id"] == doc_id)


# -- wayback helper ---------------------------------------------------------------

def test_pdf_captures_newest_first_deduped_by_digest_and_capped():
    target = "https://www.banque-france.fr/wp101.pdf?a=1&b=2"
    f = FakeFetcher(cdx={target: [["timestamp", "digest"],
                                  ["20040101000000", "AAA"], ["20080101000000", "BBB"],
                                  ["20120101000000", "AAA"], ["20160101000000", "CCC"]]})
    assert pdf_captures(f, target, max_captures=10) == [
        "20160101000000", "20120101000000", "20080101000000"]   # 2004 = same bytes as 2012
    assert pdf_captures(f, target, max_captures=2) == ["20160101000000", "20120101000000"]
    q = parse_qs(urlsplit(f.calls[0]).query)
    assert q["url"] == [target]
    assert set(q["filter"]) == {"statuscode:200", "mimetype:application/pdf"}


def test_pdf_captures_raises_when_cdx_unreachable():
    f = FakeFetcher(cdx={"https://x/a.pdf": RuntimeError("blocked")})
    with pytest.raises(RuntimeError):
        pdf_captures(f, "https://x/a.pdf", max_captures=5)


# -- remove_rows_locked -----------------------------------------------------------

def test_remove_rows_locked_keeps_other_lines_verbatim(tmp_path):
    a, b = _row("a", "fr", content=GOOD), _row("b", "fr", content=GOOD2)
    cfg = _setup(tmp_path, [(a, None), (b, None), (_row("c", "nl", content=GOOD), None)])
    path = cfg.manifest_file("fr")
    # A hand-formatted line must survive byte for byte.
    path.write_text(json.dumps(a) + "\n" + json.dumps(b, indent=None, separators=(",", ":")) + "\n")
    nl_before = cfg.manifest_file("nl").read_bytes()
    assert remove_rows_locked(cfg, {"fr": ["a", "zz"]}) == 1
    assert path.read_text() == json.dumps(b, separators=(",", ":")) + "\n"
    assert cfg.manifest_file("nl").read_bytes() == nl_before
    assert remove_rows_locked(cfg, {"fr": ["a"]}) == 0            # idempotent
    assert remove_rows_locked(cfg, {"zz": ["a"]}) == 0            # no such bank file


# -- plan loading -----------------------------------------------------------------

def test_unusable_plans_raise(tmp_path):
    with pytest.raises(PlanError):
        load_plan(tmp_path / "missing.csv")
    bad = tmp_path / "bad.csv"
    bad.write_text("doc_id,action\nx,replace\n")
    with pytest.raises(PlanError, match="candidates"):
        load_plan(bad)
    empty = tmp_path / "empty.csv"
    empty.write_text("doc_id,action,candidates,note\n\n")
    with pytest.raises(PlanError, match="no rows"):
        load_plan(empty)


def test_committed_plan_loads():
    from pathlib import Path
    rows = load_plan(Path(__file__).resolve().parents[1] / "deploy/plans/2026-11-19-audit-pdfs.csv")
    assert [(r.doc_id, r.action) for r in rows] == [
        ("a9d3a210bc14458b", "replace"), ("10d9d3155d5bed16", "replace"),
        ("580580d322cdcdd0", "replace"), ("52ea76c564cfae62", "replace"),
        ("d38a1aaf9c69d723", "retire")]
    assert rows[3].candidates == ["wayback"]
    assert rows[4].candidates == ["duplicate-of:e30216a289c89f04"]
    assert "2021" in rows[2].note
    assert all(r.note for r in rows)


def test_committed_plan_ships_in_the_image():
    """deploy/README tells the operator to run /app/deploy/plans/...: the
    Dockerfile must copy deploy/ and .dockerignore must not drop the plans."""
    import fnmatch
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert "COPY deploy /app/deploy" in (root / "deploy/Dockerfile").read_text()
    patterns = [ln.strip() for ln in (root / ".dockerignore").read_text().splitlines()
                if ln.strip() and not ln.startswith("#")]
    rel = "deploy/plans/2026-11-19-audit-pdfs.csv"
    assert not any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(Path(rel).name, p)
                   or rel.startswith(p.rstrip("/") + "/") for p in patterns)


# -- replace ----------------------------------------------------------------------

def test_fallback_candidate_after_a_failing_one_write(tmp_path):
    row = _row("wp895", "fr", content=TRUNCATED)
    other = _row("nlrow", "nl", content=GOOD2)
    cfg = _setup(tmp_path, [(row, TRUNCATED), (other, GOOD2)])
    nl_before = cfg.manifest_file("nl").read_bytes()
    f = FakeFetcher({"https://a/html": (HTML, "text/html"), "https://a/cut": TRUNCATED,
                     "https://a/good": GOOD})
    plan = _plan(tmp_path, [["wp895", "replace",
                             "https://a/dead https://a/html https://a/cut https://a/good",
                             "BdF current file"]])
    s = replace_artifacts(cfg, plan, write=True, fetcher=f,
                          now="2026-11-19T10:00:00+00:00")
    r = _one(s, "wp895")
    assert r["outcome"] == "replaced"
    assert r["winning_url"] == "https://a/good"
    assert [t["url"] for t in r["tried"]] == ["https://a/dead", "https://a/html",
                                              "https://a/cut", "https://a/good"]
    assert r["tried"][-1]["reason"] == "ok"
    assert "not a PDF" in r["tried"][1]["reason"]
    assert (r["old_size"], r["new_size"]) == (len(TRUNCATED), len(GOOD))
    assert (r["old_sha256"], r["new_sha256"]) == (sha(TRUNCATED), sha(GOOD))
    assert _file(cfg, row).read_bytes() == GOOD
    new = _rows(cfg)["wp895"]
    assert new["doc_id"] == "wp895" and new["pdf_url"] == row["pdf_url"]
    assert new["local_path"] == row["local_path"]
    assert new["sha256"] == sha(GOOD) and new["mime_type"] == "application/pdf"
    assert new["alt_urls"] == ["https://a/good"]
    assert new["artifact_replaced"] == {"at": "2026-11-19T10:00:00+00:00",
                                        "from_url": "https://a/good",
                                        "previous_sha256": sha(TRUNCATED),
                                        "note": "BdF current file"}
    assert cfg.manifest_file("nl").read_bytes() == nl_before     # other bank untouched
    assert f.calls == ["https://a/dead", "https://a/html", "https://a/cut", "https://a/good"]
    # The report CSV names the row.
    lines = list(csv.DictReader((cfg.reports_dir / "replace_artifact.csv").open()))
    assert lines[0]["doc_id"] == "wp895" and lines[0]["outcome"] == "replaced"


def test_winning_url_moved_to_front_and_own_pdf_url_not_added(tmp_path):
    own = "https://fr.example/own.pdf"
    row = _row("r1", "fr", content=TRUNCATED, pdf_url=own,
               alt_urls=["https://mirror/x.pdf", "https://a/good"])
    cfg = _setup(tmp_path, [(row, TRUNCATED)])
    f = FakeFetcher({"https://a/good": GOOD})
    s = replace_artifacts(cfg, _plan(tmp_path, [["r1", "replace", "https://a/good", "n"]]),
                          write=True, fetcher=f)
    assert _one(s, "r1")["outcome"] == "replaced"
    assert _rows(cfg)["r1"]["alt_urls"] == ["https://a/good", "https://mirror/x.pdf"]

    row2 = _row("r2", "fr", content=TRUNCATED, pdf_url=own)
    cfg2 = _setup(tmp_path / "two", [(row2, TRUNCATED)])
    s2 = replace_artifacts(cfg2, _plan(tmp_path, [["r2", "replace", own, "n"]], "p2.csv"),
                           write=True, fetcher=FakeFetcher({own: GOOD}))
    assert _one(s2, "r2")["outcome"] == "replaced"
    assert _rows(cfg2)["r2"]["alt_urls"] == []                    # the row's own pdf_url


def test_all_candidates_fail_unresolved_nothing_written(tmp_path):
    row = _row("wp101", "fr", content=TRUNCATED)
    cfg = _setup(tmp_path, [(row, TRUNCATED)])
    before = _tree(cfg.data_dir)
    f = FakeFetcher({"https://a/cut": TRUNCATED, "https://a/html": (HTML, "text/html")})
    s = replace_artifacts(cfg, _plan(tmp_path, [["wp101", "replace",
                                                  "https://a/cut https://a/html ftp://nope", "n"]]),
                          write=True, fetcher=f)
    r = _one(s, "wp101")
    assert r["outcome"] == "unresolved"
    assert len(r["tried"]) == 3 and "not a URL" in r["tried"][2]["reason"]
    after = {k: v for k, v in _tree(cfg.data_dir).items() if not k.startswith("reports/")}
    assert after == before
    assert s.counts == {"unresolved": 1}


def test_duplicate_sha_refused(tmp_path):
    row = _row("ghost", "fr", content=TRUNCATED)
    owner = _row("owner", "it", content=GOOD, doc_type="C1")
    cfg = _setup(tmp_path, [(row, TRUNCATED), (owner, GOOD)])
    before = _tree(cfg.data_dir)
    f = FakeFetcher({"https://a/good": GOOD, "https://a/good2": GOOD2})
    s = replace_artifacts(cfg, _plan(tmp_path, [["ghost", "replace",
                                                  "https://a/good https://a/good2", "n"]]),
                          write=True, fetcher=f)
    r = _one(s, "ghost")
    assert r["outcome"] == "duplicate-of:owner"
    assert r["new_sha256"] == sha(GOOD)
    assert f.calls == ["https://a/good"]                 # the first passing candidate decides
    assert {k: v for k, v in _tree(cfg.data_dir).items() if not k.startswith("reports/")} == before
    assert s.counts == {"duplicate-of": 1}


def test_wayback_keyword_expands_to_raw_captures(tmp_path):
    row = _row("wp101", "fr", content=TRUNCATED, pdf_url="https://www.banque-france.fr/wp101.pdf")
    cfg = _setup(tmp_path, [(row, TRUNCATED)])
    pdf_url = row["pdf_url"]
    newest = f"https://web.archive.org/web/20150101000000id_/{pdf_url}"
    older = f"https://web.archive.org/web/20090101000000id_/{pdf_url}"
    f = FakeFetcher({newest: TRUNCATED, older: GOOD},
                    cdx={pdf_url: [["timestamp", "digest"], ["20090101000000", "A"],
                                   ["20150101000000", "B"]]})
    s = replace_artifacts(cfg, _plan(tmp_path, [["wp101", "replace", "wayback", "n"]]),
                          write=True, fetcher=f)
    r = _one(s, "wp101")
    assert r["outcome"] == "replaced" and r["winning_url"] == older
    assert [t["url"] for t in r["tried"]] == ["wayback", newest, older]
    assert r["tried"][0]["reason"] == "2 capture(s)"
    assert _rows(cfg)["wp101"]["alt_urls"] == [older]
    assert _file(cfg, row).read_bytes() == GOOD


def test_wayback_unreachable_or_empty_is_a_reason(tmp_path):
    row = _row("w", "fr", content=TRUNCATED)
    cfg = _setup(tmp_path, [(row, TRUNCATED)])
    f = FakeFetcher(cdx={row["pdf_url"]: RuntimeError("proxy said no")})
    r = _one(replace_artifacts(cfg, _plan(tmp_path, [["w", "replace", "wayback", "n"]]),
                               fetcher=f), "w")
    assert r["outcome"] == "unresolved"
    assert r["tried"][0]["reason"].startswith("CDX query failed")
    r = _one(replace_artifacts(cfg, _plan(tmp_path, [["w", "replace", "wayback", "n"]]),
                               fetcher=FakeFetcher()), "w")
    assert r["tried"][0]["reason"] == "no PDF capture"


def test_dry_run_writes_nothing(tmp_path):
    row = _row("wp895", "fr", content=TRUNCATED)
    ghost = _row("ghost", "it", content=EMPTY, doc_type="C1")
    owner = _row("owner", "it", content=OWNER, doc_type="C1")
    cfg = _setup(tmp_path, [(row, TRUNCATED), (ghost, EMPTY), (owner, OWNER)])
    before = _tree(cfg.data_dir)
    f = FakeFetcher({"https://a/good": GOOD})
    s = replace_artifacts(cfg, _plan(tmp_path, [["wp895", "replace", "https://a/good", "n"],
                                                ["ghost", "retire", "duplicate-of:owner", "n"]]),
                          fetcher=f)
    assert s.dry_run is True
    assert [r["outcome"] for r in s.rows] == ["would-replace", "would-retire"]
    after = {k: v for k, v in _tree(cfg.data_dir).items() if not k.startswith("reports/")}
    assert after == before
    assert not (cfg.data_dir / "raw_orphans").exists()


def test_rendered_row_keeps_its_html_sibling(tmp_path):
    html_path = "data/raw/ecb/A3/2020/rend.html"
    row = _row("rend", "ecb", content=TRUNCATED, doc_type="A3", html_path=html_path)
    cfg = _setup(tmp_path, [(row, TRUNCATED)])
    sib = cfg.data_dir / "raw/ecb/A3/2020/rend.html"
    sib.write_bytes(b"<html>the source</html>")
    s = replace_artifacts(cfg, _plan(tmp_path, [["rend", "replace", "https://a/good", "n"]]),
                          write=True, fetcher=FakeFetcher({"https://a/good": GOOD}))
    assert _one(s, "rend")["outcome"] == "replaced"
    assert sib.read_bytes() == b"<html>the source</html>"
    new = _rows(cfg)["rend"]
    assert new["html_path"] == html_path and new["local_path"] == row["local_path"]
    assert _file(cfg, row).read_bytes() == GOOD


def test_html_artifact_row_is_refused(tmp_path):
    row = _row("page", "ecb", content=HTML, ext="html", mime="text/html")
    cfg = _setup(tmp_path, [(row, HTML)])
    f = FakeFetcher({"https://a/good": GOOD})
    r = _one(replace_artifacts(cfg, _plan(tmp_path, [["page", "replace", "https://a/good", "n"]]),
                               write=True, fetcher=f), "page")
    assert r["outcome"] == "error" and ".pdf" in r["error"]
    assert f.calls == []


def test_idempotent_second_write(tmp_path):
    row = _row("wp895", "fr", content=TRUNCATED)
    ghost = _row("ghost", "it", content=EMPTY, doc_type="C1",
                 source_url="https://bis.example/review/r200924a.htm")
    owner = _row("owner", "it", content=OWNER, doc_type="C1")
    cfg = _setup(tmp_path, [(row, TRUNCATED), (ghost, EMPTY), (owner, OWNER)])
    plan = _plan(tmp_path, [["wp895", "replace", "https://a/cut https://a/good", "n"],
                            ["ghost", "retire", "duplicate-of:owner", "BIS serves 0 bytes"]])
    f = FakeFetcher({"https://a/cut": TRUNCATED, "https://a/good": GOOD})
    s1 = replace_artifacts(cfg, plan, write=True, fetcher=f)
    assert [r["outcome"] for r in s1.rows] == ["replaced", "retired"]
    snap = _tree(cfg.data_dir)
    s2 = replace_artifacts(cfg, plan, write=True, fetcher=f)
    assert [r["outcome"] for r in s2.rows] == ["already-fixed", "already-retired"]
    after = _tree(cfg.data_dir)
    assert {k: v for k, v in after.items() if not k.startswith("reports/")} == \
        {k: v for k, v in snap.items() if not k.startswith("reports/")}


# -- retire -----------------------------------------------------------------------

def test_retire_moves_file_removes_row_stamps_owner(tmp_path):
    ghost = _row("ghost", "it", content=EMPTY, doc_type="C1",
                 pdf_url="https://www.bis.org/review/r200924a.pdf",
                 source_url="https://www.bis.org/review/r200924a.htm")
    owner = _row("owner", "it", content=OWNER, doc_type="C1")
    keep = _row("keep", "it", content=GOOD, doc_type="C1")
    other = _row("frrow", "fr", content=GOOD2)
    cfg = _setup(tmp_path, [(ghost, EMPTY), (owner, OWNER), (keep, GOOD), (other, GOOD2)])
    fr_before = cfg.manifest_file("fr").read_bytes()
    keep_line = json.dumps(keep)
    s = replace_artifacts(cfg, _plan(tmp_path, [["ghost", "retire", "duplicate-of:owner",
                                                 "same speech as owner"]]),
                          write=True, fetcher=FakeFetcher(), now="2026-11-19T10:00:00+00:00")
    r = _one(s, "ghost")
    assert r["outcome"] == "retired" and r["owner_doc_id"] == "owner"
    rows = _rows(cfg)
    assert "ghost" not in rows
    assert rows["owner"]["alt_urls"] == [ghost["pdf_url"]]
    assert rows["owner"]["alt_source_urls"] == [ghost["source_url"]]
    assert rows["owner"]["retired_duplicates"] == [{
        "doc_id": "ghost", "pdf_url": ghost["pdf_url"], "source_url": ghost["source_url"],
        "previous_sha256": sha(EMPTY), "at": "2026-11-19T10:00:00+00:00",
        "note": "same speech as owner"}]
    assert not _file(cfg, ghost).exists()
    moved = cfg.orphans_dir / "it/C1/2020/ghost.pdf"
    assert moved.exists() and moved.read_bytes() == EMPTY
    assert _file(cfg, owner).read_bytes() == OWNER
    assert keep_line in cfg.manifest_file("it").read_text().splitlines()   # verbatim
    assert cfg.manifest_file("fr").read_bytes() == fr_before
    log = (cfg.reports_dir / "duplicate_stamps.jsonl").read_text().splitlines()
    assert json.loads(log[0])["label"] == "replace-artifact"


@pytest.mark.parametrize("case", ["missing", "self", "html-owner", "bad-file", "no-file",
                                  "bad-candidates"])
def test_retire_refused_when_owner_missing_or_bad(tmp_path, case):
    ghost = _row("ghost", "it", content=EMPTY, doc_type="C1")
    owner_kw = {"content": OWNER, "doc_type": "C1"}
    owner_bytes = OWNER
    if case == "html-owner":
        owner_kw["html_path"] = "data/raw/it/C1/2020/owner.html"
    owner = _row("owner", "it", **owner_kw)
    if case == "bad-file":
        owner_bytes = TRUNCATED
    if case == "no-file":
        owner_bytes = None
    cfg = _setup(tmp_path, [(ghost, EMPTY), (owner, owner_bytes)])
    before = _tree(cfg.data_dir)
    cand = {"missing": "duplicate-of:nobody", "self": "duplicate-of:ghost",
            "bad-candidates": "https://x/y.pdf"}.get(case, "duplicate-of:owner")
    s = replace_artifacts(cfg, _plan(tmp_path, [["ghost", "retire", cand, "n"]]),
                          write=True, fetcher=FakeFetcher())
    r = _one(s, "ghost")
    assert r["outcome"] == "error", r
    assert r["error"]
    assert {k: v for k, v in _tree(cfg.data_dir).items() if not k.startswith("reports/")} == before


# -- plan row errors and CLI ------------------------------------------------------

def test_plan_row_errors_are_per_row_not_fatal(tmp_path):
    row = _row("wp895", "fr", content=TRUNCATED)
    cfg = _setup(tmp_path, [(row, TRUNCATED)])
    plan = _plan(tmp_path, [["nope", "replace", "https://a/good", "n"],
                            ["wp895", "frobnicate", "https://a/good", "n"],
                            ["wp895", "replace", "", "n"],
                            ["wp895", "replace", "https://a/good", "n"],
                            ["wp895", "replace", "https://a/good", "twice"]])
    s = replace_artifacts(cfg, plan, write=True, fetcher=FakeFetcher({"https://a/good": GOOD}))
    outs = [(r["outcome"], r["error"]) for r in s.rows]
    assert outs[0][0] == "error" and "unknown doc_id" in outs[0][1]
    assert outs[1][0] == "error" and "unknown action" in outs[1][1]
    assert outs[2][0] == "error" and "no candidates" in outs[2][1]
    assert outs[3][0] == "replaced"
    assert outs[4][0] == "error" and "repeated" in outs[4][1]


def test_cli_json_summary_and_exit_codes(tmp_path, monkeypatch, capsys):
    row = _row("wp895", "fr", content=TRUNCATED)
    cfg = _setup(tmp_path, [(row, TRUNCATED)])
    monkeypatch.chdir(tmp_path)          # Config() default data_dir = ./data
    import cb_corpus.replace_artifact as ra
    monkeypatch.setattr(ra, "Fetcher", lambda cfg: FakeFetcher({"https://a/good": GOOD}))
    plan = _plan(tmp_path, [["wp895", "replace", "https://a/good", "n"]])
    assert main(["replace-artifact", "--plan", str(plan)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["rows"][0]["outcome"] == "would-replace"
    assert _file(cfg, row).read_bytes() == TRUNCATED
    assert main(["replace-artifact", "--plan", str(plan), "--write"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows"][0]["outcome"] == "replaced"
    assert main(["replace-artifact", "--plan", str(tmp_path / "none.csv")]) == 1
    bad = tmp_path / "bad.csv"
    bad.write_text("x,y\n1,2\n")
    assert main(["replace-artifact", "--plan", str(bad)]) == 1


# -- interrupted runs and path guards ---------------------------------------------

def test_replace_finishes_a_run_killed_between_file_and_row(tmp_path):
    """File already holds the winning bytes, row still has the old sha256:
    not `already-fixed`, the row update is redone."""
    row = _row("wp895", "fr", content=TRUNCATED)
    cfg = _setup(tmp_path, [(row, GOOD)])
    s = replace_artifacts(cfg, _plan(tmp_path, [["wp895", "replace", "https://a/good", "n"]]),
                          write=True, fetcher=FakeFetcher({"https://a/good": GOOD}))
    assert _one(s, "wp895")["outcome"] == "replaced"
    new = _rows(cfg)["wp895"]
    assert new["sha256"] == sha(GOOD)
    assert new["artifact_replaced"]["previous_sha256"] == sha(TRUNCATED)


def test_retire_finishes_a_run_killed_after_the_move(tmp_path):
    """Owner already stamped and file already in raw_orphans/, row still in
    the manifest: the retire completes without a second audit entry."""
    ghost = _row("ghost", "it", content=EMPTY, doc_type="C1")
    owner = _row("owner", "it", content=OWNER, doc_type="C1")
    owner["alt_urls"] = [ghost["pdf_url"]]
    owner["retired_duplicates"] = [{"doc_id": "ghost", "pdf_url": ghost["pdf_url"]}]
    cfg = _setup(tmp_path, [(ghost, None), (owner, OWNER)])
    moved = cfg.orphans_dir / "it/C1/2020/ghost.pdf"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(EMPTY)
    s = replace_artifacts(cfg, _plan(tmp_path, [["ghost", "retire", "duplicate-of:owner", "n"]]),
                          write=True, fetcher=FakeFetcher())
    assert _one(s, "ghost")["outcome"] == "retired"
    rows = _rows(cfg)
    assert set(rows) == {"owner"}
    assert rows["owner"]["alt_urls"] == [ghost["pdf_url"]]
    assert len(rows["owner"]["retired_duplicates"]) == 1


def test_retire_refuses_to_clobber_a_different_file_in_raw_orphans(tmp_path):
    ghost = _row("ghost", "it", content=EMPTY, doc_type="C1")
    owner = _row("owner", "it", content=OWNER, doc_type="C1")
    cfg = _setup(tmp_path, [(ghost, EMPTY), (owner, OWNER)])
    clash = cfg.orphans_dir / "it/C1/2020/ghost.pdf"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"something else")
    r = _one(replace_artifacts(cfg, _plan(tmp_path, [["ghost", "retire",
                                                      "duplicate-of:owner", "n"]]),
                               write=True, fetcher=FakeFetcher()), "ghost")
    assert r["outcome"] == "error" and "raw_orphans" in r["error"]
    assert "ghost" in _rows(cfg)                          # row kept
    assert _file(cfg, ghost).exists() and clash.read_bytes() == b"something else"


def test_replace_refuses_an_artifact_outside_raw(tmp_path):
    row = _row("x", "fr", content=TRUNCATED)
    outside = tmp_path / "elsewhere" / "x.pdf"
    outside.parent.mkdir()
    outside.write_bytes(TRUNCATED)
    row["local_path"] = str(outside)
    cfg = _setup(tmp_path, [(row, None)])
    r = _one(replace_artifacts(cfg, _plan(tmp_path, [["x", "replace", "https://a/good", "n"]]),
                               write=True, fetcher=FakeFetcher({"https://a/good": GOOD})), "x")
    assert r["outcome"] == "error" and "outside" in r["error"]
    assert outside.read_bytes() == TRUNCATED
