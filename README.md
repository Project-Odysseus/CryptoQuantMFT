# CryptoQuantMFT

CryptoQuantMFT is a Python-based quantitative trading framework for researching, backtesting, and operationally testing crypto strategies with a strong emphasis on system design, risk controls, and runtime safety.

It is built as more than a notebook project: the repository models the full path from market-data ingestion to signal generation, trade simulation, persistence, monitoring, and recovery.

## Why this project stands out

- **End-to-end trading architecture**: data pipeline, strategy layer, risk controls, execution, persistence, and operational tooling live in one codebase.
- **Research + runtime coverage**: supports both offline strategy evaluation and paper/live-dry-run style runtime workflows.
- **Risk-aware design**: includes drawdown guards, volatility-aware sizing, stale-quote checks, spread/slippage gates, and a kill-switch path.
- **Operational thinking**: runtime checkpoints, health reporting, reconciliation hooks, backups, and reporting are treated as first-class concerns.
- **Modular engineering**: components are organized so connectors, strategies, execution adapters, and risk logic can evolve independently.

## What this demonstrates

This repository is a strong portfolio project for roles spanning:

- quantitative developer / quant engineering
- algorithmic trading infrastructure
- Python backend engineering
- systems-oriented software engineering
- data-driven product prototyping

In practical terms, it shows the ability to:

- design layered, testable Python systems
- build stateful runtime workflows instead of one-off scripts
- model real-world constraints around execution, observability, and failure recovery
- balance experimentation with operational safeguards

## Current capabilities

### Research and backtesting
- Synthetic and exchange-sourced backtests
- Strategy selection through a central registry
- Walk-forward evaluation support
- Event-driven L2-style simulator for sequential market replay
- Performance analytics and plotting

### Runtime and execution
- Paper-trading workflow and live-dry-run routing
- Exchange connector scaffolding for Kraken and Firi
- Execution routing and sandbox adapter support
- Trade logging, equity snapshots, and operational event persistence
- Session/account reconciliation hooks

### Risk and safety
- Volatility-aware position sizing
- Drawdown and exposure controls
- Spread, slippage, and stale-quote entry guards
- Watchdog-driven runtime monitoring
- Manual kill switch and resumable runtime state

## Architecture at a glance

```text
Market data connectors
  -> Market storage + streaming aggregation
  -> Strategy / signal evaluation
  -> Risk manager
  -> Paper trading engine / execution router
  -> Trade logger + health reporting + checkpoint state
```

For deeper repository walkthroughs:

- Operational runbook: [docs/runbook.md](docs/runbook.md)
- Architecture map: [docs/architecture-map.md](docs/architecture-map.md)
- Extended system explanation: [extended_Explanation.md](extended_Explanation.md)

## Repository layout

```text
main.py                  CLI entry point for backtests, runtime flows, reports, and kill switch
config.py                Environment-based settings loader
src/data/                Connectors, historical data, FX collection, and pipeline orchestration
src/storage/             SQLite/parquet persistence, aggregation, and trade logging
src/backtest/            Backtesting engine, analytics, plotting, and walk-forward evaluation
src/risk/                Risk controls, sizing logic, and kill-switch handling
src/execution/           Paper trading, adapters, routing, and reconciliation
src/runtime/             Runtime configuration and orchestration loop
src/utils/               Logging, notifications, and telemetry helpers
tests/                   Unit and integration coverage across the main subsystems
```

## Example workflows

Run the test suite:

```bash
pytest
```

Run a demo backtest:

```bash
python main.py --demo-backtest
```

Run walk-forward evaluation:

```bash
python main.py --walk-forward
```

Run a deterministic paper-runtime smoke test:

```bash
python main.py --runtime paper --use-mock-connector --runtime-iterations 3 --dashboard --report
```

Show recent persisted runtime activity:

```bash
python main.py --report --report-limit 20
```

## Project positioning

CryptoQuantMFT is best understood as a **well-structured trading systems prototype**: ambitious in scope, realistic about safety, and intentionally modular so that research, execution, and operational hardening can progress in parallel.

That combination makes it useful both as an engineering foundation and as a portfolio artifact that communicates system-level thinking.
