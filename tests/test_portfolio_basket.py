"""Basket sleeves (src/portfolio/basket.py): the cross-sectional weights, their schedule, the config, and runtime parity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.portfolio.backtest import prepare_inputs, run_book
from src.portfolio.basket import BasketSpec, CachedPanelSource, basket_weights, fetch_binance_panel, is_rebalance_day, weights_at
from src.portfolio.book import PortfolioBook
from src.portfolio.config import PortfolioConfigError, parse_portfolio_config
from src.portfolio.engine import PortfolioEngine, build_paper_adapters
from src.storage.bar_aggregator import OHLCVBar

START = datetime(2023, 1, 1, tzinfo=timezone.utc)
DAYS = 260
COINS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH")
BTC = "kraken_futures:BTC/USD"


def _panel(seed: int = 0) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    days = pd.date_range(START, periods=DAYS, freq="D", tz="UTC")
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.03, (DAYS, len(COINS))), axis=0)), index=days, columns=list(COINS))
    volume = pd.DataFrame(rng.uniform(1e6, 5e6, (DAYS, len(COINS))) * np.arange(1, len(COINS) + 1), index=days, columns=list(COINS))
    share = pd.DataFrame(np.clip(0.5 + rng.normal(0, 0.05, (DAYS, len(COINS))), 0.1, 0.9), index=days, columns=list(COINS))
    return {"close": close, "quote_volume": volume, "taker_buy_quote_volume": volume * share}


PANEL = _panel()
SPEC = BasketSpec(id="taker", coins=COINS, top_n=8, quantile=0.25, min_names=6, rebalance_days=5)


def test_weights_are_balanced_legs_that_change_only_on_rebalance_days() -> None:
    weights = basket_weights(SPEC, PANEL)
    active = weights[weights.abs().sum(axis=1) > 0]
    assert len(active) > 100
    assert np.allclose(active.clip(lower=0).sum(axis=1), 0.5) and np.allclose(active.clip(upper=0).sum(axis=1), -0.5)
    assert ((active > 0).sum(axis=1) == 2).all() and ((active < 0).sum(axis=1) == 2).all()
    changes = weights.diff().abs().sum(axis=1) > 0
    assert all(is_rebalance_day(stamp, SPEC.rebalance_days) for stamp in weights.index[changes.to_numpy()][1:])
    long_only = basket_weights(BasketSpec(id="lo", coins=COINS, top_n=8, quantile=0.25, min_names=6, long_only=True), PANEL)
    assert (long_only >= 0).all().all() and np.allclose(long_only[long_only.sum(axis=1) > 0].sum(axis=1), 1.0)


def test_the_weights_at_a_close_use_only_days_closed_by_then() -> None:
    full = basket_weights(SPEC, PANEL)
    for decision in [stamp for stamp in full.index[100:] if is_rebalance_day(stamp, SPEC.rebalance_days)][:12]:
        truncated = {name: frame.iloc[max(0, frame.index.get_indexer([decision - timedelta(days=1)])[0] - 119):] for name, frame in PANEL.items()}
        assert weights_at(SPEC, truncated, decision) == pytest.approx(full.loc[decision].to_dict())  # 120 days of history suffice
        spoiled = {name: frame.copy() for name, frame in PANEL.items()}
        for frame in spoiled.values():
            frame.loc[frame.index >= decision] *= 7.0  # tomorrow's data must not matter
        assert weights_at(SPEC, spoiled, decision) == pytest.approx(full.loc[decision].to_dict())
    assert weights_at(SPEC, PANEL, next(stamp for stamp in full.index if not is_rebalance_day(stamp, SPEC.rebalance_days))) is None


def test_untradable_coins_leave_the_universe() -> None:
    tradable = pd.DataFrame(True, index=PANEL["close"].index, columns=list(COINS))
    tradable["HHH"] = False  # e.g. not listed on the venue yet
    weights = basket_weights(SPEC, PANEL, tradable=tradable)
    assert (weights["HHH"] == 0).all() and np.allclose(weights[weights.abs().sum(axis=1) > 0].clip(lower=0).sum(axis=1), 0.5)


def test_the_binance_panel_keeps_completed_candles_only_and_is_fetched_once_a_day() -> None:
    day = datetime(2026, 9, 28, tzinfo=timezone.utc)
    ms = lambda d: int(d.timestamp() * 1000)  # noqa: E731

    def fetch(symbol: str, days: int) -> list[list[Any]]:
        if symbol == "BADUSDT":
            raise TimeoutError("down")
        return [[ms(day - timedelta(days=2)), "1", "1", "1", "10", "5", 0, "1000", 3, "2", "400", "0"],
                [ms(day - timedelta(days=1)), "1", "1", "1", "11", "5", 0, "2000", 3, "2", "900", "0"],
                [ms(day), "1", "1", "1", "12", "5", 0, "50", 3, "2", "20", "0"]]  # today's candle is still open

    panel = fetch_binance_panel(["AAA", "BAD"], now=day + timedelta(hours=3), fetch=fetch)
    assert list(panel["close"].columns) == ["AAA"] and panel["close"]["AAA"].tolist() == [10.0, 11.0]
    assert panel["taker_buy_quote_volume"]["AAA"].tolist() == [400.0, 900.0]
    calls: list[int] = []
    source = CachedPanelSource(fetch=lambda coins, days, now: calls.append(1) or PANEL)
    source(SPEC, day)
    source(SPEC, day + timedelta(hours=5))
    source(SPEC, day + timedelta(days=1))
    assert len(calls) == 2


def _raw_config(**basket: Any) -> dict[str, Any]:
    instruments = {BTC: {"kind": "perp", "max_leverage": 3.0, "lot_step": 0.0001, "min_order_size": 0.0001, "slippage_bps": 2}}
    for coin in COINS:
        instruments[f"kraken_futures:{coin}/USD"] = {"kind": "perp", "max_leverage": 3.0, "lot_step": 0.01, "min_order_size": 0.01, "slippage_bps": 10}
    return {
        "portfolio": {"name": "basket-test", "allocation": "equal", "initial_equity": 100_000, "rebalance_band": 0.0},
        "risk": {"max_drawdown": 0.9, "max_gross_exposure": 5.0, "max_net_exposure": 5.0, "max_instrument_weight": 2.0, "daily_loss_limit": 0.9},
        "instruments": instruments,
        "sleeves": [{"id": "btc_ma", "instrument": BTC, "interval": "1d", "strategy": "moving_average_crossover", "params": {"short_window": 5, "long_window": 20},
                     "warmup_bars": 25}],
        "baskets": [{"id": "taker", "coins": list(COINS), "top_n": 8, "quantile": 0.25, "min_names": 6, "rebalance_days": 5, **basket}],
    }


def test_the_config_expands_a_basket_into_member_sleeves_and_checks_it() -> None:
    config = parse_portfolio_config(_raw_config())
    members = [sleeve for sleeve in config.sleeves if sleeve.basket == "taker"]
    assert [sleeve.id for sleeve in members] == [f"taker__{coin.lower()}" for coin in COINS]
    assert config.group_budgets() == {"btc_ma": 1.0, "taker": 1.0}
    from src.portfolio.config import describe

    assert "basket of 8 kraken_futures perps" in describe(config) and "taker__aaa" not in describe(config)
    bad = _raw_config(coins=list(COINS) + ["ZZZ"], quantile=0.9)
    with pytest.raises(PortfolioConfigError, match="quantile"):
        parse_portfolio_config(bad)
    bad = _raw_config(coins=list(COINS) + ["ZZZ"])
    with pytest.raises(PortfolioConfigError, match="ZZZ needs a perp"):
        parse_portfolio_config(bad)
    bad = _raw_config()
    bad["portfolio"]["allocation"] = "risk_parity"
    with pytest.raises(PortfolioConfigError, match="'fixed' or 'equal' in a portfolio with baskets"):
        parse_portfolio_config(bad)


def _bars(instrument: str, seed: int) -> dict[str, list[OHLCVBar]]:
    if instrument == BTC:
        rng = np.random.default_rng(seed)
        closes = 30_000 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, DAYS * 6)))
    else:
        daily = PANEL["close"][instrument.split(":")[1].split("/")[0]].to_numpy()
        closes = np.repeat(daily, 6) * (1 + np.tile(np.linspace(-0.002, 0.0, 6), DAYS))  # each day's last 4h close is the daily close
    four = [OHLCVBar(exchange="mock", symbol=instrument, interval_seconds=14400, timestamp=START + timedelta(hours=4 * i), open=c, high=c * 1.002, low=c * 0.998, close=c, volume=1.0)
            for i, c in enumerate(closes)]
    days = [OHLCVBar(exchange="mock", symbol=instrument, interval_seconds=86400, timestamp=START + timedelta(days=d), open=row[0], high=row.max() * 1.002, low=row.min() * 0.998,
                     close=row[-1], volume=1.0) for d, row in enumerate(closes.reshape(DAYS, 6))]
    return {"4h": four, "1d": days}


ALL = {instrument: _bars(instrument, seed) for seed, instrument in enumerate([BTC] + [f"kraken_futures:{coin}/USD" for coin in COINS])}


def bars_until(grid_index: int) -> dict[tuple[str, str], list[OHLCVBar]]:
    cutoff = START + timedelta(hours=4 * grid_index)
    return {(instrument, interval): [bar for bar in bars if bar.timestamp + timedelta(seconds=bar.interval_seconds) - timedelta(hours=4) <= cutoff]
            for instrument, by_interval in ALL.items() for interval, bars in by_interval.items()}


def _source(spec: BasketSpec, now: datetime) -> dict[str, pd.DataFrame]:
    return {name: frame[frame.index + timedelta(days=1) <= now] for name, frame in PANEL.items()}


def test_the_engine_trades_the_basket_exactly_as_the_research_backtest_through_a_restart(tmp_path) -> None:
    config = parse_portfolio_config({**_raw_config(), "sleeves": _raw_config()["sleeves"] + [
        {"id": "btc_keltner_4h", "instrument": BTC, "interval": "4h", "strategy": "keltner_breakout", "params": {"window": 30, "atr_multiplier": 1.0}, "warmup_bars": 60}]})
    first_index, last_index = 6 * 110, 6 * 250

    def engine() -> PortfolioEngine:
        book = PortfolioBook.from_config(config)
        return PortfolioEngine(config, adapters=build_paper_adapters(config, book, state_dir=tmp_path), book=book, state_path=tmp_path / "engine.json",
                               basket_source=_source)

    running = engine()
    targets: dict[datetime, dict[str, float]] = {}
    for index in range(first_index, last_index):
        if index == 6 * 180:
            running = engine()  # a restart halfway
            assert running.restored and running.basket_last_decision
        report = running.run_cycle(bars_until(index), now=START + timedelta(hours=4 * (index + 1)))
        if report.decided:
            targets[START + timedelta(hours=4 * index)] = dict(report.targets)

    def loader(instrument: Any, interval: str) -> list[OHLCVBar]:
        return bars_until(last_index - 1)[(instrument.id, interval)]

    research = run_book(config, prepare_inputs(config, bar_loader=loader, basket_panel=lambda spec: PANEL), risk_overlay=False).targets
    common = [stamp for stamp in research.index if stamp in targets]
    assert len(common) > 500
    runtime = pd.DataFrame([targets[stamp] for stamp in common], index=common).reindex(columns=research.columns).fillna(0.0)
    np.testing.assert_allclose(runtime.to_numpy(), research.loc[common].to_numpy(), rtol=1e-9, atol=1e-12)
    coins = [column for column in research.columns if column != BTC]
    assert (research.loc[common, coins].abs().sum(axis=1) > 0).mean() > 0.9  # the basket really traded
    assert (research.loc[common, coins].abs() > 0).sum(axis=1).max() == 4  # two long, two short
