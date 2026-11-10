"""Importing this package registers every adapter (generic + bespoke + TOML)."""
from . import base  # noqa: F401  -> registers GenericAdapter for all 63 banks
from . import fed   # noqa: F401  -> overrides "us"
from . import ecb   # noqa: F401  -> overrides "ecb"
from . import boj   # noqa: F401  -> overrides "jp" (A3 minutes + native D1 WPs)
from . import boe   # noqa: F401  -> overrides "gb" (native D1 staff working papers)
from . import buba  # noqa: F401  -> overrides "de" (native D1 discussion papers)
from . import bdf   # noqa: F401  -> overrides "fr" (E2 sitemap + native D1 WPs)
from . import se    # noqa: F401  -> overrides "se" (native D1 working papers)
from . import rba   # noqa: F401  -> overrides "au" (A1 decisions)
from . import declarative as _declarative


def _load_declarative() -> None:
    """Register the `banks_sources.toml` adapters, printing every problem
    (missing file, 0 banks, unknown bank code / section, malformed file) on
    stderr. Never raises: a broken TOML must not break the CLI import -- the
    declarative adapters are skipped and the bespoke/generic ones still work."""
    import sys
    try:
        warnings = _declarative.load_toml().warnings()
    except Exception as exc:  # noqa: BLE001 -- belt and braces, see docstring
        warnings = [f"banks_sources.toml could not be loaded "
                    f"({type(exc).__name__}: {exc}): declarative adapters skipped"]
    for w in warnings:
        print(f"[adapters] WARNING: {w}", file=sys.stderr, flush=True)


_load_declarative()

from .base import (  # noqa: E402,F401
    ADAPTERS, INSTANCE_FACTORIES, BankAdapter, GenericAdapter,
    get_adapter, register,
)
