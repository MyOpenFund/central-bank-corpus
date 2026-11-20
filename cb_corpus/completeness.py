"""Expected-vs-downloaded completeness matrix.

For each (bank, doc_type, year) cell we compare:
  - expected: from the adapter's published meeting calendar / archive count
              (None => unknown; treated as "needs runtime listing count")
  - downloaded: rows in the manifest

status:
  ok       downloaded >= expected (or expected unknown but downloaded > 0)
  partial  0 < downloaded < expected
  missing  expected > 0 and downloaded == 0
  unknown  expected is None and downloaded == 0
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from .adapters.base import get_adapter
from .banks import BIS_63
from .storage import Storage
from .taxonomy import DocType, FULL_SCOPE, by_code
from .models import SOURCE_KEY


def actual_counts(storage: Storage) -> dict[tuple[str, str, int], int]:
    counts: dict[tuple[str, str, int], int] = defaultdict(int)
    for row in storage.iter_manifest():
        year = row.get("year") or 0
        counts[(row.get(SOURCE_KEY), row["doc_type"], int(year))] += 1
    return counts


def _status(expected: Optional[int], downloaded: int) -> str:
    if expected is None:
        return "ok" if downloaded > 0 else "unknown"
    if downloaded >= expected and expected > 0:
        return "ok"
    if downloaded == 0:
        return "missing" if expected > 0 else "unknown"
    return "partial"


def build_matrix(years: Iterable[int],
                 bank_codes: Optional[Iterable[str]] = None,
                 scope: tuple[DocType, ...] = FULL_SCOPE,
                 storage: Optional[Storage] = None) -> "list[dict]":
    """Return matrix rows. Pure w.r.t. an in-memory storage manifest."""
    storage = storage or Storage()
    actual = actual_counts(storage)
    codes = list(bank_codes) if bank_codes else [b.code for b in BIS_63]
    years = list(years)
    rows: list[dict] = []
    for code in codes:
        adapter = get_adapter(code)
        supported = set(adapter.supported_types())
        for dt in scope:
            if dt not in supported:
                continue
            for year in years:
                exp = adapter.expected_count(dt, year)
                got = actual.get((code, dt.code, year), 0)
                rows.append({
                    "bank_code": code,
                    "doc_type": dt.code,
                    "year": year,
                    "expected": exp,
                    "downloaded": got,
                    "ratio": (None if not exp else round(got / exp, 2)),
                    "status": _status(exp, got),
                })
    return rows


def to_dataframe(rows: list[dict]):
    import pandas as pd
    return pd.DataFrame(rows)


def summarize(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        out[r["status"]] += 1
    return dict(out)


def export_csv(rows: list[dict], path) -> None:
    to_dataframe(rows).to_csv(path, index=False)
