"""Runtime configuration."""
from __future__ import annotations

import ipaddress
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Crawler contact advertised in the User-Agent of every request. The default
# points to the public repo (standard bot practice) so that a stranger running
# this code never crawls the banks under the maintainer's personal identity —
# operators identify THEMSELVES via the CRAWLER_CONTACT env var (an email or
# URL webmasters can actually reach them at).
_DEFAULT_CONTACT = "https://github.com/MyOpenFund/central-bank-corpus"

# Default of Config.repec_max_pages -- the single source of truth, also used by
# RePEc walkers handed a fetcher without a Config.
REPEC_MAX_PAGES_DEFAULT = 200

# Allowed values of Config.pdf_url_served_html (see there).
PDF_URL_SERVED_HTML_MODES = ("reject", "accept")


def check_max_pages(value: object, name: str = "repec_max_pages") -> int:
    """Validate a RePEc listing-page cap: an int (not a bool) >= 1.

    Raises ValueError naming `name` otherwise (0 or a negative cap would
    silently walk nothing; a float/str/None is a configuration mistake)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an int >= 1, got {value!r}")
    return value


# -- content-check knobs from the environment (issue #21) ------------------
# The CLI builds Config() with its defaults, so an operator tunes these three
# fields through the environment (Dockge compose `environment:`), read when a
# Config is built without an explicit value. An invalid value must never
# crash a night: it prints one loud WARNING on stderr (once per process and
# variable value, not once per Config) and the default applies. An empty or
# unset variable means the default, silently.
_DEFAULT_MIN_PDF_BYTES = 1024
_DEFAULT_PDF_REQUIRE_EOF = True
_DEFAULT_PDF_URL_SERVED_HTML = "reject"
_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")
_ENV_WARNED: set[tuple[str, str]] = set()


def _env_warn(name: str, raw: str, expected: str, default: object) -> None:
    if (name, raw) in _ENV_WARNED:
        return
    _ENV_WARNED.add((name, raw))
    print(f"[config] WARNING: invalid {name}={raw!r} (expected {expected}); "
          f"using the default {default!r}", file=sys.stderr, flush=True)


def _env_min_pdf_bytes() -> int:
    raw = os.environ.get("CB_MIN_PDF_BYTES", "")
    if not raw.strip():
        return _DEFAULT_MIN_PDF_BYTES
    try:
        n = int(raw.strip())
    except ValueError:
        n = -1
    if n < 0:
        _env_warn("CB_MIN_PDF_BYTES", raw, "an int >= 0", _DEFAULT_MIN_PDF_BYTES)
        return _DEFAULT_MIN_PDF_BYTES
    return n


def _env_pdf_require_eof() -> bool:
    raw = os.environ.get("CB_PDF_REQUIRE_EOF", "")
    word = raw.strip().lower()
    if not word:
        return _DEFAULT_PDF_REQUIRE_EOF
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    _env_warn("CB_PDF_REQUIRE_EOF", raw, "1/0, true/false, yes/no or on/off",
              _DEFAULT_PDF_REQUIRE_EOF)
    return _DEFAULT_PDF_REQUIRE_EOF


def _env_pdf_url_served_html() -> str:
    raw = os.environ.get("CB_PDF_URL_SERVED_HTML", "")
    word = raw.strip().lower()
    if not word:
        return _DEFAULT_PDF_URL_SERVED_HTML
    if word in PDF_URL_SERVED_HTML_MODES:
        return word
    _env_warn("CB_PDF_URL_SERVED_HTML", raw,
              " or ".join(PDF_URL_SERVED_HTML_MODES), _DEFAULT_PDF_URL_SERVED_HTML)
    return _DEFAULT_PDF_URL_SERVED_HTML


def _user_agent() -> str:
    contact = os.environ.get("CRAWLER_CONTACT", "").strip() or _DEFAULT_CONTACT
    return f"central-bank-corpus/0.2 (+{contact})"


@dataclass
class Config:
    data_dir: Path = Path("./data")
    user_agent: str = field(default_factory=_user_agent)
    # Minimal anti-ban throttle per host (NOT politeness — kept low on purpose).
    min_delay_seconds: float = 0.5
    timeout: float = 30.0          # per-request connect/read (inactivity) timeout
    download_timeout: float = 90.0  # TOTAL deadline for a body download (defeats
                                    # slow-trickle hosts that never trip `timeout`)
    max_retries: int = 3
    # HTTP 4xx statuses Fetcher.get/get_bytes still RETRY (with backoff). Any other
    # 4xx fails at once: a 404/410 will not heal in 7 s, and retrying it only burns
    # backoff (issue #30). 403 stays retried (anti-bot blocks can be transient),
    # 408/429 are transient by definition. () = never retry a 4xx. Entries must
    # be 4xx codes (400-499): 5xx are always retried, so listing one would
    # silently do nothing -> rejected with a ValueError.
    retry_http_4xx: tuple[int, ...] = (403, 408, 429)
    # No robots.txt enforcement. No domain guard. We scrape whatever the
    # discovery layer hands us.
    prefer_pdf: bool = True
    accept_html_when_no_pdf: bool = True
    parallel_hosts: int = 10
    # When True, text/html responses are rendered to PDF via headless Chrome
    # so the on-disk corpus stays uniformly PDF for downstream ingestion.
    html_to_pdf: bool = True
    # Max IDEAS listing pages walked per RePEc series (discovery and repec-check).
    # A loop guard, NOT a data limit: the largest IDEAS series had 17 pages on
    # 2026-11-10. Reaching it while the last page still links to the next one
    # is recorded as a truncation (run-report / WARNING), never a clean end.
    repec_max_pages: int = REPEC_MAX_PAGES_DEFAULT
    # Duplicate-content stamping (storage.flush_duplicate_stamps): a record
    # whose bytes another row already owns has its URL (and source page)
    # remembered on that owner row so it is never re-downloaded. By default
    # only when the owner is a real PDF (mime_type application/pdf and no
    # html_path -- storage.is_stampable_owner; a page rendered from HTML is
    # not one): an HTML page can change while serving identical bytes today,
    # so an HTML owner's duplicates stay in-memory for the run and are
    # retried next night. recover-downloads' self-healing follows the same
    # rule (and keeps the dead URL quarantined when it may not stamp).
    # True also stamps HTML (and rendered-HTML) owners.
    stamp_duplicate_html: bool = False
    # Content checks on stored bytes (issue #21, integrity wave 2026-11):
    # an HTTP 200 is not a document. Bytes that claim to be a PDF (server
    # mime application/pdf, or a producer that expects a PDF and a server
    # that does not say HTML) must carry `%PDF` within their first 1,024
    # bytes, be at least `min_pdf_bytes` long and, when `pdf_require_eof`,
    # hold `%%EOF` in their last 2,048 bytes (a truncated download fails).
    # Applied by Storage.save() to every candidate URL before the sha256
    # dedup (an error page must never become a dedup owner), by
    # Storage.reindex() to an on-disk .pdf (`skip:bad-content`) and to the
    # headless-Chrome render output. 0 / False relax the respective rule.
    # Defaults from the environment when not passed: CB_MIN_PDF_BYTES (an
    # int >= 0) and CB_PDF_REQUIRE_EOF (1/0, true/false, yes/no, on/off);
    # an invalid value warns on stderr and keeps the default (see
    # _env_min_pdf_bytes). An explicit argument is validated strictly.
    min_pdf_bytes: int = field(default_factory=_env_min_pdf_bytes)
    pdf_require_eof: bool = field(default_factory=_env_pdf_require_eof)
    # A producer that EXPECTS a PDF (DocRecord.mime_type application/pdf)
    # but gets an HTML response (consent wall, error or "moved" page served
    # with 200): "reject" (default) skips that candidate URL and tries the
    # next fallback; when none passes, save() raises
    # storage.ContentCheckFailed (download error + quarantine like any
    # failed download). "accept" restores the old behaviour: the HTML is
    # stored, and rendered to PDF when html_to_pdf is on.
    # Not the same knob as `accept_html_when_no_pdf` above: that field is a
    # discovery-side preference (may a producer hand over an HTML page when
    # a document has no PDF at all?) and no code path reads it today; such
    # a producer sets mime_type text/html (or leaves it empty), and an HTML
    # producer's response is never judged by this setting. This one only
    # governs the case where the producer said "PDF" and the server
    # answered HTML. Default from CB_PDF_URL_SERVED_HTML (reject / accept)
    # when not passed; an invalid value warns and keeps "reject".
    pdf_url_served_html: str = field(default_factory=_env_pdf_url_served_html)
    # Networks headless Chrome (run with --no-sandbox) must never be pointed
    # at: htmlpdf.render_allowed refuses a render when the URL's host is, or
    # resolves to, an address in one of these (the HTML then stays the
    # canonical artifact). Default: loopback, RFC 1918 private, link-local
    # (incl. the 169.254.169.254 cloud-metadata endpoint), CGNAT 100.64/10
    # (the tailnet), "this network", multicast, reserved, and their IPv6
    # counterparts (loopback, ULA fc00::/7, link-local, unspecified,
    # multicast, deprecated site-local fec0::/10) plus the IPv6 forms that
    # embed an IPv4 address (NAT64 64:ff9b::/96, 6to4 2002::/16,
    # IPv4-compatible ::/96) -- these could smuggle a private IPv4 target.
    # Blocks the private network rather than allowlisting domains, which
    # would not scale to crawlers of many sites. CIDR strings; an invalid
    # entry is rejected with a ValueError.
    render_blocked_networks: tuple[str, ...] = (
        "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "169.254.0.0/16", "100.64.0.0/10", "0.0.0.0/8", "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128", "fc00::/7", "fe80::/10", "::/128", "ff00::/8",
        "fec0::/10", "64:ff9b::/96", "2002::/16", "::/96",
    )

    def __post_init__(self) -> None:
        # Normalize retry_http_4xx (a list from a caller is fine) and reject
        # anything that is not an iterable of ints, with a clear message.
        raw = self.retry_http_4xx
        try:
            codes = tuple(raw)
        except TypeError:
            raise ValueError(
                f"Config.retry_http_4xx must be an iterable of int HTTP status "
                f"codes, got {raw!r}") from None
        if any(isinstance(c, bool) or not isinstance(c, int) for c in codes):
            raise ValueError(
                f"Config.retry_http_4xx must contain only int HTTP status codes, "
                f"got {raw!r}")
        outside = [c for c in codes if not 400 <= c <= 499]
        if outside:
            raise ValueError(
                f"Config.retry_http_4xx must contain only 4xx codes (400-499), "
                f"got {outside!r} in {raw!r}")
        self.retry_http_4xx = codes
        check_max_pages(self.repec_max_pages, "Config.repec_max_pages")

        nets = self.render_blocked_networks
        if isinstance(nets, str):
            raise ValueError(
                f"Config.render_blocked_networks must be an iterable of CIDR "
                f"strings, not a single string: {nets!r}")
        try:
            nets = tuple(nets)
            for n in nets:
                ipaddress.ip_network(n, strict=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Config.render_blocked_networks: invalid network in "
                f"{self.render_blocked_networks!r}: {exc}") from None
        self.render_blocked_networks = nets

        m = self.min_pdf_bytes
        if isinstance(m, bool) or not isinstance(m, int) or m < 0:
            raise ValueError(f"Config.min_pdf_bytes must be an int >= 0, got {m!r}")
        if not isinstance(self.pdf_require_eof, bool):
            raise ValueError(
                f"Config.pdf_require_eof must be a bool, got {self.pdf_require_eof!r}")
        if self.pdf_url_served_html not in PDF_URL_SERVED_HTML_MODES:
            raise ValueError(
                f"Config.pdf_url_served_html must be one of "
                f"{PDF_URL_SERVED_HTML_MODES!r}, got {self.pdf_url_served_html!r}")

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    def stored_path(self, path: Path) -> str:
        """The manifest form of a file path (`local_path`, `html_path`): POSIX,
        RELATIVE TO THE DATA ROOT (`data_dir`, the directory holding
        `manifest/`), e.g. `raw/ecb/C1/2023/<doc_id>.pdf` -- the `documents`
        contract's rule, valid wherever the root is mounted (NAS `/app/data`
        or `/cbdata`, a Mac, a VPS); read back with `resolve_stored_path`.
        Raises ValueError for a path outside the data root (never stored)."""
        p = Path(path)
        try:
            rel = p.relative_to(self.data_dir)
        except ValueError:
            try:
                rel = p.resolve().relative_to(self.data_dir.resolve())
            except ValueError:
                raise ValueError(f"{path} is not under the data root "
                                 f"{self.data_dir}") from None
        return rel.as_posix()

    def resolve_stored_path(self, stored: Optional[str]) -> Optional[Path]:
        """On-disk path of a manifest `local_path` / `html_path` (the one
        resolver of the package): the contract form, relative to `data_dir`
        (`raw/...`). None for an empty value. The pre-contract forms -- the
        `data/` prefix (the data root seen from the app directory, stored
        until 2026-11-20) and an absolute path -- are a ValueError naming
        `migrate-contract`, never guessed at (manifest readers already
        refuse such a row: models.check_contract_row)."""
        if not stored:
            return None
        if stored.startswith("/") or stored.startswith("data/"):
            raise ValueError(f"stored path {stored!r} is not relative to the data root "
                             f"(documents contract): run `python -m cb_corpus "
                             f"migrate-contract --write` once on this data root")
        return self.data_dir / stored

    @property
    def manifest_path(self) -> Path:
        """Legacy single-file manifest. Kept as the source for the one-time split
        into per-bank files (see Storage); current storage is per-bank."""
        return self.data_dir / "manifest.jsonl"

    @property
    def manifest_dir(self) -> Path:
        """Directory of per-bank manifests: data/manifest/<bank_code>.jsonl."""
        return self.data_dir / "manifest"

    def manifest_file(self, bank_code: str) -> Path:
        return self.manifest_dir / f"{bank_code}.jsonl"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def orphans_dir(self) -> Path:
        """Quarantine for on-disk files that duplicate an indexed document
        (`sweep-orphans`): mirror tree of raw/, sibling of it, walked by nothing."""
        return self.data_dir / "raw_orphans"
