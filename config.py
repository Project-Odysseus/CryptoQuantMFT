"""Application settings loaded from environment variables.

This module centralizes configuration for exchanging credentials, runtime
logging, persistence, and FX fallback values used across the trading engine.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the CryptoQuantMFT trading engine.

    Values are loaded from the local .env file when present and validated with
    strict type hints to prevent accidental misconfiguration at runtime.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_assignment=True,
    )

    firi_api_key: str = Field(
        default="",
        description="API key for the Firi exchange integration.",
    )
    kraken_api_key: str = Field(
        default="",
        description="API key for the Kraken exchange integration.",
    )
    kraken_secret: str = Field(
        default="",
        description="API secret for the Kraken exchange integration.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Application-wide log level for the runtime logger.",
    )
    database_path: Path = Field(
        default=Path("data/cryptoquant.db"),
        description="Local database path for persistence of trades and snapshots.",
    )
    eur_nok_fallback: Decimal = Field(
        default=Decimal("11.50"),
        description="Fallback EUR/NOK exchange rate used when upstream FX data is unavailable.",
    )


settings: Settings = Settings()
