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


def test_runtime_config_loads_existing_file_and_applies_cli_overrides(tmp_path: Path) -> None:
    """Existing runtime config files should act as defaults until CLI options override them."""
    config_path = tmp_path / "runtime.json"
    RuntimeConfig(
        mode="paper",
        strategy_name="momentum_breakout",
        strategy_params={"lookback": 5},
        iterations=7,
        interval_seconds=2.0,
        use_mock_connector=True,
        watchdog_timeout_seconds=45.0,
        state_path=tmp_path / "runtime.state.json",
    ).save(config_path)

    args = argparse.Namespace(
        runtime="paper",
        strategy="moving_average_crossover",
        strategy_params="{}",
        runtime_iterations=3,
        runtime_interval=1.0,
        use_mock_connector=False,
        watchdog_timeout=30.0,
        watchdog_restarts=0,
        execution_exchange="auto",
        kill_switch=False,
        kill_switch_reason="manual",
        live_plot=False,
        live_plot_path=None,
        runtime_config_path=str(config_path),
        runtime_state_path=None,
    )

    runtime_config = build_runtime_config_from_args(
        args,
        argv=["--runtime", "paper", "--runtime-config-path", str(config_path), "--runtime-interval", "1.0"],
    )

    assert runtime_config.strategy_name == "momentum_breakout"
    assert runtime_config.strategy_params == {"lookback": 5}
    assert runtime_config.iterations == 7
    assert runtime_config.use_mock_connector is True
    assert runtime_config.watchdog_timeout_seconds == 45.0
    assert runtime_config.interval_seconds == 1.0
