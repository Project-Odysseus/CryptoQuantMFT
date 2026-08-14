"""Tests for the runtime configuration helpers."""

from __future__ import annotations

import argparse

from src.runtime import build_runtime_config_from_args


def test_runtime_config_parses_strategy_settings() -> None:
    """The runtime config helper should parse a JSON strategy options payload."""
    args = argparse.Namespace(
        runtime="paper",
        strategy="momentum_breakout",
        strategy_params='{"lookback": 3, "threshold": 0.02}',
        runtime_iterations=2,
        runtime_interval=0.5,
        use_mock_connector=True,
        watchdog_timeout=1.5,
        watchdog_restarts=1,
        execution_exchange="auto",
        kill_switch=False,
        kill_switch_reason="manual",
    )

    runtime_config = build_runtime_config_from_args(args)

    assert runtime_config.mode == "paper"
    assert runtime_config.strategy_name == "momentum_breakout"
    assert runtime_config.strategy_params == {"lookback": 3, "threshold": 0.02}
    assert runtime_config.iterations == 2
    assert runtime_config.use_mock_connector is True
