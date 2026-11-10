"""Filesystem storage, manifest, dedup.

Layout:  data/raw/<bank>/<doctype>/<year>/<doc_id>.<ext>   (ext: pdf or html)
Manifest: data/manifest/<bank>.jsonl  (one file per bank, one DocRecord row per
line). A legacy single data/manifest.jsonl is auto-split into per-bank files on
first use (see migrate_legacy_layout).

For HTML-sourced documents (e.g. ECB monetary policy accounts), the raw HTML
is ALWAYS preserved as `<doc_id>.html` (the source-of-truth); if `html_to_pdf`
is enabled and Chrome rendering succeeds, a `<doc_id>.pdf` is produced
alongside it and becomes the canonical artifact (`local_path`, `mime_type`).
The `html_path` field on the record points to the HTML sibling so downstream
consumers can fall back if the PDF rendering is missing or unsatisfactory.

No domain guard: the discovery layer is responsible for handing us URLs we
want. Dedup is enforced by doc_id (stable hash of bank+type+date+url) and by
content sha256.
"""
from __future__ import annotations

import atexit
import contextlib
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional

from .config import Config
from .http import Fetcher
from .htmlpdf import render_allowed, render_url_to_pdf
from .models import DocRecord
from .quarantine import Quarantine


@contextlib.contextmanager
def _locked(data_file: Path):
    """Exclusive flock on the SIDECAR lock file of `data_file`.

    The data file's inode changes on every rewrite (temp + os.replace), so a
    flock on the data file itself cannot give mutual exclusion between
    appenders and rewriters — an appender can hold a lock on the OLD inode
    while a rewriter swaps in a new one, orphaning the appended bytes
    (issue #18). The sidecar (`.<name>.lock`, created on demand, never
    replaced or deleted) has a stable inode. Every manifest write path —
    _append(), _repair_torn_tail(), apply_row_updates() — takes this lock.
    """
    lock = data_file.with_name("." + data_file.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


_EXT_FOR_MIME = {
    "application/pdf": "pdf",
    "text/html": "html",
    "application/xhtml+xml": "html",
}


def ext_for_mime(mime: str) -> str:
    return _EXT_FOR_MIME.get((mime or "").lower(), "bin")


_MULTI_SLASH = re.compile(r"/{2,}")


def normalize_url(url: str) -> str:
    """Canonical form for the known-URL indexes: duplicate slashes in the
    path collapsed to one (`a.eu//press` == `a.eu/press` — the one variant
    class observed in production, ECB legacy rows, PR #11). The scheme
    separator is preserved. Deliberately nothing else: trailing slashes,
    http vs https, `www.`, query order and case can all change what a
    server returns, so they stay significant (spec
    2026-08-25-url-normalization-index-design). Pure and total: returns
    its input shape for empty/scheme-less strings, never raises.

    Not to be confused with the migrations' fuzzy matchers
    (`wp_migrate.match_key_url`, `c2_migrate.match_key_url`), which drop
    scheme/case/`~hash` to JOIN records -- a different question."""
    if not url:
        return url
    scheme, sep, rest = url.partition("://")
    if not sep:
        return _MULTI_SLASH.sub("/", url)
    return scheme + sep + _MULTI_SLASH.sub("/", rest)


# -- per-bank manifest IO (module-level so non-Storage callers can reuse) ------
def _manifest_files(cfg: Config, bank_code: Optional[str] = None) -> list[Path]:
    """Per-bank manifest file(s) to read. One bank if `bank_code` given, else all
    `data/manifest/*.jsonl`. Falls back to the legacy single file for pre-split repos."""
    if bank_code is not None:
        return [cfg.manifest_file(bank_code)]
    files = sorted(cfg.manifest_dir.glob("*.jsonl")) if cfg.manifest_dir.is_dir() else []
    if files:
        return files
    return [cfg.manifest_path] if cfg.manifest_path.exists() else []


def _repair_torn_tail(f: Path, offset: int, *, expected_ino: int,
                      expected_size: int) -> list[dict]:
    """A malformed FINAL line, as seen by the caller's UNLOCKED first read, is
    presumed to be a torn append (SIGKILL/ENOSPC mid-write). But `_append()`
    (another process, same or different bank writer) or a concurrent repair
    could have completed or extended that very tail since that unlocked read
    — so before touching the file we take the bank's exclusive SIDECAR lock
    (see _locked — a flock on the data file itself is unsound across
    os.replace) and RE-READ the tail (from `offset` to current EOF) under the
    lock.

    Stale-offset guard: `offset` was computed against the file AS READ by the
    caller, identified by `expected_ino`/`expected_size` (captured at that
    unlocked read). A concurrent `apply_row_updates()` may have os.replace'd
    the file in the meantime — new inode, shifted byte offsets — so a stale
    `offset` can point MID-LINE into the new file, and truncating there would
    destroy valid rows. Therefore, after acquiring the lock and opening the
    file, we validate BEFORE any truncation decision:

      - the open fd's inode must equal `expected_ino`. If not, the file was
        rewritten by a locked rewriter, which already dropped/handled any
        torn tail -> return [] WITHOUT touching the file (the caller keeps
        the prefix rows it already read; the fresh state is seen on the next
        read — same philosophy as the "race with an in-flight writer" case
        below).
      - `offset` must lie within both `expected_size` and the current size,
        and (when > 0) sit on a line boundary: the byte at `offset-1` must
        be a newline. If not, the offset is stale -> return [] without
        touching the file.

    When the guards pass, the under-lock tail re-read proceeds:

      - if every line in the freshly re-read tail now parses as valid JSON,
        the earlier "torn" observation was just a race with an in-flight
        writer, not real corruption -> no truncation, the now-complete row(s)
        are returned so the caller doesn't lose them.
      - if a line in the re-read tail is still malformed, THAT is the real
        torn append: save the raw fragment to `<file>.torn` for forensics,
        then atomically truncate the manifest back to the last complete line.
        Truncating a *tail* needs no temp-file rewrite; the intact prefix is
        untouched on disk. Any complete rows preceding the still-torn line
        within the re-read tail are returned too.

    Order matters for the repair itself: the fragment is written FIRST, so a
    crash between the two steps never loses data — worst case the next run
    re-detects the same torn tail (torn_path.write_bytes is idempotent/
    overwriting). Holding the sidecar lock for the whole re-read+repair also
    blocks a concurrent `_append()` and apply_row_updates() (which take the
    same lock) from writing into the file while we decide/truncate.

    The lock only serializes the truncate DECISION, not the writes that can
    land in the caller's earlier unlocked-read window: an `_append()` that
    interleaves its bytes into the torn tail during that window can produce a
    garbled merged tail that still fails to parse under the lock; that tail
    is truncated like any other real tear, and the row(s) it carried
    reconverge on the next run via stable-key dedup, not via this repair.
    """
    rows: list[dict] = []
    with _locked(f), f.open("r+b") as fh:
        st = os.fstat(fh.fileno())
        if st.st_ino != expected_ino:
            return rows            # file rewritten (os.replace) since our read
        if offset > expected_size or offset > st.st_size:
            return rows            # stale offset: beyond the file we can trust
        if offset > 0:
            fh.seek(offset - 1)
            if fh.read(1) != b"\n":
                return rows        # stale offset: not on a line boundary
        fh.seek(offset)
        tail = fh.read()
        local_offset = offset
        for ln in tail.splitlines(keepends=True):
            stripped = ln.strip()
            if not stripped:
                local_offset += len(ln)
                continue
            try:
                rows.append(json.loads(stripped))
            except (json.JSONDecodeError, UnicodeDecodeError):
                torn_path = f.with_name(f.name + ".torn")
                torn_path.write_bytes(ln)
                fh.truncate(local_offset)
                print(f"[storage] WARNING: torn manifest tail repaired: {f} "
                      f"truncated at byte offset {local_offset} "
                      f"(fragment saved to {torn_path})",
                      file=sys.stderr, flush=True)
                return rows
            local_offset += len(ln)
    return rows


def _iter_manifest_file(f: Path) -> Iterator[dict]:
    """Yield rows from one manifest file.

    A malformed FINAL (non-blank) line is treated as a torn append and
    repaired in place (see `_repair_torn_tail`) — under a lock it is re-read
    and either truly repaired (row dropped) or found to have been completed
    by a concurrent writer in the meantime (row kept), loading continues
    either way. A malformed line that is NOT final is real corruption: raises
    ValueError naming the file and 1-based line number (hard stop). Blank
    lines are skipped. An intact file is only ever read, never rewritten.

    "Malformed" covers both a structurally broken JSON line
    (`json.JSONDecodeError`) and a line torn mid multi-byte UTF-8 character
    (`UnicodeDecodeError` — `json.loads` decodes `bytes` internally before
    parsing); both are torn-append symptoms and are handled identically.
    """
    # Capture the file's identity (inode, size) on the very fd the bytes are
    # read from: _repair_torn_tail uses it to detect that an os.replace by a
    # locked rewriter landed between this unlocked read and the repair lock,
    # in which case a byte offset computed here would be stale (see its
    # stale-offset guard).
    with f.open("rb") as _fh:
        st = os.fstat(_fh.fileno())
        raw = _fh.read()
    if not raw:
        return
    lines = raw.splitlines(keepends=True)
    last_content_idx = None
    for i, ln in enumerate(lines):
        if ln.strip():
            last_content_idx = i
    if last_content_idx is None:
        return  # blank/whitespace-only file -> zero rows, nothing to repair

    rows: list[dict] = []
    offset = 0
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if not stripped:
            offset += len(ln)
            continue
        try:
            rows.append(json.loads(stripped))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if i == last_content_idx:
                rows.extend(_repair_torn_tail(
                    f, offset,
                    expected_ino=st.st_ino, expected_size=st.st_size))
                break
            raise ValueError(
                f"corrupt manifest line (not the final line -> not a torn "
                f"append, hard stop): {f}:{i + 1}: {exc}"
            ) from exc
        offset += len(ln)
    yield from rows


def iter_manifest_rows(cfg: Config, bank_code: Optional[str] = None) -> Iterator[dict]:
    """Yield manifest rows across per-bank files (or one bank)."""
    for f in _manifest_files(cfg, bank_code):
        if not f.exists():
            continue
        yield from _iter_manifest_file(f)


def migrate_legacy_layout(cfg: Config) -> int:
    """One-time: split a legacy single `data/manifest.jsonl` into per-bank files.

    No-op when there is no legacy file or per-bank files already exist. Preserves
    each row's exact bytes (no reserialization), then retires the legacy file to
    `*.pre-split.bak` so it is not double-read. Returns #banks written (0 = no-op).
    """
    legacy = cfg.manifest_path
    if not legacy.exists():
        return 0
    if cfg.manifest_dir.is_dir() and any(cfg.manifest_dir.glob("*.jsonl")):
        return 0
    by_bank: dict[str, list[str]] = {}
    with legacy.open() as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            bank = json.loads(s).get("bank_code") or "_unknown"
            by_bank.setdefault(bank, []).append(s)
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    for bank, lines in by_bank.items():
        path = cfg.manifest_file(bank)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, path)
    os.replace(legacy, legacy.with_suffix(".jsonl.pre-split.bak"))
    print(f"[storage] split legacy manifest into {len(by_bank)} per-bank file(s) "
          f"under {cfg.manifest_dir}", file=sys.stderr, flush=True)
    return len(by_bank)


def _write_per_bank_unlocked(cfg: Config, rows: Iterable[dict]) -> int:
    """UNLOCKED full-file writer — callers MUST either hold the sidecar lock
    (apply_row_updates) or be single-writer by construction (migrate_legacy_layout,
    test fixtures). Everything else goes through apply_row_updates.

    Atomically (re)write the per-bank files for every bank present in `rows`
    (temp file + os.replace per bank). Returns the number of rows written."""
    migrate_legacy_layout(cfg)               # never let a legacy file shadow a write
    by_bank: dict[str, list[dict]] = {}
    n = 0
    for row in rows:
        by_bank.setdefault(row.get("bank_code") or "_unknown", []).append(row)
        n += 1
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    for bank, brows in by_bank.items():
        path = cfg.manifest_file(bank)
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w") as fh:
            for row in brows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    return n


def _read_rows_for_rewrite(f: Path) -> list[tuple[str, Optional[str]]]:
    """Fresh under-lock read for apply_row_updates: (raw_line, doc_id) pairs.

    Raw lines (newline stripped) let untouched rows be rewritten
    byte-identically — no parse/re-serialize round trip. MUST be called with
    the bank's sidecar lock held; deliberately does NOT delegate to
    _iter_manifest_file: its torn-tail repair takes the same sidecar lock,
    and a second flock on another fd of the same process self-deadlocks.
    A malformed FINAL line is a torn append: the fragment is saved to
    `<file>.torn` and the line dropped — the caller's full rewrite replaces
    the file, which IS the truncation. Exception: when the file contains
    ONLY the torn line, no pairs remain, apply_row_updates skips the rewrite
    (`if pairs:`) and the torn line stays on disk — the next reader's
    torn-tail repair handles it. A malformed NON-final line raises
    ValueError (real corruption, hard stop — same policy as
    _iter_manifest_file).
    """
    out: list[tuple[str, Optional[str]]] = []
    if not f.exists():
        return out
    raw = f.read_bytes()
    lines = raw.splitlines(keepends=True)
    last_content_idx = None
    for i, ln in enumerate(lines):
        if ln.strip():
            last_content_idx = i
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if i == last_content_idx:
                torn_path = f.with_name(f.name + ".torn")
                torn_path.write_bytes(ln)
                print(f"[storage] WARNING: torn manifest tail dropped during "
                      f"rewrite: {f} (fragment saved to {torn_path})",
                      file=sys.stderr, flush=True)
                break
            raise ValueError(
                f"corrupt manifest line (not the final line -> not a torn "
                f"append, hard stop): {f}:{i + 1}: {exc}") from exc
        out.append((stripped.decode("utf-8"), row.get("doc_id")))
    return out


def apply_row_updates(cfg: Config, updates: Mapping[str, dict]) -> int:
    """Lock-protected keyed rewrite of manifest rows (issue #18).

    `updates`: doc_id -> full replacement row (row's `bank_code` selects the
    bank file). Per bank, under the sidecar lock: re-read fresh, replace rows
    whose doc_id is in `updates`, keep every other line VERBATIM — including
    rows appended after the caller built its snapshot — then temp+os.replace.
    Expensive work (renders, network) belongs BEFORE this call, outside the
    lock. Deletions are not supported (no caller deletes; future deletes need
    explicit tombstones). doc_ids not found on disk are skipped with a
    warning. Banks are processed sequentially (one lock at a time); a crash
    between banks leaves a partial, idempotent state. Returns the number of
    rows replaced.
    """
    if not updates:
        return 0
    by_bank: dict[str, dict[str, dict]] = {}
    for did, row in updates.items():
        bank = row.get("bank_code") or "_unknown"
        by_bank.setdefault(bank, {})[did] = row
    migrate_legacy_layout(cfg)           # never let a legacy file shadow a write
    replaced = 0
    for bank in sorted(by_bank):
        bank_updates = by_bank[bank]
        path = cfg.manifest_file(bank)
        seen: set[str] = set()
        with _locked(path):
            pairs = _read_rows_for_rewrite(path)
            if pairs:
                tmp = path.with_suffix(".jsonl.tmp")
                with tmp.open("w") as fh:
                    for raw, did in pairs:
                        upd = bank_updates.get(did) if did else None
                        if upd is not None:
                            fh.write(json.dumps(upd, ensure_ascii=False) + "\n")
                            seen.add(did)
                            replaced += 1
                        else:
                            fh.write(raw + "\n")
                os.replace(tmp, path)
        for did in sorted(bank_updates.keys() - seen):
            print(f"[storage] WARNING: apply_row_updates: doc_id {did!r} not "
                  f"found in {path.name}; update skipped",
                  file=sys.stderr, flush=True)
    return replaced


def _sweep_chrome_profiles(data_dir: Path, keep: Path) -> None:
    """Remove leftover per-PID Chrome profiles whose process is gone.

    Profiles are named `.chrome-profile-<pid>`; a crashed/killed run leaves its
    profile behind, so on startup we drop any whose PID is no longer alive
    (best-effort, never raises). `keep` (our own profile) is preserved.
    """
    try:
        candidates = list(data_dir.glob(".chrome-profile-*"))
    except OSError:
        return
    for p in candidates:
        if p == keep:
            continue
        try:
            pid = int(p.name.rsplit("-", 1)[-1])
        except ValueError:
            continue
        try:
            os.kill(pid, 0)          # raises if the PID is not alive
        except OSError:
            shutil.rmtree(p, ignore_errors=True)


class Storage:
    # Name resolver handed to htmlpdf.render_allowed before every render
    # (None = socket.getaddrinfo). A class attribute so the test suite can
    # swap in a no-DNS default for every Storage (tests/conftest.py); a test
    # injects its own fake per instance.
    render_resolver = None

    def __init__(self, config: Optional[Config] = None,
                 fetcher: Optional[Fetcher] = None):
        self.cfg = config or Config()
        self.fetcher = fetcher or Fetcher(self.cfg)
        self.cfg.raw_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        # Dead-letter state for the nightly sync loop (recover-quarantine
        # design §3): consulted at the top of save() before any fetch, fed
        # from _record_download_error() and save()'s success path. Quarantine
        # writes must only ever happen from THIS serialized loop (never
        # parallel workers) — see quarantine.py's _append docstring.
        self.quarantine = Quarantine(self.cfg)
        # The quarantine "night" of this run (B7, grit wave 2026-11): the
        # LOCAL date (TZ=Europe/Paris in the stack), computed ONCE here so a
        # run that crosses midnight never counts as two failing nights. It
        # used to be the UTC date at each failure, which split a Paris run
        # starting before 01:00 across two nights.
        self._night = _dt.date.today().isoformat()
        # Render guard (B8): renders refused because the URL points at a
        # blocked network (resolver: the `render_resolver` class attribute).
        self.render_refused = 0
        # sha256 -> doc_id of the row that owns that content, not just a
        # membership set: a same-bytes save under a different doc_id needs to
        # report WHICH existing doc_id matched (`skip:duplicate-content:<id>`)
        # so a caller (recover.py) can stamp the dead URL onto the right row.
        self._hash_docid: dict[str, str] = {}
        self._ids: set[str] = set()
        self._urls: set[str] = set()
        self._source_urls: set[str] = set()
        # Duplicate-content stamps queued by save()/reindex() (B1, grit wave
        # 2026-11): owner doc_id -> ordered URLs to add to that row's
        # `alt_urls` / `alt_source_urls`. Flushed in ONE locked rewrite by
        # flush_duplicate_stamps() (end of save_many, in a finally). Ordered
        # lists, not sets: alt_urls append order must not vary across runs.
        self._pending_dup_stamps: dict[str, list[str]] = {}
        self._pending_dup_sources: dict[str, list[str]] = {}
        self._dup_count = 0
        # Chrome profile reused across every render in THIS process (a fresh
        # per-call temp profile is ~10x slower). Keyed by PID so multiple
        # concurrent download processes don't fight over one profile lock —
        # this lets HTML→PDF renders run in parallel. Lives under data/
        # (git-ignored). Purged on exit (atexit) and stale ones swept at start,
        # so the per-PID dirs don't accumulate (they can reach hundreds of MB).
        self._chrome_profile = self.cfg.data_dir / f".chrome-profile-{os.getpid()}"
        _sweep_chrome_profiles(self.cfg.data_dir, keep=self._chrome_profile)
        atexit.register(shutil.rmtree, self._chrome_profile, ignore_errors=True)
        migrate_legacy_layout(self.cfg)      # split a pre-existing single manifest
        self._load_existing()

    # -- manifest --------------------------------------------------------
    def _load_existing(self) -> None:
        for rec in self.iter_manifest():
            self._ids.add(rec["doc_id"])
            if rec.get("sha256"):
                self._hash_docid[rec["sha256"]] = rec["doc_id"]
            url = rec.get("pdf_url")
            if url:
                self._urls.add(normalize_url(url))
            # Persisted alt_urls (WP v3): the same paper may have been registered
            # under an alternate URL (a native scraper URL stamped onto a row first
            # ingested via RePEc, or an EconStor/SSRN fallback). Index them so
            # is_known_url() recognises that URL too — this is what stops a native
            # scraper from re-downloading a paper it finds under a different URL.
            for alt in rec.get("alt_urls") or []:
                if alt:
                    self._urls.add(normalize_url(alt))
            src = rec.get("source_url")
            if src:
                self._source_urls.add(normalize_url(src))
            # Optional row field (B1, grit wave 2026-11): source pages of
            # records whose content turned out to duplicate this row. Indexed
            # so is_known_source_url() (RePEc's pre-fetch skip) learns them.
            for alt_src in rec.get("alt_source_urls") or []:
                if alt_src:
                    self._source_urls.add(normalize_url(alt_src))

    def is_known_url(self, url: str) -> bool:
        """True if a record with this pdf_url is already in the manifest
        (index-side normalized, see normalize_url — duplicate-slash variants
        of a known URL count as known too)."""
        return normalize_url(url) in self._urls

    def is_known_source_url(self, url: str) -> bool:
        """True if a record with this source_url is already in the manifest.

        Own index, deliberately separate from is_known_url(): source pages
        (e.g. IDEAS paper pages) identify a listing entry BEFORE its PDF is
        known — used by incremental catalog walks to skip the per-item fetch.
        Index-side normalized like is_known_url().
        """
        return normalize_url(url) in self._source_urls

    def iter_manifest(self, bank_code: Optional[str] = None) -> Iterator[dict]:
        """All manifest rows across per-bank files, or just one bank's."""
        yield from iter_manifest_rows(self.cfg, bank_code)

    def _append(self, rec: DocRecord) -> None:
        path = self.cfg.manifest_file(rec.bank_code)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Sidecar lock (stable inode): mutual exclusion with (a) another
        # process's _append() on this bank, (b) the torn-tail repair path,
        # and (c) apply_row_updates()'s read-merge-replace. A flock on the
        # data file itself would be unsound — os.replace swaps its inode.
        with _locked(path):
            with path.open("a") as fh:
                fh.write(json.dumps(rec.to_row(), ensure_ascii=False) + "\n")

    def rewrite_manifest(self, updates: Mapping[str, dict]) -> int:
        """Apply keyed row updates under the per-bank sidecar lock (see
        apply_row_updates) and refresh the in-memory dedup indexes so a
        long-lived Storage stays consistent with disk. `updates`:
        doc_id -> full replacement row. Rows not in `updates` — including
        rows appended concurrently — are preserved verbatim. Returns the
        number of rows replaced.
        """
        n = apply_row_updates(self.cfg, updates)
        self._ids.clear(); self._hash_docid.clear(); self._urls.clear()
        self._source_urls.clear()
        self._load_existing()
        return n

    def stamp_alt_urls(self, stamps: Mapping[str, Iterable[str]],
                       source_stamps: Optional[Mapping[str, Iterable[str]]] = None,
                       ) -> tuple[int, int]:
        """Add URLs to the `alt_urls` of already-indexed rows, by doc_id.

        `stamps`: doc_id -> URLs to add (recover.py's duplicate-path
        self-healing: a dead URL whose content/doc_id matched an existing
        row gets stamped onto THAT row so `_skip_known_url`/`_is_converged`
        recognise it forever). For each row whose doc_id appears in
        `stamps`, every URL that is neither the row's own `pdf_url` nor
        already present in its `alt_urls` is appended -- this only ADDS
        alt_urls, never touches `pdf_url`/`doc_id` (dedup identity is never
        rewritten). Unknown doc_ids are silently ignored (defensive; the
        caller logs totals, this method doesn't need to explain a no-match).

        `source_stamps` (optional, same shape): source-page URLs to add to
        the row's optional `alt_source_urls` field, with the same add-only
        semantics (a URL equal to the row's own `source_url` or already
        listed is skipped; the field is only created when something is
        added). Used by the duplicate-content bookkeeping of save_many (see
        flush_duplicate_stamps) so `is_known_source_url` learns the source
        page of a record whose bytes another row already owns.

        Rows are found across EVERY bank's manifest (the owner of a
        duplicate may live in another bank's file); apply_row_updates
        rewrites each row in its own bank file.

        Only rows that actually changed are rewritten, in ONE
        rewrite_manifest call (lock-protected keyed updates). Returns
        (urls_stamped, rows_modified) -- urls_stamped counts alt_urls and
        alt_source_urls additions together; (0, 0) means nothing changed and
        no rewrite happened at all.
        """
        source_stamps = source_stamps or {}
        if not stamps and not source_stamps:
            return (0, 0)
        updates: dict[str, dict] = {}
        stamped = 0
        for row in self.iter_manifest():
            did = row.get("doc_id")
            urls = stamps.get(did) or []
            srcs = source_stamps.get(did) or []
            if not urls and not srcs:
                continue
            changed = False
            pdf_url = row.get("pdf_url")
            alt_urls = list(row.get("alt_urls") or [])
            for url in urls:
                if not url or url == pdf_url or url in alt_urls:
                    continue
                alt_urls.append(url)
                stamped += 1
            if alt_urls != (row.get("alt_urls") or []):
                row["alt_urls"] = alt_urls
                changed = True
            own_src = row.get("source_url")
            alt_srcs = list(row.get("alt_source_urls") or [])
            for url in srcs:
                if not url or url == own_src or url in alt_srcs:
                    continue
                alt_srcs.append(url)
                stamped += 1
            if alt_srcs != (row.get("alt_source_urls") or []):
                row["alt_source_urls"] = alt_srcs
                changed = True
            if changed:
                updates[row["doc_id"]] = row
        if stamped == 0:
            return (0, 0)
        self.rewrite_manifest(updates)
        return (stamped, len(updates))

    def _note_duplicate(self, rec: DocRecord, owner_doc_id: str) -> None:
        """Remember a `skip:duplicate-content` record (B1, grit wave 2026-11).

        Without this, a URL whose bytes another row already owns is
        re-downloaded every night just to be discarded again. Its `pdf_url`
        (and `source_url`) are added to the in-memory indexes at once, and
        queued for stamping onto the OWNER row (`alt_urls` /
        `alt_source_urls`) by flush_duplicate_stamps(). Accepted side
        effect: the stamped URL becomes a fallback download URL for the
        owner row. Never called in dry run (save()/reindex() return before
        any duplicate detection there)."""
        self._dup_count += 1
        self._urls.add(normalize_url(rec.pdf_url))
        bucket = self._pending_dup_stamps.setdefault(owner_doc_id, [])
        if rec.pdf_url and rec.pdf_url not in bucket:
            bucket.append(rec.pdf_url)
        if rec.source_url:
            self._source_urls.add(normalize_url(rec.source_url))
            sbucket = self._pending_dup_sources.setdefault(owner_doc_id, [])
            if rec.source_url not in sbucket:
                sbucket.append(rec.source_url)

    def flush_duplicate_stamps(self, label: str = "") -> tuple[int, int, int]:
        """Write the queued duplicate-content stamps in ONE locked rewrite
        (stamp_alt_urls, add-only) and print one stderr line
        `[label] duplicate-content: N (stamped U urls on R rows)` when any
        duplicate was seen since the last flush. Clears the queue and the
        counter. Returns (duplicates, urls_stamped, rows_modified).

        Called by save_many() in a finally; callers driving save()/reindex()
        directly (the reindex_* entry points) call it themselves."""
        n = self._dup_count
        stamps, sources = self._pending_dup_stamps, self._pending_dup_sources
        self._pending_dup_stamps, self._pending_dup_sources = {}, {}
        self._dup_count = 0
        urls = rows = 0
        if stamps or sources:
            urls, rows = self.stamp_alt_urls(stamps, source_stamps=sources)
        if n:
            prefix = f"[{label}] " if label else ""
            print(f"{prefix}duplicate-content: {n} (stamped {urls} urls on {rows} rows)",
                  file=sys.stderr, flush=True)
        return (n, urls, rows)

    # -- paths -----------------------------------------------------------
    def target_path(self, rec: DocRecord) -> Path:
        year = rec.year or 0
        ext = ext_for_mime(rec.mime_type) if rec.mime_type else (
            "pdf" if rec.pdf_url.lower().endswith(".pdf") else "html"
        )
        return (self.cfg.raw_dir / rec.bank_code / rec.doc_type.code
                / str(year) / f"{rec.doc_id}.{ext}")

    # -- download --------------------------------------------------------
    def save(self, rec: DocRecord, *, dry_run: bool = False,
             bypass_quarantine: bool = False) -> str:
        # Consult the quarantine BEFORE any network activity — a URL that has
        # failed QUARANTINE_AFTER_NIGHTS distinct nights running is skipped by
        # the bounded (Mon-Sat) sync entirely (Sunday full sweep bypasses via
        # QUARANTINE_RETRY=1, handled inside is_quarantined()). `bypass_quarantine`
        # is for recovery flows (recover-downloads --download): their whole
        # inventory comes FROM download_errors.jsonl, the same file that feeds
        # the quarantine counter, so by the time recovery runs its own targets
        # are typically already quarantined. When True, is_quarantined() is not
        # even called — the gate's state (skipped_count, on-disk file) is left
        # completely untouched by the bypass itself; a subsequent success still
        # releases the quarantine normally via record_success() below.
        if not bypass_quarantine and self.quarantine.is_quarantined(rec.pdf_url):
            return "skip:quarantined"
        if rec.doc_id in self._ids:
            return "skip:already-indexed"
        if dry_run:
            # In-memory only: dedup within this pass for accurate counts, but
            # NEVER write to the manifest. A persisted dry-run row (local_path
            # =null, sha256=null) would be re-loaded by the next real run and
            # make save() skip it as "already-indexed" — the document would
            # then never be downloaded. (This is exactly how a 155-row
            # placeholder pollution happened once.)
            self._ids.add(rec.doc_id)
            self._urls.add(normalize_url(rec.pdf_url))
            if rec.source_url:
                self._source_urls.add(normalize_url(rec.source_url))
            return "dry-run:indexed"

        # Try the preferred URL, then any fallback copies (EconStor/SSRN/cached)
        # so a 403/dead preferred host doesn't lose the document. doc_id stays
        # bound to rec.pdf_url, so dedup/identity are unaffected by which copy won.
        content = None
        mime = ""
        for url in [rec.pdf_url, *(rec.alt_urls or [])]:
            try:
                content, mime = self.fetcher.get_bytes(url)
                break
            except Exception:
                content = None
        if content is None:
            raise RuntimeError(
                f"all {1 + len(rec.alt_urls or [])} candidate URL(s) failed for {rec.pdf_url}")
        if mime:
            rec.mime_type = mime
        digest = hashlib.sha256(content).hexdigest()
        if digest in self._hash_docid:
            owner = self._hash_docid[digest]
            self._note_duplicate(rec, owner)
            return f"skip:duplicate-content:{owner}"

        is_html = mime.startswith("text/html") or mime.startswith("application/xhtml")
        if is_html:
            # Always preserve the source HTML — it's our fallback if the PDF
            # rendering is missing or unsatisfactory.
            rec.mime_type = "text/html"
            html_path = self.target_path(rec)
            html_path.parent.mkdir(parents=True, exist_ok=True)
            html_path.write_bytes(content)
            rec.html_path = str(html_path)

            refused = None
            if self.cfg.html_to_pdf:
                # Never point --no-sandbox Chrome at the private network
                # (Config.render_blocked_networks): refused -> keep the HTML.
                refused = render_allowed(rec.pdf_url, self.cfg,
                                         resolver=self.render_resolver)
                if refused:
                    self.render_refused += 1
                    print(f"[storage] render refused for {rec.pdf_url}: {refused} "
                          f"(HTML kept as the artifact)", file=sys.stderr, flush=True)
            if self.cfg.html_to_pdf and not refused:
                # Try to render a PDF sibling. If Chrome fails, the HTML still
                # remains and becomes the canonical artifact.
                pdf_rec = DocRecord(  # transient copy for path resolution
                    bank_code=rec.bank_code, doc_type=rec.doc_type, title=rec.title,
                    pdf_url=rec.pdf_url, date=rec.date,
                    mime_type="application/pdf",
                )
                # doc_id is derived from immutable fields, so same as rec.doc_id.
                pdf_path = self.target_path(pdf_rec)
                try:
                    # Chrome re-fetches the live URL; share the host's rate
                    # budget so this render + the get_bytes above don't burst.
                    self.fetcher.throttle(rec.pdf_url)
                    render_url_to_pdf(rec.pdf_url, pdf_path,
                                      user_data_dir=str(self._chrome_profile))
                    rec.mime_type = "application/pdf"
                    path = pdf_path
                except Exception:
                    # Render failed -> canonical artifact stays the HTML.
                    path = html_path
            else:
                path = html_path
        else:
            path = self.target_path(rec)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        rec.sha256 = digest
        rec.local_path = str(path)
        self._ids.add(rec.doc_id)
        self._hash_docid[digest] = rec.doc_id
        self._urls.add(normalize_url(rec.pdf_url))
        if rec.source_url:
            self._source_urls.add(normalize_url(rec.source_url))
        self._append(rec)
        self.quarantine.record_success(rec.pdf_url)
        return "saved"

    # -- reindex (no download) -------------------------------------------
    def reindex(self, rec: DocRecord, path: Path, *, dry_run: bool = False) -> str:
        """Index an already-downloaded file WITHOUT re-fetching it.

        For a document whose bytes are already on disk (at ``path``) but whose
        manifest row was lost — e.g. the manifest was reset while the downloads
        accumulated — recompute sha256 from the on-disk bytes, set
        ``local_path``/``mime_type``, and append the manifest row from the
        (re-discovered) record. Idempotent: skips a ``doc_id`` already indexed or
        whose content hash is already present. ``dry_run`` reports the action
        without writing (mirrors :meth:`save`'s no-write contract).
        """
        if rec.doc_id in self._ids:
            return "skip:already-indexed"
        if not path.is_file():
            return "skip:missing-file"
        if dry_run:
            return "dry-run:would-reindex"

        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest in self._hash_docid:
            owner = self._hash_docid[digest]
            self._note_duplicate(rec, owner)
            return f"skip:duplicate-content:{owner}"

        ext = path.suffix.lower().lstrip(".")
        rec.mime_type = {"pdf": "application/pdf", "html": "text/html"}.get(ext, rec.mime_type)
        if ext == "html":
            rec.html_path = str(path)
        rec.sha256 = digest
        rec.local_path = str(path)
        self._ids.add(rec.doc_id)
        self._hash_docid[digest] = rec.doc_id
        self._urls.add(normalize_url(rec.pdf_url))
        if rec.source_url:
            self._source_urls.add(normalize_url(rec.source_url))
        self._append(rec)
        # An externally-recovered doc registered here (no fetch at all) must
        # release its quarantine too — otherwise the nightly sync would keep
        # skipping a URL the corpus now actually has (recover-quarantine
        # design §3: any success releases the URL).
        self.quarantine.record_success(rec.pdf_url)
        return "reindexed"

    def _record_download_error(self, rec: DocRecord, exc: Exception, label: str) -> None:
        """Append one line to data/download_errors.jsonl (durable audit of every
        failed download — stdout scrolls away, this file doesn't). Append-only,
        O_APPEND line writes; never read by the crawler itself. Feeds the
        quarantine with this run's night (`self._night`, fixed at
        construction)."""
        entry = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "label": label,
            "bank_code": rec.bank_code,
            "doc_type": rec.doc_type.code,
            "title": rec.title,
            "pdf_url": rec.pdf_url,
            "alt_urls": rec.alt_urls or [],
            "source_url": rec.source_url,
            "error": f"{type(exc).__name__}: {exc}".replace("\n", " ")[:500],
        }
        path = self.cfg.data_dir / "download_errors.jsonl"
        with path.open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.quarantine.record_failure(rec.pdf_url, night=self._night)

    def save_many(self, recs: Iterable[DocRecord], *, dry_run: bool = False,
                  progress_every: int = 100, label: str = "") -> dict[str, int]:
        """Save each record, tallying `save()`'s status prefix (e.g. "saved",
        "skip", "error") and printing a periodic progress line plus one
        quarantine summary line at the end (via `Quarantine.summary_line()`,
        which CONSUMES/resets the skip counter -- so calling `save_many()`
        again on this same `Storage` afterwards, e.g. for the next bank in a
        multi-bank discovery run, reports THAT batch's own skip count, not a
        cumulative total across every batch run so far).

        Duplicate-content records seen during the batch are stamped onto the
        rows that own their content (flush_duplicate_stamps), in a finally so
        a record iterator that raises mid-batch still persists what was
        learnt; one `duplicate-content: N` stderr line when N > 0."""
        counts: dict[str, int] = {}
        total = 0
        try:
            for rec in recs:
                try:
                    status = self.save(rec, dry_run=dry_run).split(":")[0]
                except Exception as exc:
                    status = "error"
                    if not dry_run:
                        try:
                            self._record_download_error(rec, exc, label)
                        except Exception:
                            pass  # auditing must never break the crawl
                counts[status] = counts.get(status, 0) + 1
                total += 1
                if progress_every and total % progress_every == 0:
                    prefix = f"[{label}] " if label else ""
                    print(f"{prefix}processed {total} ({dict(counts)})", file=sys.stderr, flush=True)
        finally:
            try:
                self.flush_duplicate_stamps(label)
            except Exception as exc:  # never mask the batch's own outcome
                print(f"[storage] WARNING: duplicate-content stamping failed: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        summary = self.quarantine.summary_line()
        if summary:
            print(summary, file=sys.stderr, flush=True)
        return counts
