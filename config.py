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
    kraken_futures_api_key: str = Field(
        default="",
        description="API key for Kraken Futures (derivatives). Generated separately from the spot key.",
    )
    kraken_futures_secret: str = Field(
        default="",
        description="API secret for Kraken Futures (derivatives).",
    )
    deribit_client_id: str = Field(
        default="",
        description="Deribit API client id (options). Read-only scope is enough for research and checks.",
    )
    deribit_client_secret: str = Field(
        default="",
        description="Deribit API client secret.",
    )
    deribit_testnet: bool = Field(
        default=False,
        description="Use test.deribit.com (a separate account and separate keys) instead of the live exchange.",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Application-wide log level for the runtime logger.",
    )
    database_path: Path = Field(
        default=Path("data/cryptoquant.db"),
        description="Local database path for persistence of trades and snapshots.",
    )
    telegram_bot_token: str = Field(
        default="",
        description="Telegram bot token used for runtime alerts and trade notifications.",
    )
    telegram_chat_id: str = Field(
        default="",
        description="Telegram chat ID used for runtime alerts and trade notifications.",
    )
    healthcheck_url: str = Field(
        default="",
        description="Dead-man's switch: a ping URL at an outside monitor (e.g. healthchecks.io) that alerts when the runtime goes quiet.",
    )
    eur_nok_fallback: Decimal = Field(
        default=Decimal("11.50"),
        description="Fallback EUR/NOK exchange rate used when upstream FX data is unavailable.",
    )


settings: Settings = Settings()
