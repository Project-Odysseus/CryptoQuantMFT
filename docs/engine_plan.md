# One engine for every strategy type: the strategy interface and the event-driven core

Status 2026-09-27: **a plan, nothing built.** The direction is in `aspirations.md` (section 8). This file says
what to build and in which order, without breaking the live BTC book at any step.

## 1. Why

Today there are two runtimes, and both only do "at a bar close, set one weight per instrument":

- the **portfolio runtime** (`src/portfolio/`): sleeves → allocation → netting → risk overlay → order planner →
  adapter → book. It is the main path, and its research backtest (`run_book`) runs the same functions.
- the **single-strategy runtime** (`src/runtime/orchestrator.py` with `PaperTradingEngine`): the older spot path.

The strategies we want next don't fit that shape:

| Strategy | What it needs that is missing |
| --- | --- |
| Cross-sectional basket (taker-buy book) | One strategy setting 30-50 weights at once, from a point-in-time universe and outside data (Binance daily features) |
| Maker-first execution | Resting orders, timers ("reprice after 30 s, cross after 5 min"), fill events between bar closes |
| Options: tail hedge, covered call, delta-hedged strangle | Targets in exposure (delta, vega) or as a structure ("25-delta call, ~30 days"), contract selection, roll dates, exposure limits |
| Intraday rules, order-flow features | Events more often than bar closes: trades, book updates, liquidations |
| Carry (spot + perp), cross-venue hedges | Two legs on two venues, kept together |

## 2. The design in one picture

```
 data feeds ──events──┐                        ┌── timers
 (candles, trades, L2, │                        │
  funding, chains)     ▼                        ▼
                  ┌─────────────── event loop (one clock) ───────────────┐
                  │  strategies ──intents──> intent resolver               │
                  │  (subscribe to events)   (weights / exposures /        │
                  │                           structures -> positions)     │
                  │        allocation -> netting -> risk overlay           │
                  │        -> order planner -> execution policies          │
                  │        -> adapters ──fills/order events──> book        │
                  └──────────────────────────────┬────────────────────────┘
                                                 ▼
                           event log + checkpoints + SQLite (trades, tax, snapshots)
```

The same loop runs **live** (wall clock, real feeds and adapters) and in **replay** (historical events in
timestamp order, simulated adapters). Replay is research, debugging and the soak test at once.

## 3. The strategy interface

```python
class Strategy(Protocol):
    id: str

    def subscriptions(self) -> Subscriptions:
        """What it listens to: instruments x intervals, data series (funding, chains, features), timers."""

    def on_event(self, event: Event, ctx: StrategyContext) -> Intent | None:
        """React to one event; return what it wants now, or None to keep the last intent."""

    def state(self) -> dict: ...                 # checkpointed, JSON
    def restore(self, state: dict) -> None: ...
```

- `StrategyContext` is read-only: history per subscription (bars, features), its own current position and P&L,
  the clock, and the current market (prices, fitted surfaces). A strategy never places orders and never sees
  other strategies. That keeps it testable, and keeps risk in one place.
- Strategies are **deterministic** given their events. Replay reproduces a live day exactly, which makes a
  live-vs-backtest gap a bug to find, not a mystery.

**Intents: what a strategy can ask for**

| Intent | Example | Resolved by |
| --- | --- | --- |
| `TargetWeights({instrument: weight})` | trend sleeve (one instrument); a 40-coin basket | as today: weights → units |
| `TargetExposure({underlying: {"delta": 0.0, "vega": -0.005}})` | delta-hedged short vol, "keep the book delta-neutral" | a small solver over allowed instruments (perp for delta, options for vega), using `src/portfolio/exposure.py` |
| `TargetStructure(spec, size)` | "25-delta call, 25-35 days, sell 0.2 x the BTC long"; "10-delta put, 60 days" | a contract picker plus roll rules (roll N days before expiry) |
| `OrderIntent(...)` (restricted) | a later market-making or execution research strategy | passes straight to execution, still inside the risk limits; off by default |

Today's sleeves are the first case: `SleeveRunner` wrapped as a `Strategy` that listens to one bar series and
returns `TargetWeights({instrument: weight})`. Nothing about the trend sleeves changes.

## 4. The components

- **Event types:** `BarClosed`, `TradePrint`, `BookUpdate`, `FundingRate`, `ChainSnapshot`, `FeatureUpdate`
  (outside data), `Timer`, `OrderUpdate`, `Fill`, `ExchangeStatus`. Each carries exchange time and receive time.
- **Clock and scheduler:** one ordering of events; timers fire in replay exactly as live.
- **Intent resolver:** turns intents into target positions per instrument (the table above).
- **Allocation, netting, risk overlay, order planner:** the existing pure functions, extended with exposure limits
  (`[risk.exposure]`) and the scenario-loss cap.
- **Execution policies** (new): turn a planned order into child orders over time, react to `OrderUpdate`, `Fill`
  and `Timer` events. `ioc_market` (today), `maker_first` (post-only at the touch, reprice, cross after a timeout),
  later `twap`. The policy is per instrument or per strategy in the config. Strategies never know about it.
- **Adapters:** as today (Kraken Futures cross-margin, Kraken spot, sandbox), plus Deribit later. Private WebSocket
  streams become event sources when execution needs them.
- **Book:** `PortfolioBook` as today, extended with option positions and greeks for attribution.
- **Event log:** every event and decision appended (Parquet or SQLite), so a day can be replayed and audited.

## 5. Build order (each step ships on its own; the live book keeps running)

| Step | What | Done when |
| --- | --- | --- |
| E0 | Event types, `Strategy`, intents; `SleeveRunner` wrapped as a `Strategy` | The wrapped sleeves give identical weights (parity test) |
| E1 | The event loop, clock and replay driver; `PortfolioEngine.run_cycle` becomes the `BarClosed` handler | Replay of the BTC book equals `run_book` and today's runtime, bar for bar |
| E2 | Execution policies with timers and order events; `maker_first` | Paper and a live minimum-size test fill as maker, fall back to taker, and log decision/arrival prices for TCA |
| E3 | Basket strategies: a point-in-time universe, `FeatureUpdate` events for Binance daily features | The cross-sectional book runs in replay and paper with Kraken perps as the traded instruments |
| E4 | Exposure and structure intents, option instruments, exposure limits, a Deribit testnet adapter | A tail-hedge sleeve runs on recorded chains in replay and on the testnet |
| E5 | Intra-bar events (trade and book streams over WebSocket), L2 fill models in replay | An intraday research strategy runs in replay with queue-aware fills |
| E6 | Kraken spot in the portfolio path; retire `src/runtime/orchestrator.py` and the exchange-cycle half of `PaperTradingEngine` | Everything runs through one engine; the old runtime is deleted |

E0-E1 are mostly moving existing code behind new interfaces; E2 and E3 are the first new capabilities, and they are
also what the current strategies need most (maker fees, the cross-sectional book).

## 6. Decisions to make along the way

- **Synchronous strategies, async I/O.** Strategies and the pure core stay synchronous functions; feeds and adapters
  are async (as the candle feed is now). The loop is one asyncio task, so ordering is deterministic.
- **Vectorised research stays.** `run_book` remains the fast path for weight strategies; replay is the exact path.
  A parity test keeps them equal where both apply.
- **Event log format:** SQLite for decisions and orders (next to the trade log), Parquet for market events.
- **Money in `Decimal`** from the book outwards (orders, fills, cash), floats inside strategies and research.
- **Config:** a strategy section per strategy type (`[[sleeves]]` stays for weight strategies; `[[strategies]]` with
  `kind = "basket" | "options" | ...` for the rest), validated like today.
