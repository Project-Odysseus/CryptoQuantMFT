# Aspirations: where CryptoQuantMFT is headed

What separates this project from a professional systematic trading operation, beyond capital and data, and in
what order to close the gaps. `TODO.MD` holds the next concrete tasks; this file holds the direction. Revisit it
every few months and move items into `TODO.MD` when they become next.

## Where we are (2026-09-27)

**Strong for its size**
- One research-to-live path for the portfolio: the same sleeve, allocation, netting, risk and order-planning
  functions run in the backtest and the runtime, with a parity test.
- Research hygiene that many small shops skip: fees, slippage and funding in every test, a holdout split, the
  deflated Sharpe, a scorecard with a placebo, and a research log that records failures.
- Live plumbing proven with real money on Kraken spot and Kraken Futures: idempotent client order ids, fills
  settled by id, reconciliation, a money cap, live gates, and a kill switch that acts on the exchanges by itself.
- Norwegian tax records built in from day one, and SQLite as the single record.
- About 22,000 lines in `src/`, 611 tests.

**Weak**
- **Two runtimes.** The older single-strategy runtime (spot) and the portfolio runtime (perps) overlap, and the older
  one carries most of the historical fixes and quirks.
- **One decision per bar close.** Everything runs as "fetch candles, decide at the close, send market orders". No
  intra-bar events, no timers, no order-book input, and no resting or maker orders on futures.
- **No resilience layer.** No HTTP retry/backoff, no dead-man's switch, no supervisor, no protective orders resting
  on the exchange, and a single process on one machine.
- **Accounting is partly estimated.** Futures fees and funding come from rates, not from Kraken's account log.
- **Strategy breadth is narrow.** The live book is three correlated BTC trend sleeves. Cross-sectional baskets,
  carry and options exist in research only.
- **`main.py` is 2,400 lines**, and some accounting still uses floats rather than `Decimal`.

## What "industry level" means, by area

Each area: what a professional operation has, where we are, and the next step. Roughly in priority order.

### 1. Operations: never be surprised by your own system

A professional desk knows within a minute when something is wrong, and it can always get flat.

- **Monitoring:** heartbeats to an external watchdog, metrics (cycle time, API latency and errors, equity, exposure)
  on a dashboard, tiered alerts (page vs. inform), and a daily report. *We have:* Telegram event alerts and a local
  dashboard. *Next:* a dead-man's switch and a daily summary; later, metrics to a time-series store with a web
  dashboard.
- **Resilience:** a supervisor restarts the process, calls are retried with backoff, and outages degrade
  gracefully. *Next:* retries on temporary Kraken errors, systemd with `Restart=always`, and a fault-injection soak
  test in CI.
- **Protection that survives the process:** stop orders resting on the exchange, so a dead process can't lose more
  than a set amount. *Next:* a reduce-only catastrophe stop per perp position (e.g. at 3x the ATR stop), placed and
  moved by the runtime.
- **An independent risk process:** a separate small process that watches positions and equity on the exchange and
  flattens if limits break, even if the strategy process is wrong or stuck. The kill switch's exchange sweep
  (`src/execution/emergency.py`) is its first building block.
- **Drills and postmortems:** kill switch, restart, network loss and key rotation, practised on a schedule; each
  incident written up (the `todo_important.md` history is the start of this).
- **Deployment:** CI runs the tests on every commit; releases are tagged; config changes are versioned and reviewed;
  secrets live outside the repository with IP-restricted keys.

### 2. Execution: pay less to trade

At our turnover, costs decide whether an edge survives (the intraday studies died on costs).

- **Order types:** post-only limits, maker-first with a taker fallback after a timeout, and IOC only when urgent.
  *Next:* maker-first for the portfolio's rebalances; a maker fill on Kraken Futures costs 0.02% instead of 0.05%.
- **Transaction cost analysis:** every fill compared with the decision price, the arrival price and the bar close,
  by venue, size and time of day. It feeds the slippage assumptions back into research. *Next:* store the decision
  and arrival prices with each order, and add a weekly TCA report.
- **Execution algorithms:** slicing large orders over time (TWAP/VWAP), participation limits, and routing across
  venues. Only needed once size matters.
- **Private WebSocket streams** for fills and positions instead of polling, once decisions go intra-bar.

### 3. Risk: know the book in every scenario

- **Exposure, not notional:** delta, gamma, vega and a stress grid per underlying (built in `exposure.py`), with
  limits in the risk overlay (`[risk.exposure]`, not yet built).
- **Live VaR / expected shortfall** and a drawdown budget per strategy, not only for the book.
- **Venue risk:** limits per exchange for collateral held there (FTX showed exchanges fail), and collateral moved
  between venues on rules.
- **Correlation-aware sizing:** crypto correlations go to 1 in crashes; size for the crash correlation, not the
  average.
- **Strategy kill criteria** set before going live: for example, stop a sleeve if its live Sharpe over 6 months falls
  below a threshold set from its backtest distribution, or its drawdown exceeds 1.5x the historical worst.

### 4. Research: find real edges, and know when they die

- **A final holdout never touched** during selection, used once per strategy family.
- **Walk-forward and purged cross-validation** for anything with fitted parameters; combinatorial tests of how
  often a selection procedure picks a winner.
- **Capacity and impact:** how the edge decays with size, from order-book depth and our own fills.
- **Live vs. backtest monitoring:** each sleeve's live P&L tracked against its backtest over the same days; a gap
  means a bug, a cost model error, or decay.
- **Faster iteration:** a feature store (point-in-time features computed once and cached), so a new idea is a
  notebook cell, not a script.

### 5. Data: point-in-time, complete, checked

- **One store** for bars, trades, order books, funding, open interest and option chains: partitioned Parquet with
  DuckDB is enough for years at our scale.
- **Point-in-time and survivorship-free:** listings, delistings and contract changes as of each date (the Binance
  archive already includes dead coins).
- **Quality checks:** gaps, outliers, stale feeds and exchange-time vs. receive-time, run daily with alerts.
- **Own recordings:** order books, trades and option chains recorded continuously from the server (the recorders
  exist; they need a machine and disk).

### 6. Portfolio construction

- **Risk-based allocation** across sleeves (volatility targeting at book level, risk parity, correlation caps),
  and an optimiser that trades off expected return, risk and trading cost with a turnover penalty.
- **Strategy lifecycle:** idea → research → paper → small live → scaled → reduced → retired, with rules for each
  step, and capital assigned by live evidence, not by backtest alone.

### 7. Accounting: one book of record, reconciled daily

- **Every cash flow from the exchange's own records:** fills, fees, funding and transfers from Kraken's account log,
  reconciled daily against the book; differences alert.
- **P&L attribution** by sleeve, instrument, and for options by greek (delta, gamma, vega, theta, residual).
- **An immutable audit trail:** every decision, order and fill with its reason and inputs (largely there in
  `operational_events` and the Telegram reasons).
- **Tax:** the yearly export checked against Kraken's statements, and the treatment of derivatives confirmed.

### 8. Software architecture: one engine for every strategy type

This is what makes complex strategies possible.

- **One event-driven engine.** Market data, fills, timers and signals are all events; strategies subscribe to what
  they need. Bar-close trend sleeves are one case. Intraday rules, order-book strategies, options structures with
  roll schedules, and basket strategies are others. Retire the single-strategy runtime into it.
- **A richer strategy interface:** a sleeve can target baskets (many instruments), exposures (delta or vega targets
  rather than weights), or structures (an option spread), not only one weight on one instrument.
- **Simulation at more than one fidelity:** bar-level (now), order-book replay with queue position for maker
  strategies, and the same strategy code in all of them.
- **Hygiene:** `main.py` split into command modules, `Decimal` everywhere money is booked, typed configs with
  validation, and a clean package boundary between research-only and production code.

### 9. Strategy breadth: where an edge can come from

A small operation can't win on speed, so market making and latency arbitrage are out. What is realistic:

- **Risk premia at low cost:** trend (live), carry (a regime trade), the volatility premium (options, after
  study O4), with careful costs and sizing.
- **Cross-sectional signals** across many coins (the taker-buy book held up in research): needs basket sleeves.
- **Alternative and structural data:** funding, open interest, liquidations, on-chain flows and option positioning,
  where a small team can be early.
- **Diversification** across uncorrelated sleeves matters more than any single sleeve's Sharpe.

## Suggested order

1. **Now (before scaling capital):** the pre-live checklist in `TODO.MD` section 1: soak, fault injection, restart
   drill, dead-man's switch, retries, then a supervised live start.
2. **Next months:** a server with deployment and backups; maker-first execution with TCA; account-log reconciliation;
   exposure limits; basket sleeves, so the cross-sectional book can run; a catastrophe stop on the exchange.
3. **Then:** the event-driven engine that replaces both runtimes; a data store with recordings; live-vs-backtest
   monitoring and kill criteria; the options track from research to testnet.
4. **Industry level:** an independent risk process, multi-venue collateral management, execution algorithms, a
   full research platform with a feature store, and a strategy lifecycle run on live evidence.
