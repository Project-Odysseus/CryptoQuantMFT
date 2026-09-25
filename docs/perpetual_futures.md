# Perpetual futures (sandbox)

Status: first slice, **simulation only**. Nothing here places a real futures order. The older dated-futures
scaffolding (`src/execution/contracts/`, `src/execution/sizing/`, `docs/FUTURES_GUIDE.md`) is a different product
(fixed contract sizes, expiries) and is untouched.

## Why perpetuals

Costs. Research at assumed perp fees (about 0.2% per round trip against about 1.0% on spot) turned the mean
reversion strategies from clearly losing to positive and improved the trend strategies, and perps allow real
shorting. See `docs/research_log.md`. The fee and funding figures used there and in the sandbox are assumptions.

## What exists

- `src/execution/perps.py`
  - `PerpContract`: linear perpetual spec (size step, min size, max leverage, maintenance margin, fees). Numbers
    from `assumed_perp_contract()` are placeholders (`verified=False`), not read from any exchange.
  - Margin math: `unrealized_pnl`, `initial_margin`, `maintenance_margin`, `liquidation_price`.
  - `SandboxPerpExecutionAdapter`: an in-process margin account. Orders fill at the submitted price. A fill locks
    margin instead of moving notional: the wallet changes only by realized PnL, fees and funding. It rejects
    orders below the minimum size, in the wrong symbol, or that would push the position past `max_leverage`
    (reducing orders are always allowed). `on_market_update()` marks to market, accrues funding for the elapsed
    time (longs pay, shorts receive when the rate is positive) and liquidates when equity falls to maintenance
    margin (the wallet is floored at zero and any shortfall is reported as `bad_debt`; there is no insurance fund).
- `PaperTradingEngine` (exchange cycle) treats adapters with `margin_account = True` differently: equity is wallet
  plus unrealized PnL, entry size is capped by free margin times leverage (`buying_power`), and funding and
  liquidation events are logged as `funding_accrued` / `position_liquidated` operational events.
- `RiskControlConfig.liquidation_buffer_pct`: forces an exit once price is within that fraction of the
  liquidation price (10% in the perp dry-run wiring).
- `main.py`: `--execution-exchange kraken_futures` with `--runtime live_dry_run`, and `--perp-max-leverage`
  (default 2, contract cap 5). Market data still comes from the Kraken **spot** feed, so the mark price is the
  spot price (no basis or index price). `--runtime paper` and `--runtime live` with `kraken_futures` exit with an
  error; `ExecutionRouter` also refuses `live`.

## Run it

```bash
python main.py --runtime live_dry_run --execution-exchange kraken_futures --perp-max-leverage 2 \
  --strategy keltner_breakout --strategy-params '{"window": 20, "atr_multiplier": 1.5}' --dashboard --report
```

Strategies return the same -1/0/1 target position as everywhere else; 0 closes whatever is open.

## Not built yet (roughly in order)

1. **Real venue connectivity**: futures connector (mark and index price, funding rate feed) and execution adapter
   (auth, order submit/cancel, positions, fills, margin balance). The API, symbols and separate wallet differ from
   Kraken spot. Needs a verified contract spec and fee schedule first, and confirmation the product is available
   to the account holder.
2. **Reconciliation** of futures positions and margin against the exchange (`reconcile_account_state` is spot-shaped).
3. **Persistence**: the sandbox account state is in memory only (a restart resets it), and funding is not stored
   per payment beyond the event log.
4. **Tax**: `enable_tax_logging` is off for perps. The FIFO spot ledger does not model derivatives, and the
   Norwegian treatment needs checking before real money.
5. **Live safety gates** for a futures venue: exchange risk limits, kill-switch cancel/flatten for futures, startup
   equity seeding from the margin balance, a leverage ceiling in `_validate_live_runtime_request`.
6. **Strategy timeframes**: the runtime builds bars from ticks (1s default) and has no history warmup, so the
   4h/daily strategies that research favours cannot run meaningfully yet (see `todo_important.md`).
