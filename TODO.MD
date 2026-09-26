# Quantitative Crypto Trading Framework - Master Development Roadmap

A production-grade, event-driven quantitative trading system in Python designed for Medium-Frequency Trading (MFT) strategies (holding periods from seconds to 24 hours). Targets Lead-Lag, Order Book Imbalance (OBI), and Cross-Exchange Statistical Arbitrage across local (Firi) and global (Kraken/Binance) crypto exchanges.

---

## Current Critical Path: Make the Strategy Runtime Actually Runnable

This section captures the highest-priority gaps found by reviewing the docs, codebase, and current tests. The immediate goal is not to start trading yet; it is to make the repository reliably runnable for backtests, paper/runtime strategy execution, and Kraken live-dry-run preparation.

- [ ] **Priority 0: Reproducible Local Environment**
  - [x] Add a committed environment definition (`environment.yml`, `requirements.txt`, or `pyproject.toml`) covering the currently required runtime/test dependencies:
    - [x] `pydantic`
    - [x] `pydantic-settings`
    - [x] `loguru`
    - [x] `numpy`
    - [x] `pandas`
    - [x] `matplotlib`
    - [x] `pytest`
  - [ ] Declare and enforce the supported Python version for this repo
    - [ ] Either standardize on Python 3.10+ (current code uses `@dataclass(slots=True)`)
    - [ ] Or remove/replace the Python-3.10-only features if Python 3.9 support is required
  - [ ] Add a fast bootstrap path so a clean machine can install dependencies and run the baseline commands without guesswork
  - [ ] Add an environment validation command/checklist before runtime work begins

- [ ] **Priority 1: Clean Secrets and Runtime Bootstrap**
  - [ ] Replace real-looking values in `.env.example` with safe placeholders and rotate any exposed secrets outside the repository
  - [ ] Document the minimum required environment variables for:
    - [ ] demo backtests
    - [ ] mock runtime
    - [ ] Kraken live-dry-run
    - [ ] future live trading
  - [ ] Add a preflight checklist that confirms credentials/config are present without placing orders

- [ ] **Priority 2: Restore a Runnable Baseline**
  - [ ] Near-term execution focus: stabilize the current spot/Kraken strategy path before expanding the futures track
  - [x] Make the repository pass at least test collection in a clean environment
  - [x] Make these baseline commands work end-to-end:
    - [x] `python main.py --demo-backtest`
    - [x] `python main.py --walk-forward`
    - [x] `python main.py --runtime paper --use-mock-connector --runtime-iterations 3 --dashboard --report`
  - [ ] Add smoke tests for the CLI entry points above so “strategy runs at all” is continuously verified
  - [ ] Audit and fix stale imports, missing modules, and drift between tests and implementation before adding new strategy complexity

- [ ] **Priority 3: Close the Live-Execution Gap Safely**
  - [x] Break Priority 3 into tracked implementation subtasks in `todo_important.md`
  - [x] Implement the missing `live` runtime path in `RuntimeOrchestrator` instead of raising `NotImplementedError`
  - [ ] Keep the first live exchange milestone focused on **Kraken with explicit manual controls**
    - [x] exercise real Kraken authentication, order submission, cancel, and status lookup paths without enabling production trading
    - [x] add a manual quote-order preview command that validates tiny BTC/EUR orders by EUR notional without sending them
    - [x] add a guarded manual Kraken submit command for a first tiny EUR-notional order outside the strategy runtime
    - [x] add a guarded manual Kraken close-position command so the first live position can be exited without relying on the strategy runtime
    - [ ] confirm symbol mapping, balances, order IDs, and reconciliation shape against Kraken responses
      - [x] add a non-destructive closed-order history probe so real Kraken order payloads can be recovered and inspected without placing orders
      - [x] execute and inspect one tiny real Kraken round-trip so local state, persisted trades, and recovered exchange history can be compared end to end (2026-09-18 BTC/EUR buy+sell; found and fixed a silent tax-ledger failure — see `todo_important.md`); exchange-history/order-ID cross-check still outstanding
      - [ ] re-run verification when the Kraken account has at least one historical or open order so real symbol/order-id mapping can be confirmed from populated exchange data
    - [ ] verify kill-switch and cancel paths against exchange-shaped state before any real order is allowed
      - [x] add a non-destructive kill-switch preview against recovered Kraken open-order state
      - [ ] re-run that preview while at least one real Kraken open order exists so the cancel target list is validated against a populated exchange order book
  - [x] Add a hard safety gate so live trading cannot be enabled accidentally during development
  - [x] Add a manual approval checklist that must pass before moving from `paper` to `live_dry_run`, and from `live_dry_run` to `live`
  - [x] Subtask 1: define the `live_dry_run` execution path and missing seams
  - [x] Subtask 2: wire `live_dry_run` through the orchestrator and reconciliation stack
  - [x] Subtask 3: harden Kraken adapter symbol/account/order-state handling
  - [x] Subtask 4: add explicit safety gates for `--runtime live`
  - [x] Subtask 5: document operator approval checklists for promotion
  - [x] Subtask 6: add acceptance coverage for promotion and failure handling
  - [x] Subtask 7: implement the guarded `live` runtime path in the orchestrator
  - [ ] Execution plan:
    - [x] Phase A: adapter-level validation against Kraken private endpoints with read-only verification of authentication and non-destructive request/response handling
    - [x] Phase B: enable `live_dry_run` orchestrator flow to use the exchange adapter and reconciliation stack end to end without placing production orders
    - [x] Phase C: add explicit runtime guards for `--runtime live`, including a second confirmation mechanism, strict low-risk caps, and forced kill-switch readiness checks
    - [x] Phase D: add end-to-end acceptance tests for paper -> live_dry_run promotion and failure handling (stale data, order rejection, reconciliation mismatch, cancel failure)

- [ ] **Priority 3A: Norwegian Tax Logging Readiness Before Live Trading**
  - [x] Replace the current generic EUR/NOK spot-rate helper with a Norges Bank-backed daily FX source and local cache
    - [x] fetch official EUR/NOK middle rates from `https://data.norges-bank.no/api/data/EXR/B.EUR.NOK.SP?format=sdmx-json`
    - [x] store daily rates locally with trade-date lookup instead of only "latest rate" caching
    - [x] fall back to the last preceding business day for weekends and bank holidays
  - [x] Add a dedicated tax ledger schema for realized crypto and fiat tax events
    - [x] persist `id`, `timestamp_utc`, `transaction_type`, `symbol`, `amount_eur`, `norges_bank_fx_rate`, `amount_nok`, and `cost_basis_nok`
    - [x] keep tax-ledger writes separate from generic operational events so exports stay deterministic
  - [ ] Emit tax-ledger events at realization time instead of only relying on year-end summaries
    - [x] log realized trade PnL in EUR and NOK at close time
    - [x] log trading fees as separate taxable/deductible events
    - [ ] log funding fees once futures/perpetuals are enabled
    - [x] log EUR fiat conversions and maintain the running EUR cost basis for `valutagevinst/valutatap`
  - [x] Add year-end holdings valuation support for Norwegian wealth tax readiness
    - [x] snapshot crypto holdings and EUR balances as of Dec 31st 23:59 CET
    - [x] convert the holdings valuation to NOK for wealth-tax reporting
  - [x] Add export/reporting surfaces required before any real live rollout
    - [x] CSV/JSON export compatible with Koinly/Divly-style tax imports
    - [x] year-end Skattemeldingen summary covering gains, losses, fiat FX gain/loss, and Dec 31 holdings valuation
  - [x] Add acceptance coverage for tax-critical edge cases
    - [x] weekend/holiday FX fallback
    - [x] EUR pool cost-basis updates across multiple conversions
    - [x] realized PnL/fee logging from runtime execution records
  - [ ] Live-readiness rule: do not move from `live_dry_run` to real live trading until the tax-ledger path is implemented and validated end to end
    - [x] add manual EUR fiat-pool seeding via CLI before the first real Kraken trade
    - [x] add a manual Kraken preview flow that checks notional sizing, minimum order rules, and `validate=true` acceptance before the first live order
    - [x] add a guarded manual Kraken submit flow that can place the first live order while reusing preview sizing and confirmation gates
    - [x] add a guarded manual Kraken close-position flow that can exit the first live order with the same style of confirmation and validation gates
    - [x] seed the real EUR starting balance/cost basis before the first live order (done retroactively on 2026-09-25 after discovering it was skipped before the 2026-09-18 trade; fixed the CLI to surface this failure loudly going forward instead of relying on always seeding first)
    - [x] verify the first real Kraken fill and first close together produce correct `REALIZED_PNL` / `TRADING_FEE` / `FIAT_CONVERSION` rows and annual summary output (backfilled and verified for the 2026-09-18 round-trip; see `todo_important.md`)
    - [ ] export the first real trading year ledger and spot-check it against exchange history

- [ ] **Priority 3B: Manual Trading and Live-Control Closeout**
  - [x] add preview-only manual buy flow for EUR-notional Kraken spot orders
  - [x] add guarded manual buy submission with explicit live confirmations
  - [x] add preview-only manual close-position flow for Kraken spot holdings
  - [x] add guarded manual close submission with explicit live confirmations
  - [x] add a concise holdings/account-state CLI surface for balances, open orders, positions, and recent manual/live actions (2026-09-25: `--account-summary`, see `todo_important.md`)
  - [ ] validate partial-fill and open-order behavior with real Kraken order states instead of immediate-fill assumptions (2026-09-25: async *full*-fill reconciliation now validated and fixed live, see `todo_important.md`; genuine partial fills still unexercised)
  - [x] complete one tiny real Kraken round-trip and compare (2026-09-25, strategy-driven this time, not just manual — see `todo_important.md` for the full incident/fix record):
    - [x] manual preview output vs actual submitted size (true by construction — `submit_quote_order`/`submit_close_position` submit the exact `rounded_size` their own preview computed)
    - [x] remote Kraken order ids vs locally persisted order ids (cross-checked directly against `fetch_closed_orders()` while correcting the tax ledger — remote order ids and fills matched local trade rows exactly once corrected)
    - [x] post-trade balances and positions vs reconciliation snapshot
    - [x] trade logger rows vs Kraken history (cross-checked and corrected 2026-09-25 — found and fixed a real gap where reconciled fills logged `fee=0.0` instead of the real fee; see `todo_important.md`)
    - [x] tax-ledger rows vs actual fill and fee data (backfilled and verified 2026-09-25, see `todo_important.md`)

- [ ] **Priority 3C: Immediate next live-system milestones**
  - [x] prove one real Kraken round-trip end to end before any broader live rollout (2026-09-18 manual, 2026-09-25 strategy-driven — see `todo_important.md`)
  - [x] add a single-command holdings / portfolio / open-orders operator summary (2026-09-25: `--account-summary`)
  - [x] apply manual-order safety and exchange-rule enforcement consistently to strategy-driven live orders (2026-09-25: Kraken order-size/precision/balance parity, see `todo_important.md`)
  - [ ] harden real-order lifecycle handling for open orders, partial fills, delayed fills, and cancel/replace decisions (2026-09-25: open-order/delayed-fill handling fixed and validated live for full fills; partial fills and cancel/replace still open, see `todo_important.md`)
  - [x] keep the first live rollout limited to long/flat spot trading until these checks pass (still enforced: `allow_short=False` unconditionally for `--runtime live`)

- [ ] **Priority 4: Strategy Runtime Reliability**
  - [x] Treat paper mode as the source-of-truth validation lane before any live promotion
  - [x] Add a “known-good” baseline strategy/runtime config that is used for paper-mode validation every time
  - [x] Persist and surface enough context to debug blocked entries:
    - [x] selected strategy and params
    - [x] latest bars/signal
    - [x] risk decision reason
    - [x] execution adapter response
  - [ ] Add end-to-end paper-runtime acceptance criteria:
    - [ ] startup checks pass
    - [ ] snapshots persist
    - [ ] bars are produced
    - [ ] signals are generated
    - [ ] risk decisions are logged
    - [ ] orders/trades/equity snapshots persist
    - [ ] dashboard/report output remains readable
  - [ ] Make strategy-driven live execution match manual live controls:
    - [x] enforce exchange minimum order sizes and precision before strategy orders are submitted (2026-09-25: `KrakenExecutionAdapter.submit_order`, see `todo_important.md` — same checks as the manual preview/submit flows, unit-tested but not yet exercised against a real account)
    - [x] surface rejected-too-small / precision / balance failures explicitly in runtime health output (the rejection message flows through `_route_exchange_order` → `order.last_reason` → an `order_lifecycle` operational event, which the existing runtime health dashboard already lists under "Recent operational events" — not a dedicated summary field, but a specific, visible reason string per rejection)
    - [ ] define close/exit behavior clearly when the live strategy flips from buy to sell
    - [ ] confirm live strategy orders remain long/flat only for Kraken spot until shorting support is explicitly designed

- [ ] **Perpetual futures: first slice (2026-09-25)**: simulation only, see `docs/perpetual_futures.md`.
  - [x] Contract spec, margin math (equity, initial/maintenance margin, liquidation price) and `SandboxPerpExecutionAdapter` with leverage cap, funding accrual and liquidation (`src/execution/perps.py`)
  - [x] Engine support for margin accounts (margin equity, buying-power sizing, funding/liquidation events) and `RiskControlConfig.liquidation_buffer_pct`
  - [x] `--execution-exchange kraken_futures` (`live_dry_run` only) and `--perp-max-leverage`; `paper` and `live` refuse it
  - [x] Fixed: exchange-cycle stops (drawdown/time/ATR) never fired on shorts because no entry price was tracked for them
  - [x] Verified real contract spec, fee schedule and funding history from Kraken Futures' public API (2026-09-25): `--futures-venue-check`, `src/data/kraken_futures.py`. Funding is ~0.009%/day for BTC/ETH, a third of the earlier assumption
  - [ ] Product availability for the account holder (Norway) is not in the public data: confirm with Kraken
  - [x] Real Kraken Futures execution adapter (signed REST, IOC market orders with unique cliOrdId, reduce-only closes, fill reconciliation via /fills, position/margin sync) behind the live gates: futures credentials, verified contract, <=3x leverage, kraken_futures risk limits, kill-switch cancel-all (2026-09-26). Tested against recorded response shapes only; `--futures-verify-credentials` is the first real check
  - [x] Futures market-data feed: `KrakenFuturesConnector` prices perp runs on the contract mark price (2026-09-26); also fixed the runtime dashboard computing perp equity as cash + notional
  - [x] Persisted perp dry-run account (`data/perp_sandbox_<contract>.json`, `--perp-sandbox-reset`) and perp cash flows (realized PnL, fees, funding) in the tax ledger at Norges Bank USD/NOK for live runs (2026-09-26). Also fixed buying power ignoring the entry fee, which rejected max-size perp entries
  - [ ] Capture live funding and actual fees from Kraken's account log (fills carry neither), and confirm the Norwegian tax treatment of derivative flows
  - [ ] Kill-switch flatten (close the open position), not just cancel
- [ ] **Priority 5: Resolve the Incomplete Futures Track** (the older dated-futures scaffolding below is a different product from the perpetuals above)
  - [ ] Preferred sequencing: explicitly defer futures behind the spot/Kraken runtime unless there is a short-term need to trade futures first
  - [ ] Decide whether futures support is part of the near-term critical path or should be explicitly deferred behind the spot/Kraken runtime
  - [ ] If futures remains in scope, finish the incomplete futures implementation:
    - [ ] `src/execution/contracts/futures_contract.py`
    - [ ] `src/execution/contracts/registry.py`
    - [ ] `src/execution/sizing/contract_sizer.py`
    - [ ] `config/contracts.yaml`
  - [ ] Add the missing futures unit/integration tests instead of leaving placeholder `pass` test files
  - [ ] Resolve the stale futures integration reference to `src.portfolio.portfolio_manager_v2` by either implementing the portfolio module or replacing/removing the obsolete test path
  - [ ] Complete the unfinished sections in `docs/FUTURES_GUIDE.md` once the futures scope and APIs are real
  - [ ] Futures implementation plan:
    - [ ] Phase A: define the contract data model invariants and fill `config/contracts.yaml` with real contract examples used by tests
    - [ ] Phase B: implement registry load/list/validate behavior for YAML-backed contract lookup
    - [ ] Phase C: implement notional, rounding, and margin calculations in `FuturesContractSizer`
    - [ ] Phase D: decide how futures integrate with the existing execution/risk/runtime stack before adding portfolio-level behavior
    - [ ] Phase E: finish docs and replace placeholder tests with executable unit/integration coverage

- [ ] **Priority 5A: Resolve the stale `src.portfolio` reference**
  - [ ] Current finding: `tests/integration/test_futures_portfolio_integration.py` references `src.portfolio.portfolio_manager_v2`, but that module does not exist in the current tree or git history
  - [x] Stop the placeholder integration test from breaking the default suite while the futures track is deferred
  - [ ] Choose one resolution path and then implement it:
    - [ ] Option 1: add a minimal `src/portfolio/portfolio_manager_v2.py` only if futures work really needs a separate portfolio layer
    - [ ] Option 2: rewrite `tests/integration/test_futures_portfolio_integration.py` to target the current execution/risk/runtime architecture instead of a missing portfolio module
    - [ ] Option 3: explicitly defer that integration test behind a futures milestone and mark it accordingly so the baseline suite can run cleanly
  - [ ] Preferred path unless futures architecture changes: Option 2 or Option 3, because the repo already centers runtime state around `PaperTradingEngine`, `ExecutionAdapter`, `RiskManager`, and `SessionAccountStateTracker`
  - [ ] Add a follow-up test policy so placeholder integration tests do not break the default suite during unrelated runtime work

- [ ] **Priority 6: Keep Docs and Roadmap in Sync With Reality**
  - [ ] Update the architecture map and runbook whenever the runtime path, recovery flow, or live/dry-run behavior changes
  - [ ] Add a short “current maturity / what is safe to run” section to the roadmap or docs so future work does not assume live trading is already production-ready
  - [ ] Track readiness by milestone:
    - [x] baseline backtest runnable
    - [x] mock runtime runnable
    - [x] live-dry-run with Kraken runnable
    - [ ] small-size live rollout approved after paper and live-dry-run validation
    - [ ] live trading checklist complete

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
- [x] **Volume-confirmed momentum strategy** (2026-09-25): `volume_confirmed_momentum_strategy` (now in `src/backtest/strategies.py`), registered in `StrategyRegistry` as `volume_confirmed_momentum`. Only signals on a price momentum breakout (same math as `momentum_breakout`) when the triggering bar's volume also exceeds a multiple of the average of the *preceding* bars (excluding itself, to avoid diluting the spike baseline with the spike). Along the way, found and fixed a real data-quality bug: `KrakenConnector.fetch_snapshot()` was reporting Kraken's 24h-rolling ticker volume as if it were per-tick volume, which is nearly constant and would have made this signal a no-op live; it now tracks Kraken's cumulative today-volume counter across polls and reports the delta (with UTC-midnight-reset handling). Historical OHLC fetches (`fetch_kraken_ohlcv`, used for backtests) were unaffected — Kraken's OHLC endpoint already returns correct per-candle volume. Backtested against real Kraken BTC/EUR history (1-minute and 5-minute bars): the volume filter did what it was designed to do (fewer trades, better win rate, smaller drawdown than the unconfirmed `momentum_breakout` variant on the same window), but no strategy showed a real edge over this short (~2.5 day), calm sample — expected given the sample size, not a red flag. Not yet run live.
- [x] **Strategy research toolkit + six new strategies + first sensitivity sweep** (2026-09-25). Details: `docs/research_guide.md` (how to use and read it), `docs/research_log.md` (findings).
  - Strategy factories moved into `src/backtest/strategies.py`; vectorized causal indicators in `src/backtest/indicators.py`; `latch_position()` turns entry/exit rule arrays into a held position.
  - New registered strategies: `donchian_breakout`, `keltner_breakout`, `trend_tstat`, `volatility_squeeze`, `rsi_reversion`, `trend_pullback`.
  - `src/research/` (catalog with hypotheses and sweep grids, IS/holdout evaluation on mark-to-market equity, sweeps, robustness summary, heatmaps), `scripts/research/research.py` (`list`/`backtest`/`compare`/`sweep`, replaces `walk_forward_scan.py`), `notebooks/strategy_research.ipynb`, CSV loader for multi-year Kraken OHLCVT history.
  - Finding: costs (~1% round trip) decide most results. Mean reversion and short-horizon momentum lose net of costs. Slow trend-following is the only robust family, but it lags buy-and-hold in rallies and earns its keep in bear markets.
  - [ ] Re-run the sweep on multi-year history (Kraken OHLCVT CSVs) before trusting any candidate
  - [ ] Fix the remaining research ↔ runtime gaps tracked in `todo_important.md` (re-entry after a forced exit, 4h/1d bar interval + history warmup) before paper-trading a researched strategy. Exit-on-zero semantics fixed 2026-09-25.
  - [ ] Evaluate maker (post-only limit) execution: ~0.5% instead of ~1.0% round trip roughly doubles the 4h trend family's Sharpe (see Phase 6)
  - [ ] Cross-strategy return correlation in the research summary (needed for the portfolio stage)
- [ ] **Intraday-to-few-days strategy suite (requested 2026-09-26)**: holding periods from under an hour up to about 3 days, instead of the daily/weekly trend rules researched so far.
  - [ ] Research on 15m/1h/4h Kraken perp candles back to 2020 (`--source perp`, add 15m/1h to the research defaults), judged net of taker and maker costs; short horizons are where costs decided every result so far
  - [ ] Candidate ideas: time-of-day/session effects, opening-range and volatility breakouts with same-day exits, short-horizon reversal after outsized moves, funding-rate and basis signals, volume/flow bursts
  - [ ] Add a max-holding-period rule (e.g. exit after N bars or at a session boundary) so strategies stay intraday-to-few-days by construction
  - [ ] Maker (post-only) execution for entries, since intraday edge per trade is small relative to the 0.05% taker fee
  - [x] First pass (2026-09-26, `scripts/research/intraday_study.py`, `docs/research_log.md`): short-term reversal and a late-US-session effect are real but worth only a few bps; a walk-forward forecast combining 14 OHLCV features has Sharpe 1.2-1.7 before costs, loses at taker fees, is marginal at (optimistic) maker fees, and faded in 2025-26. Nothing to trade from price/volume alone at our fee tier
- [ ] **Research roadmap: data and models most likely to produce an edge (ranked, 2026-09-26)**
  - [x] **Realistic maker-fill modelling** (2026-09-26, `src/research/execution.py`, `--fills maker`): limit orders fill only when a later bar trades through them. It turned the intraday forecast's optimistic maker Sharpe of ~1.0 into 0.1-0.35 (ETH) and negative (BTC); daily trend strategies are unaffected
  - [x] **Positioning and funding data** (2026-09-26, `src/data/positioning.py`, `scripts/research/positioning_study.py`): public history, no keys needed, for funding (Binance/Bybit/Deribit), open interest (Binance/Bybit), Binance long/short ratios and taker flow, and Deribit DVOL. Crowding is contrarian at 1-3 days on both coins. An unfitted crowding composite (funding, OI change, long/short ratios) has out-of-sample IC +0.03-0.07 and positive Sharpe after taker costs in all 12 cases, but long/short is weak (0.3-0.6) and uneven by year. A fitted ridge over 21 features did worse. Not a standalone strategy; use it as an input (below)
    - [ ] Test crowding as a filter on the daily trend rules (skip or delay entries while longs are crowded)
    - [ ] Use funding/OI as ranking signals in the cross-sectional work and as timing for funding carry
    - [x] Liquidation history isn't published anywhere, so record liquidations live (with the order-flow recorder) before testing cascades (Binance and Bybit liquidations and Kraken Futures liquidation trades are in the recorder)
    - [ ] OKX serves only a few months of public funding and OI history, so it isn't in the study; add it to the live recorder
  - [x] **Record Kraken order-flow data now** (2026-09-26, `src/data/recorder.py`, `python main.py --record-market-data`, runbook section "Recording order-flow data"). It records Kraken Futures and spot trades with taker side, checked books sampled every second (top 10 levels, plus futures depth within 10-100 bps), perp tickers (funding, OI), and Binance/Bybit liquidations. Found along the way: Binance's legacy `/ws/!forceOrder@arr` path accepts connections but sends nothing, so the recorder uses `/market/ws/`
    - [ ] Keep it running (the history only becomes useful after weeks); check `--market-data-status` for gaps
    - [ ] After ~4 weeks: trade-flow imbalance, book imbalance and liquidation-burst features on 1m-1h bars, same study layout as `positioning_study.py`
    - [ ] Optional: move it to an always-on machine (a small VPS) so laptop sleep doesn't leave gaps
  - [ ] **Cross-sectional strategies across many perps** (Kraken lists hundreds): rank coins by recent return, funding or volume and hold the top vs bottom, market-neutral; needs the multi-asset portfolio backtester
    - [x] Data: `src/data/binance_archive.py`, every Binance USDT perp since 2020 including ~340 delisted ones (LUNA, FTT, ...), daily bars, funding and premium, resumable
    - [x] Portfolio backtester: `src/research/portfolio.py` (target weights per coin, fees, liquidity-based slippage, per-coin funding, delistings, long/short leg attribution), plus `rank_weights`, `liquid_universe`, `cross_sectional_ic`
    - [x] Study: `scripts/research/cross_sectional_study.py`, 8 features, IC and long/short portfolios, signs fixed in-sample, 2024+ holdout, `--kraken-only`; tested end to end on synthetic data
    - [ ] **On wifi:** `python scripts/research/cross_sectional_study.py --download` (about 50,000 small files; stopped 2026-09-26 on a mobile hotspot), then `--kraken-only`, then write up in `docs/research_log.md`
  - [ ] **Funding/basis carry** (long spot, short perp when funding is paid): measure net carry after fees across coins and exchanges before building; Kraken BTC/ETH funding averaged only ~3%/yr over the last year
  - [x] **Volatility forecasting for position sizing** (2026-09-26, `src/research/volatility.py`, `scripts/research/volatility_study.py`). HAR-RV is the most accurate forecast and EWMA is close; the runtime's 10-bar window is the worst and under-forecasts by ~1.8x. Sizing at entry to a volatility target lifts MA crossover and long/short Keltner beyond a shuffled-forecast placebo (Sharpe +0.07 to +0.36), not Keltner long-only or Donchian; resizing open positions and vol-timing buy-and-hold don't help. GARCH was skipped: HAR on intraday data is the stronger standard
    - [x] Runtime: opt-in `--target-annual-vol` sizes entries from an EWMA forecast as a share of equity (runbook "Position size"); found that the default sizing is really a fixed 10% of equity (`todo_important.md`)
    - [ ] HAR in the runtime (needs 1h bars and a year of history at startup); EWMA is used for now
  - [ ] **Calendar/event features**: US macro releases (CPI, FOMC), options expiries, CME weekend gaps
  - [ ] **Multiple-testing control**: we have now run thousands of configurations; add a deflated Sharpe or reality-check test and keep a never-touched final holdout
- [ ] **Needs exchange API keys (Binance / Bybit / OKX / Deribit)**: collected here so research never waits on keys. Everything so far used public data. Put keys in `.env` only, read-only first, with no withdrawal permission, and never print them. Check first that each venue offers derivatives to a Norwegian account.
  - [ ] Read-only keys: the fee tier each account actually gets (maker/taker, VIP level), to replace the assumed fees in carry and cross-sectional backtests
  - [ ] Trading keys and an execution adapter per venue: only after a strategy has passed paper and dry-run on that venue
  - [ ] Deribit: options positions and trading for the planned options strategies (options market data itself is public)
  - [ ] OKX: keys don't unlock more history; its funding and OI need live recording (add OKX to `config/market_data.json` feeds)

- [ ] **Single-Exchange Mean Reversion & Regime Signals**
  - [ ] Build rolling $Z$-score calculator on a single-exchange spread/price series over 5m, 15m, and 1h lookbacks:
    $$Z_t = \frac{\text{Spread}_t - \mu_{\text{rolling}}}{\sigma_{\text{rolling}}}$$
  - [ ] Implement Ornstein-Uhlenbeck (OU) process parameter estimation for mean-reversion speed ($\theta$) and signal half-life calculation
  - [ ] Validate that the signal remains stable across regime changes and does not overfit to recent bars
  - [ ] expand signal research from single-signal prototypes toward a production candidate set:
    - [ ] order book imbalance across several depths
    - [ ] microprice and spread-pressure features
    - [ ] volume-delta / aggressor-flow features
    - [ ] liquidity and volatility regime filters
    - [ ] multi-timeframe confirmation between fast and slow signals
- [ ] **Signal Validation Milestone**
  - [x] Ensure each signal has a simple test and an example plot/output
  - [x] Keep the signal stack lightweight and easy to reason about before expanding complexity
  - [x] Keep the backtest engine strategy-driven so a user-selected signal can be run on any supported time frame
  - [ ] Add signal robustness checks for volatility spikes, stale quotes, and missing data
  - [ ] add signal comparison and attribution reports so multiple signals can be ranked by edge, stability, turnover, and correlation
- [ ] **Machine Learning Signal Aggregator**
  - [ ] Train `xgboost` / `scikit-learn` classification model predicting $k$-step forward directional return using multi-timeframe OBI, $Z$-score, and regime features
  - [ ] Implement Feature Importance and SHAP value analysis to filter uninformative signals
  - [ ] Keep a simple fallback rule-based signal path for live trading if ML features are unavailable
  - [ ] defer ML promotion until the rule-based multi-signal stack has clear baselines and attribution

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
  - [ ] define trading rules for systematic live deployment:
    - [ ] max daily loss and hard stop escalation
    - [ ] cooldown after rejected orders or repeated stale data
    - [ ] max concurrent open orders
    - [ ] max holding time / time-stop rules for non-reverting setups
    - [ ] explicit rules for when a signal is ignored because another stronger signal already owns the position

- [ ] **Priority 4A: Multi-signal portfolio construction and crossing**
  - Context: see `docs/portfolio_architecture_assessment.md` (2026-09-25) — the current `RuntimeOrchestrator`/`PaperTradingEngine`/`RiskManager`/`TradeLogger` stack is single-strategy, single-symbol, single-adapter by construction, with no `strategy_id` attribution anywhere. The stated end goal is multiple strategies across spot/futures/derivatives/options with portfolio-level risk, so this section is a real architecture project, not a config toggle — read that doc before starting here.
  - [x] First step taken (2026-09-25): `trades.strategy_id` now exists (migrated in place) and `PaperTradingEngine`/`TradeLogger.log_trade` thread a `strategy_id` through on every trade, so future multi-strategy attribution won't need a historical backfill. This is groundwork only — no combiner, allocator, or per-strategy risk sleeve exists yet.
  - [ ] add a signal-combiner layer that converts many raw signals into one target position / target notional
  - [ ] support per-signal confidence, weighting, and attribution
  - [ ] net conflicting signals internally before routing orders to the exchange
  - [ ] support “no trade” outcomes when buy and sell pressure mostly cancel
  - [ ] add portfolio overview for multi-signal live trading:
    - [ ] current target vs actual position
    - [ ] per-signal desired exposure
    - [ ] netted exposure after crossing
    - [ ] recent execution decisions and skipped crossed trades
- [ ] **Post-Trade & Portfolio Risk Controls**
  - [x] **Circuit Breakers:** Implement daily Max Drawdown limit (2026-09-25: `RiskControlConfig.daily_loss_limit_pct` + `RiskManager.evaluate()` — resets at each calendar-day boundary, distinct from the existing all-time-peak `hard_stop_drawdown_pct`; wired into `PaperTradingEngine.run()` fully, and into `run_exchange_cycle()` for entry-gating only, not yet for a full-day-boundary reset check on every cycle since that path only sees the latest bar — see `todo_important.md`)
  - [x] **Time Stops:** Force-close a position that has been held past `time_stop_bars` regardless of the strategy's own signal (2026-09-25: `RiskManager.evaluate_exit()`, wired into both `PaperTradingEngine.run()` and the exchange-backed `run_exchange_cycle()` path — the latter needed a real persisted entry timestamp added to the adapter layer first, see `todo_important.md`)
  - [x] **Volatility-Based Stops:** ATR-driven stop-loss (2026-09-25: `RiskManager.evaluate_exit()` / `_estimate_atr()`, now wired into both `run()` and `run_exchange_cycle()`; not yet a *trailing* stop — it's a fixed distance from the entry price, not one that ratchets in the position's favor)
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
  - [x] Maintain a single source of truth for runtime state: health report, trade logger, reconciliation status, and kill-switch state
  - [ ] Make the runtime easy to monitor with a clear CLI dashboard and a small operational checklist for each run
  - [ ] Use the same risk config and execution path every day so changes are comparable across sessions
  - [ ] Urgency: High

- [ ] **Lane B: Signal Research (Separate Experimental Lane)**
  - [ ] Run signal experiments in parallel, but outside the production-like runtime path
  - [x] Keep research outputs in a separate backtest or notebook workflow, not in the live-paper runtime (2026-09-25: `src/research/`, `scripts/research/research.py`, `notebooks/strategy_research.ipynb`, outputs in `data/research/`)
  - [x] Compare candidate strategies offline using walk-forward / out-of-sample evaluation before promoting them (2026-09-25: in-sample/holdout split + parameter sweeps, `research.py compare/sweep`)
  - [ ] Store research results with metadata: strategy name, parameters, dataset, metrics, and recommendation status (partly: every run writes `results.csv` + `config.json` with parameters, dates and costs; recommendation status lives in `docs/research_log.md`, not in a registry)
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
  - [ ] Add Norwegian tax-operating controls: ledger completeness checks, yearly export procedure, and Dec 31 valuation checklist before any production rollout