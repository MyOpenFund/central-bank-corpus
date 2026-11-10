"""B5 (grit wave 2026-11): `banks_sources.toml` load problems are reported
loudly on stderr instead of being discarded, and a malformed file no longer
breaks the CLI import."""
from __future__ import annotations

import pytest

import cb_corpus.adapters as adapters
from cb_corpus.adapters import declarative

GOOD = """
[ch.sitemap]
sitemap_url = "https://www.snb.ch/sitemap.xml"
[ch.sitemap.patterns]
A2 = "/x/.*\\\\.pdf$"

[ca.listing]
[ca.listing.entries]
A3 = [["https://bankofcanada.ca/list", "/a3/.*"]]
"""


@pytest.fixture
def registry(monkeypatch):
    reg: dict = {}
    monkeypatch.setattr(declarative, "INSTANCE_FACTORIES", reg)
    return reg


def _write(tmp_path, text):
    p = tmp_path / "banks_sources.toml"
    p.write_text(text)
    return p


def test_good_file_reports_loaded_and_no_warnings(tmp_path, registry):
    rep = declarative.load_toml(_write(tmp_path, GOOD))
    assert rep.loaded == ["ch", "ca"]
    assert set(registry) == {"ch", "ca"}
    assert rep.warnings() == []


def test_missing_file_is_a_warning(tmp_path, registry):
    rep = declarative.load_toml(tmp_path / "nope.toml")
    assert rep.loaded == [] and registry == {}
    [w] = rep.warnings()
    assert "not found" in w and "nope.toml" in w


def test_empty_file_zero_loaded_is_a_warning(tmp_path, registry):
    rep = declarative.load_toml(_write(tmp_path, "# nothing\n"))
    assert rep.loaded == []
    assert any("0 bank" in w for w in rep.warnings())


def test_unknown_bank_code_warned_and_not_registered(tmp_path, registry):
    text = GOOD + '\n[zz.sitemap]\nsitemap_url = "https://x/sm.xml"\n'
    rep = declarative.load_toml(_write(tmp_path, text))
    assert rep.unknown_codes == ["zz"]
    assert "zz" not in registry and set(registry) == {"ch", "ca"}
    assert any("unknown bank code" in w and "zz" in w for w in rep.warnings())


def test_unknown_section_warned(tmp_path, registry):
    text = GOOD + '\n[se.sitmap]\nsitemap_url = "https://x/sm.xml"\n'
    rep = declarative.load_toml(_write(tmp_path, text))
    assert rep.unknown_sections == ["se.sitmap"]
    assert "se" not in registry
    assert any("unknown section" in w and "se.sitmap" in w for w in rep.warnings())


def test_malformed_toml_does_not_raise_and_registers_nothing(tmp_path, registry):
    rep = declarative.load_toml(_write(tmp_path, GOOD + "\n[broken\n"))
    assert rep.error and registry == {}
    assert any("malformed" in w for w in rep.warnings())


def test_bad_entry_aborts_the_whole_file_atomically(tmp_path, registry):
    # A typo'd doc-type code fails while building factories: nothing half-registered.
    text = GOOD + '\n[se.sitemap]\nsitemap_url = "https://x"\n[se.sitemap.patterns]\nQ9 = "x"\n'
    rep = declarative.load_toml(_write(tmp_path, text))
    assert rep.error and "Q9" in rep.error
    assert registry == {}


def test_import_hook_prints_warnings_and_survives_malformed(tmp_path, registry,
                                                            monkeypatch, capsys):
    monkeypatch.setattr(declarative, "DEFAULT_TOML", _write(tmp_path, "[broken\n"))
    adapters._load_declarative()          # must not raise
    err = capsys.readouterr().err
    assert "banks_sources.toml" in err and "malformed" in err
    assert "declarative adapters skipped" in err


def test_import_hook_survives_an_unexpected_exception(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("surprise")
    monkeypatch.setattr(declarative, "load_toml", boom)
    adapters._load_declarative()
    assert "surprise" in capsys.readouterr().err


def test_shipped_file_loads_cleanly(registry):
    rep = declarative.load_toml()
    assert rep.loaded and rep.warnings() == []
