"""HTML -> PDF via headless Chrome.

Used by Storage to render text/html responses (e.g. ECB monetary policy
accounts) into PDFs so the corpus stays uniform for downstream ingestion
(eigenmind handles PDF, not HTML).

Why Chrome and not the cached HTML file:
  ECB pages reference CSS by relative URL; rendering the local cached .html
  with file:// loses the styling. We point Chrome at the live URL so external
  resources resolve properly.
"""
from __future__ import annotations

import ipaddress
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .config import Config


def _blocked_by(addr: str, networks) -> Optional[str]:
    """The blocked network `addr` falls in, else None. IPv4-mapped IPv6
    (::ffff:a.b.c.d) is checked as its IPv4 address too; an unparseable
    address counts as blocked (fail closed)."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])   # drop an IPv6 zone id
    except ValueError:
        return "unparseable address"
    candidates = [ip]
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        candidates.append(mapped)
    for cand in candidates:
        for net in networks:
            if cand.version == net.version and cand in net:
                return str(net)
    return None


def render_allowed(url: str, cfg: "Config",
                   resolver: Optional[Callable] = None) -> Optional[str]:
    """Whether headless Chrome may be pointed at `url`: None = allowed, else
    the reason it is refused.

    Chrome runs with --no-sandbox, so it must never be aimed at the private
    network (loopback, RFC 1918, link-local / cloud metadata, the tailnet's
    CGNAT range, ...): `cfg.render_blocked_networks`. Rules: the scheme must
    be http(s); a host must be present; an IP-literal host is checked
    directly; a name is resolved with `resolver` (default
    `socket.getaddrinfo`, looked up at call time so tests can inject a fake)
    and EVERY resolved address must lie outside the blocked networks; a
    resolution failure or an empty answer is refused (fail closed).

    Limits: only the top-level URL is inspected. Chrome's own sub-resource
    requests (scripts, frames, redirects, CSS) are NOT -- that would need an
    egress proxy. Chrome also resolves the name again itself, so a DNS answer
    that changes between this check and the render is not covered either.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return f"unparseable URL ({exc})"
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return f"scheme {scheme or '(none)'!r} not allowed (http/https only)"
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        return f"unparseable URL ({exc})"
    if not host:
        return "no host in URL"
    networks = [ipaddress.ip_network(n, strict=False) for n in cfg.render_blocked_networks]
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal:
        net = _blocked_by(host, networks)
        return f"address {host} is in blocked network {net}" if net else None
    resolve = resolver or socket.getaddrinfo
    try:
        infos = resolve(host, port or (443 if scheme == "https" else 80),
                        type=socket.SOCK_STREAM)
    except Exception as exc:  # noqa: BLE001 -- any resolver failure fails closed
        return f"could not resolve {host} ({type(exc).__name__}: {exc})"
    addrs = [info[4][0] for info in infos or [] if info and len(info) > 4 and info[4]]
    if not addrs:
        return f"could not resolve {host} (no address)"
    for addr in addrs:
        net = _blocked_by(str(addr), networks)
        if net:
            return f"{host} resolves to {addr}, in blocked network {net}"
    return None


# Try the standard macOS path first, then common Linux names.
_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
)


def find_chrome() -> Optional[str]:
    for c in _CHROME_CANDIDATES:
        if "/" in c:
            if Path(c).exists():
                return c
        else:
            path = shutil.which(c)
            if path:
                return path
    return None


def render_url_to_pdf(url: str, output: Path,
                      chrome: Optional[str] = None,
                      timeout: float = 60.0,
                      user_data_dir: Optional[str] = None) -> None:
    """Render the page at `url` to `output` via headless Chrome.

    If `user_data_dir` is given, the directory is reused across calls —
    avoids cold-start cost (per-call temp profile is ~10x slower because
    Chrome rebuilds the profile every invocation). The caller owns cleanup.
    When omitted, a fresh tempdir is created per call (safe but slow).

    Raises RuntimeError if Chrome is not found or the conversion fails.
    """
    chrome = chrome or find_chrome()
    if chrome is None:
        raise RuntimeError("no Chrome / Chromium binary found")
    output.parent.mkdir(parents=True, exist_ok=True)
    if user_data_dir is not None:
        _run_chrome(chrome, url, output, user_data_dir, timeout)
    else:
        with tempfile.TemporaryDirectory(prefix="cbc_chrome_") as tmpdir:
            _run_chrome(chrome, url, output, tmpdir, timeout)
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError(f"Chrome produced no PDF for {url}")


def _run_chrome(chrome: str, url: str, output: Path,
                user_data_dir: str, timeout: float) -> None:
    # `--headless=new` is ~25x slower than legacy `--headless` for print-to-pdf
    # (cold-startup overhead per invocation). Use legacy.
    cmd = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        f"--user-data-dir={user_data_dir}",
        f"--print-to-pdf={output}",
        url,
    ]
    # IMPORTANT: do NOT use capture_output / PIPE here.
    # Chrome streams verbose output to stderr; if we capture it via a pipe
    # without draining, the pipe buffer (~64KB) fills and Chrome blocks for
    # the entire timeout. Discarding to DEVNULL keeps it fast (~4s/render).
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Chrome failed (exit {e.returncode}) on {url}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Chrome timed out after {timeout}s on {url}") from e
