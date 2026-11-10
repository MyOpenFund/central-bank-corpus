"""Per-bank adapter framework.

A `BankAdapter` knows how to (1) discover official documents for its bank across
doc types A-F and (2) state the *expected* number of a given type in a year
(from the bank's published meeting calendar / archive) for the completeness
matrix.

`GenericAdapter` is the zero-config fallback registered for every BIS-63 bank:
it already yields speeches (C1, via the BIS index) and working papers (D, via
RePEc discovery). Concrete adapters subclass it and add the bank-specific
listings (A1/A2/A3, B, E, F) by implementing `_discover_native`.
"""
from __future__ import annotations

import sys
from abc import ABC
from datetime import date
from typing import Callable, Iterator, Optional

from ..banks import Bank, BIS_63, get_bank
from ..http import Fetcher
from ..models import DocRecord
from ..sources.bis_speeches import BISSpeechIndex
from ..sources.repec import RePEcDiscovery
from ..taxonomy import DocType, FULL_SCOPE

# Class-based registry: bespoke hand-written adapters (Fed, ECB, ...).
ADAPTERS: dict[str, type["BankAdapter"]] = {}

# Instance-factory registry: declarative TOML-configured adapters.
# Hand-written ADAPTERS take precedence over factories.
INSTANCE_FACTORIES: dict[str, Callable[[Bank, Optional[Fetcher]], "BankAdapter"]] = {}


def register(*bank_codes: str) -> Callable[[type["BankAdapter"]], type["BankAdapter"]]:
    def deco(cls: type["BankAdapter"]) -> type["BankAdapter"]:
        for code in bank_codes:
            ADAPTERS[code] = cls
        return cls
    return deco


class BankAdapter(ABC):
    #: doc types this adapter can pull directly from the bank's own site
    native_types: tuple[DocType, ...] = ()
    #: expected items per year per native type (from meeting calendar/archive);
    #: None => "count the listing at runtime"
    expected_per_year: dict[DocType, Optional[int]] = {}

    def __init__(self, bank: Bank, fetcher: Optional[Fetcher] = None):
        self.bank = bank
        self.fetcher = fetcher or Fetcher()
        self._bis = BISSpeechIndex(self.fetcher)
        self._repec = RePEcDiscovery(self.fetcher)
        #: discovery-time fetch failures recorded during this adapter's life.
        #: A non-empty list means discovery was PARTIAL — the caller should
        #: re-run (discovery is idempotent) rather than trust the result as
        #: complete. This is what stops a transient blip from silently
        #: dropping a whole year/listing.
        self.errors: list[dict] = []

    # ---- discovery -----------------------------------------------------
    def _fetch_text(self, url: str, *, context: str = "") -> Optional[str]:
        """Fetch `url`, or record the failure and return None.

        Use this instead of a bare ``try/except: continue`` so a failed listing
        fetch is visible (logged + appended to ``self.errors``) instead of being
        swallowed silently. Returning None lets the caller skip *this* item while
        the recorded error signals that a re-run is needed for completeness.
        """
        try:
            return self.fetcher.get_text(url)
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            self.errors.append({
                "bank": self.bank.code,
                "context": context,
                "url": url,
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"!! discovery fetch FAILED [{self.bank.code} {context}]: "
                  f"{url} -> {type(exc).__name__}", file=sys.stderr, flush=True)
            return None
    def supported_types(self) -> tuple[DocType, ...]:
        extra = (DocType.C1, DocType.D1, DocType.D2)
        return tuple(dict.fromkeys(self.native_types + extra))

    def discover(self, doc_type: DocType,
                 since: Optional[date] = None,
                 native_only: bool = False) -> Iterator[DocRecord]:
        if doc_type == DocType.C1:
            # C1 comes from the shared BIS index (no adapter has a native C1
            # route today) — skipped entirely under native_only: the sync
            # job's catalog phase owns that source.
            if native_only:
                return
            yield from self._bis.discover(since=since, only_banks={self.bank.code})
        elif doc_type in (DocType.D1, DocType.D2):
            # WP v3: a bank that declares D1/D2 in `native_types` discovers them
            # from its own site (precise dates, no RePEc lag); every other bank
            # keeps the RePEc path. A bank must not opt in until its v2 rows have
            # been date-migrated (see docs/IMPLEMENTATION_PLAN.md phase 3) — flipping
            # before migration would re-download papers found under new URLs.
            # fr and se opted in before their migration was applied (see that doc).
            if doc_type in self.native_types:
                recs = self._discover_native(doc_type, since)
                # Zero re-download after migration: a native scraper often finds a
                # paper under a different URL than its v2 RePEc-era row, so its
                # doc_id differs and save() (which dedups by doc_id/sha256, not URL)
                # would re-fetch it. The migration registered the native URL in the
                # row's alt_urls, so is_known_url() recognises it — skip those here,
                # before download. Set by the pipeline (`_skip_known_url`); absent in
                # reindex/test paths, which want every record.
                skip = getattr(self, "_skip_known_url", None)
                if skip is not None:
                    recs = (r for r in recs if not skip(r.pdf_url))
                yield from recs
            else:
                if native_only:
                    return
                yield from (r for r in self._repec.discover_bank(self.bank.code)
                            if r.doc_type == doc_type)
        else:
            # Every other native type (A/B/D3/E/F) is subject to the SAME
            # already-known-URL skip as D1/D2 above, for the same reason: a
            # native scraper's URL for a given post/release can differ from
            # the one under which it was first indexed (a migration, a site
            # reformat, an old one-off scraper's URL quirk — e.g. the ECB D3
            # double-slash legacy rows, see `data: index normalized URL forms
            # for 15 legacy D3 rows`). doc_id-based dedup in Storage.save()
            # only catches that case if doc_id happens to match; is_known_url()
            # (primary pdf_url + alt_urls) catches it unconditionally, and
            # doing it here — before download — is what actually stops the
            # nightly re-fetch, since save() only skips by doc_id BEFORE
            # fetching too, not by URL. For every native type whose URL *is*
            # stable across runs this is a no-op optimisation (save()'s
            # doc_id check would have skipped it anyway) — there is no native
            # type/adapter that relies on re-yielding an already-known URL to
            # pick up changed content (audited: no adapter re-fetches a stable
            # URL for that purpose; see fix/silent-series task-1 report).
            recs = self._discover_native(doc_type, since)
            skip = getattr(self, "_skip_known_url", None)
            if skip is not None:
                recs = (r for r in recs if not skip(r.pdf_url))
            yield from recs

    def discover_all(self, scope: tuple[DocType, ...] = FULL_SCOPE,
                     since: Optional[date] = None,
                     native_only: bool = False) -> Iterator[DocRecord]:
        for dt in scope:
            if dt in self.supported_types():
                yield from self.discover(dt, since=since, native_only=native_only)

    def _discover_native(self, doc_type: DocType,
                         since: Optional[date]) -> Iterator[DocRecord]:
        """Override in concrete adapters for A/B/E/F. Default: nothing."""
        return iter(())

    # ---- completeness --------------------------------------------------
    def expected_count(self, doc_type: DocType, year: int) -> Optional[int]:
        """Expected number of `doc_type` documents in `year`.

        Returns None when unknown (the matrix then falls back to the listing
        count crawled at runtime).
        """
        return self.expected_per_year.get(doc_type)


@register(*[b.code for b in BIS_63])
class GenericAdapter(BankAdapter):
    """Fallback: speeches + working papers only, for every bank."""
    native_types = ()


def get_adapter(bank_code: str, fetcher: Optional[Fetcher] = None) -> BankAdapter:
    bank = get_bank(bank_code)
    # 1. Bespoke hand-written class wins.
    cls = ADAPTERS.get(bank_code)
    if cls is not None and cls is not GenericAdapter:
        return cls(bank, fetcher)
    # 2. Declarative TOML factory.
    factory = INSTANCE_FACTORIES.get(bank_code)
    if factory is not None:
        return factory(bank, fetcher)
    # 3. Generic fallback.
    return GenericAdapter(bank, fetcher)
