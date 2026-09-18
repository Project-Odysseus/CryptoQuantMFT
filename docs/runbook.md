# Runtime runbook

This runbook covers startup, daily checks, backup/restore, and recovery for the local paper-trading runtime.

## Files to monitor

- `data/cryptoquant.db` – the SQLite database used by the trade logger and daily summary persistence.
- `data/runtime_state.json` – the runtime checkpoint file written by the orchestrator when `--runtime-state-path` is provided or when the default runtime state path is used.
- `data/runtime_config.json` – optional runtime config snapshot written when `--runtime-config-path` is provided.
- `logs/app.log` – rotating application log output from the logger utility.

## Startup checklist

1. Activate the intended Python environment.
2. Confirm the runtime settings in `.env` are correct, especially `database_path`, `telegram_bot_token`, and `telegram_chat_id` if you intend to use Telegram alerts.
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

This verifies Kraken private-endpoint authentication, balance/open-order normalization, status/cancel request handling, and the validate-only order path without placing a production order.
It also previews which open Kraken orders the kill switch would attempt to cancel after recovering exchange-shaped order state locally.

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
```

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
- Confirm the verification output's kill-switch preview matches expectations for any currently open Kraken orders.
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
