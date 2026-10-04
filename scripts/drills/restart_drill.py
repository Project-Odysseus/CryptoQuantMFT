"""Crash and restart drill for the paper portfolio: kill -9 the runtime at chosen moments, restart it, compare.

    python scripts/drills/restart_drill.py [config/portfolio.example.toml] [--bars 240] [--random-kills 6] [--bets]

The runtime writes two things when it trades: the paper exchange's account (`paper_<venue>.json`, at every fill) and
its own checkpoint (`engine.json`). A crash between the two leaves them telling different stories, and what happens
at the restart decides whether an order is sent twice. This drill makes that happen on purpose.

Each scenario runs the real `PortfolioRuntime` and `PortfolioEngine` in a child process on replayed synthetic candles
(no network, a state folder and database of its own), kills it with SIGKILL, restarts it on the same state folder as
often as needed, and lets it finish. Kill points:

- `order_recorded`: the engine has written down that it is about to send an order, and has not sent it;
- `order_sent`: the paper exchange has filled an order, the engine has not booked it yet;
- `fill_logged`: the fill is booked and logged, the checkpoint not yet written;
- `before_checkpoint`: every order of the decision is done, the checkpoint not yet written;
- `torn_checkpoint`: the same, with a half-written temporary checkpoint file left behind;
- `after_checkpoint`: just after the checkpoint (the clean case);
- `random`: SIGKILL from outside at a random time, several times in one run.

A scenario passes when, at the end, the book's positions equal the paper exchange's, no reconciliation mismatch was
ever logged, and the fills and final equity equal those of a run that was never interrupted (so no order was sent
twice and none was lost). Exit code 0 only when every scenario passes.

Paper only. It never touches `data/`, Kraken or Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

HISTORY_DAYS = 120
POINTS = ("order_recorded", "order_sent", "fill_logged", "before_checkpoint", "torn_checkpoint", "after_checkpoint")


# --- the child: one runtime process -----------------------------------------------------------------------------------

class ReplayFeed:
    """Synthetic candles replayed one grid bar per fetch. After a restart it resumes at the moment of the crash.

    The clock of every fetch is written to `clock_path` first, so a restarted process sees the same candles and the
    same time as the cycle that was killed: time never runs backwards across a restart, as it can't in reality.
    (An engine that works on orders between decisions, like resting maker orders with a timeout, depends on that.)
    Without a stored clock it resumes at the engine's last decided bar.
    """

    def __init__(self, mock: Any, resume_from: Any | None, clock_path: Path | None = None) -> None:
        self.mock = mock
        self.clock_path = clock_path
        if clock_path is not None and clock_path.exists():
            self.mock.clock = int(clock_path.read_text())
        elif resume_from is not None:
            # the first poll after a restart sees no new bar, like a real restart within the same bar
            self.mock.clock = int((resume_from - mock.start).total_seconds() // mock.grid_seconds)

    def now(self) -> Any:
        return self.mock.now()

    async def fetch(self, keys: Any, now: Any = None) -> Any:
        from src.portfolio.feed import FeedResult

        if self.clock_path is not None:
            self.clock_path.write_text(str(self.mock.clock))
        result = FeedResult(now=self.mock.now(), bars={key: self.mock._bars(*key) for key in keys})
        self.mock.clock += 1
        return result


def _die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)  # no cleanup, no flush: what `kill -9` or a power cut does


def arm(engine: Any, point: str, count: int) -> None:
    """Make the process kill itself the `count`-th time it reaches `point`."""
    seen = {"n": 0}

    def hit() -> None:
        seen["n"] += 1
        if seen["n"] == count:
            _die()

    if point == "order_recorded":
        original_save = engine._save

        def save() -> None:
            original_save()
            if engine.pending_orders:  # the checkpoint written just before an order goes out
                hit()

        engine._save = save
    elif point == "order_sent":
        for adapter in engine.adapters.values():
            original = adapter.submit_order

            def submit(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
                result = _original(*args, **kwargs)
                if result.status in ("FILLED", "SUBMITTED"):
                    hit()
                return result

            adapter.submit_order = functools.wraps(original)(submit)  # the engine reads the signature to see what the adapter supports
    elif point == "fill_logged":
        original_fill = engine._book_fill

        def book_fill(*args: Any, **kwargs: Any) -> None:
            original_fill(*args, **kwargs)
            hit()

        engine._book_fill = book_fill
    elif point in ("before_checkpoint", "torn_checkpoint"):
        original_log = engine._log_cycle

        def log_cycle(report: Any) -> None:
            original_log(report)
            if report.fills:
                if point == "torn_checkpoint" and seen["n"] + 1 == count:
                    text = json.dumps(engine.to_dict())
                    engine.state_path.with_suffix(engine.state_path.suffix + ".tmp").write_text(text[: len(text) // 2])
                hit()

        engine._log_cycle = log_cycle
    elif point == "after_checkpoint":
        original_cycle = engine.run_cycle

        def run_cycle(*args: Any, **kwargs: Any) -> Any:
            report = original_cycle(*args, **kwargs)
            if report.fills:
                hit()
            return report

        engine.run_cycle = run_cycle
    else:
        raise ValueError(f"unknown crash point {point!r}")


def with_drill_bets(config_path: str, clock: dict[str, Any]) -> tuple[Any, Any, Any]:
    """The config plus one contract sleeve that bets every bar, with a made-up venue that depends only on the bar number.

    Every bar it buys 10 Yes of market A at the offer (a taker order) and bids for 10 Yes of market B below the
    offer (a maker order, which the made-up book fills one bar later), and holds both until they resolve two bars
    on. So a kill can land on a bet recorded but not sent, sent but not booked, resting, or about to settle.
    """
    import tomllib

    import pandas as pd

    from src.portfolio import contracts
    from src.portfolio.config import parse_portfolio_config

    def bar_of(instrument: str) -> int:
        return int(instrument.split("-")[1])

    class DrillBets:
        def targets(self, context: Any) -> list[Any]:
            bar = clock["bar"]()
            keep = [contracts.ContractTarget(instrument, units, 0.5) for instrument, units in context.held.items() if units]
            return keep + [contracts.ContractTarget(f"kalshi:DRILL-{bar}-A", 10, 0.52, reason="drill taker"),
                           contracts.ContractTarget(f"kalshi:DRILL-{bar}-B", 10, 0.50, style="maker", reason="drill maker")]

    def books(instrument: str) -> Any:
        age = clock["bar"]() - bar_of(instrument)
        return pd.DataFrame([("bid", 0.48, 1000.0), ("ask", 0.52 if age <= 0 else 0.50, 1000.0)], columns=["side", "price", "size"])

    def results(instrument: str) -> bool | None:
        return bar_of(instrument) % 2 == 0 if clock["bar"]() - bar_of(instrument) >= 2 else None

    contracts.register("drill_bets", DrillBets)
    raw = tomllib.loads(Path(config_path).read_text())
    raw["contract_sleeves"] = [{"id": "drill_bets", "venue": "kalshi", "strategy": "drill_bets", "budget": 0.05}]
    return parse_portfolio_config(raw, path=config_path), books, results


def child(args: argparse.Namespace) -> int:
    from src.portfolio.book import PortfolioBook
    from src.portfolio.config import load_portfolio_config
    from src.portfolio.engine import PortfolioEngine, build_paper_adapters
    from src.portfolio.feed import MockCandleFeed
    from src.portfolio.runtime import PortfolioRuntime
    from src.runtime.config import BAR_INTERVALS
    from src.storage.trade_logger import TradeLogger

    state_dir = Path(args.state_dir)
    config = load_portfolio_config(args.config)
    bets = os.environ.get("DRILL_BETS") == "1"
    clock = {"bar": lambda: 0}
    books = results = None
    if bets:
        config, books, results = with_drill_bets(args.config, clock)
    book = PortfolioBook.from_config(config)
    logger = TradeLogger(database_path=state_dir / "drill.db")
    engine = PortfolioEngine(config, adapters=build_paper_adapters(config, book, state_dir=state_dir, contract_books=books), book=book, trade_logger=logger,
                             state_path=state_dir / "engine.json", contract_books=books)
    engine.binary_results = results
    per_day = 86400 // BAR_INTERVALS[engine.grid_interval]
    mock = MockCandleFeed(config.instruments, grid_interval=engine.grid_interval, history_days=HISTORY_DAYS, total_days=HISTORY_DAYS + args.bars // per_day + 2)
    if config.baskets:
        engine.basket_source = mock.basket_panel
    feed = ReplayFeed(mock, engine.last_grid_bar, state_dir / "replay_clock")
    clock["bar"] = lambda: mock.clock - 1  # the bar of the cycle now running (the feed has already stepped to the next)
    runtime = PortfolioRuntime(engine, feed, interval_seconds=0, trade_logger=logger)
    if args.crash:
        point, count = args.crash.split(":")
        arm(engine, point, int(count))
    end = HISTORY_DAYS * per_day - 1 + args.bars
    print("READY", flush=True)

    async def loop() -> None:
        while mock.clock <= end:
            if await runtime.run_once() is None:
                raise SystemExit(3)  # a failed cycle is a drill failure, not something to retry past

    asyncio.run(loop())
    return 0


# --- the parent: scenarios and checks ---------------------------------------------------------------------------------

def run_child(config: str, state_dir: Path, bars: int, *, crash: str | None = None, kill_after: float | None = None) -> int:
    """Run one runtime process to its end, to its own crash point, or until it is killed `kill_after` seconds after starting work."""
    command = [sys.executable, str(Path(__file__).resolve()), "--child", "--config", config, "--state-dir", str(state_dir), "--bars", str(bars)]
    if crash:
        command += ["--crash", crash]
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if kill_after is not None:
        process.stdout.readline()  # READY: imports and the checkpoint load are done
        time.sleep(kill_after)
        process.kill()
    _out, err = process.communicate()
    if process.returncode not in (0, -signal.SIGKILL):
        print(err[-2000:], file=sys.stderr)
    return process.returncode


def outcome(state_dir: Path) -> dict[str, Any]:
    """What a finished run left behind: the book, the paper exchange, the fills and any mismatch."""
    engine = json.loads((state_dir / "engine.json").read_text())
    book = {instrument: float(position["units"]) for instrument, position in engine["book"]["positions"].items() if float(position["units"]) != 0.0}
    exchange: dict[str, float] = {}
    for path in state_dir.glob("paper_*.json"):
        venue = path.stem.removeprefix("paper_")
        account = json.loads(path.read_text())
        if account.get("account") == "binary":  # a betting venue: signed Yes contracts per market
            exchange.update({f"{venue}:{market}": float(units) for market, units in account.get("units", {}).items() if float(units) != 0.0})
            continue
        for symbol, position in account.get("positions", {}).items():
            if float(position["size"]) != 0.0:
                exchange[f"{venue}:{symbol}"] = float(position["size"])
    with closing(sqlite3.connect(state_dir / "drill.db")) as connection:
        fills = connection.execute("SELECT timestamp, pair, side, size, price FROM trades WHERE source LIKE 'portfolio%' ORDER BY id").fetchall()
        mismatches = connection.execute("SELECT COUNT(*) FROM operational_events WHERE event_type = 'portfolio_reconciliation_mismatch'").fetchone()[0]
    cash = sum(float(amount) for amount in engine["book"]["cash"].values())
    return {"book": book, "exchange": exchange, "fills": [tuple(row) for row in fills], "mismatches": int(mismatches), "cash": cash,
            # an order still resting at the end (a maker order) is a normal end state; anything else pending is not
            "pending": sorted(order_id for order_id, meta in engine.get("pending_orders", {}).items() if meta.get("style") != "maker"),
            "resting": sorted((meta["instrument"], meta["side"], meta["units"]) for meta in engine.get("pending_orders", {}).values() if meta.get("style") == "maker"),
            "unreconciled": engine.get("unreconciled", {}), "cycle": engine["cycle"]}


def judge(result: dict[str, Any], reference: dict[str, Any]) -> list[str]:
    """Every way `result` differs from a clean run, in plain words (empty = pass)."""
    problems = []
    keys = sorted(set(result["book"]) | set(result["exchange"]))
    apart = {key: (result["book"].get(key, 0.0), result["exchange"].get(key, 0.0)) for key in keys if abs(result["book"].get(key, 0.0) - result["exchange"].get(key, 0.0)) > 1e-9}
    if apart:
        problems.append(f"book and paper exchange disagree: {apart}")
    if result["mismatches"]:
        problems.append(f"{result['mismatches']} reconciliation mismatch event(s)")
    if result["unreconciled"] or result["pending"]:
        problems.append(f"left unreconciled {result['unreconciled']} / pending {result['pending']}")
    if result["resting"] != reference["resting"]:
        problems.append(f"resting orders {result['resting']} vs {reference['resting']} in a clean run")
    if len(result["fills"]) != len(reference["fills"]):
        problems.append(f"{len(result['fills'])} fills logged, a clean run has {len(reference['fills'])}")
    elif result["fills"] != reference["fills"]:
        first = next(index for index, (a, b) in enumerate(zip(result["fills"], reference["fills"])) if a != b)
        problems.append(f"fill {first} differs: {result['fills'][first]} vs {reference['fills'][first]}")
    if result["book"] != reference["book"]:
        problems.append(f"final positions {result['book']} vs {reference['book']} in a clean run")
    if abs(result["cash"] - reference["cash"]) > 1e-6 * max(1.0, abs(reference["cash"])):
        problems.append(f"final cash {result['cash']:.6f} vs {reference['cash']:.6f} in a clean run")
    return problems


def scenario(config: str, bars: int, workdir: Path, name: str, *, crash: str | None = None, random_kills: int = 0, rng: random.Random | None = None,
             duration: float = 5.0) -> tuple[int, dict[str, Any]]:
    """Run one scenario in its own folder; returns (the number of times the process was killed, its outcome)."""
    state_dir = workdir / name.replace(":", "_")
    state_dir.mkdir(parents=True)
    kills = 0
    if crash:
        code = run_child(config, state_dir, bars, crash=crash)
        kills += code == -signal.SIGKILL
    for _ in range(random_kills):
        code = run_child(config, state_dir, bars, kill_after=(rng or random).uniform(0.0, duration))
        if code == 0:
            break  # it finished before the kill came
        kills += 1
    for _ in range(3):
        if run_child(config, state_dir, bars) == 0:
            break
    else:
        raise RuntimeError(f"{name}: the runtime would not finish after its restart (see stderr above)")
    return kills, outcome(state_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config_path", nargs="?", default="config/portfolio.example.toml")
    parser.add_argument("--bars", type=int, default=240, help="Grid bars to trade through after the warmup")
    parser.add_argument("--counts", type=int, nargs="*", default=[1, 3, 6], help="For each kill point: die at its N-th occurrence, one scenario per N")
    parser.add_argument("--points", nargs="*", default=list(POINTS), choices=POINTS, help="Kill points to run (default: all)")
    parser.add_argument("--random-kills", type=int, default=6, help="Scenarios killed from outside at random times (each is killed up to 3 times)")
    parser.add_argument("--seed", type=int, default=20261003)
    parser.add_argument("--keep", action="store_true", help="Keep the state folders (printed at the end)")
    parser.add_argument("--bets", action="store_true", help="Add a contract sleeve that bets every bar on a made-up venue (taker and maker), so kills land on bets too")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", help=argparse.SUPPRESS)
    parser.add_argument("--crash", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child(args)

    workdir = Path(tempfile.mkdtemp(prefix="restart-drill-"))
    rng = random.Random(args.seed)
    started = time.monotonic()
    if args.bets:
        os.environ["DRILL_BETS"] = "1"  # the child processes inherit it
    _kills, reference = scenario(args.config_path, args.bars, workdir, "reference")
    duration = time.monotonic() - started
    print(f"Reference run of {args.config_path}: {args.bars} bars, {len(reference['fills'])} fills, final positions {reference['book']} ({duration:.1f}s)")
    if not reference["fills"]:
        print("The reference run never traded: use more --bars, or the drill proves nothing.")
        return 2
    if problems := judge(reference, reference):
        print(f"The uninterrupted run is already inconsistent: {problems}")
        return 2

    rows = []
    for point in args.points:
        for count in args.counts:
            name = f"{point}:{count}"
            kills, result = scenario(args.config_path, args.bars, workdir, name, crash=name)
            rows.append((name, kills, judge(result, reference) if kills else ["never reached the kill point"]))
    for index in range(args.random_kills):
        kills, result = scenario(args.config_path, args.bars, workdir, f"random_{index + 1}", random_kills=3, rng=rng, duration=max(0.5, duration - 1.0))
        rows.append((f"random #{index + 1}", kills, judge(result, reference)))

    failed = [row for row in rows if row[2] and row[2] != ["never reached the kill point"]]
    skipped = [row for row in rows if row[2] == ["never reached the kill point"]]
    print(f"\n{'scenario':<24} {'kills':>5}  result")
    for name, kills, problems in rows:
        print(f"{name:<24} {kills:>5}  {'PASS' if not problems else 'n/a (' + problems[0] + ')' if problems == ['never reached the kill point'] else 'FAIL: ' + '; '.join(problems)}")
    print(f"\n{len(rows) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} not reached; state folders in {workdir}" if args.keep or failed
          else f"\n{len(rows) - len(skipped)} passed, {len(skipped)} not reached.")
    if not (args.keep or failed):
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
