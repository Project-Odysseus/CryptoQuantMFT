#!/usr/bin/env bash
# Nightly backup: a consistent SQLite copy of data/cryptoquant.db (trades, events, the Norwegian tax ledger), the
# portfolio checkpoints, runtime state and configs. Keeps BACKUP_KEEP_DAYS days in BACKUP_DIR. If BACKUP_REMOTE is set
# (an rsync target, e.g. user@nas:/backups/cryptoquant), the folder is copied there too: a backup on the same machine
# doesn't survive a dead disk or a burglary. Settings come from deploy/backup.env (see deploy/README.md).
set -euo pipefail
cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-$HOME/cryptoquant-backups}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
target="$BACKUP_DIR/$(date -u +%Y-%m-%dT%H%MZ)"
mkdir -p "$target"
if [ -f data/cryptoquant.db ]; then
  sqlite3 data/cryptoquant.db ".backup '$target/cryptoquant.db'"   # safe while the runtime is writing
fi
paths=()
for path in data/portfolio data/runtime_state*.json data/runtime_config.json data/kill_switch_state.json config deploy/backup.env; do
  [ -e "$path" ] && paths+=("$path")
done
tar -czf "$target/state.tar.gz" "${paths[@]}"
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +"$BACKUP_KEEP_DAYS" -exec rm -rf {} +
if [ -n "${BACKUP_REMOTE:-}" ]; then
  rsync -a "$target" "$BACKUP_REMOTE/"
fi
echo "backup written to $target"
