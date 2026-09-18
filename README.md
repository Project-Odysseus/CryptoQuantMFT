# CryptoQuantMFT

CryptoQuantMFT is a Python trading-framework prototype for researching, backtesting, paper-running, and carefully validating a future Kraken live path. The current source of truth is **paper mode**, with **Kraken `live_dry_run`** as the promotion lane and explicit safety gates around real `live` mode.

## Current state

- **Stable today:** demo backtests, walk-forward evaluation, paper runtime, runtime health reporting, reconciliation, checkpoints, and kill-switch controls
- **Validated against Kraken without trading:** private-endpoint auth, balances, open/closed order lookups, status/cancel probes, validate-only order requests, kill-switch preview, and manual quote-order preview
- **Now in place for go-live prep:** Norwegian tax-ledger foundation with Norges Bank EUR/NOK rates, FIFO EUR cost-basis tracking, yearly summary/export, and year-end holdings valuation
- **Not yet signed off for production trading:** populated real-order reconciliation and post-trade tax-ledger validation against actual Kraken fills

## What the repo can do

- **Research/backtesting:** synthetic and Kraken-sourced backtests, walk-forward runs, L2-style event simulation, analytics, and plotting
- **Runtime/execution:** `paper`, `live_dry_run`, and guarded `live` runtime modes; exchange-shaped adapters for Kraken/Firi; persistence for trades, equity, events, and runtime state
- **Risk/safety:** volatility-aware sizing, drawdown/exposure limits, spread/slippage/staleness guards, watchdog monitoring, reconciliation, and kill-switch controls
- **Tax readiness:** Norwegian tax-event storage for EUR-quoted live trades, EUR fiat-pool tracking, annual summary export, and year-end wealth snapshot support

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

Run the committed baseline paper-runtime config:

```bash
python main.py --runtime paper --runtime-config-path config/runtime.paper.json --dashboard --report
```

Run the exchange-shaped Kraken dry-run promotion lane:

```bash
python main.py --runtime live_dry_run --execution-exchange kraken --runtime-iterations 3 --dashboard --report
```

Run the non-destructive Kraken verification before any live promotion:

```bash
python main.py --kraken-verify-dry-run --kraken-verify-symbol BTC/EUR
```

This checks private-endpoint auth, balances, open/closed orders, status/cancel handling, and a `validate=true` order request without placing a real order.

Preview a small Kraken BTC/EUR buy by EUR notional without sending it:

```bash
python main.py --kraken-preview-order --kraken-preview-symbol BTC/EUR --kraken-preview-quote-amount 3
```

This fetches live pair rules, current price, your EUR balance, rounds the BTC size to Kraken precision, and only runs `AddOrder` with `validate=true`.

Seed the EUR fiat pool before the first real Kraken trade:

```bash
python main.py --tax-log-fiat-eur 1000 --tax-fx-rate 11.50 --tax-reference initial_capital
```

Print the Norwegian tax summary and optionally export the ledger:

```bash
python main.py --tax-report --tax-year 2026 --tax-export-path exports/tax_2026.csv
```

`--runtime live` is now guarded on purpose: it requires `--enable-live-trading`, the exact confirmation token `--live-confirmation ENABLE_LIVE_TRADING`, an explicit non-auto `--execution-exchange`, and a ready/inactive kill-switch state before the CLI will even attempt the live path.

Show recent persisted runtime activity:

```bash
python main.py --report --report-limit 20
```

## Path to first safe live trade

1. Keep paper mode stable and rerun Kraken verification.
2. Seed the EUR fiat pool for tax basis tracking.
3. Run a manual `--kraken-preview-order` for the intended tiny order size.
4. Execute a small real Kraken trade later.
5. Immediately verify reconciliation, order IDs, fees, and tax-ledger rows from that real fill.

Until step 5 is checked, treat the project as **pre-live but close**.
