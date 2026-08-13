"""Reusable plotting helpers for equity curves and trade annotations."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


class StrategyPlotter:
    """Create simple matplotlib charts from backtest-style data."""

    def __init__(self, output_dir: str | Path | None = None) -> None:
        self.output_dir = Path(output_dir or "plots")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def plot_equity_curve(self, equity_series: Sequence[float], *, title: str = "Equity Curve", output_path: str | Path | None = None) -> Path:
        """Plot a simple equity curve line chart."""
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(list(equity_series), color="#2563eb", linewidth=1.8)
        ax.set_title(title)
        ax.set_xlabel("Bar Index")
        ax.set_ylabel("Equity")
        ax.grid(alpha=0.3)
        fig.tight_layout()

        target_path = Path(output_path or self.output_dir / f"{title.lower().replace(' ', '_')}.png")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(target_path, dpi=150)
        plt.close(fig)
        return target_path

    def plot_trades(self, trade_prices: Sequence[float], *, title: str = "Trades", output_path: str | Path | None = None) -> Path:
        """Plot trade entries as vertical markers over a simple line of prices."""
        if not trade_prices:
            raise ValueError("At least one trade price is required")

        fig, ax = plt.subplots(figsize=(8, 4))
        prices = list(trade_prices)
        ax.plot(prices, color="#111827", linewidth=1.4)
        ax.scatter(range(len(prices)), prices, color="#ef4444", s=20, alpha=0.8)
        ax.set_title(title)
        ax.set_xlabel("Trade Index")
        ax.set_ylabel("Price")
        ax.grid(alpha=0.3)
        fig.tight_layout()

        target_path = Path(output_path or self.output_dir / f"{title.lower().replace(' ', '_')}.png")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(target_path, dpi=150)
        plt.close(fig)
        return target_path

    def plot_equity_and_trades(self, equity_series: Sequence[float], trade_prices: Sequence[float], *, title: str = "Equity and Trades", output_path: str | Path | None = None) -> Path:
        """Create a combined chart with an equity curve and trade markers."""
        fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
        axes[0].plot(list(equity_series), color="#2563eb", linewidth=1.8)
        axes[0].set_title(title)
        axes[0].set_ylabel("Equity")
        axes[0].grid(alpha=0.3)

        prices = list(trade_prices)
        axes[1].plot(prices, color="#111827", linewidth=1.4)
        axes[1].scatter(range(len(prices)), prices, color="#ef4444", s=20, alpha=0.8)
        axes[1].set_xlabel("Trade Index")
        axes[1].set_ylabel("Price")
        axes[1].grid(alpha=0.3)

        fig.tight_layout()
        target_path = Path(output_path or self.output_dir / f"{title.lower().replace(' ', '_')}.png")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(target_path, dpi=150)
        plt.close(fig)
        return target_path
