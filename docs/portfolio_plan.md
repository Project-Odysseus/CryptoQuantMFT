# Portfolio, multi-strategy and multi-asset plan

A step-by-step plan for running several strategies on several coins and venues as one portfolio. It is written so
that you, or any coding agent, can pick it up cold and continue. It doesn't depend on who wrote the earlier steps.
Each step says which files to touch, what to build, how to test it, and how to tell it's done.

Status as of 2026-09-26: Phases 0-4 are done. A portfolio config runs in paper with
`main.py --runtime paper --portfolio PATH --runtime-iterations 0`, on live Kraken candles, through a cross-margin
sandbox, with snapshots, a dashboard and alerts. Phase 5 (a multi-day paper/dry-run soak, ideally on an always-on
server) is next (see the tracker in section 10).

---

## 0. Read this first: how to work on this plan

**Before you start**
- Read `CLAUDE.md` (project rules), then sections 1-4 here, then the step you are about to do.
- Set up the environment: `source ~/anaconda3/etc/profile.d/conda.sh && conda activate CryptoArb`, and run
  `pytest` once to see a green baseline.
- `src/data/` matches a `.gitignore` pattern. New files there need `git add -f`.

**Rules that always apply** (from `CLAUDE.md` and the user)
- Paper mode is the source of truth, then `live_dry_run`, then live.
- Never run `--runtime live`, `--kill-switch`, or the manual Kraken submit/close flows unless the user explicitly
  asks.
- Commit locally after each step with a conventional message (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`). Add
  no `Co-authored-by:` trailer, and never `git push`; the user pushes.
- No bulk downloads or order-flow recording while the user is on a mobile hotspot. Ask, or add it to `TODO.MD` as
  "on wifi".
- If something needs Binance/Bybit/OKX/Deribit API keys, add it to the "Needs exchange API keys" section of
  `TODO.MD` instead of blocking. Never print key values.
- Use `decimal.Decimal` for money that leaves the process (order sizes, prices sent to an exchange, tax ledger).
  Use floats and numpy inside calculations.
- Network code is async and must fail gracefully per venue.
- Give every public function type hints and a Google-style docstring that explains *why*.
- When runtime behaviour changes, update `docs/runbook.md` and `docs/architecture-map.md` in the same commit.

**The loop for every step**
1. Read the step, and the existing files it names (open them; don't guess their APIs).
2. Write the tests for the step's "done when" list, then the code.
3. Run `pytest` (the full suite must stay green) and the step's **Verify** command.
4. Update the docs the step names. Tick the step in section 10 here and the matching line in `TODO.MD`.
5. Commit. One step per commit, or a few commits if the step is large.

**If you get stuck:** write what you found under the step as "Findings", tick nothing, commit the notes, and move to
the next step that doesn't depend on it. A documented dead end beats an undocumented workaround.

---

## 1. Goal

Define a portfolio in **one config file** and run it with **one command**, in research, paper, dry-run and
eventually live, with the same logic everywhere.

```bash
python scripts/research/portfolio_backtest.py config/portfolio.example.toml          # research backtest of the whole book
python main.py --portfolio-check config/portfolio.example.toml                       # validate the file and print the resolved plan
python main.py --runtime paper --portfolio config/portfolio.example.toml --dashboard # run it
```

A portfolio is made of **sleeves**. A sleeve is one strategy, with its parameters, on one instrument (venue +
symbol), at one bar interval, with a budget (its share of the portfolio's risk or capital). Examples:
- `btc_trend_1d`: `moving_average_crossover(4, 48)` on Kraken Futures BTC perp, daily bars, 40% budget;
- `eth_keltner_ls`: `keltner_breakout(40, 2)` long/short on the ETH perp, daily bars, 30% budget;
- `btc_trend_4h`: `moving_average_crossover(8, 96)` on the BTC perp, 4h bars, 30% budget.

**What "done" looks like for the user**
- Adding or removing a strategy or coin means editing the config file only, not code.
- The dashboard shows each sleeve's target, each instrument's net position, and P&L per sleeve and per instrument.
- A research backtest of the same file matches the paper run's decisions on the same bars.
- A bad config fails at startup with a message that says exactly what to fix.
- A restart resumes the same positions and decisions without duplicate orders.

**Not in scope yet:** high-frequency or order-book strategies, cross-venue arbitrage, options (a later phase; see
7.6), heavy optimisation of allocation weights.

---

## 2. Design principles (keep these in mind the whole way)

1. **Target weights everywhere.** Every sleeve outputs a *target weight*: a signed share of portfolio equity for
   its instrument (0.2 = long 20% of equity, -0.1 = short 10%). Everything downstream turns target weights into
   trades. This is the same contract as `src/risk/sizing.py` (a share of equity, never units) and
   `src/research/portfolio.py` (weights per coin per day).
2. **Pure core, thin shell.** Allocation, netting, risk caps and order planning are pure functions: data in, data
   out, no network, no database, no clock. Runtime, research and tests call the same functions. The shell
   (connectors, adapters, logger, orchestrator) only moves data in and out.
3. **Research/runtime parity.** A research backtest and a paper run fed the same bars must produce the same target
   weights. Test this explicitly (step 2.7).
4. **Net before trading.** If two sleeves want +0.3 and -0.1 of BTC, the book trades to +0.2 once. Each sleeve's
   P&L is still attributed from its own virtual position.
5. **Stateful, incremental accounting.** The current paper mode replays the whole bar history through the engine on
   every cycle. The portfolio engine must instead keep a book (positions, cash, fees, funding) and update it
   incrementally each cycle, in paper mode too, via sandbox adapters. That is what makes restarts, multi-symbol
   runs and long runs cheap and correct.
6. **Fail safe.** Stale data on an instrument means no new risk there. An unknown or unreconciled state means no
   new orders anywhere until it's resolved. A breached portfolio limit de-risks everything.
7. **Config-driven and validated.** Every setting has a sensible default. Every mistake gets a precise error at
   startup, never mid-run.
8. **Observable.** Every order, fill, target and decision can be traced to a sleeve (`strategy_id` = sleeve id) in
   SQLite through `TradeLogger`, the source of truth for reports.
9. **Small steps.** Each step leaves the system working and tested. The single-strategy runtime keeps working until
   the portfolio runtime has replaced it in paper and dry-run.

---

## 3. What exists today (2026-09-26)

| Piece | Where | State |
| --- | --- | --- |
| Strategies and registry | `src/backtest/strategies.py`, `src/backtest/runner.py` (`StrategyRegistry`), `src/research/catalog.py` (`build_strategy`) | Every strategy has a vectorised `signal_series(bars)` giving -1/0/1 per bar |
| Sizing | `src/risk/sizing.py` (`build_sizer`, `SIZERS`) | fixed_fraction, fixed_notional, vol_target, atr_risk, kelly; all return a share of equity |
| Risk gates and stops | `src/risk/controls.py` (`RiskManager`, `RiskControlConfig`, `DEFAULT_EXCHANGE_RISK_LIMITS`) | Per single position: drawdown, daily loss, volatility gate, ATR/time/drawdown stops, liquidation buffer, re-entry gate |
| Research portfolio backtester | `src/research/portfolio.py` (`simulate_portfolio`, `rank_weights`, `liquid_universe`) | Daily bars only; weights per coin per day, fees, slippage by liquidity, funding, delistings |
| Single-strategy engine | `src/execution/paper_trading.py` (`PaperTradingEngine`) | One symbol. `run()` (paper) replays all bars each cycle; `run_exchange_cycle()` (dry-run/live) is incremental over an adapter |
| Execution adapters | `src/execution/adapters.py` (Sandbox, Kraken, Firi), `src/execution/perps.py` (`SandboxPerpExecutionAdapter`), `src/execution/kraken_futures_adapter.py` | One adapter per engine; orders take a `symbol` argument |
| Orchestrator | `src/runtime/orchestrator.py` (`RuntimeOrchestrator.run_cycle`) | One strategy, one trading symbol; filters bars to `trading_symbol` |
| Market data | `src/data/exchanges.py` connectors, `src/data/pipeline.py` (`MarketDataPipeline`, `drain_completed_bars`) | Pipeline accepts several connectors; the orchestrator uses one symbol |
| Persistence | `src/storage/trade_logger.py` (`trades.strategy_id` exists), runtime checkpoints `data/runtime_state*.json` | No portfolio tables yet |
| Wiring | `main.py` (`build_runtime_orchestrator`) | Single strategy, single symbol |

**Known limitations to design around**
- The orchestrator keeps one bar history for one symbol, and paper `run()` replays it every cycle.
- `RiskManager` gates one position (`position_open` blocks a second entry) and has no portfolio view.
- `simulate_portfolio` assumes daily bars (it annualises with 365).
- Kraken spot can't short. Spot sleeves must be long-only, or the net target clamps at 0.
- Spot is in EUR and perps in USD, so portfolio equity across venues needs an FX rate (`src/data/fx.py`).

---

## 4. Target architecture

```
config/portfolio.*.toml
        │  load + validate (src/portfolio/config.py)
        ▼
 ┌──────────────────────── one portfolio cycle (src/portfolio/engine.py) ────────────────────────┐
 │ market data: completed bars per instrument & interval (MarketDataPipeline, multi-symbol)      │
 │   → SleeveRunner per sleeve: strategy.signal_series → signal (-1/0/1)                         │
 │                              → sizer (share of equity) → sleeve target weight                 │
 │   → allocate budgets (src/portfolio/allocation.py): scale sleeves by budget / risk            │
 │   → net per instrument (src/portfolio/netting.py) + attribution back to sleeves               │
 │   → portfolio risk overlay (src/portfolio/risk.py): per-instrument, gross, net, per-venue     │
 │     caps, drawdown de-risking, stale-data holds                                               │
 │   → order planning (src/portfolio/orders.py): current book vs targets → orders                │
 │     (rebalance bands, min sizes, lot steps, exits first, reduce-only, spot long-only)         │
 │   → execution: one adapter per venue (sandbox in paper/dry-run, real in live)                 │
 │   → book update + reconciliation per instrument (src/portfolio/book.py)                       │
 │   → TradeLogger: orders/fills with sleeve ids, portfolio snapshot, decisions, alerts          │
 └────────────────────────────────────────────────────────────────────────────────────────────┘
research: scripts/research/portfolio_backtest.py runs the same sleeve → allocate → net → risk functions over history
```

**New package `src/portfolio/`** (proposed names; keep them unless there's a reason not to):

| Module | Responsibility | Pure? |
| --- | --- | --- |
| `config.py` | `PortfolioConfig`, `SleeveConfig`, `InstrumentConfig`, `PortfolioRiskConfig`; `load_portfolio_config(path)` from TOML; validation with precise errors | yes |
| `sleeves.py` | Build each sleeve's strategy and sizer; compute its signal and target weight from its bars | yes |
| `allocation.py` | Turn sleeve budgets into scales: `fixed`, `equal`, `inverse_vol` (equal risk); optional sleeve vol targeting | yes |
| `netting.py` | Sum sleeve targets per instrument; keep per-sleeve contributions for attribution | yes |
| `risk.py` | Portfolio caps and de-risking: per-instrument, gross, net, per-venue, drawdown and daily-loss scaling, stale-data holds | yes |
| `orders.py` | Plan orders from current positions to targets: bands, min size, lot step, exits first, reduce-only flags | yes |
| `book.py` | `PortfolioBook`: positions per instrument, cash per venue, average entry, fees, funding, mark-to-market, per-sleeve virtual P&L, JSON checkpoint | state, no I/O |
| `engine.py` | `PortfolioEngine.run_cycle(...)`: glue for one cycle; the only module that talks to adapters and the logger | shell |

**Identifiers**
- **Instrument id:** `"<venue>:<symbol>"`, e.g. `"kraken_futures:BTC/USD"`, `"kraken:BTC/EUR"`. Netting happens
  only within one instrument. BTC spot and BTC perp are different instruments.
- **Sleeve id:** a short unique slug from the config, e.g. `btc_trend_1d`. It becomes `strategy_id` on trades and
  events.

---

## 5. Configuration format (TOML, read with the standard library's `tomllib`)

```toml
# config/portfolio.example.toml
[portfolio]
name = "trend-core"
base_currency = "USD"            # equity and caps are measured in this currency
initial_equity = 10000           # paper and dry-run starting equity, in base currency
rebalance_band = 0.02            # skip trades smaller than 2% of equity (exits always go through)
allocation = "inverse_vol"       # fixed | equal | inverse_vol

[risk]
max_gross_exposure = 1.5         # sum of |weights| across instruments
max_net_exposure = 1.0           # |sum of weights|
max_instrument_weight = 0.6      # per instrument, after netting
max_venue_exposure = { kraken_futures = 1.5, kraken = 0.5 }
max_drawdown = 0.40              # from the equity peak: flatten and stay flat until a person resets it (a kill)
daily_loss_limit = 0.05          # beyond it, no new risk until the next UTC day
drawdown_derisk_start = 0.25     # optional, off by default: scale all targets down linearly from here...
drawdown_derisk_floor = 0.5      # ...to 50% at max_drawdown
stale_after_bars = 2             # an instrument with no new bar for this many intervals takes no new risk

[instruments."kraken_futures:BTC/USD"]
kind = "perp"                    # perp | spot
max_leverage = 2.0
[instruments."kraken_futures:ETH/USD"]
kind = "perp"
max_leverage = 2.0

[[sleeves]]
id = "btc_trend_1d"
instrument = "kraken_futures:BTC/USD"
interval = "1d"
strategy = "moving_average_crossover"
params = { short_window = 4, long_window = 48 }
long_only = true
budget = 0.4                     # share of the portfolio (fixed/equal/inverse_vol use it differently; see 2.3)
sizing = "vol_target"            # any name from src/risk/sizing.py
sizing_params = { target_annual_vol = 0.5 }
warmup_bars = 200
enabled = true

[[sleeves]]
id = "eth_keltner_ls"
instrument = "kraken_futures:ETH/USD"
interval = "1d"
strategy = "keltner_breakout"
params = { window = 40, atr_multiplier = 2.0 }
budget = 0.3
sizing = "vol_target"
sizing_params = { target_annual_vol = 0.5 }
stops = { atr_stop_multiplier = 3.0, time_stop_bars = 60 }   # sleeve-level exits (optional)
```

**Validation rules** (each failure names the key and the fix):
- Sleeve ids are unique slugs (`[a-z0-9_]+`). Each sleeve's instrument exists in `[instruments.*]`.
- `strategy` exists in the registry. `params` are accepted by it (`build_strategy` already raises on unknown ones).
  `sizing` / `sizing_params` pass `build_sizer`.
- `interval` is one of `BAR_INTERVALS` (`src/runtime/config.py`).
- A spot instrument is used only by long-only sleeves, or is flagged `allow_short = false` so net targets clamp at
  0.
- Budgets are positive. Under `fixed` they must sum to at most 1 (warn when below 1: cash drag). Under `equal` and
  `inverse_vol` they are relative weights.
- Risk limits are positive, with `drawdown_derisk_start < max_drawdown`.
- Unknown keys are errors, not silently ignored: a typo must not quietly fall back to a default.

---

## 6. Build plan

Every step lists **Files**, **Tasks**, **Tests**, **Verify** and **Done when**. Keep the order. Phases 1-2 are pure
Python and need no network, so they are good hotspot work.

### Phase 0: Foundations (done)
- [x] 0.1 One sizing contract: every sizer returns a share of equity, and the engine converts it to units in one
  place (`src/risk/sizing.py`; see `todo_important.md`).
- [x] 0.2 A research portfolio backtester for weights per coin per day (`src/research/portfolio.py`).

### Phase 1: Research. Prove the multi-sleeve book is worth running

**1.1 Sleeve targets over history**
- Files: new `src/portfolio/sleeves.py` (pure), used by research now and runtime later.
- Tasks:
  - `SleeveSpec` (id, instrument, interval, strategy, params, long_only, sizing, sizing_params, budget).
  - `sleeve_signals(spec, bars) -> np.ndarray` via `build_strategy(...).signal_series(bars)`.
  - `sleeve_target_weights(spec, bars) -> np.ndarray`: signal times the sizer's share at each entry, held until the
    signal changes. This matches the runtime, which sizes at entry and doesn't resize (see
    `src/research/volatility.py::vol_scaled_positions` with `entry_only=True`). The sizer sees only bars up to that
    bar.
- Tests: long-only never goes negative; the size is fixed while a position is held; no look-ahead (changing bar
  t+1 never changes the weight at t).
- Done when: for one sleeve, `simulate_portfolio(prices, weights)` reproduces the single-strategy research
  backtest's returns within costs.
- Findings (2026-09-26):
  - A long-only sleeve matches `SimpleBacktester` exactly at zero cost, and within 0.2% of final equity with perp
    costs (`test_a_long_only_sleeve_reproduces_the_single_strategy_backtest`).
  - **Flips differ from the single-strategy stack.** `SleeveRunner` flips in one bar (long to short at the same
    close). `SimpleBacktester` and `PaperTradingEngine` go flat on the flip bar and open the other side on the next
    bar. Portfolio research and the portfolio runtime share the sleeve runner, so they agree with each other. But a
    long/short sleeve won't match a single-strategy backtest bar for bar around flips. The one-bar flip is what the
    order planner (2.6, "split a flip into close plus open") expects.

**1.2 Generalise `simulate_portfolio` to any bar interval**
- Files: `src/research/portfolio.py`.
- Tasks: add `periods_per_year` (inferred from the index spacing). Keep funding as per-bar rates; the caller
  converts daily funding to per-bar.
- Tests: the same book on daily vs 4h bars (4h prices from the same daily path) gives the same daily returns.
- Findings (2026-09-26): `day_start_equity` (for the daily-loss rule) was set *after* the day's first bar had
  booked its P&L. On daily bars the day's loss was therefore always 0 and the halt never fired. It now starts from
  the equity before that bar (bars are stamped at their open).

**1.3 Allocation functions**
- Files: new `src/portfolio/allocation.py`.
- Tasks: `allocate(sleeve_weights: dict[id, series], budgets, method, vol_lookback)` returns scaled sleeve weights:
  - `fixed`: weight x budget;
  - `equal`: weight x 1/N of enabled sleeves;
  - `inverse_vol`: each sleeve gets budget in proportion to 1 / (its recent return volatility), from past data only,
    re-estimated monthly, so every sleeve contributes similar risk.
- Tests: budgets sum as documented; `inverse_vol` gives a low-vol sleeve a bigger weight; no look-ahead.

**1.4 Research script and study**
- Files: new `scripts/research/portfolio_backtest.py`, new `config/portfolio.example.toml`.
- Tasks:
  - Load the config (step 2.1 can start as a small reader here and move into `src/portfolio/config.py` in 2.1).
  - Load bars per instrument with `load_bars(symbol, interval, source="perp")`, which is cached (no download).
  - Build sleeve weights, allocate, net per instrument (2.4 can start inline), and simulate.
  - Print per sleeve and for the portfolio: Sharpe, CAGR, max drawdown, turnover, correlation matrix of sleeve
    returns. Save CSVs under `data/research/portfolio_<ts>/`.
- Study: BTC and ETH perps; MA crossover (1d and 4h) and long/short Keltner (1d); vol-target sizing. Compare each
  sleeve alone with `fixed`, `equal` and `inverse_vol` books. Write the findings in `docs/research_log.md`.
- Done when: the script runs from the config alone, and the log entry says which allocation to default to and why.
- Findings (2026-09-26, `docs/research_log.md`): default to `equal`; `inverse_vol` adds nothing when sleeves are
  already vol-sized. The `max_drawdown` halt is a permanent kill (a flat book can't recover its drawdown), so it
  must sit above the book's worst drawdown. The example now uses 40%. De-risking on drawdown lowered Sharpe, so
  it is off by default. The pipeline lives in `src/portfolio/backtest.py` (`prepare_inputs`, `run_book`).

### Phase 2: Portfolio core (pure functions, no I/O)

**2.1 Config and validation**
- Files: `src/portfolio/config.py`, `config/portfolio.example.toml`, `main.py` (`--portfolio-check PATH`).
- Tasks: dataclasses for section 5; `load_portfolio_config(path) -> PortfolioConfig`; every rule in section 5
  gives a precise message; `--portfolio-check` prints the resolved sleeves, instruments, budgets, sizers and risk
  limits, touching no network or DB.
- Tests: the example config loads; one test per validation rule (bad strategy name, unknown param, duplicate id,
  unknown key, spot short, budgets over 1 under `fixed`, bad interval).
- Verify: `python main.py --portfolio-check config/portfolio.example.toml`.

**2.2 Sleeve runner (runtime form)**
- Files: `src/portfolio/sleeves.py`.
- Tasks: `SleeveState` holds what a sleeve must remember between cycles: last signal, current target weight, entry
  price, entry time, bars held, re-entry block, Kelly trade returns. It serialises to JSON.
  `SleeveRunner.update(state, bars) -> (new_state, target_weight, decision)` applies the strategy, the sizer (at
  entry only), sleeve stops (ATR/time/drawdown via `RiskManager.evaluate_exit`) and the re-entry gate
  (`gate_reentry`).
- Tests: same results as 1.1 on history; a stop flattens the sleeve and blocks re-entry until the signal resets;
  state round-trips through JSON.

**2.3 Allocation (runtime form)**
- The same functions as 1.3, fed the latest sleeve targets plus the rolling sleeve return history kept in the book.

**2.4 Netting and attribution**
- Files: `src/portfolio/netting.py`.
- Tasks: `net_targets(sleeve_targets: {sleeve_id: (instrument, weight)}) -> {instrument: weight}`, plus
  `attribution: {instrument: {sleeve_id: weight}}`.
- Tests: the sum is preserved; opposite sleeves cancel; attribution sums back to the net.

**2.5 Portfolio risk overlay**
- Files: `src/portfolio/risk.py`.
- Tasks: `apply_portfolio_risk(targets, book_state, config, now) -> (targets, actions)`, where `actions` lists what
  was changed and why:
  1. stale instruments hold their current weight: no increase, reductions allowed;
  2. spot instruments clamp at 0;
  3. per-instrument cap;
  4. per-venue cap, scaling that venue's instruments proportionally;
  5. net cap, then gross cap, scaling proportionally;
  6. drawdown de-risk multiplier;
  7. daily-loss and max-drawdown halts: no new risk, or flatten all.
- Tests: one per rule; every cap result is within limits; the actions explain every change.
- Note for 2.6: the overlay only sees instruments that have a target. An instrument that is held but has no sleeve
  left (for example a sleeve was disabled) is not flattened by it, so the order planner must treat a missing
  target as 0.

**2.6 Order planner**
- Files: `src/portfolio/orders.py`.
- Tasks: `plan_orders(current_units, target_weights, prices, equity, instrument_specs, band) -> list[PlannedOrder]`,
  where each order has instrument, side, units (Decimal-rounded to the lot step), reduce_only and reason.
  - Skip changes smaller than the band, unless closing or flipping.
  - Skip orders below the exchange minimum (log them as `below_min_size`).
  - Split a flip into close plus open.
  - Order exits and reductions before increases, so margin frees up first.
- Tests: rounding never exceeds the target; the band suppresses churn; flips split; exits come first; minimum
  sizes are respected.

**2.7 Parity test**
- Run the research path (1.x) and the runtime core (2.2-2.6, driven bar by bar) on the same bars. Assert identical
  target weights at every bar close.

### Phase 3: Book and paper execution (stateful, incremental)

**3.1 `PortfolioBook`**
- Files: `src/portfolio/book.py`.
- Tasks:
  - Positions per instrument (units, average entry), cash per venue, fees, funding (perps), realised and
    unrealised P&L.
  - Equity in base currency: FX-convert the EUR venues with `src/data/fx.py` and record the rate used.
  - Per-sleeve virtual positions for attribution.
  - `apply_fill(...)`, `mark(prices)`, `to_json()` / `from_json()`.
- Tests: the P&L of a round trip matches hand calculation; fees and funding are booked; the JSON round trip is
  exact; attribution sums to book P&L minus netting effects (report the residual).

**3.2 Paper execution through sandbox adapters**
- Files: `src/portfolio/engine.py`; reuse `SandboxExecutionAdapter` and `SandboxPerpExecutionAdapter`.
- Finding (2026-09-26): `SandboxPerpExecutionAdapter` (a `MarginAccountAdapter`) holds **one** contract, with one
  position and one wallet, so "one adapter per venue" can't hold BTC and ETH perps together. The options are
  (a) one sandbox perp adapter per instrument (isolated margin, with collateral split per instrument), or (b) extend
  the margin adapter to several contracts sharing one wallet (cross margin, which is how Kraken Futures'
  multi-collateral account works). Needs a decision before 3.2.
- Decision (2026-09-26, user): a new multi-contract cross-margin sandbox, leaving the single-contract adapter and
  the single-strategy runtime untouched. Built as `SandboxCrossMarginPerpAdapter` (`src/execution/cross_margin.py`,
  tests in `tests/test_cross_margin_sandbox.py`). It has one wallet and one margin check across all positions,
  per-symbol funding and slippage, `reduce_only` orders, account-wide liquidation, and an atomic JSON state file.
- Tasks: one adapter per venue; submit planned orders with `symbol=`; apply fills to the book; reconcile the book
  against each adapter per instrument; log orders, fills and decisions with `strategy_id=<sleeve_id>`. For netted
  orders, use the dominant sleeve's id or `portfolio`, and record the attribution in the event metadata.
- Tests: a two-sleeve, two-instrument book over synthetic bars trades the planned orders; the book equals the
  adapters' view.

**3.3 Checkpoint and restart**
- Files: `src/portfolio/engine.py`, runtime state path.
- Tasks: save the book, all `SleeveState`s and the last processed bar per instrument after every cycle with an
  atomic write (write a temp file, then rename). On start, load it and reconcile with the adapters before doing
  anything.
- Tests: kill after cycle N and restart; there are no duplicate orders and the positions are identical.

### Phase 4: Runtime integration

**4.1 Multi-instrument market data**
- Files: `src/data/pipeline.py`, `main.py`.
- Tasks:
  - One connector per instrument (Kraken spot or futures).
  - Bars per (instrument, interval) from the aggregator (`drain_completed_bars` per interval).
  - Warmup per sleeve from history loaders (`_load_warmup_bars` in `main.py`).
  - Stale detection per instrument.
- Tests: two instruments with different intervals produce the right completed bars; one failing connector marks
  only its instrument stale.

- Findings and decision (2026-09-26): **REST candles, not a WebSocket feed.** The single-strategy runtime builds
  bars from ticker polls. A 4h or daily bar built that way only sees prices at poll times, so its high and low are
  too narrow (ATR-based rules then see different bars than research did), and a restart mid-bar leaves a gap. The
  portfolio runtime instead reads the exchange's completed candles (`src/portfolio/feed.py`, on the same cached
  series the research used). Polling once a minute sees a new 4h candle within a minute, a delay that is noise for
  4h and daily decisions, and it costs a few requests a minute, far inside Kraken's public limits. A WebSocket feed
  becomes worth it for intraday or order-book strategies, and for private fill updates in live trading. The
  blocking HTTP calls run in worker threads, so the event loop never blocks.

**4.2 `--portfolio` runtime**
- Files: `main.py` (`--portfolio PATH` with `--runtime`), a new `PortfolioOrchestrator` or an extension of
  `RuntimeOrchestrator`.
- Tasks: a cycle is fetch, drain bars, run a portfolio cycle only when a sleeve's bar completed (otherwise just mark
  the book), then checkpoint and health. Keep the watchdog, the kill switch, alerts and the single-strategy path
  working. The `max_drawdown` halt never lifts by itself (see 1.4), so add an explicit operator reset (for
  example `--portfolio-reset-peak`) that is logged as an event.
- Verify: `python main.py --runtime paper --portfolio config/portfolio.example.toml --use-mock-connector
  --runtime-iterations 5 --dashboard`.

**4.3 Persistence and reporting**
- Files: `src/storage/trade_logger.py`, `main.py` (report and dashboard).
- Tasks:
  - New `portfolio_snapshots` table: timestamp, equity, gross, net, and per-instrument and per-sleeve JSON.
  - Decisions and risk actions as operational events.
  - Daily summary per sleeve.
  - Dashboard: sleeves (signal, target, P&L), instruments (target vs actual, notional), risk usage vs caps, and the
    last risk actions.
- Tests: a snapshot round trip; the report reads from the DB, not from memory.

**4.4 Alerts**
- Stale instrument, cap breach or de-risking engaged, reconciliation mismatch per instrument, and a sleeve disabled
  by errors (Telegram via the existing notifier).

### Phase 5: `live_dry_run`
- Run the portfolio for days against sandbox adapters shaped like the real venues (`ExecutionRouter`), with real
  market data.
- Check the reconciliation, restarts, sizing and reports.
- Record the findings in `todo_important.md`.

### Phase 6: Live readiness (user go-ahead required at every step)
- The kill switch flattens every instrument on every venue and cancels all orders; test it against sandboxes.
- Minimum order sizes and lot steps per instrument come from exchange metadata (Kraken `AssetPairs`, Kraken Futures
  instruments).
- Tax: spot fills per asset feed the existing FIFO ledger, and perps feed the derivative ledger per symbol. Netting
  must not bypass either one. **Done 2026-09-26** (`PortfolioEngine(record_tax=True)`, live only): each realized
  P&L, fee and funding flow is recorded as it happens, and spot fills go through FIFO lots. Failed writes are queued
  in the checkpoint and retried. The tests check that the ledger totals equal the book's.
- Live gates as today (`--enable-live-trading`, the confirmation token, an explicit exchange), plus a
  portfolio-level `max_live_notional`.
- Promotion checklist in `docs/runbook.md`.

### Phase 7: Enhancements (after the core runs in paper)
- 7.1 Portfolio-level volatility targeting (scale the whole book to a target vol).
- 7.2 Correlation-aware caps (cluster exposure: BTC and ETH move together).
- 7.3 Sleeve health: auto-disable a sleeve after repeated errors or a sleeve-level drawdown, and alert.
- 7.4 Cross-sectional sleeves: a sleeve whose target is a basket (from `scripts/research/cross_sectional_study.py`),
  after the wifi download and research.
- 7.5 Regime filters as sleeve inputs (the crowding composite from the positioning research).
- 7.6 Options sleeves (Deribit): needs an options pricing/greeks module and keys; see "Needs exchange API keys" in
  `TODO.MD`.

---

## 7. Things to keep in mind (checklist of pitfalls)

**Correctness**
- No look-ahead. A decision at bar t may only use bars that closed at or before t. Across intervals, a daily
  sleeve's target changes only at the daily close, and 4h sleeves keep reading the last daily target in between.
- Clock alignment: bars are stamped at their open time, so a daily bar's close is timestamp + 1 day. Use UTC
  everywhere.
- Netting changes fills, not sleeve P&L. Attribute P&L from sleeves' virtual positions and report the residual
  (the netting benefit or cost) separately.
- Rounding to lot steps must never overshoot a cap. Round toward zero on increases.
- Spot can't go short. Clamp, and log `spot_short_clamped`.
- Perps: margin is per venue, and leverage is the notional / venue equity. Keep the liquidation buffer check per
  instrument (`liquidation_buffer_pct`).
- Funding accrues on perps positions every settlement, so book it per instrument (the sandbox perp adapter already
  does).
- Currency: Kraken spot is EUR, perps are USD. Convert at a recorded rate, and never add EUR and USD raw.
- Partial fills and async fills in live: the book updates from fills, never from orders. Reconcile every cycle.

**Robustness**
- One venue failing must not stop the others. Mark its instruments stale, hold them, and alert.
- Restart safety: checkpoint after every cycle with atomic writes. On start, reconcile with the adapters before
  trading. Use client order ids with the sleeve id and cycle number so a retried submit is recognisable.
- Idempotency: planning the same cycle twice must not double-order. Store the last processed bar per instrument.
- Every refusal has a reason code (`below_min_size`, `stale_instrument`, `venue_cap`, `drawdown_halt`, ...).
  Nothing is silently sized to zero.
- Config errors surface at startup, not at the first trade.

**Usability**
- One config file and one command, with good defaults and `--portfolio-check`.
- The dashboard answers three questions at a glance: what do we hold, why, and how is each sleeve doing.
- Keep the single-strategy CLI working. The portfolio is additive.

**Research hygiene**
- Pick budgets and allocation methods from research with a holdout, not from the best cell.
- BTC and ETH are highly correlated: two sleeves on them are not two independent bets.
- Record every study, including the ones that fail, in `docs/research_log.md`.
- Costs: use perp fees (0.05% taker) plus slippage, and funding. For spot on Kraken, 0.40% taker.

---

## 8. Testing and verification guide
- Unit tests for every pure function (Phase 2). They are fast and need no network.
- Property checks: netting preserves the sum; caps are never exceeded; rounding never overshoots; no look-ahead
  (perturb future bars and assert past outputs are unchanged).
- Parity test between research and runtime (2.7).
- Scenario tests with synthetic bars: a crash day (stops, de-risking), a stale venue, a restart mid-run, a flip.
- Smoke run: `python main.py --runtime paper --portfolio config/portfolio.example.toml --use-mock-connector
  --runtime-iterations 5 --dashboard`.
- `tests/conftest.py` already isolates the DB, the perp sandbox state and Telegram. Keep new tests inside it (use
  `tmp_path`).

---

## 9. User guide (grows as steps land)
- **Validate a config:** `python main.py --portfolio-check config/portfolio.example.toml`.
- **Backtest a config:** `python scripts/research/portfolio_backtest.py config/portfolio.example.toml`.
- **Try a new signal as a sleeve:** `notebooks/signal_research.ipynb`, section 7 (`candidate_report`).
- **Run in paper:** `python main.py --runtime paper --portfolio config/portfolio.example.toml --runtime-iterations 0`
  (until Ctrl-C). Then `python main.py --portfolio config/portfolio.example.toml --dashboard` shows it.
- **Sizing options:** `python main.py --list-sizing`.
- **Add a sleeve:**
  1. Copy a `[[sleeves]]` block, give it a new `id`, and change the instrument, strategy and params.
  2. Run `--portfolio-check`.
  3. Backtest it.
  4. Paper-run it.

---

## 10. Progress tracker
- [x] 0.1 Sizing contract
- [x] 0.2 Research portfolio backtester
- [x] 1.1 Sleeve targets over history (2026-09-26). `src/portfolio/sleeves.py` (`SleeveRunner.step`, `run_sleeve`, `SleeveState`) merges steps 1.1 and 2.2: research replays the same bar-by-bar runner as the runtime. Tests: `tests/test_portfolio_sleeves.py`. See the step's findings on flips.
- [x] 1.2 Any bar interval in `simulate_portfolio` (2026-09-26): `periods_per_year`, `rebalance_band`, the `adjust_targets` hook with `current_weights`, per-instrument fees (`PortfolioCosts.fee_pct` as a mapping), and the day-start fix in the step's findings. Tests in `tests/test_portfolio.py`.
- [x] 1.3 Allocation functions (2026-09-26): `src/portfolio/allocation.py`, tests in `tests/test_portfolio_allocation.py`.
- [x] 1.4 Research script, example config, study and log entry (2026-09-26): `scripts/research/portfolio_backtest.py` over `src/portfolio/backtest.py`, tests in `tests/test_portfolio_backtest.py`, findings in `docs/research_log.md`.
- [x] 2.1 Config and validation, `--portfolio-check` (2026-09-26): `src/portfolio/config.py` (`load_portfolio_config`, `describe`, all rules, every error reported at once, non-numeric values reported instead of crashing), `main.py --portfolio-check PATH` (exit code 1 on a bad config), tests in `tests/test_portfolio_config.py`.
- [x] 2.2 Sleeve runner with state (2026-09-26, the same module as 1.1): stepping one bar at a time with a JSON restart every bar matches `run_sleeve`; stops flatten the sleeve and block re-entry until the signal resets. `SleeveState` keeps `bars_held` instead of an entry time.
- [x] 2.3 Allocation (runtime form) (2026-09-26): `Allocator` in `src/portfolio/allocation.py` steps once per grid bar, refits on the same schedule from past returns only, and checkpoints to JSON. It matches `allocate_history` bar by bar.
- [x] 2.4 Netting and attribution (2026-09-26): `src/portfolio/netting.py`, tests in `tests/test_portfolio_allocation.py`.
- [x] 2.5 Portfolio risk overlay (2026-09-26): `src/portfolio/risk.py` (`apply_portfolio_risk`, `drawdown_multiplier`, `array_overlay` for research), tests in `tests/test_portfolio_risk.py`. The config also rejects a non-positive daily-loss limit, venue cap or `stale_after_bars`.
- [x] 2.6 Order planner (2026-09-26): `src/portfolio/orders.py` (`plan_orders`, `OrderPlan`, `PlannedOrder`), tests in `tests/test_portfolio_orders.py`. The band matches `simulate_portfolio`'s. Held instruments without a target are closed. Every skip has a reason (`within_band`, `below_min_size`, `below_lot_step`, `no_price`, `no_short`).
- [x] 2.7 Parity test (2026-09-26): `test_the_runtime_path_bar_by_bar_matches_the_research_backtest`. It steps each sleeve as its bars complete, steps the allocator per 4h grid bar and nets, with a JSON checkpoint every bar. The result equals `run_book`'s targets under all three allocation methods. Delaying a decision by one bar fails it. The risk overlay is the same pure function in both paths (`array_overlay` wraps `apply_portfolio_risk`); its inputs (equity, current weights) come from the book in Phase 3.
- [x] 3.1 Portfolio book (2026-09-26): `src/portfolio/book.py` (`PortfolioBook`), tests in `tests/test_portfolio_book.py`. It covers spot and linear-perp accounting in `Decimal`, cash per venue in the venue's currency, FX to base, funding, the peak and UTC day start for the risk rules, virtual sleeve positions with the residual reported, and an exact JSON round trip.
- [x] 3.2 Paper execution through sandbox adapters (2026-09-26): `src/portfolio/engine.py` (`PortfolioEngine`, `build_paper_adapters`), tests in `tests/test_portfolio_engine.py`. Perps go through the cross-margin sandbox and spot through the spot sandbox. Reduce-only orders pass through, and funding and liquidations are booked in the book the same way the exchange books them. The book is reconciled every cycle, and a mismatch is logged and alerted. Fills are logged with the driving sleeve (or `portfolio` when several net) as `strategy_id`, and each fill sends a Telegram message listing its sleeves and risk actions. Pre-risk targets match `run_book` bar by bar.
- [x] 3.3 Checkpoint and restart (2026-09-26): the engine (book, sleeve states, allocator, last processed bars) and the cross-margin sandbox each write an atomic JSON file. A run killed mid-way and restarted gives the same fills and book as an uninterrupted run. Repeating a cycle decides nothing twice.
- [x] 4.1 Multi-instrument market data (2026-09-26): `CandleFeed` fetches completed Kraken candles per (instrument, interval) concurrently, in threads. A failing key keeps its last good bars and is reported; an instrument is stale when its fetch fails or its bar is older than `stale_after_bars`. See the REST decision under 4.1.
- [x] 4.2 `--portfolio` runtime (2026-09-26): `main.py --runtime paper|live_dry_run --portfolio PATH` (live is refused) runs `PortfolioRuntime` (`src/portfolio/runtime.py`). `--runtime-iterations 0` runs until Ctrl-C/SIGTERM, with a clean checkpoint. The kill switch flattens everything and stops the loop. `--portfolio-reset-peak` re-arms the book after the drawdown kill. Mock runs use a temp state folder. Tests are in `tests/test_portfolio_runtime.py`.
- [x] 4.3 Persistence and reporting (2026-09-26): a `portfolio_snapshots` table (on every decision, and hourly), decisions and fills as operational events, and `main.py --portfolio PATH --dashboard` (instruments' target vs actual, sleeves' own and allocated weight, P&L and last action, the residual, acting risk limits, recent alerts). A daily summary per sleeve is still to do.
- [x] 4.4 Alerts (2026-09-26): stale or failing data per instrument, risk limits acting, rejected orders, reconciliation mismatches, sleeves disabled after 3 failing cycles, and repeated cycle errors. Each is sent once when it starts and once when it clears.
- [ ] 5 `live_dry_run` soak
- [ ] 6 Live readiness (user go-ahead)
- [ ] 7 Enhancements

**Open decisions for the user** (don't block on them; use the default and note it):
- Default sizing in the single-strategy runtime: `fixed_fraction` 10% (current) or `vol_target`. Default: keep
  `fixed_fraction`.
- Portfolio base currency: USD (perps) or EUR (spot, tax). Default: USD, with EUR conversion for tax and reports.
- Starting sleeves and budgets: from the Phase 1 study. Default until then: the example config.
- Portfolio kill level and de-risking: the study (1.4) supports a 40% drawdown kill and no de-risking for the
  example book. That is a lot of drawdown for a first live book, so a smaller live allocation may suit better
  than a tighter kill.
