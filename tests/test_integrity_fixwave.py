"""Fix wave on the integrity storage work (issue #21, review 2026-11):
artifact file modes, env-driven content-check knobs, the bad_content
run-report counter, checked renders in convert-html / retry-html, the
MIME sniff, `.part` temps in sweep-orphans and a read-only quarantine dir."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from cb_corpus import storage as storage_mod
from cb_corpus.storage import _atomic_write_bytes
from tests.conftest import public_resolver
from tests.test_content_integrity import (U, _rec, _render_storage, _storage,
                                          make_pdf)


@pytest.fixture
def umask():
    """Set the process umask for one test, restored afterwards."""
    old = os.umask(0o022)
    os.umask(old)
    yield lambda m: os.umask(m)
    os.umask(old)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# -- M1: artifacts follow the umask ----------------------------------------

@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o002, 0o664)])
def test_atomic_write_follows_the_umask(tmp_path, umask, mask, expected):
    umask(mask)
    p = tmp_path / "a.pdf"
    _atomic_write_bytes(p, b"data")
    assert _mode(p) == expected


@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o002, 0o664)])
def test_saved_pdf_follows_the_umask(tmp_path, umask, mask, expected):
    umask(mask)
    st = _storage(tmp_path, {U: (make_pdf(), "application/pdf")})
    rec = _rec(U)
    assert st.save(rec) == "saved"
    assert _mode(Path(rec.local_path)) == expected


@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o002, 0o664)])
def test_rendered_pdf_follows_the_umask(tmp_path, monkeypatch, umask, mask, expected):
    umask(mask)
    st, _ = _render_storage(tmp_path, monkeypatch,
                            lambda out: out.write_bytes(make_pdf("r")))
    rec = _rec(U, mime="text/html")
    assert st.save(rec) == "saved"
    assert rec.local_path.endswith(".pdf")
    assert _mode(Path(rec.local_path)) == expected
    assert _mode(Path(rec.html_path)) == expected


# -- S4: sniff the bytes before trusting an HTML mime header ---------------

@pytest.mark.parametrize("producer_mime", ["application/pdf", "text/html", ""])
@pytest.mark.parametrize("served", ["text/html", "text/html; charset=utf-8",
                                    "application/xhtml+xml"])
def test_pdf_bytes_served_as_html_are_stored_as_pdf(tmp_path, producer_mime, served):
    body = make_pdf("mislabelled")
    st = _storage(tmp_path, {U: (body, served)})
    rec = _rec(U, mime=producer_mime)
    assert st.save(rec) == "saved"
    assert rec.mime_type == "application/pdf"
    assert rec.local_path.endswith(".pdf") and rec.html_path is None
    assert Path(rec.local_path).read_bytes() == body


def test_html_bytes_served_as_html_still_rejected_for_a_pdf_producer(tmp_path):
    from cb_corpus.storage import ContentCheckFailed
    st = _storage(tmp_path, {U: (b"<html>wall</html>" * 100, "text/html")})
    with pytest.raises(ContentCheckFailed):
        st.save(_rec(U))


# -- S2: bad_content in the run report, reasons survive the 500-char cut --

def test_save_many_reports_bad_content_as_a_subcount_of_error(tmp_path):
    from cb_corpus.runreport import RunReport
    good = "https://bank.example/good.pdf"
    dead = "https://bank.example/dead.pdf"
    st = _storage(tmp_path, {U: (b"<html>wall</html>" * 100, "text/html"),
                             good: (make_pdf("good"), "application/pdf")})
    counts = st.save_many([_rec(U), _rec(good), _rec(dead)], label="xx:C1")
    assert counts == {"error": 2, "saved": 1, "bad-content": 1}
    report = RunReport("cb", "discover")
    s = report.source("xx")
    s.record_saved_counts(counts)
    assert (s.docs_seen, s.docs_new, s.docs_failed, s.bad_content) == (3, 1, 2, 1)
    d = report.to_dict()
    assert d["sources"][0]["bad_content"] == 1
    assert d["totals"]["bad_content"] == 1
    assert d["totals"]["docs_seen"] == 3


def test_save_many_without_rejection_has_no_bad_content_key(tmp_path):
    st = _storage(tmp_path, {U: (make_pdf(), "application/pdf")})
    assert st.save_many([_rec(U)]) == {"saved": 1}


def test_bad_content_does_not_change_the_exit_code():
    from cb_corpus.runreport import RunReport
    for counts, code in (({"saved": 1, "error": 1, "bad-content": 1}, 0),
                         ({"error": 1, "bad-content": 1}, 3)):
        r = RunReport("cb", "discover")
        r.source("xx").record_saved_counts(counts)
        assert r.finish() == code


def test_reasons_come_before_the_long_urls_in_the_error_record(tmp_path):
    import json
    long = "https://bank.example/" + "very-long-path-segment/" * 20
    urls = [long + "a.pdf", long + "b.pdf", long + "c.pdf"]
    st = _storage(tmp_path, {
        urls[0]: (b"<html>wall</html>" * 100, "text/html"),
        urls[1]: (b"%PDF-1.4\n" + b"x" * 5000, "application/pdf"),     # no %%EOF
        urls[2]: (b"%PDF-1.4 tiny", "application/pdf"),
    })
    st.save_many([_rec(urls[0], alt_urls=urls[1:])], label="xx:C1")
    entry = json.loads((tmp_path / "download_errors.jsonl").read_text())
    err = entry["error"]
    assert len(err) <= 500
    assert "the server sent HTML" in err
    assert "truncated PDF" in err
    assert "too small for a PDF" in err


# -- S1: the three content-check knobs from the environment ----------------

from cb_corpus import config as config_mod                         # noqa: E402
from cb_corpus.config import Config                                # noqa: E402

_KNOB_ENV = ("CB_MIN_PDF_BYTES", "CB_PDF_REQUIRE_EOF", "CB_PDF_URL_SERVED_HTML")


@pytest.fixture
def clean_knob_env(monkeypatch):
    for name in _KNOB_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config_mod, "_ENV_WARNED", set())
    return monkeypatch


def test_knobs_default_without_env(clean_knob_env):
    c = Config()
    assert (c.min_pdf_bytes, c.pdf_require_eof, c.pdf_url_served_html) == (
        1024, True, "reject")


@pytest.mark.parametrize("raw,expected", [("0", 0), ("4096", 4096), (" 2048 ", 2048)])
def test_min_pdf_bytes_from_env(clean_knob_env, raw, expected):
    clean_knob_env.setenv("CB_MIN_PDF_BYTES", raw)
    assert Config().min_pdf_bytes == expected


@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("No", False), ("off", False),
    ("1", True), ("TRUE", True), ("yes", True), ("on", True)])
def test_pdf_require_eof_from_env(clean_knob_env, raw, expected):
    clean_knob_env.setenv("CB_PDF_REQUIRE_EOF", raw)
    assert Config().pdf_require_eof is expected


@pytest.mark.parametrize("raw,expected", [("accept", "accept"), (" Reject ", "reject")])
def test_pdf_url_served_html_from_env(clean_knob_env, raw, expected):
    clean_knob_env.setenv("CB_PDF_URL_SERVED_HTML", raw)
    assert Config().pdf_url_served_html == expected


@pytest.mark.parametrize("name,raw,field,default", [
    ("CB_MIN_PDF_BYTES", "-5", "min_pdf_bytes", 1024),
    ("CB_MIN_PDF_BYTES", "1k", "min_pdf_bytes", 1024),
    ("CB_PDF_REQUIRE_EOF", "maybe", "pdf_require_eof", True),
    ("CB_PDF_URL_SERVED_HTML", "allow", "pdf_url_served_html", "reject"),
])
def test_invalid_env_warns_loudly_and_keeps_the_default(clean_knob_env, capsys,
                                                        name, raw, field, default):
    clean_knob_env.setenv(name, raw)
    assert getattr(Config(), field) == default          # never a crash
    assert getattr(Config(), field) == default
    err = capsys.readouterr().err
    assert err.count("WARNING") == 1                    # once per process, not per Config
    assert name in err and repr(raw) in err


def test_empty_env_means_default(clean_knob_env, capsys):
    for name in _KNOB_ENV:
        clean_knob_env.setenv(name, "")
    c = Config()
    assert (c.min_pdf_bytes, c.pdf_require_eof, c.pdf_url_served_html) == (
        1024, True, "reject")
    assert capsys.readouterr().err == ""


def test_explicit_argument_wins_over_env(clean_knob_env):
    clean_knob_env.setenv("CB_MIN_PDF_BYTES", "4096")
    assert Config(min_pdf_bytes=10).min_pdf_bytes == 10


def test_env_knobs_reach_storage(clean_knob_env, tmp_path):
    clean_knob_env.setenv("CB_MIN_PDF_BYTES", "0")
    clean_knob_env.setenv("CB_PDF_REQUIRE_EOF", "0")
    from cb_corpus.storage import Storage
    from tests.test_content_integrity import FakeFetcher
    st = Storage(Config(data_dir=tmp_path, html_to_pdf=False),
                 FakeFetcher({U: (b"%PDF-1.4 tiny", "application/pdf")}))
    assert st.save(_rec(U)) == "saved"


# -- S3: convert-html and retry-html render through the checked helper ----

import json as _json                                                # noqa: E402
import subprocess                                                   # noqa: E402


def _seed_html_row(tmp_path, code="a"):
    """One text/html manifest row with its HTML file on disk."""
    cfg = Config(data_dir=tmp_path)
    html = tmp_path / "raw" / "ecb" / "A3" / "2024" / f"{code}.html"
    html.parent.mkdir(parents=True, exist_ok=True)
    html.write_bytes(b"<html>seed</html>")
    cfg.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg.manifest_path.open("a") as fh:
        fh.write(_json.dumps({
            "bank_code": "ecb", "doc_type": "A3", "title": "x",
            "pdf_url": f"https://e.test/{code}.en.html", "date": "2024-01-01",
            "year": 2024, "doc_id": code, "mime_type": "text/html",
            "local_path": str(html), "sha256": None, "language": "en"}) + "\n")
    return cfg, html


def _row(cfg, code="a"):
    from cb_corpus.storage import iter_manifest_rows
    return next(r for r in iter_manifest_rows(cfg) if r["doc_id"] == code)


def _dir_names(p: Path) -> list[str]:
    return sorted(x.name for x in p.iterdir())


def test_convert_renders_to_a_temp_then_moves_into_place(tmp_path, monkeypatch):
    from cb_corpus import convert as convert_mod
    cfg, html = _seed_html_row(tmp_path)
    monkeypatch.setattr(convert_mod, "find_chrome", lambda: "/fake/chrome")
    outputs = []

    def fake_render(url, output, **kw):
        outputs.append(output)
        output.write_bytes(make_pdf("ok"))
    monkeypatch.setattr(convert_mod, "render_url_to_pdf", fake_render)
    counts = convert_mod.convert_existing(cfg, resolver=public_resolver)
    assert counts == {"converted": 1, "refused": 0}
    pdf = html.with_suffix(".pdf")
    [out] = outputs
    assert out != pdf and out.parent == pdf.parent
    assert _dir_names(html.parent) == ["a.html", "a.pdf"]


@pytest.mark.parametrize("partial", [b"%PDF-1.4\n" + b"x" * 5000, b"<html>err</html>"])
def test_convert_rejects_bad_render_output(tmp_path, monkeypatch, partial, capsys):
    from cb_corpus import convert as convert_mod
    cfg, html = _seed_html_row(tmp_path)
    monkeypatch.setattr(convert_mod, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(convert_mod, "render_url_to_pdf",
                        lambda url, output, **kw: output.write_bytes(partial))
    counts = convert_mod.convert_existing(cfg, resolver=public_resolver)
    assert counts.get("error") == 1 and "converted" not in counts
    assert _dir_names(html.parent) == ["a.html"]          # no .pdf, no temp
    assert _row(cfg)["mime_type"] == "text/html"
    assert "render output rejected" in capsys.readouterr().err


def test_convert_killed_render_leaves_no_partial_pdf(tmp_path, monkeypatch):
    from cb_corpus import convert as convert_mod
    cfg, html = _seed_html_row(tmp_path)
    monkeypatch.setattr(convert_mod, "find_chrome", lambda: "/fake/chrome")

    def dies(url, output, **kw):
        output.write_bytes(b"%PDF-1.4 half")
        raise RuntimeError("Chrome timed out")
    monkeypatch.setattr(convert_mod, "render_url_to_pdf", dies)
    assert convert_mod.convert_existing(cfg, resolver=public_resolver).get("error") == 1
    assert _dir_names(html.parent) == ["a.html"]


def _strategy(tmp_path, monkeypatch, behaviour):
    """Run retry_html._try_strategy with a fake chrome_print_to_pdf."""
    from cb_corpus import retry_html as retry_mod
    seen = []

    def fake_print(chrome, url, output, udd, timeout, **kw):
        seen.append(output)
        behaviour(output)
    monkeypatch.setattr(retry_mod, "chrome_print_to_pdf", fake_print)
    final = tmp_path / "d" / "a.pdf"
    final.parent.mkdir()
    ok = retry_mod._try_strategy("/fake/chrome", U, final, timeout=1,
                                 virtual_time_budget_ms=None, user_data_dir="x",
                                 cfg=Config(data_dir=tmp_path))
    return ok, final, seen


def test_retry_strategy_success_goes_through_a_temp(tmp_path, monkeypatch):
    ok, final, [out] = _strategy(tmp_path, monkeypatch,
                                 lambda o: o.write_bytes(make_pdf("ok")))
    assert ok and out != final and final.read_bytes() == make_pdf("ok")
    assert _dir_names(final.parent) == ["a.pdf"]


def test_retry_strategy_rejects_bad_output(tmp_path, monkeypatch):
    ok, final, _ = _strategy(tmp_path, monkeypatch,
                             lambda o: o.write_bytes(b"%PDF-1.4\n" + b"x" * 5000))
    assert not ok and _dir_names(final.parent) == []


def test_retry_strategy_timeout_leaves_no_partial_pdf(tmp_path, monkeypatch):
    def half_then_timeout(o):
        o.write_bytes(b"%PDF-1.4 half")
        raise subprocess.TimeoutExpired("chrome", 1)
    ok, final, _ = _strategy(tmp_path, monkeypatch, half_then_timeout)
    assert not ok and _dir_names(final.parent) == []


def test_retry_does_not_adopt_a_partial_pdf_left_on_disk(tmp_path, monkeypatch, capsys):
    from cb_corpus import retry_html as retry_mod
    cfg, html = _seed_html_row(tmp_path)
    partial = html.with_suffix(".pdf")
    partial.write_bytes(b"%PDF-1.4 half")               # left by a killed run
    monkeypatch.setattr(retry_mod, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(retry_mod, "chrome_print_to_pdf",
                        lambda chrome, url, output, *a, **kw: output.write_bytes(make_pdf("new")))
    counts = retry_mod.retry_failed(cfg, parallelism=1, resolver=public_resolver)
    assert counts.get("recovered") == 1 and "already-pdf" not in counts
    assert partial.read_bytes() == make_pdf("new")
    assert _row(cfg)["mime_type"] == "application/pdf"
    assert "not adopted" in capsys.readouterr().err


def test_retry_still_adopts_a_valid_pdf_left_on_disk(tmp_path, monkeypatch):
    from cb_corpus import retry_html as retry_mod
    cfg, html = _seed_html_row(tmp_path)
    html.with_suffix(".pdf").write_bytes(make_pdf("done"))
    monkeypatch.setattr(retry_mod, "find_chrome", lambda: "/fake/chrome")
    counts = retry_mod.retry_failed(cfg, parallelism=1, resolver=public_resolver)
    assert counts.get("already-pdf") == 1


# -- S5: `.part` temps in sweep-orphans -----------------------------------

from datetime import datetime, timezone                             # noqa: E402

from cb_corpus import orphans                                       # noqa: E402

_NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _temp(cfg, rel: str, data: bytes, age_s: float) -> Path:
    p = cfg.raw_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    t = _NOW.timestamp() - age_s
    os.utime(p, (t, t))
    return p


def _sweep_cfg(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.raw_dir.mkdir(parents=True)
    cfg.manifest_dir.mkdir(parents=True)
    return cfg


def _report(summary) -> list[dict]:
    return [_json.loads(l) for l in Path(summary.report_path).read_text().splitlines()]


def test_is_temp_part():
    assert orphans.is_temp_part(Path(".abc.pdf.0123456789ab.part"))
    assert not orphans.is_temp_part(Path("abc.part"))          # not hidden
    assert not orphans.is_temp_part(Path(".abc.pdf"))


def test_recent_temp_is_ignored_and_old_one_reported_as_stale(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    fresh = _temp(cfg, "xx/C1/2025/.a.pdf.111111111111.part", b"%PDF half", 60)
    stale = _temp(cfg, "xx/C1/2025/.b.pdf.222222222222.part", b"%PDF half2", 3 * 86400)
    s = orphans.sweep_orphans(cfg, now=_NOW)
    assert (s.temps_fresh, s.temps_stale) == (1, 1)
    assert (s.orphans, s.unindexed, s.duplicates) == (0, 0, 0)
    [entry] = _report(s)
    assert entry["rel"] == "xx/C1/2025/.b.pdf.222222222222.part"
    assert entry["action"] == "stale-temp"
    assert fresh.exists() and stale.exists()                  # dry run: nothing moves


def test_stale_temp_moved_only_with_move(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    fresh = _temp(cfg, "xx/C1/2025/.a.pdf.111111111111.part", b"%PDF half", 60)
    stale = _temp(cfg, "xx/C1/2025/.b.pdf.222222222222.part", b"%PDF half2", 3 * 86400)
    s = orphans.sweep_orphans(cfg, move=True, now=_NOW)
    assert s.temps_stale == 1 and s.moved == 1
    assert fresh.exists() and not stale.exists()
    assert (cfg.orphans_dir / "xx/C1/2025/.b.pdf.222222222222.part").read_bytes() == b"%PDF half2"


def test_temp_min_age_is_configurable(tmp_path):
    cfg = _sweep_cfg(tmp_path)
    _temp(cfg, "xx/C1/2025/.a.pdf.111111111111.part", b"%PDF half", 600)
    assert orphans.sweep_orphans(cfg, now=_NOW).temps_fresh == 1
    s = orphans.sweep_orphans(cfg, now=_NOW, temp_min_age=300)
    assert (s.temps_fresh, s.temps_stale) == (0, 1)


def test_cli_sweep_orphans_temp_min_age_flag(tmp_path, monkeypatch, capsys):
    from cb_corpus import cli
    monkeypatch.chdir(tmp_path)
    cfg = Config()
    cfg.raw_dir.mkdir(parents=True)
    p = cfg.raw_dir / "xx" / ".a.pdf.111111111111.part"
    p.parent.mkdir()
    p.write_bytes(b"x")
    t = datetime.now(timezone.utc).timestamp() - 2 * 3600
    os.utime(p, (t, t))
    assert cli.main(["sweep-orphans", "--temp-min-age-hours", "1"]) == 0
    out = capsys.readouterr().out
    assert '"temps_stale": 1' in out and "1 stale temp" in out
