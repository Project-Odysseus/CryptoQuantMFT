"""Tests for the runtime configuration helpers."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.runtime import RuntimeConfig, build_runtime_config_from_args


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


def test_runtime_config_round_trips_to_disk(tmp_path: Path) -> None:
    """Runtime config serialization should support save/load round-trips."""
    config_path = tmp_path / "runtime.json"
    runtime_config = RuntimeConfig(mode="paper", strategy_name="momentum_breakout", strategy_params={"lookback": 3}, config_path=config_path, state_path=tmp_path / "runtime.state.json")

    runtime_config.save(config_path)
    reloaded = RuntimeConfig.load(config_path)

    assert reloaded.mode == "paper"
    assert reloaded.strategy_name == "momentum_breakout"
    assert reloaded.strategy_params == {"lookback": 3}
    assert reloaded.state_path == str(tmp_path / "runtime.state.json")


def test_runtime_config_default_watchdog_timeout_is_more_forgiving() -> None:
    """The runtime should use a more forgiving default watchdog window."""
    runtime_config = RuntimeConfig()

    assert runtime_config.watchdog_timeout_seconds == 30.0
