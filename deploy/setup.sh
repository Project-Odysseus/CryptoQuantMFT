#!/usr/bin/env bash
# One-time (and re-runnable) setup of the always-on Ubuntu machine. Run from the repo root as your normal user:
#
#   bash deploy/setup.sh               # conda env, collectors, nightly backups, log rotation
#   bash deploy/setup.sh --with-paper  # also start the BTC book in PAPER mode (no orders)
#
# It never installs or starts live trading: that stays a manual step behind the live gates (deploy/README.md).
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
USER_NAME="$(id -un)"
WITH_PAPER=0
[ "${1:-}" = "--with-paper" ] && WITH_PAPER=1

echo "== packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git sqlite3 rsync logrotate curl ca-certificates

echo "== no sleep, clock in sync"
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null
sudo timedatectl set-ntp true

echo "== conda environment (CryptoArb)"
CONDA="$HOME/miniforge3/bin/conda"
if [ ! -x "$CONDA" ]; then
  curl -fsSL -o /tmp/miniforge.sh "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
  bash /tmp/miniforge.sh -b -p "$HOME/miniforge3"
fi
if "$CONDA" env list | grep -q "^CryptoArb "; then
  "$CONDA" env update -n CryptoArb -f environment.yml --prune
else
  "$CONDA" env create -f environment.yml
fi
PYTHON="$HOME/miniforge3/envs/CryptoArb/bin/python"

echo "== folders and secrets"
mkdir -p data logs/collectors
if [ -f .env ]; then chmod 600 .env; else echo "   no .env yet: the collectors don't need one; copy it over before paper/live runs"; fi
[ -f deploy/backup.env ] || printf 'BACKUP_DIR=%s/cryptoquant-backups\nBACKUP_KEEP_DAYS=30\n# BACKUP_REMOTE=user@host:/path\n' "$HOME" > deploy/backup.env

echo "== systemd units and log rotation"
render() { sed -e "s#@USER@#$USER_NAME#g" -e "s#@REPO@#$REPO#g" -e "s#@PYTHON@#$PYTHON#g" "$1"; }
for unit in deploy/systemd/*.service deploy/systemd/*.timer; do
  render "$unit" | sudo tee "/etc/systemd/system/$(basename "$unit")" >/dev/null
done
render deploy/logrotate/cryptoquant | sudo tee /etc/logrotate.d/cryptoquant >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now cryptoquant-liquidations.service cryptoquant-option-chains.service cryptoquant-spreads.service cryptoquant-backup.timer
if [ "$WITH_PAPER" = 1 ]; then
  sudo systemctl enable --now cryptoquant-paper.service
fi

echo "== check"
"$PYTHON" -m pytest -q -x tests/test_research_harness.py tests/test_market_data_recorder.py
systemctl --no-pager --lines=0 status cryptoquant-liquidations cryptoquant-option-chains cryptoquant-spreads cryptoquant-backup.timer || true
[ "$WITH_PAPER" = 1 ] && systemctl --no-pager --lines=0 status cryptoquant-paper || true
echo "Done. Logs: journalctl -u cryptoquant-liquidations -f   (see deploy/README.md)"
