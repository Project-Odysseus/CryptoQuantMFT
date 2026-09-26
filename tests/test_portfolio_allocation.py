"""Allocation (src/portfolio/allocation.py) and netting (src/portfolio/netting.py) of sleeve weights."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.portfolio.allocation import allocate_history, sleeve_scales
from src.portfolio.netting import net_history, net_targets

DAYS = pd.date_range("2024-01-01", periods=200, freq="D", tz="UTC")


def test_fixed_and_equal_scales() -> None:
    budgets = {"a": 0.5, "b": 0.3, "c": 0.1}
    assert sleeve_scales(budgets, "fixed") == budgets
    assert sleeve_scales(budgets, "equal") == pytest.approx({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3})
    assert sleeve_scales({}, "equal") == {}
    with pytest.raises(ValueError, match="unknown allocation"):
        sleeve_scales(budgets, "risk_parity")


def test_inverse_vol_gives_calmer_sleeves_more_and_budgets_act_as_relative_risk_weights() -> None:
    scales = sleeve_scales({"calm": 1.0, "wild": 1.0}, "inverse_vol", volatility={"calm": 0.2, "wild": 0.4})
    assert scales == pytest.approx({"calm": 2 / 3, "wild": 1 / 3})
    assert sum(scales.values()) == pytest.approx(1.0)

    weighted = sleeve_scales({"a": 2.0, "b": 1.0}, "inverse_vol", volatility={"a": 0.3, "b": 0.3})
    assert weighted == pytest.approx({"a": 2 / 3, "b": 1 / 3})

    # a sleeve without a usable volatility is treated as average; with none usable, budgets decide alone
    partial = sleeve_scales({"a": 1.0, "b": 1.0, "c": 1.0}, "inverse_vol", volatility={"a": 0.2, "b": 0.4, "c": float("nan")})
    assert partial["c"] == pytest.approx(partial["a"] * 0.2 / 0.3)
    assert sleeve_scales({"a": 3.0, "b": 1.0}, "inverse_vol", volatility={}) == pytest.approx({"a": 0.75, "b": 0.25})


def _returns(seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"calm": rng.normal(0, 0.01, len(DAYS)), "wild": rng.normal(0, 0.04, len(DAYS))}, index=DAYS)


def test_allocate_history_scales_each_sleeve_and_drops_sleeves_without_a_budget() -> None:
    weights = pd.DataFrame({"a": 1.0, "b": -0.5, "off": 1.0}, index=DAYS)
    fixed = allocate_history(weights, {"a": 0.6, "b": 0.4}, "fixed")
    assert list(fixed.columns) == ["a", "b"]
    assert (fixed["a"] == 0.6).all() and (fixed["b"] == -0.2).all()
    equal = allocate_history(weights, {"a": 0.6, "b": 0.4}, "equal")
    assert (equal["a"] == 0.5).all() and (equal["b"] == -0.25).all()


def test_allocate_history_inverse_vol_refits_on_schedule_from_past_returns_only() -> None:
    weights = pd.DataFrame({"calm": 1.0, "wild": 1.0}, index=DAYS)
    returns = _returns()
    scaled = allocate_history(weights, {"calm": 1.0, "wild": 1.0}, "inverse_vol", instrument_returns=returns, lookback=60, refit_every=30)

    assert scaled.iloc[0].tolist() == [0.5, 0.5]  # no history yet: budgets alone
    later = scaled.iloc[90:]
    assert (later["calm"] > 0.7).all() and (later.sum(axis=1).round(12) == 1.0).all()
    for start in range(0, len(DAYS), 30):
        block = scaled.iloc[start : start + 30]
        assert (block.nunique() == 1).all()  # held between refits

    for cut in (45, 100, 150):
        changed = returns.copy()
        changed.iloc[cut + 1 :] *= 10.0
        perturbed = allocate_history(weights, {"calm": 1.0, "wild": 1.0}, "inverse_vol", instrument_returns=changed, lookback=60, refit_every=30)
        pd.testing.assert_frame_equal(perturbed.iloc[: cut + 1], scaled.iloc[: cut + 1])

    with pytest.raises(ValueError, match="instrument_returns"):
        allocate_history(weights, {"calm": 1.0, "wild": 1.0}, "inverse_vol")


def test_net_targets_sums_per_instrument_and_keeps_each_sleeves_share() -> None:
    net, attribution = net_targets({
        "btc_trend": ("kraken_futures:BTC/USD", 0.3),
        "btc_mean_rev": ("kraken_futures:BTC/USD", -0.1),
        "eth_trend": ("kraken_futures:ETH/USD", 0.2),
        "eth_short": ("kraken_futures:ETH/USD", -0.2),
    })
    assert net == pytest.approx({"kraken_futures:BTC/USD": 0.2, "kraken_futures:ETH/USD": 0.0})
    assert attribution["kraken_futures:BTC/USD"] == {"btc_trend": 0.3, "btc_mean_rev": -0.1}
    for instrument, shares in attribution.items():
        assert sum(shares.values()) == pytest.approx(net[instrument])
    assert net_targets({}) == ({}, {})


def test_net_history_sums_sleeve_columns_into_instrument_columns() -> None:
    index = DAYS[:3]
    sleeves = pd.DataFrame({"a": [0.3, 0.0, 0.5], "b": [-0.1, -0.2, 0.0], "c": [0.4, 0.4, 0.4]}, index=index)
    instruments = net_history(sleeves, {"a": "BTC", "b": "BTC", "c": "ETH"})
    assert instruments["BTC"].tolist() == pytest.approx([0.2, -0.2, 0.5])
    assert instruments["ETH"].tolist() == pytest.approx([0.4, 0.4, 0.4])
    assert instruments.sum(axis=1).tolist() == pytest.approx(sleeves.sum(axis=1).tolist())


def test_the_runtime_allocator_matches_allocate_history_bar_by_bar_and_survives_a_restart() -> None:
    import json

    from src.portfolio.allocation import Allocator

    returns = _returns(seed=7)
    returns.iloc[0] = np.nan  # the first bar has no return, as in run_book
    weights = pd.DataFrame(1.0, index=DAYS, columns=["calm", "wild"])
    budgets = {"calm": 1.0, "wild": 2.0}
    for method in ("equal", "fixed", "inverse_vol"):
        expected = allocate_history(weights, budgets, method, instrument_returns=returns, lookback=40, refit_every=15)
        allocator = Allocator(budgets, method, lookback=40, refit_every=15)
        rows = []
        for _, row in returns.iterrows():
            rows.append(allocator.step(row.to_dict()))
            allocator = Allocator.from_dict(json.loads(json.dumps(allocator.to_dict())))
        np.testing.assert_allclose(pd.DataFrame(rows, index=DAYS)[["calm", "wild"]].to_numpy(), expected.to_numpy(), rtol=1e-9)

    with pytest.raises(ValueError, match="unknown allocation"):
        Allocator(budgets, "risk_parity")
