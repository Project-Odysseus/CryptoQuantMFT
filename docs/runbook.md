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

4. For a deterministic smoke test before a real paper run, add `--use-mock-connector`.
5. Confirm the startup banner, the health snapshot, and the first operational events before leaving the runtime unattended.
6. The health snapshot now reports entry-decision reasons, current position-side PnL, and the latest trade/alert context so you can audit blocked fills without digging through raw logs.
7. The live plot is written to `plots/runtime_live_plot.png` and updates each runtime cycle while the process is running.

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
