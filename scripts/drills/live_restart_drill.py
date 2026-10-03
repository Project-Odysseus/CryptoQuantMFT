"""Crash and restart drill against the REAL Kraken Futures account, at the book's minimum size.

    python scripts/drills/live_restart_drill.py config/portfolio.btc_live.toml --confirm ENABLE_LIVE_TRADING

THIS PLACES REAL ORDERS. It starts the live portfolio through `main.py` (so every live gate applies), kills the
process with SIGKILL the moment Kraken has accepted its first order, restarts it on the same state folder, and
checks that the order was not sent twice and that the book equals the account. Then it closes the position again
through the engine (reduce-only), so the account ends as it started and the trade log and tax ledger hold both
fills.

Steps and checks:

0. Read-only: the account holds no position in the book's contracts and no resting order, and the kill switch is
   inactive. The drill uses its own state folder (`data/portfolio/<name>-drill`), never the live book's.
1. Start live. The first cycle adopts the account and decides. When `sendorder` returns, the process kills itself:
   no fill booked, no stop placed, no end-of-cycle checkpoint. If no order is due at this bar, it stops cleanly and
   says so; nothing was sent.
2. Read-only: Kraken shows the position, and the checkpoint written just before the send names the order.
3. Restart live for a few cycles. Checks: one order on Kraken under this book's client ids, the fill booked once
   (trade log), the book equal to Kraken's position, the protective stop resting, no reconciliation mismatch.
4. Close the position through the engine and check the account is flat with no order left.

Use `--keep-position` to skip step 4 (the position then stays open with its exchange stop and no process running).
Exit code 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CONFIRMATION = "ENABLE_LIVE_TRADING"
STOP_TAG_MARK = "-stop-"


def live_argv(config: str, state_dir: Path, *, interval: float, iterations: int) -> list[str]:
    return ["main.py", "--runtime", "live", "--portfolio", config, "--portfolio-state-dir", str(state_dir), "--enable-live-trading",
            "--live-confirmation", CONFIRMATION, "--runtime-iterations", str(iterations), "--runtime-interval", f"{interval:g}"]


def child_crash(args: argparse.Namespace) -> int:
    """The live runtime, armed to die the moment Kraken has accepted its first order."""
    from src.execution.kraken_futures_cross import KrakenFuturesCrossMarginAdapter

    original = KrakenFuturesCrossMarginAdapter.submit_order

    def submit(self: Any, **kwargs: Any) -> Any:
        result = original(self, **kwargs)
        if result.status == "SUBMITTED":
            os.kill(os.getpid(), signal.SIGKILL)  # Kraken has the order; this process never learns what became of it
        return result

    KrakenFuturesCrossMarginAdapter.submit_order = submit  # type: ignore[method-assign]
    import main

    sys.argv = live_argv(args.config, Path(args.state_dir), interval=args.interval, iterations=args.cycles)
    main.main()
    return 0


class Drill:
    def __init__(self, config_path: str, state_dir: Path, interval: float) -> None:
        from config import settings
        from src.portfolio.config import load_portfolio_config

        self.config_path, self.state_dir, self.interval = config_path, state_dir, interval
        self.config = load_portfolio_config(config_path)
        self.database = Path(settings.database_path)
        self.checks: list[tuple[str, bool, str]] = []
        self.started = datetime.now(timezone.utc)

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append((name, bool(passed), detail))
        print(f"  {'PASS' if passed else 'FAIL'}  {name}{' (' + detail + ')' if detail else ''}", flush=True)
        return bool(passed)

    def account(self) -> dict[str, Any]:
        """Kraken's own view, read-only: positions, equity, open orders and fills."""
        import main

        _config, adapter = main.build_live_portfolio_adapter(self.config)
        synced = adapter.sync_account()
        open_orders = adapter._client.call("GET", "openorders").get("openOrders", [])
        fills = adapter._client.call("GET", "fills").get("fills", [])
        return {"positions": {symbol: size for symbol, size in synced["positions"].items() if size}, "equity": synced["equity"],
                "foreign": synced["foreign_positions"], "open_orders": open_orders, "fills": fills}

    def checkpoint(self) -> dict[str, Any]:
        return json.loads((self.state_dir / "engine.json").read_text())

    def book(self) -> dict[str, float]:
        positions = self.checkpoint()["book"]["positions"]
        return {name.split(":", 1)[1]: float(position["units"]) for name, position in positions.items() if float(position["units"]) != 0.0}

    def rows(self, query: str, *params: Any) -> list[tuple]:
        with closing(sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)) as connection:
            return connection.execute(query, params).fetchall()

    def run_live(self, *, crash: bool, cycles: int) -> int:
        if crash:
            command = [sys.executable, str(Path(__file__).resolve()), "--child-crash", "--config", self.config_path, "--state-dir", str(self.state_dir),
                       "--interval", f"{self.interval:g}", "--cycles", str(cycles)]
        else:
            command = [sys.executable, *live_argv(self.config_path, self.state_dir, interval=self.interval, iterations=cycles)]
        process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=cycles * self.interval + 600)
        tail = "\n".join((process.stdout or "").strip().splitlines()[-4:])
        if tail:
            print("    " + tail.replace("\n", "\n    "), flush=True)
        if process.returncode not in (0, -signal.SIGKILL):
            print((process.stderr or "")[-1500:], file=sys.stderr)
        return process.returncode

    def run(self, *, keep_position: bool) -> None:
        from src.risk.kill_switch import KillSwitchController

        import main

        print("0. Before: read-only checks", flush=True)
        before = self.account()
        self.check("the account holds no position in the book's contracts", not before["positions"], f"equity {before['equity']:.4f}, other positions {before['foreign'] or 'none'}")
        self.check("no order is resting on Kraken Futures", not before["open_orders"], f"{len(before['open_orders'])} open")
        self.check("the kill switch is inactive", not KillSwitchController(state_file=main.PORTFOLIO_KILL_SWITCH_FILE).is_active())
        self.check("the drill's state folder is new", not self.state_dir.exists(), str(self.state_dir))
        if not all(passed for _name, passed, _detail in self.checks):
            return

        print("1. Live start, killed with SIGKILL the moment Kraken accepts the first order", flush=True)
        code = self.run_live(crash=True, cycles=3)
        if code != -signal.SIGKILL:
            after = self.account()
            self.check("no order was due at this bar, so nothing was sent", code == 0 and not after["positions"] and len(after["fills"]) == len(before["fills"]),
                       f"exit code {code}; run the drill again when the book wants a position")
            return
        self.check("the process died by SIGKILL right after the order went out", True)

        print("2. While it is dead: what Kraken and the checkpoint say", flush=True)
        saved = self.checkpoint()
        pending = saved.get("pending_orders", {})
        prefix = f"cqm-{saved['book_id']}"
        self.check("the checkpoint names the order it was about to send", len(pending) == 1, ", ".join(pending) or "none")
        dead = self.account()
        time.sleep(2.0)
        own_fills = [fill for fill in self.account()["fills"] if str(fill.get("cliOrdId") or "").startswith(prefix) and STOP_TAG_MARK not in str(fill.get("cliOrdId"))]
        self.check("Kraken filled it", bool(dead["positions"]) and bool(own_fills), f"position {dead['positions']}, {len(own_fills)} fill(s)")
        self.check("the engine had not booked it", self.book() == {}, f"book {self.book() or 'flat'}")
        self.check("no protective stop was placed yet", not dead["open_orders"])
        fills_logged = len(self.rows("SELECT id FROM trades WHERE source = 'portfolio_live' AND timestamp >= ?", self.started.isoformat()))

        print("3. Restart on the same state", flush=True)
        code = self.run_live(crash=False, cycles=3)
        self.check("the restarted runtime ran its cycles and stopped cleanly", code == 0, f"exit code {code}")
        alive = self.account()
        orders = {str(fill.get("cliOrdId")) for fill in alive["fills"] if str(fill.get("cliOrdId") or "").startswith(prefix) and STOP_TAG_MARK not in str(fill.get("cliOrdId"))}
        self.check("one order on Kraken under this book's ids: nothing was sent twice", len(orders) == 1, ", ".join(sorted(orders)))
        self.check("Kraken's position is unchanged by the restart", alive["positions"] == dead["positions"], f"{alive['positions']}")
        self.check("the book equals Kraken's position", self.book() == alive["positions"], f"book {self.book()}")
        logged = self.rows("SELECT side, size, price FROM trades WHERE source = 'portfolio_live' AND timestamp >= ?", self.started.isoformat())
        self.check("the fill is in the trade log once", len(logged) == fills_logged + 1, f"{logged}")
        restored = self.checkpoint()
        self.check("nothing is left pending or unreconciled", not restored.get("pending_orders") and not restored.get("unreconciled") and not restored.get("open_plan"))
        stops = [order for order in alive["open_orders"] if STOP_TAG_MARK in str(order.get("cliOrdId") or "")]
        expects_stop = self.config.risk.exchange_stop_pct is not None
        self.check("the protective stop is resting on Kraken" if expects_stop else "no stop is configured, none placed", len(stops) == (1 if expects_stop else 0),
                   ", ".join(f"{order.get('side')} {order.get('unfilledSize') or order.get('size')} at {order.get('stopPrice')}" for order in stops))
        mismatches = self.rows("SELECT message FROM operational_events WHERE event_type = 'portfolio_reconciliation_mismatch' AND timestamp >= ?", self.started.isoformat())
        self.check("no reconciliation mismatch was logged", not mismatches, f"{len(mismatches)}")

        if keep_position:
            print("4. Skipped (--keep-position): the position stays open with its exchange stop and no process running", flush=True)
            return
        print("4. Close the position through the engine", flush=True)
        if self.book() != alive["positions"]:
            self.check("the position was closed", False, "the book and Kraken disagree, so the engine can't close it: run `python main.py --kill-switch`")
            return
        report = self.flatten()
        final = self.account()
        self.check("the account is flat again and no order is resting", not final["positions"] and not final["open_orders"],
                   f"{len(report.fills)} closing fill(s), {len(report.rejected)} rejected")
        print(f"    Kraken equity {before['equity']:.4f} -> {final['equity']:.4f} ({final['equity'] - before['equity']:+.4f}: the round trip's price move and fees)", flush=True)

    def flatten(self) -> Any:
        """Close everything with reduce-only orders through the engine, so the book, the trade log and the tax ledger record it."""
        import main
        from config import settings
        from src.portfolio.book import PortfolioBook
        from src.portfolio.engine import PortfolioEngine
        from src.storage.trade_logger import TradeLogger
        from src.utils.telegram import TelegramNotifier

        config, adapter = main.build_live_portfolio_adapter(self.config)
        engine = PortfolioEngine(config, adapters={"kraken_futures": adapter}, book=PortfolioBook.from_config(config),
                                 trade_logger=TradeLogger(database_path=settings.database_path), notifier=TelegramNotifier(), mode="live",
                                 state_path=self.state_dir / "engine.json", record_tax=True)
        adapter.sync_account()
        now = datetime.now(timezone.utc)
        report = engine.flatten(now=now, reason="live restart drill finished")
        for _attempt in range(20):  # Kraken's fill list can lag a filled order by a few seconds: wait for it, never resend
            if not engine.pending_orders:
                break
            time.sleep(1.5)
            engine._settle_pending(report, datetime.now(timezone.utc))
        engine._flush_tax(datetime.now(timezone.utc))
        engine._save()
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config_path", nargs="?", default="config/portfolio.btc_live.toml")
    parser.add_argument("--confirm", default="", help=f"Must be {CONFIRMATION}: this drill places real orders")
    parser.add_argument("--interval", type=float, default=20.0, help="Seconds between runtime cycles during the drill")
    parser.add_argument("--keep-position", action="store_true", help="Leave the position open at the end instead of closing it")
    parser.add_argument("--child-crash", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", help=argparse.SUPPRESS)
    parser.add_argument("--cycles", type=int, default=3, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_crash:
        return child_crash(args)
    if args.confirm != CONFIRMATION:
        print(f"This drill places real orders on Kraken Futures. Pass --confirm {CONFIRMATION} to run it.")
        return 2
    os.chdir(ROOT)
    from src.portfolio.config import load_portfolio_config

    state_dir = Path("data/portfolio") / f"{load_portfolio_config(args.config_path).name}-drill"
    drill = Drill(args.config_path, state_dir, args.interval)
    drill.run(keep_position=args.keep_position)
    failed = [name for name, passed, _detail in drill.checks if not passed]
    print(f"\n{len(drill.checks) - len(failed)} of {len(drill.checks)} checks passed; state in {state_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
