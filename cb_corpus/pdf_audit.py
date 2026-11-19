"""`audit-pdfs`: read-only check of the PDFs already on disk against the content rules.

The content check (`pdfcheck.check_pdf_bytes`, issue #21) guards NEW writes and
reindexes. It says nothing about the files stored before it existed, and its
`%%EOF` rule (`Config.pdf_require_eof`, a marker within the last
`pdfcheck.EOF_WINDOW` bytes) was chosen without a measurement on the real
corpus: a legitimate PDF with more trailing bytes after its last `%%EOF` would
be rejected the next time it is downloaded or reindexed. This command measures
both, on every manifest row whose artifact is a `.pdf`:

* the rows whose file would fail today's rules, by reason (`missing`,
  `not-pdf`, `too-small`, `no-eof-in-window`), per bank and document type;
* for a `no-eof-in-window` file, how far from the end its last `%%EOF` is
  (`eof_from_end`, searched in the last `tail_bytes` bytes; empty when there is
  none there): the window that would accept it.

It only reads: a file's first `pdfcheck.MAGIC_WINDOW` bytes and its last
`tail_bytes` bytes (never the whole file), and writes one CSV of the failing
rows (`reports/pdf_audit.csv` by default). Nothing in the manifests, `raw/`,
the quarantine or the run reports is touched, and no network is used.
"""
from __future__ import annotations

import csv
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .config import Config
from .orphans import _resolve_local_path
from .pdfcheck import EOF_MARKER, EOF_WINDOW, MAGIC_WINDOW, has_pdf_magic
from .storage import iter_manifest_rows

#: Default number of trailing bytes searched for the last `%%EOF` of a file
#: that fails the window rule (the measurement, not the rule).
DEFAULT_TAIL_BYTES = 1024 * 1024

CSV_FIELDS = ("bank_code", "doc_type", "doc_id", "reason", "size", "eof_from_end",
              "local_path")


@dataclass
class AuditSummary:
    rows_checked: int = 0
    ok: int = 0
    failing: int = 0
    by_reason: dict = field(default_factory=dict)
    by_series: dict = field(default_factory=dict)        # "bank doc_type" -> failing count
    eof_from_end: dict = field(default_factory=dict)     # bucket -> count (no-eof-in-window only)
    min_pdf_bytes: int = 0
    eof_window: int = EOF_WINDOW
    csv_path: str = ""


def _is_pdf_row(row: dict, path: Optional[Path]) -> bool:
    mime = (row.get("mime_type") or "").split(";")[0].strip().lower()
    if mime == "application/pdf":
        return True
    lp = row.get("local_path") or ""
    return lp.lower().endswith(".pdf") or bool(path and path.suffix.lower() == ".pdf")


def _bucket(distance: Optional[int]) -> str:
    """Where the last `%%EOF` sits, from the end, for the summary histogram."""
    if distance is None:
        return "none in tail"
    for limit in (4096, 16384, 65536, 262144):
        if distance <= limit:
            return f"<= {limit}"
    return "> 262144"


def audit_file(path: Path, *, min_bytes: int, tail_bytes: int = DEFAULT_TAIL_BYTES
               ) -> tuple[Optional[str], int, Optional[int]]:
    """(reason or None, size, eof_from_end) for one file, by today's rules.

    Same order as `pdfcheck.check_pdf_bytes`: magic, size floor, `%%EOF` in the
    last EOF_WINDOW bytes. `eof_from_end` is the distance from the end of the
    file to the end of its last `%%EOF` within the last `tail_bytes` bytes
    (None when there is none there); computed for every file."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return "missing", 0, None
    with path.open("rb") as fh:
        head = fh.read(MAGIC_WINDOW)
        fh.seek(max(0, size - max(tail_bytes, EOF_WINDOW)))
        tail = fh.read()
    pos = tail.rfind(EOF_MARKER)
    eof_from_end = None if pos < 0 else len(tail) - (pos + len(EOF_MARKER))
    if not has_pdf_magic(head):
        return "not-pdf", size, eof_from_end
    if size < min_bytes:
        return "too-small", size, eof_from_end
    if EOF_MARKER not in tail[-EOF_WINDOW:]:
        return "no-eof-in-window", size, eof_from_end
    return None, size, eof_from_end


def audit_pdfs(cfg: Config, *, banks: Optional[Iterable[str]] = None,
               csv_path: Optional[str] = None, tail_bytes: int = DEFAULT_TAIL_BYTES,
               progress_every: int = 5000) -> AuditSummary:
    """Check every `.pdf` artifact of the manifests (optionally some banks)."""
    if tail_bytes < EOF_WINDOW:
        raise ValueError(f"tail_bytes must be >= {EOF_WINDOW}")
    wanted = {b.strip().lower() for b in banks or () if b.strip()}
    out = Path(csv_path) if csv_path else cfg.reports_dir / "pdf_audit.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = AuditSummary(min_pdf_bytes=cfg.min_pdf_bytes, csv_path=str(out))
    reasons: Counter = Counter()
    series: Counter = Counter()
    buckets: Counter = Counter()
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    with tmp.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in iter_manifest_rows(cfg):
            bank = (row.get("bank_code") or "").lower()
            if wanted and bank not in wanted:
                continue
            lp = row.get("local_path") or ""
            path = _resolve_local_path(cfg, lp) if lp else None
            if not lp or not _is_pdf_row(row, path):
                continue
            summary.rows_checked += 1
            if path is None:
                reason, size, eof = "missing", 0, None
            else:
                reason, size, eof = audit_file(path, min_bytes=cfg.min_pdf_bytes,
                                               tail_bytes=tail_bytes)
            if reason is None:
                summary.ok += 1
            else:
                summary.failing += 1
                reasons[reason] += 1
                series[f"{bank} {row.get('doc_type') or ''}"] += 1
                if reason == "no-eof-in-window":
                    buckets[_bucket(eof)] += 1
                writer.writerow({"bank_code": bank, "doc_type": row.get("doc_type") or "",
                                 "doc_id": row.get("doc_id") or "", "reason": reason,
                                 "size": size, "eof_from_end": "" if eof is None else eof,
                                 "local_path": lp})
            if progress_every and summary.rows_checked % progress_every == 0:
                print(f"[audit-pdfs] {summary.rows_checked} checked, {summary.failing} failing",
                      file=sys.stderr, flush=True)
    os.replace(tmp, out)
    summary.by_reason = dict(reasons.most_common())
    summary.by_series = dict(series.most_common())
    summary.eof_from_end = dict(sorted(buckets.items()))
    return summary
