"""`replace-artifact`: put the right bytes behind a known row, or retire a ghost row.

`audit-pdfs` lists stored PDFs that are not the document (truncated files, HTML
error pages, empty files). `Storage.save()` never re-downloads a doc_id it
already knows, so nothing else can fix them. This command runs a reviewed plan,
a CSV with the columns `doc_id, action, candidates, note`:

* `action=replace`: `candidates` is a space-separated list of URLs and/or the
  keyword `wayback` (the HTTP-200 PDF captures of the row's OWN `document_url`,
  newest first, as raw `id_` URLs: `sources.wayback.pdf_captures`, at most
  `wayback_max_captures` of them, one per distinct payload). Each candidate is
  fetched in order with the project's `Fetcher` (throttle, UA, retries) and
  must pass the content check new writes go through
  (`pdfcheck.check_pdf_bytes` with `Config.min_pdf_bytes` /
  `pdf_require_eof`; a PDF is required whatever the server's content type
  says). A passing candidate whose sha256 already belongs to ANOTHER
  manifest row is recorded in `tried` (`duplicate-of:<doc_id>`) and the
  search goes on; the first passing, non-duplicate candidate wins and the
  later ones are not fetched. When no candidate wins but one was a duplicate,
  the outcome is `duplicate-of:<doc_id>` (the first one seen): nothing is
  written, the operator decides (retire the row instead, most likely). With
  `--write`, the bytes replace the row's artifact atomically at the SAME path
  (`storage._atomic_write_bytes`; an `html_path` sibling of a rendered row is
  left alone), then the row is updated under its bank's sidecar lock
  (`storage.merge_rows_locked`): `sha256`, `mime_type` application/pdf, the
  winning URL moved to the front of `alt_urls` when it is not the row's own
  `document_url` (`save()` always tries `document_url` first: the winning URL becomes the
  FIRST FALLBACK), and one entry appended to the `artifact_replaced` list
  {at, from_url, previous_sha256, note} (history kept; a legacy single dict
  becomes the list's first entry). `doc_id`, `document_url` and `local_path` never
  change. Refused (outcome `error`, nothing fetched): an artifact that is not
  a `.pdf` path (PDF bytes over an `.html` artifact would be a lie about the
  file), a path outside `raw/`, a file another manifest row also points at
  (same resolved `local_path` or `html_path`: overwriting it would change that
  row's bytes too), and a doc_id present more than once in the manifests.
* `action=retire`: `candidates` is `duplicate-of:<owner doc_id>`. The owner must
  be another manifest row, a real PDF owner (`storage.is_stampable_owner`),
  whose file passes the content check on disk AND hashes to the owner row's
  own `sha256`. Refused when the retired row's file IS the owner's file
  (`os.path.samefile`: same path or a hard link; moving it would take the
  owner's bytes away) and when either doc_id appears more than once in the
  manifests. With `--write`, in this order (each step is idempotent, so a
  run killed midway is finished by the next one): the retired row's `document_url`
  and `source_url` are stamped on the owner (`Storage.stamp_duplicates`, the
  nightly duplicate-content machinery: `alt_urls`, `alt_source_urls`, one line
  in `reports/duplicate_stamps.jsonl`) so discovery never mints the row again;
  the owner row gets a `retired_duplicates` audit entry {doc_id, document_url,
  source_url, previous_sha256, at, note}; the retired row's file (and its
  `html_path` sibling, if any) moves to `raw_orphans/` (mirror tree, never
  deleted: `orphans.move_to_orphans`); the row is removed from its bank's
  manifest under the sidecar lock (`storage.remove_rows_locked`).

The default run is a dry run: candidates ARE fetched (network reads, needed to
say what would win) but nothing under `raw/`, `raw_orphans/` or the manifests
is written. Both modes write the report CSV (`reports/replace_artifact.csv` by
default), one line per plan row as soon as the row is done (flushed), so a
killed run leaves a partial report. Idempotent: a replace whose winning bytes are
already the row's `sha256` and the file on disk is `already-fixed`; a retire
whose row is gone and whose owner carries its `retired_duplicates` entry is
`already-retired` (a rerun only says `already-fixed` when the candidate
still serves the same bytes). The plan is read as UTF-8 with an optional BOM;
doc_ids (and owner ids) are stripped and lowercased. Plan-row problems (unknown doc_id or action, no
candidates, a doc_id repeated in the plan) are per-row `error` outcomes; only
an unusable plan (missing file, missing columns, no rows) raises PlanError.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import Config
from .http import Fetcher
from .models import row_bank, row_url
from .orphans import OrphanEntry, _resolve_local_path, _sha256_of, move_to_orphans
from .pdfcheck import check_pdf_bytes
from .sources.wayback import pdf_captures, raw_url
from .storage import (Storage, _atomic_write_bytes, is_stampable_owner, iter_manifest_rows,
                      merge_rows_locked, normalize_url, pdf_file_problem, remove_rows_locked)

PLAN_COLUMNS = ("doc_id", "action", "candidates", "note")
ACTIONS = ("replace", "retire")
WAYBACK = "wayback"
DUPLICATE_OF = "duplicate-of:"
LABEL = "replace-artifact"
#: Default cap on the Wayback captures tried for the `wayback` keyword (the
#: newest distinct payloads). CLI: --wayback-max-captures.
DEFAULT_WAYBACK_MAX_CAPTURES = 10

CSV_FIELDS = ("line", "doc_id", "bank_code", "action", "outcome", "winning_url",
              "old_size", "new_size", "old_sha256", "new_sha256", "owner_doc_id",
              "tried", "note", "error")


class PlanError(ValueError):
    """The plan file cannot be used at all (the command exits 1)."""


@dataclass
class PlanRow:
    line: int                 # 1-based line number in the CSV (header = 1)
    doc_id: str
    action: str
    candidates: list[str]
    note: str


@dataclass
class ReplaceSummary:
    dry_run: bool
    plan: str
    csv_path: str
    counts: dict = field(default_factory=dict)     # outcome (duplicate-of collapsed) -> n
    rows: list = field(default_factory=list)       # one result dict per plan row


def load_plan(path) -> list[PlanRow]:
    """Parse the plan CSV. Raises PlanError when it cannot be used at all:
    unreadable, a required column missing, or no data row. Blank rows are
    skipped; values are stripped; everything else is judged per row later."""
    p = Path(path)
    try:
        with p.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            header = [h.strip() for h in (reader.fieldnames or [])]
            missing = [c for c in PLAN_COLUMNS if c not in header]
            if missing:
                raise PlanError(f"plan {p}: missing column(s) {', '.join(missing)} "
                                f"(expected {', '.join(PLAN_COLUMNS)})")
            reader.fieldnames = header
            rows: list[PlanRow] = []
            for rec in reader:
                vals = {k: (rec.get(k) or "").strip() for k in PLAN_COLUMNS}
                if not any(vals.values()):
                    continue
                rows.append(PlanRow(line=reader.line_num, doc_id=vals["doc_id"].lower(),
                                    action=vals["action"].lower(),
                                    candidates=vals["candidates"].split(),
                                    note=vals["note"]))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise PlanError(f"plan {p}: {type(exc).__name__}: {exc}") from exc
    if not rows:
        raise PlanError(f"plan {p}: no rows")
    return rows


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _disk(path: Optional[Path]) -> tuple[Optional[int], str]:
    """(size, sha256) of the file at `path`, (None, "") when absent/unreadable."""
    if path is None or not path.is_file():
        return None, ""
    try:
        sha, size, _ = _sha256_of(path)
    except OSError:
        return None, ""
    return size, sha


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class _Run:
    def __init__(self, cfg: Config, *, write: bool, fetcher, wayback_max_captures: int,
                 now: Optional[str]):
        self.cfg, self.write, self.fetcher = cfg, write, fetcher
        self.wayback_max = wayback_max_captures
        self.now = now
        self.rows: dict[str, dict] = {}
        self.owners_of: dict[str, set[str]] = {}       # sha256 -> doc_ids
        self.copies: Counter = Counter()                 # doc_id -> rows carrying it
        self.users_of: dict[str, set[str]] = {}          # resolved file path -> doc_ids
        for row in iter_manifest_rows(cfg):
            did = row.get("doc_id")
            if not did:
                continue
            self.rows[did] = row
            self.copies[did] += 1
            if row.get("sha256"):
                self.owners_of.setdefault(row["sha256"], set()).add(did)
            for key in ("local_path", "html_path"):
                k = self._path_key(row.get(key) or "")
                if k:
                    self.users_of.setdefault(k, set()).add(did)
        self._storage: Optional[Storage] = None

    # -- helpers -------------------------------------------------------------
    def _path_key(self, local_path: str) -> str:
        p = _resolve_local_path(self.cfg, local_path) if local_path else None
        return str(p.resolve()) if p is not None else ""

    def _single(self, doc_id: str, what: str) -> None:
        n = self.copies.get(doc_id, 0)
        if n > 1:
            raise _RowError(f"{what} {doc_id} duplicated in the manifests ({n} rows with "
                            f"this doc_id): fix the manifest first")

    def storage(self) -> Storage:
        """Built on first need only (a retire with --write): Storage() has
        side effects (directories, Chrome profile sweep) a dry run avoids."""
        if self._storage is None:
            self._storage = Storage(self.cfg, self.fetcher)
        return self._storage

    def at(self) -> str:
        return self.now or _now_iso()

    def _set_sha(self, doc_id: str, old: str, new: str) -> None:
        if old:
            self.owners_of.get(old, set()).discard(doc_id)
        if new:
            self.owners_of.setdefault(new, set()).add(doc_id)

    def _expand(self, row: dict, tokens: list[str], tried: list[dict]) -> Iterator[str]:
        """Candidate URLs in plan order, lazily: the `wayback` CDX query only
        runs when every earlier candidate failed. Notes (CDX result, a token
        that is not a URL) go to `tried` at their place in the order. A URL
        already yielded is not yielded again."""
        seen: set[str] = set()
        for tok in tokens:
            if tok.lower() == WAYBACK:
                target = row_url(row) or ""
                try:
                    stamps = pdf_captures(self.fetcher, target, max_captures=self.wayback_max)
                except Exception as exc:  # noqa: BLE001 - reported, never fatal
                    tried.append({"url": WAYBACK, "reason": f"CDX query failed "
                                  f"({type(exc).__name__}: {exc})"[:300]})
                    continue
                tried.append({"url": WAYBACK, "reason": (f"{len(stamps)} capture(s)"
                                                         if stamps else "no PDF capture")})
                urls = [raw_url(target, ts) for ts in stamps]
            elif tok.lower().startswith(("http://", "https://")):
                urls = [tok]
            else:
                tried.append({"url": tok, "reason": "not a URL nor `wayback`: skipped"})
                continue
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    yield u

    # -- actions -------------------------------------------------------------
    def replace(self, plan: PlanRow, res: dict) -> None:
        row = self.rows.get(plan.doc_id)
        if row is None:
            raise _RowError(f"unknown doc_id {plan.doc_id!r} (not in any manifest)")
        res["bank_code"] = row_bank(row) or ""
        lp = row.get("local_path") or ""
        path = _resolve_local_path(self.cfg, lp) if lp else None
        if path is None or path.suffix.lower() != ".pdf":
            raise _RowError(f"artifact is not a .pdf path ({lp or 'no local_path'}): "
                            f"only a PDF artifact can be replaced")
        if not _inside(path, self.cfg.raw_dir):
            raise _RowError(f"artifact path {path} is outside {self.cfg.raw_dir}")
        self._single(plan.doc_id, "row")
        sharers = sorted(self.users_of.get(self._path_key(lp), set()) - {plan.doc_id})
        if sharers:
            raise _RowError(f"artifact file {path} is also the file of row(s) "
                            f"{', '.join(sharers)}: replacing it would change their bytes")
        old_size, disk_sha = _disk(path)
        res["old_size"], res["old_sha256"] = old_size, row.get("sha256") or ""

        tried: list[dict] = res["tried"]
        winner: Optional[tuple[str, bytes]] = None
        first_dup: Optional[tuple[str, str, int]] = None     # (owner, sha256, size)
        for url in self._expand(row, plan.candidates, tried):
            try:
                body, _served = self.fetcher.get_bytes(url)
            except Exception as exc:  # noqa: BLE001 - the next candidate is tried
                tried.append({"url": url, "reason": f"fetch failed ({type(exc).__name__}: "
                              f"{exc})"[:300]})
                continue
            problem = check_pdf_bytes(body, min_bytes=self.cfg.min_pdf_bytes,
                                      require_eof=self.cfg.pdf_require_eof)
            if problem:
                tried.append({"url": url, "reason": problem})
                continue
            body_sha = _sha(body)
            others = sorted(self.owners_of.get(body_sha, set()) - {plan.doc_id})
            if others:
                tried.append({"url": url, "reason": f"{DUPLICATE_OF}{others[0]}"})
                if first_dup is None:
                    first_dup = (others[0], body_sha, len(body))
                continue
            tried.append({"url": url, "reason": "ok"})
            winner = (url, body)
            break
        if winner is None:
            if first_dup is not None:
                owner, dsha, dsize = first_dup
                res.update(outcome=f"{DUPLICATE_OF}{owner}", owner_doc_id=owner,
                           new_sha256=dsha, new_size=dsize)
            else:
                res["outcome"] = "unresolved"
            return
        url, body = winner
        new_sha = _sha(body)
        res.update(winning_url=url, new_size=len(body), new_sha256=new_sha)
        if row.get("sha256") == new_sha and disk_sha == new_sha:
            res["outcome"] = "already-fixed"
            return
        if not self.write:
            res["outcome"] = "would-replace"
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(path, body)
        at = self.at()
        own = normalize_url(row_url(row) or "")

        def merge(r: dict) -> bool:
            # previous_sha256 from the row as re-read under the lock.
            history = r.get("artifact_replaced") or []
            if isinstance(history, dict):           # legacy single entry
                history = [history]
            r["artifact_replaced"] = [*history, {"at": at, "from_url": url,
                                                 "previous_sha256": r.get("sha256") or "",
                                                 "note": plan.note}]
            r["sha256"] = new_sha
            r["mime_type"] = "application/pdf"
            if normalize_url(url) != own:
                rest = [u for u in (r.get("alt_urls") or [])
                        if normalize_url(u) != normalize_url(url)]
                r["alt_urls"] = [url, *rest]
            return True

        bank = row_bank(row) or "_unknown"
        n = merge_rows_locked(self.cfg, {bank: {plan.doc_id: merge}})
        if n == 0:
            raise _RowError(f"file replaced, but row {plan.doc_id} was not found in "
                            f"{bank}.jsonl at update time: row not updated")
        if n > 1:
            raise _RowError(f"file replaced, but row {plan.doc_id} is duplicated in "
                            f"{bank}.jsonl: {n} rows updated")
        self._set_sha(plan.doc_id, row.get("sha256") or "", new_sha)
        row["sha256"] = new_sha
        res["outcome"] = "replaced"

    def retire(self, plan: PlanRow, res: dict) -> None:
        if len(plan.candidates) != 1 or not plan.candidates[0].startswith(DUPLICATE_OF):
            raise _RowError(f"retire needs exactly one `{DUPLICATE_OF}<owner doc_id>` "
                            f"candidate, got {' '.join(plan.candidates)!r}")
        owner_id = plan.candidates[0][len(DUPLICATE_OF):].strip().lower()
        res["owner_doc_id"] = owner_id
        if not owner_id or owner_id == plan.doc_id:
            raise _RowError(f"owner {owner_id!r} must be another doc_id")
        owner = self.rows.get(owner_id)
        if owner is None:
            raise _RowError(f"owner {owner_id!r} is not in any manifest")
        row = self.rows.get(plan.doc_id)
        if row is None:
            done = any(e.get("doc_id") == plan.doc_id
                       for e in owner.get("retired_duplicates") or [] if isinstance(e, dict))
            if done:
                res["outcome"] = "already-retired"
                return
            raise _RowError(f"unknown doc_id {plan.doc_id!r} (not in any manifest)")
        res["bank_code"] = row_bank(row) or ""
        self._single(plan.doc_id, "row")
        self._single(owner_id, "owner")
        if not is_stampable_owner(owner, self.cfg):
            raise _RowError(f"owner {owner_id} is not a real PDF owner (mime_type "
                            f"{owner.get('mime_type')!r}, html_path "
                            f"{'set' if owner.get('html_path') else 'unset'})")
        olp = owner.get("local_path") or ""
        opath = _resolve_local_path(self.cfg, olp) if olp else None
        if opath is None:
            raise _RowError(f"owner {owner_id} has no resolvable local_path ({olp!r})")
        problem = pdf_file_problem(opath, self.cfg)
        if problem:
            raise _RowError(f"owner {owner_id} file fails the content check: {problem}")
        osize, osha = _disk(opath)
        if osha != (owner.get("sha256") or ""):
            raise _RowError(f"owner {owner_id} file does not hash to its row's sha256")
        res["new_size"], res["new_sha256"] = osize, osha

        paths: list[Path] = []
        for key in ("local_path", "html_path"):
            lp = row.get(key) or ""
            p = _resolve_local_path(self.cfg, lp) if lp else None
            if p is not None and p not in paths:
                if not _inside(p, self.cfg.raw_dir):
                    raise _RowError(f"{key} {p} is outside {self.cfg.raw_dir}")
                try:
                    same = p.exists() and os.path.samefile(p, opath)
                except OSError:
                    same = False
                if same:
                    raise _RowError(f"{key} {p} is the owner's file ({opath}): moving it "
                                    f"would take the owner's bytes away")
                sharers = sorted(self.users_of.get(self._path_key(lp), set()) - {plan.doc_id})
                if sharers:
                    raise _RowError(f"{key} {p} is also the file of {', '.join(sharers)}: "
                                    f"moving it would take their bytes away")
                paths.append(p)
        size = _disk(paths[0])[0] if paths else None
        res["old_size"], res["old_sha256"] = size, row.get("sha256") or ""
        if not self.write:
            res["outcome"] = "would-retire"
            return

        st = self.storage()
        _, _, persisted = st.stamp_duplicates(
            [{"url": row_url(row) or "", "source_url": row.get("source_url") or "",
              "owner_doc_id": owner_id}], LABEL)
        if owner_id not in persisted:
            raise _RowError(f"owner {owner_id} could not be stamped (row changed?)")
        entry = {"doc_id": plan.doc_id, "document_url": row_url(row) or "",
                 "source_url": row.get("source_url") or "",
                 "previous_sha256": row.get("sha256") or "", "at": self.at(),
                 "note": plan.note}

        def audit(r: dict) -> bool:
            current = [e for e in r.get("retired_duplicates") or []]
            if any(isinstance(e, dict) and e.get("doc_id") == plan.doc_id for e in current):
                return False
            r["retired_duplicates"] = [*current, entry]
            return True

        merge_rows_locked(self.cfg, {row_bank(owner) or "_unknown": {owner_id: audit}})

        for p in paths:
            if not p.exists():
                continue                      # moved by an earlier, interrupted run
            psize, psha = _disk(p)
            rel = p.resolve().relative_to(self.cfg.raw_dir.resolve()).as_posix()
            e = OrphanEntry(path=str(p), rel=rel, bank="", doc_type="", year_dir="",
                            ext=p.suffix.lower().lstrip("."), size=psize or 0, sha256=psha,
                            valid_pdf=False, matched_doc_id=owner_id, action="would-move",
                            dest=rel)
            move_to_orphans(e, self.cfg)
            if e.action != "moved":
                raise _RowError(f"could not move {p} to raw_orphans/: {e.error}")
        remove_rows_locked(self.cfg, {row_bank(row) or "_unknown": [plan.doc_id]})
        self._set_sha(plan.doc_id, row.get("sha256") or "", "")
        del self.rows[plan.doc_id]
        owner.setdefault("retired_duplicates", []).append(entry)
        res["outcome"] = "retired"


class _RowError(Exception):
    """A per-row refusal: outcome `error` with this message, nothing written
    for the row (unless the message says otherwise)."""


def replace_artifacts(cfg: Config, plan_path, *, write: bool = False, fetcher=None,
                      csv_path=None,
                      wayback_max_captures: int = DEFAULT_WAYBACK_MAX_CAPTURES,
                      now: Optional[str] = None) -> ReplaceSummary:
    """Run the plan (see the module docstring). Raises PlanError for an
    unusable plan; every per-row problem is an `error` outcome instead.
    `now` (ISO string) pins the audit timestamps (tests)."""
    if wayback_max_captures < 1:
        raise PlanError("--wayback-max-captures must be >= 1")
    plan = load_plan(plan_path)
    out = Path(csv_path) if csv_path else cfg.reports_dir / "replace_artifact.csv"
    run = _Run(cfg, write=write, fetcher=fetcher or Fetcher(cfg),
               wayback_max_captures=wayback_max_captures, now=now)
    summary = ReplaceSummary(dry_run=not write, plan=str(plan_path), csv_path=str(out))
    seen: set[str] = set()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as report:
        writer = csv.DictWriter(report, fieldnames=CSV_FIELDS)
        writer.writeheader()
        report.flush()
        for p in plan:
            res = _run_row(run, p, seen)
            summary.rows.append(res)
            writer.writerow(_csv_line(res))
            report.flush()
    summary.counts = dict(Counter(r["outcome"].split(":")[0] if r["outcome"].startswith(
        DUPLICATE_OF) else r["outcome"] for r in summary.rows))
    return summary


def _run_row(run: "_Run", p: PlanRow, seen: set[str]) -> dict:
    """One plan row -> its result dict. Only a BaseException that is not an
    Exception (KeyboardInterrupt, SystemExit) escapes."""
    res: dict[str, Any] = {"line": p.line, "doc_id": p.doc_id, "bank_code": "",
                           "action": p.action, "outcome": "error", "winning_url": "",
                           "old_size": None, "new_size": None, "old_sha256": "",
                           "new_sha256": "", "owner_doc_id": "", "tried": [],
                           "note": p.note, "error": ""}
    try:
        if not p.doc_id:
            raise _RowError("empty doc_id")
        if p.action not in ACTIONS:
            raise _RowError(f"unknown action {p.action!r} (expected "
                            f"{' or '.join(ACTIONS)})")
        if not p.candidates:
            raise _RowError("no candidates")
        # The first VALID row of a doc_id runs; any later one is refused
        # (two actions on one row in one run would race each other).
        if p.doc_id in seen:
            raise _RowError(f"doc_id {p.doc_id!r} repeated in the plan (the first "
                            f"valid row ran)")
        seen.add(p.doc_id)
        getattr(run, p.action)(p, res)
    except _RowError as exc:
        res["outcome"], res["error"] = "error", str(exc)
    except Exception as exc:  # noqa: BLE001 - one row must not stop the plan
        res["outcome"] = "error"
        res["error"] = f"{type(exc).__name__}: {exc}"[:500]
    if res["outcome"] == "error":
        print(f"[{LABEL}] {p.doc_id or '?'} (line {p.line}): {res['error']}",
              file=sys.stderr, flush=True)
    return res


def _csv_line(r: dict) -> dict:
    line = {k: r.get(k) for k in CSV_FIELDS}
    line["tried"] = " | ".join(f"{t['url']}: {t['reason']}" for t in r["tried"])
    for k in ("old_size", "new_size"):
        line[k] = "" if line[k] is None else line[k]
    return line
