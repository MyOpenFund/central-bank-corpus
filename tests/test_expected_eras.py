"""`expected_count(doc_type, year)` honours the year (issue #23).

A flat per-type constant judged every year by today's cadence: the ECB
Governing Council met about monthly before 2015 and six-weekly since, so a
pre-2015 year missing three monetary-policy decisions (9 of 12) read `ok`
against the post-2015 8. The tables now accept eras `[(from_year, count)]`,
measured on the committed manifests (spec, 2026-11-18). No network: adapters
get a dummy fetcher and the completeness matrix reads an in-memory manifest.
"""
from __future__ import annotations

import json

import pytest

from cb_corpus.adapters import declarative
from cb_corpus.adapters.base import (ADAPTERS, expected_for_year, get_adapter,
                                     normalize_expected)
from cb_corpus.banks import get_bank
from cb_corpus.completeness import build_matrix
from cb_corpus.config import Config
from cb_corpus.storage import Storage
from cb_corpus.taxonomy import DocType


def _ad(code):
    return get_adapter(code, fetcher=object())


# ---- the helpers ----------------------------------------------------------

def test_flat_int_applies_to_every_year():
    assert expected_for_year(8, 1950) == 8 and expected_for_year(8, 2030) == 8


def test_eras_pick_the_last_era_started_on_or_before_the_year():
    eras = normalize_expected([(1999, 20), (2002, 11), (2015, 8)])
    assert [expected_for_year(eras, y) for y in (1999, 2001, 2002, 2014, 2015, 2040)] == \
        [20, 20, 11, 11, 8, 8]


def test_a_year_before_the_first_era_and_a_none_era_have_no_expectation():
    eras = normalize_expected([(2008, 11), (2024, 8)])
    assert expected_for_year(eras, 2007) is None
    gap = normalize_expected([(1999, 12), (2010, None), (2015, 8)])
    assert expected_for_year(gap, 2012) is None and expected_for_year(gap, 2016) == 8


def test_none_and_missing_mean_unknown():
    assert expected_for_year(None, 2020) is None


def test_eras_given_out_of_order_are_sorted():
    assert normalize_expected([(2015, 8), (1999, 12)]) == ((1999, 12), (2015, 8))


@pytest.mark.parametrize("bad", [
    -1, True, 2.5, "8", [], [(1999,)], [(1999, 12, 3)], [("1999", 12)],
    [(1999, -1)], [(1999, 12), (1999, 8)], [(1999, True)], [[1999, "none", 1]],
])
def test_malformed_tables_raise_value_error(bad):
    with pytest.raises(ValueError):
        normalize_expected(bad)


def test_every_hand_written_table_is_well_formed():
    for cls in ADAPTERS.values():
        for dt, v in cls.expected_per_year.items():
            normalize_expected(v)       # raises on a malformed table


# ---- the measured tables (spec §3 B4, committed manifests 2026-11-18) -----

@pytest.mark.parametrize("dt,year,exp", [
    (DocType.A1, 1998, None), (DocType.A1, 1999, 20), (DocType.A1, 2001, 20),
    (DocType.A1, 2002, 11), (DocType.A1, 2014, 11), (DocType.A1, 2015, 8),
    (DocType.A2, 1999, 11), (DocType.A2, 2014, 11), (DocType.A2, 2015, 8),
    # accounts exist from 2015: earlier years are unknown, not missing
    (DocType.A3, 2003, None), (DocType.A3, 2014, None), (DocType.A3, 2015, 8),
    (DocType.A3, 2025, 8),
    (DocType.E4, 1999, 12), (DocType.E4, 2014, 12), (DocType.E4, 2015, 8),
])
def test_ecb_eras(dt, year, exp):
    assert _ad("ecb").expected_count(dt, year) == exp


@pytest.mark.parametrize("dt,year,exp", [
    (DocType.A1, 1997, None), (DocType.A1, 2007, None), (DocType.A1, 2008, 11),
    (DocType.A1, 2023, 11), (DocType.A1, 2024, 8),
    (DocType.A3, 2006, None), (DocType.A3, 2007, 11), (DocType.A3, 2023, 11),
    (DocType.A3, 2024, 8),
    (DocType.E1, 2006, 4), (DocType.E1, 2026, 4),
])
def test_rba_eras(dt, year, exp):
    assert _ad("au").expected_count(dt, year) == exp


def test_fed_unchanged():
    fed = _ad("us")
    assert [fed.expected_count(dt, 1999) for dt in (DocType.A2, DocType.A3, DocType.F1)] == [8, 8, 4]
    assert fed.expected_count(DocType.A3, 2024) == 8


# ---- TOML form --------------------------------------------------------------

@pytest.fixture
def registry(monkeypatch):
    reg: dict = {}
    monkeypatch.setattr(declarative, "INSTANCE_FACTORIES", reg)
    return reg


def _toml(tmp_path, text):
    p = tmp_path / "banks_sources.toml"
    p.write_text(text)
    return declarative.load_toml(p)


def test_toml_sitemap_and_listing_accept_ints_and_eras(tmp_path, registry):
    rep = _toml(tmp_path, (
        '[ch.sitemap]\nsitemap_url = "https://x/s.xml"\n'
        '[ch.sitemap.patterns]\nA2 = "/a2/"\nE2 = "/e2/"\n'
        '[ch.sitemap.expected_per_year]\nA2 = [[2000, 4], [2020, "none"], [2022, 6]]\nE2 = 1\n'
        '[ca.listing]\n[ca.listing.entries]\nA3 = [["https://b/l", "/a3/"]]\n'
        '[ca.listing.expected_per_year]\nA3 = [[2010, 8]]\n'))
    assert rep.loaded == ["ch", "ca"] and rep.warnings() == []
    ch = registry["ch"](get_bank("ch"), object())
    assert [ch.expected_count(DocType.A2, y) for y in (1999, 2000, 2020, 2021, 2023)] == \
        [None, 4, None, None, 6]
    assert ch.expected_count(DocType.E2, 1950) == 1
    ca = registry["ca"](get_bank("ca"), object())
    assert ca.expected_count(DocType.A3, 2009) is None and ca.expected_count(DocType.A3, 2010) == 8


@pytest.mark.parametrize("bad", [
    'A2 = [[2000]]', 'A2 = [[2000, -3]]', 'A2 = "eight"', 'A2 = [[2000, 4], [2000, 5]]',
    'A2 = [[2000, "None"]]',
])
def test_toml_malformed_eras_abort_the_file(tmp_path, registry, bad):
    rep = _toml(tmp_path, ('[ch.sitemap]\nsitemap_url = "https://x/s.xml"\n'
                           '[ch.sitemap.patterns]\nA2 = "/a2/"\n'
                           f'[ch.sitemap.expected_per_year]\n{bad}\n'))
    assert rep.error and registry == {}


# ---- completeness end to end ---------------------------------------------

def test_completeness_flags_a_pre_2015_ecb_year_short_of_its_era(tmp_path):
    """9 decisions in 2010 used to be `ok` against the flat 8; the 2002-2014
    era expects 11, so the cell is now `partial`. 8 in 2016 stays ok."""
    cfg = Config(data_dir=tmp_path)
    rows = [{"doc_id": f"a{y}{i}", "corpus": "central-bank", "source_code": "ecb", "doc_type": "A1", "year": y,
             "date": f"{y}-{i + 1:02d}-10", "title": "d",
             "document_url": f"https://ecb/{y}/{i}.pdf"}
            for y, n in ((2010, 9), (2016, 8)) for i in range(n)]
    p = cfg.manifest_file("ecb")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    st = Storage(cfg, fetcher=object())
    m = {(r["year"]): r for r in build_matrix([2010, 2016], bank_codes=["ecb"],
                                              scope=(DocType.A1,), storage=st)}
    assert m[2010]["expected"] == 11 and m[2010]["status"] == "partial"
    assert m[2016]["expected"] == 8 and m[2016]["status"] == "ok"
