"""Second-pass HTML -> PDF retry with escalating strategies.

After `convert-html` runs, the manifest still has some entries with
mime_type=text/html — these are the cases where Chrome failed (timeout, page
errored, transient network glitch). This module walks those leftovers and
retries with progressively more patient strategies. URLs that resist all
strategies are written to `data/failed_urls.txt` for manual handling.

Strategies tried in order:
  1. Chrome with 120s timeout (vs default 60s).
  2. Chrome with 120s timeout + --virtual-time-budget (wait for JS to settle).
  3. Chrome with 180s timeout, fresh user-data-dir per attempt.
"""
from __future__ import annotations

import concurrent.futures
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .config import Config
from .htmlpdf import RenderRefused, chrome_print_to_pdf, find_chrome, render_allowed
from .storage import iter_manifest_rows, apply_row_updates


def _try_strategy(chrome: str, url: str, output: Path,
                  *, timeout: float,
                  virtual_time_budget_ms: Optional[int],
                  user_data_dir: str,
                  cfg: Optional[Config] = None, resolver=None) -> bool:
    """One Chrome attempt through the shared guarded launcher
    (htmlpdf.chrome_print_to_pdf): a private-network URL raises
    RenderRefused, Chrome is never started. `resolver`: the same name
    resolver as retry_failed's pre-check (None = getaddrinfo)."""
    extra = ((f"--virtual-time-budget={virtual_time_budget_ms}",)
             if virtual_time_budget_ms is not None else ())
    try:
        chrome_print_to_pdf(chrome, url, output, user_data_dir, timeout,
                            extra_args=extra, cfg=cfg, resolver=resolver)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return output.exists() and output.stat().st_size > 0


def _try_all_strategies(chrome: str, url: str, pdf_path: Path,
                        user_data_dir: str, cfg: Optional[Config] = None,
                        resolver=None) -> bool:
    """Run the 3 escalating strategies. Returns True on first success.
    RenderRefused (render guard) propagates: no strategy may launch Chrome."""
    if _try_strategy(chrome, url, pdf_path,
                     timeout=120, virtual_time_budget_ms=None,
                     user_data_dir=user_data_dir, cfg=cfg, resolver=resolver):
        return True
    if _try_strategy(chrome, url, pdf_path,
                     timeout=120, virtual_time_budget_ms=15000,
                     user_data_dir=user_data_dir, cfg=cfg, resolver=resolver):
        return True
    # Strategy 3: fresh profile.
    with tempfile.TemporaryDirectory(prefix="cbc_retry_fresh_") as fresh:
        return _try_strategy(chrome, url, pdf_path,
                             timeout=180, virtual_time_budget_ms=15000,
                             user_data_dir=fresh, cfg=cfg, resolver=resolver)


def retry_failed(config: Optional[Config] = None,
                 parallelism: int = 4, resolver=None) -> dict[str, int]:
    """Retry every text/html row in the manifest. Update + write failures.

    `parallelism` runs N concurrent Chrome instances (each with its own
    user-data-dir) — roughly N× speedup, bottlenecked by per-domain rate
    limits and disk/CPU.

    Detects rows whose PDF already exists on disk (e.g. recovered by a
    previously-killed retry) and updates the manifest without re-rendering.

    Returns {status: count}. Statuses:
      recovered    - PDF rendered/found; manifest updated.
      already-pdf  - PDF already existed on disk (no Chrome needed).
      still-fail   - all strategies failed; URL added to data/failed_urls.txt.
      refused      - URL refused by the render guard (htmlpdf.render_allowed:
                     private network, non-http scheme, ...); Chrome never
                     launched, row left untouched. Always present in the result.
      skip         - row is no longer text/html (already converted).

    `resolver`: name resolver for the render guard (default getaddrinfo).
    """
    cfg = config or Config()
    chrome = find_chrome()
    if chrome is None:
        raise RuntimeError("no Chrome / Chromium binary found")

    counts: dict[str, int] = {"refused": 0}
    counts_lock = threading.Lock()
    failures: list[tuple[str, str]] = []
    failures_lock = threading.Lock()
    updates: dict[str, tuple[str, str, Optional[str]]] = {}
    updates_lock = threading.Lock()

    rows = list(iter_manifest_rows(cfg))
    if not rows:
        return {"refused": 0}

    # Build worklist of HTML rows + identify already-rendered PDFs.
    worklist: list[tuple[dict, Path, Path]] = []
    for row in rows:
        if row.get("mime_type") != "text/html":
            counts["skip"] = counts.get("skip", 0) + 1
            continue
        local = row.get("local_path")
        url = row.get("pdf_url")
        if not local or not url:
            counts["skip"] = counts.get("skip", 0) + 1
            continue
        html_path = Path(local)
        pdf_path = html_path.with_suffix(".pdf")
        # Reconcile: if a previously-killed retry already produced the PDF,
        # just update the manifest. Avoids redoing 4+ minutes of Chrome work.
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            updates[row["doc_id"]] = ("application/pdf", str(pdf_path),
                                      str(html_path) if html_path.exists() else None)
            counts["already-pdf"] = counts.get("already-pdf", 0) + 1
            continue
        reason = render_allowed(url, cfg, resolver=resolver)
        if reason:
            counts["refused"] += 1
            print(f"[retry-html] render refused for {url}: {reason}",
                  file=sys.stderr, flush=True)
            continue
        worklist.append((row, html_path, pdf_path))

    print(f"[retry-html] worklist: {len(worklist)} HTMLs need rendering, "
          f"{counts.get('already-pdf', 0)} already had PDFs",
          file=sys.stderr, flush=True)

    # Allocate one user-data-dir per worker thread to avoid Chrome profile lock.
    udds: list[str] = []
    tmpdirs: list[tempfile.TemporaryDirectory] = []
    for _ in range(parallelism):
        td = tempfile.TemporaryDirectory(prefix="cbc_retry_")
        tmpdirs.append(td)
        udds.append(td.name)
    # Each thread sticks to one UDD.
    local_data = threading.local()
    udd_iter = iter(udds)
    assign_lock = threading.Lock()

    def get_udd() -> str:
        if not hasattr(local_data, "udd"):
            with assign_lock:
                local_data.udd = next(udd_iter)
        return local_data.udd

    def process(args: tuple[dict, Path, Path]) -> None:
        row, html_path, pdf_path = args
        udd = get_udd()
        try:
            ok = _try_all_strategies(chrome, row["pdf_url"], pdf_path, udd, cfg,
                                     resolver=resolver)
        except RenderRefused as exc:     # DNS changed since the pre-check
            with counts_lock:
                counts["refused"] += 1
            print(f"[retry-html] {exc}", file=sys.stderr, flush=True)
            return
        if ok:
            with updates_lock:
                updates[row["doc_id"]] = ("application/pdf", str(pdf_path),
                                          str(html_path) if html_path.exists() else None)
            with counts_lock:
                counts["recovered"] = counts.get("recovered", 0) + 1
        else:
            with failures_lock:
                failures.append((row["pdf_url"], row.get("bank_code", "?")))
            with counts_lock:
                counts["still-fail"] = counts.get("still-fail", 0) + 1
        with counts_lock:
            done = (counts.get("recovered", 0) + counts.get("still-fail", 0))
        print(f"[retry-html] {done}/{len(worklist)} {row['pdf_url'][:80]} -> "
              f"{'recovered' if ok else 'FAIL'}",
              file=sys.stderr, flush=True)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as ex:
            list(ex.map(process, worklist))
    finally:
        for td in tmpdirs:
            td.cleanup()

    # Keyed, lock-protected write-back (storage.apply_row_updates): replaces
    # the manual fresh-re-read merge this code used to do — that pattern
    # narrowed the race window to milliseconds but never closed it (#18).
    row_by_id = {r.get("doc_id"): r for r in rows}
    row_updates: dict[str, dict] = {}
    for did, (mime, local, html_path) in updates.items():
        row = row_by_id[did]
        row["mime_type"], row["local_path"] = mime, local
        if html_path:
            row["html_path"] = html_path
        row_updates[did] = row
    if row_updates:
        apply_row_updates(cfg, row_updates)

    # Write failed URLs to a file the user can pick up.
    if failures:
        out = cfg.data_dir / "failed_urls.txt"
        with out.open("w") as fh:
            fh.write("# URLs that failed every Chrome render strategy.\n")
            fh.write("# Format:  <bank_code>\\t<url>\n")
            for url, code in failures:
                fh.write(f"{code}\t{url}\n")
        print(f"[retry-html] wrote {len(failures)} failed URLs to {out}",
              file=sys.stderr, flush=True)

    if counts["refused"]:
        print(f"[retry-html] render refused: {counts['refused']} (private network, "
              f"unresolvable host or non-http URL(s); rows left as HTML)",
              file=sys.stderr, flush=True)
    return counts
