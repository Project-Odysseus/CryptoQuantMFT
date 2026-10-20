"""The lab's data layer (plan Phase 1): instruments, adapters, the versioned catalog, quality cards, futures
construction, aligned panels.

    from quantlab.data import InstrumentMaster, DataCatalog, load_panel
    master = InstrumentMaster.load("config/instruments.toml")
    panel = load_panel(master, ["idx:NDX", "idx:SPX"], catalog=DataCatalog("data/quantlab"))
    panel["close"], panel.cards["idx:NDX"].to_markdown(), panel.snapshots
"""

from quantlab.data.catalog import DataCatalog, Snapshot, fingerprint
from quantlab.data.instruments import ContractSpec, Instrument, InstrumentMaster
from quantlab.data.panel import Panel, align, asof_daily, load_panel, universe
from quantlab.data.quality import DataCard, data_card

__all__ = ["ContractSpec", "DataCard", "DataCatalog", "Instrument", "InstrumentMaster", "Panel", "Snapshot", "align", "asof_daily", "data_card",
           "fingerprint", "load_panel", "universe"]
