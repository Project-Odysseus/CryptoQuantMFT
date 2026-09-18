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
    trading_symbol: str | None = None
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
            "trading_symbol": self.trading_symbol,
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
            trading_symbol=payload.get("trading_symbol"),
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


def build_runtime_config_from_args(args: argparse.Namespace, argv: list[str] | None = None) -> RuntimeConfig:
    """Build a runtime configuration object from CLI arguments."""
    effective_argv = None if argv is None else list(argv)
    config_path = getattr(args, "runtime_config_path", None)

    loaded_config: RuntimeConfig | None = None
    if config_path and Path(config_path).exists():
        loaded_config = RuntimeConfig.load(config_path)

    strategy_params: dict[str, Any] = dict(loaded_config.strategy_params) if loaded_config is not None else {}
    if _argument_was_provided(effective_argv, "--strategy-params") or loaded_config is None:
        parsed_params = json.loads(getattr(args, "strategy_params", "{}") or "{}")
        if not isinstance(parsed_params, dict):
            raise ValueError("strategy_params must be a JSON object")
        strategy_params = parsed_params

    state_path = getattr(args, "runtime_state_path", None)
    if loaded_config is not None and not _argument_was_provided(effective_argv, "--runtime-state-path"):
        state_path = loaded_config.state_path
    if state_path is None and config_path is not None:
        state_path = str(Path(config_path).with_suffix(".state.json"))
    live_plot = bool(getattr(args, "live_plot", False))
    if loaded_config is not None and not _argument_was_provided(effective_argv, "--live-plot"):
        live_plot = bool(loaded_config.live_plot)
    live_plot_path = getattr(args, "live_plot_path", None)
    if loaded_config is not None and not _argument_was_provided(effective_argv, "--live-plot-path"):
        live_plot_path = loaded_config.live_plot_path
    if live_plot and not live_plot_path:
        live_plot_path = "plots/runtime_live_plot.png"

    runtime_config = RuntimeConfig(
        mode=_resolve_cli_value(
            effective_argv,
            "--runtime",
            getattr(args, "runtime", None),
            loaded_config.mode if loaded_config is not None else "paper",
        ),
        strategy_name=_resolve_cli_value(
            effective_argv,
            "--strategy",
            getattr(args, "strategy", None),
            loaded_config.strategy_name if loaded_config is not None else "moving_average_crossover",
        ),
        strategy_params=strategy_params,
        iterations=int(
            _resolve_cli_value(
                effective_argv,
                "--runtime-iterations",
                getattr(args, "runtime_iterations", 3),
                loaded_config.iterations if loaded_config is not None else 3,
            )
        ),
        interval_seconds=float(
            _resolve_cli_value(
                effective_argv,
                "--runtime-interval",
                getattr(args, "runtime_interval", 1.0),
                loaded_config.interval_seconds if loaded_config is not None else 1.0,
            )
        ),
        use_mock_connector=bool(
            _resolve_cli_value(
                effective_argv,
                "--use-mock-connector",
                getattr(args, "use_mock_connector", False),
                loaded_config.use_mock_connector if loaded_config is not None else False,
            )
        ),
        watchdog_timeout_seconds=float(
            _resolve_cli_value(
                effective_argv,
                "--watchdog-timeout",
                getattr(args, "watchdog_timeout", 30.0),
                loaded_config.watchdog_timeout_seconds if loaded_config is not None else 30.0,
            )
        ),
        watchdog_restarts=int(
            _resolve_cli_value(
                effective_argv,
                "--watchdog-restarts",
                getattr(args, "watchdog_restarts", 0),
                loaded_config.watchdog_restarts if loaded_config is not None else 0,
            )
        ),
        exchange=_resolve_exchange_value(
            effective_argv,
            getattr(args, "execution_exchange", "auto"),
            loaded_config.exchange if loaded_config is not None else None,
        ),
        trading_symbol=_resolve_cli_value(
            effective_argv,
            "--trading-symbol",
            getattr(args, "trading_symbol", None),
            loaded_config.trading_symbol if loaded_config is not None else None,
        ),
        kill_switch=bool(getattr(args, "kill_switch", False)),
        kill_switch_reason=_resolve_cli_value(
            effective_argv,
            "--kill-switch-reason",
            getattr(args, "kill_switch_reason", "manual"),
            loaded_config.kill_switch_reason if loaded_config is not None else "manual",
        ),
        live_plot=live_plot,
        live_plot_path=live_plot_path,
        config_path=config_path or (str(loaded_config.config_path) if loaded_config is not None and loaded_config.config_path is not None else None),
        state_path=state_path,
    )
    if config_path:
        runtime_config.save(config_path)
    return runtime_config


def _argument_was_provided(argv: list[str] | None, option: str) -> bool:
    if argv is None:
        return True
    return any(token == option or token.startswith(f"{option}=") for token in argv)


def _resolve_cli_value(argv: list[str] | None, option: str, cli_value: Any, loaded_value: Any) -> Any:
    if _argument_was_provided(argv, option):
        return cli_value
    return loaded_value


def _resolve_exchange_value(argv: list[str] | None, cli_value: str | None, loaded_value: str | None) -> str | None:
    if _argument_was_provided(argv, "--execution-exchange"):
        return None if cli_value == "auto" else cli_value
    return loaded_value
