# Priority 3 execution tracker

This file breaks **Priority 3: Close the Live-Execution Gap Safely** into concrete subtasks. Each item should be checked off here when completed, and mirrored back into `TODO.MD`.

- [x] **Subtask 1: Define the live-dry-run execution path**
  - [x] Trace the current `paper`, `live_dry_run`, and `live` runtime wiring from CLI to orchestrator to adapters
  - [x] Identify the exact missing seams that block `live_dry_run` from acting as the pre-live promotion lane
  - [x] Write the target behavior for `live_dry_run` vs `live` so the implementation keeps trading disabled by default
  - **Current wiring**
    - CLI accepts `--runtime paper|live_dry_run|live` and passes it into `RuntimeConfig`, `ExecutionRouter`, `PaperTradingEngine`, and `RuntimeOrchestrator`.
    - `paper` runs with no execution adapter.
    - `live_dry_run` already gets a sandbox adapter shaped like the requested exchange.
    - `live` currently selects exchange adapters, but `RuntimeOrchestrator.run_once()` still aborts with `NotImplementedError` before execution.
  - **Blocking seams**
    - The runtime still treats `live` as a hard stop instead of a guarded execution path.
    - `live_dry_run` is not yet explicitly documented/enforced as the only pre-live promotion lane.
    - There is no explicit operator approval gate or second confirmation path for real `live`.
    - There is no acceptance coverage proving promotion/failure behavior across `paper`, `live_dry_run`, and guarded `live`.
  - **Target behavior**
    - `paper`: baseline source-of-truth lane using paper fills and the existing reporting/debug stack.
    - `live_dry_run`: use exchange-specific routing/reconciliation surfaces and production-like runtime state handling, but never permit production order placement.
    - `live`: remain blocked unless explicit safety gates, operator confirmation, kill-switch readiness, and strict caps are satisfied.
    - The next implementation order should be: `live_dry_run` orchestration, Kraken state hardening, `live` safety gate, operator checklist, then acceptance tests.

- [x] **Subtask 2: Wire `live_dry_run` through the runtime orchestrator**
  - [x] Route `live_dry_run` cycles through exchange adapters and reconciliation without enabling production trading
  - [x] Preserve paper-mode behavior as the source-of-truth baseline
  - [x] Persist health, order, reconciliation, and kill-switch context for `live_dry_run`
  - **Completed changes**
    - Resolved a real exchange-selection seam so `live_dry_run` now aligns its sandbox execution adapter with the effective market-data venue instead of drifting to generic `sandbox`/`USD` state when exchange selection is `auto`.
    - Aligned Firi fallback behavior so missing Firi credentials move both the market-data connector and the dry-run adapter back to Kraken together.
    - Kept `paper` behavior unchanged while adding explicit tests that `live_dry_run` runs through exchange-shaped adapter/account-state handling and still avoids real live execution.

- [x] **Subtask 3: Harden Kraken adapter state handling**
  - [x] Verify symbol mapping, remote order IDs, balances, and reconciliation payload handling against Kraken-shaped responses
  - [x] Ensure non-destructive auth/status/cancel flows behave correctly when credentials are missing or remote responses are partial
  - [x] Add tests for order lifecycle and account-state recovery using Kraken-shaped fixtures
  - **Completed changes**
    - Kraken order submission/status handling now normalizes exchange-shaped order payloads instead of depending on one narrow `QueryOrders` shape.
    - Kraken recovery now accepts Kraken-style balance and order payloads and converts them into the generic reconciliation structure used by the runtime.
    - Kraken adapter state now defaults to `EUR` cash handling, while cancel/status flows preserve local staged state when credentials are absent.

- [x] **Subtask 4: Add safety gates for live promotion**
  - [x] Prevent accidental `--runtime live` execution during development
  - [x] Add a second confirmation / explicit enablement mechanism for real live mode
  - [x] Require kill-switch readiness and strict low-risk caps before any live promotion path is considered valid
  - **Completed changes**
    - Added CLI-level live guards requiring `--enable-live-trading` plus the exact `--live-confirmation ENABLE_LIVE_TRADING` token.
    - Blocked `--runtime live` when exchange selection is still `auto`, when a mock connector is requested, when the kill switch is active, or when exchange caps exceed the conservative live ceiling.
    - Added a kill-switch readiness helper plus tests covering both rejected and allowed guarded-live requests.

- [x] **Subtask 5: Add operator approval and runbook checks**
  - [x] Create a manual approval checklist for `paper -> live_dry_run`
  - [x] Create a stricter manual approval checklist for `live_dry_run -> live`
  - [x] Document the non-destructive verification steps operators must complete before promotion
  - **Completed changes**
    - Added explicit runbook checklists for `paper -> live_dry_run` and `live_dry_run -> live`.
    - Added a concrete exchange-shaped `live_dry_run` command for Kraken as the near-term promotion lane.
    - Documented the operator checks required before any guarded `live` attempt and before any later promotion with real capital.

- [x] **Subtask 6: Add acceptance coverage for promotion and failure handling**
  - [x] Add tests for `paper -> live_dry_run` promotion behavior
  - [x] Add tests for stale data, order rejection, reconciliation mismatch, and cancel failure paths
  - [x] Confirm the runtime still rejects real live trading unless all explicit live guards are satisfied
  - **Completed changes**
    - Added promotion coverage showing `paper` keeps no execution adapter while `live_dry_run` adds exchange-shaped sandbox routing.
    - Added acceptance coverage for dry-run rejection context, kill-switch cancel failure capture, and the guarded-live rejection boundary.
    - Kept explicit coverage that guarded `live` stays behind the CLI safety gates and conservative exchange caps.

- [x] **Subtask 7: Implement the guarded `live` runtime path**
  - [x] Replace the current `NotImplementedError` hard-stop in `RuntimeOrchestrator` with a guarded live execution flow
  - [x] Reuse the existing exchange adapter, reconciliation, health-report, and kill-switch surfaces without bypassing the new live CLI safety gates
  - [x] Keep real live execution conservative and explicit so the runtime behavior matches the documented promotion model
  - **Completed changes**
    - Replaced the old live-mode hard stop with an incremental exchange-backed cycle path instead of replaying the full paper history every runtime loop.
    - Kept `live_dry_run` and `live` on the same exchange-backed execution model so staged orders can remain `SUBMITTED/OPEN` across cycles without being auto-canceled as fake paper rejections.
    - Added coverage proving guarded `live` can run through the orchestrator path while still staying behind the CLI safety gates and without requiring this session to place real orders.

- [ ] **Remaining Priority 3 closeout checks**
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
  - [ ] Validate partial-fill and resting-open-order behavior against real Kraken state instead of immediate-fill assumptions — **partially validated 2026-09-25**: the real live sessions above proved async full-fill reconciliation (submit → SUBMITTED → later polled and correctly detected as FILLED) works after the bug fixes. A genuine *partial* fill (an order that fills less than its full size and stays open) was never actually exercised — all real fills this session were full fills. Still open.
  - [x] Confirm strategy-driven live orders obey the same Kraken minimum-size, precision, and balance checks as manual orders (2026-09-25: `KrakenExecutionAdapter.submit_order` — the method every strategy-driven live order routes through via `PaperTradingEngine.run_exchange_cycle()` → `_route_exchange_order()` — previously only checked `size > 0` and passed the strategy's raw size straight to `AddOrder`. It now fetches real Kraken pair metadata (cached per pair to avoid hammering the public API every cycle), rounds to `lot_decimals`, and rejects before calling `AddOrder` if the rounded size is below `ordermin`, the notional is below `costmin`, or the live EUR/base-asset balance is insufficient — the exact checks `submit_quote_order`/`submit_close_position` already used for manual orders. If the metadata/balance fetch itself fails (network/API issue), the check is skipped rather than blocking, matching this adapter's existing fail-gracefully behavior — Kraken's own `AddOrder` response remains the backstop in that case. Tests added in `tests/test_execution_adapters.py` (5 new: below-min-size, below-min-cost, insufficient quote balance, insufficient base position on sell, precision rounding). Not yet exercised against a real Kraken account with live strategy orders — only unit-tested.
  - [ ] Re-run Kraken verification when the account contains at least one real historical or open order
  - [ ] Confirm recovered real Kraken order IDs, symbol mapping, and reconciliation state from populated exchange data
  - [ ] Confirm kill-switch preview targets the expected real open Kraken orders

- [ ] **Position-level risk controls: closeout for the exchange-backed path**
  - [x] Added `RiskManager.evaluate_exit()` (time-stop + ATR stop-loss) and a rolling daily-loss circuit breaker (`RiskControlConfig.daily_loss_limit_pct`, distinct from the existing all-time peak `hard_stop_drawdown_pct`) on 2026-09-25. Fully wired into `PaperTradingEngine.run()` (the deterministic backtest/paper-mode loop) with tests (`tests/test_risk_controls.py`, `tests/test_paper_trading.py`).
  - [x] `run_exchange_cycle()` (the `live_dry_run`/`live` per-cycle path) got the daily-loss circuit breaker (day-start equity tracked as an instance attribute on `PaperTradingEngine`, so it survives across cycles within one running process but resets on restart — no checkpoint persistence yet).
  - [x] `run_exchange_cycle()` now gets the time-stop / ATR stop-loss exits too (2026-09-25). Root-caused first: `_current_account_state()` was faking `avg_entry_price` as just the *current* price, which would have made the ATR stop a no-op even if wired in blindly. Fixed at the source: `ExecutionAdapter._apply_fill_to_account_state()` (the shared base class fill handler used by Sandbox/Kraken/Firi) now tracks a real weighted-average entry price and open timestamp per position, cleared when the position returns to flat. `get_account_snapshot()` exposes both; `_current_account_state()` reads the real entry price instead of faking it, and returns the open timestamp too. `run_exchange_cycle()` computes `bars_held` by counting bars at/after that timestamp and calls the same `RiskManager.evaluate_exit()` used by `run()`, so a stale/adverse position now force-closes in `live_dry_run`/`live` exactly like it does in paper mode. Tests added in `tests/test_execution_adapters.py` (entry-price/open-time tracking across adds and a full close) and `tests/test_paper_trading.py` (exchange-cycle force-close on time-stop with no exit signal). Smoke-tested via `--runtime live_dry_run --use-mock-connector`.
  - [ ] The ATR stop implemented is a **fixed** stop distance from the entry price, not a trailing stop that ratchets in the position's favor (`TODO.MD`'s Phase 4 line still says "ATR-driven trailing stop-loss logic" — trailing behavior is a separate, not-yet-done enhancement).
  - [ ] Counterparty/balance-distribution monitoring (Firi NOK vs Kraken EUR/BTC) from the same `TODO.MD` section is still untouched.
  - [ ] `daily_loss_limit_pct`/`time_stop_bars`/`atr_stop_multiplier`/`position_drawdown_stop_pct` defaults set in `main.py::build_runtime_orchestrator` (5% daily loss, 60-bar time stop, 3x ATR, 5% drawdown stop) are first-pass placeholder values, not tuned against any real backtest — revisit before relying on them for anything beyond paper mode.
  - [x] Added `RiskControlConfig.position_drawdown_stop_pct` (2026-09-25): a plain percentage stop against entry, independent of ATR, checked in `evaluate_exit()` for either side. Wired into `PaperTradingEngine.run()`/`run_exchange_cycle()` (already covered by the shared `evaluate_exit()` call) and, separately, into `SimpleBacktester.run()` (which did not call `evaluate_exit()` at all before this — the time-stop/ATR-stop from the entry above were never reflected in `--demo-backtest`/backtest-script equity curves until now). Tested in `tests/test_risk_controls.py` and `tests/test_simple_backtest.py`. Backtested against real Kraken BTC/EUR data at 5%: never triggered, because the whole test window's price only ranged ~4.6% end to end — an honest null result, not a bug.
  - [x] Enabled real short-position support end to end (2026-09-25), after finding that `SimpleBacktester` was already opening genuine short positions while `run_exchange_cycle()` (`live_dry_run`/`live`) hard-blocked them (`spot_shorting_disabled`) — so backtests were silently crediting P&L from trades that could never actually execute live. Fixed:
    - `ExecutionAdapter._apply_fill_to_account_state()` (shared by Sandbox/Kraken/Firi) previously clamped any position at zero (`max(0.0, ...)`) on a sell, and separately never cleaned up a `buy` fill that exactly covered a short back to flat, and treated *any* previous non-positive position as "fresh entry" (silently resetting a short's real entry price on every partial cover). All three fixed; both the buy and sell branches now correctly handle opening a short from flat, adding to a short, partially covering a short, fully covering a short, and flipping straight through flat from one side to the other within a single fill.
    - `PaperTradingEngine` gained `allow_short: bool = False`. `PaperTradingEngine.run()` (paper mode) already shorted unconditionally regardless of this flag — that was pre-existing and unrelated to this change — so `allow_short` only gates the new short-entry/short-close branches added to `run_exchange_cycle()`.
    - `main.py::build_runtime_orchestrator` sets `allow_short=True` for `paper` and `live_dry_run` (both simulated; `live_dry_run` always routes through `SandboxExecutionAdapter` regardless of exchange, so this can never become a real order) and `allow_short=False` for `live` unconditionally — real Kraken/Firi spot has no margin/short capability, and this must never change without that capability actually existing.
    - `StrategyRegistry` gained `can_short(name)` / `register(..., can_short=...)`, and the runtime startup banner now prints `strategy_can_short: <yes/no>` and `shorting_enabled_this_run: <bool>` so an operator can see both facts before a run starts, per explicit request — smoke-tested via the banner output for `paper`, `live_dry_run`.
    - Tests added across `tests/test_execution_adapters.py` (short open/add/partial-cover/full-cover/flip-both-directions), `tests/test_paper_trading.py` (`run_exchange_cycle` blocks by default, opens+closes a short when `allow_short=True`), and `tests/test_runner.py` (registry `can_short`).
    - [x] Added the long-biased asymmetric variant (2026-09-25): `volume_confirmed_momentum_strategy` gained optional `short_threshold`/`short_volume_multiplier`/`allow_short` parameters (default to the symmetric behavior when unset), and a new registered strategy `volume_confirmed_momentum_biased` (`volume_confirmed_momentum_biased_strategy`) wraps it with a short side that requires a 2x bigger confirmed move and 1.5x more volume than the long side. Backtested against the same real Kraken BTC/EUR window: cut short trades from 11 to 1 (the 10 filtered ones weren't a clean win either — filtering to just the strongest short left a nearly identical net result, -0.0395% vs -0.0391%, not a proven improvement over this short, calm sample). Tests added in `tests/test_simple_backtest.py` and `tests/test_runner.py`.

- [ ] **Research ↔ runtime signal parity (found 2026-09-25 while building the research toolkit)**
  - [x] **Exit semantics aligned (2026-09-25, on explicit go-ahead).** `SimpleBacktester` (every backtest and research sweep) treats a signal of 0 as "be flat" and closes on `signal <= 0` / `>= 0`; the runtime used to close a long only on `signal < 0`, so any strategy that exits by returning 0 (all the new entry/exit strategies, `momentum_breakout`, `band_reversion`, `make_long_only` wrappers) would have kept holding live positions the backtest had closed. `PaperTradingEngine.run()` and `run_exchange_cycle()` now close a long on `signal <= 0` and a short on `signal >= 0`. There is no size scaler yet, so 0 always means "exit the whole position". Revisit when portfolio-level sizing exists (0 may then mean "target size 0" among partial sizes). Tests: `test_engine_closes_a_long_when_signal_returns_to_zero`, `test_exchange_cycle_closes_long_and_short_positions_when_signal_returns_to_zero`; two existing tests that used 0 to mean "hold" now hold with 1.
  - [x] Found and fixed while building perpetual futures: `PaperTradingEngine._current_account_state` only returned an entry price and open time for longs (`position_size > 0.0`), so in `run_exchange_cycle` (`live_dry_run`/`live`) `evaluate_exit` got `None` for shorts and the drawdown, time and ATR stops never fired on them, and shorts showed no unrealized PnL. Both now use `position_size != 0.0`. Regression test: `test_position_stop_loss_now_applies_to_shorts_in_exchange_cycles`.
  - [ ] Found while testing: the paper `run()` loop cancels a short sell from flat (`('sell', 'CANCELED')`), so paper mode cannot actually open shorts through `run()`. The exchange-backed path (`live_dry_run`) can. Not yet investigated.
  - [x] **Re-entry after a forced exit (fixed 2026-09-26).** After a risk stop closed a position, the next bar re-entered if the signal still said the same side, so a stop only reset the entry price. `gate_reentry` (`src/risk/controls.py`) now holds that side flat until the signal has left it at least once (the opposite side is never blocked), in `SimpleBacktester`, `PaperTradingEngine.run()` and `run_exchange_cycle()`; the exchange cycle records the blocked entry as `reentry_after_forced_exit`. The block lives in memory, so a process restart clears it.
  - [x] **Bar interval and warmup (2026-09-26).** `--bar-interval 4h|1d|...` builds bars of that length independently of the poll interval (`StreamingAggregator.drain_completed`), the strategy acts only when a bar completes and in-between polls run `PaperTradingEngine.run_mark_cycle` (fills, funding, liquidation, equity), and `--warmup-bars N` seeds history from Kraken spot OHLC or futures mark candles. Also found: runtime tests wrote into the real `data/cryptoquant.db`, shared a perp sandbox state file with real runs, and posted real Telegram alerts; `tests/conftest.py` now isolates all three.

- [x] **Allowed entries sized to zero (fixed 2026-09-26).** `RiskManager.evaluate` multiplied the position size by a "Kelly" factor estimated from the market's last 20 bar returns, not from the strategy's trades. It returned 0 whenever recent bars fell more than they rose (42% of sampled points on BTC 4h), ignored trade direction, and so turned allowed entries into zero-size orders with no reason recorded (`entries_were_allowed_but_no_fill_was_recorded`). It is now opt-in (`RiskControlConfig.kelly_sizing`, default False); sizing is `risk_per_trade_pct / volatility`, capped by the position and notional limits and the engine's per-trade risk cap. This changes position sizes in paper, dry-run, live and `run_backtest`-based backtests (not the research toolkit, which uses no risk manager). A trade-outcome-based Kelly would be the proper replacement if sizing by edge is wanted later.

- [ ] **Entry sizing mixes units (found 2026-09-26 while adding volatility-target sizing)**
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
  - [ ] Decide whether `--target-annual-vol` becomes the default and the unit-based path is removed. That would
    change the paper baseline's sizes, so it needs the user's go-ahead.

- [x] **First real `--runtime live` sessions (2026-09-25) — found and fixed 3 real bugs, none catastrophic**
  - Ran `moving_average_crossover` live on Kraken BTC/EUR with `--risk-per-trade-pct 0.35` (needed to clear Kraken's minimum order size on a ~14 EUR account) across 4 short guarded sessions. Every bug below was caught because it was actually run live, not because it was anticipated — this is the expected/intended value of live-testing before scaling size.
  - **Bug 1 — equity initialized wrong.** Already fixed and committed before the first run this session (`d1e46c3`): `initial_cash` was hardcoded 1000.0 regardless of mode, comparing a real ~14 EUR balance against a fake 1000 EUR peak and blocking every entry with `drawdown_limit`. No real order was placed on that first attempt.
  - **Bug 2 — no fill reconciliation, causing a duplicate real order.** After fixing bug 1, a real buy filled but the adapter's local state never learned about it (nothing polled Kraken after `submit_order()` returned `SUBMITTED`), so the next cycle still thought it was flat and placed a second real buy before it could be stopped. Real consequence: position size doubled (0.00006752 + 0.00006746 BTC) versus what the strategy intended for a single entry; no loss beyond ~0.04 EUR total fees. Fixed with `PaperTradingEngine._reconcile_exchange_fills()` (`0b15cb0`), polling pending orders each cycle via the adapter's existing `recover_execution_state()`.
  - **Bug 3 — `KrakenExecutionAdapter.get_order_status()` mutated the order before `reconcile_order_state()` could diff it**, which silently defeated bug 2's fix on the very next live test: a real sell correctly closed the doubled position, but local state was never updated, so the engine spent the rest of that run (until it hit Kraken's rate limit) repeatedly trying to resubmit the same sell. Every attempt was correctly rejected by the order-parity balance check (real safety net working as designed — no further real trades happened), but it burned ~280 wasted API calls. Fixed by making `get_order_status()` read-only (`8a9e1ab`).
  - **A 4th real session, after all three fixes, ran clean**: one real buy, one real sell, no duplicate orders, correct local reconciliation confirmed against Kraken's own closed-order history. This is the first evidence the exchange-backed live loop can run a full entry/exit cycle correctly end to end.
  - **Known remaining gap, not yet fixed**: even in the clean run, the trades that got auto-logged via `_reconcile_exchange_fills` recorded `fee=0.0` instead of the real fee — Kraken's `QueryOrders` response can report `status=closed` before the `fee` field settles, and the reconciliation code trusts whatever it gets back on the first poll. This corrupted the auto-generated tax-ledger rows for those trades (fee-based `TRADING_FEE` events were silently skipped). Manually corrected this session via a one-off backfill script (rolled back the wrong tax-ledger rows/fiat-lot state, re-inserted all 5 real trades from this session with the correct fees pulled from Kraken's real closed-order history, restored the EUR fiat lot to its pre-session state first) — final EUR pool total (13.9893) matches the real Kraken balance (13.9892) to the cent. **Before any larger real capital or unattended run**, fix this properly: either re-poll once more after a short delay before trusting a zero/missing fee, or treat `fee` as authoritative only when Kraken's response explicitly includes a nonzero trade count for the order.
  - **Process note, not a bug**: `moving_average_crossover`'s default windows (`short_window=3`, `long_window=6`) on real 1-second bars produce very fast, noisy signal flips — barely a "moving average" at that timescale. Worth reconsidering bar interval or window size for any future live test so signals reflect real momentum rather than tick noise.
  - DB backups taken before each ledger correction this session: `data/cryptoquant.db.bak.20260925130620`, `data/cryptoquant.db.bak.20260925184648`.
