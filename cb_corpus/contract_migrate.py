"""`migrate-contract`: rewrite the stored manifests in the documents-contract form.

One-shot for the documents contract (vault-contract 1.0.0, 2026-11-20). New
rows are written in the contract form (DocRecord.to_row) and every reader
expects that form: a row still in an older form makes any other command
stop with models.ContractRowError, naming this command (the read-time
upgrade of the transition was removed on 2026-11-20, once the NAS manifests
were migrated). This command rewrites the rows already stored; it works on
the raw JSON (no reader of this package involved), so an outside clone with
manifests of any earlier form can still run it. Per row (contract_upgrade,
every key keeping its position, every other value unchanged):

  - `corpus: "central-bank"` added as the first key;
  - `bank_code` renamed `source_code` (`pdf_url` -> `document_url` too, if any
    row still has it);
  - the `data/` prefix dropped from `local_path` and `html_path` (paths become
    relative to the data root); an ABSOLUTE path under the data root is made
    relative too; any other absolute path, or a `data/` prefix whose remainder
    is not a clean relative path (`data//raw/...`, `data/data/raw/...`), is an
    error: the row is left as is and reported (`bad_path`, exit 1);
  - an undated row: `date_precision` nulled and `date_kind` dropped (no date,
    no precision, no kind -- the vault would only notice them).

The upgraded row is then held to the readers' own check
(models.contract_problems): a row they would still refuse -- `pdf_url` AND
`document_url` (in the row or a `retired_duplicates` entry), an empty
`source_code`, another `corpus` -- is not something this command can fix
without guessing, so it is never counted `unchanged`: it is `refused`, left
as is and reported by doc_id (exit 1). Like `both` and `bad_path`, the
verdict is per row: the file's other rows are still upgraded (the command
has never been all-or-nothing per file; a refused row is kept byte-identical
either way, so the file is no worse, and a rerun after the hand fix reports
only what is left).

It also writes `vocabulary.json` at the data root (vocabulary.write_vocabulary).

Files handled: every per-bank manifest `data/manifest/<bank>.jsonl`; with
--write, a pre-split single `data/manifest.jsonl` is first split per bank
(storage.migrate_legacy_layout, raw lines kept). Not
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
`<file>.torn`, dropped); a malformed earlier line is a hard stop (exit 1, the
file untouched). "Malformed" is storage's definition in both modes: broken
JSON, broken UTF-8, or a JSON value that is not an object (`[1, 2]`). A row
whose JSON holds an escaped lone surrogate is rewritten with ASCII escapes
(see `_dump`). The rewrite is fsynced before os.replace and its temp file
removed on any failure (document_url_migrate.rewrite_file).
"""
from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, MutableMapping, Optional

from .config import Config
from .document_url_migrate import (LEGACY_URL_KEY, _read_lines_dry, _read_lines_locked,
                                   rewrite_file)
from .filelock import locked_sidecar
from .models import CORPUS, PATH_KEYS, SOURCE_KEY, URL_KEY, contract_problems
from .storage import migrate_legacy_layout
from .vocabulary import VOCABULARY_FILE, vocabulary_bytes, write_vocabulary

#: The source code's key before the documents contract (2026-11-20).
LEGACY_SOURCE_KEY = "bank_code"
#: The prefix manifest paths carried before they became relative to the data
#: root (`data/raw/...` -> `raw/...`, 2026-11-20).
LEGACY_DATA_PREFIX = "data/"


def contract_upgrade(row: MutableMapping[str, Any], *, undated: bool = True) -> list[str]:
    """Bring one manifest row to the documents contract IN PLACE, every key
    keeping its position and every other value untouched. Returns what
    changed, in this order (empty = already in the contract form):

      - `document_url`: `pdf_url` renamed (row and retired_duplicates entries);
      - `corpus`: `corpus: "central-bank"` added as the first key;
      - `source_code`: `bank_code` renamed; `both_source_names` instead when
        the row carries BOTH names -- left as is, never guess which value is
        right (migrate-contract reports it as an error);
      - `local_path`, `html_path`: the legacy `data/` prefix dropped (paths
        become relative to the data root; an absolute path is left as is);
        `bad_path` instead when what would remain is not a clean relative
        path (`data//raw/...`, `data/data/raw/...`, a bare `data/`): that
        path is left as is (migrate-contract leaves the whole row and
        reports it as an error);
      - `date_precision`: nulled on an undated row (no date, no precision);
      - `date_kind`: dropped from an undated row (the key is absent, never
        null, when there is no date -- DocRecord.to_row's rule).
    The last two are data fixes, not renames: only with `undated=True` (the
    default; `undated=False` leaves them to the tools that own dates).

    Pure raw-JSON work: no reader of this package is involved, so an outside
    clone holding manifests of any earlier form can still run it."""
    changes: list[str] = []
    renamed = _rename_key(row)
    for entry in row.get("retired_duplicates") or []:
        if isinstance(entry, dict):
            renamed = _rename_key(entry) or renamed
    if renamed:
        changes.append(URL_KEY)
    if "corpus" not in row:
        items = [("corpus", CORPUS), *row.items()]
        row.clear()
        row.update(items)
        changes.append("corpus")
    if LEGACY_SOURCE_KEY in row:
        if SOURCE_KEY in row:
            changes.append("both_source_names")
        elif _rename_key(row, LEGACY_SOURCE_KEY, SOURCE_KEY):
            changes.append(SOURCE_KEY)
    for key in PATH_KEYS:
        v = row.get(key)
        if isinstance(v, str) and v.startswith(LEGACY_DATA_PREFIX):
            rest = v[len(LEGACY_DATA_PREFIX):]
            # Fixed point: strip only when the remainder is a clean relative
            # path. `data//raw/x` would become absolute, `data/data/raw/x`
            # would need a second pass, a bare `data/` would become empty.
            if rest and not rest.startswith("/") and not rest.startswith(LEGACY_DATA_PREFIX):
                row[key] = rest
                changes.append(key)
            elif "bad_path" not in changes:
                changes.append("bad_path")
    if undated and not row.get("date"):
        if row.get("date_precision") is not None:
            row["date_precision"] = None
            changes.append("date_precision")
        if "date_kind" in row:
            del row["date_kind"]
            changes.append("date_kind")
    return changes


def _rename_key(d: MutableMapping[str, Any], old: str = LEGACY_URL_KEY,
                new: str = URL_KEY) -> bool:
    """`old` -> `new` in place, position kept. False (nothing done) when `old`
    is absent or `new` is already there."""
    if old not in d or new in d:
        return False
    items = [((new if k == old else k), v) for k, v in d.items()]
    d.clear()
    d.update(items)
    return True


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
    or an unclean `data/` prefix (the row is left as is, an error, see
    `bad_path_ids`). `refused`: a row the readers would still refuse once
    upgraded (models.contract_problems: `pdf_url` and `document_url` both,
    an empty `source_code`, another `corpus`), left as is, an error (see
    `refused_ids`; `refused_why`, parallel to it, holds each row's
    contract_problems phrases and `refused_reasons` counts each phrase:
    such a row is fixed by hand, see fix_by_hand). `torn`: malformed final
    line (dropped on a --write rewrite)."""
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
    refused: int = 0
    torn: int = 0
    unparsed: int = 0          # never set for a manifest (kept for the shared readers)
    written: bool = False
    both_ids: list[str] = field(default_factory=list)
    bad_path_ids: list[str] = field(default_factory=list)
    refused_ids: list[str] = field(default_factory=list)
    refused_why: list[list[str]] = field(default_factory=list)
    refused_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def rows(self) -> int:
        return self.upgraded + self.unchanged + self.both + self.bad_path + self.refused


@dataclass
class MigrationResult:
    files: list[FileCounts]
    #: vocabulary.json: "written" | "unchanged" (--write), "would-write" |
    #: "unchanged" (dry run).
    vocabulary: str


def _dump(row: dict) -> str:
    """A rewritten row as one JSON line, UTF-8 as every writer
    (`ensure_ascii=False`). A row holding a lone surrogate (an escaped
    `\\ud800` in the stored JSON: it parses, but cannot be encoded as
    UTF-8) is written with ASCII escapes instead: same values, still a valid
    UTF-8 file, decided here so the dry run plans exactly what --write
    writes. (The vault refuses such a row, `bad_text`/`bad_extra`, before
    and after the migration alike.)"""
    line = json.dumps(row, ensure_ascii=False)
    try:
        line.encode("utf-8")
    except UnicodeEncodeError:
        line = json.dumps(row, ensure_ascii=True)
    return line


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
            if bad or "bad_path" in changes:
                # an unusable path: the whole row is left as is (never half
                # migrated), reported, exit 1
                counts.bad_path += 1
                counts.bad_path_ids.append(did)
                out.append(raw)
                continue
            changes += [k for k in rel if k not in changes]
            probs = contract_problems(new)
            if probs:
                # what every reader would still refuse: never guessed at,
                # never "unchanged"; the whole row left as is, exit 1
                counts.refused += 1
                counts.refused_ids.append(did)
                counts.refused_why.append(probs)
                for p in probs:
                    counts.refused_reasons[p] = counts.refused_reasons.get(p, 0) + 1
                out.append(raw)
                continue
            for name in changes:
                attr = _CHANGE_FIELDS[name]
                setattr(counts, attr, getattr(counts, attr) + 1)
            if changes:
                counts.upgraded += 1
                out.append(_dump(new))
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
            rewrite_file(path, out)
            counts.written = True
    return counts


def migrate_contract(cfg: Config, *, write: bool = False,
                     bank_codes: Optional[Iterable[str]] = None) -> MigrationResult:
    """Upgrade every per-bank manifest (or the given banks) to the documents
    contract, then write `vocabulary.json` at the data root. Dry run unless
    `write`. A legacy single `data/manifest.jsonl` is split into per-bank
    files first with --write (storage.migrate_legacy_layout); the dry run
    only warns about it."""
    if write:
        migrate_legacy_layout(cfg)
    if bank_codes:
        files = [cfg.manifest_file(b) for b in bank_codes]
    else:
        files = sorted(cfg.manifest_dir.glob("*.jsonl")) if cfg.manifest_dir.is_dir() else []
        if cfg.manifest_path.exists():
            print(f"[migrate-contract] WARNING: legacy {cfg.manifest_path} not "
                  f"counted (--write splits it per bank first)", file=sys.stderr)
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
               "local_path", "html_path", "date_precision", "date_kind", "bad_path",
               "refused", "torn")

#: How many doc_ids the report lists per kind of error and file, and how many
#: refused rows it explains one line each (the result keeps them all); the
#: rest is summed up as "(+N more)". The CLI's `--show-ids N` default; 0 =
#: all.
SHOW_IDS_DEFAULT = 20


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
            f"date_kind={get('date_kind')} bad_path={get('bad_path')} "
            f"refused={get('refused')} torn={get('torn')}")


def _ids(name: str, ids: list[str], show: int) -> str:
    """` <name>=a,b,c` (at most `show` ids, 0 = all, then ` (+N more)`), or ""."""
    if not ids:
        return ""
    n = show or len(ids)
    more = f" (+{len(ids) - n} more)" if len(ids) > n else ""
    return f" {name}={','.join(ids[:n])}{more}"


def fix_by_hand(problem: str, bank: str) -> str:
    """How an operator fixes a refused row for one contract_problems phrase
    (what remains after the upgrade: migrate-contract handles the rest)."""
    if "pdf_url" in problem:
        return ("remove `pdf_url` (on the row or its retired_duplicates entry) once its "
                "URL is checked against `document_url`; keep `document_url`")
    if problem == f"no `{SOURCE_KEY}`":
        return f"fill `{SOURCE_KEY}` with `{bank}` (this manifest's bank code)"
    if problem.startswith("`corpus`"):
        return ("move the row out of this manifest: it belongs to another corpus "
                "(never relabel it)")
    return "edit the row so its keys follow the documents contract"


def report_lines(result: MigrationResult, *, write: bool,
                 show_ids: int = SHOW_IDS_DEFAULT) -> list[str]:
    """The report: one count line per file (ids listed up to `show_ids`, 0 =
    all), then per file with refused rows its per-reason counts and one line
    per listed refused id (its problems and how to fix it by hand), the
    TOTAL, the vocabulary line and, when rows were refused, a closing line
    saying this command cannot fix them."""
    head = "migrate-contract" + ("" if write else " (dry-run)")
    lines = []
    for r in result.files:
        name = Path(r.path).name
        flag = " WRITTEN" if r.written else ""
        lines.append(f"{head}: {name}: {_counts(r)}{flag}"
                     + _ids("both_ids", r.both_ids, show_ids)
                     + _ids("bad_path_ids", r.bad_path_ids, show_ids)
                     + _ids("refused_ids", r.refused_ids, show_ids))
        if r.refused:
            reasons = "; ".join(f"{p}={n}" for p, n in r.refused_reasons.items())
            lines.append(f"{head}: {name}: refused reasons: {reasons}")
            bank = Path(r.path).stem
            n = show_ids or len(r.refused_ids)
            for did, probs in zip(r.refused_ids[:n], r.refused_why[:n]):
                fixes = "; ".join(dict.fromkeys(fix_by_hand(p, bank) for p in probs))
                lines.append(f"{head}: {name}: refused {did}: {'; '.join(probs)} "
                             f"-> fix by hand: {fixes}")
    t = summary(result)
    lines.append(f"{head}: TOTAL files={t['files']} {_counts(t)} "
                 f"files_written={t['files_written']}")
    lines.append(f"{head}: {VOCABULARY_FILE}: {result.vocabulary}")
    if t["refused"]:
        lines.append(f"{head}: {t['refused']} refused row(s) left as is: this command "
                     f"cannot fix them; edit each by hand as its line says (no job "
                     f"running; --show-ids 0 lists them all), then run this dry run "
                     f"again")
    return lines
