# Runtime runbook

This runbook covers startup, daily checks, backup/restore, and recovery for the local paper-trading runtime.

## Files to monitor

- `data/cryptoquant.db` – the SQLite database used by the trade logger and daily summary persistence.
- `data/runtime_state.json` – the runtime checkpoint file written by the orchestrator when `--runtime-state-path` is provided or when the default runtime state path is used.
- `data/runtime_config.json` – optional runtime config snapshot written when `--runtime-config-path` is provided.
- `logs/app.log` – rotating application log output from the logger utility.

## Startup checklist

1. Activate the intended Python environment.
2. Confirm the runtime settings in `.env` are correct, especially `database_path`, `telegram_bot_token`, and `telegram_chat_id` if you intend to use Telegram alerts. Then run `python main.py --telegram-test`: it sends one sample trade message marked `[TEST]`. Runtime alerts fail quietly (a missing token only logs `telegram_notifier_skipped`), so this is the way to know messages arrive.
3. Start the runtime with a saved config and checkpoint path so the state can be recovered later:

```bash
python main.py \
  --runtime paper \
  --runtime-iterations 3 \
  --runtime-interval 1.0 \
  --runtime-config-path data/runtime_config.json \
  --runtime-state-path data/runtime_state.json \
  --live-plot \
  --live-plot-path plots/runtime_live_plot.png \
  --dashboard \
  --report \
  --daily-summary
```

4. For the repository’s known-good paper baseline, use the committed config:

```bash
python main.py \
  --runtime paper \
  --runtime-config-path config/runtime.paper.json \
  --dashboard \
  --report
```

5. For a deterministic smoke test before a real paper run, add `--use-mock-connector`.
6. Confirm the startup banner, the health snapshot, and the first operational events before leaving the runtime unattended.
7. The health snapshot now reports entry-decision reasons, current position-side PnL, the latest bar and signal, and the latest order/adapter context so you can audit blocked fills without digging through raw logs.
8. The live plot is written to `plots/runtime_live_plot.png` and updates each runtime cycle while the process is running.
9. `--runtime live` is intentionally guarded: it now requires `--enable-live-trading`, `--live-confirmation ENABLE_LIVE_TRADING`, an explicit `--execution-exchange`, and a ready/inactive kill-switch state before the CLI will proceed.
10. Before any live promotion, run the non-destructive Kraken verification probe:

```bash
python main.py --kraken-verify-dry-run --kraken-verify-symbol BTC/EUR
```

This verifies Kraken private-endpoint authentication, balance/open/closed-order normalization, status/cancel request handling, and the validate-only order path without placing a production order. It also previews which open Kraken orders the kill switch would attempt to cancel after recovering exchange-shaped order state locally.

11. Before the first real Kraken order, preview the intended tiny BTC/EUR buy without sending it:

```bash
python main.py --kraken-preview-order --kraken-preview-symbol BTC/EUR --kraken-preview-quote-amount 3
```

This fetches live Kraken pair minimums and precision, checks your EUR balance, estimates the BTC size from the current ask, rounds to Kraken lot precision, and only calls `AddOrder` with `validate=true`.

12. If the preview passes and you intentionally want to send the first live order, use the guarded manual-submit command:

```bash
python main.py \
  --kraken-submit-order \
  --kraken-submit-symbol BTC/EUR \
  --kraken-submit-quote-amount 3.5 \
  --enable-live-trading \
  --live-confirmation ENABLE_LIVE_TRADING \
  --kraken-submit-confirmation SUBMIT_KRAKEN_ORDER
```

This submits a **real Kraken market order**. It is intentionally gated behind the live opt-in token, a second manual-submit confirmation token, and kill-switch readiness checks.

13. Before closing a live BTC/EUR position manually, preview the full close first:

```bash
python main.py --kraken-preview-close-position --kraken-close-symbol BTC/EUR
```

14. To close the full live BTC/EUR position manually, use:

```bash
python main.py \
  --kraken-close-position \
  --kraken-close-symbol BTC/EUR \
  --enable-live-trading \
  --live-confirmation ENABLE_LIVE_TRADING \
  --kraken-close-confirmation SUBMIT_KRAKEN_ORDER
```

This submits a **real Kraken market sell** for the full currently held base-asset size after validate-only checks pass.

## Live portfolio trading (Kraken Futures perps)

Live uses the same config and engine as paper, but with a real Kraken Futures account behind it
(`KrakenFuturesCrossMarginAdapter`, `src/execution/kraken_futures_cross.py`). Nothing starts unless every gate
passes: `--enable-live-trading`, `--live-confirmation ENABLE_LIVE_TRADING`, no mock data, Kraken Futures keys in
`.env`, a ready and inactive kill switch, Kraken Futures perps only with `max_leverage` at most 3x, and
`[risk] max_gross_notional` set (the most money the book may hold in positions; the risk overlay enforces it).

**Promotion checklist**
1. Paper-run the exact config for at least 2-4 weeks. Check the dashboard, the alerts, a restart, and the kill
   switch.
2. Create API keys with read and trade permissions, no withdrawal permission, and an IP allow-list for the
   machine that runs it. Put them in `.env` and run `python main.py --futures-verify-credentials`.
3. Choose the size with `scripts/research/risk_budget.py` and set `scale`. Set `max_gross_notional` well below the
   capital to start (e.g. 20-30% of it). Fund the Kraken Futures multi-collateral wallet, and log for tax how the
   collateral was bought.
4. Start it:
   ```bash
   python main.py --runtime live --portfolio config/portfolio.live.toml --enable-live-trading \
       --live-confirmation ENABLE_LIVE_TRADING --runtime-iterations 0 --runtime-interval 60
   ```
   The first live start sets the book to the account (its positions and collateral) and prints them. Live state is
   kept in `data/portfolio/<name>-live/`, apart from paper.
5. Watch for these Telegram alerts: `external_position_change` or `liquidation` (the book then allows only
   reductions until you run it once with `--portfolio-adopt-exchange` after checking), `equity_drift` (book and
   exchange equity differ by more than 0.5%), and `tax_record_failed`.

**How live orders work:** immediate-or-cancel market orders, reduce-only for closes and reductions. Client order ids
are derived from the checkpointed cycle, so after a crash between sending and hearing back, the restarted engine
finds the fill on Kraken by client id instead of sending again. Kraken's positions and margin are the source of
truth, and the book is reconciled every cycle. Fees are estimated from the contract's taker rate, and funding from
Kraken's published hourly rates; the equity-drift check catches what those estimates miss.

## Choosing capital and size (portfolio)

```bash
python scripts/research/risk_budget.py config/portfolio.example.toml --capital 10000 --max-drawdown 0.2
```

This prints, for your capital, what the book did historically at several sizes: the worst day, week and month, one-day
VaR and expected shortfall, the deepest drawdown, the longest time under water, and the margin used. It also gives
the largest `scale` whose drawdown times a safety factor (default 1.5) fits your limit. Put the choice in the config
(`[portfolio] scale` and `initial_equity`). Research and the runtime both apply it, and `--portfolio-check` shows
it. See the 2026-09-26 risk-budget entry in `docs/research_log.md`.

## Telegram messages

Every fill the runtime records sends one message: the mode (`[PAPER]`, `[DRY RUN]` or `[LIVE]`), side, size, symbol
and price. Then it says why the trade happened, which strategy and parameters were used, the position after it, and
the account (equity, P&L, the last hour, drawdown from the peak). The "why" line has two sources:

- **The strategy's signal:** `entry: the signal turned long/short` or `exit: the signal left long/short`, with the
  signal value on that bar (`+1`, `-1` or `0`).
- **A risk stop that overrides the signal:** a time stop, an ATR stop, a position drawdown stop, the liquidation
  buffer, or the daily-loss or drawdown limits. The message names which one.

Paper mode replays its bar history each cycle, so trades are recognised by time, side, size and price, and each one
is sent once. After a restart, trades from the warmup history are not re-sent. More than 5 new trades in one cycle
are summarised in a single line. Other runtime alerts (stale data, reconciliation, risk stops, heartbeat) arrive as
`ALERT <event>` followed by readable lines.

## Running a portfolio (several strategies and coins)

A portfolio is one TOML file (`config/portfolio.example.toml`; every field is in `docs/portfolio_plan.md`, section 5).
Check it, backtest it, then paper-run it:

```bash
python main.py --portfolio-check config/portfolio.example.toml
python scripts/research/portfolio_backtest.py config/portfolio.example.toml
python main.py --runtime paper --portfolio config/portfolio.example.toml --runtime-iterations 0 --runtime-interval 60
python main.py --portfolio config/portfolio.example.toml --dashboard      # from another terminal, any time
```

- `--runtime-iterations 0` runs until Ctrl-C (or SIGTERM). The current cycle finishes and the checkpoint is written
  before it exits, so restarting with the same command resumes the same positions and decisions.
- **Market data:** completed Kraken candles over REST, the same cached series the research used, polled every
  `--runtime-interval` seconds. Decisions happen only when a new grid bar completes (the shortest sleeve interval,
  e.g. 4h). Other cycles only mark the book.
- **Execution:** `paper` and `live_dry_run` both trade against sandbox accounts shaped like the venues: one
  cross-margin account per perp venue, with fees, slippage, funding and liquidation. For `--runtime live`, see
  "Live portfolio trading" below.
- **State:** `data/portfolio/<name>/engine.json` (book, sleeves, allocator) and `paper_<venue>.json` (the sandbox
  account). Delete the folder to start fresh. `--use-mock-connector` uses synthetic candles in a fresh temp folder, so
  it never touches the real paper state.
- **Telegram:** one message per fill (which sleeves drove it and which risk limits acted). There are also alerts,
  sent once when a problem starts and once when it clears, for: stale or failing market data per instrument, a risk
  limit acting, rejected orders, reconciliation mismatches, a sleeve disabled after 3 failing cycles, and failed
  cycles (the runtime stops itself after 5 in a row).
- **Kill switch:** `python main.py --kill-switch` from any terminal. At its next cycle the portfolio closes every
  position with reduce-only orders and stops.
- **After the max-drawdown kill:** the book stays flat until you re-arm it with `--portfolio-reset-peak` (logged).
- **Currency:** one currency per portfolio for now. USD perps are fine, but EUR spot mixed with USD perps is refused
  until an FX feed exists.
- **Tax records (live only):** every perp realized P&L, fee and funding payment is written to the tax ledger as it
  happens, under the contract name (e.g. `PF_XBTUSD`) and valued in NOK at Norges Bank's rate. Spot fills go through
  the FIFO lots, which need your EUR deposits logged first (`--tax-log-fiat-eur`). A record that can't be written
  (e.g. Norges Bank is down) is queued in the checkpoint, alerted once, and retried every cycle. Paper and dry-run
  never write the ledger.

## Daily operational checks

After a run starts, verify the following:

- the process remains healthy and the latest heartbeat is fresh
- the report output still looks sensible
- the SQLite database and runtime checkpoint files are present
- the log file is being written to `logs/app.log`

Useful commands:

```bash
python main.py --report --report-limit 20
python main.py --dashboard
python main.py --daily-summary
python main.py --tax-report --tax-year 2026
python main.py --tax-log-fiat-eur 1000 --tax-fx-rate 11.50 --tax-reference initial_capital
```

## Running a strategy on its researched timeframe

Strategies researched on 4h or daily bars need the runtime to build the same bars:

```bash
python main.py --runtime live_dry_run --execution-exchange kraken \
  --bar-interval 4h --warmup-bars 200 --runtime-interval 60 --runtime-iterations 100000 \
  --strategy moving_average_crossover --strategy-params '{"short_window": 8, "long_window": 96}' --dashboard
```

- `--runtime-interval` is how often prices are polled; `--bar-interval` is the bar length. Between bar closes a
  cycle only marks the account to market (fills, funding, liquidation checks); the strategy acts when a bar
  completes, on the bar's close.
- `--warmup-bars` loads that many completed candles at startup (Kraken spot OHLC, max 720; futures mark candles
  for `kraken_futures`). The dashboard shows the current signal immediately, but the first trade waits for the
  next bar close, so a restart never acts on a bar that closed hours earlier.
- The time stop counts bars, so `time_stop_bars=60` on 4h bars is 10 days.

### Position size

Every sizing method gives a **share of equity** (0.25 = a position worth 25% of equity). The risk manager caps it,
and the engine converts it to units at the entry price. Positions are sized once, at entry, and not resized while
open.

| `--sizing` | What it does | Example `--sizing-params` |
| --- | --- | --- |
| `fixed_fraction` (default) | The same share every time; `--risk-per-trade-pct` sets it (default 0.10) | `'{"fraction": 0.2}'` |
| `fixed_notional` | The same amount of money every time, e.g. to clear an exchange minimum on a small account | `'{"notional": 50}'` |
| `vol_target` | Share = target / EWMA volatility forecast: bigger in calm markets, smaller in turbulent ones. `--target-annual-vol 0.5` is shorthand | `'{"target_annual_vol": 0.5}'` |
| `atr_risk` | Lose about `risk_fraction` of equity if a stop `atr_multiplier` ATRs away is hit. Pair with the same ATR stop in the risk config | `'{"risk_fraction": 0.01, "atr_multiplier": 2}'` |
| `kelly` | A fraction of the Kelly leverage from the strategy's own closed trades; `fallback_fraction` until `min_trades` exist; can start from a research prior | `'{"kelly_fraction": 0.5, "prior_mean": 0.02, "prior_std": 0.1, "prior_trades": 20}'` |

```bash
python main.py --list-sizing                                              # every method with its parameters and defaults
python main.py --runtime paper --use-mock-connector --sizing vol_target --sizing-params '{"target_annual_vol": 0.4}' --dashboard
```

- **Caps, in order:**
  - the exchange's `max_position_size`: 0.5 of equity on `kraken` and `kraken_futures`, 0.35 on `firi`, 1.0 on
    `sandbox` (`DEFAULT_EXCHANGE_RISK_LIMITS` in `src/risk/controls.py`);
  - the per-trade notional limit: 500 on `kraken` and `kraken_futures`, 400 on `firi`;
  - total exposure capacity;
  - buying power.
- **Refusals carry a reason.** When a method can't size, the entry is refused with that reason instead of being
  placed at size 0. Reasons: `volatility_forecast_unavailable` (fewer than 20 bars; use `--warmup-bars`),
  `atr_unavailable`, `kelly_no_edge` (its trades lose on average), `position_caps`.
- **Logging:** the chosen method, share and inputs (e.g. `annual_volatility_forecast`, `full_kelly`) are in each
  entry decision's risk details.
- **Persistence:** `--sizing` and `--sizing-params` are saved with `--runtime-config-path`. Kelly's trade history
  lives in memory and restarts empty. Seed it with a research prior, since daily strategies trade too rarely to
  learn Kelly quickly.
- **Research:** `vol_target` helped MA crossover and long/short Keltner, but not breakout long-only rules (see
  `research_log.md`).

## Recording order-flow data

`--record-market-data` records public data that can't be downloaded later. It needs no credentials and places no
orders. It records:
- Kraken Futures trades (taker side, and whether each was a liquidation), order-book samples every second (top 10
  levels plus the size within 10/25/50/100 bps of the mid), and the ticker (mark and index price, funding, open
  interest) every 10 s;
- Kraken spot trades and book samples;
- Binance (all symbols) and Bybit liquidations.

Symbols and intervals are in `config/market_data.json`.

```bash
mkdir -p logs
nohup caffeinate -is python main.py --record-market-data > logs/market_data_recorder.log 2>&1 &   # start in the background
python main.py --market-data-status          # rows, days, disk use per venue/channel, and recording gaps
pkill -f -- --record-market-data             # stop cleanly (SIGTERM); Ctrl-C works in the foreground
```

- Data goes to `data/market_data/<venue>/<channel>/<YYYY-MM-DD>.csv`. Finished days are compacted to `.parquet`
  when the day rolls over, or at the next start. Expect roughly 100 MB a day at the default settings.
- Run one recorder at a time. It is safe to restart: it appends to today's files and drops a half-written last row.
- Each feed reconnects on its own with backoff. Every connect, disconnect, start, stop and 5-minute heartbeat
  (with row counts) is written to `recorder/events`, so the status command can list the gaps.
- `caffeinate -is` stops the Mac from idle-sleeping while plugged in, but closing the lid still sleeps it. Gaps from
  sleep appear in the status output.
- A book whose checksum (spot) or sequence number (futures) stops matching is rebuilt by reconnecting that feed.
  The log shows `resync:` when this happens; a few a day is normal.

## Promotion checklist: paper -> live_dry_run

Use this only after the paper baseline has been stable. The goal is to exercise exchange-shaped execution and reconciliation without enabling production trading.

1. Confirm the known-good paper baseline still runs cleanly with `config/runtime.paper.json`.
2. Confirm recent dashboard/report output shows:
   - healthy runtime state
   - no active kill switch
   - no unresolved reconciliation mismatches
   - readable trade / event persistence
3. Confirm the target exchange is explicit (`--execution-exchange kraken` for the current near-term path).
4. Confirm the target symbol is explicit if you are not using the default exchange symbol.
5. Confirm credentials are loaded if you want exchange-shaped auth/status behavior, but remember `live_dry_run` must still avoid real order placement.
6. Confirm the kill-switch state file exists and is inactive.
7. Start the dry-run lane with exchange-shaped routing:

```bash
python main.py \
  --runtime live_dry_run \
  --execution-exchange kraken \
  --runtime-iterations 3 \
  --dashboard \
  --report
```

   Perpetual futures can be rehearsed the same way with `--execution-exchange kraken_futures` (the in-process margin
   sandbox). Before any live perp run, check credentials read-only with `--futures-verify-credentials`; see
   `docs/perpetual_futures.md` for the extra gates.

8. Verify after startup:
   - runtime mode reports `live_dry_run`
   - account state shows the expected exchange/base currency
   - orders, if any, are reported through the sandbox adapter rather than a production venue
   - no stale data, reconciliation, or kill-switch alerts appear unexpectedly

## Promotion checklist: live_dry_run -> live

This checklist is intentionally stricter. Completing it does **not** mean the repo is ready to trade now; it only defines the manual approvals required before `live` should ever be attempted.

1. Keep `paper` as the source-of-truth baseline and `live_dry_run` as the immediate promotion lane.
2. Confirm the exchange remains limited to the current near-term target (`kraken`).
3. Confirm exchange symbol mapping, order IDs, balance normalization, reconciliation payload handling, and the non-destructive Kraken verification probe have all passed recently.
4. Confirm the kill switch is ready, inactive, and understood operationally.
5. Confirm conservative caps remain in place for the target exchange:
   - `max_position_size <= 0.5`
   - `max_notional_per_trade <= 500`
   - `max_total_notional <= 2500`
   - `max_open_positions <= 1`
   - `max_open_orders <= 2`
6. Confirm operator intent explicitly by requiring both:
   - `--enable-live-trading`
   - `--live-confirmation ENABLE_LIVE_TRADING`
7. Confirm `--execution-exchange` is explicit and `--use-mock-connector` is not present.
8. Confirm you have a rollback plan: kill-switch activation, order-cancel procedure, and state inspection path.
9. Do **not** start `live` until recent Kraken verification, paper stability, and live-dry-run checks all pass together; the guarded live path now exists, but exchange validation is still the final confidence gate before any capital is exposed.

## Non-destructive verification steps before any promotion

- Run `python main.py --dashboard --report` and confirm the persisted runtime state is readable.
- Run the paper baseline again if there is any doubt about current repo state.
- Run `python main.py --kraken-verify-dry-run --kraken-verify-symbol BTC/EUR` and confirm all checks pass before trusting exchange credentials or payload normalization.
- Run `python main.py --kraken-preview-order --kraken-preview-symbol BTC/EUR --kraken-preview-quote-amount <eur_amount>` and confirm the intended notional clears Kraken minimum size/cost rules before attempting any first live order.
- If you already hold BTC and plan to exit manually, run `python main.py --kraken-preview-close-position --kraken-close-symbol BTC/EUR` before using the close command.
- Use `python main.py --kraken-submit-order ... --enable-live-trading --live-confirmation ENABLE_LIVE_TRADING --kraken-submit-confirmation SUBMIT_KRAKEN_ORDER` only when you intentionally want to place a live order after the preview has passed.
- Use `python main.py --kraken-close-position ... --enable-live-trading --live-confirmation ENABLE_LIVE_TRADING --kraken-close-confirmation SUBMIT_KRAKEN_ORDER` only when you intentionally want to submit a live market close of the current position.
- Confirm the verification output's kill-switch preview matches expectations for any currently open Kraken orders.
- Seed the EUR fiat pool before the first real Kraken trade with `python main.py --tax-log-fiat-eur <amount> --tax-fx-rate <eur_nok_rate> --tax-reference initial_capital`.
- Confirm the Norwegian tax ledger stays readable with `python main.py --tax-report --tax-year <year>` and export it before any production rollout.
- For dry-run work, prefer a short bounded run first (`--runtime-iterations 3`) before longer sessions.
- If testing live guards only, verify the CLI refuses `--runtime live` without the required flags instead of trying to work around the protections.
- If the kill switch was triggered earlier, reset and verify the state before any further promotion attempt.

## Backup procedure

Back up the runtime artifacts before you change strategies, rotate credentials, or make a major operational change.

1. Stop the runtime first.
2. Create a timestamped backup folder:

```bash
backup_dir="backups/$(date +%F-%H%M)"
mkdir -p "$backup_dir"
```

3. Copy the important files:

```bash
cp -p data/cryptoquant.db "$backup_dir/cryptoquant.db"
cp -p data/runtime_state.json "$backup_dir/runtime_state.json"
[ -f data/runtime_config.json ] && cp -p data/runtime_config.json "$backup_dir/runtime_config.json"
cp -p logs/app.log "$backup_dir/app.log"
```

4. Keep the backup alongside any notes about the run mode, strategy, and exchange configuration.

## Restore procedure

1. Stop the runtime.
2. Restore the backed-up files into place:

```bash
cp -p backups/<timestamp>/cryptoquant.db data/cryptoquant.db
cp -p backups/<timestamp>/runtime_state.json data/runtime_state.json
[ -f backups/<timestamp>/runtime_config.json ] && cp -p backups/<timestamp>/runtime_config.json data/runtime_config.json
```

3. Restart the runtime with the recovered checkpoint:

```bash
python main.py \
  --runtime paper \
  --runtime-iterations 3 \
  --runtime-interval 1.0 \
  --runtime-config-path data/runtime_config.json \
  --runtime-state-path data/runtime_state.json \
  --resume-runtime \
  --dashboard \
  --report \
  --daily-summary
```

4. Verify the restored state and recent report output before continuing.

## Crash and reconnect recovery

If the process crashes or the connection drops:

1. Check `logs/app.log` for the last error, watchdog message, or shutdown reason.
2. If a checkpoint exists, restart with `--resume-runtime` to reload the last persisted runtime state.
3. If the runtime was interrupted midway through a cycle, use the latest consistent SQLite database and checkpoint as the source of truth.
4. If the runtime reports stale quotes, reconciliation mismatches, or other health issues, review the latest dashboard/report output before re-enabling the strategy.
