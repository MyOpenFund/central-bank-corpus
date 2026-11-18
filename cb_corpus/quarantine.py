"""Per-URL download quarantine — dead-letter state for the nightly sync path.

One month of production left exactly 76 unique documents failing every
single night (dead RePEc-sourced PDF URLs, `download_errors.jsonl` audit).
This module stops that re-hammering: it tracks, per URL, the number of
DISTINCT ATTEMPTED nights a download has failed — not necessarily calendar-
consecutive: once a URL is actually quarantined it stops being attempted on
bounded nights, so its night list only grows on nights it's retried (e.g. the
Sunday full sweep's `QUARANTINE_RETRY=1` bypass) and failed again. Only a
SUCCESS resets the count. Once the distinct-attempted-nights count reaches
`QUARANTINE_AFTER_NIGHTS` (env, default 5) the caller is told to skip the URL
in the nightly bounded (Mon-Sat) sync — only the Sunday full sweep (`sync
full`, via `QUARANTINE_RETRY=1`) still tries it. Any success releases the
URL immediately (tombstone); a later failure restarts the count from zero.

State: `data/download_quarantine.jsonl` beside `download_errors.jsonl`,
append-only, one JSON row per event:
    {"url": ..., "nights": ["YYYY-MM-DD", ...], "quarantined": bool}
    {"url": ..., "released": true}                          # tombstone

Latest line per url wins on replay (same discipline as the manifest files in
storage.py) — a `released` tombstone clears all prior state for that url.
Corrupt/torn lines are skipped with ONE warning for the whole load (not one
per line: a bad night can produce many failing URLs, and this file is
operational churn, not an audit record worth a hard stop) — deliberately
simpler than storage.py's torn-tail repair machinery: this file is never
read back for anything but quarantine decisions, so there is nothing to
repair-in-place, only something to never let crash the caller.

Same-night repeats (a URL retried more than once within one nightly run,
before Quarantine.record_failure is even in the picture) must count as ONE
failing night — otherwise a single bad night with internal retries could
quarantine a URL that has actually only failed once.

Several writers (issue #25, integrity wave 2026-11). The real invariant is
NOT a single writer: the nightly job runs up to `DISCOVER_WORKERS` (6)
parallel per-bank discover processes (deploy/run-job.sh), each with its own
Storage -> Quarantine on this one file, plus recover-downloads. With
last-line-wins replay, a writer appending from its stale startup state
could regress another writer's night count or resurrect a released URL.
So every write (record_failure, record_success, seed) happens under ONE
exclusive sidecar flock (`.download_quarantine.jsonl.lock`,
filelock.locked_sidecar -- the manifests' discipline) and, inside it, first
REPLAYS the lines other processes appended since this instance last read
the file (it keeps its byte offset), then computes its row from that fresh
state and appends it. The startup load takes the same lock (only when the
file exists), so it never sees a half-written line from a live writer.

Reads do not take the lock: `is_quarantined` decides from the state loaded
at startup plus whatever this instance replayed while writing -- one read
per run, by design (a URL another worker quarantines during the run is
skipped from the next run on). Limit: `flock` is advisory, and its
guarantees on a network filesystem (NFS/SMB) depend on the server and the
mount; on the NAS's local disk it is exact.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from typing import Callable, Optional

from .config import Config
from .filelock import locked_sidecar

_DEFAULT_THRESHOLD = 5


def _threshold() -> int:
    """Distinct-failing-nights threshold. Re-read from the environment on
    EVERY call (never cached on the instance) so `QUARANTINE_AFTER_NIGHTS`
    can be tuned between runs (or even between calls, in tests) without
    reconstructing a Quarantine. Malformed/non-positive values fall back to
    the default rather than raising — a typo in an env var must never crash
    the nightly sync."""
    raw = os.environ.get("QUARANTINE_AFTER_NIGHTS", str(_DEFAULT_THRESHOLD))
    try:
        n = int(raw)
    except ValueError:
        return _DEFAULT_THRESHOLD
    return n if n > 0 else _DEFAULT_THRESHOLD


class Quarantine:
    """Per-URL dead-letter state, backed by an append-only JSONL file."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._path = cfg.data_dir / "download_quarantine.jsonl"
        # url -> {"nights": [...]}; "quarantined" is NOT trusted from disk as
        # the live truth (see is_quarantined) — it's written for operator
        # readability only, recomputed fresh against the CURRENT threshold.
        self._state: dict[str, dict] = {}
        self._skipped = 0
        # How far this instance has replayed the file: byte offset, inode
        # (a rewrite or deletion means "replay from the start") and whether
        # the bytes read so far end with a newline (a torn tail left by a
        # killed writer must not swallow our next row). See _catch_up.
        self._offset = 0
        self._ino: Optional[int] = None
        self._ends_nl = True
        self._load()

    # -- state file IO ---------------------------------------------------
    def _load(self) -> None:
        """Startup read, under the sidecar lock when the file exists (a
        missing file is read lock-free: reading must create nothing).

        A data dir where the sidecar cannot be created or opened for append
        (read-only mount, foreign owner -- e.g. a reporting command run on a
        snapshot) must not make every command fail at startup: the state is
        then read WITHOUT the lock, after one WARNING. Such a read can at
        worst miss a line being appended at that instant; the writes
        (_update) still take the lock and fail loudly if it is unavailable."""
        if not self._path.exists():
            return
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(locked_sidecar(self._path))
            except OSError as exc:
                print(f"[quarantine] WARNING: cannot lock {self._path} "
                      f"({type(exc).__name__}: {exc}); reading it unlocked",
                      file=sys.stderr, flush=True)
            self._catch_up()

    def _catch_up(self) -> None:
        """Replay the bytes appended since this instance's last read. MUST be
        called under the sidecar lock: every writer holds it for its whole
        append, so the tail read here is complete -- except a fragment left
        by a writer killed mid-write, which is skipped as corrupt (and
        terminated by the next _write). If the file was replaced (new inode)
        or shrank below our offset, or vanished, the state is rebuilt from
        the file's current content (disk is the truth)."""
        try:
            fh = self._path.open("rb")
        except FileNotFoundError:
            if self._ino is not None:
                self._state, self._offset, self._ino, self._ends_nl = {}, 0, None, True
            return
        with fh:
            st = os.fstat(fh.fileno())
            if st.st_ino != self._ino or st.st_size < self._offset:
                self._state, self._offset, self._ends_nl = {}, 0, True
            fh.seek(self._offset)
            data = fh.read()
        self._ino = st.st_ino
        self._replay(data)
        self._offset += len(data)
        if data:
            self._ends_nl = data.endswith(b"\n")

    def _replay(self, data: bytes) -> None:
        warned = False
        for ln in data.splitlines():
            if not ln.strip():
                continue
            try:
                row = json.loads(ln.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                row = None
            url = row.get("url") if isinstance(row, dict) else None
            if not url:
                if not warned:
                    print(f"[quarantine] WARNING: corrupt line(s) in {self._path}, "
                          f"skipped", file=sys.stderr, flush=True)
                    warned = True
                continue
            if row.get("released"):
                self._state.pop(url, None)
                continue
            if "seeded" in row:
                # A seeded row is unconditionally quarantined (see seed())
                # independent of the night-count threshold -- no synthetic
                # nights list to replay.
                self._state[url] = {"nights": [], "seeded": True}
                continue
            self._state[url] = {"nights": list(row.get("nights") or [])}

    def _update(self, compute: Callable[[], Optional[dict]]) -> None:
        """The one write path: under the sidecar lock, catch up on other
        writers' lines, then let `compute` decide from that fresh state (it
        mutates self._state and returns the row to append, or None for no
        write)."""
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        with locked_sidecar(self._path):
            self._catch_up()
            row = compute()
            if row is not None:
                self._write(row)

    def _write(self, row: dict) -> None:
        """Append one JSON row with a single write() on an O_APPEND fd (under
        the lock held by _update). A torn tail from a killed writer is first
        terminated with a newline, so it stays one corrupt line instead of
        merging with this row."""
        line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        if not self._ends_nl:
            line = b"\n" + line
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view):]
            self._ino = os.fstat(fd).st_ino
        finally:
            os.close(fd)
        self._offset += len(line)
        self._ends_nl = True

    def _unchanged_since_last_read(self) -> bool:
        """Cheap lock-free probe: True when nobody has written the file since
        this instance last read or wrote it (same inode, same size) -- or it
        does not exist and we never saw it."""
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return self._ino is None
        return st.st_ino == self._ino and st.st_size == self._offset

    # -- public API --------------------------------------------------------
    def is_quarantined(self, url: str) -> bool:
        """True only when `url` is ACTIVELY quarantined for this call.

        Recomputed from the night count against the CURRENT threshold (not
        the cached on-disk `quarantined` flag from whenever it was last
        written) — "read at call time" per the design contract. Every True
        return here represents one URL the caller will actually skip this
        run, which is what skipped_count()/summary_line() report; a
        QUARANTINE_RETRY=1 bypass returns False (so the Sunday full sweep
        retries everything) WITHOUT mutating state and WITHOUT counting as
        a skip.
        """
        state = self._state.get(url)
        if not state:
            return False
        if not state.get("seeded") and len(state["nights"]) < _threshold():
            return False
        if os.environ.get("QUARANTINE_RETRY") == "1":
            return False
        self._skipped += 1
        return True

    def record_failure(self, url: str, night: str) -> None:
        """Record one failed download attempt for `url` on `night`
        ("YYYY-MM-DD"). A `night` equal to the LAST recorded night for this
        url is a no-op on the count (same-night idempotence) but still
        appends a state line (cheap, and keeps the on-disk trail showing the
        retry happened). The nights list is capped at the current threshold
        so a URL that fails for months doesn't grow this file's per-url
        state without bound — only the most recent nights are kept.

        No-op (nothing written, nothing mutated) when `url`'s CURRENT state
        is a SEEDED quarantined row (see `seed()`): a seeded row carries no
        night-count history to update, and it must not decay into a fresh
        1-night count row just because the Sunday full sweep
        (`QUARANTINE_RETRY=1`) retried it and failed — that would let the
        bounded Mon-Sat sync start re-hammering the URL again before it
        re-accumulates `QUARANTINE_AFTER_NIGHTS` worth of failures. A seeded
        row stays quarantined until an explicit `record_success()` release.

        Decided under the sidecar lock on the state AFTER replaying other
        writers' lines (see the module docstring): a night another process
        recorded for this url is continued, not overwritten.
        """
        def compute() -> Optional[dict]:
            existing = self._state.get(url)
            if existing and existing.get("seeded"):
                return None
            state = self._state.setdefault(url, {"nights": []})
            nights = state["nights"]
            if not nights or nights[-1] != night:
                nights.append(night)
            threshold = _threshold()
            if len(nights) > threshold:
                del nights[: len(nights) - threshold]
            quarantined = len(nights) >= threshold
            return {"url": url, "nights": list(nights), "quarantined": quarantined}
        self._update(compute)

    def record_success(self, url: str) -> None:
        """Release `url`: append a `released` tombstone and clear in-memory
        state. A subsequent failure starts a fresh night count from zero —
        no memory of the prior failing streak survives a release.

        No-op if `url` has no active state (not in the in-memory dict) — a
        guard against unbounded growth if callers fire this on every successful
        download without checking quarantine status first.

        "No active state" is judged on the fresh state (after replaying other
        writers' lines under the lock), so a URL another process started
        failing during this run is released too. Called on every successful
        save, so the common case stays lock-free: when the url is unknown
        here AND the file is unchanged since this instance last read it
        (same inode and size), there is nothing to release."""
        if url not in self._state and self._unchanged_since_last_read():
            return

        def compute() -> Optional[dict]:
            if url not in self._state:
                return None
            self._state.pop(url, None)
            return {"url": url, "released": True}
        self._update(compute)

    def seed(self, url: str, reason: str = "") -> None:
        """Seed `url` as ALREADY quarantined, for a document confirmed
        unrecoverable after a manual hunt (recover-quarantine design §4) --
        the nightly sync must stop re-hammering it immediately, without
        waiting to accumulate `QUARANTINE_AFTER_NIGHTS` worth of synthetic
        failures first (that would misrepresent the actual failure history:
        it never really failed that many DISTINCT nights, a human just
        confirmed it dead). Writes one line
        `{"url": url, "seeded": reason, "quarantined": True}` -- honest
        history, distinct from a real night-count row. The "seeded" marker
        is unconditionally quarantined (independent of the night-count
        threshold, so raising QUARANTINE_AFTER_NIGHTS later never
        un-quarantines it) until a `record_success` release."""
        def compute() -> dict:
            self._state[url] = {"nights": [], "seeded": True}
            return {"url": url, "seeded": reason, "quarantined": True}
        self._update(compute)

    def skipped_count(self) -> int:
        """Number of is_quarantined() calls since the counter was last reset
        that returned True — i.e. URLs the calling sync loop actually
        skipped (bypassed calls never count, since nothing was skipped).
        A live, non-consuming read: unlike `summary_line()`, calling this
        does NOT reset the counter."""
        return self._skipped

    def summary_line(self) -> Optional[str]:
        """One-line nightly log summary, or None when nothing was skipped
        (keeps a clean run's log free of a zero-noise line).

        CONSUMES the skip counter: reading it resets it to zero, so a caller
        that runs several `save_many()` batches against the SAME Storage/
        Quarantine instance in one process (e.g. one discovery run working
        multiple banks in turn) gets each batch's OWN skip count here,
        rather than a running cumulative total that keeps growing across
        every batch. `skipped_count()` reads the same counter, so after a
        `summary_line()` call it too reports skips since that reset."""
        if self._skipped == 0:
            return None
        line = f"quarantine: skipped {self._skipped} url(s)"
        self._skipped = 0
        return line
