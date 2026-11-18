"""Shared date helper (cb_corpus/dates.py): a date and its precision from a
title or a URL. Pure functions, no network, no manifest."""
from __future__ import annotations

from datetime import date

import pytest

from cb_corpus.dates import (PICK_FIRST, PICK_LAST, find_title_date, find_url_date,
                             parse_title_date, parse_url_date)


# ---- titles -----------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    # two days, one month: the meeting ENDS on the last day named
    ("Minutes of the MPC Meeting held on 3 and 4 February 1999", (date(1999, 2, 4), "day")),
    ("Minutes of the Monetary Policy Committee Meeting ending 8 July 2015",
     (date(2015, 7, 8), "day")),
    ("Minutes of the special Monetary Policy Committee meeting ending on 10 March 2020",
     (date(2020, 3, 10), "day")),
    # two days over two months: a REAL first of the month
    ("Minutes of the MPC Meeting held on 31 July and 1 August 2002", (date(2002, 8, 1), "day")),
    ("Minutes of the MPC Meeting held on 18 September 2001", (date(2001, 9, 18), "day")),
    # ordinal suffixes, abbreviations, a comma before the year
    ("Meeting of 3rd and 4th Feb. 1999", (date(1999, 2, 4), "day")),
    ("Statement, 21st Sept 2010", (date(2010, 9, 21), "day")),
    ("Decision of 2 March, 2004", (date(2004, 3, 2), "day")),
    # month first (US / Japanese style)
    ("Meeting on January 28 and 29, 2016 [PDF 114KB]", (date(2016, 1, 29), "day")),
    ("Meeting on October 31 and November 1, 2016 [PDF 106KB]", (date(2016, 11, 1), "day")),
    ("Meeting on  May 21 and 22, 2015 [PDF 87KB]", (date(2015, 5, 22), "day")),
    ("Summary, fixed announcement date of June 4, 2025", (date(2025, 6, 4), "day")),
    # month only: first day of the month, precision month
    ("Bank Rate maintained at 0.1% - June 2021", (date(2021, 6, 1), "month")),
    ("Monetary Policy Report - November 2025", (date(2025, 11, 1), "month")),
    ("Monetary Policy Report - July 2026 (to be published at 12pm)", (date(2026, 7, 1), "month")),
    ("Bank Rate held at 0.25%, govt bond purchases at £435bn and corporate bond "
     "purchases at up to £10bn - December 2016", (date(2016, 12, 1), "month")),
    ("Bank Rate reduced to 4.75% - November 2024", (date(2024, 11, 1), "month")),
    # a file name used as a title
    ("november-2018.pdf", (date(2018, 11, 1), "month")),
    ("transcript-june-2020-mpc-decision-press-briefing.pdf", (date(2020, 6, 1), "month")),
    # nothing usable
    ("Bank Rate maintained at 0.5%", (None, None)),
    ("Monetary Policy Committee voted unanimously to raise Bank Rate to 0.75%", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
    # not a month name: "market", "mayor", "decide"
    ("Market 2020 outlook", (None, None)),
    ("The mayor 2019 report and the decide 2018 note", (None, None)),
    # an impossible day is not a date; the month still is one
    ("Meeting of 31 February 2010", (date(2010, 2, 1), "month")),
])
def test_parse_title_date(title, expected):
    assert parse_title_date(title) == expected


def test_title_rule_first_takes_the_first_day_named():
    t = "Minutes of the MPC Meeting held on 3 and 4 February 1999"
    assert parse_title_date(t, PICK_FIRST) == (date(1999, 2, 3), "day")
    assert parse_title_date(t, PICK_LAST) == (date(1999, 2, 4), "day")
    t = "Minutes of the MPC Meeting held on 31 July and 1 August 2002"
    assert parse_title_date(t, PICK_FIRST) == (date(2002, 7, 31), "day")
    t = "Meeting on October 31 and November 1, 2016"
    assert parse_title_date(t, PICK_FIRST) == (date(2016, 10, 31), "day")
    # a range over the new year: the first day belongs to the year before
    t = "Meeting held on 31 December and 1 January 2004"
    assert parse_title_date(t, PICK_FIRST) == (date(2003, 12, 31), "day")
    assert parse_title_date(t, PICK_LAST) == (date(2004, 1, 1), "day")


def test_title_rule_applies_across_several_dated_phrases():
    t = "Minutes of 5 May 2010, published 19 May 2010"
    assert parse_title_date(t, PICK_LAST) == (date(2010, 5, 19), "day")
    assert parse_title_date(t, PICK_FIRST) == (date(2010, 5, 5), "day")
    t = "Inflation Report - February 2019 and May 2019"
    assert parse_title_date(t, PICK_LAST) == (date(2019, 5, 1), "month")
    assert parse_title_date(t, PICK_FIRST) == (date(2019, 2, 1), "month")


def test_a_day_beats_a_month_wherever_it_stands():
    t = "August 2018 Inflation Report, meeting ending 1 August 2018"
    assert parse_title_date(t) == (date(2018, 8, 1), "day")


def test_unknown_title_rule_is_refused():
    with pytest.raises(ValueError):
        parse_title_date("3 February 1999", "middle")


def test_find_title_date_keeps_the_evidence():
    m = find_title_date("Minutes of the MPC Meeting held on 3 and 4 February 1999")
    assert (m.date, m.precision, m.evidence) == (date(1999, 2, 4), "day", "3 and 4 February 1999")
    m = find_title_date("Bank Rate maintained at 0.1% - June 2021")
    assert (m.date, m.precision, m.evidence) == (date(2021, 6, 1), "month", "June 2021")
    assert find_title_date("no date here") is None


# ---- URLs -------------------------------------------------------------

BOJ = r"/g(?P<yy>\d{2})(?P<m>\d{2})(?P<d>\d{2})\.pdf$"
SMP = r"/publications/smp/(?P<y>\d{4})/(?P<mon>feb|may|aug|nov)/?$"
MPR = r"/publications/mpr/mpr-(?P<y>\d{4})-(?P<m>\d{2})(?:-(?P<d>\d{2}))?(?:/|$)"


@pytest.mark.parametrize("url,pattern,expected", [
    ("https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2026/g260123.pdf", BOJ,
     (date(2026, 1, 23), "day")),
    # a real first of the month in a URL stays a day
    ("https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2016/g161101.pdf", BOJ,
     (date(2016, 11, 1), "day")),
    ("https://www.rba.gov.au/publications/smp/2020/may/", SMP, (date(2020, 5, 1), "month")),
    ("https://www.rba.gov.au/publications/smp/2020/may/boxes.html", SMP, (None, None)),
    ("https://www.bankofcanada.ca/publications/mpr/mpr-2016-04/", MPR, (date(2016, 4, 1), "month")),
    ("https://www.bankofcanada.ca/publications/mpr/mpr-2026-10-28/in-focus-6/", MPR,
     (date(2026, 10, 28), "day")),
    ("https://www.bankofcanada.ca/publications/mpr/", MPR, (None, None)),
    # two-digit years: the pivot sends 98 to 1998
    ("https://www.ecb.europa.eu/press/pr/date/1998/html/pr981201.en.html",
     r"pr(?P<yy>\d{2})(?P<m>\d{2})(?P<d>\d{2})\.", (date(1998, 12, 1), "day")),
    # a year alone: 1 January, precision year
    ("https://bank.example/annual-report-2019.pdf", r"report-(?P<y>\d{4})\.pdf$",
     (date(2019, 1, 1), "year")),
    # an impossible date is no date
    ("https://bank.example/g261340.pdf", BOJ, (None, None)),
    ("", BOJ, (None, None)),
    (None, BOJ, (None, None)),
])
def test_parse_url_date(url, pattern, expected):
    assert parse_url_date(url, pattern) == expected


def test_url_month_names_and_abbreviations():
    pat = r"(?<![a-z])(?P<mon>[a-z]+)-(?P<y>\d{4})(?!\d)"
    assert parse_url_date("https://boe/minutes/2015/mpc-october-2015", pat) == (
        date(2015, 10, 1), "month")
    assert parse_url_date("https://boe/minutes/2015/sept-2015", pat) == (date(2015, 9, 1), "month")
    # "report-2015": the pattern matches but "report" is not a month
    assert parse_url_date("https://boe/monetary-policy-report-2015", pat) == (None, None)


def test_url_pattern_tries_every_match_until_one_is_a_date():
    pat = r"(?<![a-z])(?P<mon>[a-z]+)-(?P<y>\d{4})(?!\d)"
    url = "https://boe/files/2025/monetary-policy-summary-and-minutes-june-2025.pdf"
    assert parse_url_date(url, pat) == (date(2025, 6, 1), "month")


def test_url_pattern_without_a_year_group_is_a_programming_error():
    with pytest.raises(ValueError):
        parse_url_date("https://x/01-02", r"(?P<m>\d{2})-(?P<d>\d{2})")


def test_find_url_date_keeps_the_evidence():
    m = find_url_date("https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2026/g260123.pdf", BOJ)
    assert (m.date, m.precision, m.evidence) == (date(2026, 1, 23), "day", "/g260123.pdf")
