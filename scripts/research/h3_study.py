"""H3, variance risk premium phase A: run the pre-registered study (research/prereg/H3.txt) on development data.

    python scripts/research/h3_study.py      # writes data/research/h3_<time>/ (tables, raw report, manifest)

Configurations are logged to the trial ledger (family "H3"). The frozen holdout stays locked.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import governance  # noqa: E402
from src.research.bar_engine import by_year, performance  # noqa: E402
from src.research.costs import VENUES  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.hypotheses.h3_vrp import daily, prepare_sleeves, sleeve_run, vrp_frame  # noqa: E402
from src.research.inference import circular_shifts, deflated_sharpe, log_trial, newey_west_ols, placebo_share  # noqa: E402
from src.research.stats import sharpe_per_period  # noqa: E402

COINS = ("BTC", "ETH")
YEAR = 365.0
VARIANTS = {"ewma": ("ewma", False), "iv": ("iv", False), "iv_debiased": ("iv_debiased", False), "a2_proxy_riskoff": ("ewma", True)}


def book(sleeves: list, sizing: str, *, risk_off: bool = False, multiplier: float = 1.0, shifts: dict[str, int] | None = None, part: str = "net") -> tuple[pd.Series, dict[str, pd.Series]]:
    """Equal-weight daily book over sleeves, plus each sleeve's daily series."""
    per = {}
    for data in sleeves:
        run = sleeve_run(data, sizing, multiplier=multiplier, risk_off_proxy=risk_off, iv_shift_days=(shifts or {}).get(data.coin, 0))
        per[f"{data.coin}:{data.sleeve}"] = daily(run.frame[part])
    frame = pd.DataFrame(per)
    start = frame.apply(lambda s: s.ne(0).idxmax()).max()  # common start: every sleeve has begun trading
    frame = frame.loc[start:].fillna(0.0)
    return frame.mean(axis=1), {k: frame[k] for k in frame}


def matched(values: pd.Series, reference_vol: float) -> dict[str, float]:
    """Max drawdown and worst month after scaling `values` to `reference_vol` annualised."""
    scale = reference_vol / (values.std(ddof=1) * np.sqrt(YEAR))
    stats = performance(values * scale, periods_per_year=YEAR)
    return {"matched_max_dd_pct": stats["max_drawdown"] * 100, "matched_worst_month_pct": stats["worst_month"] * 100}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--placebo-runs", type=int, default=300)
    parser.add_argument("--no-ledger", action="store_true")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path("data/research") / f"h3_{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    vrp = {coin: vrp_frame(coin) for coin in COINS}
    data_range = f"{min(v.index.min() for v in vrp.values()):%Y-%m-%d}..{max(v.index.max() for v in vrp.values()):%Y-%m-%d}"

    # ---- P1 / P2a -------------------------------------------------------------------------------------------------
    p_rows, year_rows = [], []
    for coin, frame in vrp.items():
        for column in ("vrp", "vrp_var"):
            data = frame[column].dropna()
            result = newey_west_ols(data.to_numpy(), np.zeros((len(data), 0)), lags=30)
            p_rows.append({"coin": coin, "test": f"P1 mean {column}", "estimate": result.loc["const", "coef"], "t_NW30": result.loc["const", "t"], "n": len(data),
                           "share_positive": float((data > 0).mean())})
        data = frame[["vrp", "spread"]].dropna()
        result = newey_west_ols(data["vrp"], data["spread"], lags=30)
        p_rows.append({"coin": coin, "test": "P2a vrp ~ (iv - rv_trail) slope", "estimate": result.loc["x1", "coef"], "t_NW30": result.loc["x1", "t"], "n": len(data),
                       "share_positive": np.nan})
        grouped = frame.dropna(subset=["vrp"]).groupby(frame.dropna(subset=["vrp"]).index.year)
        for year, group in grouped:
            year_rows.append({"coin": coin, "year": year, "days": len(group), "mean_iv": group["iv"].mean(), "mean_rv_fwd": group["rv_fwd"].mean(),
                              "mean_vrp": group["vrp"].mean(), "share_positive": float((group["vrp"] > 0).mean())})
        for label, mask in {"spread top quintile": frame["spread"] >= frame["spread"].quantile(0.8), "spread bottom quintile": frame["spread"] <= frame["spread"].quantile(0.2)}.items():
            values = frame.loc[mask, "vrp"].dropna()
            p_rows.append({"coin": coin, "test": f"P2a mean vrp, {label}", "estimate": values.mean(), "t_NW30": np.nan, "n": len(values), "share_positive": float((values > 0).mean())})
    p_tests, p_years = pd.DataFrame(p_rows), pd.DataFrame(year_rows)
    p1_pass = all((p_tests[(p_tests["coin"] == c) & (p_tests["test"] == "P1 mean vrp")]["t_NW30"] > 2).all() for c in COINS)
    p1_years = p_years.groupby("coin").apply(lambda g: (g["mean_vrp"] > 0).mean(), include_groups=False)

    # ---- A1 / A2-proxy --------------------------------------------------------------------------------------------
    sleeves = {coin: prepare_sleeves(coin, vrp[coin], VENUES["kraken_perp"]) for coin in COINS}
    every = [s for coin in COINS for s in sleeves[coin]]
    books, rows, coin_rows, sleeve_rows, year_tables, placebo_rows = {}, [], [], [], {}, []
    for name, (sizing, risk_off) in VARIANTS.items():
        net, per = book(every, sizing, risk_off=risk_off)
        gross, _ = book(every, sizing, risk_off=risk_off, part="gross")
        net2, _ = book(every, sizing, risk_off=risk_off, multiplier=2.0)
        books[name] = net
        perf = performance(net, periods_per_year=YEAR)
        trades = sum(sleeve_run(s, sizing, risk_off_proxy=risk_off).trades() for s in every)
        mean_weight = float(np.mean([sleeve_run(s, sizing, risk_off_proxy=risk_off).frame["position"].abs().mean() for s in every]))
        rows.append({"variant": name, "gross_sharpe": performance(gross, periods_per_year=YEAR)["sharpe"], "net_sharpe": perf["sharpe"],
                     "net_sharpe_2x": performance(net2, periods_per_year=YEAR)["sharpe"], "ann_net_pct": perf["ann_return"] * 100, "ann_vol_pct": perf["ann_vol"] * 100,
                     "max_dd_pct": perf["max_drawdown"] * 100, "worst_month_pct": perf["worst_month"] * 100, "mean_abs_weight": mean_weight, "trades": trades})
        for coin in COINS:
            coin_net, _ = book(sleeves[coin], sizing, risk_off=risk_off)
            coin_perf = performance(coin_net, periods_per_year=YEAR)
            coin_rows.append({"variant": name, "coin": coin, "net_sharpe": coin_perf["sharpe"], "ann_vol_pct": coin_perf["ann_vol"] * 100, "max_dd_pct": coin_perf["max_drawdown"] * 100,
                              "_series": coin_net})
        for key, series in per.items():
            stats = performance(series, periods_per_year=YEAR)
            sleeve_rows.append({"variant": name, "sleeve": key, "net_sharpe": stats["sharpe"], "ann_vol_pct": stats["ann_vol"] * 100, "max_dd_pct": stats["max_drawdown"] * 100})
        year_tables[name] = by_year(net, periods_per_year=YEAR)["sharpe"]
        if not args.no_ledger:
            log_trial("H3", "h3_study", {"variant": name}, data_range=data_range, gross_sharpe=rows[-1]["gross_sharpe"], net_sharpe=perf["sharpe"],
                      net_sharpe_2x=rows[-1]["net_sharpe_2x"], trades=trades, sharpe_per_period_net=sharpe_per_period(net))
        if sizing != "ewma":
            shifts = {coin: circular_shifts(len(vrp[coin]), args.placebo_runs, min_shift=60, seed=i + 7) for i, coin in enumerate(COINS)}
            placebo = [performance(book(every, sizing, shifts={c: int(shifts[c][run]) for c in COINS})[0], periods_per_year=YEAR)["sharpe"] for run in range(args.placebo_runs)]
            placebo_rows.append({"variant": name, "real": perf["sharpe"], "placebo_median": float(np.median(placebo)), "placebo_95": float(np.quantile(placebo, 0.95)),
                                 "share_beaten": placebo_share(perf["sharpe"], placebo)})

    table = pd.DataFrame(rows)
    reference_vol = table.loc[table["variant"] == "ewma", "ann_vol_pct"].iloc[0] / 100
    matched_rows = [{"variant": name, **matched(series, reference_vol)} for name, series in books.items()]
    table = table.merge(pd.DataFrame(matched_rows), on="variant")
    table = table.merge(pd.DataFrame([{"variant": name, **deflated_sharpe(series, "H3")} for name, series in books.items()])[["variant", "dsr_family", "dsr_global"]], on="variant")
    placebos = pd.DataFrame(placebo_rows)
    per_coin = pd.DataFrame(coin_rows)
    coin_matched = []
    for coin in COINS:
        ref = per_coin[(per_coin["variant"] == "ewma") & (per_coin["coin"] == coin)]["ann_vol_pct"].iloc[0] / 100
        for _, row in per_coin[per_coin["coin"] == coin].iterrows():
            coin_matched.append({"variant": row["variant"], "coin": coin, "net_sharpe": row["net_sharpe"], "max_dd_pct": row["max_dd_pct"], **matched(row["_series"], ref)})
    per_coin = pd.DataFrame(coin_matched)
    years = pd.DataFrame(year_tables)
    corr = pd.DataFrame(books).corr()

    # verdict inputs
    base = table.set_index("variant").loc["ewma"]
    verdict = {}
    for name in ("iv", "iv_debiased", "a2_proxy_riskoff"):
        row = table.set_index("variant").loc[name]
        coins_better = sum(
            (per_coin[(per_coin.variant == name) & (per_coin.coin == c)]["matched_max_dd_pct"].iloc[0] < per_coin[(per_coin.variant == "ewma") & (per_coin.coin == c)]["matched_max_dd_pct"].iloc[0])
            and (per_coin[(per_coin.variant == name) & (per_coin.coin == c)]["net_sharpe"].iloc[0] >= per_coin[(per_coin.variant == "ewma") & (per_coin.coin == c)]["net_sharpe"].iloc[0])
            for c in COINS)
        beaten = placebos.set_index("variant")["share_beaten"].get(name, np.nan)
        verdict[name] = {"sharpe_ge_ewma": row["net_sharpe"] >= base["net_sharpe"], "matched_dd_lower": row["matched_max_dd_pct"] < base["matched_max_dd_pct"],
                         "coins_improved": coins_better, "placebo_beaten": beaten, "net_2x_positive": row["net_sharpe_2x"] > 0, "dsr_family": row["dsr_family"]}
    verdicts = pd.DataFrame(verdict).T

    for name, frame in {"p_tests": p_tests, "p_years": p_years, "book": table, "per_coin": per_coin, "sleeves": pd.DataFrame(sleeve_rows),
                        "years": years, "placebo": placebos, "verdict": verdicts, "correlation": corr}.items():
        frame.to_csv(out / f"{name}.csv")
    governance.write_manifest(out, args=args, data=data_range, extra={"hypothesis": "H3", "prereg": "research/prereg/H3.txt"})

    report = ["# H3 results (development data only; holdout locked)", "", f"Run {stamp}, commit `{governance.git_commit()}`, outputs in `{out}`.", "",
              "## P1 / P2a", "", md_table(p_tests, index=False, digits=4), "", "By year:", "", md_table(p_years, index=False), "",
              f"P1 pass (NW t > 2 both coins): {p1_pass}; share of positive years: {p1_years.to_dict()}", "",
              "## A1 / A2-proxy: the live sleeves on BTC and ETH, equal-weight book", "", md_table(table, index=False), "",
              "Per coin (matched = scaled to that coin's EWMA book vol):", "", md_table(per_coin, index=False), "",
              "Per sleeve:", "", md_table(pd.DataFrame(sleeve_rows), index=False), "", "Per-year Sharpe:", "", md_table(years), "",
              "Placebo (IV shifted in time):", "", md_table(placebos, index=False), "", "Verdict inputs:", "", md_table(verdicts), "",
              "Book return correlation:", "", md_table(corr), ""]
    (out / "report_raw.md").write_text("\n".join(report))
    print("\n".join(report))


if __name__ == "__main__":
    main()
