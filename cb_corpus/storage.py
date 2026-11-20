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
import datetime as _dt
# Not used directly any more (the lock lives in filelock.py), but kept as a
# module attribute: tests patch `storage.fcntl.flock` -- the same module
# object filelock.py calls -- to simulate lock-window races.
import fcntl  # noqa: F401
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Optional

from .config import Config
from .filelock import locked_sidecar
from .http import Fetcher
from .htmlpdf import RenderRefused, render_allowed, render_url_to_pdf
from .models import DocRecord, row_bank, row_url, upgrade_row
from .pdfcheck import check_pdf_bytes, has_pdf_magic
from .quarantine import Quarantine


class ContentCheckFailed(RuntimeError):
    """Every candidate URL of a record failed (issue #21): at least one
    answered HTTP 200 with bytes that failed the content check (HTML for an
    expected PDF, a body without the PDF shape -- see pdfcheck), and the
    others failed the content check too or could not be fetched at all.

    A RuntimeError on purpose: save()'s callers already route any exception
    to the download-error path (save_many -> download_errors.jsonl with the
    batch label, and the quarantine night count), so a rejected body is
    retried on later nights, then quarantined, exactly like a dead URL --
    never stored, never indexed, never a dedup owner. The message names
    every candidate URL with its reason."""


def _temp_sibling(path: Path) -> Path:
    """A fresh, unique temp path in `path`'s directory (`.<name>.<random>.part`),
    created empty. Same directory on purpose: os.replace is atomic only
    within one filesystem. Hidden and `.part`-suffixed so readers that glob
    raw/ for `*.pdf` / `*.html` (reindex) never pick up a half-written file;
    `sweep-orphans` walks every file, dotfiles included: it skips a recent
    temp and reports an old one (left by a killed process) as a stale temp
    (orphans.is_temp_part).

    Created with mode 0o666 through os.open, so the process umask applies
    and the final artifact (renamed from this file) gets the same mode a
    plain write would give (0644 under umask 022, 0664 under 002) --
    tempfile.mkstemp would force 0600 on every artifact."""
    for _ in range(100):
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.part")
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            continue
        os.close(fd)
        return tmp
    raise FileExistsError(f"no free temp name next to {path}")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` so that `path` holds either its previous
    content (or nothing) or ALL of `data` -- never a prefix (issue #21).

    A plain write_bytes killed mid-way (SIGKILL, ENOSPC, power loss) left a
    truncated artifact at the final path; the disaster-recovery reindex then
    indexed it and save()'s doc_id guard meant it was never retried. Here the
    bytes go to a temp sibling (_temp_sibling), are fsync'ed, then moved
    into place with os.replace (atomic on POSIX). On any failure the temp
    file is removed and the exception propagates (save() then fails like any
    download: download error, quarantine night, retried later). The parent
    directory itself is not fsync'ed: after a crash the rename may be lost,
    which leaves no file -- the safe outcome."""
    tmp = _temp_sibling(path)
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def pdf_file_problem(path: Path, cfg: Config) -> Optional[str]:
    """Why the on-disk file `path` is not an acceptable PDF (the same check
    as a download: pdfcheck.check_pdf_bytes with Config.min_pdf_bytes /
    pdf_require_eof), or None when it is. A missing or unreadable file is a
    problem too, never an exception."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return f"unreadable ({type(exc).__name__})"
    return check_pdf_bytes(data, min_bytes=cfg.min_pdf_bytes,
                           require_eof=cfg.pdf_require_eof)


def render_pdf_checked(pdf_path: Path, cfg: Config,
                       render: Callable[[Path], None], *, url: str,
                       label: str = "storage") -> None:
    """Produce `pdf_path` with `render` (one headless-Chrome run writing the
    path it is given), atomically and checked (issue #21, A3). The one
    render path of Storage.save(), convert-html and retry-html.

    `render` writes a temp sibling of `pdf_path` (_temp_sibling), never the
    final path: a Chrome killed or timed out mid-print used to leave a
    partial PDF there, which a later run then adopted. The output must pass
    the same PDF check as a download (pdfcheck.check_pdf_bytes with
    Config.min_pdf_bytes / pdf_require_eof -- a complete Chrome PDF ends
    with %%EOF) before it is fsync'ed and moved into place. Otherwise one
    `[<label>] render output rejected` line goes to stderr and a
    RuntimeError is raised. Whatever `render` raises (RenderRefused,
    subprocess errors, ...) propagates unchanged. The temp file is removed
    on every path."""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_sibling(pdf_path)
    try:
        render(tmp)
        problem = check_pdf_bytes(tmp.read_bytes(), min_bytes=cfg.min_pdf_bytes,
                                  require_eof=cfg.pdf_require_eof)
        if problem:
            print(f"[{label}] render output rejected for {url}: {problem} "
                  f"(HTML kept as the artifact)", file=sys.stderr, flush=True)
            raise RuntimeError(f"render output rejected for {url}: {problem}")
        with tmp.open("rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, pdf_path)
    finally:
        tmp.unlink(missing_ok=True)


# Storage.reindex() status for an on-disk .pdf that fails the PDF check
# (issue #21, A2). Tallied under its own key by status_key().
BAD_CONTENT = "skip:bad-content"


def status_key(status: str) -> str:
    """The tally key of a save()/reindex() status: its prefix ("saved",
    "skip", "dry-run", ...), except `skip:bad-content`, which keeps its own
    key "bad-content" -- a file that failed the content check is a damage
    signal, not a routine skip, and must stay visible in the counts."""
    if status == BAD_CONTENT:
        return "bad-content"
    return status.split(":")[0]


def _is_html_mime(mime: str) -> bool:
    m = (mime or "").lower()
    return m.startswith("text/html") or m.startswith("application/xhtml")


# Exclusive flock on the SIDECAR lock file of a manifest (see
# filelock.locked_sidecar for why a sidecar). Every manifest write path —
# _append(), _repair_torn_tail(), apply_row_updates(), merge_rows_locked(),
# remove_rows_locked() —
# takes this lock. Kept under its historical name: the lock moved to
# cb_corpus/filelock.py so quarantine.py can share it without importing this
# module.
_locked = locked_sidecar


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


class NotARow(ValueError):
    """A manifest line that is valid JSON but not an object (`[1, 2]`, `42`,
    `"text"`, `null`): malformed like a broken line -- a torn tail when it is
    the final line, a hard stop before the end."""


#: What a malformed manifest line raises from `_parse_row`.
MALFORMED = (json.JSONDecodeError, UnicodeDecodeError, NotARow)


def _parse_row(line: bytes) -> dict:
    """One manifest line -> its row; raises one of MALFORMED otherwise."""
    row = json.loads(line)
    if not isinstance(row, dict):
        raise NotARow(f"not a JSON object: {type(row).__name__}")
    return row


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
                rows.append(_parse_row(stripped))
            except MALFORMED:
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

    "Malformed" covers a structurally broken JSON line
    (`json.JSONDecodeError`), a line torn mid multi-byte UTF-8 character
    (`UnicodeDecodeError` — `json.loads` decodes `bytes` internally before
    parsing) and a JSON value that is not an object (`NotARow`: `[1, 2]`,
    `42`); all are handled identically (`_parse_row`, `MALFORMED`).
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
            rows.append(_parse_row(stripped))
        except MALFORMED as exc:
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
    # upgrade_row: TRANSITION (pdf_url -> document_url, see models.row_url):
    # a row read under the old key name is handed out under the new one, so
    # any rewrite built from it writes `document_url` only.
    for row in rows:
        yield upgrade_row(row)


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
            bank = row_bank(json.loads(s)) or "_unknown"
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
        by_bank.setdefault(row_bank(row) or "_unknown", []).append(row)
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
            row = _parse_row(stripped)
        except MALFORMED as exc:
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

    `updates`: doc_id -> full replacement row (row's `source_code`, or its
    pre-contract `bank_code`, selects the bank file; models.row_bank). Per
    bank, under the sidecar lock: re-read fresh, replace rows whose doc_id
    is in `updates`, keep every other line VERBATIM — including rows
    appended after the caller built its snapshot — then temp+os.replace.
    Expensive work (renders, network) belongs BEFORE this call, outside the
    lock. Deletions are not supported here: remove_rows_locked() is the one
    locked delete. doc_ids not found on disk are skipped with a
    warning. Banks are processed sequentially (one lock at a time); a crash
    between banks leaves a partial, idempotent state. Returns the number of
    rows replaced.
    """
    if not updates:
        return 0
    by_bank: dict[str, dict[str, dict]] = {}
    for did, row in updates.items():
        bank = row_bank(row) or "_unknown"
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
                            # transition (models.upgrade_row): never write
                            # the old `pdf_url` key name back.
                            upd = upgrade_row(dict(upd))
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


def is_stampable_owner(row: Mapping[str, object], cfg: Config) -> bool:
    """May a duplicate URL be persisted onto this owner row? The ONE
    eligibility rule for duplicate-content stamping (storage's nightly
    bookkeeping AND recover.py's self-healing).

    True only for a REAL PDF owner: `mime_type` application/pdf AND no
    `html_path`. A row rendered from HTML (Storage.save with html_to_pdf, or
    flipped later by convert-html / retry-html) also carries mime_type
    application/pdf, but its `sha256` is the HTML's: a page that can change
    while serving identical bytes today (a shared error page would absorb
    every URL serving it). `Config.stamp_duplicate_html=True` makes every
    owner eligible."""
    if cfg.stamp_duplicate_html:
        return True
    return (str(row.get("mime_type") or "").lower() == "application/pdf"
            and not row.get("html_path"))


def merge_rows_locked(cfg: Config, merges: Mapping[str, Mapping[str, object]]) -> int:
    """Read-merge-write of manifest rows INSIDE each bank's sidecar lock.

    `merges`: bank_code -> {doc_id: merge(row) -> bool}. Per bank, under the
    lock (same discipline as apply_row_updates): re-read the file fresh, hand
    each targeted row (a parsed dict) to its merge function, which mutates it
    in place and returns True when it changed it; every other line is kept
    VERBATIM. The file is rewritten (temp + os.replace) only when at least
    one row changed. Because the row is re-read under the lock, a change made
    to it by another writer after the caller's own snapshot is kept, not
    reverted -- the difference with apply_row_updates, which writes whole
    replacement rows built from a snapshot. Returns the number of rows
    changed."""
    migrate_legacy_layout(cfg)           # never let a legacy file shadow a write
    changed_total = 0
    for bank in sorted(merges):
        bank_merges = merges[bank]
        if not bank_merges:
            continue
        path = cfg.manifest_file(bank)
        with _locked(path):
            pairs = _read_rows_for_rewrite(path)
            out: list[str] = []
            changed = 0
            for raw, did in pairs:
                fn = bank_merges.get(did) if did else None
                if fn is not None:
                    row = upgrade_row(json.loads(raw))   # transition, see models
                    if fn(row):
                        out.append(json.dumps(row, ensure_ascii=False))
                        changed += 1
                        continue
                out.append(raw)
            if changed:
                tmp = path.with_suffix(".jsonl.tmp")
                with tmp.open("w") as fh:
                    for line in out:
                        fh.write(line + "\n")
                os.replace(tmp, path)
        changed_total += changed
    return changed_total


def remove_rows_locked(cfg: Config, doc_ids: Mapping[str, Iterable[str]]) -> int:
    """Remove manifest rows by doc_id, INSIDE each bank's sidecar lock.

    `doc_ids`: bank_code -> doc_ids to remove from that bank's manifest (the
    caller knows the bank; no scan of every file). Same discipline as
    merge_rows_locked: the file is re-read fresh under the lock, every other
    line is kept VERBATIM (rows appended after the caller's snapshot
    included), and it is rewritten (temp + os.replace) only when a row was
    actually removed. A doc_id absent from the file, or a bank without a
    manifest file, is a no-op, so a replay is idempotent. Returns the number
    of rows removed.

    No tombstone is written: the downstream vault ingest soft-deletes a row
    that no manifest carries any more (its `last_seen_at` stops moving).
    Callers keep their own audit trail (replace-artifact stamps the owner
    row's `retired_duplicates`)."""
    migrate_legacy_layout(cfg)           # never let a legacy file shadow a write
    removed_total = 0
    for bank in sorted(doc_ids):
        ids = {d for d in doc_ids[bank] if d}
        path = cfg.manifest_file(bank)
        if not ids or not path.exists():
            continue
        with _locked(path):
            pairs = _read_rows_for_rewrite(path)
            keep = [raw for raw, did in pairs if did not in ids]
            removed = len(pairs) - len(keep)
            if removed:
                tmp = path.with_suffix(".jsonl.tmp")
                with tmp.open("w") as fh:
                    for line in keep:
                        fh.write(line + "\n")
                os.replace(tmp, path)
        removed_total += removed
    return removed_total


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
    # injects its own fake per instance. NB: a plain function stored on the
    # CLASS must be wrapped in staticmethod(...), or attribute access through
    # an instance would bind it as a method (self passed as the host).
    render_resolver = None

    def __init__(self, config: Optional[Config] = None,
                 fetcher: Optional[Fetcher] = None):
        self.cfg = config or Config()
        self.fetcher = fetcher or Fetcher(self.cfg)
        self.cfg.raw_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.reports_dir.mkdir(parents=True, exist_ok=True)
        # Dead-letter state for the nightly sync loop (recover-quarantine
        # design §3): consulted at the top of save() before any fetch, fed
        # from _record_download_error() and save()'s success path. Parallel
        # discover workers each hold their own Quarantine on the one shared
        # file; its writes are serialized by a sidecar flock with catch-up
        # replay (issue #25) — see quarantine.py's module docstring.
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
        # Records whose save() raised ContentCheckFailed (issue #21): every
        # candidate URL served bytes that failed the content check (or could
        # not be fetched). save_many() prints the batch's own count.
        self.content_rejected = 0
        # sha256 -> doc_id of the row that owns that content, not just a
        # membership set: a same-bytes save under a different doc_id needs to
        # report WHICH existing doc_id matched (`skip:duplicate-content:<id>`)
        # so a caller (recover.py) can stamp the dead URL onto the right row.
        self._hash_docid: dict[str, str] = {}
        self._ids: set[str] = set()
        self._urls: set[str] = set()
        self._source_urls: set[str] = set()
        # doc_id -> bank_code of every indexed row (finds a duplicate's owner
        # file without scanning every manifest).
        self._bank_by_id: dict[str, str] = {}
        # Duplicate-content records queued by save()/reindex() (B1, grit wave
        # 2026-11), in arrival order (alt_urls append order must not vary
        # across runs): {url, source_url, owner_doc_id}. Persisted on the
        # owner row by flush_duplicate_stamps() (end of save_many, finally).
        self._pending_dups: list[dict] = []
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
            self._bank_by_id[rec["doc_id"]] = row_bank(rec) or "_unknown"
            if rec.get("sha256"):
                self._hash_docid[rec["sha256"]] = rec["doc_id"]
            url = row_url(rec)
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
        """True if a record with this document_url is already in the manifest
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
        self._source_urls.clear(); self._bank_by_id.clear()
        self._load_existing()
        return n

    def _bank_of(self, doc_ids: Iterable[str]) -> dict[str, str]:
        """doc_id -> bank_code for already-indexed rows (the in-memory map,
        falling back to one scan of every manifest for ids it lacks, e.g.
        rows appended by another process)."""
        out: dict[str, str] = {}
        missing: set[str] = set()
        for did in doc_ids:
            bank = self._bank_by_id.get(did)
            if bank:
                out[did] = bank
            else:
                missing.add(did)
        if missing:
            for row in self.iter_manifest():
                did = row.get("doc_id")
                if did in missing:
                    out[did] = row_bank(row) or "_unknown"
                    self._bank_by_id[did] = out[did]
        return out

    def _merge_stamps(self, stamps: Mapping[str, Iterable[str]],
                      source_stamps: Mapping[str, Iterable[str]],
                      eligible=None) -> tuple[int, int, set[str]]:
        """Add-only merge of `stamps` (into alt_urls) and `source_stamps`
        (into alt_source_urls), INSIDE each owner bank's manifest lock
        (merge_rows_locked): the owner row is re-read under the lock and
        merged, so a concurrent change to it is never reverted. `eligible`
        (row -> bool, optional) vetoes stamping a row. Comparisons use
        normalize_url: a candidate equal (after normalisation) to the row's
        document_url/alt_urls (or source_url/alt_source_urls) is not added.

        Returns (urls_stamped, rows_modified, persisted_owner_ids) -- the
        last being every owner row found AND eligible (its URLs are on the
        row afterwards, whether newly added or already there)."""
        owners = {d for d, u in stamps.items() if u} | {d for d, u in source_stamps.items() if u}
        if not owners:
            return (0, 0, set())
        banks = self._bank_of(owners)
        persisted: set[str] = set()
        added: dict[str, int] = {}

        def merger(did: str):
            urls = list(stamps.get(did) or [])
            srcs = list(source_stamps.get(did) or [])

            def merge(row: dict) -> bool:
                if eligible is not None and not eligible(row):
                    return False
                persisted.add(did)
                n = 0
                for field, own_key, cands in (("alt_urls", "document_url", urls),
                                              ("alt_source_urls", "source_url", srcs)):
                    current = list(row.get(field) or [])
                    seen = {normalize_url(u) for u in current if u}
                    if row.get(own_key):
                        seen.add(normalize_url(row[own_key]))
                    for url in cands:
                        key = normalize_url(url) if url else ""
                        if not url or key in seen:
                            continue
                        current.append(url)
                        seen.add(key)
                        n += 1
                    if current != (row.get(field) or []):
                        row[field] = current
                if n:
                    added[did] = n
                return n > 0
            return merge

        by_bank: dict[str, dict] = {}
        for did, bank in banks.items():
            by_bank.setdefault(bank, {})[did] = merger(did)
        merge_rows_locked(self.cfg, by_bank)
        # Keep the in-memory indexes in step (add-only: no reload, which
        # would drop this run's in-memory-only entries).
        for did in persisted:
            for u in stamps.get(did) or []:
                if u:
                    self._urls.add(normalize_url(u))
            for u in source_stamps.get(did) or []:
                if u:
                    self._source_urls.add(normalize_url(u))
        return (sum(added.values()), len(added), persisted)

    def stamp_alt_urls(self, stamps: Mapping[str, Iterable[str]],
                       source_stamps: Optional[Mapping[str, Iterable[str]]] = None,
                       ) -> tuple[int, int]:
        """Add URLs to the `alt_urls` of already-indexed rows, by doc_id.

        `stamps`: doc_id -> URLs to add (a URL whose content/doc_id matched an
        existing row gets stamped onto THAT row so `_skip_known_url` /
        `_is_converged` recognise it forever). Low-level and UNCONDITIONAL:
        production paths go through `stamp_duplicates()`, which applies
        `is_stampable_owner` and writes the audit log; this method is kept
        for direct/administrative use and tests. For each row whose doc_id appears in
        `stamps`, every URL that is neither the row's own `document_url` nor
        already present in its `alt_urls` (compared after normalize_url) is
        appended -- this only ADDS alt_urls, never touches `document_url`/`doc_id`
        (dedup identity is never rewritten). Unknown doc_ids are silently
        ignored (defensive; the caller logs totals).

        `source_stamps` (optional, same shape): source-page URLs to add to
        the row's optional `alt_source_urls` field, with the same add-only
        semantics against `source_url`/`alt_source_urls`; the field is only
        created when something is added. Used by the duplicate-content
        bookkeeping (flush_duplicate_stamps) so `is_known_source_url`
        learns the source page of a record whose bytes another row owns.

        Rows may live in ANY bank's manifest (a duplicate's owner can be in
        another bank's file). The merge happens under each bank's sidecar
        lock on a fresh re-read of the owner row (merge_rows_locked), so a
        concurrent change to that row is preserved, never reverted. A bank
        file is only rewritten when one of its rows actually changed.
        Returns (urls_stamped, rows_modified) -- urls_stamped counts
        alt_urls and alt_source_urls additions together; (0, 0) = nothing
        changed and nothing was rewritten.
        """
        urls, rows, _ = self._merge_stamps(stamps, source_stamps or {})
        return (urls, rows)

    def _note_duplicate(self, rec: DocRecord, owner_doc_id: str) -> None:
        """Remember a `skip:duplicate-content` record (B1, grit wave 2026-11).

        Without this, a URL whose bytes another row already owns is
        re-downloaded every night just to be discarded again. Its `document_url`
        (and `source_url`) are added to the in-memory indexes at once (this
        run never fetches it again), and queued for flush_duplicate_stamps(),
        which persists them on the OWNER row (`alt_urls` / `alt_source_urls`)
        only when that row is a real PDF (is_stampable_owner; see
        Config.stamp_duplicate_html).
        Accepted side effect of a persisted stamp: the URL becomes a fallback
        download URL for the owner row. Never called in dry run
        (save()/reindex() return before any duplicate detection there)."""
        self._urls.add(normalize_url(rec.document_url))
        if rec.source_url:
            self._source_urls.add(normalize_url(rec.source_url))
        self._pending_dups.append({"url": rec.document_url, "source_url": rec.source_url or "",
                                   "owner_doc_id": owner_doc_id})

    def discard_duplicate_stamps(self) -> int:
        """Drop the queued duplicate-content stamps without writing anything
        (dry runs; callers that must not persist). The in-memory index
        entries stay for this run. Returns how many were dropped."""
        n = len(self._pending_dups)
        self._pending_dups = []
        return n

    def flush_duplicate_stamps(self, label: str = "", *, dry_run: bool = False,
                               safe: bool = False) -> tuple[int, int, int]:
        """Persist the queued duplicate-content stamps and report them.

        Each queued duplicate is stamped onto its owner row (add-only, one
        locked merge per bank, see _merge_stamps) ONLY if the owner is a real
        PDF (is_stampable_owner: mime_type application/pdf and no html_path)
        -- or any owner when `Config.stamp_duplicate_html` is True. An HTML
        owner, including one rendered to PDF, keeps the URL in this run's
        in-memory indexes only, so it is retried next night as before (an
        HTML page can change while keeping identical bytes today). Every
        duplicate is logged as one JSON line in
        `<data_dir>/reports/duplicate_stamps.jsonl` (see stamp_duplicates;
        also written when the merge fails, with an `error`). One stderr line
        `[label] duplicate-content: N (stamped U urls on R rows)` when N > 0.
        The queue is cleared in every case (also on failure). Returns
        (duplicates, urls_stamped, rows_modified).

        `dry_run`: nothing is written, the queue is just discarded.
        `safe`: a failure while stamping is printed as a WARNING instead of
        raised -- for `finally` blocks, so it never replaces the caller's
        own exception. Called by save_many(); callers driving save()/
        reindex() directly (reindex_*, recover.py) call it themselves."""
        dups, self._pending_dups = self._pending_dups, []
        if dry_run or not dups:
            return (len(dups) if not dry_run else 0, 0, 0)
        try:
            return self._flush(dups, label)
        except Exception as exc:
            if not safe:
                raise
            print(f"[storage] WARNING: duplicate-content stamping failed: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return (len(dups), 0, 0)

    def stamp_duplicates(self, dups: list[dict], label: str, *,
                         stamp_sources: bool = True) -> tuple[int, int, set[str]]:
        """Stamp duplicate URLs onto their owner rows and log every one.

        `dups`: dicts {url, source_url, owner_doc_id}. Each URL (and source
        page) is merged onto its owner row (add-only, under the bank's lock,
        see _merge_stamps) only if the owner is_stampable_owner(); every dup
        is then appended as one JSON line to
        `<data_dir>/reports/duplicate_stamps.jsonl` (ts, url, source_url,
        owner_doc_id, owner_bank, label, persisted). If the merge raises, the
        lines are still written (persisted false, plus an `error` field)
        before the exception propagates. Shared by flush_duplicate_stamps()
        and recover.py. `stamp_sources=False` logs `source_url` but does not
        stamp it into `alt_source_urls` (recover.py: its self-healing has
        always stamped `alt_urls` only). Returns (urls_stamped, rows_modified,
        persisted_owner_ids)."""
        stamps: dict[str, list[str]] = {}
        sources: dict[str, list[str]] = {}
        for d in dups:
            src = d.get("source_url") if stamp_sources else ""
            for bucket, url in ((stamps, d.get("url")), (sources, src)):
                if url:
                    lst = bucket.setdefault(d["owner_doc_id"], [])
                    if url not in lst:
                        lst.append(url)
        error: Optional[str] = None
        persisted: set[str] = set()
        try:
            urls, rows, persisted = self._merge_stamps(
                stamps, sources, lambda row: is_stampable_owner(row, self.cfg))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}".replace("\n", " ")[:500]
            self._safe_log_duplicates(dups, label, persisted, error)
            raise
        self._safe_log_duplicates(dups, label, persisted, error)
        return (urls, rows, persisted)

    def _safe_log_duplicates(self, dups: list[dict], label: str,
                             persisted: set[str], error: Optional[str]) -> None:
        """`_log_duplicates`, but an unwritable log never fails the stamping:
        the merge result (already on disk) stays authoritative and a merge
        error is never masked by a log error. Warns on stderr instead."""
        try:
            self._log_duplicates(dups, label, persisted, error)
        except Exception as exc:  # noqa: BLE001 - audit must not break the crawl
            print(f"[{label}] WARNING: duplicate log not written: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    def _log_duplicates(self, dups: list[dict], label: str, persisted: set[str],
                        error: Optional[str]) -> None:
        try:
            banks = self._bank_of({d["owner_doc_id"] for d in dups})
        except Exception:     # the log must not depend on a readable manifest
            banks = {}
        ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        log = self.cfg.reports_dir / "duplicate_stamps.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as fh:
            for d in dups:
                entry = {
                    "ts": ts, "url": d.get("url") or "",
                    "source_url": d.get("source_url") or "",
                    "owner_doc_id": d["owner_doc_id"],
                    "owner_bank": banks.get(d["owner_doc_id"], ""),
                    "label": label, "persisted": d["owner_doc_id"] in persisted,
                }
                if error:
                    entry["error"] = error
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _flush(self, dups: list[dict], label: str) -> tuple[int, int, int]:
        urls, rows, _ = self.stamp_duplicates(dups, label)
        prefix = f"[{label}] " if label else ""
        print(f"{prefix}duplicate-content: {len(dups)} (stamped {urls} urls on {rows} rows)",
              file=sys.stderr, flush=True)
        return (len(dups), urls, rows)

    # -- paths -----------------------------------------------------------
    def target_path(self, rec: DocRecord) -> Path:
        year = rec.year or 0
        ext = ext_for_mime(rec.mime_type) if rec.mime_type else (
            "pdf" if rec.document_url.lower().endswith(".pdf") else "html"
        )
        return (self.cfg.raw_dir / rec.bank_code / rec.doc_type.code
                / str(year) / f"{rec.doc_id}.{ext}")

    # -- content check ---------------------------------------------------
    def _sniffed_mime(self, body: bytes, served_mime: str) -> str:
        """The mime to judge and store `body` under: "application/pdf" when
        the server labels it HTML but the bytes carry the PDF magic
        (pdfcheck.has_pdf_magic) -- the bytes win over the header. The full
        shape check is NOT applied here: _content_problem then judges the
        bytes as a PDF, so a mislabelled good PDF is stored as a .pdf, and a
        truncated or too-small one is refused for its real PDF reason
        (never stored and rendered as HTML, never reported as "the server
        sent HTML"), whatever the producer and Config.pdf_url_served_html.
        Here the body must START with the magic (after a byte-order mark or
        whitespace): an HTML page that merely mentions "%PDF" in its first KB
        (a title, a meta tag) stays HTML. The 1 KB tolerance of has_pdf_magic
        applies to bytes already judged a PDF, not to this sniff.
        Any other answer keeps `served_mime`."""
        head = body[:1024].lstrip(b"\xef\xbb\xbf \t\r\n\x00")
        if _is_html_mime(served_mime) and head.startswith(b"%PDF"):
            return "application/pdf"
        return served_mime

    def _content_problem(self, body: bytes, served_mime: str,
                         expected_mime: str) -> Optional[str]:
        """Why `body` (served with `served_mime`) must not be stored for a
        producer that expected `expected_mime`, or None when it may be.

        (1) Expected PDF (`expected_mime` application/pdf) but served HTML:
            refused when Config.pdf_url_served_html is "reject" (consent
            walls and error pages are HTML); "accept" lets it through to the
            HTML path (stored, rendered when html_to_pdf), the old behaviour.
        (2) Bytes that CLAIM to be a PDF -- the server says application/pdf,
            or it does not say HTML and the producer expects a PDF (an empty
            or octet-stream mime) -- must have a PDF's shape
            (pdfcheck.check_pdf_bytes with Config.min_pdf_bytes /
            pdf_require_eof). This also catches HTML bytes labelled
            application/pdf by the server, whatever the producer expected:
            they would otherwise be stored as a .pdf.
        HTML producers (`expected_mime` text/html or empty) served HTML are
        untouched: an HTML page is their document. save() first passes the
        server's mime through _sniffed_mime, so bytes carrying the PDF magic
        never reach (1) whatever their header says: (2) judges them."""
        expects_pdf = (expected_mime or "").lower() == "application/pdf"
        served = (served_mime or "").lower()
        if _is_html_mime(served):
            if expects_pdf and self.cfg.pdf_url_served_html == "reject":
                return (f"expected a PDF, the server sent HTML ({served}); "
                        f"see Config.pdf_url_served_html")
            return None
        if served == "application/pdf" or expects_pdf:
            return check_pdf_bytes(body, min_bytes=self.cfg.min_pdf_bytes,
                                   require_eof=self.cfg.pdf_require_eof)
        return None

    def _render_pdf(self, url: str, pdf_path: Path) -> None:
        """Render `url` with headless Chrome into `pdf_path` through
        render_pdf_checked (temp sibling, PDF check, atomic move; issue #21,
        A3). A rejected or failed render raises, so the caller keeps the
        HTML as the artifact (the existing fallback). RenderRefused (the
        private-network guard) propagates unchanged."""
        render_pdf_checked(
            pdf_path, self.cfg,
            lambda tmp: render_url_to_pdf(url, tmp,
                                          user_data_dir=str(self._chrome_profile),
                                          cfg=self.cfg, resolver=self.render_resolver),
            url=url)

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
        if not bypass_quarantine and self.quarantine.is_quarantined(rec.document_url):
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
            self._urls.add(normalize_url(rec.document_url))
            if rec.source_url:
                self._source_urls.add(normalize_url(rec.source_url))
            return "dry-run:indexed"

        # Try the preferred URL, then any fallback copies (EconStor/SSRN/cached)
        # so a 403/dead preferred host doesn't lose the document. doc_id stays
        # bound to rec.document_url, so dedup/identity are unaffected by which copy won.
        # Each candidate's bytes go through the content check (issue #21)
        # BEFORE the sha256 dedup below: an error page served with 200 must
        # never be stored, nor become the dedup owner that N other URLs
        # serving the same page would then be filed under. A failing
        # candidate is skipped like a failed fetch and the next one tried.
        # The producer's expectation is read BEFORE the server's mime
        # overwrites rec.mime_type.
        expected_mime = rec.mime_type
        content = None
        mime = ""
        failures: list[tuple[str, str]] = []   # (url, short reason)
        rejected = False
        for url in [rec.document_url, *(rec.alt_urls or [])]:
            try:
                body, served = self.fetcher.get_bytes(url)
            except Exception as exc:
                failures.append((url, f"fetch failed ({type(exc).__name__})"))
                continue
            served = self._sniffed_mime(body, served)
            problem = self._content_problem(body, served, expected_mime)
            if problem:
                rejected = True
                failures.append((url, problem))
                print(f"[storage] content check rejected {url}: {problem}",
                      file=sys.stderr, flush=True)
                continue
            content, mime = body, served
            break
        if content is None:
            n = 1 + len(rec.alt_urls or [])
            if rejected:
                self.content_rejected += 1
                # The distinct short reasons FIRST, the per-URL detail after:
                # download_errors.jsonl cuts `error` at 500 characters, and
                # a few long URLs must not push the reasons out of it.
                reasons = " | ".join(dict.fromkeys(r for _, r in failures))
                raise ContentCheckFailed(
                    f"content check failed: {reasons} -- {n} candidate URL(s): "
                    + "; ".join(f"{u}: {r}" for u, r in failures))
            raise RuntimeError(f"all {n} candidate URL(s) failed for {rec.document_url}")
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
            _atomic_write_bytes(html_path, content)
            rec.html_path = self.cfg.stored_path(html_path)

            refused = None
            if self.cfg.html_to_pdf:
                # Never point --no-sandbox Chrome at the private network
                # (Config.render_blocked_networks): refused -> keep the HTML.
                refused = render_allowed(rec.document_url, self.cfg,
                                         resolver=self.render_resolver)
                if refused:
                    self.render_refused += 1
                    print(f"[storage] render refused for {rec.document_url}: {refused} "
                          f"(HTML kept as the artifact)", file=sys.stderr, flush=True)
            if self.cfg.html_to_pdf and not refused:
                # Try to render a PDF sibling. If Chrome fails, the HTML still
                # remains and becomes the canonical artifact.
                pdf_rec = DocRecord(  # transient copy for path resolution
                    bank_code=rec.bank_code, doc_type=rec.doc_type, title=rec.title,
                    document_url=rec.document_url, date=rec.date,
                    mime_type="application/pdf",
                )
                # doc_id is derived from immutable fields, so same as rec.doc_id.
                pdf_path = self.target_path(pdf_rec)
                try:
                    # Chrome re-fetches the live URL; share the host's rate
                    # budget so this render + the get_bytes above don't burst.
                    self.fetcher.throttle(rec.document_url)
                    self._render_pdf(rec.document_url, pdf_path)
                    rec.mime_type = "application/pdf"
                    path = pdf_path
                except RenderRefused as exc:
                    # The choke-point guard disagreed with the pre-check
                    # above (e.g. DNS answer changed): still refused.
                    self.render_refused += 1
                    print(f"[storage] {exc} (HTML kept as the artifact)",
                          file=sys.stderr, flush=True)
                    path = html_path
                except Exception:
                    # Render failed -> canonical artifact stays the HTML.
                    path = html_path
            else:
                path = html_path
        else:
            path = self.target_path(rec)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(path, content)

        rec.sha256 = digest
        rec.local_path = self.cfg.stored_path(path)
        self._ids.add(rec.doc_id)
        self._hash_docid[digest] = rec.doc_id
        self._bank_by_id[rec.doc_id] = rec.bank_code
        self._urls.add(normalize_url(rec.document_url))
        if rec.source_url:
            self._source_urls.add(normalize_url(rec.source_url))
        self._append(rec)
        self.quarantine.record_success(rec.document_url)
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

        Content check (issue #21, A2): a ``.pdf`` file must pass the same PDF
        check as a download (pdfcheck.check_pdf_bytes with
        Config.min_pdf_bytes / pdf_require_eof) BEFORE it is hashed. A
        truncated file left by a killed process or a full disk, or an error
        page saved as .pdf, returns ``skip:bad-content`` with one stderr line
        naming the file: it is never indexed, never a dedup owner, and its
        quarantine is not released. The file is left in place (evidence; the
        operator decides). The check also runs in dry run -- reading is not
        writing, and "would reindex" would be a lie for such a file. An
        ``.html`` artifact is not checked (an HTML page is its own document).
        """
        if rec.doc_id in self._ids:
            return "skip:already-indexed"
        if not path.is_file():
            return "skip:missing-file"
        ext = path.suffix.lower().lstrip(".")
        content: Optional[bytes] = None
        if ext == "pdf":
            content = path.read_bytes()
            problem = check_pdf_bytes(content, min_bytes=self.cfg.min_pdf_bytes,
                                      require_eof=self.cfg.pdf_require_eof)
            if problem:
                print(f"[storage] reindex skipped bad-content {path}: {problem}",
                      file=sys.stderr, flush=True)
                return BAD_CONTENT
        if dry_run:
            return "dry-run:would-reindex"

        if content is None:
            content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest in self._hash_docid:
            owner = self._hash_docid[digest]
            self._note_duplicate(rec, owner)
            return f"skip:duplicate-content:{owner}"

        rec.mime_type = {"pdf": "application/pdf", "html": "text/html"}.get(ext, rec.mime_type)
        if ext == "html":
            rec.html_path = self.cfg.stored_path(path)
        rec.sha256 = digest
        rec.local_path = self.cfg.stored_path(path)
        self._ids.add(rec.doc_id)
        self._hash_docid[digest] = rec.doc_id
        self._bank_by_id[rec.doc_id] = rec.bank_code
        self._urls.add(normalize_url(rec.document_url))
        if rec.source_url:
            self._source_urls.add(normalize_url(rec.source_url))
        self._append(rec)
        # An externally-recovered doc registered here (no fetch at all) must
        # release its quarantine too — otherwise the nightly sync would keep
        # skipping a URL the corpus now actually has (recover-quarantine
        # design §3: any success releases the URL).
        self.quarantine.record_success(rec.document_url)
        return "reindexed"

    def _record_download_error(self, rec: DocRecord, exc: Exception, label: str) -> None:
        """Append one line to data/download_errors.jsonl (durable audit of every
        failed download — stdout scrolls away, this file doesn't). Append-only,
        O_APPEND line writes; never read by the crawler itself. Feeds the
        quarantine with this run's night (`self._night`, fixed at
        construction). The append holds the file's sidecar lock
        (filelock.locked_sidecar) so a locked rewrite of the file
        (`migrate-document-url --write`) never loses a line."""
        entry = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "label": label,
            "bank_code": rec.bank_code,
            "doc_type": rec.doc_type.code,
            "title": rec.title,
            "document_url": rec.document_url,
            "alt_urls": rec.alt_urls or [],
            "source_url": rec.source_url,
            "error": f"{type(exc).__name__}: {exc}".replace("\n", " ")[:500],
        }
        path = self.cfg.data_dir / "download_errors.jsonl"
        with _locked(path), path.open("a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.quarantine.record_failure(rec.document_url, night=self._night)

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
        rows that own their content (flush_duplicate_stamps: real PDF owners
        only by default, see is_stampable_owner), in a finally so a record
        iterator that raises mid-batch still persists what was learnt; a stamping failure there is only a
        WARNING (never masks the batch). Never written in dry run. One
        `duplicate-content: N` line, one `render refused: N` line and one
        `bad-content: N` line on stderr when N > 0 for this batch.

        A record whose every candidate URL failed the content check
        (ContentCheckFailed, issue #21) is tallied as "error" like any failed
        download -- it is one (download_errors.jsonl, quarantine, a failed
        document in the run-report) -- and counted again on the
        `bad-content` line so a host serving error pages with 200 is visible
        as such, not lost among dead URLs. The returned counts then also
        carry a "bad-content" key (a SUB-count of "error", present only when
        > 0), which RunReport's record_saved_counts folds into the report's
        `bad_content` counter without counting those records twice."""
        counts: dict[str, int] = {}
        total = 0
        refused_before = self.render_refused
        rejected_before = self.content_rejected
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
            # safe=True: a stamping failure is a WARNING, never a replacement
            # for the batch's own exception (or its return value).
            self.flush_duplicate_stamps(label, dry_run=dry_run, safe=True)
        refused = self.render_refused - refused_before
        if refused:
            prefix = f"[{label}] " if label else ""
            print(f"{prefix}render refused: {refused} (private network or unresolvable host; "
                  f"HTML kept)",
                  file=sys.stderr, flush=True)
        rejected = self.content_rejected - rejected_before
        if rejected:
            counts["bad-content"] = rejected
            prefix = f"[{label}] " if label else ""
            print(f"{prefix}bad-content: {rejected} (every candidate URL failed the "
                  f"content check; recorded in download_errors.jsonl)",
                  file=sys.stderr, flush=True)
        summary = self.quarantine.summary_line()
        if summary:
            print(summary, file=sys.stderr, flush=True)
        return counts
