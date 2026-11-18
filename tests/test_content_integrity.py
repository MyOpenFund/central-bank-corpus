"""Content integrity of stored bytes (issue #21, integrity wave 2026-11, A1-A3).

An HTTP 200 is not a document. Storage.save() checks each candidate URL's
bytes BEFORE the sha256 dedup, Storage.reindex() checks on-disk `.pdf`
files, artifacts are written atomically, and a Chrome render is checked
before it replaces the HTML as the artifact. No network: every fetcher is a
local fake."""
from __future__ import annotations

import pytest

from cb_corpus.config import Config


# -- Config ---------------------------------------------------------------

def test_config_defaults():
    cfg = Config()
    assert cfg.min_pdf_bytes == 1024
    assert cfg.pdf_require_eof is True
    assert cfg.pdf_url_served_html == "reject"


@pytest.mark.parametrize("bad", [-1, 1.5, "1024", None, True])
def test_config_rejects_bad_min_pdf_bytes(bad):
    with pytest.raises(ValueError, match="min_pdf_bytes"):
        Config(min_pdf_bytes=bad)


def test_config_accepts_zero_min_pdf_bytes():
    assert Config(min_pdf_bytes=0).min_pdf_bytes == 0


@pytest.mark.parametrize("bad", ["Reject", "keep", "", None, True])
def test_config_rejects_unknown_pdf_url_served_html(bad):
    with pytest.raises(ValueError, match="pdf_url_served_html"):
        Config(pdf_url_served_html=bad)


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_config_rejects_non_bool_pdf_require_eof(bad):
    with pytest.raises(ValueError, match="pdf_require_eof"):
        Config(pdf_require_eof=bad)
