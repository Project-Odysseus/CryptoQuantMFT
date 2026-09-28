"""The portfolio review reads runtime snapshots and benchmarks the book against its own BTC marks."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.portfolio.review import review, snapshot_frame


def _snapshots(days: int = 60) -> list[dict]:
    rng = np.random.default_rng(3)
    btc = 60_000 * np.cumprod(1 + rng.normal(0.001, 0.03, days * 6))
    out, equity = [], 100.0
    for i, price in enumerate(btc):
        if i:
            equity *= 1 + 0.5 * (price / btc[i - 1] - 1)  # half exposure to BTC
        out.append({"timestamp": (pd.Timestamp("2026-10-01", tz="UTC") + pd.Timedelta(hours=4 * i)).isoformat(), "portfolio": "btc-live", "equity": equity,
                    "instruments": {"kraken_futures:BTC/USD": {"price": price, "units": 0.001, "weight": 0.5, "realized_pnl": 0.0, "fees": 0.01, "funding": 0.002}},
                    "sleeves": {"btc_ma_1d": {"instrument": "kraken_futures:BTC/USD", "strategy": "moving_average_crossover", "own_weight": 1.0, "allocated_weight": 0.5, "pnl": 1.0}}})
    return out[::-1]  # the logger returns newest first


def test_review_recovers_half_beta_from_snapshots() -> None:
    tables = review(_snapshots())
    versus = tables["versus"].iloc[:, 0]
    assert versus["beta"] == pytest.approx(0.5, abs=0.02)  # daily compounding of a 4-hourly half position, so not exact
    assert versus["correlation"] > 0.99
    assert tables["summary"].loc["days", "book"] == 59
    assert list(tables["instruments"].index) == ["kraken_futures:BTC/USD"] and "btc_ma_1d" in tables["sleeves"].index


def test_a_zero_mark_is_missing_not_a_price() -> None:
    snaps = _snapshots(3)
    snaps[0]["instruments"]["kraken_futures:BTC/USD"]["price"] = 0.0
    assert snapshot_frame(snaps)["price:kraken_futures:BTC/USD"].isna().sum() == 1
