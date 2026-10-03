"""Allocation (src/portfolio/allocation.py) and netting (src/portfolio/netting.py) of sleeve weights."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.portfolio.allocation import Allocator, allocate_history, sleeve_scales
from src.portfolio.netting import net_history, net_targets

DAYS = pd.date_range("2024-01-01", periods=200, freq="D", tz="UTC")


def test_fixed_and_equal_scales() -> None:
    budgets = {"a": 0.5, "b": 0.3, "c": 0.1}
    assert sleeve_scales(budgets, "fixed") == budgets
    assert sleeve_scales(budgets, "equal") == pytest.approx({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3})
    assert sleeve_scales({}, "equal") == {}
    with pytest.raises(ValueError, match="unknown allocation"):
        sleeve_scales(budgets, "black_litterman")
    with pytest.raises(ValueError, match="return history"):
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
    weights["wild"] = np.where(np.arange(len(DAYS)) % 50 < 25, 1.0, -0.5)  # sleeve weights that change, for the covariance methods
    for method in ("equal", "fixed", "inverse_vol", "risk_parity", "hrp"):
        expected = allocate_history(weights, budgets, method, instrument_returns=returns, lookback=40, refit_every=15)
        allocator = Allocator(budgets, method, lookback=40, refit_every=15)
        rows = []
        for stamp, row in returns.iterrows():
            rows.append(allocator.step(row.to_dict(), weights=weights.loc[stamp].to_dict()))
            allocator = Allocator.from_dict(json.loads(json.dumps(allocator.to_dict())))
        scales = pd.DataFrame(rows, index=DAYS)[["calm", "wild"]]
        np.testing.assert_allclose((scales * weights).to_numpy(), expected.to_numpy(), rtol=1e-9)

    with pytest.raises(ValueError, match="unknown allocation"):
        Allocator(budgets, "black_litterman")


def test_risk_parity_equalises_risk_contributions_and_follows_budgets() -> None:
    from src.portfolio.allocation import risk_parity_weights

    # uncorrelated: weights are inversely proportional to volatility
    cov = np.diag([0.1**2, 0.2**2])
    assert risk_parity_weights(cov, np.ones(2)) == pytest.approx([2 / 3, 1 / 3])
    # correlated, three assets: every risk contribution equal
    vols = np.array([0.1, 0.2, 0.4])
    corr = np.array([[1.0, 0.6, 0.2], [0.6, 1.0, 0.4], [0.2, 0.4, 1.0]])
    cov = corr * np.outer(vols, vols)
    w = risk_parity_weights(cov, np.ones(3))
    contributions = w * (cov @ w)
    assert w.sum() == pytest.approx(1.0) and contributions / contributions.sum() == pytest.approx([1 / 3] * 3, abs=1e-8)
    # budgets 2:1:1 give contributions 50/25/25
    w = risk_parity_weights(cov, np.array([2.0, 1.0, 1.0]))
    contributions = w * (cov @ w)
    assert contributions / contributions.sum() == pytest.approx([0.5, 0.25, 0.25], abs=1e-8)


def test_hrp_gives_a_correlated_pair_less_than_independent_sleeves() -> None:
    from src.portfolio.allocation import hrp_weights

    assert hrp_weights(np.eye(4) * 0.04) == pytest.approx([0.25] * 4)  # independent and equally volatile: equal
    corr = np.eye(4)
    corr[0, 1] = corr[1, 0] = 0.9
    w = hrp_weights(corr * 0.04)
    # (the two independent sleeves can differ: single linkage breaks the tie between them one way, a known HRP trait)
    assert w.sum() == pytest.approx(1.0) and w[0] == pytest.approx(w[1])
    assert w[0] < min(w[2], w[3]) and w[0] + w[1] < 0.5  # the pair is close to one bet, so it shares roughly one bet's capital
    assert hrp_weights(np.array([[0.04]])) == pytest.approx([1.0])
    # a more volatile sleeve gets less
    w = hrp_weights(np.diag([0.01, 0.04, 0.04]))
    assert w[0] > w[1] == pytest.approx(w[2])


def test_covariance_scales_measure_risk_while_positioned_and_fall_back_to_budgets() -> None:
    from src.portfolio.allocation import covariance_scales

    rng = np.random.default_rng(1)
    active = rng.normal(0, 0.02, (90, 2))
    flat = np.zeros((90, 1))  # a long-only sleeve that sat out the whole window
    block = np.hstack([active, flat])
    budgets = {"a": 1.0, "b": 1.0, "flat": 1.0}
    for method in ("risk_parity", "hrp"):
        scales = covariance_scales(budgets, method, block, min_periods=30)
        assert sum(scales.values()) == pytest.approx(1.0)
        assert scales["flat"] == pytest.approx(1 / 3, abs=0.08)  # treated as a typical sleeve, not as riskless
    # a sleeve positioned a third of the time is sized by its risk while positioned, not diluted by the flat bars
    part_time = np.where(np.arange(90)[:, None] % 3 == 0, rng.normal(0, 0.02, (90, 1)), 0.0)
    scales = covariance_scales({"a": 1.0, "b": 1.0, "part": 1.0}, "risk_parity", np.hstack([active, part_time]), min_periods=30)
    assert scales["part"] < 0.45
    assert covariance_scales({"a": 3.0, "b": 1.0}, "risk_parity", active[:5], min_periods=30) == pytest.approx({"a": 0.75, "b": 0.25})
    assert covariance_scales({"a": 1.0, "b": 1.0}, "hrp", np.zeros((50, 2)), min_periods=30) == pytest.approx({"a": 0.5, "b": 0.5})


def test_covariance_allocations_use_only_past_sleeve_returns() -> None:
    weights = pd.DataFrame({"calm": 1.0, "wild": 1.0}, index=DAYS)
    returns = _returns(seed=2)
    for method in ("risk_parity", "hrp"):
        scaled = allocate_history(weights, {"calm": 1.0, "wild": 1.0}, method, instrument_returns=returns, lookback=60, refit_every=30)
        assert (scaled.iloc[90:]["calm"] > scaled.iloc[90:]["wild"]).all()  # the calmer sleeve gets more
        for cut in (45, 100, 150):
            changed = returns.copy()
            changed.iloc[cut + 1 :] *= 10.0
            perturbed = allocate_history(weights, {"calm": 1.0, "wild": 1.0}, method, instrument_returns=changed, lookback=60, refit_every=30)
            pd.testing.assert_frame_equal(perturbed.iloc[: cut + 1], scaled.iloc[: cut + 1])
        with pytest.raises(ValueError, match="instrument_returns"):
            allocate_history(weights, {"calm": 1.0, "wild": 1.0}, method)


@pytest.mark.parametrize("method", ["risk_parity", "hrp"])
def test_a_basket_is_one_unit_measured_by_its_own_return(method: str) -> None:
    """A market-neutral basket earns little from the market move its coins share, so it gets more than a trend sleeve on one of them."""
    import json

    rng = np.random.default_rng(8)
    bars = 400
    index = pd.date_range("2024-01-01", periods=bars, freq="4h", tz="UTC")
    market = rng.normal(0.0, 0.01, bars)
    returns = pd.DataFrame({"trend": market, "b__x": market + rng.normal(0.0, 0.002, bars), "b__y": market + rng.normal(0.0, 0.002, bars)}, index=index)
    weights = pd.DataFrame({"trend": 1.0, "b__x": 0.5, "b__y": -0.5}, index=index)  # the basket is long x, short y
    groups = {"trend": "trend", "b__x": "b", "b__y": "b"}
    budgets = {"trend": 1.0, "b__x": 1.0, "b__y": 1.0}
    allocated = allocate_history(weights, budgets, method, instrument_returns=returns, lookback=120, refit_every=30, groups=groups)
    scales = allocated / weights
    assert np.allclose(scales["b__x"], scales["b__y"])  # members share their unit's scale
    assert np.allclose(scales["trend"] + scales["b__x"], 1.0)  # two units, not three sleeves
    assert scales["b__x"].iloc[-1] > 3 * scales["trend"].iloc[-1]  # the hedged basket is far calmer than the trend sleeve
    assert np.allclose(scales.iloc[0], 0.5)  # no history yet: the budgets alone

    allocator = Allocator({"trend": 1.0, "b": 1.0}, method, lookback=120, refit_every=30, groups=groups)
    for position, stamp in enumerate(index):
        row = allocator.step(returns.loc[stamp].to_dict() if position else None, weights=weights.loc[stamp].to_dict())
        assert row["trend"] == pytest.approx(scales["trend"].iloc[position], rel=1e-9) and row["b"] == pytest.approx(scales["b__x"].iloc[position], rel=1e-9)
        if position % 97 == 0:
            allocator = Allocator.from_dict(json.loads(json.dumps(allocator.to_dict())))
    with pytest.raises(ValueError, match="takes no groups"):
        allocate_history(weights, budgets, "inverse_vol", instrument_returns=returns, groups=groups)
