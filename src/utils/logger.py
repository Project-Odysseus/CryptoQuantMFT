"""Application logging utilities for the CryptoQuantMFT trading engine.

The logger is configured with colored console output for local debugging and
JSON-formatted rotating file output for structured telemetry. The file rotation
keeps logs bounded while preserving a forensic trail of runtime events.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from config import settings
from src.utils.telemetry import install_exception_hooks


LOG_DIR: Path = Path("logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _configure_logger() -> None:
    """Set up rotating asynchronous log sinks with console and JSON file output."""
    logger.remove()

    logger.add(
        sink=sys.stdout,
        level=settings.log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | <level>{message}</level>",
        colorize=True,
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )

    logger.add(
        sink=str(LOG_DIR / "app.log"),
        level=settings.log_level,
        rotation="10 MB",
        retention="14 days",
        compression="gz",
        enqueue=True,
        serialize=True,
        backtrace=True,
        diagnose=True,
    )

    install_exception_hooks()


_configure_logger()
