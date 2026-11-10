"""Load declarative adapter configs from `banks_sources.toml`.

Each bank entry in the TOML produces a factory that builds either a
`GenericSitemapAdapter` or a `ListingCrawlerAdapter` on demand. Factories are
registered in `base.INSTANCE_FACTORIES`, behind hand-written ADAPTERS.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..banks import Bank
from ..http import Fetcher
from ..taxonomy import DocType, by_code
from .base import BankAdapter, INSTANCE_FACTORIES
from .generic_sitemap import GenericSitemapAdapter
from .listing_crawler import ListingCrawlerAdapter

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

DEFAULT_TOML = Path(__file__).resolve().parents[1] / "banks_sources.toml"


def _resolve_types(d: dict) -> dict[DocType, object]:
    return {by_code(k): v for k, v in d.items()}


def _make_sitemap_factory(cfg: dict) -> Callable[[Bank, Optional[Fetcher]], BankAdapter]:
    sitemap_url = cfg["sitemap_url"]
    patterns = _resolve_types(cfg.get("patterns", {}))
    expected = _resolve_types(cfg.get("expected_per_year", {}))

    def factory(bank: Bank, fetcher: Optional[Fetcher] = None) -> BankAdapter:
        return GenericSitemapAdapter(
            bank, fetcher,
            sitemap_url=sitemap_url,
            patterns=patterns,
            expected_per_year=expected,
        )
    return factory


def _make_listing_factory(cfg: dict) -> Callable[[Bank, Optional[Fetcher]], BankAdapter]:
    raw_entries = cfg.get("entries", {})
    entries: dict[DocType, list[tuple[str, str]]] = {}
    for k, lst in raw_entries.items():
        dt = by_code(k)
        # lst is a list of [url, regex] pairs
        entries[dt] = [(item[0], item[1]) for item in lst]
    expected = _resolve_types(cfg.get("expected_per_year", {}))
    year_range = tuple(cfg["year_range"]) if "year_range" in cfg else None

    def factory(bank: Bank, fetcher: Optional[Fetcher] = None) -> BankAdapter:
        return ListingCrawlerAdapter(
            bank, fetcher,
            entries=entries,
            expected_per_year=expected,
            year_range=year_range,
        )
    return factory


_SECTIONS = ("sitemap", "listing")


@dataclass
class TomlLoadReport:
    """What `load_toml` did, so problems are reported instead of discarded.

    `loaded`: bank codes registered, in file order. `missing`: the file does
    not exist. `error`: the file is malformed (TOML syntax, or an entry that
    cannot be built, e.g. an unknown doc-type code) -- then NOTHING from the
    file is registered. `unknown_codes`: top-level keys that are not one of
    the 63 BIS bank codes (skipped). `unknown_sections`: `<code>.<section>`
    tables other than `sitemap` / `listing` (ignored)."""
    path: Path
    loaded: list[str] = field(default_factory=list)
    missing: bool = False
    error: Optional[str] = None
    unknown_codes: list[str] = field(default_factory=list)
    unknown_sections: list[str] = field(default_factory=list)

    def warnings(self) -> list[str]:
        """One human-readable line per problem; [] when the load was clean."""
        name = self.path.name
        if self.missing:
            return [f"{name} not found at {self.path}: declarative adapters skipped"]
        if self.error:
            return [f"{name} is malformed ({self.error}): declarative adapters skipped"]
        out: list[str] = []
        if self.unknown_codes:
            out.append(f"{name}: unknown bank code(s) skipped: "
                       f"{', '.join(self.unknown_codes)}")
        if self.unknown_sections:
            out.append(f"{name}: unknown section(s) ignored (expected "
                       f"{' / '.join(_SECTIONS)}): {', '.join(self.unknown_sections)}")
        if not self.loaded:
            out.append(f"{name}: 0 bank(s) loaded from {self.path}")
        return out


def load_toml(path: Optional[Path] = None) -> TomlLoadReport:
    """Register factories from a TOML config (default: the shipped
    `banks_sources.toml`) and return a TomlLoadReport. Never raises for a
    missing or malformed file: the report says what went wrong (see
    `TomlLoadReport.warnings`). All factories are built before any is
    registered, so a bad entry never leaves the registry half-filled."""
    from ..banks import BIS_63
    path = DEFAULT_TOML if path is None else path
    report = TomlLoadReport(path=path)
    if not path.exists():
        report.missing = True
        return report
    known = {b.code for b in BIS_63}
    staged: dict[str, Callable[[Bank, Optional[Fetcher]], BankAdapter]] = {}
    try:
        data = tomllib.loads(path.read_text())
        for bank_code, sections in data.items():
            if bank_code not in known:
                report.unknown_codes.append(bank_code)
                continue
            if not isinstance(sections, dict):
                report.unknown_sections.append(bank_code)
                continue
            report.unknown_sections.extend(
                f"{bank_code}.{k}" for k in sections if k not in _SECTIONS)
            if "sitemap" in sections:
                staged[bank_code] = _make_sitemap_factory(sections["sitemap"])
            elif "listing" in sections:
                staged[bank_code] = _make_listing_factory(sections["listing"])
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError,
            KeyError, TypeError, ValueError, IndexError) as exc:
        report.error = f"{type(exc).__name__}: {exc}"
        report.unknown_codes.clear()
        report.unknown_sections.clear()
        return report
    for code, factory in staged.items():
        INSTANCE_FACTORIES[code] = factory
        report.loaded.append(code)
    return report
