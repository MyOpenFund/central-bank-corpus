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

The module also owns the two policy-core tables, per bank and document type:
`POLICY_DATE_KIND` (what the date IS: meeting, publication, period) and
`POLICY_DATE_RULES` (where the date is read from), with `infer_policy_date`
to apply the rules to one document.
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

#: What a date IS. One date field cannot say it by itself:
#:   meeting      the day the policy meeting ended / the decision was taken
#:                (minutes dated at their meeting are read weeks later)
#:   publication  the day (or month) the document was released
#:   period       the issue the document belongs to ("the May 2020 Statement"),
#:                a month, not an event
KIND_MEETING = "meeting"
KIND_PUBLICATION = "publication"
KIND_PERIOD = "period"
DATE_KINDS = (KIND_MEETING, KIND_PUBLICATION, KIND_PERIOD)

#: (bank_code, doc type code) -> what `date` is, for the policy core. This
#: table is the single owner of that knowledge: new rows take their
#: `date_kind` from it (DocRecord.to_row) and the `policy-dates` backfill
#: stamps it on existing rows. It records what each series' date has always
#: been -- it does not move any date to another meaning. A pair that is not
#: listed claims nothing (no `date_kind` key on the row).
POLICY_DATE_KIND: dict[tuple[str, str], str] = {
    # Federal Reserve: everything is dated at the FOMC meeting, except the
    # semi-annual Monetary Policy Report (its own release day).
    ("us", "A2"): KIND_MEETING,
    ("us", "A3"): KIND_MEETING,
    ("us", "B1"): KIND_MEETING,
    ("us", "F1"): KIND_MEETING,
    ("us", "E1"): KIND_PUBLICATION,
    # ECB: decision, statement and press conference on the meeting day; the
    # account is dated at its release, four weeks later; staff projections
    # are an issue (month).
    ("ecb", "A1"): KIND_MEETING,
    ("ecb", "A2"): KIND_MEETING,
    ("ecb", "B1"): KIND_MEETING,
    ("ecb", "A3"): KIND_PUBLICATION,
    ("ecb", "E1"): KIND_PERIOD,
    # Bank of England: minutes at the day the MPC meeting ended (or its
    # month); the Monetary Policy Report is an issue (month).
    ("gb", "A3"): KIND_MEETING,
    ("gb", "E1"): KIND_PERIOD,
    # Reserve Bank of Australia: decision and minutes at the board meeting;
    # the Statement on Monetary Policy is a quarterly issue.
    ("au", "A1"): KIND_MEETING,
    ("au", "A3"): KIND_MEETING,
    ("au", "E1"): KIND_PERIOD,
    # Bank of Canada: the summary of deliberations is dated at the month it
    # was published (two weeks after the decision); the Monetary Policy
    # Report is an issue.
    ("ca", "A3"): KIND_PUBLICATION,
    ("ca", "E1"): KIND_PERIOD,
    # Bank of Japan: minutes at the last day of the meeting.
    ("jp", "A3"): KIND_MEETING,
}


def date_kind_for(bank_code: str, doc_type) -> Optional[str]:
    """What `date` is for this bank and document type (a DocType or its
    code), from POLICY_DATE_KIND; None when the table does not say."""
    code = getattr(doc_type, "code", doc_type)
    return POLICY_DATE_KIND.get((bank_code, code))


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


# ---------------------------------------------------------------------------
# Policy core: where each series' date is read from
# ---------------------------------------------------------------------------

#: `<month name>-<year>` in a URL path ("mpc-october-2015", "13march-2020").
URL_MONTH_NAME_YEAR = (r"(?<![a-z])(?P<mon>" + "|".join(_MONTH_NAMES)
                       + r")-(?P<y>(?:19|20)\d{2})(?!\d)")
#: A full YYYYMMDD run of digits ("monetary20180801a", "/19990518/").
URL_YYYYMMDD = r"(?<!\d)(?P<y>(?:19|20)\d{2})(?P<m>[01]\d)(?P<d>[0-3]\d)(?!\d)"
#: ECB document file names: `<prefix>YYMMDD`, with or without the `ecb.` stem.
ECB_DECISION_URL = r"/(?:ecb\.)?(?:pr|mp)(?P<yy>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?!\d)"
ECB_STATEMENT_URL = r"/(?:ecb\.)?is(?P<yy>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?!\d)"
ECB_ACCOUNT_URL = r"/(?:ecb\.)?mg(?P<yy>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?!\d)"
ECB_PRESS_CONFERENCE_URL = r"/ecb\.ds(?P<yy>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?!\d)"
#: ECB staff projections: `...projections...YYYYMM` -- an issue, no day.
ECB_PROJECTIONS_URL = r"projections[a-z]*(?P<y>(?:19|20)\d{2})(?P<m>0[1-9]|1[0-2])(?!\d)"
#: RBA board minutes, two conventions: YYYY-MM-DD.html (2015+), DDMMYYYY.html.
RBA_MINUTES_URL_ISO = r"/rba-board-minutes/\d{4}/(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\.html$"
RBA_MINUTES_URL_DMY = r"/rba-board-minutes/\d{4}/(?P<d>\d{2})(?P<m>\d{2})(?P<y>\d{4})\.html$"
#: RBA Statement on Monetary Policy: /smp/<year>/<feb|may|aug|nov>/ -- an issue.
RBA_SMP_URL = r"/publications/smp/(?P<y>\d{4})/(?P<mon>feb|may|aug|nov)/?$"
#: RBA rate decisions: the URL (mr-11-01.html) carries no date. The adapter
#: reads the day from the listing's link text ("1 February 2011") and writes
#: it at the end of the title it builds.
RBA_DECISION_TITLE = r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})$"
#: Bank of Canada Monetary Policy Report: /mpr/mpr-YYYY-MM[-DD]/[sub-page/].
BOC_MPR_URL = r"/publications/mpr/mpr-(?P<y>\d{4})-(?P<m>\d{2})(?:-(?P<d>\d{2}))?(?:/|$)"
#: A Bank of Canada MPR document sits under an issue slug; anything else under
#: /publications/mpr/ is an index or a side page, not a report.
BOC_MPR_REPORT_URL = r"/publications/mpr/mpr-\d{4}-\d{2}"
#: Bank of Canada summary of deliberations: /<YYYY>/<MM>/<slug> = the month it
#: was PUBLISHED. (The slug also names the decision day; it is not used: the
#: date of this series has always been its publication month.)
BOC_DELIBERATIONS_URL = r"bankofcanada\.ca/(?P<y>\d{4})/(?P<m>\d{2})/"
#: Bank of Japan minutes: gYYMMDD.pdf = the last day of the meeting.
BOJ_MINUTES_URL = r"/g(?P<yy>\d{2})(?P<m>\d{2})(?P<d>\d{2})\.pdf$"

#: Row fields a rule step may read.
EVIDENCE_FIELDS = ("title", "pdf_url", "source_url")


@dataclass(frozen=True)
class DateStep:
    """One place to look for a date. `field` is the row field read; `pattern`
    a find_url_date pattern, or None to read natural language
    (find_title_date, with `pick`). `name` is what audit CSVs call the step."""
    name: str
    field: str
    pattern: Optional[str] = None
    pick: str = PICK_LAST


@dataclass(frozen=True)
class PolicyDateRule:
    """How one series (bank x doc type) is dated. `steps` are tried in order
    and the first that finds a date wins; a later step that finds a date in
    another period is a conflict (nothing is inferred). `report_pattern`,
    when set, must match the document URL: a row that does not match is not
    a document of the series at all (an index page) and is never dated."""
    steps: tuple[DateStep, ...]
    report_pattern: Optional[str] = None


def _url(pattern: str, name: str = "pdf_url") -> DateStep:
    return DateStep(name=name, field="pdf_url", pattern=pattern)


_FED_URL = PolicyDateRule(steps=(_url(URL_YYYYMMDD),))
# Bank of England: the title names the day the meeting ended ("held on 3 and
# 4 February 1999" -> the 4th) or only a month ("... - June 2021"); when the
# title names nothing, the page URL then the file URL give the month.
_BOE_TITLE_THEN_URLS = PolicyDateRule(steps=(
    DateStep(name="title", field="title", pick=PICK_LAST),
    DateStep(name="source_url", field="source_url", pattern=URL_MONTH_NAME_YEAR),
    DateStep(name="pdf_url", field="pdf_url", pattern=URL_MONTH_NAME_YEAR),
))

#: (bank_code, doc type code) -> where the date of the series is read from.
#: Same keys as POLICY_DATE_KIND. Used by the `policy-dates` backfill for
#: existing rows and by the producers of new rows, so both agree by
#: construction. A series that is not listed is never dated by inference.
POLICY_DATE_RULES: dict[tuple[str, str], PolicyDateRule] = {
    ("us", "A2"): _FED_URL,
    ("us", "A3"): _FED_URL,
    ("us", "B1"): _FED_URL,
    ("us", "E1"): _FED_URL,
    ("us", "F1"): _FED_URL,
    ("ecb", "A1"): PolicyDateRule(steps=(_url(ECB_DECISION_URL),)),
    ("ecb", "A2"): PolicyDateRule(steps=(_url(ECB_STATEMENT_URL),)),
    ("ecb", "A3"): PolicyDateRule(steps=(_url(ECB_ACCOUNT_URL),)),
    ("ecb", "B1"): PolicyDateRule(steps=(_url(ECB_PRESS_CONFERENCE_URL),)),
    ("ecb", "E1"): PolicyDateRule(steps=(_url(ECB_PROJECTIONS_URL),)),
    ("gb", "A3"): _BOE_TITLE_THEN_URLS,
    ("gb", "E1"): _BOE_TITLE_THEN_URLS,
    ("au", "A1"): PolicyDateRule(steps=(
        DateStep(name="title:iso-day", field="title", pattern=RBA_DECISION_TITLE),)),
    ("au", "A3"): PolicyDateRule(steps=(
        _url(RBA_MINUTES_URL_ISO, "pdf_url:iso"), _url(RBA_MINUTES_URL_DMY, "pdf_url:ddmmyyyy"))),
    ("au", "E1"): PolicyDateRule(steps=(_url(RBA_SMP_URL),)),
    ("ca", "A3"): PolicyDateRule(steps=(_url(BOC_DELIBERATIONS_URL),)),
    ("ca", "E1"): PolicyDateRule(steps=(_url(BOC_MPR_URL),), report_pattern=BOC_MPR_REPORT_URL),
    ("jp", "A3"): PolicyDateRule(steps=(_url(BOJ_MINUTES_URL),)),
}

STATUS_DATED = "dated"
STATUS_NO_RULE = "no-rule"
STATUS_NOT_A_REPORT = "not-a-report"
STATUS_NO_EVIDENCE = "no-evidence"
STATUS_CONFLICT = "conflict"


@dataclass(frozen=True)
class Inference:
    """Outcome of infer_policy_date. `match` and `rule` (the winning step's
    name) are set when `status` is "dated"; `detail` explains a conflict."""
    status: str
    match: Optional[DateMatch] = None
    rule: str = ""
    detail: str = ""


def same_period(a: DateMatch, b: DateMatch) -> bool:
    """Do two dates agree, each as far as it goes? Compared at the coarser of
    the two precisions: a day agrees with its own month, two days must be
    equal."""
    coarsest = max(PRECISIONS.index(a.precision), PRECISIONS.index(b.precision))
    if a.date.year != b.date.year:
        return False
    if coarsest >= PRECISIONS.index(PRECISION_YEAR):
        return True
    if a.date.month != b.date.month:
        return False
    return coarsest >= PRECISIONS.index(PRECISION_MONTH) or a.date == b.date


def infer_policy_date(bank_code: str, doc_type, *, title: Optional[str] = "",
                      pdf_url: Optional[str] = "", source_url: Optional[str] = "",
                      rules: Optional[dict] = None) -> Inference:
    """The date of one policy document, from its own title and URLs only.

    Applies the series' rule (`rules`, default POLICY_DATE_RULES): "no-rule"
    when the series has none, "not-a-report" when the URL is not a document
    of the series, "no-evidence" when no step finds a date, "conflict" when
    two steps find dates in different periods, else "dated" with the first
    step's match. Never reads the stored date: what is inferred does not
    depend on what the row holds today."""
    code = getattr(doc_type, "code", doc_type)
    rule = (POLICY_DATE_RULES if rules is None else rules).get((bank_code, code))
    if rule is None:
        return Inference(STATUS_NO_RULE)
    if rule.report_pattern and not re.search(rule.report_pattern, pdf_url or "", re.I):
        return Inference(STATUS_NOT_A_REPORT)
    fields = {"title": title, "pdf_url": pdf_url, "source_url": source_url}
    found: list[tuple[DateStep, DateMatch]] = []
    for step in rule.steps:
        text = fields[step.field]
        m = (find_title_date(text, step.pick) if step.pattern is None
             else find_url_date(text, step.pattern))
        if m is not None:
            found.append((step, m))
    if not found:
        return Inference(STATUS_NO_EVIDENCE)
    step, best = found[0]
    for other_step, other in found[1:]:
        if not same_period(best, other):
            return Inference(
                STATUS_CONFLICT, rule=step.name,
                detail=f"{step.name} says {best.evidence!r}, "
                       f"{other_step.name} says {other.evidence!r}")
    return Inference(STATUS_DATED, match=best, rule=step.name)
