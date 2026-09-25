# Perpetual futures (sandbox)

Status: sandbox plus a **real Kraken Futures adapter** behind the live safety gates. The real adapter has only been
tested against recorded response shapes, never against the live API. The older dated-futures
scaffolding (`src/execution/contracts/`, `src/execution/sizing/`, `docs/FUTURES_GUIDE.md`) is a different product
(fixed contract sizes, expiries) and is untouched.

## Why perpetuals

Costs. Research at Kraken Futures' published fees (0.05% taker, about 0.2% per round trip with slippage, against
about 1.0% on spot) turned the mean reversion strategies from clearly losing to positive and improved the trend
strategies, and perps allow real shorting. See `docs/research_log.md`.

## Verified against the venue (public data, 2026-09-25)

`python main.py --futures-venue-check --futures-symbol BTC/USD` reads Kraken Futures' public endpoints (no
credentials, no orders) and prints the contract, fees, mark and index price and funding statistics. Findings:
`PF_XBTUSD` is a USD-quoted linear flexible future, size step 0.0001 BTC, tick 1 USD, taker 0.05% / maker 0.02% at
the entry tier, first-tier margin 1% initial (100x venue limit) and 0.5% maintenance in 8 size tiers, hourly
funding averaging about 0.009%/day for BTC and ETH and about 0 for SOL (it changes sign often). Kraken lists no
banned countries for the contract in its public data, but that is not the same as availability to your account:
confirm with Kraken. The contract is USD-quoted, so a EUR account carries currency exposure.
`src/data/kraken_futures.py` is the client and `perp_contract_from_instrument()` builds a verified `PerpContract`.

## What exists

- `src/execution/perps.py`
  - `PerpContract`: linear perpetual spec (size step, min size, max leverage, tiered maintenance margin, fees).
    `perp_contract_from_instrument()` builds a verified one from Kraken's public data; `assumed_perp_contract()`
    is an offline placeholder (`verified=False`).
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

## Real orders (`--runtime live`)

`src/execution/kraken_futures_adapter.py` (`KrakenFuturesExecutionAdapter`) trades one perpetual through the
authenticated REST API, following Kraken's OpenAPI spec (https://docs.kraken.com/openapi/futures-rest.yaml):

- Requests are signed per Kraken's scheme: SHA-256 of the URL-encoded parameters + nonce + endpoint path, then
  HMAC-SHA-512 with the base64-decoded secret. The signature covers the exact encoded body that is sent.
- Every order is an IOC market order (`orderType=mkt`, which Kraken caps at 1% from the market) with a unique
  `cliOrdId`. Pure reductions are sent `reduceOnly=true`. Size rounding, the minimum size and this account's
  leverage cap are checked locally first.
- Fills are reconciled from `/fills` by `cliOrdId` (the order-status endpoint forgets orders 5 seconds after they
  finish), so a lost HTTP response still gets booked once. Position, entry price, margin equity and available
  margin are then re-read from `/openpositions` and `/accounts` (the `flex` multi-collateral wallet) and treated as
  the truth. A position that changes without our order is logged as `position_liquidated` or
  `position_changed_externally`.
- Fills carry no fee in Kraken's API, so fees are estimated from the contract's taker rate.
- The kill switch also calls `/cancelallorders` for the contract.

Gates (all required, on top of the normal `--enable-live-trading --live-confirmation ENABLE_LIVE_TRADING`):
`KRAKEN_FUTURES_API_KEY` and `KRAKEN_FUTURES_SECRET` in `.env` (a futures key, separate from the spot key), a
contract spec fetched from Kraken's public API at startup (no placeholder), `--perp-max-leverage` at most 3, the
`kraken_futures` exchange risk limits (same caps as spot Kraken), and a ready, inactive kill switch. Tax logging is
off for perps. Startup equity is Kraken's `marginEquity` in USD.

Before a first live run: `python main.py --futures-verify-credentials --futures-symbol BTC/USD` makes read-only calls
(accounts, positions, open orders) and prints what it sees. It places no orders.

The runtime symbol for perps is USD-quoted (`--trading-symbol BTC/USD`, the default when `kraken_futures` is used),
matching the contract.

## Not built yet (roughly in order)

1. **Availability**: whether Kraken Futures is open to the account holder in Norway is not in the public data.
2. **Market data**: the runtime still takes prices from the Kraken spot feed, not the futures mark price.
3. **Persistence**: the sandbox account state is in memory only (a restart resets it), and funding is not stored
   per payment beyond the event log.
4. **Tax**: `enable_tax_logging` is off for perps. The FIFO spot ledger does not model derivatives, and the
   Norwegian treatment needs checking before real money.
5. **Flatten on kill switch**: the kill switch cancels orders but does not close an open position.
6. **Strategy timeframes**: the runtime builds bars from ticks (1s default) and has no history warmup, so the
   4h/daily strategies that research favours cannot run meaningfully yet (see `todo_important.md`).
