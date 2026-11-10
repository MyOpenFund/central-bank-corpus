"""Runtime configuration."""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

# Crawler contact advertised in the User-Agent of every request. The default
# points to the public repo (standard bot practice) so that a stranger running
# this code never crawls the banks under the maintainer's personal identity —
# operators identify THEMSELVES via the CRAWLER_CONTACT env var (an email or
# URL webmasters can actually reach them at).
_DEFAULT_CONTACT = "https://github.com/MyOpenFund/central-bank-corpus"

# Default of Config.repec_max_pages -- the single source of truth, also used by
# RePEc walkers handed a fetcher without a Config.
REPEC_MAX_PAGES_DEFAULT = 200


def check_max_pages(value: object, name: str = "repec_max_pages") -> int:
    """Validate a RePEc listing-page cap: an int (not a bool) >= 1.

    Raises ValueError naming `name` otherwise (0 or a negative cap would
    silently walk nothing; a float/str/None is a configuration mistake)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an int >= 1, got {value!r}")
    return value


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
    # Networks headless Chrome (run with --no-sandbox) must never be pointed
    # at: htmlpdf.render_allowed refuses a render when the URL's host is, or
    # resolves to, an address in one of these (the HTML then stays the
    # canonical artifact). Default: loopback, RFC 1918 private, link-local
    # (incl. the 169.254.169.254 cloud-metadata endpoint), CGNAT 100.64/10
    # (the tailnet), "this network", multicast, reserved, and their IPv6
    # counterparts (loopback, ULA fc00::/7, link-local, unspecified,
    # multicast). Blocks the private network rather than allowlisting
    # domains, which would not scale to crawlers of many sites. CIDR strings;
    # an invalid entry is rejected with a ValueError.
    render_blocked_networks: tuple[str, ...] = (
        "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "169.254.0.0/16", "100.64.0.0/10", "0.0.0.0/8", "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128", "fc00::/7", "fe80::/10", "::/128", "ff00::/8",
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

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

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
