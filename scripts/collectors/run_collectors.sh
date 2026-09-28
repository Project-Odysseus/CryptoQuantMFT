#!/usr/bin/env bash
# Light research collectors that restart on failure: Binance + Bybit liquidations (no order books, a few MB a day)
# and hourly Deribit option chains (~5 MB a day). History for these can't be downloaded later, so they should run
# on the always-on machine. Funding, OI and long/short ratios are not collected: their history is backfilled from
# public REST and the Binance archive (src/data/positioning.py).
#
#   scripts/collectors/run_collectors.sh            # both, until Ctrl-C
#   tail -f logs/collectors/*.log
set -u
cd "$(dirname "$0")/../.."
mkdir -p logs/collectors
PY="${PYTHON:-python}"

forever() {  # name, command...: rerun the command whenever it exits, with a short pause
  local name="$1"; shift
  while true; do
    echo "$(date -u +%FT%TZ) starting $name" >> "logs/collectors/$name.log"
    "$@" >> "logs/collectors/$name.log" 2>&1
    echo "$(date -u +%FT%TZ) $name exited with $?; restarting in 30s" >> "logs/collectors/$name.log"
    sleep 30
  done
}

forever liquidations "$PY" main.py --record-market-data --record-config config/market_data.liquidations.json &
forever option_chains "$PY" main.py --record-option-chains BTC ETH --option-chain-interval 3600 &
trap 'kill 0' INT TERM
wait
