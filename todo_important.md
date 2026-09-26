# Live-execution tracker (Kraken spot and Kraken Futures)

The concrete work to make live trading safe, with the history of what was found in real runs. Tick items here and
mirror them in `TODO.MD`.

## Open

- [ ] **Spot: partial fills and resting orders against real Kraken state.** Async full fills are validated live
  (2026-09-25). **Resting order validated 2026-09-26:** a 0.00005 BTC limit buy 15% under the bid
  (`OQS5AR-Y7MBP-M7EQMA`) went through `submit_order`, Kraken listed it open as `BTC/EUR`, status and recovery read
  it as `OPEN` with nothing filled, and the engine's reconciliation kept it pending and logged no trade. Still open:
  a genuine partial fill, which can't be forced at minimum size (it needs an order larger than the trades that hit it)
- [x] **Spot: re-run the verification against populated exchange data.** Done 2026-09-26: `--kraken-verify-dry-run`
  passes all 7 checks, and Kraken's 7 closed orders match the 7 real trades in the database on side, size, price and
  fee. It found one bug, fixed: order history names the pair by its altname (`XBTEUR`), which recovered as
  `XBTEUR` instead of `BTC/EUR`
- [ ] **`live_dry_run` fills look like real fills in `trades`** (exchange `kraken`, source `paper_trading`; e.g.
  trades 23594-23597 on 2026-09-25). The tax ledger is unaffected (checked), but reports and dashboards can't tell
  sandbox fills from real ones. Tag them (exchange `kraken_sandbox` or a mode column)
- [x] **Spot: kill-switch preview against a real open order** (the cancel target list, checked against Kraken's
  actual open orders). Done 2026-09-26, together with a real activation. The test found that `--kill-switch` on its
  own cancelled nothing on the exchange (it only wrote the state file), and that the single-strategy runtime never
  re-read that file. Both fixed: it now cancels every Kraken order and closes every Kraken Futures position
  reduce-only by itself, and `--kill-switch-reset` re-arms it. Live result: the preview from a fresh process listed
  the resting order `OQS5AR-Y7MBP-M7EQMA`; `--kill-switch` cancelled it (Kraken: `canceled`, nothing filled, "User
  requested"), `CancelAll` found nothing left, Kraken Futures was flat, the Telegram alert arrived, and the reset
  re-armed it
- [x] **Spot: reconciled fills can record `fee=0`.** Kraken's `QueryOrders` can report `closed` before the fee
  settles, and reconciliation trusted the first answer. This corrupted tax rows once (backfilled by hand on
  2026-09-25). Fixed 2026-09-26: a fill with no fee waits up to `FEE_SETTLE_MAX_POLLS` (3) cycles for it, then is
  logged with an `exchange_fee_unsettled` warning. Partial fills now log only their share of the fee, and a
  synchronous fill can't be logged a second time by reconciliation
- [x] **Futures (single-contract adapter): IOC write-off race.** It marked an order cancelled on the first check if
  `/fills` hadn't caught up yet, which could lose a real fill. Fixed 2026-09-26: both Kraken Futures adapters now
  share the same rule (3 checks and at least 15 s old before an unfilled order is written off)
- [x] **Risk exits: trailing ATR stop.** The ATR stop was a fixed distance from the entry. Added 2026-09-26:
  `atr_trailing = true` trails it behind the best close since entry (reason `atr_trailing_stop`), for the runtime
  and for portfolio sleeves
- [x] **Risk exits: placeholder defaults.** The 5% daily loss, 60-bar time stop, 3x ATR and 5% drawdown stop in
  `build_runtime_orchestrator` were never tested against a backtest. Tested 2026-09-26
  (`scripts/research/stop_study.py`, research log): the time stop is off, the ATR stop trails, the position stop is
  10%, and the hard stop is set to 10% (it had inherited 2%, which would have shut the runtime down routinely)
- [x] **Paper `run()` can open a short from flat** (the sell used to be cancelled; fixed 2026-09-26 with a
  regression test)
- [ ] **Futures: trigger the kill switch once against a real open position at minimum size** (mirrors `TODO.MD`
  section 1)

Dropped: Kraken/Firi counterparty monitoring (all trading is on Kraken).

## Done: the guarded live path (2026-09-18 to 2026-09-25)

1. The `live_dry_run` path is defined and wired through the orchestrator, with exchange-shaped sandbox routing and
   reconciliation, and it never places a real order.
2. The Kraken adapter's state handling was hardened (symbol mapping, remote order IDs, balances, partial responses)
   and tested with Kraken-shaped fixtures.
3. Live gates: `--enable-live-trading` plus `--live-confirmation ENABLE_LIVE_TRADING`, no `auto` exchange, no mock
   connector, a ready and inactive kill switch, and conservative exchange caps.
4. Operator checklists are in the runbook for `paper -> live_dry_run` and `live_dry_run -> live`.
5. There is acceptance coverage for promotion, stale data, order rejection, reconciliation mismatch and cancel
   failure.
6. The guarded `live` runtime path replaced the old `NotImplementedError`; the exchange-backed cycle is
   incremental.

## History (details of completed work and incidents)

- **Priority 3 closeout checks** (the open ones are listed at the top)
  - [x] Add a non-destructive Kraken verification command covering auth, balances, open/closed orders, status/cancel probes, and validate-only order requests
  - [x] Add a kill-switch preview against recovered Kraken order state
  - [x] Add preview-only manual Kraken open and close-position flows
  - [x] Add guarded manual Kraken open and close-position submission flows
  - [x] Add a concise account-summary/holdings surface for balances, positions, open orders, and recent live/manual actions (2026-09-25: `--account-summary` in `main.py`, `_run_account_summary`). Non-destructive: fetches live Kraken balances/positions/open orders (if credentials configured), pair metadata + current ask to compute "smallest order Kraken will accept right now" in EUR, plus recent trades and recent manual/live action events. Verified live against the real account: EUR 14.1367, flat, no open orders, current BTC/EUR minimum ≈3.69 EUR (binding on `ordermin`, not `costmin`). Along the way, fixed `TradeLogger.list_events` to support filtering by `event_types` via SQL — without it, the last-500-events window was entirely crowded out by `runtime_cycle_completed`/`runtime_health_snapshot` noise and the actual manual-action events (days old) never surfaced.
  - [x] Execute one tiny real Kraken round-trip (2026-09-18: manual BTC/EUR buy 5.021e-05 @ 69712.4, close/sell @ 69967.5, ~0.0128 EUR realized gain) — trades persisted via `cli_manual_live_order`/`cli_manual_live_close`
    - **Found and fixed a real gap:** both tax-ledger writes silently failed at trade time (`EUR fiat pool is insufficient` / `asset lot inventory is insufficient for BTC`) because the EUR fiat pool was never seeded before the trade. Worse, the CLI printed `"Tax logging: recorded for this fill"` even though it had failed, since `log_trade` swallowed the exception and always returned a trade id.
    - Recovered the real EUR deposit from Kraken's own `Ledgers` endpoint (read-only, non-destructive): 15.00 EUR gross, 0.82 EUR Kraken deposit fee, 14.18 EUR net, at 2026-09-18T11:59:40Z.
    - Backfilled the tax ledger directly via `TradeLogger.log_fiat_conversion` (seed lot, historical timestamp) + `TradeLogger.record_trade_tax_events` for both trades, using the real Norges Bank EUR/NOK rate already cached for that date (10.8095). `tax_ledger`, `tax_eur_fiat_lots`, and `tax_asset_lots` now correctly reflect this trade. DB was backed up first (`data/cryptoquant.db.bak.20260925130620`).
    - Fixed the root cause: `TradeLogger.log_trade` now returns `(trade_id, tax_event_error)` instead of swallowing the failure, and the manual submit/close CLI paths (`_run_kraken_order_submission`, `_run_kraken_close_submission` in `main.py`) now print an explicit `"Tax logging: FAILED"` warning with the reason instead of falsely claiming success. Chosen over a hard preflight block: a posteriori backfill via Kraken's own ledger proved workable, so visibility at trade time (not blocking) was the right fix.
    - Still open: comparing recovered exchange history (order IDs, closed-order payloads) end to end against this trade has not been done yet — see the unchecked items below.
  - [x] Confirm strategy-driven live orders obey the same Kraken minimum-size, precision, and balance checks as manual orders (2026-09-25: `KrakenExecutionAdapter.submit_order` — the method every strategy-driven live order routes through via `PaperTradingEngine.run_exchange_cycle()` → `_route_exchange_order()` — previously only checked `size > 0` and passed the strategy's raw size straight to `AddOrder`. It now fetches real Kraken pair metadata (cached per pair to avoid hammering the public API every cycle), rounds to `lot_decimals`, and rejects before calling `AddOrder` if the rounded size is below `ordermin`, the notional is below `costmin`, or the live EUR/base-asset balance is insufficient — the exact checks `submit_quote_order`/`submit_close_position` already used for manual orders. If the metadata/balance fetch itself fails (network/API issue), the check is skipped rather than blocking, matching this adapter's existing fail-gracefully behavior — Kraken's own `AddOrder` response remains the backstop in that case. Tests added in `tests/test_execution_adapters.py` (5 new: below-min-size, below-min-cost, insufficient quote balance, insufficient base position on sell, precision rounding). Not yet exercised against a real Kraken account with live strategy orders — only unit-tested.

- **Position-level risk controls for the exchange-backed path**
  - [x] Added `RiskManager.evaluate_exit()` (time-stop + ATR stop-loss) and a rolling daily-loss circuit breaker (`RiskControlConfig.daily_loss_limit_pct`, distinct from the existing all-time peak `hard_stop_drawdown_pct`) on 2026-09-25. Fully wired into `PaperTradingEngine.run()` (the deterministic backtest/paper-mode loop) with tests (`tests/test_risk_controls.py`, `tests/test_paper_trading.py`).
  - [x] `run_exchange_cycle()` (the `live_dry_run`/`live` per-cycle path) got the daily-loss circuit breaker (day-start equity tracked as an instance attribute on `PaperTradingEngine`, so it survives across cycles within one running process but resets on restart — no checkpoint persistence yet).
  - [x] `run_exchange_cycle()` now gets the time-stop / ATR stop-loss exits too (2026-09-25). Root-caused first: `_current_account_state()` was faking `avg_entry_price` as just the *current* price, which would have made the ATR stop a no-op even if wired in blindly. Fixed at the source: `ExecutionAdapter._apply_fill_to_account_state()` (the shared base class fill handler used by Sandbox/Kraken/Firi) now tracks a real weighted-average entry price and open timestamp per position, cleared when the position returns to flat. `get_account_snapshot()` exposes both; `_current_account_state()` reads the real entry price instead of faking it, and returns the open timestamp too. `run_exchange_cycle()` computes `bars_held` by counting bars at/after that timestamp and calls the same `RiskManager.evaluate_exit()` used by `run()`, so a stale/adverse position now force-closes in `live_dry_run`/`live` exactly like it does in paper mode. Tests added in `tests/test_execution_adapters.py` (entry-price/open-time tracking across adds and a full close) and `tests/test_paper_trading.py` (exchange-cycle force-close on time-stop with no exit signal). Smoke-tested via `--runtime live_dry_run --use-mock-connector`.
  - [x] Added `RiskControlConfig.position_drawdown_stop_pct` (2026-09-25): a plain percentage stop against entry, independent of ATR, checked in `evaluate_exit()` for either side. Wired into `PaperTradingEngine.run()`/`run_exchange_cycle()` (already covered by the shared `evaluate_exit()` call) and, separately, into `SimpleBacktester.run()` (which did not call `evaluate_exit()` at all before this — the time-stop/ATR-stop from the entry above were never reflected in `--demo-backtest`/backtest-script equity curves until now). Tested in `tests/test_risk_controls.py` and `tests/test_simple_backtest.py`. Backtested against real Kraken BTC/EUR data at 5%: never triggered, because the whole test window's price only ranged ~4.6% end to end — an honest null result, not a bug.
  - [x] Enabled real short-position support end to end (2026-09-25), after finding that `SimpleBacktester` was already opening genuine short positions while `run_exchange_cycle()` (`live_dry_run`/`live`) hard-blocked them (`spot_shorting_disabled`) — so backtests were silently crediting P&L from trades that could never actually execute live. Fixed:
    - `ExecutionAdapter._apply_fill_to_account_state()` (shared by Sandbox/Kraken/Firi) previously clamped any position at zero (`max(0.0, ...)`) on a sell, and separately never cleaned up a `buy` fill that exactly covered a short back to flat, and treated *any* previous non-positive position as "fresh entry" (silently resetting a short's real entry price on every partial cover). All three fixed; both the buy and sell branches now correctly handle opening a short from flat, adding to a short, partially covering a short, fully covering a short, and flipping straight through flat from one side to the other within a single fill.
    - `PaperTradingEngine` gained `allow_short: bool = False`. `PaperTradingEngine.run()` (paper mode) already shorted unconditionally regardless of this flag — that was pre-existing and unrelated to this change — so `allow_short` only gates the new short-entry/short-close branches added to `run_exchange_cycle()`.
    - `main.py::build_runtime_orchestrator` sets `allow_short=True` for `paper` and `live_dry_run` (both simulated; `live_dry_run` always routes through `SandboxExecutionAdapter` regardless of exchange, so this can never become a real order) and `allow_short=False` for `live` unconditionally — real Kraken/Firi spot has no margin/short capability, and this must never change without that capability actually existing.
    - `StrategyRegistry` gained `can_short(name)` / `register(..., can_short=...)`, and the runtime startup banner now prints `strategy_can_short: <yes/no>` and `shorting_enabled_this_run: <bool>` so an operator can see both facts before a run starts, per explicit request — smoke-tested via the banner output for `paper`, `live_dry_run`.
    - Tests added across `tests/test_execution_adapters.py` (short open/add/partial-cover/full-cover/flip-both-directions), `tests/test_paper_trading.py` (`run_exchange_cycle` blocks by default, opens+closes a short when `allow_short=True`), and `tests/test_runner.py` (registry `can_short`).
    - [x] Added the long-biased asymmetric variant (2026-09-25): `volume_confirmed_momentum_strategy` gained optional `short_threshold`/`short_volume_multiplier`/`allow_short` parameters (default to the symmetric behavior when unset), and a new registered strategy `volume_confirmed_momentum_biased` (`volume_confirmed_momentum_biased_strategy`) wraps it with a short side that requires a 2x bigger confirmed move and 1.5x more volume than the long side. Backtested against the same real Kraken BTC/EUR window: cut short trades from 11 to 1 (the 10 filtered ones weren't a clean win either — filtering to just the strongest short left a nearly identical net result, -0.0395% vs -0.0391%, not a proven improvement over this short, calm sample). Tests added in `tests/test_simple_backtest.py` and `tests/test_runner.py`.

- **Research ↔ runtime signal parity** (found 2026-09-25 while building the research toolkit)
  - [x] **Exit semantics aligned (2026-09-25, on explicit go-ahead).** `SimpleBacktester` (every backtest and research sweep) treats a signal of 0 as "be flat" and closes on `signal <= 0` / `>= 0`; the runtime used to close a long only on `signal < 0`, so any strategy that exits by returning 0 (all the new entry/exit strategies, `momentum_breakout`, `band_reversion`, `make_long_only` wrappers) would have kept holding live positions the backtest had closed. `PaperTradingEngine.run()` and `run_exchange_cycle()` now close a long on `signal <= 0` and a short on `signal >= 0`. There is no size scaler yet, so 0 always means "exit the whole position". Revisit when portfolio-level sizing exists (0 may then mean "target size 0" among partial sizes). Tests: `test_engine_closes_a_long_when_signal_returns_to_zero`, `test_exchange_cycle_closes_long_and_short_positions_when_signal_returns_to_zero`; two existing tests that used 0 to mean "hold" now hold with 1.
  - [x] Found and fixed while building perpetual futures: `PaperTradingEngine._current_account_state` only returned an entry price and open time for longs (`position_size > 0.0`), so in `run_exchange_cycle` (`live_dry_run`/`live`) `evaluate_exit` got `None` for shorts and the drawdown, time and ATR stops never fired on them, and shorts showed no unrealized PnL. Both now use `position_size != 0.0`. Regression test: `test_position_stop_loss_now_applies_to_shorts_in_exchange_cycles`.
  - [x] **Re-entry after a forced exit (fixed 2026-09-26).** After a risk stop closed a position, the next bar re-entered if the signal still said the same side, so a stop only reset the entry price. `gate_reentry` (`src/risk/controls.py`) now holds that side flat until the signal has left it at least once (the opposite side is never blocked), in `SimpleBacktester`, `PaperTradingEngine.run()` and `run_exchange_cycle()`; the exchange cycle records the blocked entry as `reentry_after_forced_exit`. The block lives in memory, so a process restart clears it.
  - [x] **Bar interval and warmup (2026-09-26).** `--bar-interval 4h|1d|...` builds bars of that length independently of the poll interval (`StreamingAggregator.drain_completed`), the strategy acts only when a bar completes and in-between polls run `PaperTradingEngine.run_mark_cycle` (fills, funding, liquidation, equity), and `--warmup-bars N` seeds history from Kraken spot OHLC or futures mark candles. Also found: runtime tests wrote into the real `data/cryptoquant.db`, shared a perp sandbox state file with real runs, and posted real Telegram alerts; `tests/conftest.py` now isolates all three.

- [x] **Allowed entries sized to zero (fixed 2026-09-26).** `RiskManager.evaluate` multiplied the position size by a "Kelly" factor estimated from the market's last 20 bar returns, not from the strategy's trades. It returned 0 whenever recent bars fell more than they rose (42% of sampled points on BTC 4h), ignored trade direction, and so turned allowed entries into zero-size orders with no reason recorded (`entries_were_allowed_but_no_fill_was_recorded`). It is now opt-in (`RiskControlConfig.kelly_sizing`, default False); sizing is `risk_per_trade_pct / volatility`, capped by the position and notional limits and the engine's per-trade risk cap. This changes position sizes in paper, dry-run, live and `run_backtest`-based backtests (not the research toolkit, which uses no risk manager). A trade-outcome-based Kelly would be the proper replacement if sizing by edge is wanted later.

- [x] **Entry sizing mixes units (found 2026-09-26 while adding volatility-target sizing; fixed 2026-09-26)**
  - **Fix:** `src/risk/sizing.py` now owns all sizing.
    - Every sizer returns a share of equity. The risk manager applies the caps, and the engine converts the share to
      units in `_resolve_order_size`, the only place this happens.
    - The old unit caps (`default_order_size` and `risk_per_trade_pct × equity / price`) and the market-return Kelly
      multiplier are gone.
    - Choose a method with `--sizing` / `--sizing-params` (list them with `--list-sizing`). It's saved in the runtime
      config.
    - The default, `fixed_fraction` at `--risk-per-trade-pct` (0.10), is what paper runs actually did before, so
      paper baselines keep their sizes.
    - `run_backtest` now defaults to full size (`BacktestConfig.sizing_params = {"fraction": 1.0}`), like the
      research toolkit. It used to be `min(1, 0.02 / 10-bar std)`.
    - Simulated partial fills are a share of each order instead of `default_order_size` units.
    - Tests: `tests/test_sizing.py`.
  - The original analysis, kept for reference:
  - The volatility term in the default sizing has no effect.
    - `RiskManager.evaluate` returns `risk_per_trade_pct / volatility` capped by `max_position_size`, and its own
      caps treat that number as a share of equity (`available_capacity / equity`).
    - `PaperTradingEngine._resolve_order_size` reads it as asset units. It takes the minimum of that,
      `default_order_size` (1 unit), `cash / price` and `risk_per_trade_pct × equity / price`.
    - For BTC and ETH the last cap always binds. So every entry is a fixed `risk_per_trade_pct` of equity: 10% by
      default, and 35% in the live sessions below, which is why `--risk-per-trade-pct 0.35` was what cleared
      Kraken's minimum.
  - For a coin cheap enough that the share read as units is less than 10% of equity in notional (e.g. DOGE), it
    would bind and give nonsense sizes.
  - `max_position_size` and the exchange caps are also compared with positions in units in one place and in shares
    in another.
  - [x] Opt-in `--target-annual-vol` (2026-09-26, `RiskControlConfig.target_annual_volatility`). It sizes each entry
    explicitly as a share of equity: target / EWMA forecast (10-day half-life), capped by `max_position_size`, the
    exchange's `max_notional_per_trade` and buying power. The engine converts the share to units at the entry
    price. Entries are refused with `volatility_forecast_unavailable` until there are 20 bars of history (use
    `--warmup-bars`). Research basis: `docs/research_log.md`, 2026-09-26 volatility entry.
  - [x] The unit-based path is removed (2026-09-26). The default stays `fixed_fraction` 10%, which keeps paper
    baseline sizes the same. Switching the default to `vol_target` is a separate decision for the user.

- [x] **First real `--runtime live` sessions (2026-09-25) — found and fixed 3 real bugs, none catastrophic**
  - Ran `moving_average_crossover` live on Kraken BTC/EUR with `--risk-per-trade-pct 0.35` (needed to clear Kraken's minimum order size on a ~14 EUR account) across 4 short guarded sessions. Every bug below was caught because it was actually run live, not because it was anticipated — this is the expected/intended value of live-testing before scaling size.
  - **Bug 1 — equity initialized wrong.** Already fixed and committed before the first run this session (`d1e46c3`): `initial_cash` was hardcoded 1000.0 regardless of mode, comparing a real ~14 EUR balance against a fake 1000 EUR peak and blocking every entry with `drawdown_limit`. No real order was placed on that first attempt.
  - **Bug 2 — no fill reconciliation, causing a duplicate real order.** After fixing bug 1, a real buy filled but the adapter's local state never learned about it (nothing polled Kraken after `submit_order()` returned `SUBMITTED`), so the next cycle still thought it was flat and placed a second real buy before it could be stopped. Real consequence: position size doubled (0.00006752 + 0.00006746 BTC) versus what the strategy intended for a single entry; no loss beyond ~0.04 EUR total fees. Fixed with `PaperTradingEngine._reconcile_exchange_fills()` (`0b15cb0`), polling pending orders each cycle via the adapter's existing `recover_execution_state()`.
  - **Bug 3 — `KrakenExecutionAdapter.get_order_status()` mutated the order before `reconcile_order_state()` could diff it**, which silently defeated bug 2's fix on the very next live test: a real sell correctly closed the doubled position, but local state was never updated, so the engine spent the rest of that run (until it hit Kraken's rate limit) repeatedly trying to resubmit the same sell. Every attempt was correctly rejected by the order-parity balance check (real safety net working as designed — no further real trades happened), but it burned ~280 wasted API calls. Fixed by making `get_order_status()` read-only (`8a9e1ab`).
  - **A 4th real session, after all three fixes, ran clean**: one real buy, one real sell, no duplicate orders, correct local reconciliation confirmed against Kraken's own closed-order history. This is the first evidence the exchange-backed live loop can run a full entry/exit cycle correctly end to end.
  - **Known remaining gap, not yet fixed**: even in the clean run, the trades that got auto-logged via `_reconcile_exchange_fills` recorded `fee=0.0` instead of the real fee — Kraken's `QueryOrders` response can report `status=closed` before the `fee` field settles, and the reconciliation code trusts whatever it gets back on the first poll. This corrupted the auto-generated tax-ledger rows for those trades (fee-based `TRADING_FEE` events were silently skipped). Manually corrected this session via a one-off backfill script (rolled back the wrong tax-ledger rows/fiat-lot state, re-inserted all 5 real trades from this session with the correct fees pulled from Kraken's real closed-order history, restored the EUR fiat lot to its pre-session state first) — final EUR pool total (13.9893) matches the real Kraken balance (13.9892) to the cent. **Before any larger real capital or unattended run**, fix this properly: either re-poll once more after a short delay before trusting a zero/missing fee, or treat `fee` as authoritative only when Kraken's response explicitly includes a nonzero trade count for the order.
  - **Process note, not a bug**: `moving_average_crossover`'s default windows (`short_window=3`, `long_window=6`) on real 1-second bars produce very fast, noisy signal flips — barely a "moving average" at that timescale. Worth reconsidering bar interval or window size for any future live test so signals reflect real momentum rather than tick noise.
  - DB backups taken before each ledger correction this session: `data/cryptoquant.db.bak.20260925130620`, `data/cryptoquant.db.bak.20260925184648`.
