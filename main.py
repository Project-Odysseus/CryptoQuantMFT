"""Application entry point for the CryptoQuantMFT trading engine."""

from __future__ import annotations

import argparse
import asyncio
import signal
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from src.backtest import BacktestConfig, EventDrivenSimulator, StrategyPlotter, compare_backtests, evaluate_walk_forward, resolve_strategy, run_backtest
from src.data.exchanges import FiriConnector, KrakenConnector, MockExchangeConnector
from src.execution import ExecutionRouter, PaperTradingEngine
from src.risk.controls import RiskControlConfig, RiskManager
from src.risk.kill_switch import KillSwitchController
from src.data.historical import fetch_kraken_ohlcv
from src.data.pipeline import MarketDataPipeline
from src.runtime import RuntimeConfig, RuntimeOrchestrator, RuntimeWatchdogError, build_runtime_config_from_args
from src.storage.bar_aggregator import OHLCVBar
from src.storage.market_store import MarketStore
from src.storage.order_book import OrderBookSnapshot
from src.storage.trade_logger import TradeLogger
from src.utils.logger import logger


async def run_pipeline(iterations: int = 3, interval_seconds: float = 1.0) -> None:
    """Run the data pipeline for a small number of cycles using the available connectors."""
    store = MarketStore(database_path=settings.database_path)
    pipeline = MarketDataPipeline(store=store, interval_seconds=max(1, int(round(interval_seconds))))

    pipeline.add_connector(KrakenConnector(symbol="BTC/EUR"))

    if settings.firi_api_key:
        pipeline.add_connector(FiriConnector(symbol="BTC/NOK"))

    for index in range(iterations):
        snapshots = await pipeline.run_once()
        bars = pipeline.flush_bars()
        logger.info(
            "pipeline_cycle=%s snapshots=%s bars=%s",
            index + 1,
            [snapshot.symbol for snapshot in snapshots],
            len(bars),
        )
        if index < iterations - 1:
            await asyncio.sleep(interval_seconds)


def build_runtime_orchestrator(
    *,
    config: RuntimeConfig | None = None,
    mode: str | None = None,
    interval_seconds: float | None = None,
    use_mock_connector: bool | None = None,
    watchdog_timeout_seconds: float | None = None,
    exchange: str | None = None,
) -> tuple[RuntimeOrchestrator, MarketDataPipeline]:
    """Build the runtime orchestrator and its market-data pipeline for a run."""
    runtime_config = config or RuntimeConfig(
        mode=mode or "paper",
        interval_seconds=interval_seconds or 1.0,
        use_mock_connector=use_mock_connector or False,
        watchdog_timeout_seconds=watchdog_timeout_seconds or 30.0,
        exchange=exchange,
    )

    market_data_interval_seconds = max(1, int(round(runtime_config.interval_seconds)))
    store = MarketStore(database_path=settings.database_path)
    pipeline = MarketDataPipeline(store=store, interval_seconds=market_data_interval_seconds)

    if runtime_config.use_mock_connector:
        pipeline.add_connector(MockExchangeConnector(symbol="BTC/EUR"))
    else:
        exchange_name = (runtime_config.exchange or "kraken").lower()
        if exchange_name in {"auto", "kraken"}:
            pipeline.add_connector(KrakenConnector(symbol="BTC/EUR"))
        elif exchange_name == "firi" and settings.firi_api_key:
            pipeline.add_connector(FiriConnector(symbol="BTC/NOK"))
        elif exchange_name == "firi":
            logger.warning("runtime_firi_api_key_missing falling back to kraken")
            pipeline.add_connector(KrakenConnector(symbol="BTC/EUR"))

    logger.info(
        "runtime_market_data_config interval_seconds={} connector={} use_mock={}",
        market_data_interval_seconds,
        runtime_config.exchange or "auto",
        runtime_config.use_mock_connector,
    )

    risk_manager = RiskManager(
        RiskControlConfig(
            max_drawdown_pct=0.25,
            max_volatility_pct=0.50,
            risk_per_trade_pct=0.10,
            max_position_size=1.0,
            volatility_window=10,
            kelly_fraction=0.5,
            kelly_window=20,
            max_slippage_pct=0.20,
            max_spread_pct=0.20,
            max_notional_per_trade=10000.0,
            max_total_notional=25000.0,
            paper_mode=(runtime_config.mode == "paper"),
        )
    )
    trade_logger = TradeLogger(database_path=settings.database_path)
    execution_router = ExecutionRouter(mode=runtime_config.mode, exchange=runtime_config.exchange)
    engine = PaperTradingEngine(
        initial_cash=1000.0,
        default_order_size=1.0,
        partial_fill_fraction=1.0,
        max_order_lifetime_bars=3,
        risk_manager=risk_manager,
        trade_logger=trade_logger,
        execution_adapter=execution_router.adapter,
    )
    strategy = resolve_strategy(runtime_config.strategy_name, **runtime_config.strategy_params)
    # Derive the primary trading symbol from the first connector so bars and
    # live-price tracking stay consistent with a single currency.
    primary_symbol: str | None = None
    if pipeline.connectors:
        primary_symbol = getattr(pipeline.connectors[0], "symbol", None)
    orchestrator = RuntimeOrchestrator(
        pipeline=pipeline,
        execution_engine=engine,
        strategy=strategy,
        mode=runtime_config.mode,
        interval_seconds=runtime_config.interval_seconds,
        watchdog_timeout_seconds=runtime_config.watchdog_timeout_seconds,
        trade_logger=trade_logger,
        strategy_name=runtime_config.strategy_name,
        strategy_params=runtime_config.strategy_params,
        runtime_config=runtime_config,
        checkpoint_path=runtime_config.state_path,
        live_plot=runtime_config.live_plot,
        live_plot_path=runtime_config.live_plot_path,
        trading_symbol=primary_symbol,
    )
    return orchestrator, pipeline


async def run_runtime_orchestrator(
    *,
    config: RuntimeConfig | None = None,
    mode: str = "paper",
    iterations: int = 3,
    interval_seconds: float = 1.0,
    use_mock_connector: bool = False,
    watchdog_timeout_seconds: float = 30.0,
    watchdog_restarts: int = 0,
    exchange: str | None = None,
    resume_runtime: bool = False,
) -> RuntimeOrchestrator | None:
    """Run the runtime orchestrator over a simple market-data pipeline."""
    runtime_config = config or RuntimeConfig(
        mode=mode,
        iterations=iterations,
        interval_seconds=interval_seconds,
        use_mock_connector=use_mock_connector,
        watchdog_timeout_seconds=watchdog_timeout_seconds,
        watchdog_restarts=watchdog_restarts,
        exchange=exchange,
    )
    restart_attempts = max(0, runtime_config.watchdog_restarts) + 1
    last_orchestrator: RuntimeOrchestrator | None = None

    for attempt in range(restart_attempts):
        orchestrator, _pipeline = build_runtime_orchestrator(
            config=runtime_config,
            mode=runtime_config.mode,
            interval_seconds=runtime_config.interval_seconds,
            use_mock_connector=runtime_config.use_mock_connector,
            watchdog_timeout_seconds=runtime_config.watchdog_timeout_seconds,
            exchange=runtime_config.exchange,
        )
        loop = asyncio.get_running_loop()

        def _request_shutdown(signum: int) -> None:
            logger.warning("runtime_shutdown_signal signal={}", signum)
            orchestrator.request_shutdown(reason=f"signal:{signum}")

        def _install_signal_handlers() -> None:
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, lambda current_signum=signum: _request_shutdown(current_signum))
                except (AttributeError, NotImplementedError):
                    signal.signal(signum, lambda current_signum, _frame: _request_shutdown(current_signum))

        def _remove_signal_handlers() -> None:
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.remove_signal_handler(signum)
                except (AttributeError, NotImplementedError):
                    pass

        _install_signal_handlers()
        try:
            await orchestrator.run_loop(
                iterations=runtime_config.iterations,
                interval_seconds=runtime_config.interval_seconds,
                resume_from_checkpoint=resume_runtime,
            )
        except RuntimeWatchdogError as exc:
            orchestrator.request_shutdown(reason="watchdog_timeout")
            logger.warning("runtime_watchdog_triggered attempt={} reason={}", attempt + 1, exc)
            if attempt + 1 >= restart_attempts:
                raise
            logger.info("runtime_restarting attempt={} of={}", attempt + 2, restart_attempts)
            continue
        except (asyncio.CancelledError, KeyboardInterrupt) as exc:
            orchestrator.request_shutdown(reason="interrupted")
            logger.warning("runtime_interrupted error={}", exc)
        finally:
            _remove_signal_handlers()

        last_orchestrator = orchestrator
        break

    if last_orchestrator is None:
        logger.info("runtime_complete mode={} iterations={} completed=0", runtime_config.mode, runtime_config.iterations)
        return None

    logger.info("runtime_health_report {}", last_orchestrator.get_health_report())

    last_cycle = last_orchestrator.last_cycle
    if last_cycle is None or last_cycle.execution_result is None:
        logger.info("runtime_complete mode={} iterations={} completed=0", runtime_config.mode, runtime_config.iterations)
        return last_orchestrator

    portfolio_history = last_cycle.execution_result.portfolio_history
    final_equity = portfolio_history[-1].equity if portfolio_history else 1000.0
    logger.info(
        "runtime_complete mode={} iterations={} trades={} final_equity={}",
        runtime_config.mode,
        runtime_config.iterations,
        len(last_cycle.execution_result.trades),
        final_equity,
    )
    return last_orchestrator


def build_demo_bars(count: int = 60) -> list[OHLCVBar]:
    """Create a synthetic OHLCV series for a simple demo backtest."""
    closes: list[float] = []
    close = 100.0
    for index in range(count):
        close = close + (0.6 if index % 3 else -0.2) + (0.15 if index % 5 == 0 else 0.0)
        closes.append(close)

    bars: list[OHLCVBar] = []
    for index, close in enumerate(closes):
        timestamp = datetime(2024, 1, 1, 0, index, tzinfo=timezone.utc)
        bars.append(
            OHLCVBar(
                exchange="demo",
                symbol="BTC/NOK",
                interval_seconds=60,
                timestamp=timestamp,
                open=close - 0.5,
                high=close + 1.5,
                low=close - 1.5,
                close=close,
                volume=10.0 + index,
            )
        )
    return bars


def build_kraken_bars(symbol: str = "BTC/EUR", count: int = 200) -> list[OHLCVBar]:
    """Fetch recent Kraken OHLCV bars for the configured backtest."""
    return fetch_kraken_ohlcv(symbol=symbol, interval_seconds=60, count=count)


def build_demo_order_book_snapshots() -> list[OrderBookSnapshot]:
    """Create a small synthetic sequence of L2-style order book snapshots."""
    mid_prices = [100.0, 101.0, 99.5, 100.8, 102.0, 101.0]
    snapshots: list[OrderBookSnapshot] = []
    for index, mid_price in enumerate(mid_prices):
        spread = 0.6 + (index * 0.05)
        half_spread = spread / 2.0
        bids = [(mid_price - half_spread, 0.8), (mid_price - half_spread - 0.2, 0.6), (mid_price - half_spread - 0.4, 0.4)]
        asks = [(mid_price + half_spread, 0.8), (mid_price + half_spread + 0.2, 0.6), (mid_price + half_spread + 0.4, 0.4)]
        snapshots.append(
            OrderBookSnapshot(
                bids=bids,
                asks=asks,
                timestamp=datetime(2024, 1, 1, 0, index, tzinfo=timezone.utc),
            )
        )
    return snapshots


def build_demo_l2_signals(snapshots: list[OrderBookSnapshot]) -> list[float]:
    """Create simple momentum-based signals from synthetic order-book snapshots."""
    if not snapshots:
        return []

    signals: list[float] = []
    previous_mid = None
    for snapshot in snapshots:
        mid_price = (snapshot.bids[0][0] + snapshot.asks[0][0]) / 2.0 if snapshot.bids and snapshot.asks else 0.0
        if previous_mid is None:
            signals.append(0.0)
        elif mid_price > previous_mid:
            signals.append(1.0)
        else:
            signals.append(-1.0)
        previous_mid = mid_price
    return signals


def run_demo_backtest(output_dir: str | Path = "plots", bars: list[OHLCVBar] | None = None) -> None:
    """Run a small synthetic backtest and generate equity/trade plots."""
    bars = bars or build_demo_bars()
    config = BacktestConfig(strategy_name="moving_average_crossover", include_costs=False)
    result = run_backtest(bars, config=config)

    plotter = StrategyPlotter(output_dir=output_dir)
    equity_path = plotter.plot_equity_curve(
        result.equity_series,
        timestamps=result.timestamps,
        title="Demo Signal Equity Curve",
    )
    trade_path = plotter.plot_equity_and_trades(
        result.equity_series,
        result.trade_prices,
        timestamps=result.timestamps,
        trade_timestamps=result.trade_timestamps,
        price_series=[bar.close for bar in bars],
        price_timestamps=[bar.timestamp for bar in bars],
        title="Demo Signal Equity and Trades",
    )

    logger.info(
        "demo_backtest_complete total_return={} trades={} final_equity={} sharpe={} sortino={} max_drawdown={} profit_factor={} equity_plot={} trade_plot={}",
        result.total_return,
        result.trades,
        result.final_equity,
        result.metrics.sharpe_ratio,
        result.metrics.sortino_ratio,
        result.metrics.max_drawdown,
        result.metrics.profit_factor,
        equity_path,
        trade_path,
    )


def run_l2_simulation() -> None:
    """Run the lightweight L2 simulator over synthetic order-book snapshots."""
    snapshots = build_demo_order_book_snapshots()
    signals = build_demo_l2_signals(snapshots)
    simulator = EventDrivenSimulator(
        latency_ms=200,
        max_slippage=0.01,
        initial_equity=1000.0,
        position_size=1.0,
        queue_position_penalty=0.0002,
        impact_penalty=0.001,
        adverse_selection_penalty=0.0005,
    )
    trades, equity_curve = simulator.run(snapshots, signals)

    logger.info(
        "l2_simulator_complete trades={} final_equity={} first_equity={}",
        len(trades),
        equity_curve[-1] if equity_curve else 1000.0,
        equity_curve[0] if equity_curve else 1000.0,
    )


def run_paper_trading(
    bars: list[OHLCVBar],
    signals: list[float | int | str | None] | None = None,
    *,
    strategy_name: str = "moving_average_crossover",
    strategy_params: dict | None = None,
) -> None:
    """Run the paper-trading engine over bars and signals."""
    if signals is None:
        strategy = resolve_strategy(strategy_name, **(strategy_params or {}))
        signals = []
        for index in range(len(bars)):
            history = list(bars[: index + 1])
            signals.append(strategy(history, index, bars[index]))

    risk_manager = RiskManager(
        RiskControlConfig(
            max_drawdown_pct=0.25,
            max_volatility_pct=0.10,
            risk_per_trade_pct=0.10,
            max_position_size=1.0,
            volatility_window=10,
            kelly_fraction=0.5,
            kelly_window=20,
        )
    )
    trade_logger = TradeLogger(database_path="data/trades.db")
    engine = PaperTradingEngine(
        initial_cash=1000.0,
        default_order_size=1.0,
        partial_fill_fraction=1.0,
        max_order_lifetime_bars=3,
        risk_manager=risk_manager,
        trade_logger=trade_logger,
    )
    result = engine.run(bars, signals)
    final_equity = result.portfolio_history[-1].equity if result.portfolio_history else 1000.0

    logger.info(
        "paper_trading_complete orders={} trades={} final_equity={} final_cash={} final_position={}",
        len(result.orders),
        len(result.trades),
        final_equity,
        result.portfolio_history[-1].cash if result.portfolio_history else 1000.0,
        result.portfolio_history[-1].position_size if result.portfolio_history else 0.0,
    )


def print_trade_report(limit: int = 10) -> None:
    """Print the most recent trades, equity snapshots, and operational events from the SQLite logger."""
    logger = TradeLogger(database_path=settings.database_path)
    trades = logger.list_trades(limit=limit)
    snapshots = logger.list_equity_snapshots(limit=limit)
    events = logger.list_events(limit=limit)

    print(f"Recent trades (last {len(trades)}):")
    if not trades:
        print("  (none)")
    else:
        for trade in trades:
            print(
                f"  {trade['timestamp']} | {trade['side']} {trade['pair']} @ {trade['price']:.4f} size={trade['size']:.4f} fee={trade['fee']:.4f}"
            )

    print(f"\nRecent equity snapshots (last {len(snapshots)}):")
    if not snapshots:
        print("  (none)")
    else:
        for snapshot in snapshots:
            print(
                f"  {snapshot['timestamp']} | equity={snapshot['equity']:.4f} cash={snapshot['cash']:.4f} position={snapshot['position_size']:.4f}"
            )

    print(f"\nRecent operational events (last {len(events)}):")
    if not events:
        print("  (none)")
    else:
        for event in events:
            print(
                f"  {event['timestamp']} | [{event['level']}] {event['event_type']} | {event['message']}"
            )


def print_health_dashboard(
    *,
    limit: int = 10,
    runtime_config: RuntimeConfig | None = None,
    orchestrator: RuntimeOrchestrator | None = None,
) -> None:
    """Print a compact health dashboard for the current runtime state."""
    logger_store = TradeLogger(database_path=settings.database_path)
    events = logger_store.list_events(limit=limit)

    latest_event = events[0] if events else None
    latest_trade = logger_store.list_trades(limit=1)[0] if logger_store.list_trades(limit=1) else None
    latest_snapshot = logger_store.list_equity_snapshots(limit=1)[0] if logger_store.list_equity_snapshots(limit=1) else None

    report = orchestrator.get_operational_report(limit=limit) if orchestrator is not None else None

    print("Runtime health dashboard")
    print("-" * 28)
    if report is not None:
        runtime_info = report["runtime"]
        print(f"Mode: {runtime_info['mode']}")
        print(f"Strategy: {runtime_info['strategy_name']} ({runtime_info['strategy_params']})")
        print(f"Exchange: {runtime_info['exchange']}")
        print(f"Cycles: {runtime_info['cycles_completed']}")
        print(f"Healthy: {'yes' if report['runtime']['last_error'] is None else 'no'}")
        print(f"Circuit breaker: {'active' if report['safety']['circuit_breaker']['active'] else 'inactive'}")
        print(f"Kill switch: {'active' if report['safety']['kill_switch']['active'] else 'inactive'}")
        print(f"Reconciliation: {report['reconciliation']['status']}")
        print(f"Unsettled orders: {report['reconciliation']['unsettled_order_count']}")
        print(f"Mismatches: {len(report['reconciliation']['mismatches'])}")
        heartbeat = report.get("heartbeat", {})
        print(f"Heartbeat: {'stalled' if heartbeat.get('stalled') else 'healthy'} ({heartbeat.get('seconds_since_last_heartbeat')}s since last)")
        print(f"Active alerts: {', '.join(sorted(report.get('active_alerts', []))) or 'none'}")
        format_reason = report["no_trade_summary"].get("reason")
        print(f"No-trade reason: {format_reason or 'none'}")
    else:
        print(f"Mode: {runtime_config.mode if runtime_config is not None else 'unknown'}")
        print(f"Strategy: {runtime_config.strategy_name if runtime_config is not None else 'unknown'}")
        print(f"Exchange: {runtime_config.exchange if runtime_config is not None else 'unknown'}")
        print(f"Last event: {latest_event['timestamp'] if latest_event else 'none'}")
        print(f"Last event type: {latest_event['event_type'] if latest_event else 'none'}")
        print(f"Last trade: {latest_trade['timestamp'] if latest_trade else 'none'}")
        print(f"Last equity snapshot: {latest_snapshot['timestamp'] if latest_snapshot else 'none'}")
        if latest_snapshot:
            print(f"Current equity: {latest_snapshot['equity']:.4f} | cash: {latest_snapshot['cash']:.4f} | position: {latest_snapshot['position_size']:.4f}")

    if report is not None:
        account_state = report["account_state"]
        balances = account_state.get("balances", {})
        positions = account_state.get("positions", {})
        print("Account state:")
        if balances:
            for currency, amount in balances.items():
                print(f"  - balance {currency}: {amount:.4f}")
        if positions:
            for symbol, size in positions.items():
                print(f"  - position {symbol}: {size:.4f}")
        if not balances and not positions:
            print("  - (empty)")

    print("Recent operational events:")
    if not events:
        print("  (none)")
    else:
        for event in events[:5]:
            print(f"  - [{event['level']}] {event['event_type']}: {event['message']}")


def main() -> None:
    """Initialize the runtime and run either the data pipeline or a demo backtest."""
    parser = argparse.ArgumentParser(description="CryptoQuantMFT runtime")
    parser.add_argument("--demo-backtest", action="store_true", help="Run a synthetic backtest and save plots")
    parser.add_argument("--plot-output-dir", default="plots", help="Directory for generated plots")
    parser.add_argument("--strategy", default="moving_average_crossover", help="Name of the strategy to run")
    parser.add_argument("--strategy-params", default="{}", help="Optional JSON object of strategy constructor parameters, e.g. '{\"lookback\": 5, \"threshold\": 0.01}'")
    parser.add_argument("--include-costs", action="store_true", help="Apply the fee and FX cost model")
    parser.add_argument("--compare-costs", action="store_true", help="Compare baseline and cost-adjusted backtests")
    parser.add_argument("--paper-trading", action="store_true", help="Run the paper-trading engine over bars and signals")
    parser.add_argument("--use-kraken-data", action="store_true", help="Backtest on recent Kraken OHLCV bars instead of synthetic demo bars")
    parser.add_argument("--kraken-symbol", default="BTC/EUR", help="Kraken symbol to fetch, e.g. BTC/EUR")
    parser.add_argument("--kraken-bars", type=int, default=200, help="Number of Kraken OHLCV bars to fetch")
    parser.add_argument("--taker-fee", type=float, default=0.4, help="Approximate taker fee as a percentage")
    parser.add_argument("--maker-fee", type=float, default=0.25, help="Approximate maker fee as a percentage")
    parser.add_argument("--fx-spread-bps", type=float, default=10.0, help="Approximate FX spread in bps")
    parser.add_argument("--report", action="store_true", help="Print recent trades, equity snapshots, and operational events from the SQLite logger")
    parser.add_argument("--report-limit", type=int, default=10, help="Number of recent rows to print in the report")
    parser.add_argument("--dashboard", action="store_true", help="Print a compact health dashboard based on recent runtime events and portfolio snapshots")
    parser.add_argument("--daily-summary", action="store_true", help="Print the persisted daily summary report for the selected day")
    parser.add_argument("--daily-summary-date", default=None, help="Optional report date in YYYY-MM-DD format")
    parser.add_argument("--l2-simulator", action="store_true", help="Run the lightweight event-driven L2 simulator over synthetic snapshots")
    parser.add_argument("--walk-forward", action="store_true", help="Run a simple walk-forward evaluation over the selected bars")
    parser.add_argument("--walk-forward-train-window", type=int, default=40, help="Number of bars to use as the warmup/training window")
    parser.add_argument("--walk-forward-test-window", type=int, default=20, help="Number of bars to use as the out-of-sample test window")
    parser.add_argument("--walk-forward-step", type=int, default=20, help="Number of bars to move between walk-forward windows")
    parser.add_argument("--runtime", choices=["paper", "live_dry_run", "live"], help="Run the runtime orchestrator with the requested mode")
    parser.add_argument("--runtime-iterations", type=int, default=3, help="Number of runtime cycles to execute")
    parser.add_argument("--runtime-interval", type=float, default=1.0, help="Delay in seconds between runtime cycles")
    parser.add_argument("--execution-exchange", choices=["auto", "sandbox", "kraken", "firi"], default="auto", help="Exchange routing target for the runtime execution adapter")
    parser.add_argument("--use-mock-connector", action="store_true", help="Use the mock exchange connector for the runtime loop")
    parser.add_argument("--watchdog-timeout", type=float, default=30.0, help="Seconds without a completed cycle or fresh data before the watchdog triggers")
    parser.add_argument("--watchdog-restarts", type=int, default=0, help="Number of times to restart the runtime after a watchdog timeout")
    parser.add_argument("--runtime-config-path", default=None, help="Optional JSON file path used to persist and reload the runtime config")
    parser.add_argument("--runtime-state-path", default=None, help="Optional JSON file path used to persist and reload the runtime checkpoint")
    parser.add_argument("--live-plot", action="store_true", help="Write a continuously updating equity/trade plot to disk during the runtime")
    parser.add_argument("--live-plot-path", default=None, help="Optional file path for the runtime live plot image")
    parser.add_argument("--resume-runtime", action="store_true", help="Load the runtime state from a checkpoint file before starting")
    parser.add_argument("--kill-switch", action="store_true", help="Activate the runtime kill switch and cancel any open orders via the configured execution adapter")
    parser.add_argument("--kill-switch-reason", default="manual", help="Reason to record when activating the kill switch")
    args = parser.parse_args()
    runtime_config = build_runtime_config_from_args(args)

    logger.info("CryptoQuantMFT startup complete")
    logger.info("database_path={}", settings.database_path)
    logger.info("log_level={}", settings.log_level)

    logger_store = TradeLogger(database_path=settings.database_path)

    if args.kill_switch:
        controller = KillSwitchController(trade_logger=TradeLogger(database_path=settings.database_path))
        state = controller.activate(args.kill_switch_reason)
        print("Kill switch activated")
        print(f"Reason: {state['reason']}")
        print(f"Orders cancelled: {len(state['orders_cancelled'])}")
        return

    if args.l2_simulator:
        run_l2_simulation()
        return

    if args.walk_forward:
        bars = build_demo_bars()
        if args.use_kraken_data:
            bars = build_kraken_bars(symbol=args.kraken_symbol, count=args.kraken_bars)

        config = BacktestConfig(
            strategy_name=args.strategy,
            include_costs=args.include_costs,
            taker_fee=args.taker_fee,
            maker_fee=args.maker_fee,
            fx_spread_bps=args.fx_spread_bps,
        )
        result = evaluate_walk_forward(
            bars,
            config=config,
            train_window=args.walk_forward_train_window,
            test_window=args.walk_forward_test_window,
            step_size=args.walk_forward_step,
        )
        logger.info(
            "walk_forward_complete folds={} avg_return={} median_return={} positive_folds={} cumulative_return={}",
            result.summary["fold_count"],
            result.summary["avg_return"],
            result.summary["median_return"],
            result.summary["positive_folds"],
            result.summary["cumulative_return"],
        )
        return

    if args.runtime:
        orchestrator = asyncio.run(
            run_runtime_orchestrator(
                config=runtime_config,
                mode=args.runtime,
                iterations=args.runtime_iterations,
                interval_seconds=args.runtime_interval,
                use_mock_connector=args.use_mock_connector,
                watchdog_timeout_seconds=args.watchdog_timeout,
                watchdog_restarts=args.watchdog_restarts,
                exchange=None if args.execution_exchange == "auto" else args.execution_exchange,
                resume_runtime=args.resume_runtime,
            )
        )
        if args.dashboard:
            print_health_dashboard(limit=args.report_limit, runtime_config=runtime_config, orchestrator=orchestrator)
        if args.report:
            print_trade_report(limit=args.report_limit)
        if args.daily_summary:
            summary_date = None
            if args.daily_summary_date:
                try:
                    summary_date = datetime.strptime(args.daily_summary_date, "%Y-%m-%d")
                except ValueError as exc:
                    raise SystemExit(f"invalid daily summary date: {args.daily_summary_date}") from exc
            summary = logger_store.get_daily_summary(report_date=summary_date)
            print("Daily summary report")
            print("-" * 22)
            print(f"Date: {summary['report_date']}")
            print(f"Trades: {summary['total_trades']}")
            print(f"Starting equity: {summary['starting_equity']:.4f}")
            print(f"Ending equity: {summary['ending_equity']:.4f}")
            print(f"PnL: {summary['total_pnl']:.4f}")
            print(f"Max drawdown: {summary['max_drawdown']:.4f} ({summary['max_drawdown_pct']:.2%})")
            print(f"Alerts: {summary['alert_count']}")
            print(f"Active alerts: {', '.join(summary['active_alerts']) if summary['active_alerts'] else 'none'}")
            print(f"Runtime status: {summary['runtime_status']}")
            print(f"Research status: {summary['research_status']}")
            print(f"Summary: {summary['summary_text']}")
        return

    if args.dashboard:
        print_health_dashboard(limit=args.report_limit, runtime_config=runtime_config)
        return

    if args.report:
        print_trade_report(limit=args.report_limit)
        return

    if args.daily_summary:
        summary_date = None
        if args.daily_summary_date:
            try:
                summary_date = datetime.strptime(args.daily_summary_date, "%Y-%m-%d")
            except ValueError as exc:
                raise SystemExit(f"invalid daily summary date: {args.daily_summary_date}") from exc
        summary = logger_store.get_daily_summary(report_date=summary_date)
        print("Daily summary report")
        print("-" * 22)
        print(f"Date: {summary['report_date']}")
        print(f"Trades: {summary['total_trades']}")
        print(f"Starting equity: {summary['starting_equity']:.4f}")
        print(f"Ending equity: {summary['ending_equity']:.4f}")
        print(f"PnL: {summary['total_pnl']:.4f}")
        print(f"Max drawdown: {summary['max_drawdown']:.4f} ({summary['max_drawdown_pct']:.2%})")
        print(f"Alerts: {summary['alert_count']}")
        print(f"Active alerts: {', '.join(summary['active_alerts']) if summary['active_alerts'] else 'none'}")
        print(f"Runtime status: {summary['runtime_status']}")
        print(f"Research status: {summary['research_status']}")
        print(f"Summary: {summary['summary_text']}")
        return

    if args.demo_backtest:
        bars = build_demo_bars()
        if args.use_kraken_data:
            bars = build_kraken_bars(symbol=args.kraken_symbol, count=args.kraken_bars)

        if args.paper_trading:
            run_paper_trading(bars, strategy_name=args.strategy, strategy_params=runtime_config.strategy_params)
            return

        config = BacktestConfig(
            strategy_name=args.strategy,
            include_costs=args.include_costs,
            taker_fee=args.taker_fee,
            maker_fee=args.maker_fee,
            fx_spread_bps=args.fx_spread_bps,
        )
        if args.compare_costs:
            comparison = compare_backtests(bars, config=config)
            logger.info(
                "demo_backtest_compare strategy={} baseline_return={} cost_return={} equity_delta={} baseline_sharpe={} cost_sharpe={} bars={}",
                config.strategy_name,
                comparison.baseline.total_return,
                comparison.with_costs.total_return,
                comparison.equity_delta,
                comparison.baseline.metrics.sharpe_ratio,
                comparison.with_costs.metrics.sharpe_ratio,
                len(bars),
            )
        else:
            result = run_backtest(bars, config=config)
            logger.info(
                "demo_backtest_complete strategy={} include_costs={} total_return={} trades={} final_equity={} bars={}",
                config.strategy_name,
                config.include_costs,
                result.total_return,
                result.trades,
                result.final_equity,
                len(bars),
            )
        return

    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
