"""Application entry point for the CryptoQuantMFT trading engine."""

from __future__ import annotations

from config import settings
from src.utils.logger import logger


def main() -> None:
    """Initialize the runtime and confirm that core configuration is in place."""
    logger.info("CryptoQuantMFT startup complete")
    logger.info("database_path=%s", settings.database_path)
    logger.info("log_level=%s", settings.log_level)


if __name__ == "__main__":
    main()
