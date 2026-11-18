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
  kind-only                     the date was already right; `date_kind` is added
  unchanged                     nothing to write (what a second run reports)
  not-a-report                  the URL is an index page, not a document: never dated
  no-evidence                   neither title nor URL names a date: left as it is
  conflict                      title and URL name different periods: left as it is
  mismatch                      the evidence is outside the period the stored
                                date asserts: left as it is (see REFINE_ONLY)
  no-rule                       the series has no rule (scope widened by hand)

How a real first-of-month is told from a placeholder: never by the day
number. A stored `2018-08-01` whose URL says `monetary20180801a` is confirmed
by its evidence and kept; a stored `2021-01-01` whose title says "June 2021"
is a placeholder and becomes `2021-06-01` with precision `month`.

Dry-run by default (a CSV to read); `--write` applies the same plan,
re-computed row by row under each bank's manifest lock. Idempotent.
"""
from __future__ import annotations

import csv
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from .config import Config
from .dates import (POLICY_DATE_KIND, PRECISION_DAY, PRECISION_MONTH, PRECISION_YEAR,
                    PRECISIONS, STATUS_DATED, STATUS_NO_EVIDENCE, STATUS_NOT_A_REPORT,
                    DateMatch, infer_policy_date)
from .storage import iter_manifest_rows, merge_rows_locked

#: Default scope: the policy core. Both are parameters of run_policy_dates
#: (CLI: --banks / --types); a row outside the scope is never read as a
#: candidate, and a series without a rule is never written whatever the scope.
POLICY_BANKS = ("ecb", "us", "gb", "au", "ca", "jp")
POLICY_DOC_TYPES = ("A1", "A2", "A3", "B1", "E1", "F1")

#: The only row keys this command may write.
DATE_KEYS = ("date", "year", "date_precision", "date_source", "date_kind")

#: A dated row WITHOUT a `date_precision` key is read downstream as a day
#: (the corpus' meaning before precision was recorded). The command therefore
#: writes a label only where that reading would be wrong (a month, a year) or
#: where it writes the date itself; a confirmed day keeps its row unlabelled.
ASSUMED_PRECISION_WHEN_UNLABELLED = PRECISION_DAY

#: `date_source` written with a date or a precision on a row that has none:
#: the evidence is the bank's own title or URL.
EVIDENCE_DATE_SOURCE = "bank_site"

#: Default for `refine_only`: the stored date may only be REFINED, inside the
#: period it already asserts (1 January -> anywhere in that year; the 1st of
#: a month -> anywhere in that month; undated -> anything). Evidence that
#: would move a date out of that period is reported as `mismatch` and the row
#: is left alone. `--allow-moves` turns the guard off.
REFINE_ONLY = True

ACTION_UNCHANGED = "unchanged"
ACTION_KIND_ONLY = "kind-only"
ACTION_MISMATCH = "mismatch"

CSV_COLUMNS = ("doc_id", "bank", "doc_type", "old_date", "old_precision", "new_date",
               "new_precision", "kind", "action", "rule", "evidence", "fields_changed",
               "title", "pdf_url")


@dataclass
class RowPlan:
    """What the command would do to one row. `updates` holds the keys to
    write (a subset of DATE_KEYS, empty = row left byte-identical);
    `new_date` / `new_precision` describe the row AFTER the update."""
    action: str
    new_date: Optional[str] = None
    new_precision: Optional[str] = None
    kind: str = ""
    rule: str = ""
    evidence: str = ""
    updates: dict = field(default_factory=dict)


def _stored_date(value) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _target(stored: date, stored_precision: Optional[str], new: DateMatch
            ) -> Optional[tuple[date, Optional[str]]]:
    """(date, precision) the row should hold given its stored date and the
    evidence, or None when the evidence is outside the period the stored date
    asserts. The stored date is a placeholder only for the period it is the
    first day of -- and only the evidence can say it is one."""
    same_year = stored.year == new.date.year
    same_month = same_year and stored.month == new.date.month
    year_start = (stored.month, stored.day) == (1, 1)
    if new.precision == PRECISION_DAY:
        if stored == new.date:
            return new.date, PRECISION_DAY           # confirmed, 1st of month included
        if (same_year and year_start) or (same_month and stored.day == 1):
            return new.date, PRECISION_DAY           # placeholder refined to the day
        return None
    if new.precision == PRECISION_MONTH:
        if stored == new.date or (same_year and year_start):
            return new.date, PRECISION_MONTH         # 1st of that month / of the year
        if same_month:
            return stored, stored_precision          # stored is finer: keep it
        return None
    if same_year:                                    # evidence names a year only
        return (new.date, PRECISION_YEAR) if year_start else (stored, stored_precision)
    return None


def plan_row(row: dict, *, rules: Optional[dict] = None, kinds: Optional[dict] = None,
             refine_only: bool = REFINE_ONLY) -> RowPlan:
    """Plan for one manifest row (pure: reads the row, writes nothing).

    `rules` / `kinds` default to dates.POLICY_DATE_RULES / POLICY_DATE_KIND.
    The caller decides the scope; a series without a rule comes back
    `no-rule` with no update."""
    bank, code = row.get("bank_code"), row.get("doc_type")
    old_date = row.get("date") or None
    old_label = row.get("date_precision")
    inf = infer_policy_date(bank, code, title=row.get("title"), pdf_url=row.get("pdf_url"),
                            source_url=row.get("source_url"), rules=rules)
    if inf.status != STATUS_DATED:
        plan = RowPlan(action=inf.status, new_date=old_date, new_precision=old_label,
                       rule=inf.rule, evidence=inf.detail)
        # A row that stays undated must not keep claiming a precision.
        if (inf.status in (STATUS_NOT_A_REPORT, STATUS_NO_EVIDENCE)
                and old_date is None and old_label is not None):
            plan.updates["date_precision"] = None
            plan.new_precision = None
        return plan

    new = inf.match
    kind = (POLICY_DATE_KIND if kinds is None else kinds).get((bank, code)) or ""
    plan = RowPlan(action=ACTION_UNCHANGED, kind=kind, rule=inf.rule, evidence=new.evidence)
    stored = _stored_date(old_date) if old_date else None
    if old_label in PRECISIONS:
        effective = old_label
    else:
        effective = ASSUMED_PRECISION_WHEN_UNLABELLED if old_date else None

    if old_date is None:
        target: Optional[tuple[date, Optional[str]]] = (new.date, new.precision)
    else:
        target = _target(stored, effective, new) if stored else None
        if target is None and not refine_only:
            target = (new.date, new.precision)
    if target is None:
        plan.action = ACTION_MISMATCH
        plan.new_date, plan.new_precision, plan.kind = old_date, old_label, ""
        return plan

    t_date, t_prec = target
    plan.new_date, plan.new_precision = t_date.isoformat(), t_prec
    if plan.new_date != old_date:
        plan.updates["date"] = plan.new_date
        if row.get("year") != t_date.year:
            plan.updates["year"] = t_date.year
    # The label is written when the unlabelled reading would be wrong, and
    # always next to a date this command writes (a new date states its own).
    if t_prec != effective or ("date" in plan.updates and old_label != t_prec):
        plan.updates["date_precision"] = t_prec
    if plan.updates and not row.get("date_source"):
        plan.updates["date_source"] = EVIDENCE_DATE_SOURCE
    if kind and row.get("date_kind") != kind:
        plan.updates["date_kind"] = kind

    if not plan.updates:
        plan.action = ACTION_UNCHANGED
    elif set(plan.updates) == {"date_kind"}:
        plan.action = ACTION_KIND_ONLY
    else:
        plan.action = f"to-{t_prec}"
    return plan


def _in_scope(row: dict, banks: frozenset, types: frozenset) -> bool:
    return row.get("bank_code") in banks and row.get("doc_type") in types


def summary_table(per_series: dict[tuple[str, str], Counter]) -> str:
    """Markdown table: one line per bank x doc type, one column per action."""
    order = ["to-day", "to-month", "to-year", ACTION_KIND_ONLY, ACTION_UNCHANGED,
             "not-a-report", "no-evidence", "conflict", ACTION_MISMATCH, "no-rule"]
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
    reverted and only DATE_KEYS change. Returns counts per action, plus
    `rows` (rows in scope) and `written` (rows rewritten)."""
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
            bank, code = row.get("bank_code"), row.get("doc_type")
            counts[plan.action] += 1
            per_series.setdefault((bank, code), Counter())[plan.action] += 1
            if plan.updates and row.get("doc_id"):
                to_write.setdefault(bank, set()).add(row["doc_id"])
            w.writerow([row.get("doc_id"), bank, code, row.get("date") or "",
                        row.get("date_precision") or "", plan.new_date or "",
                        plan.new_precision or "", plan.kind, plan.action, plan.rule,
                        plan.evidence, " ".join(plan.updates), row.get("title") or "",
                        row.get("pdf_url") or ""])

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
            updates = plan_row(row, refine_only=refine_only).updates
            row.update(updates)
            return bool(updates)
        result["written"] = merge_rows_locked(
            cfg, {bank: {did: merge for did in ids} for bank, ids in to_write.items()})
    n_planned = sum(len(v) for v in to_write.values())
    print(f"policy-dates: {n_planned} row(s) to update; "
          + (f"{result['written']} written" if write else "dry-run, nothing written "
             "(read the CSV, then re-run with --write)"), file=sys.stderr)
    return result
