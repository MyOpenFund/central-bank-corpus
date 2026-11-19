"""Cross-process exclusive lock on a data file's SIDECAR lock file.

One small module so every state file that needs mutual exclusion between
processes (the per-bank manifests in storage.py, the download quarantine in
quarantine.py) shares one discipline, without quarantine.py having to import
storage.py (which imports quarantine.py).

Why a sidecar and not the data file itself: a data file rewritten with
temp + os.replace gets a new inode on every rewrite, so a flock taken on the
data file cannot give mutual exclusion between appenders and rewriters -- an
appender can hold a lock on the OLD inode while a rewriter swaps in a new
one, orphaning the appended bytes (issue #18). The sidecar
(`.<name>.lock`, created on demand, never replaced or deleted) has a stable
inode.

Limit: `flock` is ADVISORY (only cooperating writers that take the same lock
are excluded), and its guarantees on a network filesystem (NFS, SMB) depend
on the server and the mount; on a local filesystem it is exact.
"""
from __future__ import annotations

import contextlib
import fcntl
from pathlib import Path
from typing import Iterator


@contextlib.contextmanager
def locked_sidecar(data_file: Path) -> Iterator[None]:
    """Hold an exclusive flock on `data_file`'s sidecar lock file
    (`.<name>.lock` in the same directory) for the duration of the block.

    The sidecar is created on demand (with its parent directory); the data
    file itself is never created nor opened here. Blocking: waits until any
    other holder releases it."""
    lock = data_file.with_name("." + data_file.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
