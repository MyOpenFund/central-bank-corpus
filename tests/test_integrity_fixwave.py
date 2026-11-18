"""Fix wave on the integrity storage work (issue #21, review 2026-11):
artifact file modes, env-driven content-check knobs, the bad_content
run-report counter, checked renders in convert-html / retry-html, the
MIME sniff, `.part` temps in sweep-orphans and a read-only quarantine dir."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from cb_corpus import storage as storage_mod
from cb_corpus.storage import _atomic_write_bytes
from tests.conftest import public_resolver
from tests.test_content_integrity import (U, _rec, _render_storage, _storage,
                                          make_pdf)


@pytest.fixture
def umask():
    """Set the process umask for one test, restored afterwards."""
    old = os.umask(0o022)
    os.umask(old)
    yield lambda m: os.umask(m)
    os.umask(old)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# -- M1: artifacts follow the umask ----------------------------------------

@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o002, 0o664)])
def test_atomic_write_follows_the_umask(tmp_path, umask, mask, expected):
    umask(mask)
    p = tmp_path / "a.pdf"
    _atomic_write_bytes(p, b"data")
    assert _mode(p) == expected


@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o002, 0o664)])
def test_saved_pdf_follows_the_umask(tmp_path, umask, mask, expected):
    umask(mask)
    st = _storage(tmp_path, {U: (make_pdf(), "application/pdf")})
    rec = _rec(U)
    assert st.save(rec) == "saved"
    assert _mode(Path(rec.local_path)) == expected


@pytest.mark.parametrize("mask,expected", [(0o022, 0o644), (0o002, 0o664)])
def test_rendered_pdf_follows_the_umask(tmp_path, monkeypatch, umask, mask, expected):
    umask(mask)
    st, _ = _render_storage(tmp_path, monkeypatch,
                            lambda out: out.write_bytes(make_pdf("r")))
    rec = _rec(U, mime="text/html")
    assert st.save(rec) == "saved"
    assert rec.local_path.endswith(".pdf")
    assert _mode(Path(rec.local_path)) == expected
    assert _mode(Path(rec.html_path)) == expected
