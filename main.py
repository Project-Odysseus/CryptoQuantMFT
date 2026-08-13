"""Application entry point for the CryptoQuantMFT trading engine."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from src.backtest import SimpleBacktester, StrategyPlotter
from src.data.exchanges import FiriConnector, KrakenConnector, MockExchangeConnector
from src.data.pipeline import MarketDataPipeline
from src.storage.bar_aggregator import OHLCVBar
from src.storage.market_store import MarketStore
from src.utils.logger import logger


async def run_pipeline(iterations: int = 3, interval_seconds: float = 1.0) -> None:
    """Run the data pipeline for a small number of cycles using the available connectors."""
    store = MarketStore(database_path=settings.database_path)
    pipeline = MarketDataPipeline(store=store, interval_seconds=60)

    pipeline.add_connector(MockExchangeConnector(symbol="BTC/NOK"))

    if settings.firi_api_key:
        pipeline.add_connector(FiriConnector(symbol="BTC/NOK"))
    if settings.kraken_api_key and settings.kraken_secret:
        pipeline.add_connector(KrakenConnector(symbol="BTC/EUR"))

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


def run_demo_backtest(output_dir: str | Path = "plots") -> None:
    """Run a small synthetic backtest and generate equity/trade plots."""
    bars = build_demo_bars()
    backtester = SimpleBacktester()
    result = backtester.run(bars)

    plotter = StrategyPlotter(output_dir=output_dir)
    equity_path = plotter.plot_equity_curve(result.equity_series, title="Demo Signal Equity Curve")
    trade_path = plotter.plot_equity_and_trades(result.equity_series, result.trade_prices, title="Demo Signal Equity and Trades")

    logger.info(
        "demo_backtest_complete total_return={} trades={} final_equity={} equity_plot={} trade_plot={}",
        result.total_return,
        result.trades,
        result.final_equity,
        equity_path,
        trade_path,
    )


def main() -> None:
    """Initialize the runtime and run either the data pipeline or a demo backtest."""
    parser = argparse.ArgumentParser(description="CryptoQuantMFT runtime")
    parser.add_argument("--demo-backtest", action="store_true", help="Run a synthetic backtest and save plots")
    parser.add_argument("--plot-output-dir", default="plots", help="Directory for generated plots")
    args = parser.parse_args()

    logger.info("CryptoQuantMFT startup complete")
    logger.info("database_path={}", settings.database_path)
    logger.info("log_level={}", settings.log_level)

    if args.demo_backtest:
        run_demo_backtest(output_dir=args.plot_output_dir)
        return

    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
