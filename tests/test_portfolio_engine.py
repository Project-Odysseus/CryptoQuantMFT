"""Portfolio engine (src/portfolio/engine.py): paper execution through the cross-margin sandbox, reconciliation and restarts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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


def _config(execution: dict[str, Any] | None = None, contract_sleeves: list[dict[str, Any]] | None = None, **portfolio: Any):
    return parse_portfolio_config({
        **({"execution": execution} if execution else {}),
        **({"contract_sleeves": contract_sleeves} if contract_sleeves else {}),
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


@pytest.mark.parametrize("method", ["equal", "inverse_vol", "risk_parity", "hrp"])
def test_the_engine_targets_match_the_research_backtest(method: str) -> None:
    """Under every allocation that adapts: the engine replays its candle history at a cold start, in the backtest's order, and refits on the same calendar bars."""
    config = _config(allocation=method)
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


class FixedRates:
    """Norges Bank stand-in: fixed daily rates, or down when `fail` is set."""

    def __init__(self) -> None:
        self.fail = False

    def get_rate(self, pair: str = "EUR/NOK", at: Any = None) -> float:
        if self.fail:
            raise ConnectionError("Norges Bank unreachable")
        return {"EUR/NOK": 11.5, "USD/NOK": 10.5}[pair]


def test_live_tax_records_every_realized_pnl_fee_and_funding_flow_and_retries_failures(tmp_path) -> None:
    config = _config()
    logger = TradeLogger(tmp_path / "trades.db")
    logger.fx_rate_collector = FixedRates()
    engine = _engine(config, logger=logger)
    engine.record_tax = True
    for index in range(FIRST, 300):
        engine.run_cycle(bars_until(index), now=_now(index))
    logger.fx_rate_collector.fail = True  # the ledger can't value flows for a while
    for index in range(300, 330):
        engine.run_cycle(bars_until(index), now=_now(index))
    queued = len(engine.pending_tax)
    assert queued > 0 and any(event["event_type"] == "portfolio_tax_record_failed" for event in logger.list_events(limit=500))
    logger.fx_rate_collector.fail = False
    for index in range(330, LAST):
        engine.run_cycle(bars_until(index), now=_now(index))
    assert engine.pending_tax == []

    events = [event for event in logger.list_tax_events() if event["metadata"].get("portfolio") == "engine-test"]
    totals: dict[str, float] = {}
    for event in events:
        totals[event["transaction_type"]] = totals.get(event["transaction_type"], 0.0) + event["metadata"]["amount"]
    realized = sum(float(position.realized_pnl) for position in engine.book.positions.values())
    fees = sum(float(position.fees) for position in engine.book.positions.values())
    funding = sum(float(position.funding) for position in engine.book.positions.values())
    assert totals["REALIZED_PNL"] == pytest.approx(realized) and totals["TRADING_FEE"] == pytest.approx(-fees) and totals["FUNDING_FEE"] == pytest.approx(-funding)
    assert {event["symbol"] for event in events} == {"PF_XBTUSD", "PF_ETHUSD"}
    assert all(event["amount_nok"] == pytest.approx(event["metadata"]["amount"] * 10.5) for event in events)


def test_paper_runs_never_write_the_tax_ledger(tmp_path) -> None:
    logger = TradeLogger(tmp_path / "trades.db")
    engine = _engine(_config(), logger=logger)
    for index in range(FIRST, 260):
        engine.run_cycle(bars_until(index), now=_now(index))
    assert logger.list_tax_events() == [] and engine.pending_tax == []


def test_repeated_rejections_pause_new_risk_on_that_instrument_but_not_reductions(tmp_path) -> None:
    """After 3 rejections in a row on BTC, only BTC reductions are sent for the cooldown; ETH is untouched; restarts keep it."""
    import inspect

    from src.execution.adapters import ExecutionReport

    config = _config()
    engine = _engine(config, tmp_path, state=True)
    adapter = engine.adapters["kraken_futures"]
    real_submit = adapter.submit_order
    reject_btc = {"on": True}
    sent: list[tuple[str, bool]] = []

    def flaky_submit(**kwargs: Any) -> ExecutionReport:
        symbol, reduce_only = kwargs.get("symbol"), bool(kwargs.get("reduce_only"))
        if symbol == "BTC/USD":
            sent.append((kwargs["side"], reduce_only))
            if reject_btc["on"] and not reduce_only:
                return ExecutionReport(order_id=kwargs["order_id"], status="REJECTED", message="insufficientAvailableFunds")
        return real_submit(**kwargs)

    flaky_submit.__signature__ = inspect.signature(real_submit)  # the engine checks it accepts reduce_only
    adapter.submit_order = flaky_submit

    rejections, cooled_at, skipped_for_cooldown = 0, None, 0
    for index in range(FIRST, LAST):
        until = engine.cooldown_until.get(BTC)
        cooling = until is not None and datetime.fromisoformat(until) > _now(index)
        before = len(sent)
        report = engine.run_cycle(bars_until(index), now=_now(index))
        if cooling:
            assert all(reduce_only for _, reduce_only in sent[before:])  # nothing that adds BTC risk while cooling
        rejections += len([r for r in report.rejected if r["instrument"] == BTC])
        skipped_for_cooldown += sum(1 for skip in report.skipped if skip.reason == "rejection_cooldown")
        assert all(r["instrument"] == BTC for r in report.rejected)  # ETH trades normally
        if report.cooldowns and cooled_at is None:
            cooled_at = index
            assert BTC in report.cooldowns and rejections == 3
            # a restart mid-cooldown keeps it
            engine = PortfolioEngine(config, adapters=engine.adapters, book=engine.book, state_path=tmp_path / "engine.json")
            assert engine.cooldown_until.keys() == {BTC} and engine.rejection_streak[BTC] == 3
        if cooled_at is not None and index > cooled_at + 40:
            break
    assert cooled_at is not None and skipped_for_cooldown > 0
    # after the first cooldown, at most one probe per 24h (6 bars) reaches the exchange and is rejected
    assert rejections <= 3 + (index - cooled_at) // 6 + 1

    reject_btc["on"] = False  # the exchange accepts again: after the cooldown BTC trades, and a fill resets the streak
    for step in range(index + 1, min(index + 60, LAST)):
        report = engine.run_cycle(bars_until(step), now=_now(step))
        if any(fill["instrument"] == BTC for fill in report.fills) and not report.cooldowns:
            break
    assert BTC not in engine.rejection_streak


def test_the_cooldown_settings_are_validated() -> None:
    from src.portfolio.risk import PortfolioRiskConfig

    assert PortfolioRiskConfig(rejection_cooldown_after=0).rejection_cooldown_after == 0  # off
    with pytest.raises(ValueError, match="rejection_cooldown"):
        PortfolioRiskConfig(rejection_cooldown_hours=0)


# --- the risk model in the engine ---------------------------------------------------------------------------------

def _risk_config(**risk: Any):
    config = _config()
    from dataclasses import replace

    from src.portfolio.config import InstrumentSpec as Spec
    from src.portfolio.risk import PortfolioRiskConfig

    limits = {field: getattr(config.risk, field) for field in ("max_drawdown", "max_gross_exposure", "max_net_exposure", "max_instrument_weight", "daily_loss_limit")}
    instruments = {BTC: replace(config.instruments[BTC], group="majors"), ETH: replace(config.instruments[ETH], group="alts")}
    assert all(isinstance(spec, Spec) for spec in instruments.values())
    return replace(config, instruments=instruments, risk=PortfolioRiskConfig(**limits, vol_min_days=5, **risk))


def test_the_engines_risk_model_matches_the_research_stream_bar_for_bar() -> None:
    from src.portfolio.book_risk import estimate_stream

    config = _risk_config()
    engine = _engine(config)
    seen = {}
    for index in range(FIRST, FIRST + 60):
        engine.run_cycle(bars_until(index), now=_now(index))
        seen[START + timedelta(hours=4 * index)] = engine.risk_model.estimate().covariance.copy()

    def loader(instrument: InstrumentSpec, interval: str) -> list[OHLCVBar]:
        return bars_until(FIRST + 59)[(instrument.id, interval)]

    inputs = prepare_inputs(config, bar_loader=loader)
    stream = estimate_stream(config, inputs.grid_interval, inputs.prices, warmup_prices=inputs.warmup_prices)
    checked = 0
    for position, stamp in enumerate(inputs.prices.index):
        estimate = stream(position)
        if stamp in seen:
            np.testing.assert_allclose(estimate.covariance, seen[stamp], rtol=1e-9)
            checked += 1
    assert checked >= 40


def test_the_volatility_cap_scales_the_engines_targets_and_the_snapshot_reports_the_exposure() -> None:
    free, capped = _engine(_risk_config()), _engine(_risk_config(max_portfolio_vol=0.10, groups={"alts": {"max_gross": 0.2}}))
    acted = 0
    for index in range(FIRST, FIRST + 120):
        loose, tight = free.run_cycle(bars_until(index), now=_now(index)), capped.run_cycle(bars_until(index), now=_now(index))
        assert tight.targets == pytest.approx(loose.targets)  # the sleeves ask for the same; only the overlay differs
        estimate = capped.risk_model.estimate()
        assert estimate.stressed_volatility(tight.adjusted) <= 0.10 + 1e-9 and abs(tight.adjusted.get(ETH, 0.0)) <= 0.2 + 1e-9
        acted += any(action.rule == "vol_cap" for action in tight.risk_actions)
    assert acted > 20
    snap = capped.snapshot(tight)
    exposure = snap["exposure"]
    assert exposure["gross"] == pytest.approx(snap["gross"]) and exposure["net"] == pytest.approx(snap["net"])
    assert set(exposure["groups"]) <= {"majors", "alts"} and exposure["benchmark"] == BTC
    assert exposure["stressed_volatility"] is not None and exposure["stressed_volatility"] < 0.2  # lots and drift leave it near the 10% cap
    assert snap["limits"]["max_portfolio_vol"] == 0.10 and snap["limits"]["groups"] == {"alts": {"max_gross": 0.2}}
    assert snap["strategy_correlation"] is None or snap["strategy_correlation"]["units"] >= 2
    import json

    json.dumps(snap)  # the snapshot is stored as JSON


def test_a_restart_keeps_the_risk_model_and_the_strategy_returns(tmp_path) -> None:
    config = _risk_config(max_portfolio_vol=0.15)
    straight = _engine(config)
    for index in range(FIRST, FIRST + 100):
        straight.run_cycle(bars_until(index), now=_now(index))
    first = _engine(config, tmp_path, state=True)
    for index in range(FIRST, FIRST + 50):
        first.run_cycle(bars_until(index), now=_now(index))
    del first
    second = _engine(config, tmp_path, state=True)
    assert second.restored and second.risk_model.bars_seen == straight.risk_model.bars_seen - 50
    for index in range(FIRST + 50, FIRST + 100):
        second.run_cycle(bars_until(index), now=_now(index))
    np.testing.assert_array_equal(second.risk_model.estimate().covariance, straight.risk_model.estimate().covariance)
    assert second.unit_returns.rows == straight.unit_returns.rows and second.held_weights == straight.held_weights
    assert second.book.to_dict() == straight.book.to_dict()
    assert straight.unit_returns.summary()["units"] == 3  # the three sleeves have all traded


def test_a_checkpoint_from_before_the_risk_model_still_loads(tmp_path) -> None:
    import json

    config = _risk_config()
    first = _engine(config, tmp_path, state=True)
    for index in range(FIRST, FIRST + 5):
        first.run_cycle(bars_until(index), now=_now(index))
    path = tmp_path / "engine.json"
    payload = json.loads(path.read_text())
    for key in ("risk_model", "unit_returns", "held_weights"):
        payload.pop(key)
    path.write_text(json.dumps(payload))
    second = _engine(config, tmp_path, state=True)
    assert second.restored and second.risk_model.bars_seen == 0 and second.held_weights == {}
    assert second.run_cycle(bars_until(FIRST + 5), now=_now(FIRST + 5)).decided


def test_the_held_book_is_checked_against_the_exposure_limits() -> None:
    from dataclasses import replace

    engine = _engine(_risk_config())
    for index in range(FIRST, FIRST + 80):
        report = engine.run_cycle(bars_until(index), now=_now(index))
        if engine.book.units():
            break
    assert engine.book.units() and engine.exposure_breaches(report.equity) == []  # no [risk.exposure] limits
    engine.config = replace(engine.config, risk=replace(engine.config.risk, exposure={"max_delta": 0.001, "max_scenario_loss": 0.0001}))
    breaches = engine.exposure_breaches(report.equity)
    assert {breach["rule"] for breach in breaches} == {"max_delta", "max_scenario_loss"}
    assert all(breach["value"] > breach["limit"] for breach in breaches) and engine.snapshot(report)["exposure_breaches"] == breaches


# --- crashes inside a decision ------------------------------------------------------------------------------------

class _Crash(BaseException):
    """The process dies here: nothing after this line runs, and nothing is saved."""


def _fills_of(logger: TradeLogger) -> list[tuple]:
    return [(trade["pair"], trade["side"], trade["size"], trade["price"]) for trade in reversed(logger.list_trades())]


def _run_with_a_crash(tmp_path, arm, *, bars: int = 150) -> tuple[PortfolioEngine, TradeLogger, PortfolioEngine, TradeLogger]:
    """A clean run, and a run that dies where `arm(engine)` says, restarts on the same files and carries on."""
    config = _config()
    (tmp_path / "clean").mkdir()
    (tmp_path / "crashed").mkdir()
    clean_logger = TradeLogger(tmp_path / "clean" / "trades.db")
    clean = _engine(config, tmp_path / "clean", logger=clean_logger, state=True)
    for index in range(FIRST, FIRST + bars):
        clean.run_cycle(bars_until(index), now=_now(index))

    logger = TradeLogger(tmp_path / "crashed" / "trades.db")
    engine = _engine(config, tmp_path / "crashed", logger=logger, state=True)
    arm(engine)
    index = FIRST
    with pytest.raises(_Crash):
        while True:
            engine.run_cycle(bars_until(index), now=_now(index))
            index += 1
    del engine
    restarted = _engine(config, tmp_path / "crashed", logger=logger, state=True)
    resumed_at = int((restarted.last_grid_bar - START).total_seconds() // 14400)
    restarted.run_cycle(bars_until(resumed_at), now=_now(resumed_at))  # the first poll after the restart: no new bar yet
    assert restarted.pending_orders == {} and restarted.open_plan == [] and restarted.reconcile() == {}
    for later in range(resumed_at + 1, FIRST + bars):
        assert restarted.run_cycle(bars_until(later), now=_now(later)).mismatches == {}
    return clean, clean_logger, restarted, logger


def _die_after(engine: PortfolioEngine, name: str, nth: int, *, when=lambda engine: True) -> None:
    original, calls = getattr(engine, name), {"n": 0}

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if when(engine):
            calls["n"] += 1
            if calls["n"] == nth:
                raise _Crash()
        return result

    setattr(engine, name, wrapped)


@pytest.mark.parametrize("nth", [1, 2, 5])
def test_a_crash_after_the_exchange_filled_an_order_books_it_once_and_sends_nothing_twice(tmp_path, nth: int) -> None:
    def arm(engine: PortfolioEngine) -> None:
        adapter = engine.adapters["kraken_futures"]
        _die_after(adapter, "submit_order", nth)  # the paper exchange has the fill; the engine never saw the answer

    clean, clean_logger, restarted, logger = _run_with_a_crash(tmp_path, arm)
    assert restarted.book.to_dict() == clean.book.to_dict()
    assert _fills_of(logger) == _fills_of(clean_logger) and len(_fills_of(logger)) >= nth
    assert {trade["source"] for trade in logger.list_trades()} == {"portfolio_paper"}  # tagged with the mode, never as live
    assert restarted.adapters["kraken_futures"].positions() == clean.adapters["kraken_futures"].positions()


def test_a_crash_between_recording_an_order_and_sending_it_sends_it_after_the_restart(tmp_path) -> None:
    clean, clean_logger, restarted, logger = _run_with_a_crash(tmp_path, lambda engine: _die_after(engine, "_save", 2, when=lambda e: bool(e.pending_orders)))
    assert restarted.book.to_dict() == clean.book.to_dict() and _fills_of(logger) == _fills_of(clean_logger)
    assert [event["event_type"] for event in logger.list_events(event_types=["portfolio_plan_resumed"])] == ["portfolio_plan_resumed"]


def test_a_crash_after_a_fill_was_logged_does_not_log_it_again(tmp_path) -> None:
    clean, clean_logger, restarted, logger = _run_with_a_crash(tmp_path, lambda engine: _die_after(engine, "_book_fill", 3))
    assert restarted.book.to_dict() == clean.book.to_dict() and _fills_of(logger) == _fills_of(clean_logger)
    recovered = logger.list_events(event_types=["portfolio_fill_recovered"])
    assert len(recovered) == 1 and "logged before the crash" in recovered[0]["message"]


def test_an_interrupted_plan_is_dropped_when_a_new_bar_closed_before_the_restart(tmp_path) -> None:
    config = _config()
    logger = TradeLogger(tmp_path / "trades.db")
    engine = _engine(config, tmp_path, logger=logger, state=True)
    index = FIRST
    while len(engine.open_plan) < 1:  # stop inside the first decision that sends two or more orders: one sent, one still planned
        original = engine._execute

        def execute(entry, **kwargs):
            original(entry, **kwargs)
            if engine.open_plan:
                raise _Crash()

        engine._execute = execute
        try:
            engine.run_cycle(bars_until(index), now=_now(index))
        except _Crash:
            break
        index += 1
        assert index < LAST, "no decision with two orders in this history"
    unsent = [entry["order_id"] for entry in json.loads((tmp_path / "engine.json").read_text())["open_plan"]]
    assert unsent
    del engine
    restarted = _engine(config, tmp_path, logger=logger, state=True)
    report = restarted.run_cycle(bars_until(index + 1), now=_now(index + 1))  # the process was down until the next bar closed
    assert report.decided and restarted.open_plan == [] and report.mismatches == {}
    dropped = logger.list_events(event_types=["portfolio_plan_dropped"])
    assert len(dropped) == 1 and dropped[0]["metadata"]["orders"] == unsent
    assert not {fill["order_id"] for fill in report.fills} & set(unsent)  # the old orders were never sent: the new decision planned its own


def test_another_books_fill_with_the_same_order_id_does_not_hide_a_recovered_fill(tmp_path) -> None:
    """Found by the live drill: the paper soak's "pf-1-0" fill made the live book's recovered "pf-1-0" fill look logged."""
    config = _config()
    logger = TradeLogger(tmp_path / "shared.db")
    (tmp_path / "other").mkdir()
    other = _engine(config, tmp_path / "other", logger=logger, state=True)  # e.g. the paper soak, writing to the same database
    index = FIRST
    while not logger.list_trades():
        other.run_cycle(bars_until(index), now=_now(index))
        index += 1
    others_fills = len(logger.list_trades())

    (tmp_path / "book").mkdir()
    engine = _engine(config, tmp_path / "book", logger=logger, state=True)
    _die_after(engine.adapters["kraken_futures"], "submit_order", 1)
    crashed_at = FIRST
    with pytest.raises(_Crash):
        while True:
            engine.run_cycle(bars_until(crashed_at), now=_now(crashed_at))
            crashed_at += 1
    del engine
    restarted = _engine(config, tmp_path / "book", logger=logger, state=True)
    assert restarted.book_id != other.book_id
    report = restarted.run_cycle(bars_until(crashed_at), now=_now(crashed_at))
    assert report.fills and report.fills[0]["order_id"] in {event["metadata"]["order_id"] for event in logger.list_events(event_types=["portfolio_fill"])
                                                            if event["metadata"]["book_id"] == other.book_id}  # the ids really do collide
    assert len(logger.list_trades()) == others_fills + len(report.fills)  # the recovered fill reached the trade log
    assert logger.list_events(event_types=["portfolio_fill_recovered"]) == []



def test_a_running_book_is_compared_with_the_backtest_bar_by_bar() -> None:
    """The paper-versus-backtest check: the same bars through the engine and through `run_book` hold the same positions."""
    from src.portfolio import paper_check

    config = _config()
    engine = _engine(config)
    snapshots = []
    for index in range(FIRST, FIRST + 150):
        report = engine.run_cycle(bars_until(index), now=_now(index))
        snapshots.append({"timestamp": _now(index).isoformat(), **engine.snapshot(report)})
    snapshots.append({"timestamp": (_now(FIRST + 150) + timedelta(minutes=30)).isoformat(), **engine.snapshot(None)})  # an hourly snapshot: not a decision

    def loader(instrument: InstrumentSpec, interval: str) -> list[OHLCVBar]:
        return bars_until(FIRST + 149)[(instrument.id, interval)]

    frame = paper_check.decision_frame(snapshots, "4h")
    inputs = paper_check.start_at(prepare_inputs(config, bar_loader=loader), frame.index[0])
    assert inputs.prices.index[0] == frame.index[0] and len(inputs.warmup_prices) == FIRST  # the backtest starts where the book did; the rest is warmup
    book = run_book(config, inputs, lots=True)
    assert len(frame) == 150 and frame.index[0] == START + timedelta(hours=4 * FIRST)  # stamped with the bar decided on, one row per decision
    result = paper_check.compare(snapshots, book, "4h")
    summary = result["summary"]
    assert summary["bars"] == 150 and summary["max_abs_position_gap"] < 0.03  # a lot or the band, never a different decision, from the first bar on
    assert abs(summary["return_gap"]) < 0.01 and set(result["returns"].columns) == {"paper", "backtest", "gap"}
    assert paper_check.verdict(summary, tolerance=0.03).startswith("Positions agree")
    assert paper_check.one_lot_weight(config, {BTC: 50_000.0, ETH: 2_500.0}, 10_000.0) == pytest.approx(0.0005)  # 0.0001 BTC at 50,000 over 10,000

    wrong = [dict(snap, instruments={name: dict(row, weight=(row["weight"] or 0.0) + 0.3) for name, row in snap["instruments"].items()}) for snap in snapshots]
    assert paper_check.verdict(paper_check.compare(wrong, book, "4h")["summary"], tolerance=0.03).startswith("Positions DIFFER")
    assert paper_check.compare([], book, "4h")["summary"] == {"bars": 0}
    assert "One bar so far" in paper_check.verdict(paper_check.compare(snapshots[:1], book, "4h")["summary"], tolerance=0.03)


# --- maker-first execution -------------------------------------------------------------------------------------------


class Quotes:
    """A hand-set best bid and ask per symbol, standing in for Kraken's ticker."""

    def __init__(self) -> None:
        self.touch: dict[str, tuple[float, float]] = {}

    def __call__(self, symbol: str) -> tuple[float, float] | None:
        return self.touch.get(symbol)

    def around(self, bars: dict, *, shift: float = 0.0) -> None:
        """A one-basis-point half-spread around each instrument's last 4h close, moved by `shift` (0.001 = up 10 bps)."""
        for (instrument, interval), series in bars.items():
            if interval == "4h":
                mid = series[-1].close * (1.0 + shift)
                self.touch[instrument.split(":", 1)[1]] = (mid * 0.9999, mid * 1.0001)


def _maker_engine(tmp_path, quotes: Quotes, *, logger=None, timeout: float = 120.0) -> PortfolioEngine:
    config = _config(execution={"policy": "maker_first", "maker_timeout_seconds": timeout})
    book = PortfolioBook.from_config(config)
    return PortfolioEngine(config, adapters=build_paper_adapters(config, book, state_dir=tmp_path, quote_source=quotes), book=book, trade_logger=logger,
                           state_path=tmp_path / "engine.json")


def _first_decision_with_orders(engine: PortfolioEngine, quotes: Quotes):
    """Cycle bar by bar (letting every resting order time out) until a decision sends orders; returns (index, report)."""
    for index in range(FIRST, LAST):
        bars = bars_until(index)
        quotes.around(bars)
        report = engine.run_cycle(bars, now=_now(index))
        if report.orders:
            return index, report
        engine.run_cycle(bars, now=_now(index) + timedelta(minutes=5))
    raise AssertionError("no decision sent an order")


def test_maker_first_orders_rest_at_the_touch_and_fill_there_when_the_market_comes_to_them(tmp_path) -> None:
    logger = TradeLogger(tmp_path / "trades.db")
    quotes = Quotes()
    engine = _maker_engine(tmp_path, quotes, logger=logger)
    index, report = _first_decision_with_orders(engine, quotes)
    adapter = engine.adapters["kraken_futures"]
    assert report.fills == [] and len(engine.pending_orders) == len(report.orders) > 0  # sent, resting, nothing traded yet
    assert all(order_id.endswith("-mk") and meta["style"] == "maker" for order_id, meta in engine.pending_orders.items())
    assert engine.snapshot(report)["pending_orders"] == len(report.orders) and engine.snapshot(report)["execution"] == "maker_first"
    resting = adapter.resting_orders()
    for order_id, meta in engine.pending_orders.items():
        bid, ask = quotes.touch[meta["instrument"].split(":", 1)[1]]
        assert resting[order_id]["limit_price"] == (bid if meta["side"] == "buy" else ask)  # a buy joins the bid, a sell the ask

    bars = bars_until(index)
    quiet = engine.run_cycle(bars, now=_now(index) + timedelta(seconds=30))  # the market hasn't moved: still resting
    assert quiet.fills == [] and len(engine.pending_orders) == len(report.orders)
    for order in resting.values():  # the market trades through every resting price
        quotes.touch[order["symbol"]] = (order["limit_price"] * 0.9990, order["limit_price"] * 0.9992) if order["side"] == "buy" else (order["limit_price"] * 1.0008, order["limit_price"] * 1.0010)
    hit = engine.run_cycle(bars, now=_now(index) + timedelta(seconds=60))
    assert len(hit.fills) == len(report.orders) and engine.pending_orders == {} and engine.open_plan == [] and adapter.resting_orders() == {}
    for fill in hit.fills:
        assert fill["liquidity"] == "maker" and fill["price"] == resting[fill["order_id"]]["limit_price"]
        assert fill["fee"] == pytest.approx(fill["units"] * fill["price"] * 0.0002)  # the maker fee, not the taker's 0.05%
    assert engine.reconcile() == {} and not hit.rejected
    results = logger.list_events(event_types=["portfolio_maker_result"])
    assert len(results) == len(report.orders) and {event["metadata"]["outcome"] for event in results} == {"filled"}
    assert len(logger.list_trades()) == len(report.orders)
    assert engine.run_cycle(bars, now=_now(index) + timedelta(seconds=120)).fills == []  # nothing left to send


def test_a_maker_order_that_is_not_reached_is_cancelled_and_the_rest_goes_to_market(tmp_path) -> None:
    logger = TradeLogger(tmp_path / "trades.db")
    quotes = Quotes()
    engine = _maker_engine(tmp_path, quotes, logger=logger, timeout=120.0)
    index, report = _first_decision_with_orders(engine, quotes)
    adapter = engine.adapters["kraken_futures"]
    planned = {meta["instrument"]: (meta["side"], float(meta["units"])) for meta in engine.pending_orders.values()}
    bars = bars_until(index)
    quotes.around(bars, shift=0.001 if all(side == "buy" for side, _units in planned.values()) else 0.0)  # the market runs away from the resting buys
    assert engine.run_cycle(bars, now=_now(index) + timedelta(seconds=60)).fills == [] and len(engine.pending_orders) == len(planned)  # not yet timed out
    late = engine.run_cycle(bars, now=_now(index) + timedelta(seconds=120))
    assert adapter.resting_orders() == {} and engine.pending_orders == {} and engine.open_plan == []
    assert {fill["instrument"]: (fill["side"], fill["units"]) for fill in late.fills} == planned  # the whole order, now as a market order
    for fill in late.fills:
        symbol = fill["instrument"].split(":", 1)[1]
        mid = sum(quotes.touch[symbol]) / 2.0
        assert fill["order_id"].endswith("-tk") and "liquidity" not in fill
        assert fill["price"] == pytest.approx(mid * (1.0002 if fill["side"] == "buy" else 0.9998))  # at the market now (after the move), plus the usual slippage
        assert fill["fee"] == pytest.approx(fill["units"] * fill["price"] * 0.0005)
    assert engine.reconcile() == {}
    assert {event["metadata"]["outcome"] for event in logger.list_events(event_types=["portfolio_maker_result"])} == {"unfilled"}
    assert logger.list_events(event_types=["portfolio_order_unfilled"]) == []  # a timeout is not a warning


def test_a_restart_with_maker_orders_resting_picks_them_up_and_books_each_fill_once(tmp_path) -> None:
    logger = TradeLogger(tmp_path / "trades.db")
    quotes = Quotes()
    index, report = _first_decision_with_orders(_maker_engine(tmp_path, quotes, logger=logger), quotes)
    trades_before = len(logger.list_trades())

    engine = _maker_engine(tmp_path, quotes, logger=logger)  # the process died with the orders resting
    adapter = engine.adapters["kraken_futures"]
    assert engine.restored and len(engine.pending_orders) == len(report.orders) == len(adapter.resting_orders())
    bars = bars_until(index)
    assert engine.run_cycle(bars, now=_now(index) + timedelta(seconds=30)).fills == [] and len(adapter.resting_orders()) == len(report.orders)  # not resent, not dropped
    first = next(iter(adapter.resting_orders().values()))
    quotes.touch[first["symbol"]] = (first["limit_price"] * 0.999, first["limit_price"] * 0.9992) if first["side"] == "buy" else (first["limit_price"] * 1.0008, first["limit_price"] * 1.001)
    hit = engine.run_cycle(bars, now=_now(index) + timedelta(seconds=60))
    assert [fill["liquidity"] for fill in hit.fills] == ["maker"]

    engine = _maker_engine(tmp_path, quotes, logger=logger)  # and again, after that fill was booked and saved
    done = engine.run_cycle(bars, now=_now(index) + timedelta(seconds=180))  # whatever still rested has now timed out
    assert engine.pending_orders == {} and engine.open_plan == [] and engine.adapters["kraken_futures"].resting_orders() == {} and engine.reconcile() == {}
    assert len(logger.list_trades()) == trades_before + len(report.orders) == trades_before + len(hit.fills) + len(done.fills)  # every order traded once


def test_flatten_withdraws_resting_orders_and_never_sends_their_remainder(tmp_path) -> None:
    quotes = Quotes()
    engine = _maker_engine(tmp_path, quotes)
    index, report = _first_decision_with_orders(engine, quotes)
    held = dict(engine.book.units())
    flat = engine.flatten(now=_now(index) + timedelta(seconds=10), reason="test")
    assert engine.adapters["kraken_futures"].resting_orders() == {} and engine.pending_orders == {} and engine.open_plan == []
    assert all(units == 0 for units in engine.book.units().values()) and len(flat.fills) == sum(1 for units in held.values() if units)
    after = engine.run_cycle(bars_until(index), now=_now(index) + timedelta(seconds=300))
    assert after.fills == [] and all(units == 0 for units in engine.book.units().values())  # no late market order for what the maker orders didn't fill


def test_a_halt_goes_straight_to_market_and_taker_books_are_untouched(tmp_path) -> None:
    from src.portfolio.orders import PlannedOrder

    quotes = Quotes()
    maker, taker = _maker_engine(tmp_path, quotes), _engine(_config())
    order = PlannedOrder(instrument=BTC, side="buy", units=Decimal("0.01"), price=50_000.0, reason="open", reduce_only=False, target_weight=0.1)
    assert maker._plan_entry(order, number=0, attribution={}, urgent=False)["order_id"].endswith("-mk")
    assert "style" not in maker._plan_entry(order, number=0, attribution={}, urgent=True)  # a drawdown or daily-loss halt
    assert "style" not in maker._plan_entry(order, number=0, attribution={})  # and anything not planned by a normal decision (flatten)
    assert "style" not in taker._plan_entry(order, number=0, attribution={}, urgent=False)  # the default policy never rests an order
    with pytest.raises(Exception, match="maker_timeout_seconds"):
        _config(execution={"policy": "maker_first", "maker_timeout_seconds": 5})
    with pytest.raises(Exception, match="policy must be one of"):
        _config(execution={"policy": "limit"})


def test_paper_funding_charged_just_before_a_crash_is_booked_after_the_restart(tmp_path) -> None:
    """The paper exchange saves a funding charge before the engine checkpoints; a kill in between must not lose it from the book."""
    config = _config()

    def funding_in_book(engine: PortfolioEngine) -> float:
        return sum(float(position["funding"]) for position in engine.to_dict()["book"]["positions"].values())

    engine = _engine(config, tmp_path, state=True)
    index = FIRST
    while not engine.book.units() or not any(engine.book.units().values()):
        engine.run_cycle(bars_until(index), now=_now(index))
        index += 1
    engine.run_cycle(bars_until(index), now=_now(index))
    adapter = engine.adapters["kraken_futures"]
    assert adapter.funding_paid_total != 0.0 and funding_in_book(engine) == pytest.approx(adapter.funding_paid_total)

    # the next cycle gets as far as the exchange charging funding, and the process dies before its checkpoint
    adapter.on_market_update(prices=adapter.marks(), timestamp=_now(index) + timedelta(hours=2))
    charged = adapter.funding_paid_total
    assert charged != pytest.approx(funding_in_book(engine))

    restarted = _engine(config, tmp_path, state=True)
    assert restarted.restored and funding_in_book(restarted) != pytest.approx(charged)  # the checkpoint doesn't have it
    restarted.run_cycle(bars_until(index), now=_now(index) + timedelta(hours=2))
    assert funding_in_book(restarted) == pytest.approx(restarted.adapters["kraken_futures"].funding_paid_total) == pytest.approx(charged)
    assert float(restarted.book.equity()) == pytest.approx(restarted.adapters["kraken_futures"].equity())  # book and exchange agree again


def test_the_backtest_comparison_reads_positions_once_a_decisions_maker_orders_have_settled() -> None:
    from src.portfolio import paper_check

    def snap(minutes: int, weight: float, *, decision: bool, pending: int, equity: float = 10_000.0) -> dict:
        return {"timestamp": (START + timedelta(hours=4, minutes=minutes)).isoformat(), "equity": equity, "pending_orders": pending,
                "instruments": {BTC: {"weight": weight, "target": 0.4 if decision else None}}}

    frame = paper_check.decision_frame([
        snap(1, 0.0, decision=True, pending=1),                        # the decision: its order is resting, nothing held yet
        snap(6, 0.4, decision=False, pending=0, equity=9_999.0),       # stored when the fill arrived
        snap(60, 0.41, decision=False, pending=0, equity=10_050.0),    # an hourly snapshot later in the bar: not used
    ], "4h")
    assert len(frame) == 1 and frame.index[0] == START
    assert frame.iloc[0][f"w:{BTC}"] == 0.4 and frame.iloc[0]["equity"] == 10_000.0  # the settled position, the decision's equity
    still = paper_check.decision_frame([snap(1, 0.0, decision=True, pending=1)], "4h")
    assert still.iloc[0][f"w:{BTC}"] == 0.0  # nothing settled yet: what the decision snapshot shows
