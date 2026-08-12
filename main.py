"""Application entry point for the CryptoQuantMFT trading engine."""

from __future__ import annotations

import asyncio

from config import settings
from src.data.exchanges import FiriConnector, KrakenConnector, MockExchangeConnector
from src.data.pipeline import MarketDataPipeline
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


def main() -> None:
    """Initialize the runtime and run the data pipeline loop."""
    logger.info("CryptoQuantMFT startup complete")
    logger.info("database_path=%s", settings.database_path)
    logger.info("log_level=%s", settings.log_level)
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
