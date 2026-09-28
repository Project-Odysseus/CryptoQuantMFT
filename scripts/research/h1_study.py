"""H1, leverage crowding: run the pre-registered study (research/prereg/H1.txt) on the development window.

    python scripts/research/h1_study.py              # writes research/reports/H1.md and data/research/h1_<time>/

Every rule configuration is logged to the trial ledger (family "H1"). The frozen holdout stays locked.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import skew

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import governance  # noqa: E402
from src.research.bar_engine import by_year, performance  # noqa: E402
from src.research.costs import VENUES  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402
from src.research.hypotheses.h1_crowding import HORIZONS, build_signals, prepare_market, run_carry, run_tilt  # noqa: E402
from src.research.inference import (  # noqa: E402
    circular_shifts,
    deflated_sharpe,
    driscoll_kraay_ols,
    log_trial,
    newey_west_ols,
    placebo_share,
)
from src.research.stats import sharpe_per_period  # noqa: E402

COINS = ("BTC", "ETH", "SOL")
YEAR = 365.0
TILT_GRID = [(source, hold) for source in ("z", "basis_z") for hold in (1, 3, 7)]
CARRY_GRID = [(fees, hold) for fees in ("binance", "kraken") for hold in (7, 14, 30)]
PLACEBO_RUNS = 500


def pooled(series_by_coin: dict[str, pd.Series]) -> pd.Series:
    """Equal-weight average across the coins that have a value that day."""
    return pd.DataFrame(series_by_coin).mean(axis=1, skipna=True).dropna()


def sharpe(values: pd.Series) -> float:
    return performance(values, periods_per_year=YEAR)["sharpe"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--placebo-runs", type=int, default=PLACEBO_RUNS)
    parser.add_argument("--no-ledger", action="store_true", help="don't log trials (for dry runs of the script itself)")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path("data/research") / f"h1_{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    report: list[str] = []

    # ---- data -----------------------------------------------------------------------------------------------------
    signals = {coin: build_signals(coin) for coin in COINS}
    markets = {coin: prepare_market(coin, signals[coin], VENUES) for coin in COINS}
    panel = pd.concat([frame.assign(date=frame.index) for frame in signals.values()], ignore_index=True)
    panel.to_parquet(out / "signals.parquet")
    coverage = pd.DataFrame({coin: {
        "decisions": len(frame), "first z": f"{frame['z'].first_valid_index():%Y-%m-%d}",
        "first dOI3": f"{frame['dOI3'].first_valid_index():%Y-%m-%d}",
        "first basis z": f"{frame['basis_z'].first_valid_index():%Y-%m-%d}" if frame["basis_z"].notna().any() else "none",
        "OI-weighted share": round(float(frame.loc[frame["F"].notna(), "oi_weighted"].mean()), 3),
        "last decision": f"{frame.index.max():%Y-%m-%d}"} for coin, frame in signals.items()}).T
    data_range = f"{panel['date'].min():%Y-%m-%d}..{panel['date'].max():%Y-%m-%d}"

    # ---- diagnostics ------------------------------------------------------------------------------------------------
    diag_rows = []
    for coin, frame in signals.items():
        for column in ("z", "dOI3", "basis_z", "F_ann"):
            values = frame[column].dropna()
            if values.empty:
                continue
            diag_rows.append({"coin": coin, "signal": column, "n": len(values), "mean": values.mean(), "std": values.std(), "skew": skew(values),
                              "share z>2" if column == "z" else "p99": (values > 2).mean() if column == "z" else values.quantile(0.99),
                              "ac1": values.autocorr(1), "ac7": values.autocorr(7), "ac30": values.autocorr(30)})
    diagnostics = pd.DataFrame(diag_rows)
    correlation = panel[["z", "dOI3", "basis_z", "F_ann", "basis_ann"]].corr(method="spearman")
    trigger_rows = []
    for coin, frame in signals.items():
        crowded = (frame["z"] > 2) & (frame["dOI3"] > 0)
        trigger_rows.append({"coin": coin, "days": int(frame["z"].notna().sum()), "crowded days (z>2 & OI up)": int(crowded.sum()),
                             "washed-out days (z<-1.5)": int((frame["z"] < -1.5).sum()),
                             "basis z>2 & OI up": int(((frame["basis_z"] > 2) & (frame["dOI3"] > 0)).sum()),
                             "basis z<-1.5": int((frame["basis_z"] < -1.5).sum())})
    triggers = pd.DataFrame(trigger_rows)

    # ---- predictive tests ------------------------------------------------------------------------------------------
    reg_rows = []
    for h in HORIZONS:
        y = f"fwd_{h}d"
        for coin, frame in signals.items():
            for signal in ("z", "basis_z"):
                data = frame[[signal, y]].dropna()
                if len(data) < 100:
                    continue
                result = newey_west_ols(data[y], data[signal], lags=h)
                reg_rows.append({"h": h, "coin": coin, "signal": signal, "slope_bps": result.loc["x1", "coef"] * 1e4, "t": result.loc["x1", "t"], "n": len(data)})
        for signal in ("z", "basis_z"):
            result = driscoll_kraay_ols(panel, y, [signal], time="date", lags=h)
            reg_rows.append({"h": h, "coin": "pooled", "signal": signal, "slope_bps": result.loc[signal, "coef"] * 1e4, "t": result.loc[signal, "t"], "n": int(result.loc[signal, "n"])})
    regressions = pd.DataFrame(reg_rows)

    inter_rows = []
    panel["oi_up"] = (panel["dOI3"] > 0).astype(float).where(panel["dOI3"].notna())
    panel["z_x_oi_up"] = panel["z"] * panel["oi_up"]
    for h in HORIZONS:
        result = driscoll_kraay_ols(panel, f"fwd_{h}d", ["z", "oi_up", "z_x_oi_up"], time="date", lags=h)
        for name in ("z", "oi_up", "z_x_oi_up"):
            inter_rows.append({"h": h, "term": name, "coef_bps": result.loc[name, "coef"] * 1e4, "t": result.loc[name, "t"], "n": int(result.loc[name, "n"])})
    interaction = pd.DataFrame(inter_rows)

    sort_rows = []
    for signal in ("z", "basis_z"):
        data = panel.dropna(subset=[signal])
        data = data.assign(quintile=pd.qcut(data[signal], 5, labels=[1, 2, 3, 4, 5]))
        for h in HORIZONS:
            for quintile, group in data.groupby("quintile", observed=True):
                r = group[f"fwd_{h}d"].dropna()
                sort_rows.append({"signal": signal, "h": h, "quintile": int(quintile), "n": len(r), "mean_bps": r.mean() * 1e4, "skew": skew(r),
                                  "q05_bps": r.quantile(0.05) * 1e4, "signal_range": f"{group[signal].min():.2f}..{group[signal].max():.2f}"})
    sorts = pd.DataFrame(sort_rows)
    extreme_rows = []
    for h in HORIZONS:
        r = panel[f"fwd_{h}d"]
        for label, mask in {"z>2 & OI up": (panel["z"] > 2) & (panel["dOI3"] > 0), "z>2 & OI down": (panel["z"] > 2) & (panel["dOI3"] <= 0),
                            "z<-1.5": panel["z"] < -1.5, "all": panel["z"].notna()}.items():
            values = r[mask].dropna()
            extreme_rows.append({"h": h, "bucket": label, "n": len(values), "mean_bps": values.mean() * 1e4, "q05_bps": values.quantile(0.05) * 1e4 if len(values) else np.nan})
    extremes = pd.DataFrame(extreme_rows)

    slope3 = regressions[(regressions["h"] == 3) & (regressions["signal"] == "z") & (regressions["coin"] != "pooled")]
    kill_sign = int((slope3["slope_bps"] < 0).sum()) < 2

    # ---- rules -----------------------------------------------------------------------------------------------------
    rule_rows, year_tables, coin_rows, placebo_rows, dsr_rows = [], {}, [], [], []
    best_series: dict[str, pd.Series] = {}
    for source, hold in TILT_GRID:
        coins = [c for c in COINS if signals[c][source].notna().any()]
        label = f"tilt source={source} min_hold={hold}"
        nets, nets2, grosses, totals, bases, trades = {}, {}, {}, {}, {}, 0
        for coin in coins:
            runs = run_tilt(signals[coin], markets[coin], source=source, min_hold=hold)
            runs2 = run_tilt(signals[coin], markets[coin], source=source, min_hold=hold, multiplier=2.0)
            active = signals[coin][source].notna().reindex(runs["overlay"].frame.index).fillna(False)
            nets[coin], grosses[coin] = runs["overlay"].net[active], runs["overlay"].gross[active]
            nets2[coin] = runs2["overlay"].net[active]
            totals[coin], bases[coin] = runs["total"].net[active], runs["base"].net[active]
            trades += runs["overlay"].trades()
            coin_rows.append({"rule": label, "coin": coin, "gross_sharpe": sharpe(grosses[coin]), "net_sharpe": sharpe(nets[coin]), "net_sharpe_2x": sharpe(nets2[coin]),
                              "ann_net_pct": nets[coin].mean() * YEAR * 100, "exposure": float((runs["overlay"].frame.loc[active, "position"] != 0).mean()),
                              "trades": runs["overlay"].trades(), "base_sharpe": sharpe(bases[coin]), "base+tilt_sharpe": sharpe(totals[coin]),
                              "base_maxdd": performance(bases[coin], periods_per_year=YEAR)["max_drawdown"], "base+tilt_maxdd": performance(totals[coin], periods_per_year=YEAR)["max_drawdown"]})
        net, net2, gross = pooled(nets), pooled(nets2), pooled(grosses)
        perf = performance(net, periods_per_year=YEAR)
        years = by_year(net, periods_per_year=YEAR)
        year_tables[label] = pd.concat({coin: by_year(nets[coin], periods_per_year=YEAR)["sharpe"] for coin in coins} | {"pooled": years["sharpe"]}, axis=1)
        # placebo: each coin's signal inputs shifted by its own random 60+ days
        shifts = {coin: circular_shifts(len(markets[coin].perp_days), args.placebo_runs, min_shift=60, seed=i + 11) for i, coin in enumerate(coins)}
        placebo = [sharpe(pooled({coin: run_tilt(signals[coin], markets[coin], source=source, min_hold=hold, shift_days=int(shifts[coin][run]))["overlay"].net for coin in coins}))
                   for run in range(args.placebo_runs)]
        if not args.no_ledger:
            log_trial("H1", "h1_study", {"rule": "tilt", "source": source, "min_hold": hold}, data_range=data_range, gross_sharpe=sharpe(gross),
                      net_sharpe=perf["sharpe"], net_sharpe_2x=sharpe(net2), trades=trades, sharpe_per_period_net=sharpe_per_period(net))
        best_series[label] = net
        rule_rows.append({"rule": label, "coins": len(coins), "gross_sharpe": sharpe(gross), "net_sharpe": perf["sharpe"], "net_sharpe_2x": sharpe(net2),
                          "ann_net_pct": perf["ann_return"] * 100, "max_dd_pct": perf["max_drawdown"] * 100, "trades": trades,
                          "coins_positive": sum(sharpe(nets[c]) > 0 for c in coins), "years_positive": f"{int((years['return'] > 0).sum())}/{len(years)}",
                          "placebo_beaten": placebo_share(perf["sharpe"], placebo)})
        placebo_rows.append({"rule": label, "real": perf["sharpe"], "placebo_median": float(np.median(placebo)), "placebo_95": float(np.quantile(placebo, 0.95)), "share_beaten": placebo_share(perf["sharpe"], placebo)})

    carry_detail = []
    for fees, hold in CARRY_GRID:
        label = f"carry fees={fees} expected_hold={hold}d"
        nets, nets2, grosses, trades, parts = {}, {}, {}, 0, []
        for coin in COINS:
            frame, info = run_carry(signals[coin], markets[coin], VENUES, fees=fees, expected_hold_days=hold)
            frame2, _ = run_carry(signals[coin], markets[coin], VENUES, fees=fees, expected_hold_days=hold, multiplier=2.0)
            active = signals[coin]["F_ann"].notna().reindex(frame.index).fillna(False)
            nets[coin], grosses[coin], nets2[coin] = frame["net"][active], frame["gross"][active], frame2["net"][active]
            entries = int(((frame["position"] > 0) & (frame["position"].shift(1, fill_value=0) == 0)).sum())
            trades += entries
            coin_rows.append({"rule": label, "coin": coin, "gross_sharpe": sharpe(grosses[coin]), "net_sharpe": sharpe(nets[coin]), "net_sharpe_2x": sharpe(nets2[coin]),
                              "ann_net_pct": nets[coin].mean() * YEAR * 100, "exposure": float(frame.loc[active, "position"].mean()), "trades": entries})
            carry_detail.append({"rule": label, "coin": coin, "round_trip_pct": info["round_trip"] * 100, "hurdle_ann_pct": info["hurdle_ann"] * 100,
                                 "funding_pct_yr": frame["funding"][active].mean() * YEAR * 100, "basis_pnl_pct_yr": (frame["spot"] + frame["perp"])[active].mean() * YEAR * 100,
                                 "cost_pct_yr": frame["cost"][active].mean() * YEAR * 100, "net_pct_yr": nets[coin].mean() * YEAR * 100,
                                 "net_on_capital_pct_yr": nets[coin].mean() * YEAR * 100 / 1.5, "time_in": float(frame.loc[active, "position"].mean()), "entries": entries})
        net, net2, gross = pooled(nets), pooled(nets2), pooled(grosses)
        perf = performance(net, periods_per_year=YEAR)
        years = by_year(net, periods_per_year=YEAR)
        year_tables[label] = pd.concat({coin: by_year(nets[coin], periods_per_year=YEAR)["return"] * 100 for coin in COINS} | {"pooled": years["return"] * 100}, axis=1)
        shifts = {coin: circular_shifts(len(markets[coin].perp_days), args.placebo_runs, min_shift=60, seed=i + 101) for i, coin in enumerate(COINS)}
        placebo = [sharpe(pooled({coin: run_carry(signals[coin], markets[coin], VENUES, fees=fees, expected_hold_days=hold, shift_days=int(shifts[coin][run]))[0]["net"] for coin in COINS}))
                   for run in range(args.placebo_runs)]
        always = pooled({coin: run_carry(signals[coin], markets[coin], VENUES, fees=fees, expected_hold_days=hold, always_on=True)[0]["net"] for coin in COINS})
        if not args.no_ledger:
            log_trial("H1", "h1_study", {"rule": "carry", "fees": fees, "expected_hold": hold}, data_range=data_range, gross_sharpe=sharpe(gross),
                      net_sharpe=perf["sharpe"], net_sharpe_2x=sharpe(net2), trades=trades, sharpe_per_period_net=sharpe_per_period(net))
        best_series[label] = net
        rule_rows.append({"rule": label, "coins": 3, "gross_sharpe": sharpe(gross), "net_sharpe": perf["sharpe"], "net_sharpe_2x": sharpe(net2),
                          "ann_net_pct": perf["ann_return"] * 100, "max_dd_pct": perf["max_drawdown"] * 100, "trades": trades,
                          "coins_positive": sum(sharpe(nets[c]) > 0 for c in COINS), "years_positive": f"{int((years['return'] > 0).sum())}/{len(years)}",
                          "placebo_beaten": placebo_share(perf["sharpe"], placebo), "always_on_sharpe": sharpe(always), "always_on_ann_pct": always.mean() * YEAR * 100})
        placebo_rows.append({"rule": label, "real": perf["sharpe"], "placebo_median": float(np.median(placebo)), "placebo_95": float(np.quantile(placebo, 0.95)), "share_beaten": placebo_share(perf["sharpe"], placebo)})

    for label, series in best_series.items():
        dsr_rows.append({"rule": label, **deflated_sharpe(series, "H1", rule=label.split()[0])})
    rules, per_coin, placebos, dsr, carry_table = (pd.DataFrame(rule_rows), pd.DataFrame(coin_rows), pd.DataFrame(placebo_rows),
                                                   pd.DataFrame(dsr_rows), pd.DataFrame(carry_detail))
    rules = rules.merge(dsr[["rule", "dsr_family", "dsr_global", "dsr_family_rule_var", "dsr_global_rule_var"]], on="rule")
    rules["passes_dev"] = ((rules["net_sharpe"] > 0) & (rules["dsr_family"] >= 0.95) & (rules["net_sharpe_2x"] > 0)
                           & (rules["coins_positive"] >= 2) & (rules["placebo_beaten"] >= 0.95)
                           & rules["years_positive"].map(lambda s: int(s.split("/")[0]) >= 0.6 * int(s.split("/")[1])))
    binance_carry = rules[rules["rule"].str.startswith("carry fees=binance")]
    kill_carry = bool((binance_carry["net_sharpe"] <= 0).all())

    for name, frame in {"coverage": coverage, "diagnostics": diagnostics, "correlation": correlation, "triggers": triggers, "regressions": regressions,
                        "interaction": interaction, "sorts": sorts, "extremes": extremes, "rules": rules, "per_coin": per_coin, "placebo": placebos,
                        "dsr": dsr, "carry_detail": carry_table}.items():
        frame.to_csv(out / f"{name}.csv", index=name in ("coverage", "correlation"))
    for label, table in year_tables.items():
        table.to_csv(out / f"years_{label.replace(' ', '_').replace('=', '-')}.csv")
    governance.write_manifest(out, args=args, data=data_range, extra={"hypothesis": "H1", "prereg": "research/prereg/H1.txt"})

    # ---- report ------------------------------------------------------------------------------------------------------
    pd.set_option("display.width", 200)
    report += ["# H1 results (development data only; holdout locked)", "",
               f"Run {stamp}, commit `{governance.git_commit()}`, outputs in `{out}`. Pre-registration: `research/prereg/H1.txt`.", "",
               "## Data coverage", "", md_table(coverage), "",
               "## Signal diagnostics", "", md_table(diagnostics, index=False), "", "Spearman correlation (pooled):", "", md_table(correlation), "",
               "Trigger counts:", "", md_table(triggers, index=False), "",
               "## Predictive tests", "", "Per-coin Newey-West and pooled Driscoll-Kraay regressions of the forward log return on the signal (slope in bps per unit z):", "",
               md_table(regressions, index=False), "", "Interaction (pooled, Driscoll-Kraay):", "", md_table(interaction, index=False), "",
               "Quintile sorts (pooled breakpoints):", "", md_table(sorts, index=False), "", "Extreme buckets:", "", md_table(extremes, index=False), "",
               f"Kill check (z slope at 3d negative in >= 2 of 3 coins): {'KILL' if kill_sign else 'pass'}.", "",
               "## Rules", "", md_table(rules, index=False), "", "Per coin:", "", md_table(per_coin, index=False), "",
               "Carry decomposition (% of notional a year):", "", md_table(carry_table, index=False), "",
               f"Kill check (Binance-fee carry net <= 0 for every hold): {'KILL' if kill_carry else 'pass'}.", "",
               "Per-year net Sharpe (tilt) / net return % (carry):", ""]
    for label, table in year_tables.items():
        report += [f"`{label}`", "", md_table(table), ""]
    report += ["## Placebo", "", md_table(placebos, index=False), "", "## Deflated Sharpe", "", md_table(dsr, index=False), ""]
    Path("research/reports").mkdir(parents=True, exist_ok=True)
    (out / "report_raw.md").write_text("\n".join(report))
    print("\n".join(report))


if __name__ == "__main__":
    main()
