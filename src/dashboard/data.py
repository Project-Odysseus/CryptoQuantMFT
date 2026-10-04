"""What the dashboard shows, read from the database and the state folders. Read-only: nothing here writes or trades.

Every book the runtime runs writes snapshots to SQLite (`portfolio_snapshots`) on each decision and hourly, and
events (`operational_events`) for fills and alerts. This module turns those into the JSON the web page asks for:
the list of books, one book's latest state, its history as time series, its fills and alerts, and the health of the
background jobs (judged from how recently each one last wrote something).

The database is opened read-only for every request, so a slow page can never hold a lock the runtime needs.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASKET_SEPARATOR = "__"  # a basket's member sleeves are "<basket>__<coin>"; the dashboard shows the basket as one strategy
CONFIRM_DATE = datetime(2026, 11, 17, tzinfo=timezone.utc)


def clean(value: Any) -> Any:
    """`value` with NaN and infinities turned into None, recursively: browsers reject NaN in JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value


def _age_seconds(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    then = datetime.fromisoformat(stamp)
    then = then if then.tzinfo else then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds()


def unit_of(sleeve_id: str) -> str:
    """The strategy a sleeve belongs to on the dashboard: itself, or its basket."""
    return sleeve_id.split(BASKET_SEPARATOR, 1)[0] if BASKET_SEPARATOR in sleeve_id else sleeve_id


class DashboardData:
    """Read-only views of one database and repository folder."""

    def __init__(self, database_path: str | Path, root: str | Path = ".", *, fetch: Any = None, live_max_age: float = 60.0) -> None:
        """`root` is the repository folder (for the state files and collectors' output); `fetch` gets a URL's JSON (Kraken's public tickers)."""
        self.database_path = Path(database_path)
        self.root = Path(root)
        self._fetch = fetch
        self._live_max_age = live_max_age
        self._marks: tuple[float, dict[str, float]] | None = None

    def _live_marks(self) -> tuple[float, dict[str, float]] | None:
        """(fetched at, venue symbol -> Kraken's mark price now), from one public request kept for a minute; None when Kraken can't be reached."""
        if self._marks is not None and time.time() - self._marks[0] <= self._live_max_age:
            return self._marks
        from src.data import kraken_spreads

        try:
            tickers = (self._fetch or kraken_spreads.http_json_gzip)(f"{kraken_spreads.API}/tickers").get("tickers", [])
        except Exception:  # noqa: BLE001 - the page shows the bar-close value without it
            return None
        self._marks = (time.time(), {str(item["symbol"]): float(item["markPrice"]) for item in tickers if item.get("markPrice")})
        return self._marks

    def live(self, name: str) -> dict[str, Any] | None:
        """A book's value at Kraken's prices right now: its last stored equity plus each position's move since the price that equity used.

        A book values itself at its last bar close, so between closes its equity stands still. This adds the move
        since then for display. It reads the book's stored positions and public prices; the book itself is untouched
        and keeps deciding on bar closes. Fees and funding since the snapshot (stored at least hourly) are not in it.
        """
        from src.data.kraken_futures import venue_symbol_for

        snapshot, marks = self.book(name), self._live_marks()
        if snapshot is None or marks is None:
            return None
        rows, moved = {}, 0.0
        for instrument, row in (snapshot.get("instruments") or {}).items():
            venue, _, symbol = instrument.partition(":")
            price = marks[1].get(venue_symbol_for(symbol)) if venue == "kraken_futures" else None
            if price is None or not row.get("price"):
                continue
            change = float(row.get("units") or 0.0) * (price - float(row["price"]))
            moved += change
            rows[instrument] = {"price": price, "move": price / float(row["price"]) - 1.0, "pnl": change}
        equity, initial = float(snapshot["equity"]) + moved, float(snapshot.get("initial_equity") or 0.0)
        return clean({"name": name, "as_of": datetime.fromtimestamp(marks[0], timezone.utc).isoformat(), "snapshot_time": snapshot["timestamp"], "bar_close_equity": snapshot["equity"],
                      "equity": equity, "since_bar_close": moved, "change": equity / initial - 1.0 if initial else None, "instruments": rows})

    def _rows(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        if not self.database_path.exists():
            return []
        with closing(sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True, timeout=5.0)) as connection:
            try:
                return connection.execute(query, params).fetchall()
            except sqlite3.OperationalError:  # a table that doesn't exist yet in a fresh database
                return []

    # --- books ---------------------------------------------------------------------------------------------------

    def books(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Every book with snapshots: its latest equity, exposure and how long ago it last reported."""
        now = now or datetime.now(timezone.utc)
        out = []
        for name, count, first, last in self._rows("SELECT portfolio, COUNT(*), MIN(timestamp), MAX(timestamp) FROM portfolio_snapshots GROUP BY portfolio ORDER BY portfolio"):
            snap = self.book(name) or {}
            initial = float(snap.get("initial_equity") or 0.0)
            age = _age_seconds(last, now)
            out.append({"name": name, "mode": snap.get("mode") or "paper", "snapshots": count, "first": first, "last": last, "age_seconds": age,
                        "reporting": age is not None and age < 2.5 * 3600.0,  # snapshots are written at least hourly while a book runs
                        "equity": snap.get("equity"), "initial_equity": initial, "change": float(snap["equity"]) / initial - 1.0 if initial and snap.get("equity") is not None else None,
                        "drawdown": snap.get("drawdown"), "gross": snap.get("gross"), "net": snap.get("net"),
                        "positions": sum(1 for row in (snap.get("instruments") or {}).values() if row.get("units"))})
        return clean(out)

    def book(self, name: str) -> dict[str, Any] | None:
        """A book's latest snapshot, as the engine wrote it, with its timestamp."""
        rows = self._rows("SELECT timestamp, payload FROM portfolio_snapshots WHERE portfolio = ? ORDER BY id DESC LIMIT 1", (name,))
        if not rows:
            return None
        return clean({"timestamp": rows[0][0], **json.loads(rows[0][1])})

    def history(self, name: str, *, max_points: int = 1500) -> dict[str, Any]:
        """A book's snapshots as time series: equity, drawdown, exposure, risk, the benchmark's price, P&L per strategy.

        More than `max_points` snapshots are thinned evenly (the latest is always kept).
        """
        rows = self._rows("SELECT timestamp, payload FROM portfolio_snapshots WHERE portfolio = ? ORDER BY id", (name,))
        if len(rows) > max_points:
            step = math.ceil(len(rows) / max_points)
            rows = rows[::step] + ([rows[-1]] if (len(rows) - 1) % step else [])
        series: dict[str, list[Any]] = {key: [] for key in ("time", "equity", "drawdown", "gross", "net", "beta", "volatility", "stressed_volatility", "benchmark", "effective_bets")}
        strategies: dict[str, list[float | None]] = {}
        weights: dict[str, list[float | None]] = {}
        benchmark_name = None
        for position, (stamp, payload) in enumerate(rows):
            snap = json.loads(payload)
            exposure = snap.get("exposure") or {}
            instruments = snap.get("instruments") or {}
            benchmark_name = benchmark_name or exposure.get("benchmark") or next((key for key in instruments if "BTC" in key.upper() or "XBT" in key.upper()), None)
            series["time"].append(stamp)
            series["equity"].append(snap.get("equity"))
            series["drawdown"].append(snap.get("drawdown"))
            series["gross"].append(snap.get("gross"))
            series["net"].append(snap.get("net"))
            series["beta"].append(exposure.get("beta_exposure"))
            series["volatility"].append(exposure.get("volatility"))
            series["stressed_volatility"].append(exposure.get("stressed_volatility"))
            series["effective_bets"].append((snap.get("strategy_correlation") or {}).get("effective_bets"))
            series["benchmark"].append((instruments.get(benchmark_name) or {}).get("price") or None if benchmark_name else None)
            totals: dict[str, float] = {}
            for sleeve_id, row in (snap.get("sleeves") or {}).items():
                totals[unit_of(sleeve_id)] = totals.get(unit_of(sleeve_id), 0.0) + float(row.get("pnl") or 0.0)
            for unit in set(strategies) | set(totals):
                strategies.setdefault(unit, [None] * position).append(totals.get(unit))
            for instrument in set(weights) | {key for key, row in instruments.items() if row.get("weight")}:
                weights.setdefault(instrument, [None] * position).append(float((instruments.get(instrument) or {}).get("weight") or 0.0))
        return clean({"name": name, "benchmark": benchmark_name, **series, "strategies": strategies, "weights": weights})

    def fills(self, name: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """A book's fills, newest first. Fills logged before events carried their book's name are matched by their
        exact timestamp to one of this book's decision snapshots (a fill and its snapshot share the cycle's clock)."""
        decision_times = {stamp for (stamp,) in self._rows("SELECT timestamp FROM portfolio_snapshots WHERE portfolio = ?", (name,))}
        out = []
        for stamp, message, metadata in self._rows("SELECT timestamp, message, metadata FROM operational_events WHERE event_type = 'portfolio_fill' ORDER BY id DESC LIMIT 2000"):
            meta = json.loads(metadata) if metadata else {}
            owner = meta.get("portfolio")
            if owner == name or (owner is None and stamp in decision_times):
                out.append({"time": stamp, "instrument": meta.get("instrument"), "side": meta.get("side"), "units": meta.get("units"), "price": meta.get("price"), "fee": meta.get("fee"),
                            "reason": meta.get("reason"), "liquidity": meta.get("liquidity") or "taker", "strategies": sorted({unit_of(key) for key in (meta.get("sleeves") or {})}), "message": message})
                if len(out) >= limit:
                    break
        return clean(out)

    def alerts(self, name: str | None = None, *, limit: int = 60) -> list[dict[str, Any]]:
        """Recent alerts and all-clears, newest first; with `name`, only that book's and those not tied to a book."""
        out = []
        query = "SELECT timestamp, event_type, level, message, metadata FROM operational_events WHERE source = 'portfolio' AND event_type IN ('portfolio_alert', 'portfolio_alert_cleared', 'portfolio_cycle_error', 'portfolio_reconciliation_mismatch') ORDER BY id DESC LIMIT 1000"
        for stamp, event_type, level, message, metadata in self._rows(query):
            owner = (json.loads(metadata) if metadata else {}).get("portfolio")
            if name is None or owner in (None, name):
                out.append({"time": stamp, "kind": "cleared" if event_type.endswith("cleared") else "alert", "level": level, "message": message, "book": owner})
                if len(out) >= limit:
                    break
        return out

    # --- the machine ---------------------------------------------------------------------------------------------

    def _newest(self, folder: Path, pattern: str) -> float | None:
        files = [path.stat().st_mtime for path in folder.glob(pattern) if path.is_file()] if folder.exists() else []
        return max(files) if files else None

    def health(self, *, now: float | None = None) -> list[dict[str, Any]]:
        """Each background job, judged by how long ago it last wrote something: name, what it is, seconds since, and whether that is fine."""
        now = now or time.time()
        jobs: list[tuple[str, str, float | None, float]] = []
        for state in sorted((self.root / "data/portfolio").glob("*/engine.json")) if (self.root / "data/portfolio").exists() else []:
            if not state.parent.name.endswith("-drill"):
                jobs.append((f"book: {state.parent.name}", "the book's checkpoint, written every cycle", state.stat().st_mtime, 20 * 60.0))
        jobs.append(("option chains", "hourly Deribit chains (the H4/H5 studies need them)", self._newest(self.root / "data/options/deribit/BTC", "*/*.parquet"), 2.5 * 3600.0))
        jobs.append(("Kraken spreads", "every perp's bid and ask, each 10 minutes", self._newest(self.root / "data/market_data/kraken_spreads/tickers", "*.csv"), 45 * 60.0))
        jobs.append(("prediction-market books", "Kalshi and Polymarket quotes and depth, each 10 minutes", self._newest(self.root / "data/market_data/prediction_markets/quotes", "*.csv"), 45 * 60.0))
        jobs.append(("research daily job", "stores the H4/H5 data once a day", self._newest(self.root / "logs/research", "daily.log"), 30 * 3600.0))
        return [{"job": job, "what": what, "seconds_since": now - last if last else None, "ok": bool(last) and now - last <= limit, "limit_seconds": limit} for job, what, last, limit in jobs]

    def research(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Where the pre-registered H4/H5 test stands: data stored so far and days until it may be run."""
        now = now or datetime.now(timezone.utc)
        panel = self.root / "data/research/prediction_markets"
        return {"snapshots_cached": len(list((panel / "panel").glob("*.parquet"))) if (panel / "panel").exists() else 0,
                "events_cached": len(list((panel / "kalshi_events").glob("*.json"))) if (panel / "kalshi_events").exists() else 0,
                "confirmatory_run_on": f"{CONFIRM_DATE:%Y-%m-%d}", "days_until": max(0, (CONFIRM_DATE - now).days), "done": (panel / "confirmatory_done.json").exists()}

    def costs(self, config_path: str | Path) -> list[dict[str, Any]]:
        """Kraken's measured cost per coin next to the config's assumed slippage (empty until the collector has run)."""
        from src.data import kraken_spreads
        from src.data.kraken_futures import venue_symbol_for
        from src.portfolio.config import load_portfolio_config

        config = load_portfolio_config(self.root / config_path)
        assumed = {venue_symbol_for(spec.symbol): spec.slippage_bps for spec in config.instruments.values() if spec.venue == "kraken_futures" and spec.kind == "perp"}
        table = kraken_spreads.cost_table(assumed, root=self.root / kraken_spreads.ROOT)
        return clean([{"symbol": symbol, **row} for symbol, row in table.to_dict(orient="index").items()]) if len(table) else []
