"""Banque de France Working Papers (D1) from publications.banque-france.fr /
www.banque-france.fr.

Two publication systems (fr native WP design, 2026-08-24):

- **Legacy** ``publications.banque-france.fr`` — frozen archive 1994 -> Sept
  2023 (last n°924). One page per calendar year,
  ``/liste-chronologique/documents-de-travail_year={YYYY}.html``, listing
  every paper of that year in a ``div.node-publication`` block per paper: the
  category span carries ``n°NUM``, ``div.element2`` the title, the
  ``date-display-single`` span the day (machine-readable ``content`` ISO
  datetime, human text "Publié le DD/MM/YYYY" as fallback), and a ``.pdf``
  link somewhere in the block. Filenames are wildly inconsistent across years
  — ``document-de-travail_NNN_YYYY.pdf``, ``working-paper-NNN-month-YYYY.pdf``,
  ``doc_de_travail_NNN_-_YYYYMMDD.pdf``, ``dtNNN.pdf``, ``wpNNN.pdf``,
  ``wpNNN_0.pdf`` / ``wpNNN_1.pdf`` (re-upload / revised-version suffixes) —
  so the PDF is ALWAYS the link the row itself carries, never derived from a
  pattern. 1995 is a confirmed gap (404); any other missing/absent/empty year
  page is skipped the same way (per boj_wp's pattern).

  Days are a uniform CMS placeholder of "01" for older years (no real day was
  ever recorded) but genuine for later years. Since a paper's own row gives no
  way to tell "true 1st" from "placeholder 1st" in isolation, precision is
  decided PER YEAR PAGE from the full sample: if every row on the page falls
  on day 1, the page is month precision (dates normalized to day 1); if any
  row shows a different day, that proves the page carries real days, so ALL
  its rows (including any genuine 1sts) are trusted as day precision.

- **New** ``www.banque-france.fr/en/publications-and-research/our-main-publications/working-papers``
  — the paginated (``?page=0..N``, 12/paper, newest-first) listing (2018 ->
  present). Each listing card (``a.card.card-vertical``) carries the title
  and an already **day-precision** date ("21st of August 2026" —
  ``Dth of Month YYYY``); the WP number is NOT on the listing, only on the
  paper's own detail page (linked by the card), as free text — either its
  ``og:description`` meta ("Working Paper Series no. 660." /
  "Document de travail n° 1060." for the odd FR-only page) or, when that
  meta lacks it, the visible description paragraph
  (``.field--name-field-espaces2-header-text``) — regex-extracted, with a
  skip-and-warn when neither carries it (never guessed from the URL slug or
  card position). The detail page also carries authors
  (``.author-names-wrapper .author-names`` spans) — parsed for completeness
  and possible future use, but not persisted: ``DocRecord`` has no author
  field today, so wiring that in is out of scope here. PDF: ALWAYS the
  scraped ``.pdf`` download link on the detail page, never derived (its
  ``/system/files/{YYYY-MM}/...`` path isn't a stable function of the
  number). ``source_url`` is the EN detail page (the FR page shares the same
  PDF and isn't ingested separately).

This module exposes both walkers (``_iter_legacy``, ``_iter_new``) and the
public merged entry point ``discover_fr_wp`` (interface-parity with the
other native-WP sources, e.g. ``discover_buba_wp``/``discover_boj_wp``):
bounded mode (``since`` set) walks the new system only -- the legacy archive
is frozen at 2023 and can never have anything newer, so skipping it is a
pure fetch-economy win; full mode (``since=None``) walks and merges both,
keyed on the WP number, with the new system's record winning any overlap
(2018-2023) since it carries day precision where legacy is usually month.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from typing import Iterator, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http import Fetcher
from ..models import DocRecord
from ..taxonomy import DocType

BDF_LEGACY = "https://publications.banque-france.fr"
_LEGACY_YEAR = BDF_LEGACY + "/liste-chronologique/documents-de-travail_year={year}.html"
# Frozen archive bounds: earliest year page that exists, and the last year
# before the site's own hand-off to the new system (last legacy paper n°924,
# Sept 2023). 1995 is a confirmed 404 gap; probed anyway like every other year.
_LEGACY_FIRST_YEAR = 1994
_LEGACY_LAST_YEAR = 2023

_NUM_RE = re.compile(r"n°\s*(\d+)", re.I)
_DMY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})")


def _legacy_row_date(row) -> Optional[date]:
    """Parse a legacy row's date span: prefer the machine-readable ISO
    ``content`` attribute, fall back to the "Publié le DD/MM/YYYY" text."""
    span = row.find("span", class_="date-display-single")
    if span is None:
        return None
    content = (span.get("content") or "").strip()
    if content:
        try:
            return date.fromisoformat(content[:10])
        except ValueError:
            pass
    m = _DMY_RE.search(span.get_text(" ", strip=True))
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def parse_legacy_year(html: str, base_url: str = BDF_LEGACY
                      ) -> list[tuple[Optional[date], str, int, str, str]]:
    """From a BdF legacy year-listing page, return (date, precision, number,
    title, pdf_url) per paper.

    Content-based: each ``div.node-publication`` block is one paper. A row
    missing its number, date, or PDF link is malformed and is skipped with a
    stderr warning rather than raising or silently dropping the whole page.
    See the module docstring for the per-page precision rule.
    """
    soup = BeautifulSoup(html, "lxml")
    parsed: list[tuple[date, int, str, str]] = []
    for row in soup.find_all("div", class_="node-publication"):
        cat = row.find("span", class_="category")
        m = _NUM_RE.search(cat.get_text(" ", strip=True)) if cat else None
        if not m:
            print(f"WARNING [bdf-legacy] row with no paper number, skipped: {base_url}",
                  file=sys.stderr, flush=True)
            continue
        number = int(m.group(1))

        pdf_a = next((a for a in row.find_all("a", href=True)
                      if a["href"].lower().endswith(".pdf")), None)
        if pdf_a is None:
            print(f"WARNING [bdf-legacy] n°{number} has no PDF link, skipped: {base_url}",
                  file=sys.stderr, flush=True)
            continue
        pdf_url = urljoin(base_url, pdf_a["href"])

        d = _legacy_row_date(row)
        if d is None:
            print(f"WARNING [bdf-legacy] n°{number} has no parseable date, skipped: {base_url}",
                  file=sys.stderr, flush=True)
            continue

        title_el = row.find("div", class_="element2")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            title = f"Documents de travail n°{number}"

        parsed.append((d, number, title, pdf_url))

    if not parsed:
        return []

    # Per-page precision: any non-1 day proves the page carries real days.
    page_is_day = any(d.day != 1 for d, *_ in parsed)
    prec = "day" if page_is_day else "month"
    out: list[tuple[Optional[date], str, int, str, str]] = []
    for d, number, title, pdf_url in parsed:
        dd = d if page_is_day else date(d.year, d.month, 1)
        out.append((dd, prec, number, title, pdf_url))
    return out


def _iter_legacy(fetcher: Fetcher, years: Optional[set] = None
                 ) -> Iterator[tuple[int, DocRecord]]:
    """Yield (WP number, DocRecord) for BdF legacy Working Papers (D1),
    1994..2023 (frozen archive, last n°924). Walks year pages newest-first; a
    year whose page is missing, absent (e.g. the 1995 gap), or empty is
    skipped gracefully. `years` restricts the walk to those calendar years.

    The number is yielded alongside the record (private, single-consumer
    contract) because it is IRRECOVERABLE from the built DocRecord alone —
    titles don't reliably carry it and filenames are inconsistent (see module
    docstring) — and Task 3's cross-system merge needs it to dedup legacy
    rows against the new-system walker's overlap-era papers."""
    for y in range(_LEGACY_LAST_YEAR, _LEGACY_FIRST_YEAR - 1, -1):
        if years is not None and y not in years:
            continue
        url = _LEGACY_YEAR.format(year=y)
        try:
            html = fetcher.get_text(url)
        except Exception as exc:
            print(f"WARNING [bdf-legacy] year {y} fetch failed: {exc}",
                  file=sys.stderr, flush=True)
            continue
        for d, prec, number, title, pdf_url in parse_legacy_year(html, url):
            yield number, DocRecord(
                bank_code="fr", doc_type=DocType.D1, title=title,
                pdf_url=pdf_url, source_url=url, date=d, provenance="bank_site",
                mime_type="application/pdf",
                date_precision=prec, date_source="bank_site",
            )


# ---------------------------------------------------------------------
# New system: www.banque-france.fr paginated working-papers listing
# ---------------------------------------------------------------------

BDF_NEW = "https://www.banque-france.fr"
_NEW_LISTING = BDF_NEW + "/en/publications-and-research/our-main-publications/working-papers"
_NEW_PAGE = _NEW_LISTING + "?page={n}"
# The listing's own pagination links carry every page number up to the last
# one (discovered from page 0 — no separate "how many pages" endpoint).
_PAGE_NUM_RE = re.compile(r"\?page=(\d+)")
# Card date: "21st of August 2026" / "2nd of June 2022" — ordinal day, full
# month name, 4-digit year.
_ORDINAL_DATE_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)\s+of\s+([A-Za-z]+)\s+((?:19|20)\d{2})", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
# WP number: EN "Working Paper Series no. 660." / FR "Document de travail n° 1060."
_WP_NUM_RE = re.compile(
    r"Working Paper Series no\.?\s*(\d+)|Document de travail n°\s*(\d+)", re.I)


def _new_ordinal_date(text: str) -> Optional[date]:
    """"21st of August 2026" -> date(2026, 8, 21); unparseable -> None."""
    m = _ORDINAL_DATE_RE.search(text or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(2)[:3].lower())
    if mon is None:
        return None
    try:
        return date(int(m.group(3)), mon, int(m.group(1)))
    except ValueError:
        return None


def parse_new_listing_page(html: str, base_url: str = BDF_NEW
                           ) -> list[tuple[date, str, str]]:
    """From a new-site working-papers listing page, return (date, title,
    detail_url) per card, in page order (the site lists newest-first).

    Content-based: any ``a.card.card-vertical`` is a paper card; one missing
    its href, title, or a parseable date is malformed and is skipped with a
    stderr warning rather than raising or silently dropping the whole page.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[tuple[date, str, str]] = []
    for card in soup.select("a.card.card-vertical"):
        href = card.get("href")
        title_el = card.select_one(".card-title")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        date_el = card.select_one("small")
        d = _new_ordinal_date(date_el.get_text(" ", strip=True)) if date_el else None
        if not href or not title or d is None:
            print(f"WARNING [bdf-new] malformed listing card, skipped: {base_url}",
                  file=sys.stderr, flush=True)
            continue
        out.append((d, title, urljoin(base_url, href)))
    return out


def _new_wp_number(text: str) -> Optional[int]:
    m = _WP_NUM_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def parse_new_detail(html: str) -> tuple[Optional[int], list[str], Optional[str]]:
    """From a new-site working-paper detail page, return (number, authors,
    pdf_url).

    ``number`` is regex-extracted from the ``og:description`` meta first,
    falling back to the visible description paragraph
    (``.field--name-field-espaces2-header-text``) when the meta lacks it;
    None when absent from both (the caller skips-with-warning — never
    guessed from the URL or card position). ``authors`` come from the
    ``.author-names-wrapper`` spans (comma suffix stripped); parsed for
    completeness but not persisted onto ``DocRecord`` (no author field
    today). ``pdf_url`` is ALWAYS the scraped ``.pdf`` download link, never
    derived from the number.
    """
    soup = BeautifulSoup(html, "lxml")

    og_desc = ""
    for meta in soup.find_all("meta"):
        if (meta.get("property") or "").lower() == "og:description":
            og_desc = meta.get("content") or ""
            break
    body_el = soup.select_one(".field--name-field-espaces2-header-text")
    body_text = body_el.get_text(" ", strip=True) if body_el else ""
    number = _new_wp_number(og_desc)
    if number is None:
        number = _new_wp_number(body_text)

    authors: list[str] = []
    for span in soup.select(".author-names-wrapper .author-names"):
        name = span.get_text(" ", strip=True).rstrip(",").strip()
        if name:
            authors.append(name)

    pdf_a = next((a for a in soup.find_all("a", href=True)
                  if a["href"].lower().endswith(".pdf")), None)
    pdf_url = urljoin(BDF_NEW, pdf_a["href"]) if pdf_a is not None else None

    return number, authors, pdf_url


def _iter_new(fetcher: Fetcher, since: Optional[date] = None,
             max_pages: int = 500) -> Iterator[tuple[int, DocRecord]]:
    """Yield (WP number, DocRecord) for BdF new-site Working Papers (D1),
    2018 -> present, exact day precision.

    Paginates ``?page=0..N`` (the last page number is read off page 0's own
    pagination links); with `since` set, stops once a WHOLE page's cards are
    all older than the cutoff (the listing is newest-first). Each surviving
    card costs one detail-page fetch, for the WP number (skip-with-warning
    when it's absent from both the ``og:description`` meta and the body —
    see ``parse_new_detail``) and the scraped PDF link (skip-with-warning
    when absent). A detail page that fails to fetch is skipped the same way.
    """
    try:
        first_html = fetcher.get_text(_NEW_PAGE.format(n=0))
    except Exception:
        return
    pages_seen = [int(x) for x in _PAGE_NUM_RE.findall(first_html)]
    last = min(max(pages_seen) if pages_seen else 0, max_pages - 1)

    for n in range(last + 1):
        html = first_html if n == 0 else None
        if html is None:
            try:
                html = fetcher.get_text(_NEW_PAGE.format(n=n))
            except Exception as exc:
                print(f"WARNING [bdf-new] page {n} fetch failed: {exc}",
                      file=sys.stderr, flush=True)
                continue
        rows = parse_new_listing_page(html, BDF_NEW)
        page_has_fresh = False
        for d, title, detail_url in rows:
            if since and d < since:
                continue
            page_has_fresh = True
            try:
                detail_html = fetcher.get_text(detail_url)
            except Exception:
                print(f"WARNING [bdf-new] detail page fetch failed, skipped: {detail_url}",
                      file=sys.stderr, flush=True)
                continue
            number, _authors, pdf_url = parse_new_detail(detail_html)
            if number is None:
                print(f"WARNING [bdf-new] no WP number in og:description or body, "
                      f"skipped: {detail_url}", file=sys.stderr, flush=True)
                continue
            if pdf_url is None:
                print(f"WARNING [bdf-new] n°{number} has no PDF link, skipped: {detail_url}",
                      file=sys.stderr, flush=True)
                continue
            yield number, DocRecord(
                bank_code="fr", doc_type=DocType.D1, title=title,
                pdf_url=pdf_url, source_url=detail_url, date=d, provenance="bank_site",
                mime_type="application/pdf",
                date_precision="day", date_source="bank_site",
            )
        if since and rows and not page_has_fresh:   # newest-first -> rest is older
            break


# ---------------------------------------------------------------------
# Merge: public entry point (Task 3)
# ---------------------------------------------------------------------

def discover_fr_wp(fetcher: Fetcher, since: Optional[date] = None,
                   years: Optional[set] = None) -> Iterator[DocRecord]:
    """Yield Banque de France Working Papers (D1) -- the public, merged entry
    point over both publication systems (see module docstring).

    Bounded mode (`since` set): only the NEW system is walked. The legacy
    archive is frozen at n°924/Sept 2023, so it can never hold anything
    newer than a bounded-mode cutoff in practice -- walking it would cost a
    fetch per year for zero possible new documents, so it is skipped
    entirely (the `fetcher` receives no legacy-host requests at all in this
    mode).

    Full mode (`since=None`): both walkers run and are merged, keyed on the
    WP number (continuous across both systems -- see module docstring). The
    1994-2023 legacy archive and the 2018-present new site overlap on
    2018-2023; for any number present in both, the NEW system's record wins
    (its date is day precision, the legacy row is usually month precision).
    Each number is yielded exactly once. `years` restricts the legacy walk
    to those calendar years (ignored in bounded mode, where legacy doesn't
    run at all).
    """
    if since is not None:
        for _num, rec in _iter_new(fetcher, since=since):
            yield rec
        return

    merged: dict[int, DocRecord] = {}
    for num, rec in _iter_legacy(fetcher, years=years):
        merged[num] = rec
    for num, rec in _iter_new(fetcher, since=None):
        merged[num] = rec          # new system wins on any overlapping number
    yield from merged.values()


# ---------------------------------------------------------------------
# Join key for wp_migrate (v2 manifest row <-> native record)
# ---------------------------------------------------------------------

_FR_NUM_RE = re.compile(r"(\d+)")


def fr_wp_number(url: str) -> Optional[int]:
    """WP number from the basename of a BdF PDF URL, or a RePEc IDEAS handle
    URL, else None.

    Both the v2 manifest's pdf_url (downloaded via RePEc, e.g.
    ``.../working-paper_155_2006.pdf`` or ``.../WP1004_0.pdf``) and this
    module's own natively-scraped pdf_url (e.g.
    ``.../document-de-travail_198_2008.pdf``, ``.../wp660_0.pdf``,
    ``.../document-de-travail-661_2018-01_0.pdf``) carry the WP number as
    the FIRST digit run in the filename -- prefix text (French/English,
    with/without hyphens or underscores) comes before it, and any
    publication year or re-upload/revision suffix (``_0``, ``_1``, a
    trailing ``-YYYY``/``_YYYYMMDD``) comes after. The same rule reads a
    RePEc IDEAS paper URL (``.../p/bfr/banfra/{NUM}.html``): its basename's
    only digit run is the WP number. A basename with no digits at all
    (never observed, but not provably impossible) returns None -- the
    caller then falls back to the exact-normalized-title tier in
    ``wp_migrate.build_report``, same as every other bank's key function
    when the URL doesn't carry a usable number.
    """
    seg = (url or "").rstrip("/").rsplit("/", 1)[-1]
    m = _FR_NUM_RE.search(seg)
    return int(m.group(1)) if m else None
