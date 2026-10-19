"""This repository's data sources as quantlab adapters (docs/quantlab_migration.md, "Adapt").

quantlab never imports the host; the host registers its sources with quantlab instead:

    from src.lab_adapters import register_host_adapters
    register_host_adapters()            # adds "kraken" and "contracts" to quantlab's adapters

Each adapter returns frames in the lab's bar schema (bars indexed by when they are final).
"""

from __future__ import annotations

CONTRACT_STORE = "data/market_data/futures/daily"


def register_host_adapters() -> None:
    """Register every host adapter with quantlab."""
    from quantlab.data import adapters

    from quantlab.data.adapters.files import FileAdapter

    from src.lab_adapters.kraken import KrakenAdapter

    adapters.register(KrakenAdapter())
    contracts = FileAdapter(CONTRACT_STORE)  # one CSV per futures contract code, recorded daily (scripts/collectors/futures_daily.py)
    contracts.name = "contracts"
    adapters.register(contracts)
