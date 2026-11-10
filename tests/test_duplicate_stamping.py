"""B1: a `skip:duplicate-content` URL is remembered on the row that owns the
content, so the next run recognises it before any fetch instead of
re-downloading it every night (grit wave 2026-11)."""
from __future__ import annotations

from datetime import date

import pytest

from cb_corpus.config import Config
from cb_corpus.models import DocRecord
from cb_corpus.storage import Storage, iter_manifest_rows
from cb_corpus.taxonomy import DocType

BODY = (b"%PDF-1.4 shared body", "application/pdf")


def _rec(name: str, bank: str = "fr", source: str = "") -> DocRecord:
    return DocRecord(bank_code=bank, doc_type=DocType.D1, title=name,
                     pdf_url=f"https://www.example.org/{bank}/{name}.pdf",
                     source_url=source, date=date(2020, 1, 1))


def _storage(tmp_path, body=BODY) -> Storage:
    st = Storage(Config(data_dir=tmp_path))
    st.fetcher.get_bytes = lambda url: body
    return st


def _row(cfg: Config, doc_id: str) -> dict:
    return next(r for r in iter_manifest_rows(cfg) if r["doc_id"] == doc_id)


def test_duplicate_url_stamped_once_on_owner_with_source_url(tmp_path, capsys):
    st = _storage(tmp_path)
    owner = _rec("owner", source="https://ideas.example/owner.html")
    dup = _rec("dup", source="https://ideas.example/dup.html")
    counts = st.save_many([owner, dup], label="fr")
    assert counts == {"saved": 1, "skip": 1}
    row = _row(st.cfg, owner.doc_id)
    assert row["alt_urls"] == [dup.pdf_url]
    assert row["alt_source_urls"] == ["https://ideas.example/dup.html"]
    assert len(list(iter_manifest_rows(st.cfg))) == 1   # no row for the dup
    err = capsys.readouterr().err
    assert "[fr] duplicate-content: 1 (stamped 2 urls on 1 rows)" in err


def test_duplicate_known_in_memory_immediately(tmp_path):
    st = _storage(tmp_path)
    owner = _rec("owner")
    dup = _rec("dup", source="https://ideas.example/dup.html")
    assert st.save(owner) == "saved"
    assert st.save(dup) == f"skip:duplicate-content:{owner.doc_id}"
    # Indexed at once, before any flush.
    assert st.is_known_url(dup.pdf_url)
    assert st.is_known_source_url("https://ideas.example/dup.html")


def test_rerun_does_not_stamp_twice(tmp_path, capsys):
    st = _storage(tmp_path)
    owner, dup = _rec("owner", source="https://s/o"), _rec("dup", source="https://s/d")
    st.save_many([owner, dup], label="fr")
    capsys.readouterr()
    st2 = _storage(tmp_path)
    st2.save_many([_rec("dup", source="https://s/d")], label="fr")
    row = _row(st2.cfg, owner.doc_id)
    assert row["alt_urls"] == [dup.pdf_url]
    assert row["alt_source_urls"] == ["https://s/d"]
    assert "[fr] duplicate-content: 1 (stamped 0 urls on 0 rows)" in capsys.readouterr().err


def test_stamp_survives_reload_known_before_any_fetch(tmp_path):
    st = _storage(tmp_path)
    owner, dup = _rec("owner"), _rec("dup", source="https://ideas.example/dup.html")
    st.save_many([owner, dup])

    st2 = Storage(Config(data_dir=tmp_path))

    def boom(url):  # any fetch would be a regression
        raise AssertionError(f"fetched {url}")
    st2.fetcher.get_bytes = boom
    assert st2.is_known_url(dup.pdf_url)
    # RePEc's pre-fetch skip (is_known_source_url) learns the source too.
    assert st2.is_known_source_url("https://ideas.example/dup.html")


def test_dry_run_writes_nothing(tmp_path):
    st = _storage(tmp_path)
    owner = _rec("owner")
    assert st.save(owner) == "saved"
    before = st.cfg.manifest_file("fr").read_bytes()
    counts = st.save_many([_rec("dup", source="https://s/d")], dry_run=True)
    assert counts == {"dry-run": 1}
    assert st.cfg.manifest_file("fr").read_bytes() == before
    assert st._pending_dup_stamps == {} and st._pending_dup_sources == {}


def test_flush_survives_exception_mid_iteration(tmp_path):
    st = _storage(tmp_path)
    owner, dup = _rec("owner"), _rec("dup")

    def recs():
        yield owner
        yield dup
        raise RuntimeError("discovery crashed")

    with pytest.raises(RuntimeError, match="discovery crashed"):
        st.save_many(recs(), label="fr")
    assert _row(st.cfg, owner.doc_id)["alt_urls"] == [dup.pdf_url]
    assert st._pending_dup_stamps == {}


def test_owner_in_another_banks_manifest(tmp_path):
    st = _storage(tmp_path)
    owner = _rec("owner", bank="ecb")
    dup = _rec("dup", bank="fr", source="https://s/fr-dup")
    st.save_many([owner])
    st.save_many([dup], label="fr")
    row = _row(st.cfg, owner.doc_id)
    assert row["bank_code"] == "ecb"
    assert row["alt_urls"] == [dup.pdf_url]
    assert row["alt_source_urls"] == ["https://s/fr-dup"]
    assert not st.cfg.manifest_file("fr").exists() or \
        st.cfg.manifest_file("fr").read_text() == ""


def test_reindex_duplicate_queued_and_flushed(tmp_path):
    st = _storage(tmp_path)
    owner = _rec("owner")
    st.save(owner)
    f = tmp_path / "copy.pdf"
    f.write_bytes(BODY[0])
    dup = _rec("dup")
    assert st.reindex(dup, f) == f"skip:duplicate-content:{owner.doc_id}"
    assert st.flush_duplicate_stamps() == (1, 1, 1)
    assert _row(st.cfg, owner.doc_id)["alt_urls"] == [dup.pdf_url]


def test_source_url_equal_to_owner_source_not_stamped(tmp_path):
    st = _storage(tmp_path)
    owner = _rec("owner", source="https://s/same")
    dup = _rec("dup", source="https://s/same")
    st.save_many([owner, dup])
    row = _row(st.cfg, owner.doc_id)
    assert row["alt_urls"] == [dup.pdf_url]
    assert "alt_source_urls" not in row


def test_stamp_alt_urls_source_stamps_add_only(tmp_path):
    st = _storage(tmp_path)
    owner = _rec("owner", source="https://s/o")
    st.save(owner)
    assert st.stamp_alt_urls({}, source_stamps={owner.doc_id: ["https://s/x", "https://s/o"]}) == (1, 1)
    assert st.stamp_alt_urls({}, source_stamps={owner.doc_id: ["https://s/x"]}) == (0, 0)
    assert _row(st.cfg, owner.doc_id)["alt_source_urls"] == ["https://s/x"]
    assert st.is_known_source_url("https://s/x")
