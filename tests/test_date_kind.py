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
from cb_corpus.dates import (DATE_KINDS, KIND_MEETING, KIND_PERIOD, KIND_PUBLICATION,
                             POLICY_DATE_KIND, date_kind_for)
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
    assert any(e["bank_code"] == "gb" for e in compute_series(cfg, today=date(2024, 9, 1)))

    # a keyed rewrite of ONE row keeps the others byte-identical, new keys included
    changed = dict(rows[2], title="Minutes three")
    assert apply_row_updates(cfg, {changed["doc_id"]: changed}) == 1
    assert st.rewrite_manifest({}) == 0
    after = f.read_text().splitlines()
    assert [a for i, a in enumerate(after) if i != 2] == [b for i, b in enumerate(before) if i != 2]
    kept = json.loads(after[2])
    assert kept["date_kind"] == "meeting" and kept["some_future_key"] == [1, 2]
