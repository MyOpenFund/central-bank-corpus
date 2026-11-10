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
