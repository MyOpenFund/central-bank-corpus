"""Shared date helper: a date AND its precision from a title or a URL.

Pure functions (no network, no manifest). Every date comes back with how
precise it is:

  day    the text names the day
  month  the text names a month only -> the date is the FIRST DAY of that month
  year   the text names a year only  -> the date is 1 January of that year

That first-day convention is the manifest's: `date` is always a full
`YYYY-MM-DD`, and `date_precision` says how much of it is real. A date on the
first of a month is therefore NOT a placeholder by itself: only the precision
(and the evidence it was read from) says so.

Two entry points, each in two flavours (`parse_*` returns the plain
`(date, precision)` pair, `find_*` also keeps the text fragment the date was
read from, for audit CSVs):

  parse_title_date(title, rule)   natural-language dates ("3 and 4 February
                                  1999", "October 31 and November 1, 2016",
                                  "June 2021")
  parse_url_date(url, pattern)    a regex with named groups for the date parts
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

PRECISION_DAY = "day"
PRECISION_MONTH = "month"
PRECISION_YEAR = "year"
#: Every value `date_precision` may take besides None (unknown / undated).
PRECISIONS = (PRECISION_DAY, PRECISION_MONTH, PRECISION_YEAR)

#: Title rules: which date to keep when a title names several days ("held on
#: 3 and 4 February 1999", "31 July and 1 August 2002") or several dated
#: phrases. A meeting is dated at its END, a rate decision is taken on the
#: last day: PICK_LAST is the default.
PICK_LAST = "last"
PICK_FIRST = "first"
TITLE_RULES = (PICK_LAST, PICK_FIRST)

#: Two-digit years in URLs (`pr981201`, `g260123`): `yy` at or above the pivot
#: is 19yy, below it 20yy. No central bank document on the web predates 1970.
TWO_DIGIT_YEAR_PIVOT = 70

#: Years outside this range are not read as years (a 4-digit number in a
#: title is more often an amount or a reference).
MIN_YEAR, MAX_YEAR = 1900, 2100

_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december")
#: English month names and their usual abbreviations -> month number.
MONTHS: dict[str, int] = {}
for _i, _name in enumerate(_MONTH_NAMES, 1):
    MONTHS[_name] = _i
    MONTHS[_name[:3]] = _i
MONTHS["sept"] = 9

# A month name or abbreviation as a whole word, with an optional full stop.
_MON = (r"(?<![A-Za-z])(?:" + "|".join(sorted(MONTHS, key=len, reverse=True))
        + r")(?![A-Za-z])\.?")
_DAY = r"(?<!\d)\d{1,2}(?!\d)(?:st|nd|rd|th)?"
_SEP = r"\s*(?:and|&|to|-|–|—|/)\s*"
_YEAR = r"(?<!\d)\d{4}(?!\d)"

# "3 February 1999", "3 and 4 February 1999", "31 July and 1 August 2002"
_DMY_RE = re.compile(
    rf"(?:(?P<d1>{_DAY})(?:\s+(?P<m1>{_MON}))?{_SEP})?"
    rf"(?P<d2>{_DAY})\s+(?P<m2>{_MON}),?\s+(?P<y>{_YEAR})", re.I)
# "June 4, 2025", "January 28 and 29, 2016", "October 31 and November 1, 2016"
_MDY_RE = re.compile(
    rf"(?P<m1>{_MON})\s+(?P<d1>{_DAY})"
    rf"(?:{_SEP}(?:(?P<m2>{_MON})\s+)?(?P<d2>{_DAY}))?,?\s+(?P<y>{_YEAR})", re.I)
# "June 2021", "november-2018", "Sept. 2010"
_MY_RE = re.compile(rf"(?P<m>{_MON})[\s,_\-]+(?P<y>{_YEAR})", re.I)


@dataclass(frozen=True)
class DateMatch:
    """A date read from a text: the date (first day of the period for a month
    or a year), its precision, and the fragment it was read from."""
    date: date
    precision: str
    evidence: str


def _month(name: str) -> Optional[int]:
    return MONTHS.get(name.strip().rstrip(".").lower())


def _day(text: str) -> int:
    return int(re.match(r"\d+", text).group(0))


def _safe_date(y: int, m: int, d: int) -> Optional[date]:
    if not MIN_YEAR <= y <= MAX_YEAR:
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _day_candidates(title: str) -> list[tuple[int, date, date, str]]:
    """(position, first day, last day, fragment) for every dated phrase that
    names a day. A phrase naming one day has first == last."""
    out: list[tuple[int, date, date, str]] = []
    for m in _DMY_RE.finditer(title):
        y = int(m["y"])
        last = _safe_date(y, _month(m["m2"]), _day(m["d2"]))
        if last is None:
            continue
        first = last
        if m["d1"]:
            mo1 = _month(m["m1"]) if m["m1"] else last.month
            # "31 December and 1 January 2004": the first day is in 2003
            first = _safe_date(y - 1 if mo1 > last.month else y, mo1, _day(m["d1"])) or last
        out.append((m.start(), first, last, m.group(0)))
    for m in _MDY_RE.finditer(title):
        y = int(m["y"])
        first = _safe_date(y, _month(m["m1"]), _day(m["d1"]))
        if first is None:
            continue
        last = first
        if m["d2"]:
            mo2 = _month(m["m2"]) if m["m2"] else first.month
            last = _safe_date(y, mo2, _day(m["d2"])) or first
            if last < first:        # "December 31 and January 1, 2004"
                first = _safe_date(y - 1, first.month, first.day) or first
        out.append((m.start(), first, last, m.group(0)))
    return sorted(out, key=lambda c: c[0])


def find_title_date(title: Optional[str], rule: str = PICK_LAST) -> Optional[DateMatch]:
    """The date a title names, with its precision and the matching fragment.

    A phrase naming a day always wins over a month-only phrase, wherever it
    stands (it is more precise). Among several candidates of the same
    precision `rule` decides: PICK_LAST keeps the last day of the last
    phrase, PICK_FIRST the first day of the first phrase. None when the title
    names no date. Month names are English, full or abbreviated."""
    if rule not in TITLE_RULES:
        raise ValueError(f"unknown title rule {rule!r} (expected one of {TITLE_RULES})")
    if not title:
        return None
    days = _day_candidates(title)
    if days:
        _pos, first, last, frag = days[-1] if rule == PICK_LAST else days[0]
        return DateMatch(last if rule == PICK_LAST else first, PRECISION_DAY, frag)
    months: list[DateMatch] = []
    for m in _MY_RE.finditer(title):
        d = _safe_date(int(m["y"]), _month(m["m"]), 1)
        if d is not None:
            months.append(DateMatch(d, PRECISION_MONTH, m.group(0)))
    if months:
        return months[-1] if rule == PICK_LAST else months[0]
    return None


def parse_title_date(title: Optional[str], rule: str = PICK_LAST
                     ) -> tuple[Optional[date], Optional[str]]:
    """`(date, precision)` named by a title, `(None, None)` when it names
    none. See find_title_date."""
    m = find_title_date(title, rule)
    return (m.date, m.precision) if m else (None, None)


def _compiled(pattern) -> re.Pattern:
    return pattern if isinstance(pattern, re.Pattern) else re.compile(pattern, re.I)


def find_url_date(url: Optional[str], pattern) -> Optional[DateMatch]:
    """The date a URL (or any text) carries, read by `pattern`: a regex (text
    or compiled; text is compiled case-insensitive) with NAMED groups

      y    4-digit year            yy   2-digit year (TWO_DIGIT_YEAR_PIVOT)
      m    month number            mon  English month name or abbreviation
      d    day number

    A year group is mandatory; the precision follows the groups that matched
    (day when `d` and a month did, month when only a month did, year
    otherwise). Every match of the pattern is tried in order until one is a
    real date (a pattern may match "report-2015" before "june-2025"). None
    when no match is a date."""
    pat = _compiled(pattern)
    names = set(pat.groupindex)
    if not names & {"y", "yy"}:
        raise ValueError(f"date pattern without a year group (y or yy): {pat.pattern!r}")
    if not url:
        return None
    for m in pat.finditer(url):
        g = {k: v for k, v in m.groupdict().items() if v}
        if "y" in g:
            y = int(g["y"])
        elif "yy" in g:
            yy = int(g["yy"])
            y = (1900 if yy >= TWO_DIGIT_YEAR_PIVOT else 2000) + yy
        else:
            continue
        mo = int(g["m"]) if "m" in g else _month(g["mon"]) if "mon" in g else None
        if mo is None and ("m" in g or "mon" in g):
            continue                       # a month group matched a non-month
        if mo is None:
            d, prec = _safe_date(y, 1, 1), PRECISION_YEAR
        elif "d" in g:
            d, prec = _safe_date(y, mo, int(g["d"])), PRECISION_DAY
        else:
            d, prec = (_safe_date(y, mo, 1) if 1 <= mo <= 12 else None), PRECISION_MONTH
        if d is not None:
            return DateMatch(d, prec, m.group(0))
    return None


def parse_url_date(url: Optional[str], pattern) -> tuple[Optional[date], Optional[str]]:
    """`(date, precision)` carried by a URL, `(None, None)` when `pattern`
    finds none. See find_url_date."""
    m = find_url_date(url, pattern)
    return (m.date, m.precision) if m else (None, None)
