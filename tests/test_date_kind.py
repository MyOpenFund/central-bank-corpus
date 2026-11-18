"""`date_kind`: what the date of a policy-core row IS (meeting, publication,
period). One table per bank x doc type, owned by the producer; an optional
manifest key every reader of the manifest must tolerate."""
from __future__ import annotations

import json
from datetime import date

import pytest

import gen_corpus
from cb_corpus.cadence import compute_series
from cb_corpus.completeness import actual_counts
from cb_corpus.config import Config
from cb_corpus.dates import (BOE_NOT_MINUTES, DATE_KINDS, FED_SEP_COMPILATION_URL,
                             KIND_MEETING, KIND_PERIOD, KIND_PUBLICATION, KINDLESS_DATE_SOURCES,
                             POLICY_DATE_KIND, POLICY_DATE_KIND_EXCEPTIONS,
                             RBA_SAME_DAY_ANNOUNCEMENT_FROM, date_kind_for, is_misfiled,
                             policy_date_kind)
from cb_corpus.models import DocRecord
from cb_corpus.storage import Storage, apply_row_updates, iter_manifest_rows
from cb_corpus.taxonomy import DocType


def test_every_kind_in_the_table_is_a_known_value():
    assert DATE_KINDS == ("meeting", "publication", "period")
    assert set(POLICY_DATE_KIND.values()) <= set(DATE_KINDS)


@pytest.mark.parametrize("bank,code,kind", [
    ("us", "A2", KIND_MEETING), ("us", "A3", KIND_MEETING), ("us", "B1", KIND_MEETING),
    ("us", "F1", KIND_MEETING), ("us", "E1", KIND_PUBLICATION),
    ("ecb", "A1", KIND_MEETING), ("ecb", "A2", KIND_MEETING), ("ecb", "B1", KIND_MEETING),
    ("ecb", "A3", KIND_PUBLICATION), ("ecb", "E1", KIND_PERIOD),
    ("gb", "A3", KIND_MEETING), ("gb", "E1", KIND_PERIOD),
    ("au", "A1", KIND_MEETING), ("au", "A3", KIND_MEETING), ("au", "E1", KIND_PERIOD),
    ("ca", "A3", KIND_PUBLICATION), ("ca", "E1", KIND_PERIOD),
    ("jp", "A3", KIND_MEETING),
])
def test_the_table_says_what_each_policy_date_is(bank, code, kind):
    assert date_kind_for(bank, code) == kind
    assert date_kind_for(bank, DocType[code]) == kind      # a DocType works too


def test_no_kind_outside_the_table():
    assert date_kind_for("us", "D1") is None       # working papers: not policy core
    assert date_kind_for("ch", "A2") is None       # a bank outside the policy core
    assert date_kind_for("gb", "A1") is None       # no such series today: nothing claimed


# ---- the kind of ONE row: the series' kind, minus documented exceptions --

def test_exceptions_are_documented_rules_on_known_series():
    assert set(POLICY_DATE_KIND_EXCEPTIONS) <= set(POLICY_DATE_KIND)
    for excs in POLICY_DATE_KIND_EXCEPTIONS.values():
        for e in excs:
            assert e.kind in DATE_KINDS and e.why
            # a rule on the date or the precision, or a dated list of meetings: never empty
            assert e.before is not None or e.precision is not None or e.on


def test_no_date_no_kind():
    assert policy_date_kind("us", "A3", date=None) is None
    assert policy_date_kind("us", "D1", date=date(2020, 1, 1)) is None


def test_rba_decisions_before_same_day_announcements_are_release_days():
    """Until December 2007 the decision was announced at 9.30 am the morning
    AFTER the board meeting; from the February 2008 meeting, at 2.30 pm on the
    meeting day. The stored date is the release day in both periods."""
    assert RBA_SAME_DAY_ANNOUNCEMENT_FROM == date(2008, 2, 5)
    k = lambda d: policy_date_kind("au", "A1", date=d, precision="day")   # noqa: E731
    assert k(date(2007, 12, 5)) == KIND_PUBLICATION       # Wednesday, meeting on the 4th
    assert k(date(1997, 7, 30)) == KIND_PUBLICATION
    assert k(date(2008, 2, 5)) == KIND_MEETING            # Tuesday, the meeting day
    # The unscheduled meeting of 18 March 2020 was announced on the 19th: a dated exception.
    assert k(date(2020, 3, 19)) == KIND_PUBLICATION
    assert k(date(2020, 3, 3)) == KIND_MEETING and k(date(2020, 4, 7)) == KIND_MEETING
    assert k(date(2026, 11, 3)) == KIND_MEETING
    # the minutes are dated at the meeting in every period
    assert policy_date_kind("au", "A3", date=date(2006, 12, 5), precision="day") == KIND_MEETING


def test_boc_report_dated_to_the_day_is_a_release_day():
    url = "https://www.bankofcanada.ca/publications/mpr/mpr-2026-10-28/in-focus-6/"
    assert policy_date_kind("ca", "E1", date=date(2026, 10, 28), precision="day",
                            pdf_url=url) == KIND_PUBLICATION
    url = "https://www.bankofcanada.ca/publications/mpr/mpr-2016-04/"
    assert policy_date_kind("ca", "E1", date=date(2016, 4, 1), precision="month",
                            pdf_url=url) == KIND_PERIOD
    # a first of the month that is a month stays a period
    assert policy_date_kind("ca", "E1", date=date(2025, 1, 1), precision="month",
                            pdf_url=url) == KIND_PERIOD


def test_boc_index_page_has_no_kind_even_when_dated():
    for url in ("https://www.bankofcanada.ca/publications/mpr/",
                "https://www.bankofcanada.ca/publications/mpr/qualitative-research-on-the-mpr/"):
        assert policy_date_kind("ca", "E1", date=date(2024, 8, 19), precision="day",
                                pdf_url=url) is None


def test_a_sitemap_last_modified_day_is_no_kind_of_date():
    assert KINDLESS_DATE_SOURCES == ("sitemap_lastmod",)
    url = "https://www.bankofcanada.ca/publications/mpr/mpr-2024-07/"
    assert policy_date_kind("ca", "E1", date=date(2024, 8, 19), precision="day",
                            date_source="sitemap_lastmod", pdf_url=url) is None
    assert policy_date_kind("ca", "E1", date=date(2024, 7, 1), precision="month",
                            date_source="bank_site", pdf_url=url) == KIND_PERIOD


BOE_FILES = "https://www.bankofengland.co.uk/-/media/boe/files"


@pytest.mark.parametrize("title,pdf_url", [
    ("Bank Rate maintained at 0.75% - August 2019",
     f"{BOE_FILES}/inflation-report/2019/august/inflation-report-august-2019.pdf"),
    ("Bank Rate maintained at 0.75% - January 2020",
     f"{BOE_FILES}/monetary-policy-report/2020/january/monetary-policy-report-january-2020.pdf"),
    ("transcript-june-2020-mpc-decision-press-briefing.pdf",
     f"{BOE_FILES}/monetary-policy-summary-and-minutes/2020/"
     "transcript-june-2020-mpc-decision-press-briefing.pdf"),
    ("MPC press conference - May 2021", f"{BOE_FILES}/x/2021/may-2021.pdf"),
])
def test_boe_rows_filed_as_minutes_that_are_not_minutes_get_no_kind(title, pdf_url):
    assert is_misfiled("gb", "A3", title=title, pdf_url=pdf_url)
    assert policy_date_kind("gb", "A3", date=date(2019, 8, 1), precision="month",
                            title=title, pdf_url=pdf_url) is None


@pytest.mark.parametrize("title,pdf_url", [
    ("Minutes of the MPC Meeting held on 3 and 4 February 1999",
     f"{BOE_FILES}/minutes/1999/minutes-february-1999.pdf"),
    ("Bank Rate reduced to 3.75% - December 2025 Monetary Policy Summary and Minutes",
     f"{BOE_FILES}/monetary-policy-summary-and-minutes/2025/"
     "monetary-policy-summary-and-minutes-december-2025.pdf"),
    ("Monetary policy summary and minutes - September 2017",
     f"{BOE_FILES}/monetary-policy-summary-and-minutes/2017/september-2017.pdf"),
])
def test_real_boe_minutes_are_not_misfiled(title, pdf_url):
    assert not is_misfiled("gb", "A3", title=title, pdf_url=pdf_url)
    assert policy_date_kind("gb", "A3", date=date(2017, 9, 1), precision="month",
                            title=title, pdf_url=pdf_url) == KIND_MEETING


def test_misfiled_is_a_rule_of_the_minutes_series_only():
    import re
    assert re.search(BOE_NOT_MINUTES, "monetary-policy-report-november-2025.pdf")
    # the Monetary Policy Report series is of course made of such files
    assert not is_misfiled("gb", "E1", title="Monetary Policy Report - November 2025",
                           pdf_url=f"{BOE_FILES}/monetary-policy-report/2025/november/"
                           "monetary-policy-report-november-2025.pdf")
    assert not is_misfiled("us", "A3", title="press conference", pdf_url="x")


def test_fed_individual_projections_are_recognisable_and_stay_meeting_dated():
    """SEPcompilation files are released about five years after the meeting
    they are dated at: the kind stays `meeting` (what the date holds); the
    pattern is exposed so a consumer, or a later change, can tell them apart."""
    import re
    comp = "https://www.federalreserve.gov/monetarypolicy/files/FOMC20150617SEPcompilation.pdf"
    table = "https://www.federalreserve.gov/monetarypolicy/files/fomcprojtabl20210922.pdf"
    assert re.search(FED_SEP_COMPILATION_URL, comp) and not re.search(FED_SEP_COMPILATION_URL, table)
    for url in (comp, table):
        assert policy_date_kind("us", "F1", date=date(2015, 6, 17), precision="day",
                                pdf_url=url) == KIND_MEETING


def test_new_rows_use_the_row_rules():
    old = DocRecord(bank_code="au", doc_type=DocType.A1, title="RBA monetary policy decision "
                    "2007-12-05", pdf_url="https://www.rba.gov.au/media-releases/2007/mr-07-21.html",
                    date=date(2007, 12, 5), date_precision="day")
    assert old.to_row()["date_kind"] == "publication"
    day = DocRecord(bank_code="ca", doc_type=DocType.E1, title="",
                    pdf_url="https://www.bankofcanada.ca/publications/mpr/mpr-2026-10-28/x/",
                    date=date(2026, 10, 28), date_precision="day")
    assert day.to_row()["date_kind"] == "publication"
    lastmod = DocRecord(bank_code="ca", doc_type=DocType.E1, title="",
                        pdf_url="https://www.bankofcanada.ca/publications/mpr/",
                        date=date(2024, 8, 19), date_precision="day",
                        date_source="sitemap_lastmod")
    assert "date_kind" not in lastmod.to_row()
    misfiled = DocRecord(bank_code="gb", doc_type=DocType.A3,
                         title="Bank Rate maintained at 0.75% - August 2019",
                         pdf_url=f"{BOE_FILES}/inflation-report/2019/august/"
                         "inflation-report-august-2019.pdf",
                         date=date(2019, 8, 1), date_precision="month")
    assert "date_kind" not in misfiled.to_row()


def _rec(bank="us", dt=DocType.A3, d=date(2024, 1, 31), **kw):
    return DocRecord(bank_code=bank, doc_type=dt, title="t", pdf_url="https://x/a.pdf",
                     date=d, date_precision="day" if d else None, **kw)


def test_a_new_policy_row_carries_its_kind_from_the_table():
    assert _rec().to_row()["date_kind"] == "meeting"
    assert _rec("ecb", DocType.A3).to_row()["date_kind"] == "publication"
    assert _rec("au", DocType.E1).to_row()["date_kind"] == "period"


def test_rows_outside_the_table_have_no_date_kind_key_at_all():
    assert "date_kind" not in _rec("us", DocType.D1).to_row()
    assert "date_kind" not in _rec("ch", DocType.A2).to_row()


def test_an_undated_row_has_no_kind():
    assert "date_kind" not in _rec(d=None).to_row()
    assert "date_kind" not in _rec(d=None, date_kind="meeting").to_row()


def test_a_producer_may_state_the_kind_itself():
    assert _rec("ch", DocType.A2, date_kind="publication").to_row()["date_kind"] == "publication"
    assert _rec(date_kind="publication").to_row()["date_kind"] == "publication"


def test_date_kind_is_not_part_of_the_identity():
    assert _rec().doc_id == _rec(date_kind="publication").doc_id


# ---- every reader of the manifest tolerates the new key ---------------

def _manifest(cfg, rows):
    f = cfg.manifest_file(rows[0]["bank_code"])
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return f


def _row(i, **extra):
    r = {"bank_code": "gb", "doc_type": "A3", "title": f"Minutes {i}",
         "pdf_url": f"https://boe/m{i}.pdf", "source_url": f"https://boe/p{i}",
         "date": f"2024-{i:02d}-01", "language": "en", "provenance": "bank_site",
         "mime_type": "application/pdf", "sha256": f"h{i}", "local_path": f"/raw/m{i}.pdf",
         "doc_id": f"id{i}", "year": 2024}
    r.update(extra)
    return r


def test_manifest_readers_and_writers_tolerate_date_kind(tmp_path):
    cfg = Config(data_dir=tmp_path)
    rows = [_row(i, date_kind="meeting", date_precision="month", some_future_key=[1, 2])
            for i in range(1, 9)]
    f = _manifest(cfg, rows)
    before = f.read_text().splitlines()

    assert [r["date_kind"] for r in iter_manifest_rows(cfg)] == ["meeting"] * 8
    st = Storage(cfg)                                   # loads the dedup indexes
    assert st.is_known_url("https://boe/m3.pdf")
    assert actual_counts(st)[("gb", "A3", 2024)] == 8   # completeness matrix
    assert "| Documents | **8** |" in gen_corpus.build_corpus_md(cfg)
    # cadence reads every row; these are month precision, so they are counted as
    # dropped placeholders (issue #23) rather than judged
    dropped: dict = {}
    assert compute_series(cfg, today=date(2024, 9, 1), dropped=dropped) == []
    assert dropped == {("gb", "A3"): 8}

    # a keyed rewrite of ONE row keeps the others byte-identical, new keys included
    changed = dict(rows[2], title="Minutes three")
    assert apply_row_updates(cfg, {changed["doc_id"]: changed}) == 1
    assert st.rewrite_manifest({}) == 0
    after = f.read_text().splitlines()
    assert [a for i, a in enumerate(after) if i != 2] == [b for i, b in enumerate(before) if i != 2]
    kept = json.loads(after[2])
    assert kept["date_kind"] == "meeting" and kept["some_future_key"] == [1, 2]
