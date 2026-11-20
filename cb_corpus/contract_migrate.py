"""`migrate-contract`: rewrite the stored manifests in the documents-contract form.

One-shot for the documents contract (vault-contract 1.0.0, 2026-11-20). New
rows are written in the contract form (DocRecord.to_row) and rows read by any
command are upgraded in memory (models.upgrade_row); this command rewrites
the rows already stored, so the transition fallbacks (models.row_bank,
Config.resolve_stored_path's `data/` branch, upgrade_row) and the vault's
own transition mode can be removed afterwards. Per row
(models.contract_upgrade, every key keeping its position, every other value
unchanged):

  - `corpus: "central-bank"` added as the first key;
  - `bank_code` renamed `source_code` (`pdf_url` -> `document_url` too, if any
    row still has it);
  - the `data/` prefix dropped from `local_path` and `html_path` (paths become
    relative to the data root); an ABSOLUTE path under the data root is made
    relative too, any other absolute path is an error (reported, left as is);
  - an undated row: `date_precision` nulled and `date_kind` dropped (no date,
    no precision, no kind -- the vault would only notice them).

It also writes `vocabulary.json` at the data root (vocabulary.write_vocabulary).

Files handled: every per-bank manifest `data/manifest/<bank>.jsonl`. Not
handled, and why: `download_errors.jsonl` entries (and the cadence files) are
not documents rows -- their `bank_code` is this repo's internal name, read
back by recover-downloads under that name; nothing else stored carries a
contract key.

Same mechanics as `migrate-document-url` (document_url_migrate): each file is
processed under its sidecar lock, re-read fresh under it; a row already in
the contract form is kept byte-identical; a row carrying BOTH `bank_code` and
`source_code` is an error, left untouched and reported, never guessed at; the
file is rewritten (temp + os.replace) only when a row changed, so a second
run does nothing. Dry run by default: nothing is written, no lock is taken.
A torn final manifest line follows storage's rewrite rule (saved to
`<file>.torn`, dropped); a malformed earlier line is a hard stop.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .config import Config
from .document_url_migrate import _read_lines_dry, _read_lines_locked
from .filelock import locked_sidecar
from .models import PATH_KEYS, contract_upgrade
from .vocabulary import VOCABULARY_FILE, vocabulary_bytes, write_vocabulary

#: contract_upgrade change names -> FileCounts attribute.
_CHANGE_FIELDS = {"document_url": "document_url", "corpus": "corpus",
                  "source_code": "source_code", "local_path": "local_path",
                  "html_path": "html_path", "date_precision": "date_precision",
                  "date_kind": "date_kind"}


@dataclass
class FileCounts:
    """Rows of one manifest. `upgraded`: rows rewritten (with --write);
    `unchanged`: already in the contract form; `both`: `bank_code` AND
    `source_code`, left untouched (an error, see `both_ids`). Per change, the
    rows it applies to: `corpus` added, `source_code` renamed from bank_code,
    `document_url` renamed from pdf_url, `local_path` / `html_path` made
    relative to the data root, `date_precision` nulled / `date_kind` dropped
    on an undated row. `bad_path`: an absolute path outside the data root
    (left as is, an error, see `bad_path_ids`). `torn`: malformed final line
    (dropped on a --write rewrite)."""
    path: str
    upgraded: int = 0
    unchanged: int = 0
    both: int = 0
    corpus: int = 0
    source_code: int = 0
    document_url: int = 0
    local_path: int = 0
    html_path: int = 0
    date_precision: int = 0
    date_kind: int = 0
    bad_path: int = 0
    torn: int = 0
    unparsed: int = 0          # never set for a manifest (kept for the shared readers)
    written: bool = False
    both_ids: list[str] = field(default_factory=list)
    bad_path_ids: list[str] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return self.upgraded + self.unchanged + self.both


@dataclass
class MigrationResult:
    files: list[FileCounts]
    #: vocabulary.json: "written" | "unchanged" (--write), "would-write" |
    #: "unchanged" (dry run).
    vocabulary: str


def _relativize(row: dict, cfg: Config) -> tuple[list[str], bool]:
    """Absolute `local_path`/`html_path` under the data root -> relative.
    Returns (keys changed, any absolute path left outside the root)."""
    changed, bad = [], False
    for key in PATH_KEYS:
        v = row.get(key)
        if isinstance(v, str) and v.startswith("/"):
            try:
                row[key] = cfg.stored_path(Path(v))
                changed.append(key)
            except ValueError:
                bad = True
    return changed, bad


def _migrate_file(path: Path, cfg: Config, *, write: bool) -> FileCounts:
    counts = FileCounts(path=str(path))

    def plan(lines: list[tuple[str, Optional[dict]]]) -> list[str]:
        out: list[str] = []
        for raw, row in lines:
            if row is None:                    # not for a manifest; kept verbatim
                out.append(raw)
                continue
            did = str(row.get("doc_id") or "?")
            new = copy.deepcopy(row)
            changes = contract_upgrade(new, undated=True)
            if "both_source_names" in changes:
                counts.both += 1
                counts.both_ids.append(did)
                out.append(raw)
                continue
            rel, bad = _relativize(new, cfg)
            changes += [k for k in rel if k not in changes]
            if bad:
                counts.bad_path += 1
                counts.bad_path_ids.append(did)
            for name in changes:
                attr = _CHANGE_FIELDS[name]
                setattr(counts, attr, getattr(counts, attr) + 1)
            if changes:
                counts.upgraded += 1
                out.append(json.dumps(new, ensure_ascii=False))
            else:
                counts.unchanged += 1
                out.append(raw)
        return out

    if not write:
        plan(_read_lines_dry(path, counts, manifest=True))
        return counts
    with locked_sidecar(path):
        if not path.exists():
            return counts
        out = plan(_read_lines_locked(path, counts, manifest=True))
        if counts.upgraded or (counts.torn and out):
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8", errors="surrogateescape") as fh:
                for line in out:
                    fh.write(line + "\n")
            os.replace(tmp, path)
            counts.written = True
    return counts


def migrate_contract(cfg: Config, *, write: bool = False,
                     bank_codes: Optional[Iterable[str]] = None) -> MigrationResult:
    """Upgrade every per-bank manifest (or the given banks) to the documents
    contract, then write `vocabulary.json` at the data root. Dry run unless
    `write`. A legacy single `data/manifest.jsonl` is not handled (any
    Storage start splits it into per-bank files first)."""
    if bank_codes:
        files = [cfg.manifest_file(b) for b in bank_codes]
    else:
        files = sorted(cfg.manifest_dir.glob("*.jsonl")) if cfg.manifest_dir.is_dir() else []
        if cfg.manifest_path.exists():
            print(f"[migrate-contract] WARNING: legacy {cfg.manifest_path} not "
                  f"handled (split it first: any Storage start does)", file=sys.stderr)
    for f in files:
        if not f.exists():
            print(f"[migrate-contract] WARNING: no manifest {f} (unknown bank code?)",
                  file=sys.stderr)
    results = [_migrate_file(f, cfg, write=write) for f in files if f.exists()]
    vpath = cfg.data_dir / VOCABULARY_FILE
    try:
        current = vpath.read_bytes()
    except FileNotFoundError:
        current = None
    if current == vocabulary_bytes():
        vocab = "unchanged"
    elif write:
        write_vocabulary(cfg.data_dir)
        vocab = "written"
    else:
        vocab = "would-write"
    return MigrationResult(files=results, vocabulary=vocab)


_TOTAL_KEYS = ("upgraded", "unchanged", "both", "corpus", "source_code", "document_url",
               "local_path", "html_path", "date_precision", "date_kind", "bad_path", "torn")


def summary(result: MigrationResult) -> dict:
    """Totals over every file (rows, files, files_written and every count)."""
    tot = {k: sum(getattr(r, k) for r in result.files) for k in _TOTAL_KEYS}
    tot["rows"] = sum(r.rows for r in result.files)
    tot["files"] = len(result.files)
    tot["files_written"] = sum(1 for r in result.files if r.written)
    return tot


def _counts(d) -> str:
    get = d.get if isinstance(d, dict) else (lambda k: getattr(d, k))
    return (f"rows={get('rows')} upgraded={get('upgraded')} unchanged={get('unchanged')} "
            f"both={get('both')} corpus={get('corpus')} source_code={get('source_code')} "
            f"document_url={get('document_url')} local_path={get('local_path')} "
            f"html_path={get('html_path')} date_precision={get('date_precision')} "
            f"date_kind={get('date_kind')} bad_path={get('bad_path')} torn={get('torn')}")


def report_lines(result: MigrationResult, *, write: bool) -> list[str]:
    head = "migrate-contract" + ("" if write else " (dry-run)")
    lines = []
    for r in result.files:
        flag = " WRITTEN" if r.written else ""
        lines.append(f"{head}: {Path(r.path).name}: {_counts(r)}{flag}"
                     + (f" both_ids={','.join(r.both_ids)}" if r.both_ids else "")
                     + (f" bad_path_ids={','.join(r.bad_path_ids)}" if r.bad_path_ids else ""))
    t = summary(result)
    lines.append(f"{head}: TOTAL files={t['files']} {_counts(t)} "
                 f"files_written={t['files_written']}")
    lines.append(f"{head}: {VOCABULARY_FILE}: {result.vocabulary}")
    return lines
