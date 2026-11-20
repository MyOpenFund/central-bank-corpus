"""`policy-dates` -- give every policy-core row the best date its own title or
URL carries, an honest precision, and say what the date is.

One-off backfill for rows written before the adapters stated a precision
(new rows are right from the start: the producers use the same rule table,
`dates.POLICY_DATE_RULES`). No network, no file moved: only the date keys of
a manifest row are ever written (`DATE_KEYS`); `doc_id`, `sha256`,
`local_path`, the URLs and the title are never touched.

For each row in scope (default: doc types A1 A2 A3 B1 E1 F1 of ecb, us, gb,
au, ca, jp) the series' rule reads a date from the title / URL, and the row is
compared with it:

  to-day / to-month / to-year   the date or its precision label changes
  day-confirmed                 the evidence names the very day the row holds:
                                the date stays, `date_precision: day` is written
  kind-only                     date and label already right; `date_kind` is added
  misfiled                      the row is not a document of its series (an
                                Inflation Report filed as minutes): dated like
                                its page, never given a `date_kind`; reported
                                on every run
  unchanged                     nothing to write (what a second run reports)
  not-a-report                  the URL is an index page, not a document: never dated
  no-evidence                   neither title nor URL names a date: left as it is
  conflict                      title and URL name different periods: left as it is
  mismatch                      the evidence is outside the period the stored
                                date asserts: left as it is (see REFINE_ONLY)
  no-rule                       the series has no rule (scope widened by hand)

How a real first-of-month is told from a placeholder: never by the day
number. A stored `2018-08-01` whose URL says `monetary20180801a` is confirmed
by its evidence, kept and labelled `day`; a stored `2021-01-01` whose title
says "June 2021" is a placeholder and becomes `2021-06-01` with precision
`month`. A stored day the evidence neither names nor contradicts (the title
only gives its month) is kept as it is, unlabelled.

The CSV also carries a `note` column (ROW_NOTES): pages captured before
publication, Federal Reserve individual projections, misfiled rows.

Dry-run by default (a CSV to read); `--write` applies the same plan,
re-computed row by row under each bank's manifest lock. Idempotent.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from .config import Config
from .dates import (FED_SEP_COMPILATION_URL, KINDLESS_DATE_SOURCES, PRECISION_DAY,
                    PRECISION_MONTH, PRECISION_YEAR, PRECISIONS, STATUS_DATED,
                    STATUS_NO_EVIDENCE, STATUS_NO_RULE, STATUS_NOT_A_REPORT, DateMatch,
                    infer_policy_date, is_misfiled, policy_date_kind)
from .models import row_bank, row_url
from .storage import iter_manifest_rows, merge_rows_locked

#: Default scope: the policy core. Both are parameters of run_policy_dates
#: (CLI: --banks / --types); a row outside the scope is never read as a
#: candidate, and a series without a rule is never written whatever the scope.
POLICY_BANKS = ("ecb", "us", "gb", "au", "ca", "jp")
POLICY_DOC_TYPES = ("A1", "A2", "A3", "B1", "E1", "F1")

#: The only row keys this command may write (or, for `date_kind`, remove).
DATE_KEYS = ("date", "year", "date_precision", "date_source", "date_kind")

#: `date_source` written with a date or a precision on a row that has none:
#: the evidence is the bank's own title or URL.
EVIDENCE_DATE_SOURCE = "bank_site"

#: Default for `refine_only`: the stored date may only be REFINED, inside the
#: period it already asserts. That period is the row's explicit
#: `date_precision` when it has one (`day`: the date itself, never refined;
#: `month`: its month; `year`: its year); for an unlabelled row it is read
#: from the date's shape (1 January -> that year; the 1st of a month -> that
#: month; any other day -> the date itself). An undated row asserts nothing.
#: Evidence that would move a date out of that period is reported as
#: `mismatch` and the row is left alone. `--allow-moves` turns the guard off
#: (a deliberate action: it needs an explicit scope, and a date moved to
#: another year leaves `local_path` under the old year folder -- files are
#: never moved).
REFINE_ONLY = True

ACTION_UNCHANGED = "unchanged"
ACTION_KIND_ONLY = "kind-only"
ACTION_DAY_CONFIRMED = "day-confirmed"
ACTION_MISFILED = "misfiled"
ACTION_MISMATCH = "mismatch"

#: A page saved before its document was published ("Monetary Policy Report -
#: July 2026 (to be published at 12pm)"): the row holds the placeholder page.
STUB_TITLE = r"\(to be published\b"


@dataclass(frozen=True)
class RowNote:
    """A remark for the CSV's `note` column: rows of `series` (None = any)
    whose `field` matches `pattern`. Notes never change what is written."""
    note: str
    field: str
    pattern: str
    series: Optional[tuple[str, str]] = None


#: Things the reader of the CSV must know about a row, beyond its date.
ROW_NOTES = (
    RowNote(note="stub: page captured before publication, stored under the real URL "
                 "(the real document is never fetched until this row and its file are deleted)",
            field="title", pattern=STUB_TITLE),
    RowNote(note="individual projections: dated at the meeting, released about five "
                 "years later",
            field="document_url", pattern=FED_SEP_COMPILATION_URL, series=("us", "F1")),
)
NOTE_MISFILED = "misfiled: not a document of this series; dated like its page, no date_kind"

CSV_COLUMNS = ("doc_id", "bank", "doc_type", "old_date", "old_precision", "new_date",
               "new_precision", "kind", "action", "rule", "evidence", "fields_changed",
               "note", "title", "document_url")


@dataclass
class RowPlan:
    """What the command would do to one row. `updates` holds the keys to
    write and `removals` the keys to drop (both within DATE_KEYS; both empty =
    row left byte-identical). `new_date`, `new_precision` and `kind` are the
    row AFTER the write, key for key (None / "" = the key is absent or null)."""
    action: str
    new_date: Optional[str] = None
    new_precision: Optional[str] = None
    kind: str = ""
    rule: str = ""
    evidence: str = ""
    note: str = ""
    updates: dict = field(default_factory=dict)
    removals: tuple = ()

    @property
    def changes(self) -> bool:
        return bool(self.updates or self.removals)

    def apply(self, row: dict) -> bool:
        """Apply the plan to `row` in place; True when it changed it."""
        row.update(self.updates)
        for key in self.removals:
            row.pop(key, None)
        return self.changes

    def fields_changed(self) -> str:
        return " ".join([*self.updates, *(f"-{k}" for k in self.removals)])


def _stored_date(value) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def asserted_period(stored: date, label: Optional[str]) -> str:
    """The period a stored date stands for (a PRECISIONS value): its explicit
    label, else what its shape allows it to be a placeholder for."""
    if label in PRECISIONS:
        return label
    if (stored.month, stored.day) == (1, 1):
        return PRECISION_YEAR
    return PRECISION_MONTH if stored.day == 1 else PRECISION_DAY


def _target(stored: date, label: Optional[str], new: DateMatch
            ) -> Optional[tuple[date, Optional[str]]]:
    """(date, precision) the row should hold given its stored date, its label
    and the evidence; None when the evidence is outside the period the stored
    date asserts (see REFINE_ONLY). A returned precision of None means "keep
    the row unlabelled": the stored day is consistent with the evidence but
    not confirmed by it."""
    asserted = asserted_period(stored, label)
    same_year = stored.year == new.date.year
    same_month = same_year and stored.month == new.date.month
    inside = same_year if asserted == PRECISION_YEAR else (
        same_month if asserted == PRECISION_MONTH else stored == new.date)
    if new.precision == PRECISION_DAY:
        if stored == new.date:
            return new.date, PRECISION_DAY           # confirmed, 1st of month included
        return (new.date, PRECISION_DAY) if inside else None   # placeholder refined
    if new.precision == PRECISION_MONTH:
        if stored == new.date:
            return new.date, PRECISION_MONTH         # the 1st of that month IS the month
        if asserted == PRECISION_YEAR and same_year:
            return new.date, PRECISION_MONTH         # year placeholder refined to a month
        return (stored, label) if same_month and asserted != PRECISION_MONTH else None
    if not same_year:                                # evidence names a year only
        return None
    if (stored.month, stored.day) == (1, 1) and asserted != PRECISION_DAY:
        return new.date, PRECISION_YEAR
    return stored, label


def _notes(row: dict, misfiled: bool) -> str:
    series = (row_bank(row), row.get("doc_type"))
    out = [NOTE_MISFILED] if misfiled else []
    for n in ROW_NOTES:
        if (n.series is None or n.series == series) and re.search(
                n.pattern, row.get(n.field) or "", re.I):
            out.append(n.note)
    return "; ".join(out)


def plan_row(row: dict, *, rules: Optional[dict] = None,
             refine_only: bool = REFINE_ONLY) -> RowPlan:
    """Plan for one manifest row (pure: reads the row, writes nothing).

    `rules` defaults to dates.POLICY_DATE_RULES. The caller decides the
    scope; a series without a rule comes back `no-rule` with no change."""
    bank, code = row_bank(row), row.get("doc_type")
    title, document_url = row.get("title"), row_url(row)
    old_date = row.get("date") or None
    old_label = row.get("date_precision")
    label = old_label if old_label in PRECISIONS else None
    inf = infer_policy_date(bank, code, title=title, document_url=document_url,
                            source_url=row.get("source_url"), rules=rules)
    if inf.status == STATUS_NO_RULE:
        return RowPlan(action=inf.status, new_date=old_date, new_precision=old_label,
                       kind=row.get("date_kind") or "")
    misfiled = is_misfiled(bank, code, title=title, document_url=document_url, rules=rules)
    note = _notes(row, misfiled)
    if inf.status != STATUS_DATED:
        plan = RowPlan(action=inf.status, new_date=old_date, new_precision=old_label,
                       kind=row.get("date_kind") or "", rule=inf.rule, evidence=inf.detail,
                       note=note)
        # A row that stays undated must not keep claiming a precision.
        if (inf.status in (STATUS_NOT_A_REPORT, STATUS_NO_EVIDENCE)
                and old_date is None and old_label is not None):
            plan.updates["date_precision"] = None
            plan.new_precision = None
        # Not a document of the series: whatever its date, it has no kind.
        if inf.status == STATUS_NOT_A_REPORT and "date_kind" in row:
            plan.removals, plan.kind = ("date_kind",), ""
        return plan

    new = inf.match
    plan = RowPlan(action=ACTION_UNCHANGED, rule=inf.rule, evidence=new.evidence, note=note)
    stored = _stored_date(old_date) if old_date else None
    if old_date is None:
        target: Optional[tuple[date, Optional[str]]] = (new.date, new.precision)
    else:
        target = _target(stored, label, new) if stored else None
        if target is None and not refine_only:
            target = (new.date, new.precision)
    if target is None:
        plan.action = ACTION_MISMATCH
        plan.new_date, plan.new_precision = old_date, old_label
        plan.kind = row.get("date_kind") or ""
        return plan

    t_date, t_prec = target
    plan.new_date, plan.new_precision = t_date.isoformat(), t_prec
    if plan.new_date != old_date:
        plan.updates["date"] = plan.new_date
        if row.get("year") != t_date.year:
            plan.updates["year"] = t_date.year
    # A precision is written whenever the evidence states one the row does not
    # hold: a month, a year, or a day it CONFIRMS (a verified fact). A stored
    # day that is merely consistent with the evidence keeps the row as it is.
    if t_prec is not None and old_label != t_prec:
        plan.updates["date_precision"] = t_prec
    source = row.get("date_source")
    if plan.updates and (not source or ("date" in plan.updates
                                         and source in KINDLESS_DATE_SOURCES)):
        source = plan.updates["date_source"] = EVIDENCE_DATE_SOURCE
    # What the date IS, for the date and precision the row will hold.
    plan.kind = policy_date_kind(bank, code, date=t_date, precision=t_prec, date_source=source,
                                 title=title, document_url=document_url) or ""
    if plan.kind and row.get("date_kind") != plan.kind:
        plan.updates["date_kind"] = plan.kind
    elif not plan.kind and "date_kind" in row:
        plan.removals = ("date_kind",)

    if misfiled:
        plan.action = ACTION_MISFILED
    elif not plan.changes:
        plan.action = ACTION_UNCHANGED
    elif not set(plan.updates) - {"date_kind"}:
        plan.action = ACTION_KIND_ONLY
    elif "date" not in plan.updates and stored == new.date and new.precision == PRECISION_DAY:
        plan.action = ACTION_DAY_CONFIRMED
    else:
        plan.action = f"to-{t_prec}"
    return plan


def _in_scope(row: dict, banks: frozenset, types: frozenset) -> bool:
    return row_bank(row) in banks and row.get("doc_type") in types


def summary_table(per_series: dict[tuple[str, str], Counter]) -> str:
    """Markdown table: one line per bank x doc type, one column per action."""
    order = ["to-day", "to-month", "to-year", ACTION_DAY_CONFIRMED, ACTION_KIND_ONLY,
             ACTION_MISFILED, ACTION_UNCHANGED, "not-a-report", "no-evidence", "conflict",
             ACTION_MISMATCH, "no-rule"]
    cols = [a for a in order if any(c[a] for c in per_series.values())]
    lines = ["| bank | type | rows | " + " | ".join(cols) + " |",
             "|---|---|---:|" + "---:|" * len(cols)]
    total: Counter = Counter()
    for (bank, code), c in sorted(per_series.items()):
        total.update(c)
        lines.append(f"| {bank} | {code} | {sum(c.values())} | "
                     + " | ".join(str(c[a]) for a in cols) + " |")
    lines.append(f"| **all** | | {sum(total.values())} | "
                 + " | ".join(str(total[a]) for a in cols) + " |")
    return "\n".join(lines)


def run_policy_dates(bank_codes: Optional[Iterable[str]] = None,
                     doc_types: Optional[Iterable[str]] = None,
                     write: bool = False, csv_path: Optional[str] = None,
                     refine_only: bool = REFINE_ONLY,
                     config: Optional[Config] = None) -> dict:
    """Plan (and with `write`, apply) the policy-date backfill.

    Scope: `bank_codes` x `doc_types` (defaults POLICY_BANKS / POLICY_DOC_TYPES).
    Always writes the CSV of every row in scope (`csv_path`, default
    data/reports/policy_dates.csv) and prints a per-series summary. With
    `write`, the rows whose plan has updates are rewritten through
    storage.merge_rows_locked: each row is re-read under its bank's manifest
    lock and its plan re-computed there, so a concurrent sync is never
    reverted and only DATE_KEYS change. `refine_only=False` (see REFINE_ONLY)
    is refused without an explicit scope. Returns counts per action, plus
    `rows` (rows in scope) and `written` (rows rewritten)."""
    if not refine_only and not (bank_codes or doc_types):
        raise ValueError("moving dates out of their period (--allow-moves) needs an "
                         "explicit scope: --banks and/or --types")
    cfg = config or Config()
    banks = frozenset(bank_codes) if bank_codes else frozenset(POLICY_BANKS)
    types = frozenset(doc_types) if doc_types else frozenset(POLICY_DOC_TYPES)

    counts: Counter = Counter()
    per_series: dict[tuple[str, str], Counter] = {}
    to_write: dict[str, set[str]] = {}          # bank -> doc_ids with updates
    out = csv_path or str(cfg.reports_dir / "policy_dates.csv")
    if not csv_path:
        cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for row in iter_manifest_rows(cfg):
            if not _in_scope(row, banks, types):
                continue
            plan = plan_row(row, refine_only=refine_only)
            bank, code = row_bank(row), row.get("doc_type")
            counts[plan.action] += 1
            per_series.setdefault((bank, code), Counter())[plan.action] += 1
            if plan.changes and row.get("doc_id"):
                to_write.setdefault(bank, set()).add(row["doc_id"])
            w.writerow([row.get("doc_id"), bank, code, row.get("date") or "",
                        row.get("date_precision") or "", plan.new_date or "",
                        plan.new_precision or "", plan.kind, plan.action, plan.rule,
                        plan.evidence, plan.fields_changed(), plan.note,
                        row.get("title") or "", row_url(row) or ""])

    result = dict(counts)
    result["rows"] = sum(counts.values())
    result["written"] = 0
    print(summary_table(per_series))
    print(f"policy-dates: {result['rows']} row(s) in scope -> {out}", file=sys.stderr)

    if write and to_write:
        def merge(row: dict) -> bool:
            # Re-planned on the row as it is NOW, under the lock.
            if not _in_scope(row, banks, types):
                return False
            return plan_row(row, refine_only=refine_only).apply(row)
        result["written"] = merge_rows_locked(
            cfg, {bank: {did: merge for did in ids} for bank, ids in to_write.items()})
    n_planned = sum(len(v) for v in to_write.values())
    print(f"policy-dates: {n_planned} row(s) to update; "
          + (f"{result['written']} written" if write else "dry-run, nothing written "
             "(read the CSV, then re-run with --write)"), file=sys.stderr)
    return result
