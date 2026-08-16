# Extended Explanation of the CryptoQuantMFT Repository

This document explains the repository as a system, not just as a set of files. It is written so another agent can understand what exists today, how the components connect, and where new work should be added.

## 1. What this repository is

CryptoQuantMFT is a lightweight Python framework for experimenting with crypto trading logic in three modes:

- Backtesting: test strategies against historical bars or synthetic data.
- Paper trading: run a strategy against market snapshots and simulate fills without real money.
- Runtime/operational loop: run a paper or live-dry-run workflow with risk controls, monitoring, persistence, and Telegram alerts.

The project is intentionally modular and conservative. It is built to support research and safe operational testing, not yet a fully hardened production trading stack.

## 2. High-level architecture

At a high level, the system flows like this:

1. Market data enters through connectors or historical fetchers.
2. The data is normalized, stored, and aggregated into bars.
3. A strategy produces a signal.
4. Risk controls decide whether the signal is allowed to become a trade.
5. An execution engine simulates or routes an order.
6. Trade and equity data are persisted to the database.
7. Operational events and Telegram alerts are generated for monitoring.

The core runtime path looks like this:

```text
main.py
  -> RuntimeConfig / RuntimeOrchestrator
  -> MarketDataPipeline
      -> ExchangeConnector(s)
      -> MarketStore / StreamingAggregator
  -> Strategy (from backtest/runner or simple_backtest)
  -> RiskManager / CircuitBreaker / KillSwitchController
  -> PaperTradingEngine / ExecutionRouter
  -> TradeLogger + TelegramNotifier + SessionAccountStateTracker
```

## 3. Repository layout

Top-level files:

- main.py: the main CLI entrypoint for backtests, runtime runs, reports, dashboards, and the kill switch.
- kill_switch.py: a small wrapper that invokes the runtime kill-switch path.
- config.py: central Pydantic settings loader for environment-based configuration.
- config/runtime.yaml: static runtime config example.
- README.md: minimal project overview and runbook pointer.
- TODO.MD: product roadmap and development checklist.
- mermaid.md: Mermaid diagram of the main classes and relationships.
- docs/runbook.md: operational guidance for startup, backup, restore, and recovery.

Main source tree:

- src/data/: market data ingestion, connectors, historical data fetching, and pipeline orchestration.
- src/storage/: persistence and aggregation of ticks, bars, order books, and trade data.
- src/signals/: simple signal-generation components.
- src/risk/: risk controls, circuit breakers, and kill-switch logic.
- src/execution/: exchange execution adapters, router, reconciliation, and paper-trading engine.
- src/runtime/: runtime configuration and orchestrator loop.
- src/backtest/: backtesting, simulation, plotting, analytics, and walk-forward evaluation.
- src/utils/: logging, Telegram, and runtime telemetry helpers.

Tests:

- tests/ contains unit and integration tests covering data ingestion, storage, signals, execution, risk management, runtime, backtesting, and Telegram notifications.

## 4. Core components and what they do

### 4.1 Root configuration and environment

#### config.py

Purpose:
- Loads environment settings from .env and validates them using pydantic-settings.
- Centralizes application-wide configuration such as API keys, runtime log level, SQLite database path, Telegram credentials, and FX fallback values.

Key classes:
- Settings: a typed configuration object.

Importance:
- Almost every component depends on this module indirectly because settings are imported from config.

#### config/runtime.yaml

Purpose:
- Static example config for runtime metadata and basic defaults.

### 4.2 Main entrypoint

#### main.py

Purpose:
- The main entrypoint for the project.
- It wires together the runtime, backtest, paper-trading, dashboard, reporting, and kill-switch flows.
- It also parses CLI arguments and constructs the right runtime objects based on those arguments.

What it does:
- Builds a MarketDataPipeline and attaches connectors.
- Creates a TradeLogger and execution engine.
- Resolves a named strategy using the backtest StrategyRegistry.
- Creates a RuntimeOrchestrator and runs it.
- Supports optional report, dashboard, daily summary, and runtime-mode commands.

Important interactions:
- Imports from src.data, src.execution, src.runtime, src.backtest, src.storage, and src.utils.
- It is the orchestration root for the whole app.

### 4.3 Kill switch wrapper

#### kill_switch.py

Purpose:
- A tiny wrapper that invokes the runtime kill-switch feature through main.py.

Why it matters:
- It provides an operational emergency path that can neutralize the runtime without changing code.

## 5. Data ingestion layer

### 5.1 Market data connectors

#### src/data/exchanges.py

Purpose:
- Defines the abstraction for exchange data ingestion.
- Provides a normalized MarketTick object that downstream components can consume.

Key classes:
- MarketTick: a normalized market snapshot with exchange, symbol, timestamp, bid, ask, last, volume, and raw payload.
- ExchangeConnector: abstract interface for connectors.
- MockExchangeConnector: deterministic offline connector for tests and smoke runs.
- FiriConnector: connector for Firi market data using the configured API key.
- KrakenConnector: connector for Kraken market data.

How it works:
- A connector connects, fetches a market snapshot, persists it to the market store, and optionally updates the streaming aggregator.
- The base class provides shared request and persistence helpers.

How it links to the rest of the system:
- Connectors feed the MarketDataPipeline.
- They write ticks into MarketStore and update the StreamingAggregator.
- The runtime depends on them to create bars and signals.

#### src/data/pipeline.py

Purpose:
- Orchestrates a group of connectors against a shared storage/aggregation stack.

Key class:
- MarketDataPipeline

How it works:
- Holds a Store and a StreamingAggregator.
- Adds connectors to a list.
- Runs each connector once per cycle and collects results.

How it links to the rest:
- The runtime and main.py both use it to pull market data into the system.
- It is the bridge between ingestion and storage/aggregation.

#### src/data/fx.py

Purpose:
- Fetches FX rates such as EUR/NOK and caches them locally for fallback.

Key class:
- FXRateCollector

How it works:
- Attempts to fetch the current rate from an upstream provider.
- Falls back to a configured local default when the upstream source is unavailable.
- Stores the rate in a local SQLite cache.

How it links to the rest:
- Used by the cost model for FX-adjusted trading cost estimates.

#### src/data/historical.py

Purpose:
- Fetches historical OHLCV bars from Kraken.

Key functions:
- fetch_kraken_ohlcv

How it links to the rest:
- Used by main.py and the backtesting flow to obtain real historical bars.
- Produces OHLCVBar objects that can be used by the backtester.

## 6. Storage and aggregation layer

### 6.1 Market persistence

#### src/storage/market_store.py

Purpose:
- Stores market ticks in SQLite and exports them to Parquet.

Key class:
- MarketStore

What it does:
- Creates a local market-ticks table.
- Persists each market tick.
- Writes out a Parquet file for downstream offline analysis.
- Exposes methods to list recent ticks and to read Parquet rows.

How it links to the rest:
- Connectors save ticks into it.
- BarAggregator and StreamingAggregator use it as a shared source of market history.
- It is one of the main persistence primitives of the project.

### 6.2 OHLCV bar aggregation

#### src/storage/bar_aggregator.py

Purpose:
- Converts persisted market ticks into OHLCV bars.

Key classes:
- OHLCVBar: a single OHLCV bar with timestamp and volume.
- BarAggregator: builds OHLCV bars for a given interval.

How it works:
- Groups ticks by exchange/symbol/time bucket.
- Computes open/high/low/close/volume values.
- Supports a small set of intervals such as 1s, 1m, 5m, 1h, etc.

How it links to the rest:
- Used by the backtesting and research workflow.
- The streaming aggregator produces bars that can later be stored and reviewed.

### 6.3 Streaming aggregation

#### src/storage/streaming_aggregator.py

Purpose:
- Maintains rolling in-progress OHLCV bars for a live or near-live runtime.

Key classes:
- StreamingBar
- StreamingAggregator

How it works:
- Receives tick updates and updates the current bar for a symbol and interval.
- Finalizes the bar when a new bucket begins or when the pipeline flushes.

How it links to the rest:
- MarketDataPipeline creates it.
- Connectors update it.
- The runtime can consume completed bars to produce signals and trades.

### 6.4 Order-book reconstruction and market microstructure metrics

#### src/storage/order_book.py

Purpose:
- Defines simple L2-style order-book snapshots and reconstructs summary metrics.

Key classes:
- OrderBookSnapshot: a basic bid/ask depth snapshot.
- OrderBookMetrics: aggregate metrics including mid price, spread, micro-price, bid/ask volume, VWAP, and imbalance.
- OrderBookReconstructor: computes these metrics from snapshots.

How it links to the rest:
- It uses signal components from src.signals/order_book_signals.py.
- It can be used by simulation and signal-generation logic.

### 6.5 Data normalization

#### src/storage/normalizer.py

Purpose:
- Aligns bars from different exchanges onto a common representation.

Key classes:
- NormalizedBar
- DataNormalizer

How it links to the rest:
- Useful when data from multiple sources must have the same time alignment before signal generation or comparison.

### 6.6 Trade and operational logging

#### src/storage/trade_logger.py

Purpose:
- The main persistence layer for trades, equity snapshots, operational events, and daily summaries.

Key class:
- TradeLogger

What it stores:
- trades: executed or simulated trade records
- equity_snapshots: portfolio equity/cash/position snapshots
- operational_events: startup, alerts, risk events, runtime actions
- daily_summary_reports: a daily summary of activity for reporting

How it links to the rest:
- RuntimeOrchestrator and PaperTradingEngine both use it.
- The CLI report and dashboard commands read from it.
- It is a central source of truth for runtime review.

## 7. Signal layer

### 7.1 Order book signals

#### src/signals/order_book_signals.py

Purpose:
- Produces simple order-book-derived signals for short-term alpha ideas.

Key classes:
- OrderBookImbalanceSignal
- MicroPriceSignal
- VolumeDeltaSignal
- OrderBookSignalEngine

What they do:
- Compute imbalance, micro-price, and flow-pressure signals from top-of-book volume data.

How they link to the rest:
- They are consumed by order-book reconstruction and can be used by strategies or research workflows.

### 7.2 Volatility and trend filters

#### src/signals/volatility_signals.py

Purpose:
- Provides simple volatility and trend filters that can gate or modify signal strength.

Key classes:
- TrendFilterSignal
- VolatilityTrendFilter

How it links to the rest:
- Strategies can use this to avoid entering during periods of extreme volatility or to confirm trend direction.

## 8. Risk and safety layer

### 8.1 Risk configuration and decisions

#### src/risk/controls.py

Purpose:
- Defines the risk controls used before entries are accepted.

Key classes:
- RiskControlConfig: configurable thresholds for drawdown, volatility, slippage, spread, stale quotes, position sizing, etc.
- RiskDecision: the output of a risk evaluation.
- CircuitBreakerState: persisted emergency-stop state.
- CircuitBreaker: hard-stop controller.
- RiskManager: gating logic for new entries.

What it does:
- Rejects trades when volatility, drawdown, spread, slippage, stale data, position limits, or exchange caps are violated.
- Computes a position size based on risk parameters and volatility-adjusted sizing logic.

How it links to the rest:
- The paper-trading engine uses it before orders are placed.
- The runtime orchestrator also uses it in the cycle loop.
- It is one of the main safety layers before live or live-dry-run execution.

### 8.2 Kill switch

#### src/risk/kill_switch.py

Purpose:
- Provides a kill-switch controller that can neutralize the system when something goes wrong.

Key class:
- KillSwitchController

How it links to the rest:
- PaperTradingEngine and RuntimeOrchestrator both interact with it.
- It can prevent further entries and record the event to the trade logger.

## 9. Execution layer

### 9.1 Execution adapter abstractions

#### src/execution/adapters.py

Purpose:
- Implements the adapter abstraction used to route orders to different execution environments.

Key classes:
- ExecutionReport: the result of a submission or cancellation request.
- ExecutionOrder: a normalized order representation used by the runtime.
- ExecutionAdapter: abstract base implementation.
- SandboxExecutionAdapter
- ExchangeExecutionAdapter
- KrakenExecutionAdapter
- FiriExecutionAdapter
- LiveExecutionAdapter
- ExecutionRouter

How it works:
- The router selects the correct adapter based on the configured mode or exchange.
- Adapters encapsulate order submission, cancellation, status lookup, and reconciliation logic.

How it links to the rest:
- The runtime’s PaperTradingEngine is wired to an execution adapter through the router.
- The reconciliation tracker observes the adapter’s state.

### 9.2 Paper trading engine

#### src/execution/paper_trading.py

Purpose:
- Simulates a simple signal-driven trading loop without real capital.

Key classes:
- PaperOrder: simple order model with status and fill information.
- PaperTrade: a simulated fill event.
- PortfolioSnapshot: portfolio snapshot after each processing step.
- PaperTradingResult: aggregate result bundle of orders, trades, and portfolio history.
- PaperTradingEngine: main engine for paper trading.

How it works:
- Processes bars and signals.
- Applies risk checks.
- Creates orders.
- Simulates fills and updates cash, position size, and equity.
- Records trades and portfolio snapshots.

How it links to the rest:
- It depends on RiskManager, CircuitBreaker, KillSwitchController, CostModel, TradeLogger, and ExecutionAdapter.
- It is the central execution engine used by the runtime loop.

### 9.3 Reconciliation layer

#### src/execution/reconciliation.py

Purpose:
- Tracks account state and reconciliation results for a runtime session.

Key classes:
- ReconciliationEntry
- SessionAccountStateTracker

How it works:
- Tracks balances, positions, open orders, and reconciliation health.
- Compares local runtime state to adapter-reported state when available.

How it links to the rest:
- The runtime orchestrator creates it and uses it in the health report.
- It helps operationally verify that local state and exchange-reported state are aligned.

## 10. Runtime orchestration layer

### 10.1 Runtime configuration

#### src/runtime/config.py

Purpose:
- Encapsulates runtime-specific configuration such as mode, strategy, interval, restart policy, and state paths.

Key classes:
- RuntimeConfig

How it links to the rest:
- main.py and RuntimeOrchestrator both depend on it.
- It is the main way the runtime is parameterized from the CLI or from stored config files.

### 10.2 Runtime orchestrator

#### src/runtime/orchestrator.py

Purpose:
- Coordinates the full operational loop for the system.

Key classes:
- RuntimeCycleResult: summary of one cycle.
- RuntimeHealth: health snapshot.
- RuntimeWatchdogError
- RuntimeWatchdog
- RuntimeOrchestrator

What it does:
- Starts up the runtime.
- Validates connectors.
- Runs a loop over market data snapshots and bar updates.
- Generates signals.
- Passes signals to the execution engine.
- Records health snapshots, checkpoint state, alerts, and trade updates.
- Persists state so the runtime can be resumed.

How it links to the rest:
- Receives a MarketDataPipeline.
- Uses PaperTradingEngine, RiskManager, CircuitBreaker, KillSwitchController, TradeLogger, TelegramNotifier, and SessionAccountStateTracker.
- It is the operational heart of the project.

## 11. Backtesting and research layer

### 11.1 Strategy registry and runner

#### src/backtest/runner.py

Purpose:
- Provides strategy selection and configuration for backtests.

Key classes:
- BacktestConfig
- BacktestComparison
- StrategyRegistry

How it works:
- Registers built-in strategies and allows callers to resolve a strategy by name.
- Supports cost-model and risk-configuration wiring.

How it links to the rest:
- The main CLI and runtime use it to resolve strategies.
- It is the entrypoint for research workflows and runtime strategy selection.

### 11.2 Simple backtester

#### src/backtest/simple_backtest.py

Purpose:
- Runs a strategy over OHLC-like bars and produces a basic backtest result.

Key classes:
- TradeRecord
- BacktestResult
- SimpleBacktester

How it works:
- Applies a strategy to bars.
- Simulates entries and exits.
- Builds an equity curve and trade records.

How it links to the rest:
- It uses risk controls and cost models.
- It is used by the runner and the CLI backtest commands.

### 11.3 Performance analytics

#### src/backtest/analytics.py

Purpose:
- Computes core performance metrics such as Sharpe, Sortino, Calmar, win rate, profit factor, and expectancy.

Key classes:
- PerformanceMetrics
- build_performance_metrics

How it links to the rest:
- The SimpleBacktester uses it to enrich a BacktestResult.

### 11.4 Cost model

#### src/backtest/costs.py

Purpose:
- Models trading costs and FX costs.

Key classes:
- CostModel
- build_default_cost_model

How it links to the rest:
- PaperTradingEngine and the backtester both use it.
- It depends on FXRateCollector for FX-adjusted cost approximation.

### 11.5 Event-driven simulator

#### src/backtest/simulator.py

Purpose:
- Simulates order fills over a sequence of order-book snapshots.

Key classes:
- EventDrivenTrade
- PendingOrder
- EventDrivenSimulator

How it links to the rest:
- It uses order-book snapshots and cost models.
- It is a more execution-like research tool than the plain OHLCV backtester.

### 11.6 Walk-forward evaluation

#### src/backtest/walk_forward.py

Purpose:
- Evaluates a strategy over rolling train/test windows.

Key classes:
- WalkForwardFoldResult
- WalkForwardResult

How it links to the rest:
- It uses the StrategyRegistry and run_backtest function.
- It is used to evaluate robustness over time rather than on a single static sample.

### 11.7 Plotting

#### src/backtest/plotting.py

Purpose:
- Produces simple strategy plots for backtest results.

Key class:
- StrategyPlotter

How it links to the rest:
- Used by the CLI demo-backtest flow and by research/analysis workflows.

## 12. Utilities and observability

### 12.1 Logging

#### src/utils/logger.py

Purpose:
- Configures the global logging system.

What it does:
- Wires the Loguru logger to console output and rotating JSON file logs.
- Installs exception hooks for uncaught exceptions.

How it links to the rest:
- The entire codebase uses this logger for diagnostics.

### 12.2 Telemetry and reconnect handling

#### src/utils/telemetry.py

Purpose:
- Adds global exception hooks and a reconnect backoff helper for transient network issues.

Key classes:
- WebSocketReconnectHandler

How it links to the rest:
- Used by the runtime and any future live websocket-based connectors.

### 12.3 Telegram notifier

#### src/utils/telegram.py

Purpose:
- Sends Telegram alerts and structured trade-update messages.

Key class:
- TelegramNotifier

How it links to the rest:
- RuntimeOrchestrator uses it to send health alerts and trade updates.
- It depends on config settings for the bot token and chat ID.

## 13. Tests and validation

The tests folder covers all major components and is a good place to see expected behavior. Important groups include:

- Data tests: exchanges, pipeline, historical data, FX rates.
- Storage tests: market store, bar aggregation, streaming aggregation, trade logger.
- Signal tests: order book signals, volatility signals.
- Risk tests: risk controls, kill switch behavior, position sizing.
- Execution tests: adapters, paper trading, reconciliation.
- Runtime tests: runtime config, orchestrator loop.
- Backtest tests: runner, simple backtest, simulator, walk-forward, plotting.
- Telegram tests: notifier transport.

These tests are useful because they show the intended API and the expected interactions between components.

## 14. How the pieces are connected in practice

The most important relationships are:

- main.py is the top-level orchestrator.
- RuntimeOrchestrator is the runtime execution engine.
- MarketDataPipeline provides market data into the runtime.
- Exchange connectors populate the market store and streaming aggregator.
- The runtime uses a strategy to generate signals.
- The risk manager approves or rejects those signals.
- PaperTradingEngine turns accepted signals into simulated orders and trades.
- TradeLogger persists the resulting trades and equity snapshots.
- TelegramNotifier publishes operational updates and trade alerts.
- SessionAccountStateTracker summarizes account state and reconciliation status for monitoring.

## 15. Where new work should usually be added

If another agent is asked to add a feature, the likely touchpoints are:

- New data source or connector:
  - src/data/exchanges.py
  - src/data/pipeline.py
  - main.py

- New strategy:
  - src/backtest/simple_backtest.py
  - src/backtest/runner.py
  - main.py

- New risk control:
  - src/risk/controls.py
  - src/risk/kill_switch.py
  - src/execution/paper_trading.py

- New execution mode:
  - src/runtime/config.py
  - src/execution/adapters.py
  - src/runtime/orchestrator.py
  - main.py

- New notification channel:
  - src/utils/telegram.py or a new module under src/utils
  - src/runtime/orchestrator.py

- New persistence fields or reports:
  - src/storage/trade_logger.py
  - src/runtime/orchestrator.py
  - main.py

## 16. Current maturity level and important limitations

The repository is a working prototype and research scaffold. It already supports:

- basic connector-based market data ingestion
- storage and bar generation
- simple signal generation
- risk gating
- paper trading
- runtime orchestration
- trade/event persistence
- Telegram alerts
- daily summaries and dashboards

However, the system still has important gaps compared to a production platform:

- real WebSocket streaming is not the main transport path yet
- the execution layer is still lightweight and mostly paper-based
- the live deployment path is only partially hardened
- the signal stack is intentionally simple and not yet a full ML or regime framework
- the deployment/hosting path still needs a real unattended-service setup

## 17. Short summary

If you want the shortest possible mental model, think of the repo as this:

- data layer: ingest and normalize market data
- storage layer: persist market snapshots, bars, and trades
- signal layer: generate trading signals
- risk layer: decide whether signals are allowed
- execution layer: turn accepted signals into simulated trades or routed orders
- runtime layer: coordinate everything in a loop
- monitoring layer: log, summarize, alert, and report

That is the whole system in one sentence: ingest market data, turn it into signals, gate it with risk, simulate or route execution, persist the outcome, and monitor it.
