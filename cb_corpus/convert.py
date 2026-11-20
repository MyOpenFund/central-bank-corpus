"""Retroactive HTML -> PDF migration.

Walks the manifest, finds every entry whose on-disk artifact is HTML, renders
the live URL via headless Chrome, replaces the .html with the .pdf, and
rewrites the manifest entry (mime_type, local_path).

Idempotent: re-running on an already-converted manifest does nothing.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Optional

from .config import Config
from .htmlpdf import RenderRefused, find_chrome, render_allowed, render_url_to_pdf
from .models import URL_KEY
from .storage import apply_row_updates, iter_manifest_rows, render_pdf_checked


def _ext_swap(path: Path, new_ext: str) -> Path:
    return path.with_suffix(new_ext)


def convert_existing(config: Optional[Config] = None,
                     dry_run: bool = False, resolver=None) -> dict[str, int]:
    """Convert every text/html entry in the manifest to PDF.

    Returns {status: count}. Statuses:
      converted     - HTML successfully rendered to PDF on disk; manifest updated.
      skip-not-html - entry mime != text/html (nothing to do).
      skip-missing  - on-disk file no longer exists.
      skip-already  - already a .pdf next to the manifest entry.
      error         - Chrome conversion failed, or its output failed the
                      PDF check (no .pdf is left behind either way).
      refused       - URL refused by the render guard (htmlpdf.render_allowed:
                      private network, non-http scheme, ...); Chrome never
                      launched, row untouched. Always present (also in dry run).

    `resolver`: name resolver for the render guard (default getaddrinfo).
    """
    cfg = config or Config()
    if find_chrome() is None:
        raise RuntimeError("no Chrome / Chromium binary found")
    snapshot: list[dict] = list(iter_manifest_rows(cfg))
    if not snapshot:
        return {"empty": 0, "refused": 0}

    counts: dict[str, int] = {"refused": 0}
    updates: dict[str, dict] = {}
    n = 0
    # One Chrome profile reused across renders — avoids per-call cold-start
    # (10x speedup vs fresh tempdir per render).
    with tempfile.TemporaryDirectory(prefix="cbc_chrome_convert_") as udd:
        for row in snapshot:
            status = _convert_one(row, user_data_dir=udd, dry_run=dry_run,
                                  cfg=cfg, resolver=resolver)
            counts[status] = counts.get(status, 0) + 1
            if status == "converted" and not dry_run:
                updates[row["doc_id"]] = row
            n += 1
            if n % 50 == 0:
                print(f"[convert-html] processed {n} ({dict(counts)})",
                      file=sys.stderr, flush=True)

    if not dry_run and updates:
        # Keyed, lock-protected write-back: rows appended during the (hours
        # long) render loop above survive — see storage.apply_row_updates.
        apply_row_updates(cfg, updates)
    if counts["refused"]:
        print(f"[convert-html] render refused: {counts['refused']} (private network, "
              f"unresolvable host or non-http URL(s); rows left as HTML)",
              file=sys.stderr, flush=True)
    return counts


def _convert_one(row: dict, *, dry_run: bool,
                 user_data_dir: Optional[str] = None,
                 cfg: Optional[Config] = None, resolver=None) -> str:
    if row.get("mime_type") != "text/html":
        return "skip-not-html"
    cfg = cfg or Config()
    # The stored `raw/...`, resolved against the data root, never the CWD.
    html_path = cfg.resolve_stored_path(row.get("local_path"))
    if html_path is None:
        return "skip-missing"
    pdf_path = _ext_swap(html_path, ".pdf")
    if pdf_path.exists() and not html_path.exists():
        return "skip-already"
    if not html_path.exists():
        return "skip-missing"
    reason = render_allowed(row.get(URL_KEY) or "", cfg, resolver=resolver)
    if reason:
        print(f"[convert-html] render refused for {row.get(URL_KEY)}: {reason}",
              file=sys.stderr, flush=True)
        return "refused"
    if dry_run:
        return "converted"  # would-be
    # Through the checked helper (issue #21): Chrome writes a temp sibling,
    # the output must pass the PDF check before it replaces pdf_path, so a
    # killed or failed render never leaves a partial .pdf behind.
    try:
        render_pdf_checked(
            pdf_path, cfg,
            lambda tmp: render_url_to_pdf(row.get(URL_KEY), tmp,
                                          user_data_dir=user_data_dir,
                                          cfg=cfg, resolver=resolver),
            url=row.get(URL_KEY), label="convert-html")
    except RenderRefused:
        return "refused"
    except Exception:
        return "error"
    # Keep the source HTML alongside the rendered PDF.
    row["mime_type"] = "application/pdf"
    row["local_path"] = cfg.stored_path(pdf_path)
    if html_path.exists():
        row["html_path"] = cfg.stored_path(html_path)
    return "converted"
