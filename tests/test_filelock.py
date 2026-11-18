"""filelock.locked_sidecar: the one cross-process lock shared by the
manifests (storage.py) and the download quarantine (quarantine.py)."""
from __future__ import annotations

from cb_corpus import storage
from cb_corpus.filelock import locked_sidecar


def test_storage_locked_is_the_shared_sidecar_lock():
    # The historical name stays importable (tests and subprocess helpers use
    # it) and is the very same lock, not a second implementation.
    assert storage._locked is locked_sidecar


def test_sidecar_created_data_file_untouched(tmp_path):
    data = tmp_path / "sub" / "state.jsonl"
    with locked_sidecar(data):
        assert (tmp_path / "sub" / ".state.jsonl.lock").exists()
    assert not data.exists()
