"""Portfolio engine (src/portfolio/engine.py): paper execution through the cross-margin sandbox, reconciliation and restarts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.portfolio.backtest import prepare_inputs, run_book
from src.portfolio.book import PortfolioBook
from src.portfolio.config import InstrumentSpec, parse_portfolio_config
from src.portfolio.engine import PortfolioEngine, build_paper_adapters
from src.storage.bar_aggregator import OHLCVBar
from src.storage.trade_logger import TradeLogger

START = datetime(2023, 1, 1, tzinfo=timezone.utc)
DAYS = 200
BTC, ETH = "kraken_futures:BTC/USD", "kraken_futures:ETH/USD"


def _closes(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    returns = np.zeros(DAYS * 6)
    for t in range(1, len(returns)):
        returns[t] = 0.1 * returns[t - 1] + rng.normal(0.0002, 0.012)
    return 100.0 * np.exp(np.cumsum(returns))


SERIES = {"BTC/USD": _closes(1) * 500, "ETH/USD": _closes(2) * 25}


def _bar(symbol: str, stamp: datetime, seconds: int, row: np.ndarray) -> OHLCVBar:
    return OHLCVBar(exchange="mock", symbol=symbol, interval_seconds=seconds, timestamp=stamp, open=float(row[0]), high=float(row.max()) * 1.003,
                    low=float(row.min()) * 0.997, close=float(row[-1]), volume=1.0)


ALL_BARS = {
    (instrument, interval): (
        [_bar(symbol, START + timedelta(hours=4 * i), 14400, np.array([c])) for i, c in enumerate(SERIES[symbol])] if interval == "4h"
        else [_bar(symbol, START + timedelta(days=d), 86400, row) for d, row in enumerate(SERIES[symbol].reshape(DAYS, 6))]
    )
    for instrument, symbol in ((BTC, "BTC/USD"), (ETH, "ETH/USD")) for interval in ("4h", "1d")
}


def bars_until(grid_index: int) -> dict[tuple[str, str], list[OHLCVBar]]:
    """What the runtime would have after the 4h bar `grid_index` closed: daily bars only once their day is over."""
    cutoff = START + timedelta(hours=4 * grid_index)
    return {
        key: [bar for bar in bars if bar.timestamp + timedelta(seconds=bar.interval_seconds) - timedelta(hours=4) <= cutoff]
        for key, bars in ALL_BARS.items()
    }


def _config(**portfolio: Any):
    return parse_portfolio_config({
        "portfolio": {"name": "engine-test", "allocation": "equal", "initial_equity": 10_000, "rebalance_band": 0.02,
                      "allocation_lookback_days": 20, "allocation_refit_days": 5, **portfolio},
        "risk": {"max_drawdown": 0.9, "max_gross_exposure": 3.0, "max_net_exposure": 3.0, "max_instrument_weight": 2.0, "daily_loss_limit": 0.5},
        "instruments": {
            BTC: {"kind": "perp", "max_leverage": 3.0, "lot_step": 0.0001, "min_order_size": 0.0001, "slippage_bps": 2},
            ETH: {"kind": "perp", "max_leverage": 3.0, "lot_step": 0.001, "min_order_size": 0.001, "slippage_bps": 2},
        },
        "sleeves": [
            {"id": "btc_ma_1d", "instrument": BTC, "interval": "1d", "strategy": "moving_average_crossover", "params": {"short_window": 5, "long_window": 20}, "warmup_bars": 25},
            {"id": "eth_keltner_4h", "instrument": ETH, "interval": "4h", "strategy": "keltner_breakout", "params": {"window": 30, "atr_multiplier": 1.0},
             "sizing": "vol_target", "sizing_params": {"target_annual_vol": 0.4}, "warmup_bars": 60},
            {"id": "btc_keltner_4h", "instrument": BTC, "interval": "4h", "strategy": "keltner_breakout", "params": {"window": 40, "atr_multiplier": 1.5}, "warmup_bars": 60},
        ],
    })


class RecordingNotifier:
    def __init__(self) -> None:
        self.trades: list[Any] = []
        self.alerts: list[dict[str, Any]] = []

    def send_trade_alert(self, alert: Any) -> bool:
        self.trades.append(alert)
        return True

    def send_alert(self, **kwargs: Any) -> bool:
        self.alerts.append(kwargs)
        return True


def _engine(config, tmp_path=None, *, logger=None, notifier=None, state=False) -> PortfolioEngine:
    book = PortfolioBook.from_config(config)
    state_dir = tmp_path if state else None
    adapters = build_paper_adapters(config, book, state_dir=state_dir)
    return PortfolioEngine(config, adapters=adapters, book=book, trade_logger=logger, notifier=notifier,
                           state_path=(tmp_path / "engine.json") if state else None)


def _now(grid_index: int) -> datetime:
    return START + timedelta(hours=4 * (grid_index + 1))


FIRST, LAST = 180, 520


def test_a_paper_portfolio_trades_and_the_book_always_matches_the_exchange(tmp_path) -> None:
    config = _config()
    logger = TradeLogger(tmp_path / "trades.db")
    notifier = RecordingNotifier()
    engine = _engine(config, logger=logger, notifier=notifier)
    fills = []
    for index in range(FIRST, LAST):
        report = engine.run_cycle(bars_until(index), now=_now(index))
        assert report.decided and report.rejected == [] and report.mismatches == {}
        fills += report.fills
    adapter = engine.adapters["kraken_futures"]
    assert len(fills) > 10 and {fill["instrument"] for fill in fills} == {BTC, ETH}
    assert float(engine.book.equity()) == pytest.approx(adapter.equity(), rel=1e-9)
    assert adapter.funding_paid_total != 0.0  # funding moved both, identically

    strategy_ids = {row["strategy_id"] for row in logger.list_trades(limit=1000)}
    assert "eth_keltner_4h" in strategy_ids and "portfolio" in strategy_ids  # ETH has one sleeve; BTC's orders net two
    assert len(notifier.trades) == len(fills)
    btc_alert = next(alert for alert in notifier.trades if alert.symbol == BTC)
    assert any(note.startswith("Sleeve btc_ma_1d (moving_average_crossover)") for note in btc_alert.notes)
    assert btc_alert.mode == "paper" and btc_alert.intent in {"open", "increase", "reduce", "close", "flip_close", "flip_open"}

    attribution = engine.book.attribution()
    assert set(attribution) == {"btc_ma_1d", "eth_keltner_4h", "btc_keltner_4h", "residual"}
    assert sum(attribution.values()) == engine.book.equity() - engine.book.initial_equity


def test_the_engine_targets_match_the_research_backtest() -> None:
    config = _config()
    engine = _engine(config)
    targets = {}
    for index in range(FIRST, LAST):
        engine.run_cycle(bars_until(index), now=_now(index))
        targets[START + timedelta(hours=4 * index)] = dict(engine_last_targets(engine))

    def loader(instrument: InstrumentSpec, interval: str) -> list[OHLCVBar]:
        return bars_until(LAST - 1)[(instrument.id, interval)]

    research = run_book(config, prepare_inputs(config, bar_loader=loader), risk_overlay=False).targets
    common = [stamp for stamp in research.index if stamp in targets]
    assert len(common) > 200
    runtime = pd.DataFrame([targets[stamp] for stamp in common], index=common).reindex(columns=research.columns).fillna(0.0)
    np.testing.assert_allclose(runtime.to_numpy(), research.loc[common].to_numpy(), rtol=1e-9, atol=1e-12)


def engine_last_targets(engine: PortfolioEngine) -> dict[str, float]:
    return engine._last_report.targets  # set by the test wrapper below


@pytest.fixture(autouse=True)
def _remember_reports(monkeypatch):
    original = PortfolioEngine.run_cycle

    def run_cycle(self, *args, **kwargs):
        report = original(self, *args, **kwargs)
        self._last_report = report
        return report

    monkeypatch.setattr(PortfolioEngine, "run_cycle", run_cycle)


def test_repeating_a_cycle_decides_nothing_twice() -> None:
    engine = _engine(_config())
    first = engine.run_cycle(bars_until(FIRST), now=_now(FIRST))
    again = engine.run_cycle(bars_until(FIRST), now=_now(FIRST) + timedelta(minutes=5))
    assert first.decided and not again.decided and again.orders == [] and again.fills == []


def test_a_restart_from_the_checkpoints_continues_exactly(tmp_path) -> None:
    config = _config()
    straight = _engine(config)
    straight_fills = [fill for index in range(FIRST, LAST) for fill in straight.run_cycle(bars_until(index), now=_now(index)).fills]

    first = _engine(config, tmp_path, state=True)
    restarted_fills = [fill for index in range(FIRST, 350) for fill in first.run_cycle(bars_until(index), now=_now(index)).fills]
    del first  # "kill" the process; only the files remain
    second = _engine(config, tmp_path, state=True)
    assert second.restored and second.adapters["kraken_futures"].restored_from_state
    restarted_fills += [fill for index in range(350, LAST) for fill in second.run_cycle(bars_until(index), now=_now(index)).fills]

    strip = lambda fills: [{key: value for key, value in fill.items() if key != "order_id"} for fill in fills]  # noqa: E731 - cycle numbers match anyway
    assert strip(restarted_fills) == strip(straight_fills)
    assert second.book.to_dict() == straight.book.to_dict()


def test_a_reconciliation_mismatch_is_reported_and_alerted(tmp_path) -> None:
    config = _config()
    notifier = RecordingNotifier()
    logger = TradeLogger(tmp_path / "trades.db")
    engine = _engine(config, logger=logger, notifier=notifier)
    for index in range(FIRST, 260):
        engine.run_cycle(bars_until(index), now=_now(index))
    engine.adapters["kraken_futures"]._positions["BTC"] = engine.adapters["kraken_futures"]._positions.get("BTC", 0.0) + 0.01
    report = engine.run_cycle(bars_until(260), now=_now(260))
    assert BTC in report.mismatches
    assert notifier.alerts and notifier.alerts[-1]["event_type"] == "portfolio_reconciliation_mismatch"
    assert any(event["event_type"] == "portfolio_reconciliation_mismatch" for event in logger.list_events(limit=50))


def test_the_engine_refuses_to_start_without_an_adapter_per_venue() -> None:
    config = _config()
    with pytest.raises(ValueError, match="no execution adapter for venue"):
        PortfolioEngine(config, adapters={}, book=PortfolioBook.from_config(config))
