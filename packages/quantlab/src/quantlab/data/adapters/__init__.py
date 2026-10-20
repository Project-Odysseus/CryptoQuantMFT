"""Data adapters: every external source behind one interface (plan 1.2).

An adapter turns (symbol, interval, start, end) into a frame in the lab's schema: OHLCV bars (quantlab.data.bars) or,
for macro series, a `value` column. No study calls a vendor directly; the catalog calls adapters and snapshots what
they return. A host or a future employer adds its own sources by registering an adapter:

    from quantlab.data.adapters import register
    register(MyBloombergExport(root="/data/bbg"))

Built in: `yahoo` (free index/ETF/futures bars), `yahoo_tr` (the same, scaled to total return), `oecd` (monthly 3-month interbank rates), `norges_bank` (daily FX
fixings against NOK), `norges_bank_usd` (the same, crossed to US dollars), and `file` (CSV or Parquet files you bring).
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd


class DataAdapter(Protocol):
    """A data source. `version` changes when the adapter's parsing changes, so snapshots record how data was made."""

    name: str
    version: str

    def fetch(self, symbol: str, *, interval: str, start: str | None = None, end: str | None = None) -> pd.DataFrame: ...


_REGISTRY: dict[str, DataAdapter] = {}
_BUILTINS_LOADED = False


def register(adapter: DataAdapter) -> None:
    """Make an adapter available by its name (replacing one of the same name, built-ins included)."""
    _register_builtins()
    _REGISTRY[adapter.name] = adapter


def get(name: str) -> DataAdapter:
    """A registered adapter; the built-in ones are registered on first use."""
    _register_builtins()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"no data adapter {name!r}; registered: {sorted(_REGISTRY)}") from None


def names() -> list[str]:
    """Registered adapter names."""
    _register_builtins()
    return sorted(_REGISTRY)


def _register_builtins() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from quantlab.data.adapters.files import FileAdapter
    from quantlab.data.adapters.public import NorgesBankAdapter, NorgesBankUsdAdapter, OecdRatesAdapter, YahooAdapter, YahooTotalReturnAdapter

    for adapter in (YahooAdapter(), YahooTotalReturnAdapter(), OecdRatesAdapter(), NorgesBankAdapter(), NorgesBankUsdAdapter(), FileAdapter()):
        _REGISTRY.setdefault(adapter.name, adapter)
