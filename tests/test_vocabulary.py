"""`vocabulary.json`: the corpus vocabulary of the `documents` contract
(vault-contract 1.0.0), generated from the code (taxonomy, banks, the
provenances the code writes) and written at the data root."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import vault_contract
from vault_contract import load_vocabulary, validate_vocabulary

from cb_corpus import vocabulary as voc
from cb_corpus.banks import BIS_63
from cb_corpus.models import CORPUS, PROVENANCES
from cb_corpus.taxonomy import DocType

PKG = Path(__file__).resolve().parent.parent / "cb_corpus"


def test_vocabulary_is_valid_for_the_pinned_contract():
    doc = voc.build_vocabulary()
    assert validate_vocabulary(doc) == []
    v = load_vocabulary(doc)
    assert v.corpus == CORPUS == "central-bank"
    assert doc["contract"] == vault_contract.CONTRACT_NAME
    assert doc["contract_version"] == vault_contract.CONTRACT_VERSION


def test_every_doc_type_with_label_and_family():
    doc = voc.build_vocabulary()
    assert doc["doc_types"] == [{"code": dt.code, "label": dt.label, "family": dt.group}
                                for dt in DocType]
    codes = [d["code"] for d in doc["doc_types"]]
    assert codes[0] == "A1" and codes[-1] == "G3" and len(codes) == 19


def test_every_bank_with_its_name():
    doc = voc.build_vocabulary()
    assert doc["source_codes"] == [{"code": b.code, "label": b.name} for b in BIS_63]
    assert {"code": "ecb", "label": "European Central Bank"} in doc["source_codes"]


def test_no_entity_scheme_and_declared_provenances():
    doc = voc.build_vocabulary()
    assert doc["entity_schemes"] == []
    assert doc["provenances"] == list(PROVENANCES)


def _literal_provenances() -> set[str]:
    """Every provenance string the package can write, read from its source:
    `provenance="..."` keyword arguments, `x.provenance = "..."` assignments,
    `provenance == "..."` comparisons, and the provenance slot of
    recover._candidate_provenance's return tuples."""
    found: set[str] = set()
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "provenance":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    found.add(node.value.value)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if (isinstance(t, ast.Attribute) and t.attr == "provenance"
                            and isinstance(node.value, ast.Constant)):
                        found.add(node.value.value)
            elif isinstance(node, ast.FunctionDef) and node.name == "_candidate_provenance":
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Return) and isinstance(sub.value, ast.Tuple)
                            and isinstance(sub.value.elts[1], ast.Constant)):
                        found.add(sub.value.elts[1].value)
    return found


def test_declared_provenances_are_exactly_those_the_code_writes():
    found = _literal_provenances()
    # sanity: the scan does see the known writers
    assert {"bank_site", "bis_index", "repec_discovery", "wayback", "mirror"} <= found
    assert found == set(PROVENANCES)


def test_write_vocabulary_at_the_data_root_atomic_and_idempotent(tmp_path):
    path = voc.write_vocabulary(tmp_path)
    assert path == tmp_path / voc.VOCABULARY_FILE == tmp_path / "vocabulary.json"
    assert json.loads(path.read_text(encoding="utf-8")) == voc.build_vocabulary()
    ino = path.stat().st_ino
    mtime = path.stat().st_mtime_ns
    assert voc.write_vocabulary(tmp_path) == path            # unchanged: not rewritten
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (ino, mtime)
    path.write_text("{}", encoding="utf-8")                   # stale -> replaced
    voc.write_vocabulary(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8")) == voc.build_vocabulary()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vocabulary.json"]   # no temp left


def test_write_vocabulary_creates_the_data_root(tmp_path):
    root = tmp_path / "new" / "data"
    voc.write_vocabulary(root)
    assert (root / "vocabulary.json").is_file()
