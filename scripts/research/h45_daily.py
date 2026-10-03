"""The daily job for H4 and H5: store the confirmatory window's data, and run the test once when it is due.

    python scripts/research/h45_daily.py

Meant for a scheduler (deploy/launchd/com.cryptoquant.research-daily.plist runs it once a day). Each run:

1. caches Kalshi's quotes and settlements for every Deribit chain recorded in the confirmatory window so far
   (`h5_study.py --collect`: counts only, no results);
2. on or after 2026-11-17, once the window holds its 20 settled daily events, runs both confirmatory studies exactly
   once, saves their output to `data/research/prediction_markets/H5_confirmatory.txt` and `H4_confirmatory.txt`, and
   sends one Telegram message saying they are ready. A marker file keeps it from ever running them twice.

It decides nothing: the pass criteria are in research/prereg/H4.txt and H5.txt, and the reports are still to be written
by hand. Research only: public data, no orders.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data/research/prediction_markets"
MARKER = OUT / "confirmatory_done.json"
NOT_BEFORE = datetime(2026, 11, 17, tzinfo=timezone.utc)


def run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "scripts/research" / script), *args], cwd=ROOT, capture_output=True, text=True, timeout=3600)


def main() -> int:
    now = datetime.now(timezone.utc)
    OUT.mkdir(parents=True, exist_ok=True)
    collected = run("h5_study.py", "--collect")
    print(f"{now:%Y-%m-%d %H:%M} UTC  {(collected.stdout.strip().splitlines() or [collected.stderr.strip()[-300:]])[-1]}")
    if collected.returncode != 0:
        return 1
    if MARKER.exists() or now < NOT_BEFORE:
        return 0
    results = {}
    for hypothesis in ("H5", "H4"):
        done = run(f"{hypothesis.lower()}_study.py", "--window", "confirm")
        if done.returncode != 0:  # most likely "Not ready": fewer than 20 settled events so far; try again tomorrow
            print(f"{hypothesis} confirmatory run not done: {(done.stdout + done.stderr).strip().splitlines()[-1]}")
            return 0
        (OUT / f"{hypothesis}_confirmatory.txt").write_text(done.stdout)
        results[hypothesis] = str(OUT / f"{hypothesis}_confirmatory.txt")
    MARKER.write_text(json.dumps({"ran_at": now.isoformat(), "outputs": results}, indent=1))
    message = ("The H4 and H5 confirmatory runs are done (run once, as pre-registered). Outputs: data/research/prediction_markets/H5_confirmatory.txt and "
               "H4_confirmatory.txt. Judge them against research/prereg/H4.txt and H5.txt, then write research/reports/H4.md and H5.md.")
    print(message)
    try:
        from src.utils.telegram import TelegramNotifier

        TelegramNotifier().send_alert(event_type="research_ready", message=message, metadata={})
    except Exception as exc:  # noqa: BLE001 - the outputs are on disk either way
        print(f"Telegram message not sent: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
