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


def _book_snapshots(days: int, sleeve_daily: dict[str, float], seed: int = 5) -> list[dict]:
    """Daily snapshots of a book whose sleeves earn a mean daily return (as a share of equity) with 1% noise."""
    rng = np.random.default_rng(seed)
    equity, pnl = 1000.0, dict.fromkeys(sleeve_daily, 0.0)
    out = []
    for day in range(days):
        moves = {sid: equity * (mean + rng.normal(0, 0.01)) for sid, mean in sleeve_daily.items()}
        for sid, move in moves.items():
            pnl[sid] += move
        equity += sum(moves.values())
        out.append({"timestamp": (pd.Timestamp("2026-10-01", tz="UTC") + pd.Timedelta(days=day)).isoformat(), "portfolio": "btc-live", "equity": equity,
                    "instruments": {"kraken_futures:BTC/USD": {"price": 60_000.0 + day}}, "sleeves": {sid: {"pnl": value} for sid, value in pnl.items()}})
    return out


def test_kill_criteria_flag_decay_by_standard_errors_not_by_sign() -> None:
    from src.portfolio.review import ReviewConfig, kill_criteria

    review = ReviewConfig(backtest_sharpe={"good": 1.0, "decayed": 1.5}, book_backtest_sharpe=1.0, book_backtest_max_drawdown=0.5)
    table = kill_criteria(_book_snapshots(400, {"good": 0.0006, "decayed": -0.0015}), review)
    assert table.loc["good", "status"] == "ok"
    assert table.loc["decayed", "status"].startswith("FLAG: decay") and "negative Sharpe" in table.loc["decayed", "status"]
    early = kill_criteria(_book_snapshots(40, {"good": 0.0006, "decayed": -0.0015}), review)
    assert early.loc["decayed", "status"].startswith("too early")


def test_a_slightly_negative_half_year_is_not_a_flag_but_a_deep_drawdown_is() -> None:
    from src.portfolio.review import ReviewConfig, kill_criteria, sharpe_standard_error

    assert sharpe_standard_error(0.7, 180) == pytest.approx(1.42, abs=0.02)  # why a raw sign rule would be noise
    review = ReviewConfig(backtest_sharpe={"a": 0.7}, book_backtest_sharpe=0.7, book_backtest_max_drawdown=0.05)
    table = kill_criteria(_book_snapshots(180, {"a": 0.0}, seed=2), review)  # no edge at all: a -0.27 Sharpe half-year
    assert table.loc["a", "live_sharpe"] < 0 and not table.loc["a", "status"].startswith("FLAG: decay")
    assert "drawdown" in table.loc["book", "status"]  # 1% daily noise easily exceeds 1.5 x 5%


def test_the_review_table_is_parsed_and_checked() -> None:
    from src.portfolio.config import PortfolioConfigError, load_portfolio_config, parse_portfolio_config

    live = load_portfolio_config("config/portfolio.btc_live.toml")
    assert live.review is not None and set(live.review.backtest_sharpe) == {sleeve.id for sleeve in live.sleeves}
    raw = {"portfolio": {"name": "x"}, "instruments": {"kraken_futures:BTC/USD": {"kind": "perp"}},
           "sleeves": [{"id": "a", "instrument": "kraken_futures:BTC/USD", "interval": "1d", "strategy": "moving_average_crossover"}],
           "review": {"backtest_sharpe": {"b": 1.0}, "min_days": 5}}
    with pytest.raises(PortfolioConfigError) as error:
        parse_portfolio_config(raw)
    assert "'b', which is not a sleeve" in str(error.value) and "min_days" in str(error.value)
