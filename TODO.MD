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
  - [ ] Add portfolio-level notional caps and per-exchange exposure limits before any live deployment
- [ ] **Post-Trade & Portfolio Risk Controls**
  - [ ] **Circuit Breakers:** Implement daily Max Drawdown limit (e.g., hard stop at -2% account equity drop in 24 hours)
  - [ ] **Time Stops:** Automatically liquidate/exit mean-reversion positions if $Z$-score fails to revert within $N$ half-lives
  - [ ] **Volatility-Based Stops:** ATR-driven trailing stop-loss logic
  - [ ] **Counterparty Risk:** Monitor and alert on asset balance distribution between Firi (NOK) and Kraken (EUR/BTC)
  - [ ] Add a hard stop path that can neutralize the portfolio if live data or execution quality degrades

---

## Phase 6: OMS & Smart Order Router (Maker-First Execution)
- [ ] **Phase 6A: Paper-Trading Execution Skeleton**
  - [x] Build a lightweight paper-trading execution engine that accepts signals and records simulated fills
  - [x] Implement a simple order state machine for `PENDING_SUBMIT` $\rightarrow$ `OPEN` $\rightarrow$ `PARTIALLY_FILLED` $\rightarrow$ `FILLED` / `CANCELED`
  - [x] Record simulated trades, fees, and portfolio updates locally for strategy evaluation
  - [x] Apply the existing backtest risk controls (drawdown, volatility, and Kelly sizing) to paper-trading entries
  - [ ] Add a simple reconciliation hook that compares local orders and fills with exchange-reported state before any live use
- [ ] **Phase 6B: Production Routing & OMS Hardening**
  - [ ] Implement **Maker-First Strategy:** Place Post-Only limit orders at bid/ask to capture lower fees/rebates
  - [ ] Implement Order Timeout Guard: If Post-Only order remains unfilled after $T$ seconds and signal alpha remains high, dynamically route as a Taker order
  - [ ] Build API reconnection recovery logic to reconcile local order state with exchange account state
  - [ ] Support testnet / sandbox routing so orders can be exercised without real capital

---

## Phase 7: Record Keeper, Trade Analytics & Telemetry
- [x] **Database Trade Logger (SQLite-backed trade/equity persistence)**
  - [x] Implement an initial SQLite logger for trades and equity snapshots
  - [x] Add a CLI report command to print recent trades and equity snapshots from the logger
  - [ ] Design a fuller relational schema for signals, errors, and richer portfolio columns
  - [ ] Add operational event rows for startup, reconnects, order rejects, and risk events
- [ ] **Performance Analytics & Review Script**
  - [ ] Script to plot cumulative equity curve vs. Buy-and-Hold Bitcoin benchmark
  - [ ] Calculate trade duration distribution, execution slippage vs. expected VWAP, and alpha decay over time
  - [ ] Add a simple health dashboard output for runtime status, open orders, and live data freshness
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
  - [ ] Build standalone `kill_switch.py` script: immediately cancels all open orders and liquidates active positions to base currency (NOK/EUR)
  - [ ] Add a safe command-line entrypoint that can be triggered manually or by the monitoring layer
- [ ] **Containerization & Hosting**
  - [ ] Write `Dockerfile` and `docker-compose.yml` for unified runtime environment
  - [ ] Deploy to cloud VPS (Hetzner / DigitalOcean) physically close to exchange server regions
  - [ ] Configure `systemd` service or `supervisord` for automatic process management
  - [ ] Add backup/restore steps for the local database and runtime state
- [ ] **Security Hardening**
  - [ ] Restrict API Key permissions on exchanges (Enable Trading, **Strictly Disable Withdrawals**)
  - [ ] Store keys encrypted using system environment variables via `pydantic-settings`
  - [ ] Document secrets rotation and incident-response procedures

---

## Phase 9: Live Trading Readiness & 24/7 Operations
This phase captures the remaining work needed to move from a prototype backtest/paper-trading engine to something that can plausibly run live crypto trading continuously with appropriate safety controls.

- [x] **Runtime Orchestration**
  - [x] Implement a single live-runtime loop that wires market data ingestion, signal generation, risk gates, execution routing, persistence, and monitoring together
  - [x] Support explicit runtime modes: `paper`, `live_dry_run`, and `live`
  - [ ] Add startup health checks and graceful shutdown handling for reconnects and unexpected process exits
  - [ ] Add a watchdog that can restart the runtime if the main loop stalls or the data feed dies
- [ ] **Exchange Execution Adapters**
  - [ ] Add authenticated order submission / cancellation adapters for Kraken and Firi
  - [ ] Support testnet / sandbox mode for safer validation before using real capital
  - [ ] Add order-book and fill reconciliation logic so local order state can be matched to exchange state
  - [ ] Add replay / recovery support so missed fills and rejected orders can be reconciled after reconnects
- [ ] **Operational Safety & Risk Controls**
  - [ ] Add account balance and inventory reconciliation against exchange state
  - [ ] Add hard stop / circuit-breaker logic for daily drawdown, API failures, or stale market data
  - [ ] Add a kill-switch command path that can cancel open orders and force liquidation/neutralization quickly
  - [ ] Add per-exchange position limits, max notional, and max open-order caps
- [ ] **Monitoring & Telemetry**
  - [ ] Add heartbeat monitoring for the strategy loop, market-data feed, and exchange connectivity
  - [ ] Add alerts for stale quotes, order failures, API rate-limit issues, and unexpected risk events
  - [ ] Persist richer operational telemetry (startups, reconnects, order rejects, fill misses, runtime health)
  - [ ] Add a simple live status endpoint or CLI report for current system health
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