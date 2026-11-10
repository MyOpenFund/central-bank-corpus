"""`recover-downloads` — inventory-driven Wayback recovery.

Every download that fails ALL of its candidate URLs is durably logged to
``data/download_errors.jsonl`` (``Storage._record_download_error``, live since
PR #6). Most of that inventory is genuinely gone from the bank's own site but
still sitting in the Wayback Machine under its ORIGINAL (now-dead) URL — this
module turns that inventory into recovered documents, the same way
``sources/wayback.py``'s ``WaybackSource``/``run_wayback_recovery`` already do
for hand-picked ``url_prefix`` sweeps, except driven by the audit trail
instead of a CDX prefix walk.

Per entry: skip it if the corpus already converged on it since the failure was
logged (nightly retries fill some gaps on their own); otherwise refresh title/
date from the IDEAS source page when there is one (same honesty as
``sources/repec.py`` discovery: month precision, ``date_source="repec"``);
look up the latest Wayback snapshot of the official URL (falling back to any
alternate URL); dry-run reports it, ``--download`` saves it with
``provenance="wayback"`` and the official bank URL untouched as ``pdf_url``
(citation + stable ``doc_id``) — the raw snapshot is just an ``alt_urls``
fallback ``Storage.save`` tries.

No fuzzy matching anywhere: an entry with no snapshot under any known URL is
reported ``unrecoverable`` and left alone, honestly. When ``--download``
finds a snapshot but ``Storage.save`` reports ``skip:*`` (the bytes
hash-match a doc already in the corpus, or the doc_id was already indexed),
the entry is reported ``duplicate`` -- there is nothing left to recover,
so it is never relabelled ``recoverable`` (which would just re-download the
same duplicate PDF every run). A ``duplicate`` verdict also SELF-HEALS: the
dead URL (and, in ``--candidates`` mode, the candidate's own verified
``final_url``) is stamped onto the matched row's ``alt_urls`` and its
quarantine is released (one ``Storage.stamp_alt_urls`` call in a ``finally``
-- applied even if the pass dies mid-loop, never during a dry-run), so the
same dead URL is
recognised as already known on every future pass instead of being
rediscovered as ``duplicate`` forever. The matched row's doc_id is recorded
per-entry in the CSV's ``canonical_doc_id`` column (empty for every other
action) -- an audit trail so an operator can spot a wrong pairing.

``--candidates <path>`` switches to a SEPARATE, candidates-only mode (no CDX
walk at all): each JSONL line is an EXTERNALLY-recovered file (hunted by hand
via Wayback / the bank's current site / a legitimate mirror --
recover-quarantine design decision 1/2) --
``{dead_pdf_url, file_path, recovered_from: wayback|bank_site|mirror,
final_url, title?}``. The audit entry is matched by ``dead_pdf_url`` (an
entry with no match is reported ``unknown-entry``, never guessed at); the
already-downloaded local file is verified (``%PDF`` magic, >20KB) before
anything is registered (``bad-file`` otherwise); the resulting ``DocRecord``
follows the provenance rules per ``recovered_from`` (decision 2). Because the
bytes already exist locally, this mode registers them via
``Storage.reindex`` (copy to ``Storage.target_path`` + index, sha256 dedup)
instead of ``Storage.save`` (no network fetch) -- same storage discipline,
never bypassed. Like the CDX-walk pass, ``--candidates`` also short-circuits
on ``_is_converged`` before any file validation or download: re-running the same
candidates file after a prior pass already self-healed it (stamped the dead
URL onto the canonical row) reports ``converged``, not ``duplicate`` -- "the
corpus already has it" is a different truth than "nothing left to recover",
and both modes now share the same vocabulary for it.
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

from .banks import get_bank
from .config import Config
from .http import Fetcher
from .models import DocRecord
from .quarantine import Quarantine
from .sources.repec import IDEAS, _paper_meta, extract_pdf_candidates
from .sources.wayback import latest_capture, raw_url
from .storage import Storage
from .taxonomy import by_code

_ACTIONS = ("recoverable", "recovered", "duplicate", "unrecoverable", "converged")
_CSV_FIELDS = ("bank", "pdf_url", "action", "snapshot_ts", "title", "canonical_doc_id")

# `Storage.save`/`Storage.reindex` prefix for a same-bytes match; the matched
# doc_id, when known, follows as a `:<doc_id>` suffix (e.g.
# "skip:duplicate-content:<doc_id>"). See `_duplicate_doc_id`.
_DUPLICATE_CONTENT_PREFIX = "skip:duplicate-content"

# --candidates local-file verification: reject anything too small to
# plausibly be a real working paper (a truncated download, an HTML error
# page saved with a .pdf extension, etc).
_MIN_CANDIDATE_PDF_BYTES = 20 * 1024
_PDF_MAGIC = b"%PDF"


def _read_inventory(cfg: Config,
                    bank_codes: Optional[Iterable[str]] = None) -> list[dict]:
    """Read ``data/download_errors.jsonl``, dedup by ``pdf_url`` keeping the
    LATEST entry (the file is append-ordered, so a later line simply
    overwrites an earlier one for the same url). Optional ``bank_codes``
    filter. A missing file is tolerated: empty inventory, a stderr note, no
    crash (the file is created lazily by the crawler only once a download
    actually fails)."""
    path = cfg.data_dir / "download_errors.jsonl"
    if not path.exists():
        print(f"[recover] no {path} -- nothing to recover", file=sys.stderr, flush=True)
        return []
    codes = set(bank_codes) if bank_codes else None
    by_url: dict[str, dict] = {}
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if codes is not None and entry.get("bank_code") not in codes:
                continue
            url = entry.get("pdf_url")
            if not url:
                continue
            by_url[url] = entry   # later line wins -> latest entry per pdf_url
    return list(by_url.values())


def _is_converged(storage: Storage, entry: dict) -> bool:
    """True if the corpus already has this document (by pdf_url, any of the
    audit entry's alt_urls, or its source_url) -- the nightly retry already
    got it since the failure was logged; no network needed to know that."""
    if storage.is_known_url(entry.get("pdf_url") or ""):
        return True
    for alt in entry.get("alt_urls") or []:
        if alt and storage.is_known_url(alt):
            return True
    source_url = entry.get("source_url") or ""
    return bool(source_url) and storage.is_known_source_url(source_url)


def _bank_homepage(bank_code: str) -> Optional[str]:
    try:
        return get_bank(bank_code).homepage
    except KeyError:
        return None


def _refresh_metadata(fetcher: Fetcher, entry: dict) -> tuple[str, object, Optional[str],
                                                              Optional[str], list[str]]:
    """(title, date, date_precision, date_source, fresh alt-url candidates).

    When ``source_url`` is an IDEAS page, fetch it once and take title/date/
    candidates via the existing ``_paper_meta``/``extract_pdf_candidates``
    (month precision, ``date_source="repec"`` -- same honesty as RePEc
    discovery). On any fetch failure, OR when ``source_url`` isn't IDEAS, fall
    back to the audit entry's own title; date stays unknown (``None``), and
    ``date_precision``/``date_source`` are left as ``None`` here so the caller
    leaves the ``DocRecord`` defaults untouched -- mirroring how
    ``WaybackSource`` already handles a paper with no recoverable date.
    """
    title = entry.get("title") or ""
    source_url = entry.get("source_url") or ""
    if source_url.startswith(IDEAS):
        try:
            html = fetcher.get_text(source_url)
            fresh_title, fresh_date = _paper_meta(html)
            cands = extract_pdf_candidates(html, _bank_homepage(entry.get("bank_code") or ""))
            return (fresh_title or title, fresh_date, "month", "repec", cands)
        except Exception:
            pass
    return (title, None, None, None, [])


def _find_snapshot(fetcher: Fetcher, pdf_url: str,
                   alt_candidates: list[str]) -> tuple[Optional[str], Optional[str]]:
    """(timestamp, original_url) for the latest Wayback snapshot of the
    official pdf_url, or -- on a miss -- of each alt url in order. ``None,
    None`` when nothing is archived anywhere."""
    for candidate in [pdf_url, *alt_candidates]:
        if not candidate:
            continue
        ts = latest_capture(fetcher, candidate)
        if ts:
            return ts, candidate
    return None, None


def _read_candidates(path: str) -> list[dict]:
    """Read a ``--candidates`` JSONL file: one externally-recovered doc per
    line. No dedup here (unlike ``_read_inventory``) -- each line is its own
    recovery decision, matched independently below.

    A malformed JSON line is tolerated: skipped with ONE stderr warning for
    that line (identifying it by line number), the good lines around it
    still collected. There is no CSV row for it -- a line that doesn't even
    parse has no URL to report against, unlike the recognised bad-* actions
    below which all start from a valid, parsed candidate."""
    out: list[dict] = []
    with open(path) as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"[recover] WARNING: malformed JSON on line {lineno} of "
                      f"{path}, skipped: {exc}", file=sys.stderr, flush=True)
    return out


def _verify_local_pdf(path: Path) -> bool:
    """True iff ``path`` exists, starts with the ``%PDF`` magic, and is
    bigger than ``_MIN_CANDIDATE_PDF_BYTES`` -- the honesty check before an
    externally-hunted file is ever registered into the corpus (recover-
    quarantine design: verified local PDF)."""
    try:
        if not path.is_file():
            return False
        if path.stat().st_size <= _MIN_CANDIDATE_PDF_BYTES:
            return False
        with path.open("rb") as fh:
            head = fh.read(len(_PDF_MAGIC))
    except OSError:
        return False
    return head == _PDF_MAGIC


def _apply_seed_quarantine(quarantine: Quarantine, path: str) -> int:
    """Apply a ``--seed-quarantine`` JSONL file (``{url, reason?}`` per
    line) -- for the docs that remain unrecoverable after the manual hunt,
    so the nightly sync stops re-hammering them immediately (recover-
    quarantine design §3/§4). Returns the number of lines applied; a
    missing/blank ``url`` on an otherwise well-formed line is skipped,
    never crashes the pass.

    Reads and validates the WHOLE file first, THEN applies every seed --
    never interleaved. A malformed line ANYWHERE in the file raises (surfaced
    loudly to the operator) BEFORE a single ``quarantine.seed()`` call has
    happened, so a bad line never leaves a partial prefix of the file
    seeded while the rest silently never runs."""
    rows: list[tuple[str, str]] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            url = row.get("url")
            if not url:
                continue
            rows.append((url, row.get("reason") or ""))

    n = 0
    for url, reason in rows:
        quarantine.seed(url, reason)
        n += 1
    return n


def _candidate_provenance(dead_url: str, final_url: str,
                          recovered_from: str) -> Optional[tuple[str, str, list[str]]]:
    """(pdf_url, provenance, alt_urls) per the recover-quarantine design's
    provenance rules (decision 2), or ``None`` for an unrecognised
    ``recovered_from`` value (never guessed at)."""
    if recovered_from == "wayback":
        # Snapshot recovery: pdf_url stays the original official URL
        # (citation + stable doc_id), the Wayback copy is the alt fallback.
        return dead_url, "wayback", ([final_url] if final_url else [])
    if recovered_from == "bank_site":
        # The paper moved to a new live URL -- pdf_url points at reality,
        # the old dead URL is kept as an alt for provenance/citation history.
        return final_url, "bank_site", ([dead_url] if dead_url else [])
    if recovered_from == "mirror":
        # Legitimate mirror/co-publication: pdf_url is still the WP's own
        # identity (the official URL), the mirror is only an alt copy.
        return dead_url, "mirror", ([final_url] if final_url else [])
    return None


def _duplicate_doc_id(status: str) -> Optional[str]:
    """The matched doc_id embedded in a `skip:duplicate-content:<doc_id>`
    status, or ``None`` when there is no suffix to extract -- either a bare
    legacy status (an old/stubbed caller that never adopted the suffix) or a
    status that isn't a duplicate-content skip at all. ``None`` means
    "nothing to stamp for this row"; callers must never guess a doc_id, and
    must never crash on the bare form."""
    if status.startswith(_DUPLICATE_CONTENT_PREFIX + ":"):
        return status[len(_DUPLICATE_CONTENT_PREFIX) + 1:] or None
    return None


def _add_stamp(stamps: dict[str, list[str]], doc_id: str, *urls: str) -> None:
    """Ordered, deduped stamp collection: dead URL first (provenance
    history), then any live alternative. Order is part of the contract --
    a set here made alt_urls append order vary across runs (hash
    randomization), churning autocommitted manifest diffs for nothing."""
    bucket = stamps.setdefault(doc_id, [])
    for url in urls:
        if url and url not in bucket:
            bucket.append(url)


def _apply_stamps(storage: Storage, stamps: dict[str, list[str]]) -> None:
    """Apply the alt_url stamps collected during a pass (see
    `run_recover_downloads`/`_run_candidates_pass`) in ONE
    `Storage.stamp_alt_urls` call, and print the honest (urls, rows)
    accounting. A no-op (no print, no rewrite) when nothing was collected --
    callers only build `stamps` at all when `download` is True, so this
    never runs during a dry-run."""
    if not stamps:
        return
    urls_stamped, rows_modified = storage.stamp_alt_urls(stamps)
    print(f"[recover] stamped {urls_stamped} alt_url(s) on {rows_modified} row(s)",
          file=sys.stderr, flush=True)


def _run_candidates_pass(cfg: Config, storage: Storage, fetcher: Fetcher,
                         candidates_path: str, bank_codes: Optional[Iterable[str]],
                         download: bool) -> tuple[dict[str, dict], list[dict]]:
    """The ``--candidates`` mode of ``run_recover_downloads`` -- see the
    module docstring. No CDX walk: every candidate's fate is decided from
    the audit entry it matches (by ``dead_pdf_url``) plus the already-
    downloaded local file.

    The inventory is read UNFILTERED (``bank_codes=None``) so an entry that
    exists but is out of the ``--banks`` scope can be told apart from one
    that genuinely doesn't exist -- the former is ``filtered``, the latter
    ``unknown-entry`` (never conflated)."""
    entries = _read_inventory(cfg, None)
    by_url = {e["pdf_url"]: e for e in entries if e.get("pdf_url")}
    codes = set(bank_codes) if bank_codes else None

    results: dict[str, dict] = {}
    csv_rows: list[dict] = []
    # doc_id -> ordered URLs to stamp onto its alt_urls, applied ONCE at the
    # end (never during dry-run -- only ever populated inside `if download:`
    # branches below).
    stamps: dict[str, list[str]] = {}

    def _bump(bank: str, action: str) -> dict:
        summary = results.setdefault(bank, {a: 0 for a in _ACTIONS})
        summary[action] = summary.get(action, 0) + 1
        return summary

    try:
        for cand in _read_candidates(candidates_path):
            dead_url = cand.get("dead_pdf_url") or ""
            entry = by_url.get(dead_url)

            if entry is None:
                _bump("_unknown", "unknown-entry")
                csv_rows.append({"bank": "_unknown", "pdf_url": dead_url,
                                 "action": "unknown-entry", "snapshot_ts": "",
                                 "title": cand.get("title") or ""})
                continue

            bank = entry.get("bank_code") or "_unknown"
            fallback_title = entry.get("title") or cand.get("title") or ""

            if codes is not None and bank not in codes:
                _bump(bank, "filtered")
                csv_rows.append({"bank": bank, "pdf_url": dead_url, "action": "filtered",
                                 "snapshot_ts": "", "title": fallback_title})
                continue

            if _is_converged(storage, entry):
                # The index already knows every URL of this entry (typically
                # via a stamp from a previous pass): "the corpus already has
                # it" is a different truth than "nothing left to recover"
                # (duplicate), and the CDX pass already reports it as such --
                # keep the two modes' vocabulary consistent (PR #13 final
                # review, prior Minor 1). No stamping, no quarantine call.
                _bump(bank, "converged")
                csv_rows.append({"bank": bank, "pdf_url": dead_url, "action": "converged",
                                 "snapshot_ts": "", "title": fallback_title})
                continue

            file_path = Path(cand.get("file_path") or "")
            if not _verify_local_pdf(file_path):
                _bump(bank, "bad-file")
                csv_rows.append({"bank": bank, "pdf_url": dead_url, "action": "bad-file",
                                 "snapshot_ts": "", "title": fallback_title})
                continue

            doc_type = None
            try:
                doc_type = by_code(entry.get("doc_type") or "")
            except KeyError:
                pass
            if doc_type is None:
                _bump(bank, "bad-doc-type")
                csv_rows.append({"bank": bank, "pdf_url": dead_url, "action": "bad-doc-type",
                                 "snapshot_ts": "", "title": fallback_title})
                continue

            final_url = cand.get("final_url") or ""
            if not final_url:
                # An empty/missing final_url would otherwise create a degenerate
                # pdf_url (e.g. bank_site's pdf_url = final_url = "") shared by
                # every such candidate -- reject it honestly, before it ever
                # reaches provenance mapping, rather than let it become an alias
                # for a doc_id that isn't really this document's identity.
                _bump(bank, "bad-candidate")
                csv_rows.append({"bank": bank, "pdf_url": dead_url, "action": "bad-candidate",
                                 "snapshot_ts": "", "title": fallback_title})
                continue
            if final_url == dead_url:
                # final_url resolving back to the exact dead URL carries no
                # honest byte-origin trail -- this is exactly the failure mode
                # that once produced a "wayback" manifest row whose alt_urls[0]
                # silently equalled its own dead pdf_url (final-review finding
                # 1: no real snapshot behind it at all). Reject before
                # provenance mapping ever sees it, for every recovered_from.
                _bump(bank, "bad-candidate")
                csv_rows.append({"bank": bank, "pdf_url": dead_url, "action": "bad-candidate",
                                 "snapshot_ts": "", "title": fallback_title})
                continue
            recovered_from = cand.get("recovered_from") or ""
            if recovered_from == "wayback" and urlparse(final_url).hostname != "web.archive.org":
                # A candidate claiming a wayback recovery whose final_url isn't
                # actually hosted on web.archive.org is not honestly a snapshot
                # -- same finding, the other half of it (a plausible-looking but
                # non-archive.org URL rather than the dead URL itself).
                _bump(bank, "bad-candidate")
                csv_rows.append({"bank": bank, "pdf_url": dead_url, "action": "bad-candidate",
                                 "snapshot_ts": "", "title": fallback_title})
                continue
            provenance_result = _candidate_provenance(dead_url, final_url, recovered_from)
            if provenance_result is None:
                _bump(bank, "bad-provenance")
                csv_rows.append({"bank": bank, "pdf_url": dead_url, "action": "bad-provenance",
                                 "snapshot_ts": "", "title": fallback_title})
                continue
            pdf_url, provenance, alt_urls = provenance_result

            # Refresh title/date from the IDEAS source page exactly as the
            # inventory-driven path does; a candidate-supplied title (if any) is
            # a better fallback than the (possibly stale) audit-entry title when
            # the IDEAS refresh itself fails.
            entry_for_refresh = dict(entry)
            if cand.get("title"):
                entry_for_refresh["title"] = cand["title"]
            title, rec_date, date_precision, date_source, _cands = _refresh_metadata(
                fetcher, entry_for_refresh)

            if provenance == "wayback" and not date_source:
                # No repec date recovered -- honestly attribute whatever date
                # metadata we do have (possibly none) to the wayback hunt itself,
                # never leaving the DocRecord default ("bank_site") standing in
                # for a document that was NOT found on the bank's own site.
                date_source = "wayback"

            rec = DocRecord(
                bank_code=bank, doc_type=doc_type, title=title,
                pdf_url=pdf_url, alt_urls=alt_urls,
                source_url=entry.get("source_url") or "",
                date=rec_date, provenance=provenance,
                mime_type="application/pdf",
            )
            if date_precision:
                rec.date_precision = date_precision
            elif rec_date is None:
                rec.date_precision = None           # no date -> no precision
            if date_source:
                rec.date_source = date_source

            _bump(bank, "recoverable")
            action = "recoverable"
            canonical_doc_id = ""

            # dest == storage.target_path(rec) is DERIVED FROM doc_id, so for an
            # already-indexed doc_id it is the SAME PATH as that doc's real
            # local_path. Probe with a dry-run BEFORE copying anything -- in
            # BOTH modes (MINOR 7: mode parity), not just --download: if this
            # doc_id is already indexed there is nothing left to recover, dry-run
            # or not, so "recoverable" would be a lie either way. In --download
            # mode it's also a safety guard -- copying then unlinking on
            # "skip:already-indexed" would DELETE the real canonical file,
            # leaving a dangling manifest row (the exact bug this guards).
            probe = storage.reindex(rec, file_path, dry_run=True)
            if probe == "skip:already-indexed":
                _bump(bank, "duplicate")
                action = "duplicate"
                canonical_doc_id = rec.doc_id
                # Self-healing, but ONLY on a real run: the dry-run contract
                # (mode parity above) must never stamp/release quarantine/write
                # the manifest. For wayback/mirror provenance rec.pdf_url IS the
                # dead URL already (this row's own identity), so the stamp is a
                # no-op for alt_urls there -- but for bank_site rec.pdf_url is
                # the NEW final_url, so dead_url is genuinely different and DOES
                # land in alt_urls (the flagship self-heal case this PR exists
                # for). Either way the quarantine release still matters.
                if download:
                    _add_stamp(stamps, rec.doc_id, dead_url)
                    storage.quarantine.record_success(dead_url)
            elif download:
                dest = storage.target_path(rec)
                status = "error"
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file_path, dest)
                    status = storage.reindex(rec, dest)
                except Exception as exc:  # noqa: BLE001 - audited below, never aborts the pass
                    try:
                        storage._record_download_error(rec, exc, "recover-downloads-candidates")
                    except Exception:
                        pass
                if status == "reindexed":
                    _bump(bank, "recovered")
                    action = "recovered"
                    # MINOR 5: tombstone the OLD dead URL's quarantine too.
                    # reindex() above already released quarantine on rec.pdf_url
                    # -- for wayback/mirror that IS dead_url (this call is then a
                    # harmless no-op), but for bank_site rec.pdf_url is the NEW
                    # final_url, so without this the OLD dead_url would stay
                    # quarantined forever even though the corpus now has the doc
                    # under its new address.
                    storage.quarantine.record_success(dead_url)
                elif status.startswith("skip:duplicate-content"):
                    # Bytes hash-matched a DIFFERENT doc_id's content -- dest is
                    # this (not-yet-indexed) doc_id's own path, so the copy just
                    # made is safe orphan bytes, never the other doc's canonical
                    # file. Remove it. `startswith` (not `==`) because the status
                    # now carries the matched doc_id as a `:<id>` suffix (e.g.
                    # "skip:duplicate-content:<doc_id>"); the bare legacy string
                    # with no suffix is tolerated too.
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                    _bump(bank, "duplicate")
                    action = "duplicate"
                    matched_id = _duplicate_doc_id(status)
                    if matched_id:
                        # Both the dead URL AND this candidate's own final_url
                        # (its verified snapshot/mirror/live copy) are stamped
                        # onto the matched row -- final_url was never dead, so
                        # only dead_url's quarantine is released.
                        canonical_doc_id = matched_id
                        _add_stamp(stamps, matched_id, dead_url, final_url)
                        storage.quarantine.record_success(dead_url)
                elif status.startswith("skip:"):
                    # Any other non-"reindexed" status (e.g. skip:missing-file,
                    # or skip:already-indexed from a same-run race with an
                    # earlier candidate line for the same doc_id): leave the
                    # file alone -- never guess it's safe to delete -- and
                    # report the status verbatim.
                    summary = results.setdefault(bank, {a: 0 for a in _ACTIONS})
                    summary[status] = summary.get(status, 0) + 1
                    action = status
                # status == "error": action stays "recoverable" (audited above).

            csv_rows.append({"bank": bank, "pdf_url": dead_url, "action": action,
                             "snapshot_ts": "", "title": title,
                             "canonical_doc_id": canonical_doc_id})

    finally:
        # Quarantine releases are durable per-entry inside the loop, while
        # stamps applied only after it -- a mid-pass crash (SIGKILL aside)
        # would leave released-but-unstamped dead URLs that the nightly
        # sync resumes hammering. finally shrinks that window to the
        # entry being processed (PR #13 final review, Minor 1).
        _apply_stamps(storage, stamps)
    return results, csv_rows


def run_recover_downloads(bank_codes: Optional[Iterable[str]] = None,
                          download: bool = False,
                          csv_path: Optional[str] = None,
                          config: Optional[Config] = None,
                          fetcher: Optional[Fetcher] = None,
                          candidates: Optional[str] = None,
                          seed_quarantine: Optional[str] = None) -> dict[str, dict]:
    """Drive the full recover-downloads pass. Dry-run by default: only the CSV
    is written, nothing is downloaded or saved (``--download`` opt-in mirrors
    the rest of the corpus's discovery commands). Returns
    ``{bank_code: {"recoverable": n, "recovered": n, "duplicate": n,
    "unrecoverable": n, "converged": n}}`` -- ``recovered`` only counts
    entries actually saved in ``--download`` mode; ``duplicate`` counts
    ONLY the two exact dedup statuses (``skip:already-indexed``,
    ``skip:duplicate-content``) -- nothing left to recover there, so the CSV
    action is honestly ``duplicate``, not ``recoverable`` (which would keep
    re-downloading the full PDF every run for no gain); any OTHER ``skip:*``
    status is reported verbatim as the CSV action, never mislabeled --
    recovery saves run with ``bypass_quarantine=True`` precisely so the
    quarantine gate (fed by the same ``download_errors.jsonl`` inventory)
    cannot silently block them. Every ``duplicate`` verdict self-heals (see
    the module docstring): its dead URL is stamped onto the matched row's
    ``alt_urls`` and its quarantine released, in ONE ``Storage.stamp_alt_urls``
    call collected across the pass and applied from a ``finally`` block --
    at the end of the pass on the normal path, but also on a mid-pass crash
    (PR #13 final review, Minor 1), so a crash never loses stamps already
    collected for entries processed before it hit -- never during a
    dry-run. A CSV
    report (``{bank, pdf_url, action, snapshot_ts, title, canonical_doc_id}``)
    is written in both modes so a dry-run's classification is never lost, and
    a CSV line never claims an action that didn't happen (a failed
    ``--download`` save stays ``recoverable``, not ``recovered``; its failure
    lands in ``download_errors.jsonl`` like any other, via the audit path).
    ``canonical_doc_id`` is filled only for a ``duplicate`` row (the matched
    row's doc_id), empty otherwise.

    ``candidates`` switches to the ``--candidates`` mode (see the module
    docstring): the CDX walk below is skipped ENTIRELY, replaced by
    :func:`_run_candidates_pass`. ``seed_quarantine``, independent of that
    switch, applies a ``--seed-quarantine`` file (:func:`_apply_seed_quarantine`)
    before the pass runs -- it may be combined with either mode, or used
    alone (a ``download_errors.jsonl``-only run with no candidates).
    """
    cfg = config or Config()
    fetcher = fetcher or Fetcher(cfg)
    storage = Storage(cfg, fetcher)

    if seed_quarantine:
        n = _apply_seed_quarantine(storage.quarantine, seed_quarantine)
        print(f"[recover] seeded {n} url(s) into quarantine from {seed_quarantine}",
              file=sys.stderr, flush=True)

    if candidates:
        results, csv_rows = _run_candidates_pass(cfg, storage, fetcher, candidates,
                                                  bank_codes, download)
        out = csv_path or str(cfg.reports_dir / "recover_downloads.csv")
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(_CSV_FIELDS))
            w.writeheader()
            for row in csv_rows:
                w.writerow(row)
        print(f"[recover] wrote {len(csv_rows)} row(s) -> {out}", file=sys.stderr, flush=True)
        return results

    entries = _read_inventory(cfg, bank_codes)
    results: dict[str, dict] = {}
    csv_rows: list[dict] = []
    # doc_id -> ordered URLs to stamp onto its alt_urls, applied ONCE at the
    # end (never during dry-run -- this whole CDX-walk pass only ever
    # attempts a save, and so can only classify a duplicate, inside
    # `if download:`).
    stamps: dict[str, list[str]] = {}

    try:
        for entry in entries:
            bank = entry.get("bank_code") or "_unknown"
            summary = results.setdefault(bank, {a: 0 for a in _ACTIONS})
            pdf_url = entry.get("pdf_url") or ""

            if _is_converged(storage, entry):
                summary["converged"] += 1
                csv_rows.append({"bank": bank, "pdf_url": pdf_url, "action": "converged",
                                 "snapshot_ts": "", "title": entry.get("title") or ""})
                continue

            title, rec_date, date_precision, date_source, cands = _refresh_metadata(fetcher, entry)
            alt_candidates = list(dict.fromkeys([*cands, *(entry.get("alt_urls") or [])]))
            ts, snapshot_of = _find_snapshot(fetcher, pdf_url, alt_candidates)

            if ts is None:
                summary["unrecoverable"] += 1
                csv_rows.append({"bank": bank, "pdf_url": pdf_url, "action": "unrecoverable",
                                 "snapshot_ts": "", "title": title})
                continue

            summary["recoverable"] += 1
            action = "recoverable"
            canonical_doc_id = ""

            if download:
                doc_type = None
                try:
                    doc_type = by_code(entry.get("doc_type") or "")
                except KeyError:
                    pass
                if doc_type is not None:
                    snapshot_url = raw_url(snapshot_of, ts)
                    rec_alts = list(dict.fromkeys(
                        [snapshot_url, *(u for u in alt_candidates if u != pdf_url)]))
                    rec = DocRecord(
                        bank_code=bank, doc_type=doc_type, title=title,
                        pdf_url=pdf_url, alt_urls=rec_alts,
                        source_url=entry.get("source_url") or "",
                        date=rec_date, provenance="wayback",
                        mime_type="application/pdf",
                    )
                    if date_precision:
                        rec.date_precision = date_precision
                    elif rec_date is None:
                        rec.date_precision = None   # no date -> no precision
                    # No repec date recovered -> the date metadata (possibly
                    # none) comes from the wayback hunt, never the DocRecord
                    # default "bank_site" (same rule as the candidates pass).
                    rec.date_source = date_source or "wayback"
                    try:
                        # bypass_quarantine=True: this whole inventory comes FROM
                        # download_errors.jsonl, the same file that feeds the
                        # quarantine counter, so by the time recovery runs its own
                        # targets are typically already quarantined -- without the
                        # bypass, save() would short-circuit to "skip:quarantined"
                        # before ever trying the Wayback snapshot alt_url, and that
                        # skip would silently fall into the "duplicate" bucket below
                        # (a lie: nothing was actually deduplicated).
                        status = storage.save(rec, bypass_quarantine=True)
                    except Exception as exc:  # noqa: BLE001 - audited below, never aborts the pass
                        status = "error"
                        try:
                            storage._record_download_error(rec, exc, "recover-downloads")
                        except Exception:
                            pass
                    if status == "saved":
                        summary["recovered"] += 1
                        action = "recovered"
                    elif status == "skip:already-indexed":
                        # The doc_id was already indexed -- always bare, the
                        # matched doc_id is rec.doc_id itself. Nothing left to
                        # recover; self-heal by stamping the dead URL onto its
                        # OWN row (a no-op for alt_urls, since pdf_url is that
                        # dead URL already) and releasing its quarantine, so a
                        # future nightly sync doesn't see it as still quarantined.
                        summary["duplicate"] += 1
                        action = "duplicate"
                        canonical_doc_id = rec.doc_id
                        _add_stamp(stamps, rec.doc_id, pdf_url)
                        storage.quarantine.record_success(pdf_url)
                    elif status.startswith("skip:duplicate-content"):
                        # Bytes hash-matched a DIFFERENT existing doc_id, carried
                        # as the status suffix (a bare legacy status with no
                        # suffix is tolerated -- no doc_id to stamp, never
                        # crashes). Either way there is nothing left to recover
                        # here. Reporting this as "recoverable" would be a lie
                        # (nothing recoverable remains) and would keep
                        # re-downloading the full PDF every run just to discover
                        # the same duplicate again -- self-heal instead: stamp
                        # the dead URL onto the row it actually matched.
                        summary["duplicate"] += 1
                        action = "duplicate"
                        matched_id = _duplicate_doc_id(status)
                        if matched_id:
                            canonical_doc_id = matched_id
                            _add_stamp(stamps, matched_id, pdf_url)
                            storage.quarantine.record_success(pdf_url)
                    elif status.startswith("skip:"):
                        # Any OTHER skip:* (e.g. a future status we don't special-
                        # case here) is reported VERBATIM, never mislabeled as
                        # "duplicate" -- an honest description of what save() said,
                        # even if unanticipated.
                        summary[status] = summary.get(status, 0) + 1
                        action = status

            csv_rows.append({"bank": bank, "pdf_url": pdf_url, "action": action,
                             "snapshot_ts": ts, "title": title,
                             "canonical_doc_id": canonical_doc_id})

    finally:
        # Quarantine releases are durable per-entry inside the loop, while
        # stamps applied only after it -- a mid-pass crash (SIGKILL aside)
        # would leave released-but-unstamped dead URLs that the nightly
        # sync resumes hammering. finally shrinks that window to the
        # entry being processed (PR #13 final review, Minor 1).
        _apply_stamps(storage, stamps)

    out = csv_path or str(cfg.reports_dir / "recover_downloads.csv")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(_CSV_FIELDS))
        w.writeheader()
        for row in csv_rows:
            w.writerow(row)
    print(f"[recover] wrote {len(csv_rows)} row(s) -> {out}", file=sys.stderr, flush=True)

    return results
