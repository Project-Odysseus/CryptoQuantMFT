"""Strategy-card every sleeve and basket of one or more portfolio configs, and write one summary table.

    python scripts/research/card_books.py config/portfolio.btc_live.toml config/portfolio.multi_paper.toml
    python scripts/research/card_books.py config/portfolio.multi_paper.toml --no-ledger --out data/research/cards_test

Each sleeve is run alone at full size, as the paper runtime would run it, over the cached Kraken history (perps from
2020), with its own sizing, costs and funding. Its catalog grid supplies the variants a parameter search would have
tried, and buy-and-hold of its instrument is the baseline (`strategy_checks.card_sleeve`). The same sleeve in two
configs is carded once. Cards are noted in the trial ledger, which is what `--portfolio-check` reads. Sleeves on
markets without a Kraken history loader (the futures book) are listed as skipped: card those from their study.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.portfolio.config import load_portfolio_config  # noqa: E402
from src.research import strategy_checks as stc  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402

COLUMNS = ["verdict", "versus baseline", "sharpe", "sharpe_low", "sharpe_high", "t", "dsr", "trials", "pbo", "placebo_p", "shrunk_sharpe", "minus_baseline",
           "p_better", "max_drawdown"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("configs", nargs="+", help="portfolio config files")
    parser.add_argument("--no-ledger", action="store_true", help="don't note the cards in the trial ledger")
    parser.add_argument("--out", help="folder for the cards and the summary (default data/research/cards_<time>)")
    args = parser.parse_args()
    out = Path(args.out or f"data/research/cards_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    rows: dict[str, dict] = {}
    done: dict[str, str] = {}
    for path in args.configs:
        config = load_portfolio_config(path)
        units = [(sleeve.id, sleeve) for sleeve in config.enabled_sleeves if sleeve.basket is None] + [(basket.id, basket) for basket in config.baskets if basket.enabled]
        for unit_id, spec in units:
            label = f"{config.name} / {unit_id}"
            key = stc.sleeve_key(spec)
            if key in done:
                rows[label] = {**rows[done[key]], "verdict": rows[done[key]]["verdict"] + f" (same as {done[key]})"}
                continue
            try:
                card = stc.card_basket(config, unit_id, ledger=not args.no_ledger) if hasattr(spec, "coins") else stc.card_sleeve(config, spec, ledger=not args.no_ledger)
            except ValueError as exc:
                rows[label] = {"verdict": f"skipped: {exc}"}
                print(f"{label}: skipped ({exc})")
                continue
            card.save(out / unit_id)
            rows[label] = card.summary()
            done[key] = label
            print(f"{label}: {card.verdict} ({card.reason}); baseline: {card.versus_baseline}")
    table = pd.DataFrame(rows).T.reindex(columns=COLUMNS)
    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / "summary.csv")
    (out / "summary.md").write_text("# Strategy cards of the running books\n\n" + md_table(table, digits=2) + "\n")
    print(md_table(table, digits=2))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
