"""H14, a causal three-state regime overlay over a trend and a range model on equity indices (research/prereg/H14.txt).

    python scripts/research/h14_study.py                 # writes data/research/h14_<time>/ and logs the trials
    python scripts/research/h14_study.py --no-ledger     # a rerun after a code fix
    python scripts/research/h14_study.py --refresh       # download the index bars again

Outputs: tables.md (every results table), charts/*.png (from the backtest output), classification.json, the strategy
card, and machine-readable CSVs (features and scores, regimes, model signals and weights, targets, executed
positions, trades, daily P&L and costs, metrics), with the config's hash in manifest.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import governance  # noqa: E402
from src.research import regime_switch as rs  # noqa: E402
from src.research import strategy_checks as stc  # noqa: E402
from src.research.hypotheses.common import md_table  # noqa: E402

HYPOTHESIS = "H14"
CORE = ["1 trend alone", "2A range alone", "2B range alone", "3A hard switch", "3B hard switch", "4A three-state, hysteresis", "4B three-state, hysteresis",
        rs.PRIMARY, "5B three-state, transition blending", "6A static 50/50", "6B static 50/50", "7A 200-day MA regime filter",
        "8 buy and hold (1x)", "8v volatility-managed long", "9A exposure-matched static"]
SHOWN = ["annual return", "volatility", "Sharpe", "Sortino", "max drawdown", "Calmar", "turnover", "trades", "avg holding days", "hit rate", "avg win", "avg loss",
         "profit factor", "exposure", "time invested", "long contribution", "short contribution", "costs per year", "gross Sharpe"]


# --- helpers ---------------------------------------------------------------------------------------------------------

def start_of(study: rs.MarketStudy, config: rs.Config) -> pd.Timestamp:
    """The market's first evaluated day: the evaluation start, or later when its features are still warming up."""
    ready = study.scores["trend_probability"].first_valid_index()
    return max(pd.Timestamp(config.data["evaluation_start"]), ready)


def periods(study: rs.MarketStudy, config: rs.Config) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    start, end, split = start_of(study, config), pd.Timestamp(config.data["evaluation_end"]), pd.Timestamp(config.data["validation_start"])
    return {"whole": (start, end), "exploratory": (start, split - pd.Timedelta(days=1)), "validation": (max(start, split), end)}


def sharpe(series: pd.Series) -> float:
    return float(series.mean() / series.std() * np.sqrt(252.0)) if len(series) > 20 and series.std() > 0 else float("nan")


def episodes_of(states: pd.Series) -> pd.DataFrame:
    """One row per uninterrupted stretch of the same state."""
    change = states.ne(states.shift())
    ids = change.cumsum()
    grouped = states.groupby(ids)
    return pd.DataFrame({"state": grouped.first(), "start": grouped.apply(lambda s: s.index[0]), "end": grouped.apply(lambda s: s.index[-1]), "days": grouped.size()})


def roll_days(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Quarterly equity-index futures roll days: about 8 days before the third Friday of Mar/Jun/Sep/Dec."""
    out = []
    for year in sorted(set(index.year)):
        for month in (3, 6, 9, 12):
            first = pd.Timestamp(year=year, month=month, day=1)
            third_friday = first + pd.Timedelta(days=(4 - first.weekday()) % 7 + 14)
            out.append(third_friday - pd.Timedelta(days=8))
    return pd.DatetimeIndex(out)


# --- the study -------------------------------------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ledger", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    lock = governance.require_prereg(HYPOTHESIS)
    config = rs.load_config()
    config_text = rs.CONFIG_PATH.read_text()
    out = Path("data/research") / f"h14_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    charts = out / "charts"
    charts.mkdir(parents=True, exist_ok=True)
    (out / "config_used.toml").write_text(config_text)
    governance.write_manifest(out, args=args, extra={"prereg_version": lock["version"], "prereg_sha256": lock["sha256"],
                                                     "config_sha256": hashlib.sha256(config_text.encode()).hexdigest()})
    rates = rs.load_rates()
    frames, checks = {}, {}
    for market in [config.data["primary"], *config.data["cross_markets"]]:
        raw = rs.load_market(config, market, refresh=args.refresh)
        checks[market] = {**rs.data_checks(raw), "usable from": f"{rs.usable_from(raw):%Y-%m-%d}"}
        frames[market] = raw[raw.index >= rs.usable_from(raw)]
    tables: list[str] = [f"# H14 results tables (pre-registration v{lock['version']}, {lock['sha256'][:12]}; config {hashlib.sha256(config_text.encode()).hexdigest()[:12]})", ""]

    def section(title: str, frame: pd.DataFrame | str, digits: int = 3) -> None:
        tables.extend([f"## {title}", "", frame if isinstance(frame, str) else md_table(frame, digits=digits), ""])

    section("Data checks (raw downloads)", pd.DataFrame(checks).T)

    # --- the development market ---------------------------------------------------------------------------------
    nd = config.data["primary"]
    study = rs.run_market(config, nd, frames[nd], rates, full=True)
    windows = periods(study, config)
    whole = windows["whole"]
    rows = {name: rs.metrics(run, *whole) for name, run in study.runs.items()}
    section(f"{nd}: every strategy, net of base costs, {whole[0]:%Y-%m-%d} to {whole[1]:%Y-%m-%d}", pd.DataFrame(rows).T[SHOWN])
    by_period = pd.DataFrame({name: {f"Sharpe {label}": rs.metrics(run, *span).get("Sharpe") for label, span in windows.items()} for name, run in study.runs.items()}).T
    section(f"{nd}: Sharpe by period", by_period)
    gross_net = pd.DataFrame({name: {"gross Sharpe": rows[name]["gross Sharpe"], "net Sharpe": rows[name]["Sharpe"], "costs per year": rows[name]["costs per year"],
                                     **{f"Sharpe at {label} costs": sharpe(recost(study.runs[name], study, config, multiplier).net.loc[whole[0]:whole[1]])
                                        for label, multiplier in config.execution.cost_multipliers.items() if label != "base"}} for name in CORE}).T
    section(f"{nd}: gross and net, three cost cases", gross_net)

    primary = study.runs[rs.PRIMARY]
    regime = study.regimes[rs.PRIMARY].loc[whole[0]:whole[1]]
    states = regime["state"]
    net = lambda name: study.runs[name].net.loc[whole[0]:whole[1]]  # noqa: E731
    years = len(states) / 252.0

    # regime diagnostics
    eps = episodes_of(states)
    stress = study.scores["stress"].loc[whole[0]:whole[1]]
    regime_table = pd.DataFrame({state: {"share of days": float((states == state).mean()), "episodes": int((eps["state"] == state).sum()),
                                         "mean duration": float(eps.loc[eps["state"] == state, "days"].mean()), "median duration": float(eps.loc[eps["state"] == state, "days"].median()),
                                         "max duration": float(eps.loc[eps["state"] == state, "days"].max()),
                                         "share of episodes under 5 days": float((eps.loc[eps["state"] == state, "days"] < 5).mean())}
                                 for state in (rs.RANGE, rs.TRANSITION, rs.TREND)}).T
    section(f"{nd}: regimes of the primary overlay ({len(eps) - 1} changes, {(len(eps) - 1) / years:.1f} a year; STRESS on {stress.mean():.1%} of days)", regime_table)
    conditional = pd.DataFrame({name: {state: sharpe(net(name)[states == state]) for state in (rs.RANGE, rs.TRANSITION, rs.TREND)} for name in ("1 trend alone", "2A range alone", "6A static 50/50", rs.PRIMARY, "8 buy and hold (1x)")}).T
    conditional_return = pd.DataFrame({name: {state: float(net(name)[states == state].mean() * 252) for state in (rs.RANGE, rs.TRANSITION, rs.TREND)} for name in ("1 trend alone", "2A range alone", "6A static 50/50", rs.PRIMARY, "8 buy and hold (1x)")}).T
    section(f"{nd}: Sharpe of each model on the days the overlay calls each regime", conditional)
    section(f"{nd}: annualised return on those days", conditional_return)

    change_days = states.index[states.ne(states.shift()).to_numpy()][1:]
    after = {}
    for horizon in (1, 3, 5, 10):
        for name in (rs.PRIMARY, "1 trend alone", "2A range alone"):
            series = net(name)
            values = [series.loc[day:].iloc[1:horizon + 1].sum() for day in change_days]
            after.setdefault(name, {})[f"{horizon} days"] = float(np.mean(values)) * 1e4
    section(f"{nd}: average P&L in the first days after a regime change (bps of capital, {len(change_days)} changes)", pd.DataFrame(after).T, 1)

    trend_eps = eps[eps["state"] == rs.TREND]
    trend_pnl = [net("1 trend alone").loc[row.start:row.end].sum() for row in trend_eps.itertuples()]
    short_eps = eps[eps["days"] < 5]
    false_switch = pd.DataFrame({"TREND episodes": [len(trend_eps)], "where the trend model lost": [int(np.sum(np.array(trend_pnl) < 0))],
                                 "its loss in those, % of capital": [float(np.sum(np.minimum(trend_pnl, 0)) * 100)],
                                 "episodes under 5 days": [len(short_eps)],
                                 "primary P&L in them, %": [float(sum(net(rs.PRIMARY).loc[row.start:row.end].sum() for row in short_eps.itertuples()) * 100)]})
    section(f"{nd}: false and short-lived switches", false_switch.T.rename(columns={0: "value"}))

    # delayed trend recognition around genuine breakouts
    trend_state = study.trend["breakout_state"].loc[whole[0]:whole[1]]
    breakouts = trend_state.index[(trend_state != 0) & (trend_state != trend_state.shift())]
    weights = study.regimes[rs.PRIMARY]["trend_weight"]
    prob = study.regimes[rs.PRIMARY]["trend_probability"]
    returns = study.returns
    delay_rows, paths = [], []
    for day in breakouts:
        direction = trend_state.loc[day]
        future = weights.loc[day:].iloc[:61]
        full = future.index[future.to_numpy() >= 1.0]
        days_to_full = int(future.index.get_loc(full[0])) if len(full) else None
        until = full[0] if len(full) else future.index[-1]
        missed = float(direction * returns.loc[day:until].iloc[1:].sum())
        range_part = study.targets[rs.PRIMARY]["range_part"].loc[day:until] * rs.vol_scale(returns, config).loc[day:until]
        range_loss = float((range_part.shift(2) * returns.loc[day:until]).cumsum().min()) if len(range_part) > 2 else 0.0
        delay_rows.append({"breakout": day, "direction": direction, "days to full trend weight": days_to_full, "market move before full weight": missed,
                           "worst range P&L during the delay": min(range_loss, 0.0)})
        paths.append(prob.loc[day:].iloc[:10].to_numpy())
    delays = pd.DataFrame(delay_rows)
    reached = delays["days to full trend weight"].dropna()
    delay_summary = pd.DataFrame({"breakouts": [len(delays)], "reached full trend weight within 60 days": [len(reached)],
                                  "median days to full weight": [float(reached.median())], "mean days": [float(reached.mean())],
                                  "mean market move before full weight (in breakout direction)": [float(delays["market move before full weight"].mean())],
                                  "worst range P&L during a delay": [float(delays["worst range P&L during the delay"].min())],
                                  "Sharpe with rapid exception": [rows[rs.PRIMARY]["Sharpe"]], "Sharpe without": [rows["5A without rapid-breakout exception"]["Sharpe"]]}).T
    section(f"{nd}: delayed trend recognition after 60-day breakouts", delay_summary.rename(columns={0: "value"}))
    prob_path = pd.DataFrame([path for path in paths if len(path) == 10]).mean().rename("mean trend probability").to_frame()
    prob_path.index = [f"day {i}" for i in range(10)]
    section(f"{nd}: trend probability on the first 10 days after a breakout", prob_path.T)

    # switching turnover and costs
    weight_change = (study.regimes[rs.PRIMARY][["trend_weight", "range_weight"]].diff().abs().sum(axis=1) > 0).loc[whole[0]:whole[1]]
    switch_turnover = primary.traded.loc[whole[0]:whole[1]][weight_change.shift(1, fill_value=False)].sum() / years
    switch_costs = primary.costs.loc[whole[0]:whole[1]][weight_change.shift(1, fill_value=False)].sum() / years
    switching = pd.DataFrame({"turnover a year": [rows[rs.PRIMARY]["turnover"]], "turnover on the day after a weight change": [float(switch_turnover)],
                              "costs a year": [rows[rs.PRIMARY]["costs per year"]], "costs on the day after a weight change": [float(switch_costs)],
                              "Sharpe": [rows[rs.PRIMARY]["Sharpe"]], "Sharpe without switching costs": [sharpe(net(rs.PRIMARY) + primary.costs.loc[whole[0]:whole[1]].where(weight_change.shift(1, fill_value=False), 0.0))]}).T
    section(f"{nd}: turnover from regime switching", switching.rename(columns={0: "value"}))

    # shorting a rising index
    ma200 = study.frame["close"].rolling(200).mean()
    above = (study.frame["close"] > ma200).loc[whole[0]:whole[1]]
    short_rows = {}
    for name in ("2A range alone", "2B range alone", rs.PRIMARY, "5B three-state, transition blending", "5C three-state, no range shorts (diagnostic)"):
        run = study.runs[name]
        series, earning = net(name), run.earning.loc[whole[0]:whole[1]]
        trades = rs.trade_records(rs.Run(name, run.signal, run.decided, earning, run.gross.loc[whole[0]:whole[1]], series, run.costs, run.traded))
        shorts = trades[trades["side"] < 0]
        short_rows[name] = {"long contribution": float(series[earning > 0].sum() / years), "short contribution": float(series[earning < 0].sum() / years),
                            "short trades": len(shorts), "short hit rate": float((shorts["pnl"] > 0).mean()) if len(shorts) else float("nan"),
                            "average short loss": float(shorts.loc[shorts["pnl"] <= 0, "pnl"].mean()) if len(shorts) else float("nan"),
                            "short P&L above 200-day MA": float(series[(earning < 0) & above].sum() / years), "short P&L below": float(series[(earning < 0) & ~above].sum() / years),
                            "Sharpe": rows[name]["Sharpe"]}
    section(f"{nd}: short range trades in a rising index (per year, fractions of capital)", pd.DataFrame(short_rows).T)

    # roll dates
    entries = rs.trade_records(primary)["entry"]
    entries = entries[(entries >= whole[0]) & (entries <= whole[1])]
    rolls = roll_days(states.index)
    near = sum(any(abs((entry - roll).days) <= 3 for roll in rolls) for entry in entries)
    base_share = sum(any(abs((day - roll).days) <= 3 for roll in rolls) for day in states.index[::5]) / len(states.index[::5])
    section("Roll dates (index proxy: no rolls in the data)", f"{near} of {len(entries)} primary trade entries ({near / max(len(entries), 1):.1%}) fall within 3 days of a quarterly roll day; "
            f"{base_share:.1%} of all days do. An index series has no roll gaps, so no signal can come from one.")

    # feature redundancy
    raw_features = study.scores[["er", "abs_slope_t", "adx", "vr", "breakout_beyond", "vol_ratio"]].loc[whole[0]:whole[1]]
    section(f"{nd}: rank correlation of the regime features", raw_features.corr(method="spearman"), 2)
    score_cols = [f"{name}_score" for name in config.regime.weights]
    rolling_corr = {f"{a} / {b}": study.scores[a].rolling(252).corr(study.scores[b]).loc[whole[0]:whole[1]].agg(["mean", "min", "max"])
                    for i, a in enumerate(score_cols) for b in score_cols[i + 1:]}
    section(f"{nd}: one-year rolling correlation between scores", pd.DataFrame(rolling_corr).T, 2)
    edge = (net("1 trend alone").rolling(20).sum().shift(-20) - net("2A range alone").rolling(20).sum().shift(-20))
    marginal = {column: float(raw_features[column].corr(edge.loc[raw_features.index], method="spearman")) for column in raw_features}
    section(f"{nd}: each feature against which model earned more over the next 20 days (rank correlation; diagnostic, uses the future)",
            pd.Series(marginal, name="rank correlation").to_frame().T, 3)
    ablation = pd.DataFrame({name: {"Sharpe": rows[name]["Sharpe"], "max drawdown": rows[name]["max drawdown"], "turnover": rows[name]["turnover"],
                                    "regime changes a year": float((study.regimes[name]["state"].loc[whole[0]:whole[1]].ne(study.regimes[name]["state"].loc[whole[0]:whole[1]].shift())).sum() / years)}
                             for name in [rs.PRIMARY, *[n for n in study.runs if n.startswith(("X ", "S "))]]}).T
    section(f"{nd}: feature ablation (without each feature; each feature alone)", ablation)

    # persistent transition
    trans = eps[eps["state"] == rs.TRANSITION]
    in_trans = states == rs.TRANSITION
    transition_table = pd.DataFrame({"share of days": [float(in_trans.mean())], "mean duration": [float(trans["days"].mean())], "max duration": [float(trans["days"].max())],
                                     "market return a year on those days": [float(study.returns.loc[whole[0]:whole[1]][in_trans].sum() / years)],
                                     "primary": [float(net(rs.PRIMARY)[in_trans].sum() / years)], "trend alone": [float(net("1 trend alone")[in_trans].sum() / years)],
                                     "static 50/50": [float(net("6A static 50/50")[in_trans].sum() / years)],
                                     "return missed vs trend alone": [float((net("1 trend alone") - net(rs.PRIMARY))[in_trans].sum() / years)],
                                     "net effect of reduced exposure vs static": [float((net(rs.PRIMARY) - net("6A static 50/50"))[in_trans].sum() / years)]}).T
    section(f"{nd}: the TRANSITION state (per year, fractions of capital)", transition_table.rename(columns={0: "value"}))

    # beta and attribution
    target = study.targets[rs.PRIMARY]
    earning = primary.earning.loc[whole[0]:whole[1]]
    market = study.returns.loc[whole[0]:whole[1]].fillna(0.0)
    share_trend = (target["trend_part"] / target["target_signal"].where(target["target_signal"] != 0)).clip(-5, 5).fillna(0.0).shift(2).loc[whole[0]:whole[1]]
    gross = primary.gross.loc[whole[0]:whole[1]]
    attribution = pd.DataFrame({"average net position": [float(earning.mean())], "average long": [float(earning.clip(lower=0).mean())], "average short": [float(earning.clip(upper=0).mean())],
                                "from average exposure (a year)": [float(earning.mean() * market.mean() * 252)],
                                "from time-varying exposure (a year)": [float(gross.mean() * 252 - earning.mean() * market.mean() * 252)],
                                "from trend signals (gross, a year)": [float((gross * share_trend).sum() / years)],
                                "from range signals (gross, a year)": [float((gross * (1 - share_trend)).sum() / years)],
                                "overlay vs static 50/50 (net, a year)": [float((net(rs.PRIMARY) - net("6A static 50/50")).sum() / years)]}).T
    section(f"{nd}: where the primary's return comes from", attribution.rename(columns={0: "value"}))
    equal_vol = net("8 buy and hold (1x)") * (net(rs.PRIMARY).std() / net("8 buy and hold (1x)").std())
    references = pd.DataFrame({"long at equal volatility": {"Sharpe": sharpe(equal_vol), "annual return": float(equal_vol.mean() * 252)},
                               **{name: {"Sharpe": rows[name]["Sharpe"], "annual return": rows[name]["annual return"]}
                                  for name in ("R MA-filtered long", "8v volatility-managed long", "1 trend alone", "2A range alone", "6A static 50/50", "9A exposure-matched static", rs.PRIMARY)}}).T
    section(f"{nd}: the primary against simpler references", references)

    # dependence on a few episodes
    trades = rs.trade_records(primary)
    trades = trades[(trades["entry"] >= whole[0]) & (trades["entry"] <= whole[1])]
    monthly = (1 + net(rs.PRIMARY)).groupby(net(rs.PRIMARY).index.to_period("M")).prod() - 1
    yearly = (1 + net(rs.PRIMARY)).groupby(net(rs.PRIMARY).index.year).prod() - 1
    best_trade = trades.loc[trades["pnl"].idxmax()] if len(trades) else None
    series = net(rs.PRIMARY)
    without = {"whole": sharpe(series),
               "without the best trade": sharpe(series.drop(series.loc[best_trade["entry"]:best_trade["exit"]].index)) if best_trade is not None else float("nan"),
               "without the best month": sharpe(series[series.index.to_period("M") != monthly.idxmax()]),
               "without the best year": sharpe(series[series.index.year != yearly.idxmax()])}
    section(f"{nd}: primary Sharpe without its best trade / month / year (best year {yearly.idxmax()})", pd.Series(without).to_frame("Sharpe").T)
    section(f"{nd}: best and worst 5 trades", pd.concat([trades.nlargest(5, "pnl").assign(rank="best"), trades.nsmallest(5, "pnl").assign(rank="worst")]).set_index("rank"))
    section(f"{nd}: best and worst 5 months", pd.concat([monthly.nlargest(5), monthly.nsmallest(5)]).to_frame("return"))
    episode_rows = {}
    for label, (first, last) in config.episodes.items():
        episode_rows[label] = {name: float((1 + study.runs[name].net.loc[first:last]).prod() - 1) for name in (rs.PRIMARY, "1 trend alone", "2A range alone", "6A static 50/50", "3A hard switch", "8 buy and hold (1x)")}
    section(f"{nd}: market environments (total return over each)", pd.DataFrame(episode_rows).T)

    # simple filters and confidence scaling
    filter_rows = {name: {"Sharpe": rows[name]["Sharpe"], "annual return": rows[name]["annual return"], "max drawdown": rows[name]["max drawdown"], "turnover": rows[name]["turnover"]}
                   for name in [rs.PRIMARY, "3A hard switch", "6A static 50/50", "P probability-weighted (confidence scaling)", "7A 200-day MA regime filter", *[n for n in study.runs if n.startswith("F ")]]}
    section(f"{nd}: the overlay against simple filters and confidence scaling", pd.DataFrame(filter_rows).T)

    # ADF diagnostic
    from src.signals.regime_features import adf_tstat

    adf = pd.Series(adf_tstat(study.range_inputs["residual"].to_numpy(), 120), index=study.frame.index).loc[whole[0]:whole[1]]
    section("ADF on the residual (diagnostic only): share of days with t < -2.9, by regime", pd.Series({state: float((adf[states == state] < -2.9).mean()) for state in (rs.RANGE, rs.TRANSITION, rs.TREND)}).to_frame("share").T)

    # --- sensitivity ---------------------------------------------------------------------------------------------
    sens_specs: dict[str, dict[str, dict[str, Any]]] = {}
    grid = config.sensitivity
    for value in grid["horizon"]:
        sens_specs[f"regime horizon {value}"] = {"regime": {"horizon": value}}
    for value in grid["trend_entry"]:
        sens_specs[f"TREND entry {value}"] = {"regime": {"trend_entry": value}}
    for value in grid["range_entry"]:
        sens_specs[f"RANGE entry {value}"] = {"regime": {"range_entry": value}}
    for value in grid["confirm"]:
        sens_specs[f"confirmation {value}"] = {"regime": {"confirm": value}}
    for value in grid["entry_z"]:
        sens_specs[f"range |z| entry {value}"] = {"range": {"entry_z": value}}
    for value in grid["breakout_entry"]:
        sens_specs[f"breakout {value}"] = {"trend": {"breakout_entry": value}}
    for value in grid["anchor_span"]:
        sens_specs[f"anchor {value}"] = {"range": {"anchor_span": value}}
    for fast, slow in grid["ma_pair"]:
        sens_specs[f"MA pair {fast}/{slow}"] = {"trend": {"fast_mean": fast, "slow_mean": slow}}
    for label in ("fast", "slow"):
        sens_specs[f"joint {label}"] = {"regime": dict(grid[label])}
    sens_rows, sens_returns = {}, {"baseline": net(rs.PRIMARY)}
    for label, changes in sens_specs.items():
        variant = config.with_changes(**changes)
        result = rs.run_market(variant, nd, frames[nd], rates, full=False)
        run = result.runs[rs.PRIMARY]
        span = periods(result, variant)
        m = rs.metrics(run, *span["whole"])
        st = result.regimes[rs.PRIMARY]["state"].loc[span["whole"][0]:span["whole"][1]]
        ep = episodes_of(st)
        sens_returns[label] = run.net.loc[whole[0]:whole[1]]
        sens_rows[label] = {"Sharpe": m["Sharpe"], "static 50/50 Sharpe": rs.metrics(result.runs["6A static 50/50"], *span["whole"])["Sharpe"],
                            "annual return": m["annual return"], "max drawdown": m["max drawdown"], "turnover": m["turnover"],
                            "regime changes a year": (len(ep) - 1) / (len(st) / 252.0), "mean regime days": float(ep["days"].mean()),
                            **{f"% {state}": float((st == state).mean()) for state in (rs.RANGE, rs.TRANSITION, rs.TREND)},
                            "Sharpe at high costs": sharpe(recost(run, result, variant, config.execution.cost_multipliers["high"]).net.loc[span["whole"][0]:span["whole"][1]]),
                            "long contribution": m["long contribution"], "short contribution": m["short contribution"],
                            "Sharpe exploratory": rs.metrics(run, *span["exploratory"]).get("Sharpe"), "Sharpe validation": rs.metrics(run, *span["validation"]).get("Sharpe")}
    baseline_row = {"Sharpe": rows[rs.PRIMARY]["Sharpe"], "static 50/50 Sharpe": rows["6A static 50/50"]["Sharpe"], "annual return": rows[rs.PRIMARY]["annual return"]}
    sensitivity = pd.DataFrame({"baseline": baseline_row, **sens_rows}).T
    section(f"{nd}: sensitivity of the primary (one at a time, then joint)", sensitivity)

    # --- cross-market ---------------------------------------------------------------------------------------------
    cross_rows, pooled = {}, {}
    cross_studies = {nd: study}
    for market in config.data["cross_markets"]:
        result = rs.run_market(config, market, frames[market], rates, full=False)
        cross_studies[market] = result
    for market, result in cross_studies.items():
        span = periods(result, config)["whole"]
        for name in ("1 trend alone", "2A range alone", "3A hard switch", rs.PRIMARY, "6A static 50/50", "9A exposure-matched static", "8 buy and hold (1x)"):
            m = rs.metrics(result.runs[name], *span)
            cross_rows[(market, name)] = {"from": f"{span[0]:%Y-%m-%d}", "Sharpe": m["Sharpe"], "annual return": m["annual return"], "max drawdown": m["max drawdown"], "turnover": m["turnover"]}
            pooled.setdefault(name, {})[market] = result.runs[name].net.loc[span[0]:span[1]]
    section("Cross-market: unchanged rules on every market", pd.DataFrame(cross_rows).T)
    pooled_rows = {name: {"Sharpe": sharpe(pd.DataFrame(series).mean(axis=1)), "annual return": float(pd.DataFrame(series).mean(axis=1).mean() * 252)} for name, series in pooled.items()}
    section("Cross-market: pooled (equal weight across the 7 markets)", pd.DataFrame(pooled_rows).T)

    # --- the strategy card and the classification ------------------------------------------------------------------
    ndx_variants = pd.DataFrame({name: net(name) for name in study.runs if not name.startswith(("8", "R "))})
    sweep = pd.DataFrame(sens_returns)
    card = stc.strategy_card(net(rs.PRIMARY), name=f"H14 {rs.PRIMARY} on {nd}", periods_per_year=252, baseline=net("6A static 50/50"), variants=ndx_variants,
                             sweep=sweep, positions=primary.earning.loc[whole[0]:whole[1]].to_frame(nd), asset_returns=study.returns.loc[whole[0]:whole[1]].fillna(0.0).to_frame(nd), family=HYPOTHESIS,
                             ledger=not args.no_ledger)
    card.save(out / "strategy_card")
    versus_hard = stc.strategy_card(net(rs.PRIMARY), name="5A vs 3A", periods_per_year=252, baseline=net("3A hard switch"), ledger=False)
    tables.extend(["## The strategy card of the primary", "", card.to_markdown(), "", f"Against the hard switch: {versus_hard.versus_baseline} "
                   f"(Sharpe difference {versus_hard.summary().get('minus_baseline', float('nan')):+.2f}; interval in its table).", ""])

    p = lambda name, label: rs.metrics(study.runs[name], *windows[label])["Sharpe"]  # noqa: E731
    best_filter = max(rows[name]["Sharpe"] for name in study.runs if name.startswith("F ") or name.startswith("7A"))
    numbers = card.details["numbers"]
    others = [m for m in config.data["cross_markets"] if m != "SPX"]
    beats = {m: rs.metrics(cross_studies[m].runs[rs.PRIMARY], *periods(cross_studies[m], config)["whole"])["Sharpe"] > rs.metrics(cross_studies[m].runs["6A static 50/50"], *periods(cross_studies[m], config)["whole"])["Sharpe"] for m in cross_studies}
    diff_low = float(card.table.at["Sharpe minus baseline, interval", "value"].split("(")[1].split(" to ")[0])
    criteria = {
        "E1 significant (card weak or better)": card.verdict in ("weak", "evidence of an edge"),
        "E2 beats 6A and 3A in both periods, paired interval vs 6A above 0": all(p(rs.PRIMARY, label) > max(p("6A static 50/50", label), p("3A hard switch", label)) for label in ("exploratory", "validation")) and diff_low > 0,
        "E3 beats exposure-matched 9A in both periods": all(p(rs.PRIMARY, label) > p("9A exposure-matched static", label) for label in ("exploratory", "validation")),
        "E4 at least the best simple filter": rows[rs.PRIMARY]["Sharpe"] >= best_filter,
        "E5 beats 6A on SPX and >= 3 of 5 others, and pooled": beats["SPX"] and sum(beats[m] for m in others) >= 3 and pooled_rows[rs.PRIMARY]["Sharpe"] > pooled_rows["6A static 50/50"]["Sharpe"],
        "E6 >= 80% of sensitivity positive and >= 2/3 above 6A": (sensitivity.loc[list(sens_rows), "annual return"] > 0).mean() >= 0.8 and (sensitivity.loc[list(sens_rows), "Sharpe"] > sensitivity.loc[list(sens_rows), "static 50/50 Sharpe"]).mean() >= 2 / 3,
        "E7 Sharpe without the best year above 0": without["without the best year"] > 0,
    }
    whole_sharpe = rows[rs.PRIMARY]["Sharpe"]
    if whole_sharpe > rows["6A static 50/50"]["Sharpe"] and not criteria["E3 beats exposure-matched 9A in both periods"]:
        outcome = "C Mostly exposure management"
    elif whole_sharpe <= max(rows["6A static 50/50"]["Sharpe"], rows["3A hard switch"]["Sharpe"]) or not criteria["E4 at least the best simple filter"]:
        outcome = "D Not supported"
    elif all(criteria.values()):
        outcome = "A Supported"
    elif criteria["E1 significant (card weak or better)"] and criteria["E2 beats 6A and 3A in both periods, paired interval vs 6A above 0"]:
        outcome = "B Partially supported"
    else:
        outcome = "E Inconclusive"
    section("Classification (pre-registered)", "\n".join([f"- {'pass' if ok else 'FAIL'}: {label}" for label, ok in criteria.items()] + ["", f"**{outcome}**"]))
    (out / "classification.json").write_text(json.dumps({"outcome": outcome, "criteria": criteria, "card": card.summary(), "beats_static_by_market": beats}, indent=2, default=str))
    (out / "tables.md").write_text("\n".join(tables))

    # --- machine-readable outputs ---------------------------------------------------------------------------------
    study.scores.to_csv(out / f"{nd}_features_scores.csv")
    pd.concat({name: frame for name, frame in study.regimes.items() if name in CORE}, axis=1).to_csv(out / f"{nd}_regimes.csv")
    pd.DataFrame({"trend_signal": study.trend["trend_signal"], **{f"range_signal {name}": signal for name, signal in study.range_signals.items() if name in CORE}}).to_csv(out / f"{nd}_model_signals.csv")
    pd.concat({name: frame for name, frame in study.targets.items() if name in CORE}, axis=1).to_csv(out / f"{nd}_targets.csv")
    for kind in ("decided", "earning", "net", "costs"):
        pd.DataFrame({name: getattr(run, kind) for name, run in study.runs.items()}).to_csv(out / f"{nd}_{kind}.csv")
    rs.trade_records(primary).to_csv(out / f"{nd}_primary_trades.csv", index=False)
    pd.DataFrame(rows).T.to_csv(out / f"{nd}_metrics.csv")
    sensitivity.to_csv(out / f"{nd}_sensitivity.csv")
    pd.DataFrame(cross_rows).T.to_csv(out / "cross_market.csv")
    delays.to_csv(out / f"{nd}_breakout_delays.csv", index=False)

    make_charts(study, cross_studies, config, whole, charts, sens_returns)
    trials = len(study.runs) + len(sens_specs) + 2 + sum(len(result.runs) for market, result in cross_studies.items() if market != nd)
    if not args.no_ledger:
        governance.record_trials("h14_study", trials, family=HYPOTHESIS, data="Yahoo index OHLC (7 markets), OECD rates, 1999-2025",
                                 details={"outcome": outcome, "sharpe_net": whole_sharpe, "card": card.verdict})
    print("\n".join(tables[-12:]))
    print(f"\n{trials} trials. Wrote {out}")


# --- charts ----------------------------------------------------------------------------------------------------------

def make_charts(study: rs.MarketStudy, cross: dict[str, rs.MarketStudy], config: rs.Config, whole: tuple[pd.Timestamp, pd.Timestamp], folder: Path,
                sens_returns: dict[str, pd.Series]) -> None:
    """Every chart in section 13.8, from the backtest output."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    a, b = whole
    regime = study.regimes[rs.PRIMARY].loc[a:b]
    close = study.frame["close"].loc[a:b]
    colors = {rs.RANGE: "#4C9AFF", rs.TRANSITION: "#FFC400", rs.TREND: "#36B37E"}

    def save(figure: Any, name: str) -> None:
        figure.tight_layout()
        figure.savefig(folder / f"{name}.png", dpi=110)
        plt.close(figure)

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(close.index, close.to_numpy(), color="black", linewidth=0.8)
    ax.set_yscale("log")
    for state, color in colors.items():
        ax.fill_between(close.index, close.min(), close.max(), where=(regime["state"] == state).to_numpy(), color=color, alpha=0.25, step="post", label=state)
    stress = study.scores["stress"].loc[a:b]
    ax.scatter(close.index[stress.to_numpy()], close[stress].to_numpy(), s=2, color="red", label="STRESS")
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title("Price with the primary overlay's regimes")
    save(fig, "01_price_regimes")

    fig, ax = plt.subplots(figsize=(14, 4))
    ax.plot(regime.index, regime["trend_probability"], linewidth=0.6)
    for level, style in ((config.regime.trend_entry, "--"), (config.regime.range_entry, "--"), (config.regime.exit_threshold, ":")):
        ax.axhline(level, linestyle=style, color="grey")
    ax.set_title("Trend probability with entry (dashed) and exit (dotted) thresholds")
    save(fig, "02_trend_probability")

    fig, axes = plt.subplots(5, 1, figsize=(14, 10), sharex=True)
    for ax, name in zip(axes, config.regime.weights):
        ax.plot(study.scores[f"{name}_score"].loc[a:b], linewidth=0.5)
        ax.set_ylabel(name)
    axes[0].set_title("Regime feature scores (walk-forward percentiles)")
    save(fig, "03_feature_scores")

    fig, axes = plt.subplots(4, 1, figsize=(14, 10), sharex=True)
    axes[0].plot(study.trend["trend_signal"].loc[a:b], linewidth=0.5)
    axes[0].set_title("Trend signal")
    axes[1].plot(study.range_signals["2A range alone"].loc[a:b], linewidth=0.5)
    axes[1].set_title("Range signal (standalone)")
    axes[2].plot(regime["trend_weight"], label="trend weight", linewidth=0.6)
    axes[2].plot(regime["range_weight"], label="range weight", linewidth=0.6)
    axes[2].legend(fontsize=8)
    axes[2].set_title("Model weights")
    axes[3].plot(study.targets[rs.PRIMARY]["target_signal"].loc[a:b], linewidth=0.5)
    axes[3].set_title("Final target signal")
    save(fig, "04_signals_weights_target")

    fig, ax = plt.subplots(figsize=(14, 4))
    ax.plot(study.runs[rs.PRIMARY].earning.loc[a:b], linewidth=0.5)
    ax.set_title("Executed position (multiple of capital)")
    save(fig, "05_exposure")

    names = ["1 trend alone", "2A range alone", "3A hard switch", "4A three-state, hysteresis", rs.PRIMARY, "6A static 50/50", "7A 200-day MA regime filter", "9A exposure-matched static", "8v volatility-managed long"]
    fig, (ax, dd) = plt.subplots(2, 1, figsize=(14, 9), sharex=True)
    for name in names:
        wealth = (1 + study.runs[name].net.loc[a:b]).cumprod()
        ax.plot(wealth, label=name, linewidth=1.4 if name == rs.PRIMARY else 0.8)
        dd.plot(wealth / wealth.cummax() - 1, linewidth=1.4 if name == rs.PRIMARY else 0.6)
    ax.set_yscale("log")
    ax.legend(fontsize=7)
    ax.set_title("Equity curves of the baselines (net)")
    dd.set_title("Drawdowns")
    save(fig, "06_equity_drawdowns")

    fig, axes = plt.subplots(3, 1, figsize=(14, 9), sharex=True)
    for name in (rs.PRIMARY, "6A static 50/50", "1 trend alone"):
        series = study.runs[name].net.loc[a:b]
        axes[0].plot(series.rolling(252).mean() / series.rolling(252).std() * np.sqrt(252), label=name, linewidth=0.7)
        axes[1].plot(series.rolling(252).sum(), linewidth=0.7)
        axes[2].plot(study.runs[name].traded.loc[a:b].rolling(252).sum(), linewidth=0.7)
    axes[0].legend(fontsize=7)
    axes[0].set_title("Rolling one-year Sharpe")
    axes[1].set_title("Rolling one-year return")
    axes[2].set_title("Rolling one-year turnover")
    save(fig, "07_rolling")

    eps = episodes_of(regime["state"])
    fig, ax = plt.subplots(figsize=(8, 4))
    for state, color in colors.items():
        ax.hist(eps.loc[eps["state"] == state, "days"], bins=40, alpha=0.5, label=state, color=color)
    ax.legend()
    ax.set_title("Regime duration (days)")
    save(fig, "08_regime_durations")

    changes = regime.index[regime["state"].ne(regime["state"].shift()).to_numpy()][1:]
    net = study.runs[rs.PRIMARY].net
    paths = [net.loc[day:].iloc[:21].cumsum().to_numpy() for day in changes]
    paths = [path for path in paths if len(path) == 21]
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(np.mean(paths, axis=0) * 1e4)
    ax.set_title("Average cumulative primary P&L after a regime change (bps)")
    save(fig, "09_after_regime_change")

    fig, ax = plt.subplots(figsize=(8, 4))
    for state, color in colors.items():
        ax.hist(net.loc[a:b][(regime["state"] == state).to_numpy()] * 1e4, bins=80, alpha=0.5, label=state, color=color, density=True)
    ax.set_xlim(-200, 200)
    ax.legend()
    ax.set_title("Daily primary return by regime (bps)")
    save(fig, "10_returns_by_regime")

    corr = study.scores[["er", "abs_slope_t", "adx", "vr", "breakout_beyond", "vol_ratio"]].loc[a:b].corr(method="spearman")
    fig, ax = plt.subplots(figsize=(6, 5))
    image = ax.imshow(corr.to_numpy(), vmin=-1, vmax=1, cmap="RdBu")
    ax.set_xticks(range(len(corr)), corr.columns, rotation=45)
    ax.set_yticks(range(len(corr)), corr.index)
    fig.colorbar(image)
    ax.set_title("Feature rank correlation")
    save(fig, "11_feature_correlation")

    fig, ax = plt.subplots(figsize=(8, 4))
    for name in (rs.PRIMARY, "6A static 50/50", "3A hard switch", "1 trend alone", "2A range alone"):
        values = [float(recost(study.runs[name], study, config, multiplier).net.loc[a:b].pipe(sharpe)) for multiplier in config.execution.cost_multipliers.values()]
        ax.plot(list(config.execution.cost_multipliers.values()), values, marker="o", label=name)
    ax.set_xlabel("cost multiplier")
    ax.set_ylabel("net Sharpe")
    ax.legend(fontsize=7)
    ax.set_title("Transaction-cost impact")
    save(fig, "12_cost_impact")

    fig, ax = plt.subplots(figsize=(10, 4))
    labels = [rs.PRIMARY, "1 trend alone", "2A range alone", "2B range alone", "6A static 50/50"]
    long_c = [float(study.runs[n].net.loc[a:b][study.runs[n].earning.loc[a:b] > 0].sum()) for n in labels]
    short_c = [float(study.runs[n].net.loc[a:b][study.runs[n].earning.loc[a:b] < 0].sum()) for n in labels]
    ax.bar(np.arange(len(labels)) - 0.2, long_c, width=0.4, label="long")
    ax.bar(np.arange(len(labels)) + 0.2, short_c, width=0.4, label="short")
    ax.set_xticks(range(len(labels)), labels, rotation=20, fontsize=7)
    ax.legend()
    ax.set_title("Long and short contribution (total, fraction of capital)")
    save(fig, "13_long_short")

    fig, ax = plt.subplots(figsize=(10, 4))
    markets = list(cross)
    primary_s = [sharpe(cross[m].runs[rs.PRIMARY].net.loc[periods(cross[m], config)["whole"][0]:b]) for m in markets]
    static_s = [sharpe(cross[m].runs["6A static 50/50"].net.loc[periods(cross[m], config)["whole"][0]:b]) for m in markets]
    hard_s = [sharpe(cross[m].runs["3A hard switch"].net.loc[periods(cross[m], config)["whole"][0]:b]) for m in markets]
    for offset, values, label in ((-0.25, primary_s, "5A primary"), (0.0, static_s, "6A static"), (0.25, hard_s, "3A hard switch")):
        ax.bar(np.arange(len(markets)) + offset, values, width=0.25, label=label)
    ax.set_xticks(range(len(markets)), markets)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.legend()
    ax.set_title("Cross-market net Sharpe, unchanged rules")
    save(fig, "14_cross_market")

    fig, ax = plt.subplots(figsize=(10, 5))
    for label, series in sens_returns.items():
        ax.plot((1 + series).cumprod(), linewidth=1.5 if label == "baseline" else 0.5)
    ax.set_yscale("log")
    ax.set_title("Primary under every sensitivity setting (net)")
    save(fig, "15_sensitivity_curves")


def recost(run: rs.Run, study: rs.MarketStudy, config: rs.Config, multiplier: float) -> rs.Run:
    """The same positions at another cost multiplier."""
    unit = run.name.startswith("8 buy")
    return rs.backtest(run.name, run.signal, study.returns, config, cost_bps=float(config.markets[study.market]["cost_bps"]), cost_multiplier=multiplier,
                       scale=None if unit else rs.vol_scale(study.returns, config), unit_scale=unit)


if __name__ == "__main__":
    main()
