"""Configuration helpers for the runtime orchestrator."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RuntimeConfig:
    """Runtime settings for a paper/live-style execution loop."""

    mode: str = "paper"
    strategy_name: str = "moving_average_crossover"
    strategy_params: dict[str, Any] = field(default_factory=dict)
    iterations: int = 3
    interval_seconds: float = 1.0
    use_mock_connector: bool = False
    watchdog_timeout_seconds: float = 5.0
    watchdog_restarts: int = 0
    exchange: str | None = None
    kill_switch: bool = False
    kill_switch_reason: str = "manual"

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "RuntimeConfig":
        """Create a runtime config from CLI arguments."""
        return build_runtime_config_from_args(args)


def build_runtime_config_from_args(args: argparse.Namespace) -> RuntimeConfig:
    """Build a runtime configuration object from CLI arguments."""
    strategy_params: dict[str, Any] = {}
    if getattr(args, "strategy_params", None):
        parsed_params = json.loads(args.strategy_params)
        if not isinstance(parsed_params, dict):
            raise ValueError("strategy_params must be a JSON object")
        strategy_params = parsed_params

    return RuntimeConfig(
        mode=args.runtime or "paper",
        strategy_name=args.strategy,
        strategy_params=strategy_params,
        iterations=args.runtime_iterations,
        interval_seconds=args.runtime_interval,
        use_mock_connector=args.use_mock_connector,
        watchdog_timeout_seconds=args.watchdog_timeout,
        watchdog_restarts=args.watchdog_restarts,
        exchange=None if args.execution_exchange == "auto" else args.execution_exchange,
        kill_switch=args.kill_switch,
        kill_switch_reason=args.kill_switch_reason,
    )
