"""Tests for the core configuration module."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from config import settings


def test_default_settings_are_valid() -> None:
    """Ensure the settings object loads default values with the expected types."""
    assert isinstance(settings.database_path, Path)
    assert settings.log_level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    assert settings.eur_nok_fallback == Decimal("11.50")
