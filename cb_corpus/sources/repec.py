"""RePEc / IDEAS working-paper URL discovery  ->  DocRecord(D1/D2).

IDEAS lists papers by series; each paper page has a "download" link to the
canonical PDF. In v2 we accept that PDF regardless of host: the bank's own
domain is preferred, but EconStor / SSRN / RePEc-cached copies are kept as
fallback so we don't drop documents whose only public source is non-bank.

IDEAS series page:  https://ideas.repec.org/s/<handle>.html
IDEAS paper page:   https://ideas.repec.org/<...>.html  -> has a download link

SERIES is a seed registry for the major banks. Handles should be confirmed on
IDEAS and extended toward all 63 banks; the crawler accepts any handle.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from typing import Callable, Iterator, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..banks import get_bank
from ..http import Fetcher, host_matches, http_status
from ..models import DocRecord
from ..runreport import SourceStats
from ..taxonomy import DocType

IDEAS = "https://ideas.repec.org"

# bank_code -> [(repec_handle, doc_type)]  (verified seed; extend cautiously)
SERIES: dict[str, list[tuple[str, DocType]]] = {
    "ecb": [("ecb:ecbwps", DocType.D1), ("ecb:ecbops", DocType.D2)],
    "us":  [("fip:fedgfe", DocType.D1), ("fip:fedgif", DocType.D1)],
    "gb":  [("boe:boeewp", DocType.D1)],
    "de":  [("zbw:bubdps", DocType.D1)],
    "it":  [("bdi:wptemi", DocType.D1)],
    "es":  [("bde:wpaper", DocType.D1)],
    "fr":  [("bfr:banfra", DocType.D1)],
    "ca":  [("bca:bocawp", DocType.D1)],
    "jp":  [("boj:bojwps", DocType.D1)],
    "ch":  [("snb:snbwpa", DocType.D1)],
    "se":  [("hhs:rbnkwp", DocType.D1)],
    "nl":  [("dnb:dnbwpp", DocType.D1)],
    "au":  [("rba:rbardp", DocType.D1)],
}


def parse_series_page(html: str, base_url: str = IDEAS) -> list[str]:
    """Return absolute IDEAS paper-page URLs listed on a series page."""
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if (href.startswith(("/a/", "/p/", "/h/")) or "/p/" in href) \
                and href.endswith(".html"):
            urls.append(urljoin(base_url, href))
    return list(dict.fromkeys(urls))


def links_to_listing_page(html: str, series: str, page: int) -> bool:
    """True if a listing page links to `<series><page>.html` (IDEAS pagination bar).

    Looks at any `<a href>` on the page, in any directory: the last path
    segment is matched exactly and case-sensitively (IDEAS emits lowercase),
    after dropping the query and fragment. So relative (`rbnkwp2.html`) and
    absolute hrefs count, while share-button URLs that merely mention the file
    in their query (`https://bsky.app/intent/compose?text=...rbnkwp2.html`) and
    longer names (`rbnkwp23.html`) do not. Malformed hrefs that `urlsplit`
    rejects are skipped. A page without any pagination bar -> False.
    """
    target = f"{series}{page}.html"
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        try:
            path = urlsplit(a["href"].strip()).path
        except ValueError:  # e.g. "http://[bad/x.html" (invalid IPv6 netloc)
            continue
        if path.rsplit("/", 1)[-1] == target:
            return True
    return False


def listing_error_truncation(exc: BaseException, page: int,
                             prev_html: Optional[str],
                             series: str) -> Optional[str]:
    """Classify a listing-page fetch error (issue #30).

    Returns None for a CLEAN end of walk: an HTTP 404 (status read from `exc`
    or its __cause__/__context__ chain) on page > 1 that page - 1 does not
    advertise -- IDEAS answers 404 on the page after the last one. Otherwise
    returns the truncation sample to record: a 404 on page 1, a 404 on a page
    that page - 1 links to, and any non-404 / non-HTTP error.
    """
    if http_status(exc) == 404 and page > 1 and prev_html is not None:
        if not links_to_listing_page(prev_html, series, page):
            return None
        return f"page {page}: HTTP 404 although page {page - 1} links to it: {exc}"
    return f"page {page}: {exc}"


def extract_pdf(paper_html: str, bank_homepage: Optional[str] = None) -> Optional[str]:
    """Return the best PDF link found on an IDEAS paper page.

    Preference order:
      1. PDF on the bank's own domain (e.g. boe.co.uk for Bank of England)
      2. PDF on bis.org (BIS re-hosts some)
      3. Any other absolute PDF link (EconStor, SSRN, RePEc cached, ...).

    PDF links surface in three shapes on IDEAS, collected in order:
      a. <a href="...pdf"> — rare today, kept for compatibility.
      b. <input name="url" value="...pdf"> — the canonical IDEAS download
         form; this is where the real PDF actually lives on modern pages.
      c. any absolute .pdf URL anywhere in the raw HTML — last-ditch fallback.

    Returns None if no PDF link is present.
    """
    c = extract_pdf_candidates(paper_html, bank_homepage)
    return c[0] if c else None


def extract_pdf_candidates(paper_html: str,
                           bank_homepage: Optional[str] = None) -> list[str]:
    """All PDF URLs on an IDEAS paper page, ORDERED by preference:
    1. the bank's own domain, 2. bis.org, 3. any other (EconStor/SSRN/cached).

    Collected from <a href>, the <input name="url"> download form, and any bare
    `.pdf` URL in the markup; deduped. Storage tries them in order so a paper
    isn't lost when the preferred host 403s / dies (see DocRecord.alt_urls).
    """
    soup = BeautifulSoup(paper_html, "lxml")
    raw: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("http") and (href.lower().endswith(".pdf") or "pdf" in href.lower()):
            raw.append(href)
    for inp in soup.find_all("input", attrs={"name": "url"}):
        value = inp.get("value", "")
        if value.startswith("http"):
            raw.append(value)
    for match in re.finditer(r'https?://[^\s"\'<>]+?\.pdf', paper_html, re.IGNORECASE):
        raw.append(match.group(0))
    raw = list(dict.fromkeys(raw))  # dedupe, preserve discovery order
    bank = [h for h in raw if bank_homepage and host_matches(h, bank_homepage)]
    bis = [h for h in raw if h not in bank and host_matches(h, "bis.org")]
    rest = [h for h in raw if h not in bank and h not in bis]
    return bank + bis + rest


def extract_official_pdf(paper_html: str, bank_homepage: str,
                         allow_bis: bool = True) -> Optional[str]:
    """Legacy strict variant: only return a PDF on the bank's domain (or BIS).

    Kept for backward compatibility with the existing test fixture.
    """
    soup = BeautifulSoup(paper_html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".pdf") and "pdf" not in href.lower():
            continue
        if not href.startswith("http"):
            continue
        if host_matches(href, bank_homepage):
            return href
        if allow_bis and host_matches(href, "bis.org"):
            return href
    return None


class RePEcDiscovery:
    def __init__(self, fetcher: Optional[Fetcher] = None,
                 max_items_per_series: int = 5000,
                 max_pages: int = 80):
        self.fetcher = fetcher or Fetcher()
        self.max_items = max_items_per_series
        self.max_pages = max_pages

    def _series_paper_pages(self, handle: str,
                            stats: Optional[SourceStats] = None) -> Iterator[list[str]]:
        """Per-page paper-page URLs for a series, following IDEAS pagination.

        IDEAS caps a series listing at ~200 items per page; older papers live
        on numbered pages (`<handle>2.html`, ...). Pages are yielded newest
        first; the walk ends cleanly when a page yields nothing new, when the
        cap is hit, or on an HTTP 404 for page k > 1 that page k-1 does not
        link to (IDEAS answers 404 on the page after the last one; see
        `listing_error_truncation`).

        Any other page-FETCH FAILURE (404 on page 1, 404 on a page that page
        k-1 advertises, network error, 5xx, ...) also ends the walk, but with
        `stats` given it is recorded as `record_fetch_error(..., truncated=True)`
        first — this must never look like a clean "last page reached" in the
        run-report; it is a truncated listing, not a completed one.
        """
        base = f"{IDEAS}/s/{handle.replace(':', '/')}"
        series = base.rsplit("/", 1)[-1]  # same name as the fetched URLs
        seen: set[str] = set()
        prev_html: Optional[str] = None
        for page in range(1, self.max_pages + 1):
            url = f"{base}.html" if page == 1 else f"{base}{page}.html"
            try:
                html = self.fetcher.get_text(url)
            except Exception as exc:
                msg = listing_error_truncation(exc, page, prev_html, series)
                if msg is not None and stats is not None:
                    stats.record_fetch_error(msg, truncated=True)
                break
            prev_html = html
            new = [u for u in parse_series_page(html) if u not in seen]
            if not new:
                break
            seen.update(new)
            yield new

    def _series_paper_urls(self, handle: str) -> list[str]:
        """All paper-page URLs for a series, FOLLOWING IDEAS pagination.

        Back-compat flat-list wrapper over `_series_paper_pages` (kept for
        existing direct callers/tests); capped at `max_items`.
        """
        ordered: list[str] = []
        for page_urls in self._series_paper_pages(handle):
            ordered.extend(page_urls)
            if len(ordered) >= self.max_items:
                break
        return ordered[: self.max_items]

    def discover_bank(self, bank_code: str,
                      skip_url: Optional[Callable[[str], bool]] = None,
                      stop_on_known: bool = False,
                      stats: Optional[SourceStats] = None) -> Iterator[DocRecord]:
        """Yield D1/D2 records for a bank's wired series.

        `stats`, when given, is forwarded to `_series_paper_pages` for every
        series so a listing-page fetch failure is recorded as truncation
        (see that method's docstring) rather than silently ending the walk.
        It also counts the unknown papers this walk passes over, via
        `stats.record_skip`: `paper_fetch_failed` (the paper page fetch
        raised; sample `"<paper_url>: <exc>"`) and `paper_no_pdf` (the page
        has no PDF candidate; sample = the paper URL). Skips are visibility
        only -- they never change the run's exit code. Without `stats`, such
        papers are dropped silently, as before.

        `skip_url(paper_page_url)` short-circuits BEFORE the per-paper fetch
        (mirror of the BIS hook). With `stop_on_known`, a listing page whose
        papers are ALL skipped ends that series' pagination — IDEAS lists
        newest first, so an all-known page means the older tail is known too;
        a page with any unknown paper keeps the walk going (mid-list backfills
        still pull it deeper). Dates play no role here: identity stays on
        stable keys.

        `skip_url` is blind to revisions: a paper page whose PDF changed since
        it was first saved is skipped just like any other known URL (same-URL
        revisions were already invisible before this method existed;
        changed-URL revisions become invisible too now that pagination skips
        known source pages). The only visibility this method offers is a
        count: every skip is tallied and printed once, at the end of the
        bank's walk, as `[repec:<bank_code>] skipped-known: N` on stderr —
        the same channel periodic repec-check audits, so a bank whose skip
        count balloons unexpectedly is discoverable, not silent.

        The counter line prints via `finally`, so it fires even if the
        consumer stops iterating early (e.g. closes the generator after the
        first record) rather than only on a full, natural exhaustion.
        """
        bank = get_bank(bank_code)
        skipped = 0
        try:
            for handle, doc_type in SERIES.get(bank_code, []):
                considered = 0
                for page_urls in self._series_paper_pages(handle, stats=stats):
                    remaining = self.max_items - considered
                    if remaining <= 0:
                        break
                    page_urls = page_urls[:remaining]
                    considered += len(page_urls)
                    unknown_on_page = 0
                    for paper_url in page_urls:
                        if skip_url is not None and skip_url(paper_url):
                            skipped += 1
                            continue
                        unknown_on_page += 1
                        try:
                            paper_html = self.fetcher.get_text(paper_url)
                        except Exception as exc:
                            if stats is not None:
                                stats.record_skip("paper_fetch_failed",
                                                  f"{paper_url}: {exc}")
                            continue
                        cands = extract_pdf_candidates(paper_html, bank.homepage)
                        if not cands:
                            if stats is not None:
                                stats.record_skip("paper_no_pdf", paper_url)
                            continue
                        title, pub_date = _paper_meta(paper_html)
                        yield DocRecord(
                            bank_code=bank_code,
                            doc_type=doc_type,
                            title=title,
                            pdf_url=cands[0],
                            alt_urls=cands[1:],          # tried by Storage if cands[0] fails
                            source_url=paper_url,
                            date=pub_date,
                            provenance="repec_discovery",
                            mime_type="application/pdf",
                            # RePEc's Creation-Date is month-only (YYYY-MM, padded to day 1
                            # by _iso_date) — record that honestly so the date-recovery
                            # waterfall and repec-check can target these rows.
                            date_precision="month",
                            date_source="repec",
                        )
                    if stop_on_known and unknown_on_page == 0:
                        break
                    if considered >= self.max_items:
                        # Cap is now fully bound -- don't re-enter the page
                        # generator for another (wasted) listing-page fetch.
                        break
        finally:
            print(f"[repec:{bank_code}] skipped-known: {skipped}",
                  file=sys.stderr, flush=True)


def _title_of(paper_html: str) -> str:
    soup = BeautifulSoup(paper_html, "lxml")
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    if soup.title:
        return soup.title.get_text(strip=True)
    return "(untitled)"


def _iso_date(s: str) -> Optional[date]:
    """Parse 'YYYY-MM-DD', 'YYYY/MM', or 'YYYY' into a date (missing parts -> 1)."""
    parts = (s or "").strip().replace("/", "-").split("-")
    try:
        y = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 and parts[1] else 1
        d = int(parts[2]) if len(parts) > 2 and parts[2] else 1
        return date(y, m, d)
    except (ValueError, IndexError):
        return None


def _paper_meta(paper_html: str) -> tuple[str, Optional[date]]:
    """Extract (title, publication date) from an IDEAS paper page.

    IDEAS embeds Highwire `citation_*` meta tags. Use `citation_publication_date`
    (YYYY/MM — the real publication date), then `citation_year`. The bare `date`
    meta is deliberately ignored: on IDEAS it is the RePEc record/index date
    (uniformly YYYY-02-02), which previously dated ~16k papers to 2 February.
    Falls back to the <h1> for the title.
    """
    soup = BeautifulSoup(paper_html, "lxml")
    title = ""
    full: Optional[date] = None
    year: Optional[int] = None
    for m in soup.find_all("meta"):
        name = (m.get("name") or m.get("property") or "").lower()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if name == "citation_title":
            title = content
        elif name == "citation_publication_date" and full is None:
            # The real publication date (YYYY/MM). Do NOT read the bare `date`
            # meta: on IDEAS that is the RePEc record/index date (uniformly
            # YYYY-02-02), which silently dated ~16k papers to 2 February.
            full = _iso_date(content)
        elif name == "citation_year" and year is None and content.isdigit():
            year = int(content)
    pub_date = full or (date(year, 1, 1) if year else None)
    return (title or _title_of(paper_html)), pub_date
