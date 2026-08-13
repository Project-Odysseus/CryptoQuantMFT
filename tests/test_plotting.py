"""Tests for the plotting helpers."""

from __future__ import annotations

from pathlib import Path

from src.backtest import StrategyPlotter


def test_strategy_plotter_writes_png_files(tmp_path: Path) -> None:
    """The plotter should generate simple chart images to disk."""
    plotter = StrategyPlotter(output_dir=tmp_path)

    equity_path = plotter.plot_equity_curve([100.0, 101.0, 102.0], title="Equity Curve")
    trade_path = plotter.plot_trades([100.0, 101.0, 102.0], title="Trades")

    assert equity_path.exists()
    assert trade_path.exists()
    assert equity_path.suffix == ".png"
    assert trade_path.suffix == ".png"
