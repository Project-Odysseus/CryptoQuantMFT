"""The lab's instrument master and host adapters stay consistent with the host's own tables."""

from __future__ import annotations

import csv
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from quantlab.data import InstrumentMaster
from src.lab_adapters.kraken import KrakenAdapter
from src.storage.bar_aggregator import OHLCVBar

ROOT = Path(__file__).resolve().parents[1]


def test_the_master_is_generated_from_the_current_tables() -> None:
    result = subprocess.run([sys.executable, str(ROOT / "scripts/lab/build_instrument_master.py"), "--check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_master_agrees_with_the_futures_runtime_table() -> None:
    master = InstrumentMaster.load(ROOT / "config/instruments.toml")
    with (ROOT / "config/futures_markets.csv").open() as handle:
        for row in csv.DictReader(handle):
            item = master[row["market"]]
            assert item.multiplier == float(row["multiplier"]) and item.tick_size == float(row["tick_size"])
            assert item.contract.months == row["months"] and item.contract.last_trade_rule == row["last_trade_rule"]
            assert item.contract.roll_sessions_before == int(row["roll_sessions_before"]) and item.calendar == row["calendar"]
    proxies = [item for item in master if item.is_proxy]
    assert proxies and all(master[item.proxy_for].kind == "future" for item in proxies)


def test_the_kraken_adapter_restamps_host_candles_by_their_close() -> None:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = [OHLCVBar(exchange="kraken_futures", symbol="BTC/USD", interval_seconds=14_400, timestamp=start + timedelta(hours=4 * i),
                     open=100.0 + i, high=101.0 + i, low=99.0 + i, close=100.5 + i, volume=1.0) for i in range(3)]
    frame = KrakenAdapter(loader=lambda symbol, interval: bars).fetch("BTC/USD", interval="4h")
    assert frame.index[0] == pd.Timestamp("2024-01-01 04:00", tz="UTC") and list(frame["close"]) == [100.5, 101.5, 102.5]
    daily = KrakenAdapter(loader=lambda symbol, interval: bars[:1]).fetch("BTC/USD", interval="1d")
    assert daily.index[0] == pd.Timestamp("2024-01-01")
