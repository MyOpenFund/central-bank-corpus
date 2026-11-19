"""Shape check for bytes that claim to be a PDF (issue #21).

An HTTP 200 is not a document: a consent wall, a CDN "document moved" page
or a bank outage page served with status 200 used to be stored as the PDF
itself, and once indexed its doc_id short-circuited every later save -- the
real bytes were never fetched again. A process killed mid-write, or a full
disk, could likewise leave a truncated file that the disaster-recovery
reindex then welded into the corpus. This module is the ONE check every
path applies before bytes become a PDF artifact:

  - Storage.save(), per candidate URL, BEFORE the sha256 dedup (an error
    page must never become a dedup owner);
  - Storage.reindex(), on a `.pdf` file found on disk;
  - every headless-Chrome render output (storage.render_pdf_checked:
    Storage.save(), convert-html, retry-html), and a PDF retry-html finds
    already on disk before adopting it;
  - recover._verify_local_pdf(), for the magic part only (its own 20 KB
    floor judges externally hunted candidates, a different question).

It is a SHAPE check, not a parser: it proves the bytes are not obviously
something else (HTML, a stub, a truncated download), never that a PDF
reader will open them. Cheap on purpose: it looks at the first and last
few KB only.
"""
from __future__ import annotations

from typing import Optional

PDF_MAGIC = b"%PDF"
# The header may be preceded by junk: PDF readers (Acrobat's documented
# tolerance) look for it in the first 1,024 bytes, so a valid file after
# leading whitespace or a BOM must pass (spec 2026-11 integrity wave, A1).
MAGIC_WINDOW = 1024
EOF_MARKER = b"%%EOF"
# Where the end-of-file marker must appear. A complete PDF ends with
# `%%EOF` optionally followed by a line end, and some producers append a
# few padding/NUL bytes; 2,048 bytes leaves room for that while still
# catching a download cut off anywhere before its last 2 KB.
EOF_WINDOW = 2048


def has_pdf_magic(head: bytes) -> bool:
    """True when `%PDF` starts (and ends) within the first MAGIC_WINDOW
    bytes of `head` (pass at least the file's first MAGIC_WINDOW bytes)."""
    return PDF_MAGIC in head[:MAGIC_WINDOW]


def pdf_problem(head: bytes, tail: bytes, size: int, *, min_bytes: int,
                require_eof: bool) -> Optional[str]:
    """The ONE rule order, on parts of a file: None when it has the shape of
    a PDF, else a reason code: "not-pdf", "too-small" or "no-eof-in-window".

    `head` holds at least the first MAGIC_WINDOW bytes (or the whole file),
    `tail` at least the last EOF_WINDOW bytes (or the whole file), `size` the
    full length. `check_pdf_bytes` (new writes) and `pdf_audit` (stored files,
    read by head and tail only) both decide through here, so they cannot
    drift apart."""
    if not has_pdf_magic(head):
        return "not-pdf"
    if size < min_bytes:
        return "too-small"
    if require_eof and EOF_MARKER not in tail[-EOF_WINDOW:]:
        return "no-eof-in-window"
    return None


def check_pdf_bytes(data: bytes, *, min_bytes: int,
                    require_eof: bool) -> Optional[str]:
    """None when `data` has the shape of a PDF, else a one-line reason.

    Rules, in order (`pdf_problem`): `%PDF` within the first MAGIC_WINDOW
    bytes (an HTML page fails here); at least `min_bytes` long (a stub or a
    cut-off body); and, when `require_eof`, `%%EOF` within the last
    EOF_WINDOW bytes (a truncated download). The thresholds come from Config
    (min_pdf_bytes, pdf_require_eof) so an operator can relax them without a
    code change."""
    code = pdf_problem(data, data, len(data), min_bytes=min_bytes, require_eof=require_eof)
    if code == "not-pdf":
        return f"not a PDF (no {PDF_MAGIC.decode()} in the first {MAGIC_WINDOW} bytes)"
    if code == "too-small":
        return f"too small for a PDF ({len(data)} bytes < {min_bytes})"
    if code == "no-eof-in-window":
        return (f"truncated PDF (no {EOF_MARKER.decode()} in the last "
                f"{EOF_WINDOW} bytes)")
    return None
