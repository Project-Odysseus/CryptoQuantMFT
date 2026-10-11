"""Run the signal card on signals whose answer the research log already has, to see that its verdicts agree.

    python scripts/research/signal_card_check.py            # logs each card to the trial ledger
    python scripts/research/signal_card_check.py --no-ledger

Development data only (the frozen holdout is cut off). The signals: the three coin rankings of the signal lab
(7-day funding, 7-day taker-buy share, 30-day return) at a 7-day horizon, and H10's morning move in BTC and ETH
against the afternoon that followed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import pit  # noqa: E402
from src.research import signal_checks as sc  # noqa: E402
from src.research import signal_lab  # noqa: E402
from src.research.hypotheses.h10_session import session_trades  # noqa: E402

FAMILY = "signal_card_check"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ledger", action="store_true", help="do not log the cards")
    args = parser.parse_args()
    ledger = not args.no_ledger

    wide = signal_lab.load_universe(top_n=50, kraken_only=True, listing="point_in_time")
    feats = signal_lab.features(wide)
    inside = wide["universe"].astype(bool)
    rankings = {"carry (minus 7-day funding)": -feats["funding_7d"].where(inside), "taker-buy share, 7 days": feats["taker_buy_share_7d"].where(inside),
                "30-day return": feats["mom_30d"].where(inside)}
    close = wide["close"].where(inside)
    board = sc.compare(rankings, close, horizon=7, family=FAMILY, ledger=ledger)
    pd.set_option("display.width", 220)
    print("Coin rankings, 7-day horizon:")
    print(board.table.to_string())
    print(f"luck threshold for the best of {len(rankings)}: {board.luck_threshold:.3f}; redundant groups: {board.redundant or 'none'}\n")
    for name, card in board.cards.items():
        print(f"{name}: {card.verdict}. {card.reason}")
        print(card.table.to_string(), "\n")

    for coin in ("BTC", "ETH"):
        trades = session_trades(pit.load_bars(coin, "5m"))
        days = pd.DatetimeIndex(pd.to_datetime(trades.index)).tz_localize(None) if getattr(trades.index, "tz", None) is not None else pd.DatetimeIndex(pd.to_datetime(trades.index))
        signal, hold = pd.Series(trades["signal"].to_numpy(), index=days), pd.Series(trades["hold"].to_numpy(), index=days)
        card = sc.signal_card(signal, forward=hold, name=f"H10 morning move, {coin}", direction=1, round_trip_bps=11.0, family=FAMILY, ledger=ledger)
        print(f"{card.name}: {card.verdict}. {card.reason}")
        print(card.table.to_string(), "\n")


if __name__ == "__main__":
    main()
