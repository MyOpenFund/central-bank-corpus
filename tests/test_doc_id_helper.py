"""B3 + B6 (grit wave 2026-11): one doc_id formula; migration URL matchers
renamed so they cannot be mistaken for the index normalizer."""
from __future__ import annotations

import hashlib

import pytest

from cb_corpus import c2_migrate, models, pipeline, repec_check, storage, wp_migrate
from cb_corpus.models import DocRecord, compute_doc_id
from cb_corpus.taxonomy import DocType

SAMPLES = [
    ("de", DocType.C1, "https://www.bis.org/review/r200115a.pdf"),
    ("ecb", DocType.D1, "https://www.ecb.europa.eu//pub/pdf/scpwps/ecb.wp1.en.pdf"),
    ("fr", DocType.A3, "https://www.banque-france.fr/é accent?q=1"),
    ("us", DocType.D2, ""),
]


@pytest.mark.parametrize("bank,dt,url", SAMPLES)
def test_compute_doc_id_equals_docrecord_doc_id(bank, dt, url):
    rec = DocRecord(bank_code=bank, doc_type=dt, title="t", pdf_url=url)
    assert compute_doc_id(bank, dt, url) == rec.doc_id
    # The historical formula, pinned: changing it would orphan every file.
    assert rec.doc_id == hashlib.sha1(f"{bank}|{dt.code}|{url}".encode()).hexdigest()[:16]


def test_compute_doc_id_accepts_a_doc_type_code():
    assert compute_doc_id("de", "C1", SAMPLES[0][2]) == compute_doc_id("de", DocType.C1, SAMPLES[0][2])


def test_reindex_bis_uses_the_helper():
    import inspect
    src = inspect.getsource(pipeline.reindex_bis_from_disk)
    assert "compute_doc_id(" in src and "hashlib.sha1" not in src


def test_migration_matchers_renamed_and_index_normalizer_untouched():
    for mod in (wp_migrate, c2_migrate):
        assert not hasattr(mod, "normalize_url")
        assert "storage.normalize_url" in mod.match_key_url.__doc__
    assert not hasattr(repec_check, "normalize_url")
    # Different questions, different answers: fuzzy match key vs index identity.
    u = "https://www.ecb.europa.eu//pub/ecb.wp1~abc123.en.pdf"
    assert wp_migrate.match_key_url(u) == "www.ecb.europa.eu/pub/ecb.wp1.en.pdf"
    assert c2_migrate.match_key_url(u) == "www.ecb.europa.eu/pub/ecb.wp1~abc123.en.pdf"
    assert storage.normalize_url(u) == "https://www.ecb.europa.eu/pub/ecb.wp1~abc123.en.pdf"
    assert "match_key_url" in storage.normalize_url.__doc__
