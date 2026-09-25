# Portfolio architecture assessment: multi-strategy, multi-asset-class readiness

**Date:** 2026-09-25
**Purpose:** The stated end goal for this project is a portfolio structure that can run *multiple strategies* across *multiple assets and instrument types* (spot, futures, derivatives, options) at once, with risk measures applied at the portfolio level, not just per trade. This doc records what the current codebase can and cannot do against that goal, so future prioritization decisions (what to build next) are made with this constraint in view rather than rediscovered later.

This is a snapshot based on reading the code on 2026-09-25 (post the tax-ledger fix in `todo_important.md`). Re-verify claims here before relying on them if the runtime path has changed since.

## Verdict

**No — not yet.** The current codebase is architected around a **single strategy trading a single symbol through a single exchange adapter per process**. That assumption is baked into several core classes, not just missing as a configuration option. Getting to "multiple strategies, multiple assets, multiple instrument types" is a real architecture project, not a config change.

This isn't a criticism of the work so far — the single-strategy path needed to exist first and now does, with real safety gates. But it means **"add multi-strategy/multi-asset support" needs to be an explicit, sequenced piece of work**, not something that falls out naturally from continuing to harden the current live-execution path.

## Evidence

### 1. `RuntimeOrchestrator` is one-strategy, one-symbol by construction
`src/runtime/orchestrator.py` takes a single `strategy`/`strategy_name` and a single `trading_symbol` at construction time. Bars for any other symbol are explicitly filtered out:

> "Symbol this runtime instance trades. Bars from other symbols are [ignored]." — and `run_once()` does exactly that: `new_bars = [b for b in new_bars if getattr(b, "symbol", self.trading_symbol) == self.trading_symbol]`.

Running strategy A on BTC/EUR and strategy B on ETH/USD today means **two separate OS processes**, each with its own orchestrator, each independently polling/streaming data, each with no visibility into what the other is doing.

### 2. `PaperTradingEngine` holds one scalar position, not a portfolio of positions
`src/execution/paper_trading.py::run()` tracks `cash` and `position_size` as single floats local to the run loop, plus one `avg_entry_price`. There is no `positions: dict[symbol, Position]` structure. It also takes one `execution_adapter` — one exchange/venue per engine instance.

### 3. `ExecutionRouter` selects exactly one adapter, not a multi-venue router
`ExecutionRouter.__init__` builds/holds a single `adapter` based on `(mode, exchange)`. It's a factory for "the one adapter this run uses," not a dispatcher that can route order A to Kraken spot and order B to a futures venue within the same run.

### 4. `TradeLogger`'s schema has no strategy or asset-class attribution
The `trades` table (`src/storage/trade_logger.py`) is `(id, timestamp, source, exchange, pair, side, price, size, fee, role_maker_taker, latency_ms)`. There is no `strategy_id` and no `instrument_type`/`asset_class` column. `source` is a free-text string (`"paper_trading"`, `"cli_manual_live_order"`, …) — usable as a crude tag if a strategy name were stuffed into it, but not a structured, queryable attribution model. Reporting "PnL by strategy" or "exposure by asset class" would require parsing that string, not a real join.

### 5. `RiskManager` reasons about one equity curve
`RiskManager.evaluate()` in `src/risk/controls.py` takes `equity`/`peak_equity` as scalars passed in by the caller, and per-exchange caps (`_exchange_caps`) — there's no per-strategy or per-instrument-type risk sleeve. A drawdown breach halts "the" account, not "strategy X's allocation." Running several strategies would currently mean either (a) one shared circuit breaker that stops everything the moment any one strategy misbehaves, or (b) N independent `RiskManager` instances with zero awareness of each other's combined exposure — both are wrong for a real portfolio.

### 6. Futures/derivatives support is placeholder-only; options don't exist at all
`src/execution/contracts/futures_contract.py` and `src/execution/sizing/contract_sizer.py` are stub code — `FuturesContractSizer.calculate_contract_qty` is literally `# TODO: Implement calculation logic / pass`. `config/contracts.yaml` and the registry are unfinished (see `TODO.MD` Priority 5, which already flags this as an open, explicitly-deferred track). There is no code anywhere for options or other derivatives — no Greeks, no margin/premium model, nothing. This isn't a gap in an existing feature; the feature doesn't exist yet.

## What this means for sequencing

The roadmap already anticipated some of this (`TODO.MD` **Priority 4A: Multi-signal portfolio construction and crossing**, **Priority 5: Resolve the Incomplete Futures Track**) but both are currently unstarted and not sequenced ahead of the live-execution hardening work. Given the stated end goal, a few things follow:

1. **Don't let single-strategy/single-symbol assumptions calcify further.** Every new feature added to `RuntimeOrchestrator`, `PaperTradingEngine`, `RiskManager`, and `TradeLogger` right now is being added to a single-strategy shape. The longer that continues, the more expensive the eventual portfolio refactor becomes (more call sites assuming a scalar `position_size`, more reports assuming one `source` string is enough attribution, etc).
2. **The risk-control gaps identified in the previous discussion (time-stops, ATR stop-loss, rolling daily-loss limit) should be designed as per-strategy/per-sleeve controls from the start**, not bolted onto the single global equity curve, even if only one strategy runs today. Retrofitting "which strategy does this drawdown belong to" later is much harder than building the attribution in now.
3. **A concrete, sequenced decision point exists**: either (a) explicitly commit to running N independent single-strategy processes against a shared `TradeLogger` database as the near-term multi-strategy model (cheap, but no portfolio-level netting/allocation/risk aggregation — just N isolated silos sharing storage), or (b) invest in an actual portfolio layer (a `PortfolioManager`/allocator that owns multiple strategy sleeves, a `strategy_id`-aware schema, and a risk manager that aggregates across sleeves) before scaling strategy count. Given the stated goal is explicit multi-strategy/multi-asset flexibility, (b) is the right target — but it's a meaningfully sized piece of work, not a side effect of other tasks.
4. **Futures/derivatives/options support is a separate, larger, and currently unstarted track.** `TODO.MD` Priority 5 already says to explicitly decide whether this is near-term critical path or deferred — this doc adds the concrete reason to revisit that decision: the portfolio layer above (point 3) should probably be designed with instrument-type-awareness (spot vs. margined/derivative sizing, different risk treatment) from day one, since retrofitting instrument-type dispatch into a spot-only portfolio layer is exactly the kind of rework this doc is trying to help avoid.

## Suggested next-step framing (not yet actioned)

Given the immediate risk-control work already agreed (time-stops, ATR stop, rolling daily-loss circuit breaker), the practical near-term path that keeps both goals moving without over-building prematurely:

- Build those risk controls with a `strategy_id` (even if only one strategy exists today) as a first-class parameter/column, so the attribution exists before a second strategy shows up.
- Treat "when do we build the real portfolio/allocator layer" as an explicit decision to revisit once risk controls + strategy-driven order parity + a sustained live-paper run (the three items already agreed as next) are done — not something to defer indefinitely, but not something to build speculatively ahead of need either.
- Keep futures/derivatives/options explicitly parked (per `TODO.MD` Priority 5's own recommendation) until the spot multi-strategy shape is proven, then revisit contract/sizing scaffolding with the portfolio layer's instrument-type-awareness in mind rather than as a bolt-on.
