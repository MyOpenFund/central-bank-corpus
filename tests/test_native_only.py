"""--native-only: shared-catalog sources are skipped, native sources kept."""
from datetime import date
from typing import Iterator, Optional

from cb_corpus.adapters.base import BankAdapter
from cb_corpus.banks import get_bank
from cb_corpus.models import DocRecord
from cb_corpus.taxonomy import DocType


class _SpyShared:
    """Stands in for BISSpeechIndex / RePEcDiscovery on an adapter instance."""
    def __init__(self):
        self.calls = 0

    def discover(self, *a, **k) -> Iterator[DocRecord]:
        self.calls += 1
        return iter(())

    def discover_bank(self, *a, **k) -> Iterator[DocRecord]:
        self.calls += 1
        return iter(())


def _rec(bank: str, dt: DocType, url: str) -> DocRecord:
    return DocRecord(bank_code=bank, doc_type=dt, title="t", document_url=url,
                     source_url=url, date=date(2026, 1, 1), language="en",
                     provenance="test")


class _NativeA3(BankAdapter):
    native_types = (DocType.A3, DocType.D1)

    def _discover_native(self, doc_type: DocType,
                         since: Optional[date]) -> Iterator[DocRecord]:
        yield _rec(self.bank.code, doc_type, f"https://x.test/{doc_type.code}.pdf")


def _spied(cls):
    a = cls(get_bank("se"))
    a._bis = _SpyShared()
    a._repec = _SpyShared()
    return a


def test_default_uses_shared_catalogs():
    a = _spied(_NativeA3)
    list(a.discover_all(scope=(DocType.C1, DocType.D2)))
    assert a._bis.calls == 1        # C1 → BIS index
    assert a._repec.calls == 1      # D2 non-native → RePEc


def test_native_only_never_touches_shared_catalogs():
    a = _spied(_NativeA3)
    recs = list(a.discover_all(scope=(DocType.C1, DocType.A3,
                                      DocType.D1, DocType.D2),
                               native_only=True))
    assert a._bis.calls == 0
    assert a._repec.calls == 0
    # native types still yielded (A3 via _discover_native, D1 via native branch)
    assert {r.doc_type for r in recs} == {DocType.A3, DocType.D1}


def test_native_only_generic_bank_yields_nothing():
    class _Generic(BankAdapter):
        native_types = ()
    a = _spied(_Generic)
    assert list(a.discover_all(native_only=True)) == []
    assert a._bis.calls == 0 and a._repec.calls == 0


def test_native_only_still_honors_skip_known_url():
    a = _spied(_NativeA3)
    a._skip_known_url = lambda url: url.endswith("D1.pdf")
    recs = list(a.discover_all(scope=(DocType.D1,), native_only=True))
    assert recs == []
