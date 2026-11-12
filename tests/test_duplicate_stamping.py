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
    assert st._pending_dups == []


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
    assert st._pending_dups == []


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


# ---- fix wave (review I1, M1-M4) --------------------------------------

import json  # noqa: E402

from cb_corpus import storage as storage_mod  # noqa: E402

HTML_BODY = (b"<html>same page</html>", "text/html")


def _log(cfg: Config) -> list[dict]:
    p = cfg.reports_dir / "duplicate_stamps.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


def test_html_owner_not_persisted_kept_in_memory_and_logged(tmp_path):
    st = Storage(Config(data_dir=tmp_path, html_to_pdf=False))
    st.fetcher.get_bytes = lambda url: HTML_BODY
    owner = _rec("owner")
    dup = _rec("dup", source="https://s/d")
    st.save_many([owner, dup], label="fr")
    row = _row(st.cfg, owner.doc_id)
    assert row["mime_type"] == "text/html"
    assert "alt_urls" not in row or row["alt_urls"] == []
    assert "alt_source_urls" not in row
    assert st.is_known_url(dup.pdf_url)            # this run: no re-fetch
    assert st.is_known_source_url("https://s/d")
    assert not Storage(Config(data_dir=tmp_path)).is_known_url(dup.pdf_url)  # next night: retried
    [entry] = _log(st.cfg)
    assert entry["persisted"] is False and entry["owner_doc_id"] == owner.doc_id


def test_stamp_duplicate_html_true_stamps_html_owner(tmp_path):
    st = Storage(Config(data_dir=tmp_path, html_to_pdf=False, stamp_duplicate_html=True))
    st.fetcher.get_bytes = lambda url: HTML_BODY
    owner, dup = _rec("owner"), _rec("dup")
    st.save_many([owner, dup], label="fr")
    assert _row(st.cfg, owner.doc_id)["alt_urls"] == [dup.pdf_url]
    assert _log(st.cfg)[0]["persisted"] is True


def test_persisted_stamp_logged_as_json_line(tmp_path):
    st = _storage(tmp_path)
    owner = _rec("owner", bank="ecb")
    dup = _rec("dup", bank="fr", source="https://s/d")
    st.save_many([owner])
    st.save_many([dup], label="fr")
    [e] = _log(st.cfg)
    assert {k: e[k] for k in ("url", "source_url", "owner_doc_id", "owner_bank", "label",
                              "persisted")} == {
        "url": dup.pdf_url, "source_url": "https://s/d", "owner_doc_id": owner.doc_id,
        "owner_bank": "ecb", "label": "fr", "persisted": True}
    assert e["ts"]


def test_summary_line_only_when_duplicates(tmp_path, capsys):
    st = _storage(tmp_path)
    st.save_many([_rec("owner")], label="fr")
    assert "duplicate-content" not in capsys.readouterr().err
    assert not (st.cfg.reports_dir / "duplicate_stamps.jsonl").exists()


def test_count_resets_per_batch(tmp_path, capsys):
    st = _storage(tmp_path)
    st.save_many([_rec("owner"), _rec("d1"), _rec("d2")], label="a")
    assert "[a] duplicate-content: 2 " in capsys.readouterr().err
    st.save_many([_rec("d3")], label="b")
    err = capsys.readouterr().err
    assert "[b] duplicate-content: 1 " in err and "duplicate-content: 3" not in err


def test_flush_failure_never_masks_the_batch(tmp_path, monkeypatch, capsys):
    st = _storage(tmp_path)

    def boom(*a, **k):
        raise ValueError("manifest corrupt")
    monkeypatch.setattr(Storage, "_merge_stamps", boom)
    counts = st.save_many([_rec("owner"), _rec("dup")], label="fr")
    assert counts == {"saved": 1, "skip": 1}
    assert "stamping failed" in capsys.readouterr().err
    assert st._pending_dups == []


def test_batch_exception_still_propagates_when_flush_fails(tmp_path, monkeypatch):
    st = _storage(tmp_path)

    def boom(*a, **k):
        raise ValueError("manifest corrupt")
    monkeypatch.setattr(Storage, "_merge_stamps", boom)

    def recs():
        yield _rec("owner")
        yield _rec("dup")
        raise RuntimeError("discovery crashed")
    with pytest.raises(RuntimeError, match="discovery crashed"):
        st.save_many(recs(), label="fr")


def test_normalized_variant_of_owner_url_not_stamped(tmp_path):
    st = _storage(tmp_path)
    owner = DocRecord(bank_code="fr", doc_type=DocType.D1, title="o",
                      pdf_url="https://www.example.org/fr/a.pdf", source_url="https://s/o")
    variant = DocRecord(bank_code="fr", doc_type=DocType.D1, title="v",
                        pdf_url="https://www.example.org//fr/a.pdf", source_url="https://s//o")
    st.save_many([owner, variant])
    row = _row(st.cfg, owner.doc_id)
    assert row.get("alt_urls") == [] and "alt_source_urls" not in row
    assert st.stamp_alt_urls({owner.doc_id: ["https://www.example.org/fr//a.pdf"]}) == (0, 0)


def test_dry_run_flush_discards_queue_and_writes_nothing(tmp_path):
    st = _storage(tmp_path)
    owner = _rec("owner")
    st.save(owner)
    st._note_duplicate(_rec("dup"), owner.doc_id)      # leftover from a real save
    before = st.cfg.manifest_file("fr").read_bytes()
    st.save_many([], dry_run=True)
    assert st._pending_dups == []
    assert st.cfg.manifest_file("fr").read_bytes() == before
    assert not (st.cfg.reports_dir / "duplicate_stamps.jsonl").exists()


def test_merge_happens_under_lock_on_fresh_row(tmp_path, monkeypatch):
    """A change made to the owner row by another writer after this Storage
    loaded it (between scan and write) must survive the stamp."""
    st = _storage(tmp_path)
    owner = _rec("owner")
    st.save(owner)
    path = st.cfg.manifest_file("fr")
    real_read = storage_mod._read_rows_for_rewrite

    def concurrent_then_read(f):
        if f == path:                      # another process retitles the row
            row = json.loads(path.read_text())
            row["title"] = "Corrected title"
            path.write_text(json.dumps(row) + "\n")
        return real_read(f)
    monkeypatch.setattr(storage_mod, "_read_rows_for_rewrite", concurrent_then_read)
    assert st.stamp_alt_urls({owner.doc_id: ["https://dead.example/a.pdf"]}) == (1, 1)
    row = _row(st.cfg, owner.doc_id)
    assert row["title"] == "Corrected title"
    assert row["alt_urls"] == ["https://dead.example/a.pdf"]


def test_no_rewrite_when_nothing_changes(tmp_path):
    st = _storage(tmp_path)
    owner = _rec("owner")
    st.save(owner)
    path = st.cfg.manifest_file("fr")
    ino = path.stat().st_ino
    assert st.stamp_alt_urls({owner.doc_id: [owner.pdf_url]}) == (0, 0)
    assert path.stat().st_ino == ino


def test_recover_discards_storage_queue(tmp_path, monkeypatch):
    from cb_corpus.recover import run_recover_downloads
    calls = []
    real = Storage.discard_duplicate_stamps
    monkeypatch.setattr(Storage, "discard_duplicate_stamps",
                        lambda self: calls.append(1) or real(self))
    run_recover_downloads(bank_codes=["fr"], download=True,
                          config=Config(data_dir=tmp_path), fetcher=object(),
                          csv_path=str(tmp_path / "r.csv"))
    assert calls == [1]


# ---- final-review fix wave: real PDF owners only, lock, log robustness --

import fcntl  # noqa: E402

from cb_corpus.storage import apply_row_updates, is_stampable_owner  # noqa: E402


def _public(host, port, *args, **kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def _rendering_storage(tmp_path, monkeypatch, **cfg_kw) -> Storage:
    """Default config (html_to_pdf=True) with a fake Chrome that succeeds:
    an HTML page becomes a mime_type=application/pdf row WITH html_path."""
    st = Storage(Config(data_dir=tmp_path, **cfg_kw))
    st.render_resolver = _public
    st.fetcher.get_bytes = lambda url: HTML_BODY

    def fake_render(url, output, **kw):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF-1.4 rendered")
    monkeypatch.setattr(storage_mod, "render_url_to_pdf", fake_render)
    return st


def test_is_stampable_owner_predicate():
    cfg = Config()
    assert is_stampable_owner({"mime_type": "application/pdf"}, cfg)
    assert is_stampable_owner({"mime_type": "Application/PDF", "html_path": None}, cfg)
    assert not is_stampable_owner({"mime_type": "application/pdf", "html_path": "/x.html"}, cfg)
    assert not is_stampable_owner({"mime_type": "text/html"}, cfg)
    assert not is_stampable_owner({}, cfg)
    relaxed = Config(stamp_duplicate_html=True)
    assert is_stampable_owner({"mime_type": "text/html"}, relaxed)
    assert is_stampable_owner({"mime_type": "application/pdf", "html_path": "/x.html"}, relaxed)


def test_rendered_html_owner_not_persisted(tmp_path, monkeypatch):
    st = _rendering_storage(tmp_path, monkeypatch)
    owner, dup = _rec("owner"), _rec("dup", source="https://s/d")
    st.save_many([owner, dup], label="fr")
    row = _row(st.cfg, owner.doc_id)
    assert row["mime_type"] == "application/pdf" and row["html_path"]   # rendered
    assert not row.get("alt_urls") and "alt_source_urls" not in row
    assert st.is_known_url(dup.pdf_url)                                # this run only
    [entry] = _log(st.cfg)
    assert entry["persisted"] is False
    st2 = Storage(Config(data_dir=tmp_path))
    assert not st2.is_known_url(dup.pdf_url)                           # retried next night
    assert not st2.is_known_source_url("https://s/d")


def test_rendered_html_owner_persisted_when_relaxed(tmp_path, monkeypatch):
    st = _rendering_storage(tmp_path, monkeypatch, stamp_duplicate_html=True)
    owner, dup = _rec("owner"), _rec("dup")
    st.save_many([owner, dup], label="fr")
    assert _row(st.cfg, owner.doc_id)["alt_urls"] == [dup.pdf_url]
    assert _log(st.cfg)[0]["persisted"] is True


def _flipped_owner(tmp_path, **cfg_kw):
    """An HTML row later flipped by convert-html / retry-html: mime pdf,
    html_path set, sha256 still the HTML's."""
    st = Storage(Config(data_dir=tmp_path, html_to_pdf=False))
    st.fetcher.get_bytes = lambda url: HTML_BODY
    owner = _rec("owner")
    st.save(owner)
    row = _row(st.cfg, owner.doc_id)
    html = row["local_path"]
    row.update(mime_type="application/pdf", local_path=html[:-5] + ".pdf", html_path=html)
    apply_row_updates(st.cfg, {owner.doc_id: row})
    st2 = Storage(Config(data_dir=tmp_path, html_to_pdf=False, **cfg_kw))
    st2.fetcher.get_bytes = lambda url: HTML_BODY
    return st2, owner


def test_converted_html_owner_not_persisted(tmp_path):
    st, owner = _flipped_owner(tmp_path)
    dup = _rec("dup")
    assert st.save_many([dup], label="fr") == {"skip": 1}
    assert not _row(st.cfg, owner.doc_id).get("alt_urls")
    assert _log(st.cfg)[0]["persisted"] is False


def test_converted_html_owner_persisted_when_relaxed(tmp_path):
    st, owner = _flipped_owner(tmp_path, stamp_duplicate_html=True)
    dup = _rec("dup")
    st.save_many([dup], label="fr")
    assert _row(st.cfg, owner.doc_id)["alt_urls"] == [dup.pdf_url]
    assert _log(st.cfg)[0]["persisted"] is True


def _lock_is_held(data_file) -> bool:
    lock = data_file.with_name("." + data_file.name + ".lock")
    with lock.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def test_merge_rows_locked_holds_sidecar_lock_during_read_and_replace(tmp_path, monkeypatch):
    st = _storage(tmp_path)
    owner = _rec("owner")
    st.save(owner)
    path = st.cfg.manifest_file("fr")
    assert not _lock_is_held(path)
    seen: list[tuple[str, bool]] = []
    real_read, real_replace = storage_mod._read_rows_for_rewrite, storage_mod.os.replace

    def spy_read(f):
        seen.append(("read", _lock_is_held(path)))
        return real_read(f)

    def spy_replace(src, dst):
        seen.append(("replace", _lock_is_held(path)))
        return real_replace(src, dst)
    monkeypatch.setattr(storage_mod, "_read_rows_for_rewrite", spy_read)
    monkeypatch.setattr(storage_mod.os, "replace", spy_replace)
    assert st.stamp_alt_urls({owner.doc_id: ["https://dead.example/a.pdf"]}) == (1, 1)
    assert seen == [("read", True), ("replace", True)]
    assert not _lock_is_held(path)


def test_merge_failure_still_logs_then_warns(tmp_path, monkeypatch, capsys):
    st = _storage(tmp_path)

    def boom(*a, **k):
        raise ValueError("manifest corrupt")
    monkeypatch.setattr(Storage, "_merge_stamps", boom)
    owner, dup = _rec("owner"), _rec("dup", source="https://s/d")
    st.save_many([owner, dup], label="fr")
    [e] = _log(st.cfg)
    assert e["persisted"] is False and e["url"] == dup.pdf_url
    assert e["source_url"] == "https://s/d" and e["label"] == "fr"
    assert "ValueError: manifest corrupt" in e["error"]
    assert "stamping failed" in capsys.readouterr().err
