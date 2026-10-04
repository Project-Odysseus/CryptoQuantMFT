"""The signal lab's combining logic (src/research/signal_lab.py). The sleeves themselves need the cached archives."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.research import signal_lab as lab

DAYS = pd.date_range("2022-01-01", "2025-12-31", freq="D", tz="UTC")


def _sleeve(name: str, before: float, after: float, vol: float, *, seed: int, tradable: bool = True) -> lab.Sleeve:
    """A daily series with a set yearly mean before and after the holdout date."""
    noise = np.random.default_rng(seed).normal(0.0, vol / np.sqrt(365), len(DAYS))
    noise -= noise.mean()
    mean = np.where(DAYS < lab.HOLDOUT, before, after) / 365.0
    return lab.Sleeve(name, pd.Series(mean + noise, index=DAYS), name, tradable=tradable)


SLEEVES = {s.name: s for s in (_sleeve("good", 0.30, 0.20, 0.20, seed=1), _sleeve("faded", 0.40, -0.20, 0.40, seed=2), _sleeve("flat", 0.0, 0.30, 0.20, seed=3),
                               _sleeve("theory", 0.50, 0.50, 0.10, seed=4, tradable=False))}


def test_metrics_and_the_summary_split_at_the_holdout() -> None:
    numbers = lab.metrics(SLEEVES["good"].returns, lab.START, lab.HOLDOUT)
    assert numbers["sharpe"] == pytest.approx(numbers["return"] / numbers["vol"]) and numbers["vol"] == pytest.approx(0.20, rel=0.1) and 0 < numbers["max_drawdown"] < 1
    assert np.isnan(lab.metrics(SLEEVES["good"].returns.iloc[:10])["sharpe"])  # too short to say anything
    table = lab.summary(SLEEVES)
    assert table.loc["faded", "sharpe_in_sample"] > 0.5 > 0 > table.loc["faded", "sharpe_holdout"] and table.loc["flat", "sharpe_holdout"] > 1


def test_the_book_is_chosen_on_in_sample_data_only_and_never_takes_a_theoretical_sleeve() -> None:
    weights = lab.choose(SLEEVES, min_sharpe=0.5)
    assert set(weights) == {"good", "faded"}  # "flat" did nothing in-sample (though it works later); "theory" can't be traded
    assert sum(weights.values()) == pytest.approx(1.0) and weights["good"] / weights["faded"] == pytest.approx(2.0, rel=0.15)  # inverse volatility
    chosen, returns = lab.combine(SLEEVES)
    assert chosen == weights and returns.loc[DAYS[5]] == pytest.approx(sum(SLEEVES[name].returns.loc[DAYS[5]] * weight for name, weight in weights.items()))
    assert lab.choose(SLEEVES, min_sharpe=50.0) == {} and lab.combine(SLEEVES, min_sharpe=50.0)[0] == {}


def test_a_preset_book_uses_the_given_weights() -> None:
    weights, returns = lab.combine(SLEEVES, {"good": 3.0, "flat": 1.0})
    assert weights == {"good": 0.75, "flat": 0.25}
    assert returns.loc[DAYS[100]] == pytest.approx(0.75 * SLEEVES["good"].returns.loc[DAYS[100]] + 0.25 * SLEEVES["flat"].returns.loc[DAYS[100]])
    assert lab.combine(SLEEVES, {"theory": 1.0})[0] == {"theory": 1.0}  # a preset may include it: that is the owner's call
    with pytest.raises(ValueError, match="no such sleeve"):
        lab.combine(SLEEVES, {"nope": 1.0})


def test_recorded_coverage_counts_files_and_days(tmp_path) -> None:
    for path in ("market_data/binance/liquidations/2026-10-01.parquet", "market_data/binance/liquidations/2026-10-02.parquet", "options/deribit/BTC/2026-10-02/120000.parquet"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_bytes(b"x" * 1000)
    table = lab.recorded_coverage(tmp_path)
    assert table.loc["Binance liquidations", ["files", "days", "first", "last"]].tolist() == [2, 2, "2026-10-01", "2026-10-02"]
    assert table.loc["Deribit BTC option chains", "days"] == 1 and table.loc["Bybit liquidations", "files"] == 0
