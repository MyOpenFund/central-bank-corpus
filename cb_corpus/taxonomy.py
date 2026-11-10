"""Central-bank document-type taxonomy.

Codes A1..G3 follow the June 2026 planning taxonomy (monetary policy, press
conferences, speeches, research, reports, projections, supervisory/statistical).
Group letters A-F define the "full" scope; G (supervisory/statistical) is
optional and excluded from FULL_SCOPE by default.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class _T:
    code: str
    group: str
    label: str


class DocType(_T, Enum):
    # A. Monetary policy - decisions & deliberation
    A1 = ("A1", "A", "Rate-decision press release")
    A2 = ("A2", "A", "Policy statement")
    A3 = ("A3", "A", "Meeting minutes / accounts / summary of deliberations")
    A4 = ("A4", "A", "Voting record")
    # B. Press conferences & transcripts
    B1 = ("B1", "B", "Press-conference transcript / Q&A")
    B2 = ("B2", "B", "Opening remarks / webcast notes")
    # C. Speeches & interviews
    C1 = ("C1", "C", "Speech")
    C2 = ("C2", "C", "Interview / op-ed / testimony")
    # D. Research
    D1 = ("D1", "D", "Working paper")
    D2 = ("D2", "D", "Occasional / discussion paper / staff note")
    D3 = ("D3", "D", "Economic letter / research blog")
    # E. Reports
    E1 = ("E1", "E", "Monetary policy / inflation report")
    E2 = ("E2", "E", "Financial stability report")
    E3 = ("E3", "E", "Annual report")
    E4 = ("E4", "E", "Economic / quarterly bulletin")
    # F. Projections
    F1 = ("F1", "F", "Staff economic projections / forecasts")
    # G. Supervisory / statistical (optional - not in FULL_SCOPE)
    G1 = ("G1", "G", "Regulatory notice / consultation")
    G2 = ("G2", "G", "Statistical release")
    G3 = ("G3", "G", "Supervisory report")

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


FULL_SCOPE_GROUPS = ("A", "B", "C", "D", "E", "F")
FULL_SCOPE: tuple[DocType, ...] = tuple(
    dt for dt in DocType if dt.group in FULL_SCOPE_GROUPS
)


def by_code(code: str) -> DocType:
    for dt in DocType:
        if dt.code == code:
            return dt
    raise KeyError(f"unknown doc type: {code}")
