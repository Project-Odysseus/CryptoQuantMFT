"""Tests for CLI-level runtime safety guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from main import (
    KRAKEN_MANUAL_ORDER_CONFIRMATION,
    LIVE_TRADING_CONFIRMATION,
    _validate_kraken_manual_close_request,
    _validate_kraken_manual_submit_request,
    _validate_live_runtime_request,
)
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


def test_validate_kraken_manual_submit_request_requires_extra_confirmation() -> None:
    """Manual Kraken submission should require its own exact confirmation token."""
    with pytest.raises(SystemExit, match="--kraken-submit-confirmation"):
        _validate_kraken_manual_submit_request(
            symbol="BTC/EUR",
            side="buy",
            quote_amount=3.5,
            enable_live_trading=True,
            live_confirmation=LIVE_TRADING_CONFIRMATION,
            submit_confirmation="not-it",
        )


def test_validate_kraken_manual_submit_request_requires_eur_symbol() -> None:
    """Manual Kraken submission is intentionally limited to EUR-quoted pairs for now."""
    with pytest.raises(SystemExit, match="EUR-quoted"):
        _validate_kraken_manual_submit_request(
            symbol="BTC/USD",
            side="buy",
            quote_amount=10.0,
            enable_live_trading=True,
            live_confirmation=LIVE_TRADING_CONFIRMATION,
            submit_confirmation=KRAKEN_MANUAL_ORDER_CONFIRMATION,
        )


def test_validate_kraken_manual_submit_request_requires_inactive_kill_switch(tmp_path: Path) -> None:
    """Manual Kraken submission should be blocked while the kill switch is active."""
    controller = KillSwitchController(state_file=tmp_path / "kill-switch.json")
    controller.activate("manual")

    with pytest.raises(SystemExit, match="kill switch is active"):
        _validate_kraken_manual_submit_request(
            symbol="BTC/EUR",
            side="buy",
            quote_amount=3.5,
            enable_live_trading=True,
            live_confirmation=LIVE_TRADING_CONFIRMATION,
            submit_confirmation=KRAKEN_MANUAL_ORDER_CONFIRMATION,
            kill_switch_controller=controller,
        )


def test_validate_kraken_manual_submit_request_allows_guarded_submit(tmp_path: Path) -> None:
    """Manual Kraken submission should pass only when all guardrails are satisfied."""
    controller = KillSwitchController(state_file=tmp_path / "kill-switch.json")

    _validate_kraken_manual_submit_request(
        symbol="BTC/EUR",
        side="buy",
        quote_amount=3.5,
        enable_live_trading=True,
        live_confirmation=LIVE_TRADING_CONFIRMATION,
        submit_confirmation=KRAKEN_MANUAL_ORDER_CONFIRMATION,
        kill_switch_controller=controller,
    )

    assert controller.state_file.exists()


def test_validate_kraken_manual_close_request_requires_extra_confirmation() -> None:
    """Manual Kraken close should require its own exact confirmation token."""
    with pytest.raises(SystemExit, match="--kraken-close-confirmation"):
        _validate_kraken_manual_close_request(
            symbol="BTC/EUR",
            enable_live_trading=True,
            live_confirmation=LIVE_TRADING_CONFIRMATION,
            close_confirmation="not-it",
        )


def test_validate_kraken_manual_close_request_requires_inactive_kill_switch(tmp_path: Path) -> None:
    """Manual Kraken close should be blocked while the kill switch is active."""
    controller = KillSwitchController(state_file=tmp_path / "kill-switch.json")
    controller.activate("manual")

    with pytest.raises(SystemExit, match="kill switch is active"):
        _validate_kraken_manual_close_request(
            symbol="BTC/EUR",
            enable_live_trading=True,
            live_confirmation=LIVE_TRADING_CONFIRMATION,
            close_confirmation=KRAKEN_MANUAL_ORDER_CONFIRMATION,
            kill_switch_controller=controller,
        )


def test_validate_kraken_manual_close_request_allows_guarded_submit(tmp_path: Path) -> None:
    """Manual Kraken close should pass only when all guardrails are satisfied."""
    controller = KillSwitchController(state_file=tmp_path / "kill-switch.json")

    _validate_kraken_manual_close_request(
        symbol="BTC/EUR",
        enable_live_trading=True,
        live_confirmation=LIVE_TRADING_CONFIRMATION,
        close_confirmation=KRAKEN_MANUAL_ORDER_CONFIRMATION,
        kill_switch_controller=controller,
    )

    assert controller.state_file.exists()
