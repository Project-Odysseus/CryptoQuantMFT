"""Application entry point for the CryptoQuantMFT trading engine."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from src.backtest import BacktestConfig, StrategyPlotter, compare_backtests, run_backtest, moving_average_crossover_strategy
from src.data.exchanges import FiriConnector, KrakenConnector
from src.execution import PaperTradingEngine
from src.risk.controls import RiskControlConfig, RiskManager
from src.data.historical import fetch_kraken_ohlcv
from src.data.pipeline import MarketDataPipeline
from src.storage.bar_aggregator import OHLCVBar
from src.storage.market_store import MarketStore
from src.utils.logger import logger


async def run_pipeline(iterations: int = 3, interval_seconds: float = 1.0) -> None:
    """Run the data pipeline for a small number of cycles using the available connectors."""
    store = MarketStore(database_path=settings.database_path)
    pipeline = MarketDataPipeline(store=store, interval_seconds=60)

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


def build_demo_bars() -> list[OHLCVBar]:
    """Create a synthetic OHLCV series for a simple demo backtest."""
    closes = [100.0, 103.0, 104.5, 108.0, 107.0, 111.0, 112.0, 115.0, 113.5, 118.0]
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


def run_paper_trading(bars: list[OHLCVBar], signals: list[float | int | str | None] | None = None) -> None:
    """Run the paper-trading engine over bars and signals."""
    if signals is None:
        strategy = moving_average_crossover_strategy(short_window=3, long_window=6)
        signals = []
        for index in range(len(bars)):
            history = list(bars[: index + 1])
            signals.append(strategy(history, index, bars[index]))

    risk_manager = RiskManager(
        RiskControlConfig(
            max_drawdown_pct=0.25,
            max_volatility_pct=0.10,
            risk_per_trade_pct=0.02,
            max_position_size=1.0,
            volatility_window=10,
            kelly_fraction=0.5,
            kelly_window=20,
        )
    )
    engine = PaperTradingEngine(
        initial_cash=1000.0,
        default_order_size=1.0,
        partial_fill_fraction=1.0,
        max_order_lifetime_bars=3,
        risk_manager=risk_manager,
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


def main() -> None:
    """Initialize the runtime and run either the data pipeline or a demo backtest."""
    parser = argparse.ArgumentParser(description="CryptoQuantMFT runtime")
    parser.add_argument("--demo-backtest", action="store_true", help="Run a synthetic backtest and save plots")
    parser.add_argument("--plot-output-dir", default="plots", help="Directory for generated plots")
    parser.add_argument("--strategy", default="moving_average_crossover", help="Name of the strategy to run")
    parser.add_argument("--include-costs", action="store_true", help="Apply the fee and FX cost model")
    parser.add_argument("--compare-costs", action="store_true", help="Compare baseline and cost-adjusted backtests")
    parser.add_argument("--paper-trading", action="store_true", help="Run the paper-trading engine over bars and signals")
    parser.add_argument("--use-kraken-data", action="store_true", help="Backtest on recent Kraken OHLCV bars instead of synthetic demo bars")
    parser.add_argument("--kraken-symbol", default="BTC/EUR", help="Kraken symbol to fetch, e.g. BTC/EUR")
    parser.add_argument("--kraken-bars", type=int, default=200, help="Number of Kraken OHLCV bars to fetch")
    parser.add_argument("--taker-fee", type=float, default=0.4, help="Approximate taker fee as a percentage")
    parser.add_argument("--maker-fee", type=float, default=0.25, help="Approximate maker fee as a percentage")
    parser.add_argument("--fx-spread-bps", type=float, default=10.0, help="Approximate FX spread in bps")
    args = parser.parse_args()

    logger.info("CryptoQuantMFT startup complete")
    logger.info("database_path={}", settings.database_path)
    logger.info("log_level={}", settings.log_level)

    if args.demo_backtest:
        bars = build_demo_bars()
        if args.use_kraken_data:
            bars = build_kraken_bars(symbol=args.kraken_symbol, count=args.kraken_bars)

        if args.paper_trading:
            run_paper_trading(bars)
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
