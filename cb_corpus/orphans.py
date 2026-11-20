"""`sweep-orphans`: quarantine on-disk files that duplicate indexed documents.

An *orphan* is any regular file under ``raw/`` whose filename stem is not a
manifest ``doc_id``. Orphans exist because an earlier doc_id scheme named the
same documents differently and the old files were never removed; nearly all
of them are byte-identical to a document that IS indexed under another id.

Rules (see the design note of 2026-09-03):

* an orphan whose sha256 is owned by a manifest row is a **duplicate** → moved
  to ``raw_orphans/<same relative path>`` (``os.replace``, same filesystem);
* any other orphan is **unindexed** → left in place, reported (candidate for
  ``reindex-from-disk``);
* a file whose stem IS a manifest doc_id is never examined, let alone moved;
* the default run is a dry-run: it classifies and reports, moves nothing;
* a file that cannot be hashed (permission error, vanished mid-walk) is
  reported as a failure, never fatal to the sweep, and never moved;
* a duplicate whose owning manifest row's own file is missing from disk is
  never moved — quarantining it would leave zero on-disk copies of that
  document;
* symlinks are ignored: never walked as orphans, never used as a move
  destination;
* a ``.part`` temp (``.<name>.<random>.part``, written by storage's atomic
  writes and checked renders, issue #21) is not an orphan: one younger than
  ``temp_min_age`` (default 24 h) may still be in use by a running job and
  is skipped (``temps_fresh``); an older one was left by a killed process
  and is reported as ``stale-temp`` (``temps_stale``), moved to
  ``raw_orphans/`` only with ``--move`` like a duplicate, never deleted.
  Its moves are counted apart (``temps_moved`` / ``temps_move_failed``;
  ``moved`` / ``move_failed`` count duplicates only), and one that cannot be
  hashed gets the same ``hash-failed`` line (and ``hash_failed`` count) as
  an orphan, still counted in ``temps_stale``, never moved.

The manifest is read once, through :func:`storage.iter_manifest_rows` (no
``Storage()`` side effects). On the NAS the run-job global lock guarantees no
concurrent manifest writer; from a workstation over SMB, run it outside the
nightly sync window.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .config import Config
from .storage import iter_manifest_rows

PDF_MAGIC = b"%PDF"
MIN_VALID_PDF_BYTES = 20 * 1024      # same honesty rule as recover._verify_local_pdf
_CHUNK = 1024 * 1024
# A `.part` temp younger than this may still be written by a running job
# (a download or a Chrome render takes minutes at most; a whole night's job
# is bounded by DISCOVER_BANK_TIMEOUT, 3 h): left alone by the sweep.
DEFAULT_TEMP_MIN_AGE_SECONDS = 24 * 3600


def is_temp_part(path: Path) -> bool:
    """True for a storage temp sibling: hidden and `.part`-suffixed
    (storage._temp_sibling names them `.<name>.<random>.part`)."""
    return path.name.startswith(".") and path.name.endswith(".part")


@dataclass
class OrphanEntry:
    path: str                 # absolute on-disk path
    rel: str                  # relative to raw/, posix
    bank: str
    doc_type: str
    year_dir: str
    ext: str                  # lower-case, without the dot; "" when none
    size: int
    sha256: str
    valid_pdf: bool
    matched_doc_id: Optional[str]
    # would-move | moved | move-failed | kept-unindexed | kept-owner-missing | hash-failed
    # | stale-temp (an old `.part` temp, dry run)
    action: str
    dest: Optional[str]       # relative path under raw_orphans/ (duplicates only)
    error: Optional[str] = None
    owner_path: Optional[str] = None   # resolved owner path (set for every duplicate)


@dataclass
class SweepSummary:
    files_seen: int
    orphans: int
    duplicates: int
    unindexed: int
    moved: int
    move_failed: int
    bytes_duplicates: int
    dry_run: bool
    report_path: str
    started_at: str
    finished_at: str
    hash_failed: int = 0
    owner_missing: int = 0
    banks: Optional[list[str]] = None
    temps_fresh: int = 0      # `.part` temps younger than temp_min_age: skipped
    temps_stale: int = 0      # older `.part` temps: reported (moved with --move)
    # Stale temps moved / whose move failed with --move; `moved` and
    # `move_failed` count duplicates only.
    temps_moved: int = 0
    temps_move_failed: int = 0


@dataclass
class ManifestIndex:
    doc_ids: set[str]
    by_hash: dict[str, str]          # sha256 -> doc_id
    owner_path: dict[str, Path]      # sha256 -> resolved on-disk path of the owning row


def _resolve_local_path(cfg: Config, local_path: str) -> Optional[Path]:
    """Best-effort on-disk path for a manifest row's ``local_path``.

    If it is absolute, use it as is; else if it contains ``"raw/"``, take the
    part after the FIRST occurrence and join it to ``cfg.raw_dir``; else the
    owner path is unknown (treated as missing). Both stored forms resolve
    alike: the contract form ``raw/...`` (relative to the data root, written
    since 2026-11-20) and the legacy ``data/raw/...`` (TRANSITION, see
    Config.resolve_stored_path).
    """
    p = Path(local_path)
    if p.is_absolute():
        return p
    marker = "raw/"
    idx = local_path.find(marker)
    if idx != -1:
        return cfg.raw_dir / local_path[idx + len(marker):]
    return None


def load_manifest_index(cfg: Config) -> ManifestIndex:
    """Index every per-bank manifest into doc_ids, a sha256->doc_id map, and a
    sha256->owner on-disk path map.

    Read-only, via :func:`storage.iter_manifest_rows` (blank lines skipped,
    torn tails handled there). A sha256 owned by two rows keeps the first
    owner — which row "owns" the bytes is immaterial to the sweep.
    """
    ids: set[str] = set()
    by_hash: dict[str, str] = {}
    owner_path: dict[str, Path] = {}
    for rec in iter_manifest_rows(cfg):
        doc_id = rec.get("doc_id")
        if not doc_id:
            continue
        ids.add(doc_id)
        sha = rec.get("sha256")
        if sha:
            by_hash.setdefault(sha, doc_id)
            if sha not in owner_path:
                local_path = rec.get("local_path")
                if local_path:
                    resolved = _resolve_local_path(cfg, local_path)
                    if resolved is not None:
                        owner_path[sha] = resolved
    return ManifestIndex(doc_ids=ids, by_hash=by_hash, owner_path=owner_path)


def _iter_all_files(cfg: Config, banks: Optional[set[str]]) -> Iterator[Path]:
    """Every regular file under raw/ — loose top-level files first, then bank
    directories in sorted order, files sorted within each bank — for the
    counters; the orphan test itself is applied by the caller so
    ``files_seen`` is honest.

    Symlinks are never yielded (broken or not — they are not this sweep's
    concern). Loose files sitting directly at the top level of ``raw/`` (not
    inside any bank directory) are included only for a full sweep
    (``banks is None``); with an explicit ``banks`` filter they belong to no
    requested bank and are skipped.
    """
    if banks is None:
        for path in sorted(cfg.raw_dir.iterdir()):
            if path.is_file() and not path.is_symlink():
                yield path
    for bank_dir in sorted(p for p in cfg.raw_dir.iterdir() if p.is_dir() and not p.is_symlink()):
        if banks is not None and bank_dir.name not in banks:
            continue
        for path in sorted(bank_dir.rglob("*")):
            if path.is_file() and not path.is_symlink():
                yield path


def _parts(rel: str) -> tuple[str, str, str]:
    """(bank, doc_type, year_dir) from a raw/-relative posix path, empty for
    any segment the path is too shallow to have."""
    parts = rel.split("/")
    bank = parts[0] if len(parts) > 1 else ""
    doc_type = parts[1] if len(parts) > 2 else ""
    year_dir = parts[2] if len(parts) > 3 else ""
    return bank, doc_type, year_dir


def _sha256_of(path: Path) -> tuple[str, int, bool]:
    """(sha256, size, valid_pdf) in one streaming pass."""
    h = hashlib.sha256()
    size = 0
    head = b""
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            if not head:
                head = chunk[:len(PDF_MAGIC)]
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size, (size > MIN_VALID_PDF_BYTES and head == PDF_MAGIC)


def classify(path: Path, raw: Path, index: ManifestIndex) -> OrphanEntry:
    """Hash one orphan and decide its provisional action.

    Raises whatever :func:`_sha256_of` raises (``OSError`` subclasses) on an
    unreadable or vanished file — the caller (:func:`sweep_orphans`) is
    responsible for containing that per file.
    """
    rel = path.relative_to(raw).as_posix()
    bank, doc_type, year_dir = _parts(rel)
    ext = path.suffix.lower().lstrip(".")
    sha, size, valid = _sha256_of(path)
    owner = index.by_hash.get(sha)
    return OrphanEntry(
        path=str(path), rel=rel, bank=bank, doc_type=doc_type, year_dir=year_dir,
        ext=ext, size=size, sha256=sha, valid_pdf=valid, matched_doc_id=owner,
        action="would-move" if owner else "kept-unindexed",
        dest=rel if owner else None,
    )


def move_to_orphans(entry: OrphanEntry, cfg: Config) -> None:
    """Move one duplicate from raw/ to raw_orphans/ (same relative path).

    ``os.replace`` inside ``data_dir`` — an atomic rename on one filesystem.
    Never overwrites: a destination that already holds the same bytes means a
    replayed sweep, so the source is simply removed; a destination with other
    content is left alone and the move is recorded as failed.

    The replayed-sweep ``unlink`` trusts the sha256 captured at classify time
    rather than re-hashing the source — safe under the NAS job lock (no
    concurrent writer can touch ``raw/`` mid-sweep), so avoid running this
    from a workstation against live SMB writers.
    """
    src = Path(entry.path)
    dest = cfg.orphans_dir / entry.rel
    if dest.is_symlink():
        entry.action, entry.error = "move-failed", "dest is a symlink"
        return
    orphans_root = cfg.orphans_dir.resolve()
    dest_parent = dest.parent.resolve()
    if dest_parent != orphans_root and orphans_root not in dest_parent.parents:
        entry.action, entry.error = "move-failed", "dest parent escapes raw_orphans/"
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest_sha, _, _ = _sha256_of(dest)
            if dest_sha != entry.sha256:
                entry.action, entry.error = "move-failed", "dest exists with different content"
                return
            src.unlink()
        else:
            os.replace(src, dest)
        entry.action = "moved"
    except OSError as exc:
        entry.action, entry.error = "move-failed", f"{type(exc).__name__}: {exc}"


def _owner_holds(owner: Optional[Path], entry: OrphanEntry,
                 cache: dict[Path, str]) -> tuple[bool, Optional[str]]:
    """Verify the manifest row's owner file still holds this duplicate's exact
    bytes: same file, same size, same sha256 — never existence alone.

    The owner is hashed at most once per owner path across the whole sweep
    (memoised in ``cache``, streaming via :func:`_sha256_of`), since many
    duplicates commonly share one owner.
    """
    if owner is None:
        return False, "owner path unknown"
    try:
        if not owner.is_file():
            return False, f"owner file missing: {owner}"
        try:
            if owner.samefile(entry.path):
                return False, "owner is this file"
        except OSError:
            return False, f"owner file missing: {owner}"
        if owner.stat().st_size != entry.size:
            return False, f"owner size differs: {owner}"
        owner_sha = cache.get(owner)
        if owner_sha is None:
            owner_sha, _, _ = _sha256_of(owner)
            cache[owner] = owner_sha
        if owner_sha != entry.sha256:
            return False, f"owner content differs: {owner}"
        return True, None
    except OSError as exc:
        return False, f"owner check failed: {type(exc).__name__}: {exc}"


def _hash_failed_entry(path: Path, rel: str, bank: str, doc_type: str, year_dir: str,
                       exc: OSError) -> OrphanEntry:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return OrphanEntry(
        path=str(path), rel=rel, bank=bank, doc_type=doc_type, year_dir=year_dir,
        ext=path.suffix.lower().lstrip("."), size=size, sha256="", valid_pdf=False,
        matched_doc_id=None, action="hash-failed", dest=None,
        error=f"{type(exc).__name__}: {exc}",
    )


def _raise_sigterm(signum, frame) -> None:
    raise SystemExit(143)


def sweep_orphans(cfg: Config, *, banks: Optional[set[str]] = None, move: bool = False,
                  progress_every: int = 1000, report_path: Optional[Path] = None,
                  now: Optional[datetime] = None,
                  temp_min_age: float = DEFAULT_TEMP_MIN_AGE_SECONDS) -> SweepSummary:
    """Find, classify, (move) and report every orphan under raw/.

    Raises ``FileNotFoundError`` when ``raw/`` is missing or not a directory —
    a mis-pointed data dir must not look like a clean corpus — or when
    ``banks`` names a code with no ``raw/<code>/`` directory.

    Installs a ``SIGTERM`` handler (main thread only) for the duration of the
    sweep so an operator-issued kill still writes the summary; the previous
    handler is always restored.

    ``temp_min_age`` (seconds between a file's mtime and the sweep start,
    ``now``): a
    ``.part`` temp younger than this is skipped, an older one reported as a
    stale temp (see the module docstring).
    """
    if not cfg.raw_dir.is_dir():
        raise FileNotFoundError(f"corpus raw dir not found or not a directory: {cfg.raw_dir}")
    if banks is not None:
        missing = {b for b in banks if not (cfg.raw_dir / b).is_dir()}
        if missing:
            raise FileNotFoundError(
                f"unknown bank code(s) (no raw/<code>/ directory): {', '.join(sorted(missing))}")
    started = now or datetime.now(timezone.utc)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    report_path = report_path or (cfg.reports_dir / f"orphan_sweep_{stamp}.jsonl")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    index = load_manifest_index(cfg)
    owner_hash_cache: dict[Path, str] = {}
    files_seen = orphans_n = dups = unindexed = moved = failed = 0
    hash_failed = owner_missing = 0
    temps_fresh = temps_stale = temps_moved = temps_failed = 0
    bytes_dups = 0

    install_handler = threading.current_thread() is threading.main_thread()
    prev_handler = signal.signal(signal.SIGTERM, _raise_sigterm) if install_handler else None
    try:
        with report_path.open("w", encoding="utf-8") as out:
            for path in _iter_all_files(cfg, banks):
                files_seen += 1
                if progress_every and files_seen % progress_every == 0:
                    print(f"sweep-orphans: examined {files_seen} files "
                          f"({orphans_n} orphans, {dups} duplicates so far)", file=sys.stderr, flush=True)
                if is_temp_part(path):
                    try:
                        age = started.timestamp() - path.stat().st_mtime
                    except OSError:          # renamed into place meanwhile
                        continue
                    if age < temp_min_age:
                        temps_fresh += 1
                        continue
                    temps_stale += 1
                    try:
                        entry = classify(path, cfg.raw_dir, index)
                    except OSError as exc:
                        # Same report line as an orphan that cannot be
                        # hashed; never moved.
                        rel = path.relative_to(cfg.raw_dir).as_posix()
                        bank, doc_type, year_dir = _parts(rel)
                        entry = _hash_failed_entry(path, rel, bank, doc_type, year_dir, exc)
                        hash_failed += 1
                    else:
                        entry.action, entry.dest = "stale-temp", entry.rel
                        if move:
                            move_to_orphans(entry, cfg)
                            if entry.action == "moved":
                                temps_moved += 1
                            else:
                                temps_failed += 1
                    out.write(json.dumps(asdict(entry), ensure_ascii=True) + "\n")
                    out.flush()
                    continue
                # Multi-dot names (e.g. "x.tar.gz") would have stem "x.tar";
                # the corpus layout is "<id>.<ext>" so this never arises here.
                if path.stem in index.doc_ids:
                    continue
                orphans_n += 1
                try:
                    entry = classify(path, cfg.raw_dir, index)
                except OSError as exc:
                    rel = path.relative_to(cfg.raw_dir).as_posix()
                    bank, doc_type, year_dir = _parts(rel)
                    entry = _hash_failed_entry(path, rel, bank, doc_type, year_dir, exc)
                    hash_failed += 1
                else:
                    if entry.matched_doc_id is None:
                        unindexed += 1
                    else:
                        dups += 1
                        owner = index.owner_path.get(entry.sha256)
                        entry.owner_path = str(owner) if owner is not None else None
                        ok, reason = _owner_holds(owner, entry, owner_hash_cache)
                        if not ok:
                            entry.action, entry.dest, entry.error = "kept-owner-missing", None, reason
                            owner_missing += 1
                        else:
                            bytes_dups += entry.size
                            if move:
                                move_to_orphans(entry, cfg)
                                if entry.action == "moved":
                                    moved += 1
                                else:
                                    failed += 1
                out.write(json.dumps(asdict(entry), ensure_ascii=True) + "\n")
                out.flush()
    finally:
        finished = datetime.now(timezone.utc)
        summary = SweepSummary(
            files_seen=files_seen, orphans=orphans_n, duplicates=dups, unindexed=unindexed,
            moved=moved, move_failed=failed, bytes_duplicates=bytes_dups, dry_run=not move,
            report_path=str(report_path), started_at=started.isoformat(),
            finished_at=finished.isoformat(), hash_failed=hash_failed, owner_missing=owner_missing,
            banks=sorted(banks) if banks is not None else None,
            temps_fresh=temps_fresh, temps_stale=temps_stale,
            temps_moved=temps_moved, temps_move_failed=temps_failed,
        )
        report_path.with_suffix(".summary.json").write_text(
            json.dumps(asdict(summary), indent=2) + "\n", encoding="utf-8")
        # Restore the handler only after the summary is on disk: a restore that
        # raises (a C-level previous handler reads back as None) must not cost
        # the operator's primary artefact.
        if install_handler and prev_handler is not None:
            signal.signal(signal.SIGTERM, prev_handler)
    return summary
