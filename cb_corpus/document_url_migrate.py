"""`migrate-document-url`: rename the stored key `pdf_url` -> `document_url`.

One-shot for the field rename of 2026-11-19 (`DocRecord.pdf_url` became
`DocRecord.document_url`; the value, and so every doc_id, is unchanged).
New rows are written with `document_url`; this command renames the key in
the rows already stored, so the transition fallback (models.row_url /
models.upgrade_row) can be removed afterwards.

Files handled (the persistent state that stores the key):

  - every per-bank manifest `data/manifest/<bank>.jsonl`: the row's key, and
    the key of each entry of an optional `retired_duplicates` audit list
    (replace-artifact);
  - `data/download_errors.jsonl` (the recover-downloads inventory, read back
    by `recover-downloads`).

Not handled, and why: `wp_dates_index.jsonl` (`evidence_url`) and
`download_quarantine.jsonl` (`url`) never stored the key; the CSV reports
under `data/reports/` are rewritten whole by their own command on the next
run and never read back; a recover-downloads `--candidates` file is the
operator's own input, read with the old key as fallback (`dead_pdf_url`).

Each file is processed under its sidecar lock (filelock.locked_sidecar, the
manifests' and the download-error log's writers take the same lock), re-read
fresh under it. Per row: the key is renamed IN PLACE (same position, every
value unchanged, `json.dumps(..., ensure_ascii=False)` like every writer);
a row already using the new name is kept byte-identical; a row carrying BOTH
names is an error: it is left untouched and reported, never guessed at. The
file is rewritten (temp + os.replace) only when a row changed, so a second
run does nothing. Dry run by default: nothing is written, the sidecar lock
is not taken (a read of a live file may see a partly written last line,
which the counts report as `torn`).

Malformed lines: a manifest follows storage's rewrite rule (a torn FINAL
line is saved to `<file>.torn` and dropped -- storage._read_rows_for_rewrite
-- a malformed earlier line is a hard stop). A download-error log line that
does not parse is kept verbatim (its reader skips it, see recover.py).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .config import Config
from .filelock import locked_sidecar
from .models import LEGACY_URL_KEY, URL_KEY
from .storage import _read_rows_for_rewrite

DOWNLOAD_ERRORS = "download_errors.jsonl"


@dataclass
class FileCounts:
    """Rows of one file. `renamed`: the old key renamed (written with
    --write); `already_new`: already `document_url`; `both`: both names
    present, left untouched (an error, see `both_ids`); `no_url`: neither
    name (left untouched); `torn`: malformed final line (dropped on a
    --write manifest rewrite); `unparsed`: malformed download-error log
    lines kept verbatim."""
    path: str
    renamed: int = 0
    already_new: int = 0
    both: int = 0
    no_url: int = 0
    torn: int = 0
    unparsed: int = 0
    written: bool = False
    both_ids: list[str] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return self.renamed + self.already_new + self.both + self.no_url


def _classify(row: dict) -> str:
    """'renamed' | 'already_new' | 'both' | 'no_url' for one row (the nested
    retired_duplicates entries count with their row: an entry with both names
    makes the row 'both'; an entry with the old name makes it 'renamed')."""
    dicts = [row, *(e for e in (row.get("retired_duplicates") or [])
                    if isinstance(e, dict))]
    if any(URL_KEY in d and LEGACY_URL_KEY in d for d in dicts):
        return "both"
    if any(LEGACY_URL_KEY in d for d in dicts):
        return "renamed"
    return "already_new" if URL_KEY in row else "no_url"


def _rename(row: dict) -> dict:
    """A copy of `row` with the old key renamed, positions and values kept
    (top level and retired_duplicates entries)."""
    def ren(d: dict) -> dict:
        return {(URL_KEY if k == LEGACY_URL_KEY else k): v for k, v in d.items()}
    out = ren(row)
    if isinstance(out.get("retired_duplicates"), list):
        out["retired_duplicates"] = [ren(e) if isinstance(e, dict) else e
                                     for e in out["retired_duplicates"]]
    return out


def _dump(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False)


def _read_lines_dry(path: Path, counts: FileCounts, *, manifest: bool) -> list[tuple[str, Optional[dict]]]:
    """Unlocked read for the dry run: (raw line, parsed row or None). Mirrors
    the --write readers without writing anything (no `.torn` file)."""
    raw = path.read_bytes()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    out: list[tuple[str, Optional[dict]]] = []
    for i, ln in enumerate(lines):
        try:
            row = json.loads(ln)
            if not isinstance(row, dict):
                raise ValueError("not a JSON object")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            if manifest:
                if i == len(lines) - 1:
                    counts.torn += 1
                    break
                raise ValueError(f"corrupt manifest line (not the final line -> "
                                 f"not a torn append, hard stop): {path}: {exc}") from exc
            counts.unparsed += 1
            out.append((ln.decode("utf-8", errors="replace"), None))
            continue
        out.append((ln.strip().decode("utf-8"), row))
    return out


def _read_lines_locked(path: Path, counts: FileCounts, *, manifest: bool) -> list[tuple[str, Optional[dict]]]:
    """Under-lock read for --write. A manifest goes through storage's own
    rewrite reader (torn final line -> `<file>.torn`, dropped; earlier
    malformed line -> ValueError); the download-error log keeps a malformed
    line verbatim."""
    if manifest:
        n_lines = sum(1 for ln in path.read_bytes().splitlines() if ln.strip())
        pairs = _read_rows_for_rewrite(path)
        if len(pairs) < n_lines:
            counts.torn += n_lines - len(pairs)
        return [(raw, json.loads(raw)) for raw, _ in pairs]
    out: list[tuple[str, Optional[dict]]] = []
    for ln in path.read_bytes().splitlines():
        if not ln.strip():
            continue
        try:
            row = json.loads(ln)
            if not isinstance(row, dict):
                raise ValueError
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            counts.unparsed += 1
            out.append((ln.decode("utf-8", errors="surrogateescape"), None))
            continue
        out.append((ln.strip().decode("utf-8"), row))
    return out


def _migrate_file(path: Path, *, write: bool, manifest: bool) -> FileCounts:
    counts = FileCounts(path=str(path))

    def plan(lines: list[tuple[str, Optional[dict]]]) -> list[str]:
        out: list[str] = []
        for raw, row in lines:
            if row is None:
                out.append(raw)
                continue
            kind = _classify(row)
            setattr(counts, kind, getattr(counts, kind) + 1)
            if kind == "both":
                counts.both_ids.append(str(row.get("doc_id") or row.get("ts") or "?"))
            out.append(_dump(_rename(row)) if kind == "renamed" else raw)
        return out

    if not write:
        plan(_read_lines_dry(path, counts, manifest=manifest))
        return counts
    with locked_sidecar(path):
        if not path.exists():
            return counts
        out = plan(_read_lines_locked(path, counts, manifest=manifest))
        # A torn manifest tail is dropped by the rewrite, as storage's other
        # locked rewrites do (only when a row remains, like apply_row_updates).
        if counts.renamed or (manifest and counts.torn and out):
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8", errors="surrogateescape") as fh:
                for line in out:
                    fh.write(line + "\n")
            os.replace(tmp, path)
            counts.written = True
    return counts


def migrate_document_url(cfg: Config, *, write: bool = False,
                         bank_codes: Optional[Iterable[str]] = None) -> list[FileCounts]:
    """Rename `pdf_url` -> `document_url` in every per-bank manifest (or the
    given banks), then in `download_errors.jsonl` (only when no bank filter
    is given). Dry run unless `write`. Returns one FileCounts per file found.
    A legacy single `data/manifest.jsonl` is not handled: any Storage start
    splits it into per-bank files first (storage.migrate_legacy_layout)."""
    if bank_codes:
        files = [cfg.manifest_file(b) for b in bank_codes]
    else:
        files = sorted(cfg.manifest_dir.glob("*.jsonl")) if cfg.manifest_dir.is_dir() else []
        if cfg.manifest_path.exists():
            print(f"[migrate-document-url] WARNING: legacy {cfg.manifest_path} not "
                  f"handled (split it first: any Storage start does)", file=sys.stderr)
    for f in files:
        if not f.exists():
            print(f"[migrate-document-url] WARNING: no manifest {f} (unknown bank code?)",
                  file=sys.stderr)
    results = [_migrate_file(f, write=write, manifest=True) for f in files if f.exists()]
    errors = cfg.data_dir / DOWNLOAD_ERRORS
    if not bank_codes and errors.exists():
        results.append(_migrate_file(errors, write=write, manifest=False))
    return results


def summary(results: list[FileCounts]) -> dict:
    """Totals over every file (rows, renamed, already_new, both, no_url,
    torn, unparsed, files_written)."""
    keys = ("renamed", "already_new", "both", "no_url", "torn", "unparsed")
    tot = {k: sum(getattr(r, k) for r in results) for k in keys}
    tot["rows"] = sum(r.rows for r in results)
    tot["files"] = len(results)
    tot["files_written"] = sum(1 for r in results if r.written)
    return tot


def report_lines(results: list[FileCounts], *, write: bool) -> list[str]:
    head = "migrate-document-url" + ("" if write else " (dry-run)")
    lines = []
    for r in results:
        d = asdict(r)
        flag = " WRITTEN" if r.written else ""
        lines.append(f"{head}: {Path(r.path).name}: rows={r.rows} renamed={r.renamed} "
                     f"already_new={r.already_new} both={r.both} no_url={r.no_url} "
                     f"torn={r.torn} unparsed={r.unparsed}{flag}"
                     + (f" both_ids={','.join(d['both_ids'])}" if r.both_ids else ""))
    t = summary(results)
    lines.append(f"{head}: TOTAL files={t['files']} rows={t['rows']} renamed={t['renamed']} "
                 f"already_new={t['already_new']} both={t['both']} no_url={t['no_url']} "
                 f"torn={t['torn']} unparsed={t['unparsed']} files_written={t['files_written']}")
    return lines
