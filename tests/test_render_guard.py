"""B8 (grit wave 2026-11): headless Chrome (run with --no-sandbox) is never
pointed at a private-network URL. No real DNS: every resolver is a fake."""
from __future__ import annotations

import socket
from datetime import date

import pytest

from cb_corpus import storage as storage_mod
from cb_corpus.config import Config
from cb_corpus.htmlpdf import render_allowed
from cb_corpus.models import DocRecord
from cb_corpus.storage import Storage
from cb_corpus.taxonomy import DocType

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def _resolver(mapping):
    """Fake getaddrinfo: host -> list of address strings; unknown -> gaierror."""
    calls = []

    def resolve(host, port, *a, **k):
        calls.append(host)
        if host not in mapping:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        out = []
        for addr in mapping[host]:
            fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
            sa = (addr, 0, 0, 0) if fam == socket.AF_INET6 else (addr, 0)
            out.append((fam, socket.SOCK_STREAM, 6, "", sa))
        return out
    resolve.calls = calls
    return resolve


CFG = Config()


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://10.1.2.3/x", "https://192.168.1.10/x",
    "http://172.16.5.4/x", "http://100.100.100.100/x",          # tailnet CGNAT
    "http://169.254.169.254/latest/meta-data/",                  # cloud metadata
    "http://0.0.0.0/x", "http://224.0.0.1/x", "http://240.0.0.1/x",
    "http://[::1]/x", "http://[fc00::1]/x", "http://[fd12:3456::1]/x",
    "http://[fe80::1]/x", "http://[::]/x", "http://[ff02::1]/x",
    "http://[::ffff:127.0.0.1]/x",                               # v4-mapped loopback
])
def test_ip_literals_in_blocked_networks_refused_without_resolving(url):
    res = _resolver({})
    reason = render_allowed(url, CFG, resolver=res)
    assert reason and "blocked" in reason
    assert res.calls == []          # literals are checked directly


def test_localhost_refused():
    reason = render_allowed("http://localhost:8080/admin", CFG,
                            resolver=_resolver({"localhost": ["127.0.0.1", "::1"]}))
    assert reason and "127.0.0.1" in reason


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x.org/a",
                                 "javascript:alert(1)", "data:text/html,hi", "x.org/a"])
def test_non_http_schemes_refused(url):
    reason = render_allowed(url, CFG, resolver=_resolver({}))
    assert reason and "scheme" in reason


def test_missing_host_refused():
    assert "host" in render_allowed("http:///x", CFG, resolver=_resolver({}))


def test_public_host_resolving_to_private_address_refused():
    res = _resolver({"evil.example": [PUBLIC_V4, "10.0.0.5"]})
    reason = render_allowed("https://evil.example/x", CFG, resolver=res)
    assert reason and "10.0.0.5" in reason


def test_resolution_failure_refused():
    reason = render_allowed("https://nowhere.invalid/x", CFG, resolver=_resolver({}))
    assert reason and "resolve" in reason


def test_empty_resolution_refused():
    reason = render_allowed("https://empty.example/x", CFG,
                            resolver=_resolver({"empty.example": []}))
    assert reason and "resolve" in reason


def test_public_address_allowed():
    res = _resolver({"www.ecb.europa.eu": [PUBLIC_V4, PUBLIC_V6]})
    assert render_allowed("https://www.ecb.europa.eu/press/x.en.html", CFG, resolver=res) is None
    assert render_allowed(f"http://{PUBLIC_V4}/x", CFG, resolver=res) is None
    assert render_allowed(f"http://[{PUBLIC_V6}]/x", CFG, resolver=res) is None


def test_blocked_networks_configurable():
    cfg = Config(render_blocked_networks=("93.184.216.0/24",))
    assert render_allowed(f"http://{PUBLIC_V4}/x", cfg, resolver=_resolver({}))
    assert render_allowed("http://10.0.0.1/x", cfg, resolver=_resolver({})) is None


def test_config_default_covers_the_documented_ranges():
    nets = set(Config().render_blocked_networks)
    assert {"127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
            "169.254.0.0/16", "100.64.0.0/10", "0.0.0.0/8", "224.0.0.0/4",
            "240.0.0.0/4", "::1/128", "fc00::/7", "fe80::/10", "::/128",
            "ff00::/8"} <= nets


@pytest.mark.parametrize("bad", [("not-a-net",), ("10.0.0.0/33",), "10.0.0.0/8"])
def test_config_rejects_invalid_networks(bad):
    with pytest.raises(ValueError, match="render_blocked_networks"):
        Config(render_blocked_networks=bad)


# ---- Storage integration ---------------------------------------------

def _html_storage(tmp_path, monkeypatch, resolver):
    st = Storage(Config(data_dir=tmp_path, html_to_pdf=True))
    st.render_resolver = resolver
    st.fetcher.get_bytes = lambda url: (b"<html>raw</html>", "text/html")
    st.fetcher.throttle = lambda url: None
    rendered = []

    def fake_render(url, output, **kw):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF-1.4 stub")
        rendered.append(url)
    monkeypatch.setattr(storage_mod, "render_url_to_pdf", fake_render)
    return st, rendered


def test_storage_refuses_render_keeps_html_counts_and_warns(tmp_path, monkeypatch, capsys):
    st, rendered = _html_storage(tmp_path, monkeypatch,
                                 _resolver({"intranet.example": ["192.168.0.7"]}))
    rec = DocRecord(bank_code="ecb", doc_type=DocType.A3, title="x",
                    pdf_url="http://intranet.example/x.html", date=date(2024, 1, 1))
    assert st.save(rec) == "saved"
    assert rendered == []                              # Chrome never launched
    assert rec.mime_type == "text/html" and rec.local_path.endswith(".html")
    assert st.render_refused == 1
    err = capsys.readouterr().err
    assert "render refused" in err and "intranet.example" in err


def test_storage_renders_public_url(tmp_path, monkeypatch):
    st, rendered = _html_storage(tmp_path, monkeypatch,
                                 _resolver({"www.ecb.europa.eu": [PUBLIC_V4]}))
    rec = DocRecord(bank_code="ecb", doc_type=DocType.A3, title="x",
                    pdf_url="https://www.ecb.europa.eu/x.en.html", date=date(2024, 1, 1))
    assert st.save(rec) == "saved"
    assert rendered == [rec.pdf_url]
    assert rec.mime_type == "application/pdf" and st.render_refused == 0


@pytest.mark.filterwarnings("ignore:A test tried to use socket.getaddrinfo")
def test_suite_never_does_real_dns():
    """pytest-socket (--disable-socket) also blocks socket.getaddrinfo, so a
    test that forgets to inject a resolver can never resolve for real."""
    with pytest.raises(Exception, match="getaddrinfo"):
        socket.getaddrinfo("www.ecb.europa.eu", 443)


@pytest.mark.filterwarnings("ignore:A test tried to use socket.getaddrinfo")
def test_default_resolver_failure_fails_closed():
    """Without an injected resolver the real getaddrinfo is used; any error
    it raises (here pytest-socket's block) refuses the render, never crashes."""
    reason = render_allowed("https://www.ecb.europa.eu/x", CFG, resolver=None)
    assert reason and "could not resolve" in reason


# ---- fix wave: parser confusion, embedded-IPv4 ranges, choke point -----

import json  # noqa: E402
from pathlib import Path  # noqa: E402
import subprocess  # noqa: E402

from cb_corpus import htmlpdf  # noqa: E402
from cb_corpus.htmlpdf import RenderRefused, chrome_print_to_pdf, render_url_to_pdf  # noqa: E402


@pytest.mark.parametrize("url", [
    "http://10.0.0.1\\@8.8.8.8/",            # urllib: 8.8.8.8 -- Chrome: 10.0.0.1
    "http://8.8.8.8\\x",
    "http://8.8.8.8/a b", "http://8.8.8.8/\tx", "http://8.8.8.8/\nx",
    " http://8.8.8.8/", "http://8.8.8.8/\x00", "http://8.8.8.8/\x7f",
])
def test_backslash_whitespace_control_refused_before_parsing(url):
    res = _resolver({})
    reason = render_allowed(url, CFG, resolver=res)
    assert reason and "backslash, whitespace or control" in reason
    assert res.calls == []


@pytest.mark.parametrize("url", [
    "http://[64:ff9b::a00:1]/",              # NAT64 of 10.0.0.1
    "http://[2002:a00:1::1]/",               # 6to4 of 10.0.0.1
    "http://[::a00:1]/",                     # IPv4-compatible 10.0.0.1
    "http://[fec0::1]/",                     # deprecated site-local
])
def test_ipv6_forms_embedding_ipv4_or_site_local_refused(url):
    reason = render_allowed(url, CFG, resolver=_resolver({}))
    assert reason and "blocked network" in reason


def test_userinfo_is_not_the_host():
    assert "blocked" in render_allowed("http://user:pw@10.0.0.1/x", CFG, resolver=_resolver({}))
    # userinfo that LOOKS like a private IP: the real host is the public one
    res = _resolver({"pub.example": [PUBLIC_V4]})
    assert render_allowed("http://10.0.0.1@pub.example/x", CFG, resolver=res) is None


def test_uppercase_scheme_still_checked():
    assert "blocked" in render_allowed("HTTP://127.0.0.1/", CFG, resolver=_resolver({}))
    res = _resolver({"www.ecb.europa.eu": [PUBLIC_V4]})
    assert render_allowed("HTTPS://www.ecb.europa.eu/x", CFG, resolver=res) is None


def test_trailing_dot_host():
    assert "blocked" in render_allowed("http://127.0.0.1./x", CFG, resolver=_resolver({}))
    res = _resolver({"localhost": ["127.0.0.1"]})
    assert "127.0.0.1" in render_allowed("http://localhost./x", CFG, resolver=res)
    assert res.calls == ["localhost"]


@pytest.mark.parametrize("url", ["http://x.example:99999/", "http://x.example:port/"])
def test_bad_port_refused(url):
    assert "unparseable" in render_allowed(url, CFG, resolver=_resolver({"x.example": [PUBLIC_V4]}))


def test_unparseable_resolved_address_fails_closed():
    def res(host, port, *a, **k):
        return [(2, 1, 6, "", ("not-an-ip", 0))]
    reason = render_allowed("http://odd.example/", CFG, resolver=res)
    assert reason and "unparseable address" in reason


def test_ipv6_zone_id_stripped():
    assert "blocked" in render_allowed("http://[fe80::1%25eth0]/", CFG, resolver=_resolver({}))
    reason = render_allowed("http://scoped.example/", CFG,
                            resolver=_resolver({"scoped.example": ["fe80::1%eth0"]}))
    assert reason and "fe80::/10" in reason


def _no_launch(monkeypatch):
    def launched(*a, **k):
        raise AssertionError("Chrome launched")
    monkeypatch.setattr(htmlpdf.subprocess, "run", launched)


def test_render_url_to_pdf_is_guarded(tmp_path, monkeypatch):
    _no_launch(monkeypatch)
    with pytest.raises(RenderRefused) as ei:
        render_url_to_pdf("http://169.254.169.254/latest", tmp_path / "o.pdf",
                          chrome="/fake/chrome", resolver=_resolver({}))
    assert "blocked" in ei.value.reason


def test_chrome_print_to_pdf_is_guarded(tmp_path, monkeypatch):
    _no_launch(monkeypatch)
    with pytest.raises(RenderRefused):
        chrome_print_to_pdf("/fake/chrome", "file:///etc/passwd", tmp_path / "o.pdf",
                            str(tmp_path), 10)


def test_chrome_print_to_pdf_launches_for_public_url(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(htmlpdf.subprocess, "run", lambda cmd, **k: seen.append(cmd))
    chrome_print_to_pdf("/fake/chrome", "https://pub.example/x", tmp_path / "o.pdf",
                        str(tmp_path), 10, extra_args=("--virtual-time-budget=5",),
                        resolver=_resolver({"pub.example": [PUBLIC_V4]}))
    [cmd] = seen
    assert "--no-sandbox" in cmd and "--virtual-time-budget=5" in cmd
    assert cmd[-1] == "https://pub.example/x"


def _html_manifest(tmp_path, url):
    html = tmp_path / "raw" / "ecb" / "A3" / "2024" / "d1.html"
    html.parent.mkdir(parents=True, exist_ok=True)
    html.write_bytes(b"<html>x</html>")
    row = {"bank_code": "ecb", "doc_type": "A3", "title": "x", "pdf_url": url,
           "date": "2024-01-01", "year": 2024, "doc_id": "d1", "mime_type": "text/html",
           "local_path": str(html), "sha256": None, "language": "en"}
    mf = tmp_path / "manifest" / "ecb.jsonl"
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_text(json.dumps(row) + "\n")
    return mf


def test_convert_html_refuses_private_url_and_leaves_row(tmp_path, monkeypatch, capsys):
    from cb_corpus import convert as convert_mod
    mf = _html_manifest(tmp_path, "http://192.168.1.5/x.en.html")
    before = mf.read_bytes()
    monkeypatch.setattr(convert_mod, "find_chrome", lambda: "/fake/chrome")
    _no_launch(monkeypatch)
    counts = convert_mod.convert_existing(Config(data_dir=tmp_path), resolver=_resolver({}))
    assert counts["refused"] == 1 and "converted" not in counts
    assert mf.read_bytes() == before
    assert "[convert-html] render refused: 1" in capsys.readouterr().err


def test_retry_html_refuses_private_url_and_leaves_row(tmp_path, monkeypatch, capsys):
    from cb_corpus import retry_html as retry_mod
    mf = _html_manifest(tmp_path, "http://intranet.example/x.en.html")
    before = mf.read_bytes()
    monkeypatch.setattr(retry_mod, "find_chrome", lambda: "/fake/chrome")
    _no_launch(monkeypatch)
    counts = retry_mod.retry_failed(Config(data_dir=tmp_path),
                                    resolver=_resolver({"intranet.example": ["10.9.8.7"]}))
    assert counts["refused"] == 1 and "still-fail" not in counts
    assert mf.read_bytes() == before
    assert not (tmp_path / "failed_urls.txt").exists()
    assert "[retry-html] render refused: 1" in capsys.readouterr().err


def test_retry_html_strategy_goes_through_the_guard(tmp_path, monkeypatch):
    from cb_corpus import retry_html as retry_mod
    _no_launch(monkeypatch)
    with pytest.raises(RenderRefused):
        retry_mod._try_strategy("/fake/chrome", "http://127.0.0.1/", tmp_path / "o.pdf",
                                timeout=1, virtual_time_budget_ms=None,
                                user_data_dir=str(tmp_path))


def test_save_many_prints_render_refused_summary(tmp_path, monkeypatch, capsys):
    st, _rendered = _html_storage(tmp_path, monkeypatch, _resolver({}))
    recs = [DocRecord(bank_code="ecb", doc_type=DocType.A3, title=n,
                      pdf_url=f"http://10.0.0.{i}/{n}.html")
            for i, n in enumerate(("a", "b"), start=1)]
    st.fetcher.get_bytes = lambda url: (url.encode(), "text/html")   # distinct bytes
    st.save_many(recs, label="ecb")
    assert "[ecb] render refused: 2" in capsys.readouterr().err
    st.save_many([], label="ecb")
    assert "render refused" not in capsys.readouterr().err   # per batch, only when > 0


# ---- final-review fix wave: resolver pass-through, summary wording ------

def test_retry_html_passes_resolver_to_the_chrome_choke_point(tmp_path, monkeypatch, capsys):
    """The resolver given to retry_failed must reach chrome_print_to_pdf's
    own guard too (not just the pre-check): with the suite's no-DNS default
    there, the render would be refused at launch time."""
    from cb_corpus import retry_html as retry_mod
    mf = _html_manifest(tmp_path, "https://pub.example/x.en.html")
    monkeypatch.setattr(retry_mod, "find_chrome", lambda: "/fake/chrome")

    def fake_run(cmd, **k):
        out = next(a for a in cmd if a.startswith("--print-to-pdf="))
        Path(out.split("=", 1)[1]).write_bytes(b"%PDF-1.4 x")
    monkeypatch.setattr(htmlpdf.subprocess, "run", fake_run)
    counts = retry_mod.retry_failed(Config(data_dir=tmp_path), parallelism=1,
                                    resolver=_resolver({"pub.example": [PUBLIC_V4]}))
    assert counts == {"refused": 0, "recovered": 1}
    assert json.loads(mf.read_text())["mime_type"] == "application/pdf"


def test_render_refused_summaries_name_unresolvable_hosts(tmp_path, monkeypatch, capsys):
    from cb_corpus import convert as convert_mod
    from cb_corpus import retry_html as retry_mod
    _no_launch(monkeypatch)
    _html_manifest(tmp_path, "http://nowhere.example/x.en.html")   # unresolvable
    monkeypatch.setattr(convert_mod, "find_chrome", lambda: "/fake/chrome")
    monkeypatch.setattr(retry_mod, "find_chrome", lambda: "/fake/chrome")
    convert_mod.convert_existing(Config(data_dir=tmp_path), resolver=_resolver({}))
    retry_mod.retry_failed(Config(data_dir=tmp_path), resolver=_resolver({}))
    err = capsys.readouterr().err
    assert "[convert-html] render refused: 1 (private network, unresolvable host or " \
           "non-http URL(s); rows left as HTML)" in err
    assert "[retry-html] render refused: 1 (private network, unresolvable host or " \
           "non-http URL(s); rows left as HTML)" in err
    st, _ = _html_storage(tmp_path / "s", monkeypatch, _resolver({}))
    st.fetcher.get_bytes = lambda url: (b"<html>x</html>", "text/html")
    st.save_many([DocRecord(bank_code="ecb", doc_type=DocType.A3, title="n",
                            pdf_url="https://nowhere.example/n.html")], label="ecb")
    assert "[ecb] render refused: 1 (private network or unresolvable host; HTML kept)" \
        in capsys.readouterr().err
