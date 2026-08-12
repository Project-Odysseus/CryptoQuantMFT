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
- [ ] **Asynchronous Live Data Collectors**
  - [ ] Implement Kraken WebSocket L2 Order Book (`book-10` depth) client using `websockets` / `ccxt.pro`
  - [ ] Implement Firi L2 REST/WS polling client with rate-limit bucket throttling
  - [ ] Implement FX Rate Collector (EUR/NOK) with local cache and fallback mechanisms
- [ ] **Multi-Timeframe Resampler**
  - [ ] Build real-time streaming aggregator converting raw tick/orderbook streams into 1-second, 1-minute, 5-minute, and 1-hour OHLCV candle bars
- [ ] **Storage Infrastructure**
  - [ ] Setup `DuckDB` / `Parquet` pipeline for high-compression, microsecond-timestamp tick and bar storage
  - [ ] Build Data Normalizer to align non-synchronous timestamps across exchanges into uniform time-bar snapshots
  - [ ] Create order book reconstruction module (computing mid-price, micro-price, bid-ask spread, L2 VWAP depth)

---

## Phase 3: MFT Signal Research & Quantitative Feature Engineering
- [ ] **Order Book Imbalance (OBI) Signals (Micro-Execution)**
  - [ ] Calculate top-$N$ bid/ask volume imbalance ratio:
    $$OBI_t = \frac{\sum_{i=1}^N V_{i,t}^{\text{bid}} - \sum_{i=1}^N V_{i,t}^{\text{ask}}}{\sum_{i=1}^N V_{i,t}^{\text{bid}} + \sum_{i=1}^N V_{i,t}^{\text{ask}}}$$
  - [ ] Implement **Micro-Price Generator** weighting price by volume at top-of-book
  - [ ] Build Volume Delta / Trade Flow Toxicity indicators (VPIN approximation)
- [ ] **Lead-Lag Signal Engine**
  - [ ] Implement rolling cross-correlation model between global markets (Kraken BTC/EUR in NOK) and local exchange (Firi BTC/NOK)
  - [ ] Build momentum spillover detector (identifying global price movements $> X\sigma$ before local market adjustments)
- [ ] **Cross-Exchange Statistical Arbitrage & Mean Reversion**
  - [ ] Build Cointegration testing suite (Engle-Granger & Johansen tests) for adjusted exchange spreads
  - [ ] Implement rolling $Z$-score calculator on the adjusted FX/fee spread over 5m, 15m, and 1h lookbacks:
    $$Z_t = \frac{\text{Spread}_t - \mu_{\text{rolling}}}{\sigma_{\text{rolling}}}$$
  - [ ] Implement Ornstein-Uhlenbeck (OU) process parameter estimation for mean-reversion speed ($\theta$) and signal half-life calculation
- [ ] **Volatility Regime & Trend Filters**
  - [ ] Implement Average True Range (ATR) ratio and ADX filters to disable mean-reversion entries during strong directional momentum
  - [ ] Implement time-based signal decay functions to dynamically adjust position conviction as signal age increases
- [ ] **Machine Learning Signal Aggregator**
  - [ ] Train `xgboost` / `scikit-learn` classification model predicting $k$-step forward directional return using multi-timeframe OBI, $Z$-score, and Lead-Lag features
  - [ ] Implement Feature Importance and SHAP value analysis to filter uninformative signals

---

## Phase 4: Risk Management & Dynamic Position Sizing
- [ ] **Pre-Trade Risk Engine (Gatekeeper)**
  - [ ] **Slippage & VWAP Check:** Reject trades where L2 book depth is insufficient to execute without exceeding max allowed slippage
  - [ ] **Spread Guard:** Block entry if current bid-ask spread exceeds historical $95^{\text{th}}$ percentile
  - [ ] **Stale Quote Guard:** Disallow order placement if WS feed latency exceeds $X$ milliseconds
- [ ] **Position Sizing & Capital Allocation**
  - [ ] Implement Volatility-Adjusted Sizing (Half-Kelly Criterion or Fixed Fractional Risk)
  - [ ] Implement Dynamic Inventory Risk Manager: penalize buy signals if portfolio inventory is heavily long-skewed on one exchange
- [ ] **Post-Trade & Portfolio Risk Controls**
  - [ ] **Circuit Breakers:** Implement daily Max Drawdown limit (e.g., hard stop at -2% account equity drop in 24 hours)
  - [ ] **Time Stops:** Automatically liquidate/exit mean-reversion positions if $Z$-score fails to revert within $N$ half-lives
  - [ ] **Volatility-Based Stops:** ATR-driven trailing stop-loss logic
  - [ ] **Counterparty Risk:** Monitor and alert on asset balance distribution between Firi (NOK) and Kraken (EUR/BTC)

---

## Phase 5: Two-Tier Backtesting & Simulation Engine
- [ ] **Tier 1: Fast Vectorized Backtester**
  - [ ] Build Polars/VectorBT vectorized engine operating on 1m, 5m, and 1h OHLCV bars for rapid signal parameter exploration
- [ ] **Tier 2: Event-Driven L2 Tick Simulator**
  - [ ] Build event loop processing time-ordered L2 snapshots sequentially (eliminating look-ahead bias)
  - [ ] Implement realistic limit order fill simulator matching orders against recorded book depth and order flow
  - [ ] Inject artificial network latency delays between signal trigger and fill execution
- [ ] **Fee & Cost Modeling**
  - [ ] Model exact Maker vs. Taker fee tiers for Firi and Kraken
  - [ ] Model FX conversion spread costs (EUR/NOK)
- [ ] **Quantitative Performance Analytics**
  - [ ] Generate statistics: Sharpe Ratio, Sortino Ratio, Max Drawdown, Calmar Ratio, Win/Loss Ratio, Profit Factor, Expectancy
  - [ ] Build Out-of-Sample (OOS) testing framework and Walk-Forward Optimization (WFO) to eliminate overfitting

---

## Phase 6: OMS & Smart Order Router (Maker-First Execution)
- [ ] **Smart Order Router (SOR)**
  - [ ] Implement **Maker-First Strategy:** Place Post-Only limit orders at bid/ask to capture lower fees/rebates
  - [ ] Implement Order Timeout Guard: If Post-Only order remains unfilled after $T$ seconds and signal alpha remains high, dynamically route as a Taker order
- [ ] **Order Management System (OMS)**
  - [ ] Implement async state machine tracking full lifecycle: `PENDING_SUBMIT` $\rightarrow$ `OPEN` $\rightarrow$ `PARTIALLY_FILLED` $\rightarrow$ `FILLED` / `CANCELED`
  - [ ] Implement automated order timeouts and stale order cancellation routines
  - [ ] Build API reconnection recovery logic to reconcile local order state with exchange account state

---

## Phase 7: Record Keeper, Trade Analytics & Telemetry
- [ ] **Database Trade Logger (DuckDB / SQLite)**
  - [ ] Design relational database schema:
    - [ ] `trades` (id, timestamp, exchange, pair, side, price, amount, fee, role_maker_taker, latency_ms)
    - [ ] `signals` (id, timestamp, z_score, obi_value, lead_lag_delta, atr, model_confidence)
    - [ ] `equity_snapshots` (timestamp, total_equity_nok, firi_fiat, firi_crypto, kraken_fiat, kraken_crypto)
    - [ ] `errors` (timestamp, error_code, component, stack_trace)
- [ ] **Performance Analytics & Review Script**
  - [ ] Script to plot cumulative equity curve vs. Buy-and-Hold Bitcoin benchmark
  - [ ] Calculate trade duration distribution, execution slippage vs. expected VWAP, and alpha decay over time
- [ ] **Telemetry & Alerting System**
  - [ ] Integrate Telegram/Discord webhook bot for instant notifications:
    - [ ] Executed Trades (Asset, Side, Price, Role, PnL)
    - [ ] System warnings (WS disconnects, elevated latency)
    - [ ] Risk breaches / Circuit breaker triggers
    - [ ] Daily PnL and inventory summary at midnight UTC

---

## Phase 8: Production Deployment & System Safety
- [ ] **Emergency Infrastructure ("Panic Button")**
  - [ ] Build standalone `kill_switch.py` script: immediately cancels all open orders and liquidates active positions to base currency (NOK/EUR)
- [ ] **Containerization & Hosting**
  - [ ] Write `Dockerfile` and `docker-compose.yml` for unified runtime environment
  - [ ] Deploy to cloud VPS (Hetzner / DigitalOcean) physically close to exchange server regions
  - [ ] Configure `systemd` service or `supervisord` for automatic process management
- [ ] **Security Hardening**
  - [ ] Restrict API Key permissions on exchanges (Enable Trading, **Strictly Disable Withdrawals**)
  - [ ] Store keys encrypted using system environment variables via `pydantic-settings`