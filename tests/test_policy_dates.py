"""`policy-dates`: the backfill that gives every policy-core row the best date
its own title or URL carries, an honest precision and a `date_kind`.

Fixtures are shaped on real manifest rows and deliberately awkward: two dates
in a title, a meeting that really ended on the 1st, month-only titles, a page
not yet published, an index page that is not a report, undated rows, rows
already right, a title that disagrees with its URL. No network anywhere."""
from __future__ import annotations

import csv
import json
from datetime import date

import pytest

from cb_corpus import policy_dates
from cb_corpus.cli import main
from cb_corpus.config import Config
from cb_corpus.dates import POLICY_DATE_KIND, POLICY_DATE_RULES, infer_policy_date
from cb_corpus.policy_dates import (DATE_KEYS, POLICY_BANKS, POLICY_DOC_TYPES, plan_row,
                                    run_policy_dates)
from cb_corpus.storage import iter_manifest_rows

BOE = "https://www.bankofengland.co.uk"
FED = "https://www.federalreserve.gov"
BOC = "https://www.bankofcanada.ca"


def row(bank, dt, title, document_url, stored, source_url="", **extra):
    """A manifest row as the corpus holds them; `stored` is its date today."""
    r = {"corpus": "central-bank", "source_code": bank, "doc_type": dt, "title": title, "document_url": document_url,
         "source_url": source_url, "date": stored, "language": "en",
         "provenance": "bank_site", "mime_type": "application/pdf",
         "sha256": "sha-" + document_url[-12:], "local_path": "raw/" + document_url[-12:],
         "doc_id": f"{bank}-{dt}-{abs(hash(document_url)) % 10**8}",
         "year": int(stored[:4]) if stored else None}
    r.update(extra)
    return r


def gb_minutes(title, stored="1999-01-01", slug="monetary-policy-committee-february-1999",
               **extra):
    y = stored[:4]
    return row("gb", "A3", title, f"{BOE}/-/media/boe/files/minutes/{y}/x.pdf", stored,
               source_url=f"{BOE}/minutes/{y}/{slug}", **extra)


# ---- the rule table ---------------------------------------------------

def test_every_dated_series_has_a_rule_and_every_rule_a_kind():
    assert set(POLICY_DATE_RULES) == set(POLICY_DATE_KIND)
    assert {b for b, _ in POLICY_DATE_RULES} <= set(POLICY_BANKS)
    assert {t for _, t in POLICY_DATE_RULES} <= set(POLICY_DOC_TYPES)


def test_default_scope_is_the_policy_core():
    assert POLICY_BANKS == ("ecb", "us", "gb", "au", "ca", "jp")
    assert POLICY_DOC_TYPES == ("A1", "A2", "A3", "B1", "E1", "F1")


@pytest.mark.parametrize("bank,dt,title,document_url,source_url,expected", [
    # Federal Reserve: the 8 digits of the document URL
    ("us", "A2", "FOMC statement 2018-08-01",
     f"{FED}/newsevents/pressreleases/monetary20180801a.htm", "", ("2018-08-01", "day")),
    ("us", "A2", "x", f"{FED}/boarddocs/press/general/1999/19990518/default.htm", "",
     ("1999-05-18", "day")),
    ("us", "A3", "x", f"{FED}/fomc/MINUTES/1996/19960130min.htm", "", ("1996-01-30", "day")),
    ("us", "B1", "x", f"{FED}/mediacenter/files/FOMCpresconf20240501.pdf", "",
     ("2024-05-01", "day")),
    ("us", "E1", "x", f"{FED}/monetarypolicy/files/20110301_mprfullreport.pdf", "",
     ("2011-03-01", "day")),
    ("us", "F1", "x", f"{FED}/monetarypolicy/files/FOMC20150617SEPcompilation.pdf", "",
     ("2015-06-17", "day")),
    # ECB: YYMMDD after the series prefix; YYYYMM for staff projections
    ("ecb", "A1", "x", "https://www.ecb.europa.eu/press/pr/date/2001/html/pr010301.en.html", "",
     ("2001-03-01", "day")),
    ("ecb", "A1", "x",
     "https://www.ecb.europa.eu/press/pr/date/2018/html/ecb.mp180726~aa11.en.html", "",
     ("2018-07-26", "day")),
    ("ecb", "A2", "x", "https://www.ecb.europa.eu/press/press_conference/"
     "monetary-policy-statement/1998/html/is981201.en.html", "", ("1998-12-01", "day")),
    ("ecb", "A3", "x", "https://www.ecb.europa.eu/press/accounts/2023/html/"
     "ecb.mg230601~9d35f80dee.en.html", "", ("2023-06-01", "day")),
    ("ecb", "B1", "x", "https://www.ecb.europa.eu/press/press_conference/"
     "monetary-policy-statement/shared/pdf/ecb.ds250605~dc79b630e3.en.pdf", "",
     ("2025-06-05", "day")),
    ("ecb", "E1", "ECB E1 2016-07-01",
     "https://www.ecb.europa.eu/pub/pdf/other/staffprojectionsguide201607.en.pdf", "",
     ("2016-07-01", "month")),
    ("ecb", "E1", "x", "https://www.ecb.europa.eu/press/projections/html/"
     "ecb.projections202603_eurosystemstaff~da4f97a747.en.html", "", ("2026-03-01", "month")),
    # RBA: decisions carry the day in the title only; minutes in the URL
    # (two conventions); the Statement is a quarterly issue
    ("au", "A1", "RBA monetary policy decision 2011-02-01",
     "https://www.rba.gov.au/media-releases/2011/mr-11-01.html", "", ("2011-02-01", "day")),
    ("au", "A3", "x",
     "https://www.rba.gov.au/monetary-policy/rba-board-minutes/2009/01122009.html", "",
     ("2009-12-01", "day")),
    ("au", "A3", "x",
     "https://www.rba.gov.au/monetary-policy/rba-board-minutes/2016/2016-11-01.html", "",
     ("2016-11-01", "day")),
    ("au", "E1", "RBA Statement on Monetary Policy 2020-05-01",
     "https://www.rba.gov.au/publications/smp/2020/may/", "", ("2020-05-01", "month")),
    # Bank of Canada
    ("ca", "A3", "BoC governing council deliberations 2025-06",
     f"{BOC}/2025/06/summary-governing-council-deliberations-fixed-announcement-"
     "date-of-june-4-2025", "", ("2025-06-01", "month")),
    ("ca", "E1", "BoC Monetary Policy Report 2016-04", f"{BOC}/publications/mpr/mpr-2016-04/",
     "", ("2016-04-01", "month")),
    ("ca", "E1", "", f"{BOC}/publications/mpr/mpr-2024-07/global-economy/", "",
     ("2024-07-01", "month")),
    ("ca", "E1", "", f"{BOC}/publications/mpr/mpr-2026-10-28/tariff-assumptions/", "",
     ("2026-10-28", "day")),
    # Bank of Japan
    ("jp", "A3", "Meeting on January 22 and 23, 2026 [PDF 292KB]",
     "https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2026/g260123.pdf", "",
     ("2026-01-23", "day")),
    ("jp", "A3", "Meeting on October 31 and November 1, 2016 [PDF 106KB]",
     "https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2016/g161101.pdf", "",
     ("2016-11-01", "day")),
    # Bank of England: the title first, then the page URL, then the file URL
    ("gb", "A3", "Minutes of the MPC Meeting held on 3 and 4 February 1999",
     f"{BOE}/-/media/boe/files/minutes/1999/minutes-february-1999.pdf",
     f"{BOE}/minutes/1999/monetary-policy-committee-february-1999", ("1999-02-04", "day")),
    ("gb", "A3", "Bank Rate maintained at 0.1% - June 2021",
     f"{BOE}/-/media/boe/files/monetary-policy-summary-and-minutes/2021/june-2021.pdf",
     f"{BOE}/monetary-policy-summary-and-minutes/2021/june-2021", ("2021-06-01", "month")),
    ("gb", "A3", "Bank Rate maintained at 0.5%",
     f"{BOE}/-/media/boe/files/monetary-policy-summary-and-minutes/2018/march-2018.pdf",
     f"{BOE}/monetary-policy-summary-and-minutes/2018/march-2018", ("2018-03-01", "month")),
    ("gb", "A3", "Bank of England cuts Bank Rate to 0.25% and introduces a package of "
     "measures designed to provide...",
     f"{BOE}/-/media/boe/files/monetary-policy-summary-and-minutes/2016/august-2016.pdf",
     f"{BOE}/monetary-policy-summary-and-minutes/2016/mpc-august-2016", ("2016-08-01", "month")),
    ("gb", "E1", "Monetary Policy Report - November 2025",
     f"{BOE}/-/media/boe/files/monetary-policy-report/2025/november/"
     "monetary-policy-report-november-2025.pdf",
     f"{BOE}/monetary-policy-report/2025/november-2025", ("2025-11-01", "month")),
])
def test_infer_policy_date(bank, dt, title, document_url, source_url, expected):
    inf = infer_policy_date(bank, dt, title=title, document_url=document_url, source_url=source_url)
    assert inf.status == "dated", inf
    assert (inf.match.date.isoformat(), inf.match.precision) == expected
    assert inf.rule and inf.match.evidence


def test_infer_outside_the_table_has_no_rule():
    assert infer_policy_date("gb", "D1", title="Paper, 3 May 2020", document_url="x").status == "no-rule"
    assert infer_policy_date("ch", "A3", title="3 May 2020", document_url="x").status == "no-rule"


def test_infer_reports_an_index_page_instead_of_dating_it():
    for url in (f"{BOC}/publications/mpr/", f"{BOC}/content_type/publications/mpr/",
                f"{BOC}/publications/mpr/qualitative-research-on-the-mpr/"):
        assert infer_policy_date("ca", "E1", title="", document_url=url).status == "not-a-report"


def test_infer_flags_a_title_that_disagrees_with_its_url():
    inf = infer_policy_date(
        "gb", "A3", title="Minutes of the MPC Meeting held on 5 and 6 June 2013",
        document_url=f"{BOE}/-/media/boe/files/minutes/2013/minutes-july-2013.pdf",
        source_url=f"{BOE}/minutes/2013/monetary-policy-committee-july-2013")
    assert inf.status == "conflict"
    assert "June 2013" in inf.detail and "july-2013" in inf.detail


def test_infer_title_day_agrees_with_a_url_that_only_knows_the_month():
    inf = infer_policy_date(
        "gb", "A3", title="Minutes of the special Monetary Policy Committee meeting "
        "ending on 10 March 2020",
        document_url=f"{BOE}/-/media/boe/files/monetary-policy-summary-and-minutes/2020/"
        "march13-2020.pdf",
        source_url=f"{BOE}/monetary-policy-summary-and-minutes/2020/13march-2020")
    assert inf.status == "dated" and inf.match.date == date(2020, 3, 10)


def test_infer_without_evidence():
    inf = infer_policy_date("gb", "A3", title="Bank Rate maintained at 0.5%",
                            document_url=f"{BOE}/x.pdf", source_url=f"{BOE}/y")
    assert inf.status == "no-evidence"


# ---- plan_row: what the backfill would write on one row ---------------

def test_two_dates_in_a_title_take_the_meeting_end():
    p = plan_row(gb_minutes("Minutes of the MPC Meeting held on 3 and 4 February 1999"))
    assert p.action == "to-day"
    assert p.updates == {"date": "1999-02-04", "date_precision": "day",
                         "date_source": "bank_site", "date_kind": "meeting"}
    assert p.evidence == "3 and 4 February 1999" and p.rule == "title"


def test_ending_phrase():
    p = plan_row(gb_minutes("Minutes of the Monetary Policy Committee Meeting ending 8 July 2015",
                            stored="2015-01-01", slug="monetary-policy-committee-july-2015"))
    assert (p.action, p.updates["date"]) == ("to-day", "2015-07-08")


def test_a_meeting_that_really_ended_on_the_first_becomes_that_day():
    p = plan_row(gb_minutes("Minutes of the MPC Meeting held on 31 July and 1 August 2002",
                            stored="2002-01-01", slug="monetary-policy-committee-august-2002"))
    assert (p.action, p.updates["date"], p.updates["date_precision"]) == (
        "to-day", "2002-08-01", "day")


@pytest.mark.parametrize("r", [
    row("us", "A2", "FOMC statement 2018-08-01",
        f"{FED}/newsevents/pressreleases/monetary20180801a.htm", "2018-08-01"),
    row("us", "E1", "FOMC Monetary Policy Report 2011-03-01",
        f"{FED}/monetarypolicy/files/20110301_mprfullreport.pdf", "2011-03-01"),
    row("ecb", "A1", "Monetary policy decision 2001-03-01",
        "https://www.ecb.europa.eu/press/pr/date/2001/html/pr010301.en.html", "2001-03-01"),
    row("ecb", "A3", "Monetary policy account 2023-06-01",
        "https://www.ecb.europa.eu/press/accounts/2023/html/ecb.mg230601~9d35f80dee.en.html",
        "2023-06-01"),
    row("au", "A1", "RBA monetary policy decision 2011-02-01",
        "https://www.rba.gov.au/media-releases/2011/mr-11-01.html", "2011-02-01"),
    row("au", "A3", "RBA board minutes 2009-12-01",
        "https://www.rba.gov.au/monetary-policy/rba-board-minutes/2009/01122009.html",
        "2009-12-01"),
    row("jp", "A3", "Meeting on October 31 and November 1, 2016 [PDF 106KB]",
        "https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2016/g161101.pdf", "2016-11-01"),
])
def test_a_real_first_of_the_month_is_never_repaired(r):
    """The day number proves nothing: the URL or title names the 1st, so the
    date stays. The day is now a verified fact and is labelled as one."""
    p = plan_row(r)
    assert p.action == "day-confirmed"
    assert set(p.updates) == {"date_precision", "date_source", "date_kind"}
    assert "date" not in p.updates and "year" not in p.updates
    assert p.updates["date_precision"] == "day" and p.updates["date_source"] == "bank_site"
    assert p.new_date == r["date"] and p.new_precision == "day"


def test_a_confirmed_day_already_labelled_only_gains_its_kind():
    r = row("us", "A2", "FOMC statement 2018-08-01",
            f"{FED}/newsevents/pressreleases/monetary20180801a.htm", "2018-08-01",
            date_precision="day", date_source="bank_site")
    p = plan_row(r)
    assert p.action == "kind-only" and p.updates == {"date_kind": "meeting"}
    assert p.new_precision == "day"


def test_what_the_csv_says_is_what_the_row_will_hold():
    """`new_date` / `new_precision` are the row after the write, key for key."""
    rows = [
        row("us", "A2", "x", f"{FED}/newsevents/pressreleases/monetary20180801a.htm",
            "2018-08-01"),
        gb_minutes("Minutes of the MPC Meeting held on 3 and 4 February 1999"),
        gb_minutes("Bank Rate maintained at 0.1% - June 2021", stored="2021-06-24",
                   slug="june-2021"),
        row("ca", "E1", "", f"{BOC}/publications/mpr/", None, date_precision="day"),
    ]
    for r in rows:
        p = plan_row(r)
        after = dict(r)
        p.apply(after)
        assert (after.get("date"), after.get("date_precision")) == (p.new_date, p.new_precision)
        assert after.get("date_kind", "") == p.kind


def test_month_only_titles_get_month_precision():
    p = plan_row(gb_minutes("Bank Rate maintained at 0.1% - June 2021", stored="2021-01-01",
                            slug="june-2021"))
    assert p.action == "to-month"
    assert p.updates == {"date": "2021-06-01", "date_precision": "month",
                         "date_source": "bank_site", "date_kind": "meeting"}
    r = row("gb", "E1", "Monetary Policy Report - November 2025",
            f"{BOE}/-/media/boe/files/monetary-policy-report/2025/november/"
            "monetary-policy-report-november-2025.pdf", "2025-01-01",
            source_url=f"{BOE}/monetary-policy-report/2025/november-2025")
    p = plan_row(r)
    assert (p.action, p.updates["date"], p.updates["date_kind"]) == (
        "to-month", "2025-11-01", "period")


def test_a_page_not_yet_published_is_dated_by_its_title_month():
    r = row("gb", "E1", "Monetary Policy Report - July 2026 (to be published at 12pm)",
            f"{BOE}/monetary-policy-report/2026/july-2026", "2026-01-01",
            source_url=f"{BOE}/monetary-policy-report/2026/july-2026")
    p = plan_row(r)
    assert (p.action, p.updates["date"], p.updates["date_precision"]) == (
        "to-month", "2026-07-01", "month")


def test_a_january_issue_keeps_its_date_and_gains_the_month_label():
    r = row("ca", "E1", "BoC Monetary Policy Report 2025-01",
            f"{BOC}/publications/mpr/mpr-2025-01/", "2025-01-01")
    p = plan_row(r)
    assert p.action == "to-month"
    assert p.updates == {"date_precision": "month", "date_source": "bank_site",
                         "date_kind": "period"}


def test_a_wrong_day_label_on_a_month_is_corrected():
    r = row("au", "E1", "RBA Statement on Monetary Policy 2026-08-01",
            "https://www.rba.gov.au/publications/smp/2026/aug/", "2026-08-01",
            date_precision="day", date_source="bank_site")
    p = plan_row(r)
    assert p.action == "to-month"
    assert p.updates == {"date_precision": "month", "date_kind": "period"}


def test_publication_month_of_the_canadian_deliberations_is_kept_and_labelled():
    r = row("ca", "A3", "BoC governing council deliberations 2025-06",
            f"{BOC}/2025/06/summary-governing-council-deliberations-fixed-announcement-"
            "date-of-june-4-2025", "2025-06-01")
    p = plan_row(r)
    # the decision day in the slug is NOT taken: no date changes meaning here
    assert p.action == "to-month"
    assert p.updates == {"date_precision": "month", "date_source": "bank_site",
                         "date_kind": "publication"}


def test_undated_rows_get_their_date_from_the_url():
    r = row("jp", "A3", "Meeting on January 22 and 23, 2026 [PDF 292KB]",
            "https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2026/g260123.pdf", None,
            date_precision="day", date_source="bank_site")
    p = plan_row(r)
    assert p.action == "to-day"
    assert p.updates == {"date": "2026-01-23", "year": 2026, "date_kind": "meeting"}
    r = row("ca", "E1", "", f"{BOC}/publications/mpr/mpr-2024-07/in-focus-1/", None,
            date_precision="day", date_source="bank_site")
    p = plan_row(r)
    assert p.action == "to-month"
    assert p.updates == {"date": "2024-07-01", "year": 2024, "date_precision": "month",
                         "date_kind": "period"}


def test_an_index_page_is_reported_not_dated_and_loses_its_false_label():
    r = row("ca", "E1", "", f"{BOC}/publications/mpr/", None,
            date_precision="day", date_source="bank_site")
    p = plan_row(r)
    assert p.action == "not-a-report"
    assert p.updates == {"date_precision": None}      # an undated row has no precision
    r2 = dict(r, date_precision=None)
    assert plan_row(r2).updates == {}


def test_a_title_that_disagrees_with_its_url_is_left_alone():
    r = gb_minutes("Minutes of the MPC Meeting held on 5 and 6 June 2013", stored="2013-01-01",
                   slug="monetary-policy-committee-july-2013")
    p = plan_row(r)
    assert p.action == "conflict" and p.updates == {}


def test_a_date_is_refined_never_moved():
    # stored in 2014, title in 2013: not a refinement of the stored year
    r = gb_minutes("Minutes of the MPC Meeting held on 17 and 18 December 2013",
                   stored="2014-01-01", slug="monetary-policy-committee")
    p = plan_row(r)
    assert p.action == "mismatch" and p.updates == {}
    # a stored real day is never replaced by another day
    r = row("us", "A3", "FOMC minutes 2013-05-02",
            f"{FED}/monetarypolicy/files/fomcminutes20130501.pdf", "2013-05-02")
    assert plan_row(r).action == "mismatch"
    # a stored first-of-month is a placeholder only for ITS month
    r = row("jp", "A3", "x", "https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2016/g161219.pdf",
            "2016-11-01")
    assert plan_row(r).action == "mismatch"
    # the guard is a parameter
    p = plan_row(r, refine_only=False)
    assert (p.action, p.updates["date"]) == ("to-day", "2016-12-19")


def test_a_first_of_month_placeholder_is_refined_inside_its_month():
    r = row("jp", "A3", "x", "https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2016/g161119.pdf",
            "2016-11-01")
    p = plan_row(r)
    assert (p.action, p.updates["date"]) == ("to-day", "2016-11-19")


def test_a_stored_day_inside_the_evidence_month_is_kept():
    r = row("gb", "A3", "Bank Rate maintained at 0.1% - June 2021", f"{BOE}/x.pdf", "2021-06-24",
            date_precision="day", date_source="bank_site")
    p = plan_row(r)
    assert p.action == "kind-only" and p.updates == {"date_kind": "meeting"}
    assert (p.new_date, p.new_precision) == ("2021-06-24", "day")


def test_an_unconfirmed_day_stays_unlabelled():
    """The title only names the month: the stored day is plausible, not
    verified. No label is written and the CSV shows none."""
    r = row("gb", "A3", "Bank Rate maintained at 0.1% - June 2021", f"{BOE}/x.pdf", "2021-06-24")
    p = plan_row(r)
    assert p.action == "kind-only" and p.updates == {"date_kind": "meeting"}
    assert (p.new_date, p.new_precision) == ("2021-06-24", None)


# ---- an explicit label says what the stored date asserts --------------

JP = "https://www.boj.or.jp/en/mopo/mpmsche_minu/minu_2016"


def test_a_first_of_month_labelled_day_is_not_refined():
    r = row("jp", "A3", "x", f"{JP}/g161119.pdf", "2016-11-01",
            date_precision="day", date_source="bank_site")
    p = plan_row(r)
    assert p.action == "mismatch" and p.updates == {}
    p = plan_row(r, refine_only=False)
    assert (p.action, p.updates["date"]) == ("to-day", "2016-11-19")


def test_a_first_of_january_labelled_day_is_not_refined_either():
    r = gb_minutes("Minutes of the MPC Meeting held on 3 and 4 February 1999",
                   date_precision="day", date_source="bank_site")
    assert plan_row(r).action == "mismatch"


def test_an_unlabelled_or_month_labelled_first_of_month_is_refined():
    for extra in ({}, {"date_precision": "month", "date_source": "bank_site"}):
        r = row("jp", "A3", "x", f"{JP}/g161119.pdf", "2016-11-01", **extra)
        p = plan_row(r)
        assert (p.action, p.updates["date"], p.updates["date_precision"]) == (
            "to-day", "2016-11-19", "day")


def test_a_label_bounds_the_period_a_placeholder_stands_for():
    # 1 January labelled `month` asserts January: June is a move, not a refinement
    r = gb_minutes("Bank Rate maintained at 0.1% - June 2021", stored="2021-01-01",
                   slug="june-2021", date_precision="month", date_source="bank_site")
    assert plan_row(r).action == "mismatch"
    # labelled `year` (as the Bank of England recovery writes it): anywhere in the year
    r = gb_minutes("Bank Rate maintained at 0.1% - June 2021", stored="2021-01-01",
                   slug="june-2021", date_precision="year", date_source="bank_site")
    p = plan_row(r)
    assert (p.action, p.updates["date"], p.updates["date_precision"]) == (
        "to-month", "2021-06-01", "month")


def test_a_day_label_on_a_month_only_date_is_still_corrected():
    r = row("au", "E1", "x", "https://www.rba.gov.au/publications/smp/2026/aug/", "2026-08-01",
            date_precision="day", date_source="bank_site")
    assert plan_row(r).updates == {"date_precision": "month", "date_kind": "period"}


# ---- date_kind is true row by row --------------------------------------

def test_rba_decision_before_2008_is_a_release_day():
    r = row("au", "A1", "RBA monetary policy decision 2007-12-05",
            "https://www.rba.gov.au/media-releases/2007/mr-07-21.html", "2007-12-05")
    p = plan_row(r)
    assert (p.action, p.kind, p.updates["date_kind"]) == (
        "day-confirmed", "publication", "publication")
    r = row("au", "A1", "RBA monetary policy decision 2008-02-05",
            "https://www.rba.gov.au/media-releases/2008/mr-08-02.html", "2008-02-05")
    assert plan_row(r).kind == "meeting"


def test_boc_report_day_is_a_release_day_and_a_month_a_period():
    r = row("ca", "E1", "", f"{BOC}/publications/mpr/mpr-2026-10-28/tariff-assumptions/",
            "2026-10-28", date_precision="day", date_source="bank_site")
    p = plan_row(r)
    assert p.action == "kind-only" and p.updates == {"date_kind": "publication"}
    r = row("ca", "E1", "", f"{BOC}/publications/mpr/mpr-2024-07/in-focus-1/", None,
            date_precision="day", date_source="bank_site")
    assert plan_row(r).updates["date_kind"] == "period"


BOE_FILES = f"{BOE}/-/media/boe/files"


def test_a_row_filed_as_minutes_that_is_not_minutes_is_dated_but_gets_no_kind():
    r = row("gb", "A3", "Bank Rate maintained at 0.75% - August 2019",
            f"{BOE_FILES}/inflation-report/2019/august/inflation-report-august-2019.pdf",
            "2019-01-01", source_url=f"{BOE}/monetary-policy-summary-and-minutes/2019/august-2019")
    p = plan_row(r)
    assert p.action == "misfiled" and p.kind == ""
    assert p.updates == {"date": "2019-08-01", "date_precision": "month",
                         "date_source": "bank_site"}
    assert "misfiled" in p.note
    r.update(p.updates)
    again = plan_row(r)
    assert again.action == "misfiled" and again.updates == {}    # reported on every run


def test_a_kind_the_rules_no_longer_give_is_removed_not_nulled():
    r = row("gb", "A3", "Bank Rate maintained at 0.75% - August 2019",
            f"{BOE_FILES}/inflation-report/2019/august/inflation-report-august-2019.pdf",
            "2019-08-01", date_precision="month", date_source="bank_site", date_kind="meeting")
    p = plan_row(r)
    assert p.action == "misfiled" and p.updates == {} and p.removals == ("date_kind",)
    p.apply(r)
    assert "date_kind" not in r
    assert plan_row(r).removals == ()
    r = row("au", "A1", "RBA monetary policy decision 2007-12-05",
            "https://www.rba.gov.au/media-releases/2007/mr-07-21.html", "2007-12-05",
            date_precision="day", date_source="bank_site", date_kind="meeting")
    assert plan_row(r).updates == {"date_kind": "publication"}


def test_fed_individual_projections_keep_the_meeting_kind_with_a_note():
    r = row("us", "F1", "Summary of Economic Projections 2015-06-17",
            f"{FED}/monetarypolicy/files/FOMC20150617SEPcompilation.pdf", "2015-06-17")
    p = plan_row(r)
    assert p.kind == "meeting" and "released about five years" in p.note
    r = row("us", "F1", "x", f"{FED}/monetarypolicy/files/fomcprojtabl20210922.pdf", "2021-09-22")
    assert plan_row(r).note == ""


def test_a_page_captured_before_publication_is_noted_as_a_stub():
    r = row("gb", "E1", "Monetary Policy Report - July 2026 (to be published at 12pm)",
            f"{BOE}/monetary-policy-report/2026/july-2026", "2026-01-01",
            source_url=f"{BOE}/monetary-policy-report/2026/july-2026")
    p = plan_row(r)
    assert p.action == "to-month" and "stub" in p.note
    assert plan_row(gb_minutes("Bank Rate maintained at 0.1% - June 2021", stored="2021-01-01",
                               slug="june-2021")).note == ""


def test_a_dated_row_whose_precision_was_not_stated_gets_its_label():
    """Key present and null = a producer that stated nothing: unlike a row
    written before labels existed, it is labelled even for a confirmed day."""
    r = row("us", "A2", "FOMC statement 2018-08-01",
            f"{FED}/newsevents/pressreleases/monetary20180801a.htm", "2018-08-01",
            date_precision=None, date_source="bank_site")
    p = plan_row(r)
    assert p.action == "day-confirmed"
    assert p.updates == {"date_precision": "day", "date_kind": "meeting"}


def test_rows_without_evidence_or_rule_are_untouched():
    r = row("gb", "A3", "Bank Rate maintained at 0.5%", f"{BOE}/a.pdf", "2018-01-01",
            source_url=f"{BOE}/b")
    assert (plan_row(r).action, plan_row(r).updates) == ("no-evidence", {})
    r = row("gb", "A1", "Decision of 3 May 2018", f"{BOE}/a.pdf", "2018-01-01")
    assert (plan_row(r).action, plan_row(r).updates) == ("no-rule", {})


def test_planning_is_idempotent():
    r = gb_minutes("Minutes of the MPC Meeting held on 3 and 4 February 1999")
    r.update(plan_row(r).updates)
    p = plan_row(r)
    assert p.action == "unchanged" and p.updates == {}


def test_plan_only_ever_writes_date_keys():
    for r in (gb_minutes("Minutes of the MPC Meeting held on 3 and 4 February 1999"),
              row("ca", "E1", "", f"{BOC}/publications/mpr/", None, date_precision="day")):
        assert set(plan_row(r).updates) <= set(DATE_KEYS)
    assert not {"doc_id", "sha256", "local_path", "document_url", "title"} & set(DATE_KEYS)


# ---- the command on a manifest ----------------------------------------

def _corpus(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    rows = {
        "gb": [
            gb_minutes("Minutes of the MPC Meeting held on 3 and 4 February 1999"),
            gb_minutes("Bank Rate maintained at 0.1% - June 2021", stored="2021-01-01",
                       slug="june-2021"),
            gb_minutes("Minutes of the MPC Meeting held on 5 and 6 June 2013",
                       stored="2013-01-01", slug="monetary-policy-committee-july-2013"),
            # outside the scope: a working paper whose title carries a date
            row("gb", "D1", "Staff Working Paper, 3 May 2020", f"{BOE}/wp/2020/x.pdf",
                "2020-01-01", date_precision="year", date_source="bank_site"),
        ],
        "us": [row("us", "A2", "FOMC statement 2018-08-01",
                   f"{FED}/newsevents/pressreleases/monetary20180801a.htm", "2018-08-01")],
        "ca": [row("ca", "E1", "", f"{BOC}/publications/mpr/", None, date_precision="day",
                   date_source="bank_site"),
               row("ca", "E1", "", f"{BOC}/publications/mpr/mpr-2024-07/in-focus-1/", None,
                   date_precision="day", date_source="bank_site")],
        # a bank outside the policy core, with a policy doc type and a dated title
        "ch": [row("ch", "A3", "Minutes of 3 and 4 February 1999", "https://snb/x.pdf",
                   "1999-01-01")],
    }
    cfg.manifest_dir.mkdir(parents=True)
    for bank, brows in rows.items():
        cfg.manifest_file(bank).write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in brows))
    return cfg


def _bytes(cfg):
    return {f.name: f.read_bytes() for f in sorted(cfg.manifest_dir.glob("*.jsonl"))}


def test_dry_run_writes_a_csv_and_no_manifest(tmp_path):
    cfg = _corpus(tmp_path)
    before = _bytes(cfg)
    out = tmp_path / "plan.csv"
    counts = run_policy_dates(config=cfg, csv_path=str(out))
    assert _bytes(cfg) == before
    assert counts == {"to-day": 1, "to-month": 2, "day-confirmed": 1, "conflict": 1,
                      "not-a-report": 1, "rows": 6, "written": 0}
    lines = list(csv.DictReader(out.open()))
    assert list(lines[0]) == ["doc_id", "bank", "doc_type", "old_date", "old_precision",
                              "new_date", "new_precision", "kind", "action", "rule",
                              "evidence", "fields_changed", "note", "title", "document_url"]
    assert len(lines) == 6                       # every row in scope, nothing else
    assert {ln["bank"] for ln in lines} == {"gb", "us", "ca"}
    assert {ln["doc_type"] for ln in lines} <= set(POLICY_DOC_TYPES)
    by_action = {ln["action"]: ln for ln in lines}
    d = by_action["to-day"]
    assert (d["old_date"], d["old_precision"], d["new_date"], d["new_precision"], d["kind"],
            d["rule"], d["evidence"], d["fields_changed"]) == (
        "1999-01-01", "", "1999-02-04", "day", "meeting", "title", "3 and 4 February 1999",
        "date date_precision date_source date_kind")
    assert by_action["conflict"]["fields_changed"] == ""
    assert by_action["not-a-report"]["new_date"] == ""
    c = by_action["day-confirmed"]
    assert (c["old_precision"], c["new_precision"], c["fields_changed"]) == (
        "", "day", "date_precision date_source date_kind")


def test_default_csv_lands_in_the_reports_directory(tmp_path):
    cfg = _corpus(tmp_path)
    run_policy_dates(config=cfg)
    assert (cfg.reports_dir / "policy_dates.csv").exists()


def test_write_changes_only_date_keys_of_rows_in_scope_and_is_idempotent(tmp_path):
    cfg = _corpus(tmp_path)
    before_bytes = _bytes(cfg)
    before = {r["doc_id"]: r for r in iter_manifest_rows(cfg)}

    counts = run_policy_dates(config=cfg, write=True, csv_path=str(tmp_path / "a.csv"))
    assert counts["written"] == 5                # 1 day, 2 month, 1 confirmed, 1 label
    after_bytes = _bytes(cfg)
    assert after_bytes["ch.jsonl"] == before_bytes["ch.jsonl"]       # bank out of scope
    after = {r["doc_id"]: r for r in iter_manifest_rows(cfg)}
    assert list(after) == list(before)                               # same rows, same order
    touched = 0
    for did, old in before.items():
        new = after[did]
        diff = {k for k in set(old) | set(new) if old.get(k, "∅") != new.get(k, "∅")}
        assert diff <= set(DATE_KEYS), (did, diff)
        assert [k for k in new if k not in DATE_KEYS] == [k for k in old if k not in DATE_KEYS]
        for k in ("doc_id", "sha256", "local_path", "document_url", "title", "source_url"):
            assert new[k] == old[k]
        touched += bool(diff)
    assert touched == 5
    wp = next(r for r in after.values() if r["doc_type"] == "D1")
    assert wp == next(r for r in before.values() if r["doc_type"] == "D1")
    # untouched lines are byte-identical (no re-serialisation)
    gb_before = before_bytes["gb.jsonl"].splitlines()
    gb_after = after_bytes["gb.jsonl"].splitlines()
    assert gb_after[2:] == gb_before[2:]         # the conflict row and the working paper

    again = run_policy_dates(config=cfg, write=True, csv_path=str(tmp_path / "b.csv"))
    assert again["written"] == 0
    assert again == {"unchanged": 4, "conflict": 1, "not-a-report": 1, "rows": 6, "written": 0}
    assert _bytes(cfg) == after_bytes


def test_written_rows_have_the_expected_dates(tmp_path):
    cfg = _corpus(tmp_path)
    run_policy_dates(config=cfg, write=True, csv_path=str(tmp_path / "a.csv"))
    rows = list(iter_manifest_rows(cfg))
    gb = [r for r in rows if r["source_code"] == "gb"]
    assert (gb[0]["date"], gb[0]["year"], gb[0]["date_precision"], gb[0]["date_kind"]) == (
        "1999-02-04", 1999, "day", "meeting")
    assert (gb[1]["date"], gb[1]["year"], gb[1]["date_precision"], gb[1]["date_kind"]) == (
        "2021-06-01", 2021, "month", "meeting")
    assert (gb[2]["date"], gb[2].get("date_precision"), gb[2].get("date_kind")) == (
        "2013-01-01", None, None)                 # the conflict: nothing written
    [us] = [r for r in rows if r["source_code"] == "us"]
    assert (us["date"], us["date_precision"], us["date_source"], us["date_kind"]) == (
        "2018-08-01", "day", "bank_site", "meeting")
    ca = [r for r in rows if r["source_code"] == "ca"]
    assert (ca[0]["date"], ca[0]["date_precision"], ca[0].get("date_kind")) == (None, None, None)
    assert (ca[1]["date"], ca[1]["year"], ca[1]["date_precision"], ca[1]["date_kind"]) == (
        "2024-07-01", 2024, "month", "period")


def test_scope_filters_narrow_the_run(tmp_path):
    cfg = _corpus(tmp_path)
    c = run_policy_dates(bank_codes=["gb"], doc_types=["A3"], config=cfg,
                         csv_path=str(tmp_path / "a.csv"))
    assert c["rows"] == 3
    c = run_policy_dates(bank_codes=["ca"], config=cfg, csv_path=str(tmp_path / "b.csv"))
    assert c["rows"] == 2


def test_widening_the_scope_cannot_touch_a_series_without_a_rule(tmp_path):
    cfg = _corpus(tmp_path)
    before = _bytes(cfg)
    c = run_policy_dates(bank_codes=["ch", "gb"], doc_types=["A3", "D1"], config=cfg,
                         write=True, csv_path=str(tmp_path / "a.csv"))
    assert c["no-rule"] == 2                     # ch A3 and gb D1: listed, never written
    after = _bytes(cfg)
    assert after["ch.jsonl"] == before["ch.jsonl"]
    assert after["gb.jsonl"].splitlines()[3] == before["gb.jsonl"].splitlines()[3]


def test_a_row_changed_by_another_writer_meanwhile_keeps_that_change(tmp_path, monkeypatch):
    """The write re-reads each row under the manifest lock: a URL stamped on
    the row by the nightly sync after the plan was built is not reverted."""
    cfg = _corpus(tmp_path)
    real = policy_dates.merge_rows_locked

    def sync_then_merge(cfg_, merges):
        f = cfg.manifest_file("gb")
        lines = f.read_text().splitlines()
        first = json.loads(lines[0])
        first["alt_urls"] = ["https://mirror/x.pdf"]
        lines[0] = json.dumps(first, ensure_ascii=False)
        f.write_text("\n".join(lines) + "\n")
        return real(cfg_, merges)
    monkeypatch.setattr(policy_dates, "merge_rows_locked", sync_then_merge)
    run_policy_dates(bank_codes=["gb"], config=cfg, write=True, csv_path=str(tmp_path / "a.csv"))
    first = next(r for r in iter_manifest_rows(cfg) if r["source_code"] == "gb")
    assert first["alt_urls"] == ["https://mirror/x.pdf"] and first["date"] == "1999-02-04"


def test_cli_policy_dates_dry_run_then_write(tmp_path, monkeypatch, capsys):
    cfg = _corpus(tmp_path)
    monkeypatch.chdir(tmp_path)                  # Config() reads ./data
    before = _bytes(cfg)
    assert main(["policy-dates", "--csv", str(tmp_path / "dry.csv")]) == 0
    assert _bytes(cfg) == before
    out = capsys.readouterr().out
    assert "to-day" in out and "gb" in out and "A3" in out
    # moving a date out of its period is a deliberate, scoped action
    with pytest.raises(SystemExit):
        main(["policy-dates", "--allow-moves", "--csv", str(tmp_path / "x.csv")])
    assert "--allow-moves" in capsys.readouterr().err
    assert _bytes(cfg) == before
    assert main(["policy-dates", "--allow-moves", "--types", "A3",
                 "--csv", str(tmp_path / "x.csv")]) == 0
    assert main(["policy-dates", "--banks", "gb", "--types", "A3", "--write",
                 "--csv", str(tmp_path / "w.csv")]) == 0
    assert _bytes(cfg)["gb.jsonl"] != before["gb.jsonl"]
    assert _bytes(cfg)["us.jsonl"] == before["us.jsonl"]


def test_moving_dates_needs_an_explicit_scope(tmp_path):
    cfg = _corpus(tmp_path)
    before = _bytes(cfg)
    with pytest.raises(ValueError, match="explicit scope"):
        run_policy_dates(config=cfg, write=True, refine_only=False,
                         csv_path=str(tmp_path / "a.csv"))
    assert _bytes(cfg) == before
    run_policy_dates(bank_codes=["gb"], config=cfg, refine_only=False,
                     csv_path=str(tmp_path / "b.csv"))
