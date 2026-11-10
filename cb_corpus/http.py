"""HTTP fetcher: retries, identifying UA, minimal per-host throttle.

This layer is intentionally NOT polite: robots.txt is ignored, the per-host
delay is set low (0.5s default) just to avoid getting hard-banned. The only
purpose is to scrape reliably.
"""
from __future__ import annotations

import sys
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from .config import Config


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def host_matches(url: str, domain: str) -> bool:
    """True if url's host is `domain` or a subdomain of it."""
    h = host_of(url)
    d = domain.lower().lstrip(".")
    return h == d or h.endswith("." + d)


def http_status(exc: BaseException | None) -> int | None:
    """HTTP status carried by `exc` or its __cause__/__context__ chain, else None."""
    # Walk BOTH links: `raise X from Y` inside `except HTTPError` puts Y on
    # __cause__ and the HTTPError on __context__. Breadth-first, cycle-safe.
    seen: set[int] = set()
    queue: list[BaseException] = [exc] if exc is not None else []
    while queue:
        cur = queue.pop(0)
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        code = getattr(getattr(cur, "response", None), "status_code", None)
        if isinstance(code, int):
            return code
        queue.extend(e for e in (cur.__cause__, cur.__context__) if e is not None)
    return None


def _close_response(exc: BaseException) -> None:
    """Release the connection held by an error response (best effort)."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return
    try:
        resp.close()
    except Exception:  # noqa: BLE001 - closing is best effort
        pass


class Fetcher:
    def __init__(self, config: Optional[Config] = None):
        self.cfg = config or Config()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.cfg.user_agent
        self._last_hit: dict[str, float] = {}

    def _fails_fast(self, code: object) -> bool:
        """A 4xx outside `cfg.retry_http_4xx` will not heal on retry (issue #30)."""
        return (isinstance(code, int) and 400 <= code < 500
                and code not in self.cfg.retry_http_4xx)

    def _throttle(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.cfg.min_delay_seconds - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def throttle(self, url: str) -> None:
        """Public per-host throttle for work done OUTSIDE this fetcher.

        Headless-Chrome PDF rendering fetches the live URL itself, bypassing
        the throttle on `get()`. Calling this before a render keeps the host's
        rate budget shared between our requests and Chrome's, so a single
        HTML doc (raw fetch + render) doesn't double-hammer the server.
        """
        self._throttle(host_of(url))

    def get(self, url: str, *, allow_redirects: bool = True) -> requests.Response:
        host = host_of(url)
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            self._throttle(host)
            try:
                r = self.session.get(url, timeout=self.cfg.timeout,
                                     allow_redirects=allow_redirects)
                r.raise_for_status()
                return r
            except requests.exceptions.HTTPError as exc:
                # Make rate-limits and blocks visible immediately, even on
                # the first attempt — these are the "are we banned?" signals.
                code = getattr(exc.response, "status_code", "?")
                if code in (429, 403, 503):
                    print(f"!! HTTP {code} on {host} (attempt {attempt + 1}): {url}",
                          file=sys.stderr, flush=True)
                _close_response(exc)
                if self._fails_fast(code):
                    raise RuntimeError(
                        f"GET failed (HTTP {code}, not retried): {url}") from exc
                last_exc = exc
                time.sleep(2 ** attempt)
            except Exception as exc:  # noqa: BLE001 - retried below
                last_exc = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"GET failed after retries: {url}") from last_exc

    def get_text(self, url: str) -> str:
        # Route through get_bytes so the TOTAL download deadline applies here too
        # — a slow-trickle listing/detail page must fail fast, not hang forever.
        body, _ = self.get_bytes(url)
        return body.decode("utf-8", errors="replace")

    def get_bytes(self, url: str) -> tuple[bytes, str]:
        """Return (body, mime_type), streaming with a TOTAL download deadline.

        `timeout` only bounds inactivity between bytes, so a slow-trickle host can
        stall a download for minutes/forever without ever tripping it (observed on
        some bank PDF hosts). We stream and abort once `download_timeout` total has
        elapsed, then retry/backoff like `get()`. mime is the bare content-type.
        """
        host = host_of(url)
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            self._throttle(host)
            try:
                r = self.session.get(url, timeout=self.cfg.timeout, stream=True)
                r.raise_for_status()
                deadline = time.monotonic() + self.cfg.download_timeout
                chunks: list[bytes] = []
                for chunk in r.iter_content(chunk_size=65536):
                    chunks.append(chunk)
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            f"download exceeded {self.cfg.download_timeout}s: {url}")
                mime = (r.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                return b"".join(chunks), mime
            except requests.exceptions.HTTPError as exc:
                code = getattr(exc.response, "status_code", "?")
                if code in (429, 403, 503):
                    print(f"!! HTTP {code} on {host} (attempt {attempt + 1}): {url}",
                          file=sys.stderr, flush=True)
                _close_response(exc)
                if self._fails_fast(code):
                    raise RuntimeError(
                        f"GET bytes failed (HTTP {code}, not retried): {url}") from exc
                last_exc = exc
                time.sleep(2 ** attempt)
            except Exception as exc:  # noqa: BLE001 - retried below
                last_exc = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"GET bytes failed after retries: {url}") from last_exc

    def head(self, url: str) -> requests.Response:
        host = host_of(url)
        self._throttle(host)
        return self.session.head(url, timeout=self.cfg.timeout, allow_redirects=True)

    def exists(self, url: str) -> bool:
        try:
            r = self.head(url)
            return r.status_code < 400
        except Exception:
            return False
