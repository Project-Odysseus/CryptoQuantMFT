"""Import the host's cached research data into the lab catalog, so lab notebooks run without downloading.

- data/research_cache/h14/*.csv (daily index bars the H14 study downloaded from Yahoo) become yahoo snapshots of the
  master's `idx:*` instruments;
- data/research_cache/multi_asset/rates.csv (OECD 3-month rates, percent by month, the H11 cache) becomes oecd
  snapshots of `rate:*`, each month known from the start of the next (as the OECD adapter stores it).

The adapter version says where each snapshot came from. Importing twice changes nothing (snapshots are by content).

    python scripts/lab/import_research_cache.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from quantlab.data.bars import validate_bars
from quantlab.data.catalog import DataCatalog
from quantlab.data.instruments import InstrumentMaster

CACHE = Path("data/research_cache")


def main() -> None:
    master = InstrumentMaster.load("config/instruments.toml")
    catalog = DataCatalog()
    for path in sorted((CACHE / "h14").glob("*.csv")):
        instrument = f"idx:{path.stem}"
        adapter, symbol = master[instrument].source("1d")
        frame = pd.read_csv(path, index_col=0, parse_dates=True)
        frame = frame[~frame.index.duplicated(keep="last")].sort_index().dropna(subset=["close"])
        frame.index = pd.DatetimeIndex(frame.index).normalize()
        frame.index.name = "time"
        snapshot = catalog.store(adapter, symbol, "1d", validate_bars(frame, interval="1d"), adapter_version=f"imported from {path} (H14 study, Yahoo)")
        print(f"{instrument:14s} {snapshot.rows:6d} bars  {snapshot.first[:10]} to {snapshot.last[:10]}")
    raw = pd.read_csv(CACHE / "multi_asset" / "rates.csv", index_col=0)
    months = pd.PeriodIndex(raw.index, freq="M")
    for area in raw.columns:
        frame = pd.DataFrame({"value": raw[area].to_numpy() / 100.0}, index=months.to_timestamp()).dropna()
        frame["available_at"] = (frame.index.to_period("M") + 1).to_timestamp()
        frame.index.name = "time"
        adapter, symbol = master[f"rate:{area}"].source("1M")
        catalog.store(adapter, symbol, "1M", frame, adapter_version="imported from data/research_cache/multi_asset/rates.csv (H11, OECD)")
        print(f"rate:{area:9s} {len(frame):6d} months")


if __name__ == "__main__":
    main()
