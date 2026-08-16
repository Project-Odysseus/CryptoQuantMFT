# Quantitative Crypto Trading Framework - Master Development Roadmap

A production-grade, event-driven quantitative trading system in Python designed for Medium-Frequency Trading (MFT) strategies (holding periods from seconds to 24 hours). Targets Lead-Lag, Order Book Imbalance (OBI), and Cross-Exchange Statistical Arbitrage across local (Firi) and global (Kraken/Binance) crypto exchanges.

---

## Phase 1: Core Architecture & Environment Setup
- [x] **Repository & Environment Isolation**
  - [x] Initialize Git repository with strict `.gitignore` (`.env`, `*.parquet`, `*.db`, `logs/`, `__pycache__/`)
  - [x] Configure `pydantic-settings` for type-safe environment configuration (`config.py`)
  - [x] Establish modular directory structure:
    ```
    ├── config/          # Yaml/Pydantic environment configurations
    ├── data/            # Local Parquet/DuckDB tick archives and OHLCV bars
    ├── src/
    │   ├── data/        # Collectors, WS listeners, multi-timeframe resamplers
    │   ├── signals/     # Alpha signal research & ML feature modules
    │   ├── risk/        # Dynamic risk, circuit breakers & inventory managers
    │   ├── backtest/    # Two-tier backtesting engine (Vectorized + Event-Driven)
    │   ├── execution/   # OMS, Smart Order Router (Maker-First) & adapters
    │   ├── storage/     # Trade Record Keeper & Database models
    │   └── utils/       # Logger, Math, VWAP, Metrics helpers
    ├── tests/           # Unit, integration, and offline mock tests
    └── main.py          # Main execution orchestrator
    ```
- [x] **Logging & Telemetry Setup**
  - [x] Configure `loguru` with asynchronous rotating file handlers (JSON formatted for analytics, colored stdout for live debugging)
  - [x] Implement global exception hooks and automatic WebSocket reconnect handlers

---

## Phase 2: Multi-Timeframe Data Pipeline & High-Frequency Storage Engine
- [x] **Asynchronous Live Data Collectors**
  - [x] Implement Kraken WebSocket L2 Order Book (`book-10` depth) client using `websockets` / `ccxt.pro` (implemented as a REST-based live connector scaffold for now; WebSocket transport remains a future enhancement)
  - [x] Implement Firi L2 REST/WS polling client with rate-limit bucket throttling (implemented as a live REST connector using the configured API key)
  - [x] Implement FX Rate Collector (EUR/NOK) with local cache and fallback mechanisms (scaffolded as part of the shared connector architecture; live FX integration remains future work)
  - [ ] Upgrade live market-data ingestion from REST polling to exchange WebSocket streams for Kraken/Firi with reconnect, resubscribe, and fallback handling for reliable live-paper and live trading
- [x] **Multi-Timeframe Resampler**
  - [x] Build real-time streaming aggregator converting raw tick/orderbook streams into 1-second, 1-minute, 5-minute, and 1-hour OHLCV candle bars
- [x] **Storage Infrastructure**
  - [x] Setup `DuckDB` / `Parquet` pipeline for high-compression, microsecond-timestamp tick and bar storage (implemented a local SQLite-backed snapshot store as the initial persistence layer)
  - [x] Build Data Normalizer to align non-synchronous timestamps across exchanges into uniform time-bar snapshots
  - [x] Create order book reconstruction module (computing mid-price, micro-price, bid-ask spread, L2 VWAP depth)

---

## Phase 3: MFT Signal Research & Quantitative Feature Engineering
- [x] **MVP Signal Bundle (Single-Exchange, Order-Book Driven)**
  - [x] Implement OBI / bid-ask imbalance signal
  - [x] Implement micro-price signal
  - [x] Implement volume-delta / flow-pressure signal
  - [x] Implement ATR-style volatility and momentum trend filter
- [ ] **Single-Exchange Mean Reversion & Regime Signals**
  - [ ] Build rolling $Z$-score calculator on a single-exchange spread/price series over 5m, 15m, and 1h lookbacks:
    $$Z_t = \frac{\text{Spread}_t - \mu_{\text{rolling}}}{\sigma_{\text{rolling}}}$$
  - [ ] Implement Ornstein-Uhlenbeck (OU) process parameter estimation for mean-reversion speed ($\theta$) and signal half-life calculation
  - [ ] Validate that the signal remains stable across regime changes and does not overfit to recent bars
- [ ] **Signal Validation Milestone**
  - [x] Ensure each signal has a simple test and an example plot/output
  - [x] Keep the signal stack lightweight and easy to reason about before expanding complexity
  - [x] Keep the backtest engine strategy-driven so a user-selected signal can be run on any supported time frame
  - [ ] Add signal robustness checks for volatility spikes, stale quotes, and missing data
- [ ] **Machine Learning Signal Aggregator**
  - [ ] Train `xgboost` / `scikit-learn` classification model predicting $k$-step forward directional return using multi-timeframe OBI, $Z$-score, and regime features
  - [ ] Implement Feature Importance and SHAP value analysis to filter uninformative signals
  - [ ] Keep a simple fallback rule-based signal path for live trading if ML features are unavailable

---

## Phase 5: Two-Tier Backtesting & Simulation Engine
- [x] **Tier 1: Strategy-Driven Backtesting Loop**
  - [x] Build a data-agnostic backtester that can run on any OHLC-like series (ticks, 1s, 1m, 5m, 1h)
  - [x] Support pluggable user-defined strategies instead of a single hard-coded signal loop
  - [x] Generate equity-curve and trade plots against time using recorded timestamps
  - [x] Add a simple strategy-selection interface so a user can choose a strategy by name/config rather than only by passing a Python callable
  - [x] Add richer risk controls such as volatility-adjusted sizing and max-drawdown guards
  - [x] Add a simple out-of-sample / walk-forward evaluation harness to test strategy robustness
- [x] **Tier 2: Event-Driven L2 Tick Simulator**
  - [x] Build a simple event loop that processes time-ordered L2 snapshots sequentially (eliminating look-ahead bias)
  - [x] Implement a lightweight fill simulator that matches orders against recorded book depth and order flow
  - [x] Inject simple artificial network latency delays between signal trigger and fill execution
  - [ ] Improve later with more realistic execution details: partial fills, queue position effects, adverse selection, and order-book slippage modeling
  - [ ] Compare simulator results against simple OHLCV backtests to ensure the execution assumptions are sensible
- [x] **Fee & Cost Modeling**
  - [x] Implement a simple first-pass fee model with approximate Maker/Taker fees for Firi and Kraken
  - [x] Apply fees to simulated trades in the backtest and paper-trading flow
  - [x] Model FX conversion spread costs (EUR/NOK) in a lightweight way
  - [ ] Improve later with exchange-specific fee schedules, tiered maker rebates, partial-fill fee handling, and more realistic FX spread modeling
- [ ] **Quantitative Performance Analytics**
  - [x] Generate a first-pass analytics layer: Sharpe Ratio, Sortino Ratio, Max Drawdown, Calmar Ratio, Win/Loss Ratio, Profit Factor, and Expectancy
  - [ ] Build Out-of-Sample (OOS) testing framework and Walk-Forward Optimization (WFO) to eliminate overfitting
  - [ ] Add benchmark comparison against simple buy-and-hold and random baselines

---

## Phase 4: Risk Management & Dynamic Position Sizing
- [x] **Pre-Trade Risk Engine (Gatekeeper)**
  - [x] **Slippage Check:** Reject entries when the current bar’s implied slippage exceeds the configured threshold
  - [x] **Spread Guard:** Block entries when the current bar’s spread exceeds the configured threshold
  - [x] **Stale Quote Guard:** Disallow entries when the quote cadence is older than the configured threshold
- [ ] **Position Sizing & Capital Allocation**
  - [x] Implement Volatility-Adjusted Sizing with a fractional Kelly sizing layer
  - [x] Implement Dynamic Inventory Risk Manager: penalize buy signals if portfolio inventory is heavily long-skewed on one exchange
  - [ ] Add dynamic sizing settings that can be tuned over time (for example, portfolio-risk percentage, max notional per trade, and regime-dependent sizing rules)
  - [ ] Add portfolio-level notional caps and per-exchange exposure limits before any live deployment
- [ ] **Post-Trade & Portfolio Risk Controls**
  - [ ] **Circuit Breakers:** Implement daily Max Drawdown limit (e.g., hard stop at -2% account equity drop in 24 hours)
  - [ ] **Time Stops:** Automatically liquidate/exit mean-reversion positions if $Z$-score fails to revert within $N$ half-lives
  - [ ] **Volatility-Based Stops:** ATR-driven trailing stop-loss logic
  - [ ] **Counterparty Risk:** Monitor and alert on asset balance distribution between Firi (NOK) and Kraken (EUR/BTC)
  - [x] Add a hard stop path that can neutralize the portfolio if live data or execution quality degrades

---

## Phase 6: OMS & Smart Order Router (Maker-First Execution)
- [ ] **Phase 6A: Paper-Trading Execution Skeleton**
  - [x] Build a lightweight paper-trading execution engine that accepts signals and records simulated fills
  - [x] Implement a simple order state machine for `PENDING_SUBMIT` $\rightarrow$ `OPEN` $\rightarrow$ `PARTIALLY_FILLED` $\rightarrow$ `FILLED` / `CANCELED`
  - [x] Record simulated trades, fees, and portfolio updates locally for strategy evaluation
  - [x] Apply the existing backtest risk controls (drawdown, volatility, and Kelly sizing) to paper-trading entries
  - [x] Add a simple reconciliation hook that compares local orders and fills with exchange-reported state before any live use
- [ ] **Phase 6B: Production Routing & OMS Hardening**
  - [ ] Implement **Maker-First Strategy:** Place Post-Only limit orders at bid/ask to capture lower fees/rebates
  - [ ] Implement Order Timeout Guard: If Post-Only order remains unfilled after $T$ seconds and signal alpha remains high, dynamically route as a Taker order
  - [x] Build API reconnection recovery logic to reconcile local order state with exchange account state
  - [x] Support testnet / sandbox routing so orders can be exercised without real capital

---

## Phase 7: Record Keeper, Trade Analytics & Telemetry
- [x] **Database Trade Logger (SQLite-backed trade/equity persistence)**
  - [x] Implement an initial SQLite logger for trades and equity snapshots
  - [x] Add a CLI report command to print recent trades and equity snapshots from the logger
  - [ ] Design a fuller relational schema for signals, errors, and richer portfolio columns
  - [x] Add operational event rows for startup, reconnects, order rejects, and risk events
- [ ] **Performance Analytics & Review Script**
  - [ ] Script to plot cumulative equity curve vs. Buy-and-Hold Bitcoin benchmark
  - [ ] Calculate trade duration distribution, execution slippage vs. expected VWAP, and alpha decay over time
  - [ ] Add cost-adjusted trade-level PnL reporting (fees, slippage, and benchmark comparison) to runtime diagnostics and summary exports
  - [x] Add a simple health dashboard output for runtime status, open orders, and live data freshness
- [ ] **Telemetry & Alerting System**
  - [ ] Integrate Telegram/Discord webhook bot for instant notifications:
    - [ ] Executed Trades (Asset, Side, Price, Role, PnL)
    - [ ] System warnings (WS disconnects, elevated latency)
    - [ ] Risk breaches / Circuit breaker triggers
    - [ ] Daily PnL and inventory summary at midnight UTC
  - [ ] Add a local fallback alert channel (e.g. log file / stdout) when external notification services are unavailable

---

## Phase 8: Production Deployment & System Safety
- [ ] **Emergency Infrastructure ("Panic Button")**
  - [x] Build standalone `kill_switch.py` script: immediately cancels all open orders and liquidates active positions to base currency (NOK/EUR)
  - [x] Add a safe command-line entrypoint that can be triggered manually or by the monitoring layer
- [ ] **Containerization & Hosting**
  - [ ] Write `Dockerfile` and `docker-compose.yml` for unified runtime environment
  - [ ] Add a 24/7 deployment plan for unattended operation: choose a small cloud VPS or managed container host, keep the runtime alive after reboot, and persist the SQLite/database state and runtime checkpoint files outside the container image
  - [ ] Add a home-server fallback plan for a Windows 10 desktop: keep the PC powered on 24/7, disable sleep/hibernate, ensure stable network access, and place the runtime on a persistent local drive with the trade database and runtime state outside the temporary user folders
  - [ ] Configure `systemd` service or `supervisord` for automatic process management
  - [ ] Configure Windows Task Scheduler or a small service wrapper so the runtime starts automatically after login/reboot and relaunches if it exits unexpectedly
  - [ ] Add a lightweight watchdog/heartbeat that restarts the runtime if the market-data feed, execution loop, or Telegram notifier stalls
  - [ ] Add remote log shipping and backup/sync for the runtime state and trade database to a second location
  - [ ] Deploy to cloud VPS (Hetzner / DigitalOcean) physically close to exchange server regions
  - [x] Add backup/restore steps for the local database and runtime state
- [ ] **Security Hardening**
  - [ ] Restrict API Key permissions on exchanges (Enable Trading, **Strictly Disable Withdrawals**)
  - [ ] Store keys encrypted using system environment variables via `pydantic-settings`
  - [ ] Document secrets rotation and incident-response procedures

---

## Phase 9: Live Trading Readiness & 24/7 Operations
This phase captures the remaining work needed to move from a prototype backtest/paper-trading engine to something that can plausibly run live crypto trading continuously with appropriate safety controls.

### Rollout Plan: Live Paper Trading + Parallel Signal Research
The recommended path is to run one conservative production-like paper-trading lane while keeping signal research in a separate experimental lane. This avoids mixing unverified strategies into the live-paper runtime and makes monitoring and decision-making clearer.

- [ ] **Lane A: Live Paper Runtime (Primary Production-Like Lane)**
  - [ ] Keep one stable baseline strategy active for paper trading only
  - [ ] Run the runtime using live market data with paper execution and the current risk controls
  - [ ] Enforce exchange-specific caps (position size, notional, open orders, open positions)
  - [ ] Maintain a single source of truth for runtime state: health report, trade logger, reconciliation status, and kill-switch state
  - [ ] Make the runtime easy to monitor with a clear CLI dashboard and a small operational checklist for each run
  - [ ] Use the same risk config and execution path every day so changes are comparable across sessions
  - [ ] Urgency: High

- [ ] **Lane B: Signal Research (Separate Experimental Lane)**
  - [ ] Run signal experiments in parallel, but outside the production-like runtime path
  - [ ] Keep research outputs in a separate backtest or notebook workflow, not in the live-paper runtime
  - [ ] Compare candidate strategies offline using walk-forward / out-of-sample evaluation before promoting them
  - [ ] Store research results with metadata: strategy name, parameters, dataset, metrics, and recommendation status
  - [ ] Use a simple promotion gate before any strategy is moved into the live-paper runtime
  - [ ] Urgency: Medium

- [ ] **Operational Workflow: Running Both Lanes Safely**
  - [ ] Create a daily operating checklist for: startup, runtime health, reconciliation status, open orders, drawdown, and kill-switch readiness
  - [ ] Maintain one “active strategy” config and one “research candidates” directory or registry
  - [ ] Separate runtime logs and research outputs so it is obvious which system is currently live-paper and which work is experimental
  - [ ] Add a simple status report that shows: active strategy, current mode, current risk config, recent trades, reconciliation health, and open research candidates
  - [ ] Urgency: High

- [ ] **Promotion / Demotion Process for Strategies**
  - [ ] Define a promotion rule set: strategy must pass offline validation, remain below drawdown thresholds in paper mode, and have no major reconciliation or execution issues
  - [ ] Define a rollback rule set: any unexpected risk breach, stale data event, or reconciliation mismatch immediately disables the active strategy in paper mode
  - [ ] Record the active strategy and risk config version for each paper run
  - [ ] Urgency: High

- [ ] **Monitoring & Visibility**
  - [ ] Build a single dashboard view that combines runtime health, account state, open orders, recent trades, and research queue status
  - [ ] Make anomalies obvious: stale quotes, reconciliation mismatches, circuit-breaker state, and kill-switch activation should be visible at a glance
  - [ ] Add a lightweight summary export or CLI report suitable for both live-paper monitoring and research review
  - [ ] Urgency: High

- [ ] **Staged Execution Path**
  - [ ] Paper mode first: live data, paper execution, no real capital exposure
  - [ ] Live dry-run second: sandbox/exchange-shaped routing with low-risk settings and human approval
  - [ ] Small live size third: only after stable paper performance and a manual sign-off checklist
  - [ ] Urgency: Medium

- [ ] **Execution Readiness Checklist**
  - [ ] Confirm exchange credentials, permissions, and sandbox/testnet access are configured
  - [ ] Confirm risk limits and kill-switch behavior are tested before each run
  - [ ] Confirm reconciliation and order-state reporting are healthy before enabling a strategy
  - [ ] Confirm operational logs, backups, and monitoring are available before a run starts
  - [ ] Urgency: High

### Suggested Priority Tasks
- [ ] **Priority 1: Build a simple strategy registry + runtime config switcher**
  - [ ] Add a registry of baseline and experimental strategies
  - [ ] Allow the runtime to select an active strategy from config or CLI without code edits
  - [ ] Keep the active strategy and research strategies separate in the workflow
  - [ ] Urgency: High

- [ ] **Priority 2: Add a consolidated operational dashboard/report**
  - [ ] Show runtime health, account state, reconciliation status, recent trades, and research candidate status in one view
  - [ ] Make the report easy to read in the terminal and suitable for quick monitoring during a run
  - [ ] Urgency: High

### Subtasks to Add Under Existing Bullet Points
- [ ] **Under Runtime Orchestration / Live Runtime**
  - [ ] Add a stable runtime config file for paper mode and a separate config file for research mode
  - [ ] Add a startup summary banner showing mode, strategy, exchange, risk limits, and current kill-switch state
  - [ ] Add a periodic health snapshot to the trade logger and CLI output
  - [ ] Urgency: High

- [ ] **Under Monitoring & Telemetry**
  - [x] Add a simple alerting channel for stale quotes, reconciliation mismatches, and risk events
  - [x] Add a Telegram notifier skeleton (configurable `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`, placeholder send hook) for future alert delivery
  - [x] Add a Telegram trade-summary payload for new trades with strategy, direction, PnL, PnL last hour, long/short, current max drawdown, distance to max drawdown point, and a placeholder diagnostics link
  - [ ] Add a heartbeat check for market-data feed and execution loop health
  - [ ] Add a persisted daily summary report for paper-trading performance and research queue status
  - [ ] Urgency: High

- [ ] **Under Deployment & Reliability**
  - [ ] Add a restart-safe state checkpoint for the active strategy and runtime config
  - [ ] Add a simple log rotation and backup policy for trade/logger state and runtime artifacts
  - [ ] Add a documented recovery procedure for restarting the paper runtime after a crash or disconnect
  - [ ] Urgency: Medium

- [ ] **Under Governance & Compliance**
  - [ ] Define an approval checklist for promoting a strategy from research to live-paper or live-dry-run
  - [ ] Record the strategy version, risk config version, and approval owner for each promoted run
  - [ ] Urgency: Medium

- [x] **Runtime Orchestration**
  - [x] Implement a single live-runtime loop that wires market data ingestion, signal generation, risk gates, execution routing, persistence, and monitoring together
  - [x] Support explicit runtime modes: `paper`, `live_dry_run`, and `live`
  - [x] Add startup health checks and graceful shutdown handling for reconnects and unexpected process exits
  - [x] Add a watchdog that can restart the runtime if the main loop stalls or the data feed dies
- [ ] **Exchange Execution Adapters**
  - [x] Add authenticated order submission / cancellation adapters for Kraken and Firi
  - [x] Support testnet / sandbox mode for safer validation before using real capital
  - [x] Add order-book and fill reconciliation logic so local order state can be matched to exchange state
  - [x] Add replay / recovery support so missed fills and rejected orders can be reconciled after reconnects
  - [ ] Add a reconciliation checklist for pre-run validation:
    - [ ] Confirm local order IDs match exchange order IDs
    - [ ] Confirm local filled size matches exchange filled size
    - [ ] Confirm local average fill price matches exchange reported price
    - [ ] Confirm local fees match exchange fees
  - [x] Add account state tracking for the current session:
    - [x] Track cash balances by currency and exchange
    - [x] Track open positions and position value by symbol
    - [x] Track unsettled orders and their current status
    - [x] Surface reconciliation status in the runtime health dashboard
- [ ] **Operational Safety & Risk Controls**
  - [x] Add account balance and inventory reconciliation against exchange state
  - [x] Add hard stop / circuit-breaker logic for daily drawdown, API failures, or stale market data
  - [x] Add a kill-switch command path that can cancel open orders and force liquidation/neutralization quickly
  - [x] Add per-exchange position limits, max notional, and max open-order caps
- [ ] **Monitoring & Telemetry**
  - [ ] Add heartbeat monitoring for the strategy loop, market-data feed, and exchange connectivity
  - [ ] Add alerts for stale quotes, order failures, API rate-limit issues, and unexpected risk events
  - [ ] Persist richer operational telemetry (startups, reconnects, order rejects, fill misses, runtime health)
  - [x] Add a simple live status endpoint or CLI report for current system health
- [ ] **Deployment & Reliability**
  - [ ] Containerize the runtime and add a production-friendly service definition for restart/recovery
  - [ ] Add log rotation, backup strategy, and state recovery for local databases and runtime checkpoints
  - [ ] Define a staged rollout plan: paper -> low-size live -> full live with human approval at each stage
  - [ ] Add a rollback path that can disable the strategy if a live run becomes unstable
- [ ] **Governance & Compliance**
  - [ ] Document API key permissions, secrets management, and runbooks for incident response
  - [ ] Define position limits, max notional, and per-exchange risk caps before enabling live trading
  - [ ] Add a manual override / approval workflow for any change to live strategy or risk settings
  - [ ] Record who approved each deployment and what risk limits were active at the time