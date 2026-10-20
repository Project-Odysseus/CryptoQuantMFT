"""Build config/instruments.toml, the lab's instrument master, from the host's tables (plan 1.1).

    python scripts/lab/build_instrument_master.py            # writes config/instruments.toml
    python scripts/lab/build_instrument_master.py --check    # fails if the file is out of date

Merges, keeping the host's ids so every instrument traces back:
- config/futures_markets.csv: listed futures (contract rules, multipliers, ticks, costs, free quote symbols);
- research/data/multi_asset_markets.csv and research/h14_regime_switch.toml: index and currency research series;
- the crypto perps in config/portfolio.*.toml.

Index series are proxies for the micro future on them (`proxy_for`). The futures runtime still reads
config/futures_markets.csv; tests/test_lab_instrument_master.py keeps the two consistent.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "config" / "instruments.toml"

# index market -> (Yahoo symbol, calendar, timezone, session close, rate area, micro future it stands in for)
INDICES = {
    "SPX": ("^GSPC", "XNYS", "America/New_York", "16:00", "USA", "cme_usd:MES"),
    "NDX": ("^NDX", "XNYS", "America/New_York", "16:00", "USA", "cme_usd:MNQ"),
    "RUT": ("^RUT", "XNYS", "America/New_York", "16:00", "USA", "cme_usd:M2K"),
    "DJI": ("^DJI", "XNYS", "America/New_York", "16:00", "USA", "cme_usd:MYM"),
    "NIKKEI": ("^N225", "XTKS", "Asia/Tokyo", "15:30", "JPN", "cme_jpy:MNI"),
    "DAX": ("^GDAXI", "XETR", "Europe/Berlin", "17:30", "EA20", "eurex_eur:FDXS"),
    "STOXX50": ("^STOXX50E", "XETR", "Europe/Berlin", "17:30", "EA20", "eurex_eur:FSXE"),
    "STOXX600": ("^STOXX", "XETR", "Europe/Berlin", "17:30", "EA20", "eurex_eur:FXXP"),
    "SMI": ("^SSMI", "XSWX", "Europe/Zurich", "17:30", "CHE", "eurex_chf:FSMS"),
    "FTSE": ("^FTSE", "XLON", "Europe/London", "16:30", "GBR", ""),
    "CAC": ("^FCHI", "XPAR", "Europe/Paris", "17:30", "EA20", ""),
    "AEX": ("^AEX", "XAMS", "Europe/Amsterdam", "17:30", "EA20", ""),
    "IBEX": ("^IBEX", "XMAD", "Europe/Madrid", "17:30", "EA20", ""),
    "MIB": ("FTSEMIB.MI", "XMIL", "Europe/Rome", "17:30", "EA20", ""),
    "HSI": ("^HSI", "XHKG", "Asia/Hong_Kong", "16:00", "USA", ""),
    "ASX": ("^AXJO", "XASX", "Australia/Sydney", "16:00", "AUS", ""),
    "TSX": ("^GSPTSE", "XTSE", "America/Toronto", "16:00", "CAN", ""),
}
CURRENCY = {"SPX": "USD", "NDX": "USD", "RUT": "USD", "DJI": "USD", "NIKKEI": "JPY", "DAX": "EUR", "STOXX50": "EUR", "STOXX600": "EUR", "SMI": "CHF",
            "FTSE": "GBP", "CAC": "EUR", "AEX": "EUR", "IBEX": "EUR", "MIB": "EUR", "HSI": "HKD", "ASX": "AUD", "TSX": "CAD"}
# research proxies for futures with no long free history: total-return ETFs (less the US short rate) and FX spot (plus
# the interest differential). id -> (name, asset class, Yahoo symbol, cost bps per side, future, notes)
ETF_PROXIES = {
    "etf:IEI": ("iShares 3-7 Year Treasury (total return)", "rates", "IEI", 1.0, "cme_usd:ZF", "from 2007; duration about 4.5 years, near the 5-year note's"),
    "etf:IEF": ("iShares 7-10 Year Treasury (total return)", "rates", "IEF", 1.0, "cme_usd:ZN", "from 2002; duration about 7.5 years, a little above the 10-year note's"),
    "etf:USO": ("United States Oil Fund (total return)", "commodity", "USO", 2.0, "cme_usd:MCL",
                "from 2006; holds front-month WTI (spread over later months since 2020), so it pays the roll like the future, less a 0.7% fee"),
}
FX_PROXIES = {"EUR": ("EA20", "cme_usd:M6E"), "GBP": ("GBR", "cme_usd:M6B"), "AUD": ("AUS", "cme_usd:M6A"), "CHF": ("CHE", "cme_usd:MSF"),
              "JPY": ("JPN", "cme_usd:MJY"), "CAD": ("CAN", "cme_usd:MCD")}
ASSET_CLASS = {"equity": "equity_index", "fx": "fx", "rates": "rates", "energy": "commodity", "metals": "commodity"}
RATE_AREAS = ["AUS", "CAN", "CHE", "EA20", "GBR", "JPN", "NOR", "NZL", "SWE", "USA"]
COST_BY_CLASS = {"major": 1.5, "minor": 3.0}
KRAKEN_TAKER_BPS = 5.0


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(float(value)) if isinstance(value, float) else str(value)
    if isinstance(value, dict):
        return "{ " + ", ".join(f'"{key}" = {_value(item)}' if not key.isidentifier() else f"{key} = {_value(item)}" for key, item in value.items()) + " }"
    return '"' + str(value).replace('"', '\\"') + '"'


def instruments() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    h14 = tomllib.loads((ROOT / "research" / "h14_regime_switch.toml").read_text())["markets"]
    h11 = {row["market"]: row for row in csv.DictReader((ROOT / "research" / "data" / "multi_asset_markets.csv").open())}

    with (ROOT / "config" / "futures_markets.csv").open() as handle:
        for row in csv.DictReader(handle):
            settlement = "physical" if row["settlement"] in ("physical", "check") else "cash"
            item = {"name": row["name"], "asset_class": ASSET_CLASS[row["sector"]], "kind": "future",
                    "currency": row["currency"], "venue": row["exchange"], "calendar": row["calendar"], "timezone": row["timezone"],
                    "session_close": row["liquid_to"], "multiplier": float(row["multiplier"]), "tick_size": float(row["tick_size"]),
                    "cost_ticks": float(row["cost_ticks_per_side"])}
            if row["free_quote_symbol"]:
                item["sources"] = {"1d": f"yahoo:{row['free_quote_symbol']}"}
            item["contract"] = {"root": row["root"], "months": row["months"], "last_trade_rule": row["last_trade_rule"],
                                "roll_sessions_before": int(row["roll_sessions_before"]), "settlement": settlement}
            item["notes"] = row["spec_status"]
            out[row["market"]] = item

    for market, (symbol, calendar, timezone, close, area, future) in INDICES.items():
        h14_row, h11_row = h14.get(market, {}), h11.get(market, {})
        dividend = h14_row.get("dividend_yield", float(h11_row["dividend_yield"]) if h11_row else 0.0)
        cost = h14_row.get("cost_bps", COST_BY_CLASS.get(h11_row.get("liquid", "major"), 1.5) if h11_row else 1.5)
        item = {"name": f"{market} index", "asset_class": "equity_index", "kind": "index", "currency": CURRENCY[market], "calendar": calendar,
                "timezone": timezone, "session_close": close, "cost_bps": float(cost), "carry_yield": float(dividend), "rate_area": area,
                "sources": {"1d": f"yahoo:{symbol}"}}
        if future and future in out:
            item["proxy_for"] = future
        if market == "DAX":
            item["notes"] = "total-return index: dividends are already in the price (carry_yield 0)"
        out[f"idx:{market}"] = item

    for instrument_id, (name, asset_class, symbol, cost, future, notes) in ETF_PROXIES.items():
        out[instrument_id] = {"name": name, "asset_class": asset_class, "kind": "etf", "currency": "USD", "calendar": "XNYS", "timezone": "America/New_York",
                              "session_close": "16:00", "cost_bps": cost, "rate_area": "USA", "sources": {"1d": f"yahoo_tr:{symbol}"}, "proxy_for": future,
                              "notes": notes}
    for currency, (area, future) in FX_PROXIES.items():
        out[f"fxusd:{currency}"] = {"name": f"{currency} against the US dollar (Norges Bank fixings, with its interest differential)", "asset_class": "fx",
                                    "kind": "spot", "currency": "USD", "calendar": "XOSL", "timezone": "Europe/Oslo", "session_close": "14:15", "cost_bps": 1.0,
                                    "rate_area": "USA", "carry_area": area, "sources": {"1d": f"norges_bank_usd:{currency}"}, "proxy_for": future,
                                    "notes": "free spot quotes have bad ticks (EUR and JPY +17% in 2008-12), so fixings"}

    for market, row in h11.items():
        if row["sector"] != "fx" or market == "NOK":
            continue
        out[f"fx:{market}NOK"] = {"name": f"{market} in NOK (Norges Bank fixing)", "asset_class": "fx", "kind": "fx_rate", "currency": "NOK",
                                  "calendar": "XOSL", "timezone": "Europe/Oslo", "session_close": "14:15", "cost_bps": float(COST_BY_CLASS[row["liquid"]]),
                                  "sources": {"1d": f"norges_bank:{market}"}}
    out["fx:USDNOK"] = {"name": "USD in NOK (Norges Bank fixing)", "asset_class": "fx", "kind": "fx_rate", "currency": "NOK", "calendar": "XOSL",
                        "timezone": "Europe/Oslo", "session_close": "14:15", "cost_bps": 1.5, "sources": {"1d": "norges_bank:USD"}}

    for area in RATE_AREAS:
        out[f"rate:{area}"] = {"name": f"{area} 3-month interbank rate (OECD, monthly)", "asset_class": "rates", "kind": "rate", "currency": "",
                               "calendar": "always", "sources": {"1M": f"oecd:{area}"}, "cost_bps": 0.0}

    perps: dict[str, float] = {}
    for path in sorted((ROOT / "config").glob("portfolio.*.toml")):
        for instrument_id, spec in tomllib.loads(path.read_text()).get("instruments", {}).items():
            if instrument_id.startswith("kraken_futures:") and spec.get("kind", "perp") == "perp":
                perps[instrument_id] = max(perps.get(instrument_id, 0.0), float(spec.get("slippage_bps", 5)))
    for instrument_id, slippage in sorted(perps.items()):
        symbol = instrument_id.split(":", 1)[1]
        out[instrument_id] = {"name": f"{symbol} perpetual (Kraken Futures)", "asset_class": "crypto", "kind": "perp", "currency": "USD", "venue": "Kraken Futures",
                              "calendar": "always", "timezone": "UTC", "session_close": "00:00", "cost_bps": KRAKEN_TAKER_BPS + slippage,
                              "sources": {"1d": f"kraken:{symbol}", "4h": f"kraken:{symbol}"}}
    for instrument in out.values():
        instrument["currency"] = instrument.get("currency") or "USD"
    return out


def render(items: dict[str, dict[str, Any]]) -> str:
    lines = ["# The lab's instrument master (quantlab.data.InstrumentMaster). GENERATED by scripts/lab/build_instrument_master.py",
             "# from config/futures_markets.csv, research/data/multi_asset_markets.csv, research/h14_regime_switch.toml and the",
             "# crypto perps in config/portfolio.*.toml. Edit those sources (or the script's tables) and regenerate.", ""]
    for instrument_id, item in items.items():
        lines.append(f'[instruments."{instrument_id}"]')
        lines += [f"{key} = {_value(value)}" for key, value in item.items()]
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if config/instruments.toml is out of date")
    args = parser.parse_args()
    text = render(instruments())
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            sys.exit(f"{OUT} is out of date: run python scripts/lab/build_instrument_master.py")
        print(f"{OUT} is up to date")
        return
    OUT.write_text(text)
    print(f"Wrote {OUT} ({text.count('[instruments.')} instruments)")


if __name__ == "__main__":
    main()
