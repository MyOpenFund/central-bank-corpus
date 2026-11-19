"""Seam tests: pin the wiring BETWEEN independently-tested layers.

A cold audit found three layers stitched together with no test exercising the
join itself:
  1. `native_only`: CLI flag -> pipeline.run -> adapter.discover_all(kwarg).
  2. `skip_url` binding in run_repec: asserted callable somewhere, but never
     which index (`storage.is_known_source_url` vs the adjacent, semantically
     different `storage.is_known_url`).
Each layer already has its own unit tests; these tests only cover the seam.
"""
import json

from cb_corpus.config import Config
from cb_corpus.models import DocRecord
from cb_corpus.taxonomy import DocType, FULL_SCOPE


# ---- seam 1: native_only, CLI -> pipeline.run -> adapter.discover_all ------
def test_native_only_seam_pipeline_run_reaches_adapter_discover_all(tmp_path, monkeypatch):
    """pipeline.run must pass native_only through get_adapter/ADAPTERS to the
    adapter's discover_all — not just accept the kwarg itself."""
    from cb_corpus import pipeline as pl
    from cb_corpus.adapters import base as base_mod

    calls = []

    class RecordingAdapter(base_mod.BankAdapter):
        native_types = ()

        def discover_all(self, scope=FULL_SCOPE, since=None, native_only=False):
            calls.append(native_only)
            return iter([])

    # Register through the REAL registry (bespoke class wins over the generic
    # fallback in get_adapter) rather than monkeypatching pipeline.get_adapter
    # itself — that would skip the seam we're trying to pin.
    monkeypatch.setitem(base_mod.ADAPTERS, "se", RecordingAdapter)

    cfg = Config(data_dir=tmp_path)
    pl.run(bank_codes=["se"], dry_run=True, config=cfg, native_only=True)
    assert calls == [True]

    calls.clear()
    pl.run(bank_codes=["se"], dry_run=True, config=cfg, native_only=False)
    assert calls == [False]


def test_native_only_seam_cli_discover_reaches_pipeline_run(monkeypatch):
    """The CLI --native-only flag must reach pipeline.run as native_only=True,
    not merely cause pipeline.run to be called (a stray kwarg-name typo or a
    dropped flag would still pass a "does it get called" test)."""
    from cb_corpus import cli

    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"se": {"saved": 0}}

    monkeypatch.setattr(cli, "run", fake_run)

    assert cli.main(["discover", "--banks", "se", "--native-only"]) == 0
    assert captured["native_only"] is True

    captured.clear()
    assert cli.main(["discover", "--banks", "se"]) == 0
    assert captured["native_only"] is False


# ---- seam 2: skip_url binding in run_repec ---------------------------------
def _seed_manifest(cfg: Config, bank_code: str, *, document_url: str, source_url: str) -> None:
    cfg.manifest_dir.mkdir(parents=True, exist_ok=True)
    rec = DocRecord(bank_code=bank_code, doc_type=DocType.D1, title="seed",
                    document_url=document_url, source_url=source_url)
    with cfg.manifest_file(bank_code).open("w") as fh:
        fh.write(json.dumps(rec.to_row(), ensure_ascii=False) + "\n")


def test_run_repec_skip_url_is_is_known_source_url_not_is_known_url(tmp_path, monkeypatch):
    """run_repec must bind skip_url to storage.is_known_source_url — its OWN
    index (source pages, e.g. IDEAS paper pages) — not the adjacent
    storage.is_known_url (PDF urls). A seam collapse onto is_known_url would
    silently skip papers whose PDF happens to already be known under a
    different source page, or (as here) wrongly treat a known PDF url as a
    known SOURCE url. Covers both incremental=True and incremental=False."""
    from cb_corpus import pipeline as pl
    from cb_corpus.sources import repec as repec_mod

    cfg = Config(data_dir=tmp_path)
    seeded_pdf = "https://boe.test/seed.pdf"
    seeded_source = "https://ideas.repec.org/p/boe/boeewp/seed.html"
    _seed_manifest(cfg, "gb", document_url=seeded_pdf, source_url=seeded_source)

    captured = {}

    class FakeRePEcDiscovery:
        def __init__(self, fetcher):
            pass

        def discover_bank(self, code, skip_url=None, stop_on_known=False):
            captured["skip_url"] = skip_url
            captured["stop_on_known"] = stop_on_known
            return iter([])

    monkeypatch.setattr(repec_mod, "RePEcDiscovery", FakeRePEcDiscovery)

    for incremental in (True, False):
        captured.clear()
        pl.run_repec(bank_codes=["gb"], dry_run=True, config=cfg, incremental=incremental)
        assert captured["stop_on_known"] is incremental
        skip = captured["skip_url"]
        # Pin the INDEX: True for the known SOURCE url (is_known_source_url
        # semantics)...
        assert skip(seeded_source) is True
        # ...but False for the known PDF url — is_known_url would say True
        # here, so this is what actually distinguishes the two indexes.
        assert skip(seeded_pdf) is False
        # And a genuinely unknown url is False either way.
        assert skip("https://boe.test/unknown.pdf") is False
