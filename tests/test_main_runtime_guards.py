"""Tests for CLI-level runtime safety guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from main import LIVE_TRADING_CONFIRMATION, _validate_live_runtime_request
from src.risk.kill_switch import KillSwitchController
from src.runtime.config import RuntimeConfig


def test_validate_live_runtime_request_requires_explicit_live_opt_in() -> None:
    """Live mode should be rejected unless the explicit enable flag is set."""
    runtime_config = RuntimeConfig(mode="live", exchange="kraken")

    with pytest.raises(SystemExit, match="--enable-live-trading"):
        _validate_live_runtime_request(
            runtime_config=runtime_config,
            use_mock_connector=False,
            enable_live_trading=False,
            live_confirmation=LIVE_TRADING_CONFIRMATION,
        )


def test_validate_live_runtime_request_requires_confirmation_token() -> None:
    """Live mode should require the exact confirmation token."""
    runtime_config = RuntimeConfig(mode="live", exchange="kraken")

    with pytest.raises(SystemExit, match="--live-confirmation"):
        _validate_live_runtime_request(
            runtime_config=runtime_config,
            use_mock_connector=False,
            enable_live_trading=True,
            live_confirmation="not-it",
        )


def test_validate_live_runtime_request_requires_explicit_exchange() -> None:
    """Live mode should refuse the auto exchange selection path."""
    runtime_config = RuntimeConfig(mode="live", exchange=None)

    with pytest.raises(SystemExit, match="explicit --execution-exchange"):
        _validate_live_runtime_request(
            runtime_config=runtime_config,
            use_mock_connector=False,
            enable_live_trading=True,
            live_confirmation=LIVE_TRADING_CONFIRMATION,
        )


def test_validate_live_runtime_request_requires_inactive_kill_switch(tmp_path: Path) -> None:
    """Live mode should be blocked while the kill switch remains active."""
    controller = KillSwitchController(state_file=tmp_path / "kill-switch.json")
    controller.activate("manual")
    runtime_config = RuntimeConfig(mode="live", exchange="kraken")

    with pytest.raises(SystemExit, match="kill switch is active"):
        _validate_live_runtime_request(
            runtime_config=runtime_config,
            use_mock_connector=False,
            enable_live_trading=True,
            live_confirmation=LIVE_TRADING_CONFIRMATION,
            kill_switch_controller=controller,
        )


def test_validate_live_runtime_request_allows_guarded_live_request(tmp_path: Path) -> None:
    """Live mode should pass the safety gate only when all requirements are satisfied."""
    controller = KillSwitchController(state_file=tmp_path / "kill-switch.json")
    runtime_config = RuntimeConfig(mode="live", exchange="kraken")

    _validate_live_runtime_request(
        runtime_config=runtime_config,
        use_mock_connector=False,
        enable_live_trading=True,
        live_confirmation=LIVE_TRADING_CONFIRMATION,
        kill_switch_controller=controller,
    )

    assert controller.state_file.exists()
