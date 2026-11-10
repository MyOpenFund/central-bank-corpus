"""Fetcher retry policy for HTTP errors (issue #30).

A 4xx outside `Config.retry_http_4xx` fails at once (one call, no backoff
sleep); retried 4xx, 5xx and network errors keep the `max_retries` contract.
`http_status` digs the status out of an exception chain.
"""
from __future__ import annotations

import pytest

from cb_corpus import http
from cb_corpus.config import Config


class _Resp:
    headers = {"content-type": "application/pdf"}

    def __init__(self, code: int):
        self.status_code = code
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise http.requests.exceptions.HTTPError(
                f"HTTP {self.status_code}", response=self)

    def iter_content(self, chunk_size):
        yield b"ok"

    @property
    def text(self):
        return "ok"

    def close(self):
        self.closed = True


class _Session:
    """Fake session: `codes` is played in order (last one repeats); or `exc` raised."""

    def __init__(self, code=None, exc=None, codes=None):
        self.codes = list(codes) if codes is not None else [code]
        self.exc, self.calls, self.responses = exc, 0, []

    def get(self, *a, **k):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        r = _Resp(self.codes[min(self.calls, len(self.codes)) - 1])
        self.responses.append(r)
        return r


@pytest.fixture
def sleeps(monkeypatch):
    rec: list[float] = []
    monkeypatch.setattr(http.time, "sleep", lambda s: rec.append(s))
    return rec


def _fetcher(session, cfg=None):
    f = http.Fetcher(cfg or Config(min_delay_seconds=0.0))
    f.session = session
    return f


METHODS = ["get", "get_bytes"]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("code", [400, 404, 410])
def test_non_retried_4xx_fails_fast_without_sleep(method, code, sleeps):
    s = _Session(code=code)
    f = _fetcher(s)
    with pytest.raises(RuntimeError) as ei:
        getattr(f, method)("https://x.test/a")
    assert s.calls == 1
    assert sleeps == []
    assert isinstance(ei.value.__cause__, http.requests.exceptions.HTTPError)
    assert http.http_status(ei.value) == code
    assert "not retried" in str(ei.value)


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("code", [429, 403, 408])
def test_default_retried_4xx_uses_all_attempts(method, code, sleeps):
    s = _Session(code=code)
    f = _fetcher(s)
    with pytest.raises(RuntimeError) as ei:
        getattr(f, method)("https://x.test/a")
    assert s.calls == f.cfg.max_retries
    assert http.http_status(ei.value) == code
    assert "after retries" in str(ei.value)


@pytest.mark.parametrize("method", METHODS)
def test_5xx_still_retried(method, sleeps):
    s = _Session(code=503)
    f = _fetcher(s)
    with pytest.raises(RuntimeError) as ei:
        getattr(f, method)("https://x.test/a")
    assert s.calls == f.cfg.max_retries
    assert http.http_status(ei.value) == 503
    assert "after retries" in str(ei.value)


@pytest.mark.parametrize("method", METHODS)
def test_connection_error_still_retried(method, sleeps):
    s = _Session(exc=http.requests.exceptions.ConnectionError("down"))
    f = _fetcher(s)
    with pytest.raises(RuntimeError) as ei:
        getattr(f, method)("https://x.test/a")
    assert s.calls == f.cfg.max_retries
    assert http.http_status(ei.value) is None
    assert "after retries" in str(ei.value)


@pytest.mark.parametrize("method", METHODS)
def test_empty_retry_set_fails_fast_on_429(method, sleeps):
    s = _Session(code=429)
    f = _fetcher(s, Config(min_delay_seconds=0.0, retry_http_4xx=()))
    with pytest.raises(RuntimeError, match="not retried"):
        getattr(f, method)("https://x.test/a")
    assert s.calls == 1
    assert sleeps == []


@pytest.mark.parametrize("method", METHODS)
def test_custom_retry_set_retries_404_not_429(method, sleeps):
    cfg = Config(min_delay_seconds=0.0, retry_http_4xx=(404,))
    s404 = _Session(code=404)
    with pytest.raises(RuntimeError):
        getattr(_fetcher(s404, cfg), method)("https://x.test/a")
    assert s404.calls == cfg.max_retries

    sleeps.clear()
    s429 = _Session(code=429)
    with pytest.raises(RuntimeError):
        getattr(_fetcher(s429, cfg), method)("https://x.test/a")
    assert s429.calls == 1
    assert sleeps == []


def test_fail_fast_403_still_prints_ban_signal(sleeps, capsys):
    s = _Session(code=403)
    with pytest.raises(RuntimeError):
        _fetcher(s, Config(min_delay_seconds=0.0, retry_http_4xx=())).get("https://x.test/a")
    assert "!! HTTP 403" in capsys.readouterr().err


def test_default_config_retry_set():
    assert Config().retry_http_4xx == (403, 408, 429)


@pytest.mark.parametrize("method", METHODS)
def test_fail_fast_closes_error_response(method, sleeps):
    s = _Session(code=404)
    with pytest.raises(RuntimeError):
        getattr(_fetcher(s), method)("https://x.test/a")
    assert [r.closed for r in s.responses] == [True]


@pytest.mark.parametrize("method", METHODS)
def test_retried_error_responses_are_closed(method, sleeps):
    s = _Session(code=503)
    with pytest.raises(RuntimeError):
        getattr(_fetcher(s), method)("https://x.test/a")
    assert len(s.responses) == 3 and all(r.closed for r in s.responses)


@pytest.mark.parametrize("method", METHODS)
def test_boundary_499_fails_fast_500_retried(method, sleeps):
    s499 = _Session(code=499)
    with pytest.raises(RuntimeError, match="not retried"):
        getattr(_fetcher(s499), method)("https://x.test/a")
    assert s499.calls == 1
    s500 = _Session(code=500)
    f = _fetcher(s500)
    with pytest.raises(RuntimeError, match="after retries"):
        getattr(f, method)("https://x.test/a")
    assert s500.calls == f.cfg.max_retries


@pytest.mark.parametrize("method", METHODS)
def test_http_error_without_response_still_retried(method, sleeps):
    s = _Session(exc=http.requests.exceptions.HTTPError("no response"))
    f = _fetcher(s)
    with pytest.raises(RuntimeError, match="after retries") as ei:
        getattr(f, method)("https://x.test/a")
    assert s.calls == f.cfg.max_retries
    assert http.http_status(ei.value) is None


@pytest.mark.parametrize("method", METHODS)
def test_503_then_404_fails_fast_on_second_call(method, sleeps):
    s = _Session(codes=[503, 404])
    with pytest.raises(RuntimeError, match="not retried") as ei:
        getattr(_fetcher(s), method)("https://x.test/a")
    assert s.calls == 2
    assert len(sleeps) == 1
    assert http.http_status(ei.value) == 404


def test_custom_set_404_then_200_succeeds_get(sleeps):
    s = _Session(codes=[404, 200])
    r = _fetcher(s, Config(min_delay_seconds=0.0, retry_http_4xx=(404,))).get("https://x.test/a")
    assert r.status_code == 200 and s.calls == 2


def test_custom_set_404_then_200_succeeds_get_bytes(sleeps):
    s = _Session(codes=[404, 200])
    body, mime = _fetcher(s, Config(min_delay_seconds=0.0, retry_http_4xx=(404,))).get_bytes(
        "https://x.test/a")
    assert body == b"ok" and mime == "application/pdf" and s.calls == 2


def test_get_text_fails_fast_on_404(sleeps):
    s = _Session(code=404)
    with pytest.raises(RuntimeError, match="not retried") as ei:
        _fetcher(s).get_text("https://x.test/a")
    assert s.calls == 1 and sleeps == []
    assert http.http_status(ei.value) == 404


def test_close_failure_does_not_mask_fail_fast(sleeps):
    class BadClose(_Resp):
        def close(self):
            raise OSError("boom")
    sess = type("S", (), {"get": lambda self, *a, **k: BadClose(404)})()
    with pytest.raises(RuntimeError, match="not retried"):
        _fetcher(sess).get("https://x.test/a")


# --- Config.retry_http_4xx validation ----------------------------------------

def test_config_retry_set_normalized_to_tuple():
    assert Config(retry_http_4xx=[404, 429]).retry_http_4xx == (404, 429)


@pytest.mark.parametrize("bad", [None, 404, "404", (404, "429"), (True,), (404.0,)])
def test_config_retry_set_rejects_non_int_iterables(bad):
    with pytest.raises(ValueError, match="retry_http_4xx"):
        Config(retry_http_4xx=bad)


# --- http_status --------------------------------------------------------------

def _http_error(code):
    return http.requests.exceptions.HTTPError("x", response=_Resp(code))


def test_http_status_none_cases():
    assert http.http_status(None) is None
    assert http.http_status(RuntimeError("bare")) is None
    assert http.http_status(http.requests.exceptions.ConnectionError("down")) is None


def test_http_status_on_exception_itself():
    assert http.http_status(_http_error(404)) == 404


def test_http_status_via_cause():
    outer = RuntimeError("wrap")
    outer.__cause__ = _http_error(410)
    assert http.http_status(outer) == 410


def test_http_status_via_context_two_levels_deep():
    inner = _http_error(429)
    mid = ValueError("mid")
    mid.__context__ = inner
    outer = RuntimeError("outer")
    outer.__context__ = mid
    assert http.http_status(outer) == 429


def test_http_status_raise_from_inside_except_http_error():
    """`raise RuntimeError() from ValueError()` inside `except HTTPError(404)`:
    __cause__ is the ValueError, the 404 sits on __context__."""
    try:
        try:
            raise _http_error(404)
        except http.requests.exceptions.HTTPError:
            raise RuntimeError("wrap") from ValueError("unrelated")
    except RuntimeError as exc:
        caught = exc
    assert isinstance(caught.__cause__, ValueError)
    assert http.http_status(caught) == 404


def test_http_status_ignores_non_int_status_code():
    class R:
        status_code = "?"
    e = http.requests.exceptions.HTTPError("x", response=R())
    assert http.http_status(e) is None


def test_http_status_survives_cycle():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert http.http_status(a) is None
