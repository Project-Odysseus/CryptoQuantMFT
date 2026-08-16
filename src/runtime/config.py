"""Configuration helpers for the runtime orchestrator."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
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
    watchdog_timeout_seconds: float = 30.0
    watchdog_restarts: int = 0
    exchange: str | None = None
    kill_switch: bool = False
    kill_switch_reason: str = "manual"
    live_plot: bool = False
    live_plot_path: str | Path | None = None
    config_path: str | Path | None = None
    state_path: str | Path | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "RuntimeConfig":
        """Create a runtime config from CLI arguments."""
        return build_runtime_config_from_args(args)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the runtime config to a JSON-compatible dictionary."""
        return {
            "mode": self.mode,
            "strategy_name": self.strategy_name,
            "strategy_params": self.strategy_params,
            "iterations": self.iterations,
            "interval_seconds": self.interval_seconds,
            "use_mock_connector": self.use_mock_connector,
            "watchdog_timeout_seconds": self.watchdog_timeout_seconds,
            "watchdog_restarts": self.watchdog_restarts,
            "exchange": self.exchange,
            "kill_switch": self.kill_switch,
            "kill_switch_reason": self.kill_switch_reason,
            "live_plot": self.live_plot,
            "live_plot_path": str(self.live_plot_path) if self.live_plot_path is not None else None,
            "config_path": str(self.config_path) if self.config_path is not None else None,
            "state_path": str(self.state_path) if self.state_path is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RuntimeConfig":
        """Deserialize a runtime config from a JSON payload."""
        return cls(
            mode=str(payload.get("mode", "paper")),
            strategy_name=str(payload.get("strategy_name", "moving_average_crossover")),
            strategy_params=dict(payload.get("strategy_params", {}) or {}),
            iterations=int(payload.get("iterations", 3)),
            interval_seconds=float(payload.get("interval_seconds", 1.0)),
            use_mock_connector=bool(payload.get("use_mock_connector", False)),
            watchdog_timeout_seconds=float(payload.get("watchdog_timeout_seconds", 30.0)),
            watchdog_restarts=int(payload.get("watchdog_restarts", 0)),
            exchange=payload.get("exchange"),
            kill_switch=bool(payload.get("kill_switch", False)),
            kill_switch_reason=str(payload.get("kill_switch_reason", "manual")),
            live_plot=bool(payload.get("live_plot", False)),
            live_plot_path=payload.get("live_plot_path"),
            config_path=payload.get("config_path"),
            state_path=payload.get("state_path"),
        )

    def save(self, path: str | Path) -> None:
        """Persist the runtime config to disk."""
        target_path = Path(path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)

    @classmethod
    def load(cls, path: str | Path) -> "RuntimeConfig":
        """Load a runtime config from disk."""
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return cls.from_dict(payload)


def build_runtime_config_from_args(args: argparse.Namespace) -> RuntimeConfig:
    """Build a runtime configuration object from CLI arguments."""
    strategy_params: dict[str, Any] = {}
    if getattr(args, "strategy_params", None):
        parsed_params = json.loads(args.strategy_params)
        if not isinstance(parsed_params, dict):
            raise ValueError("strategy_params must be a JSON object")
        strategy_params = parsed_params

    config_path = getattr(args, "runtime_config_path", None)
    state_path = getattr(args, "runtime_state_path", None)
    if state_path is None and config_path is not None:
        state_path = str(Path(config_path).with_suffix(".state.json"))
    live_plot = bool(getattr(args, "live_plot", False))
    live_plot_path = getattr(args, "live_plot_path", None)
    if live_plot and not live_plot_path:
        live_plot_path = "plots/runtime_live_plot.png"

    runtime_config = RuntimeConfig(
        mode=args.runtime or "paper",
        strategy_name=args.strategy,
        strategy_params=strategy_params,
        iterations=args.runtime_iterations,
        interval_seconds=args.runtime_interval,
        use_mock_connector=args.use_mock_connector,
        watchdog_timeout_seconds=args.watchdog_timeout,
        watchdog_restarts=args.watchdog_restarts,
        exchange=None if args.execution_exchange == "auto" else args.execution_exchange,
        kill_switch=bool(getattr(args, "kill_switch", False)),
        kill_switch_reason=getattr(args, "kill_switch_reason", "manual"),
        live_plot=live_plot,
        live_plot_path=live_plot_path,
        config_path=config_path,
        state_path=state_path,
    )
    if config_path:
        runtime_config.save(config_path)
    return runtime_config
