"""The corpus vocabulary file of the `documents` contract (vault-contract 1.0.0).

`vocabulary.json` declares the codes a manifest row may use: the source codes
(the 63 BIS member banks, `banks.BIS_63`, labelled with their names), the
document types (`taxonomy.DocType`, A1..G3 with labels and families), the
provenances the code writes (`models.PROVENANCES`) and the entity schemes
(none: a central-bank document is not about one company). The vault loads it
before the documents and refuses a row whose `source_code`, `doc_type` or
`provenance` it does not declare, so it is GENERATED from the code, never
edited by hand.

It lives at the data root, next to `manifest/`, `runs.jsonl` and
`cadence.jsonl` (`/app/data/vocabulary.json` in the container; the vault
ingestion reads it through `VOCABULARY_PATH`, the same file seen from its
mount). Written by every reporting command (discover, bis-sitemap, repec: the
nightly sync) and by `migrate-contract`; rewritten only when its content
changes (atomic temp + os.replace).

This module does not import vault-contract: the contract is a dev/test
dependency only (tests/test_vocabulary.py validates the output with it).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .banks import BIS_63
from .models import CORPUS, PROVENANCES
from .taxonomy import DocType

#: The contract this producer writes (pinned by commit in requirements-dev.txt).
CONTRACT_NAME = "documents"
CONTRACT_VERSION = "1.0.0"
#: File name at the data root.
VOCABULARY_FILE = "vocabulary.json"


def build_vocabulary() -> dict:
    """The vocabulary document, from the code (deterministic order)."""
    return {
        "contract": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "corpus": CORPUS,
        "source_codes": [{"code": b.code, "label": b.name} for b in BIS_63],
        "doc_types": [{"code": dt.code, "label": dt.label, "family": dt.group}
                      for dt in DocType],
        "provenances": list(PROVENANCES),
        "entity_schemes": [],
    }


def vocabulary_bytes() -> bytes:
    return (json.dumps(build_vocabulary(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_vocabulary(data_dir: Path) -> Path:
    """Write `<data_dir>/vocabulary.json` when absent or different (temp +
    fsync + os.replace, so a reader never sees half a file). Returns its path."""
    data_dir = Path(data_dir)
    path = data_dir / VOCABULARY_FILE
    data = vocabulary_bytes()
    try:
        if path.read_bytes() == data:
            return path
    except FileNotFoundError:
        pass
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{VOCABULARY_FILE}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path
