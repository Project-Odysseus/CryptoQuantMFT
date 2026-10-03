"""Network-loss drill for the paper portfolio: cut the runtime's network, kill it, restart it, bring the network back.

    python scripts/drills/network_drill.py [config/portfolio.btc_live.toml] [--interval 5]

It runs the real command (`main.py --runtime paper --portfolio ...`) on real Kraken candles, but in a state folder
and database of its own, with Telegram and the dead-man's switch off. The process reaches the internet only through
a small local proxy that this script starts, so "the network is down" means the proxy refuses connections: the
drill never touches the machine's own network, and nothing else on it notices.

Phases, each checked from the drill's own event log and state files:

1. **Network up.** The runtime starts cold, replays its candle history and decides. No failed cycles.
2. **Network down while running.** It keeps cycling on its last good candles, alerts once about the failing market
   data, sends no orders, and does not stop.
3. **kill -9, then a restart with the network still down.** With no candles at all it can't run a cycle; it must stop
   itself after its failed cycles without touching the checkpoint or the paper exchange.
4. **Network back, restart on the same state.** It resumes from the checkpoint, the book equals the paper exchange,
   nothing was traded twice, and no reconciliation mismatch was logged.

Paper only: no keys are used and no order leaves the machine. Exit code 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


class SwitchableProxy:
    """A local HTTPS (CONNECT) proxy that can be switched off: the drill's network cable."""

    def __init__(self) -> None:
        self.up = True
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def cut(self) -> None:
        """Refuse new connections and drop the open ones."""
        self.up = False
        for writer in list(self._writers):
            writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        try:
            request = (await reader.readline()).split()
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            if not self.up or len(request) < 2 or request[0] != b"CONNECT":
                return
            host, _, port = request[1].decode().rpartition(":")
            upstream_reader, upstream = await asyncio.open_connection(host, int(port))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            self._writers |= {writer, upstream}

            async def pipe(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
                try:
                    while self.up and (data := await source.read(65536)):
                        target.write(data)
                        await target.drain()
                except (ConnectionError, OSError):
                    pass
                finally:
                    target.close()

            await asyncio.gather(pipe(reader, upstream), pipe(upstream_reader, writer))
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            self._writers -= {writer, upstream} if upstream else {writer}
            writer.close()


class Drill:
    def __init__(self, config: str, interval: float, workdir: Path, proxy: SwitchableProxy) -> None:
        self.config, self.interval, self.workdir, self.proxy = config, interval, workdir, proxy
        self.database = workdir / "drill.db"
        self.state = workdir / "state"
        self.checks: list[tuple[str, bool, str]] = []
        self.process: asyncio.subprocess.Process | None = None

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, bool(passed), detail))
        print(f"  {'PASS' if passed else 'FAIL'}  {name}{' (' + detail + ')' if detail else ''}", flush=True)

    async def launch(self) -> None:
        env = {**os.environ, "HTTPS_PROXY": f"http://127.0.0.1:{self.proxy.port}", "HTTP_PROXY": f"http://127.0.0.1:{self.proxy.port}", "NO_PROXY": "",
               "DATABASE_PATH": str(self.database), "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "HEALTHCHECK_URL": ""}
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "main.py", "--runtime", "paper", "--portfolio", self.config, "--portfolio-state-dir", str(self.state),
            "--runtime-iterations", "0", "--runtime-interval", f"{self.interval:g}", cwd=ROOT, env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    def events(self, *types: str, after: int = 0) -> list[tuple[int, str, str]]:
        if not self.database.exists():
            return []
        marks = ", ".join("?" for _ in types)
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(f"SELECT id, event_type, message FROM operational_events WHERE id > ? AND event_type IN ({marks}) ORDER BY id", (after, *types)).fetchall()

    def last_event_id(self) -> int:
        if not self.database.exists():
            return 0
        with closing(sqlite3.connect(self.database)) as connection:
            return int(connection.execute("SELECT COALESCE(MAX(id), 0) FROM operational_events").fetchone()[0])

    def fills(self) -> int:
        if not self.database.exists():
            return 0
        with closing(sqlite3.connect(self.database)) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0])

    def positions(self) -> tuple[dict[str, float], dict[str, float], int]:
        """(book positions, paper exchange positions, the engine's cycle count) from the state files."""
        engine = json.loads((self.state / "engine.json").read_text())
        book = {name: float(position["units"]) for name, position in engine["book"]["positions"].items() if float(position["units"]) != 0.0}
        exchange: dict[str, float] = {}
        for path in self.state.glob("paper_*.json"):
            for symbol, position in json.loads(path.read_text()).get("positions", {}).items():
                if float(position["size"]) != 0.0:
                    exchange[f"{path.stem.removeprefix('paper_')}:{symbol}"] = float(position["size"])
        return book, exchange, int(engine["cycle"])

    async def wait_for(self, what: Any, timeout: float) -> bool:
        waited = 0.0
        while waited < timeout:
            if what():
                return True
            await asyncio.sleep(1.0)
            waited += 1.0
        return bool(what())

    async def run(self) -> None:
        print("1. Network up: a cold start", flush=True)
        await self.launch()
        started = await self.wait_for(lambda: bool(self.events("portfolio_decision")), 180.0)
        self.check("the runtime replays its candles and decides", started)
        if not started:
            return
        await asyncio.sleep(self.interval * 2 + 2)
        self.check("no failed cycles while the network is up", not self.events("portfolio_cycle_error"))
        book, exchange, cycle_up = self.positions()
        self.check("the book equals the paper exchange", book == exchange, f"{book or 'flat'}")
        fills_before, mark = self.fills(), self.last_event_id()

        print("2. Network down while it runs", flush=True)
        self.proxy.cut()
        alerted = await self.wait_for(lambda: any("Market data failed" in message for _id, _type, message in self.events("portfolio_alert", after=mark)), self.interval * 6 + 30)
        self.check("it alerts that market data is failing", alerted)
        await asyncio.sleep(self.interval * 4)
        _book, _exchange, cycle_down = self.positions()
        alerts = [message for _id, _type, message in self.events("portfolio_alert", after=mark) if "Market data failed" in message]
        self.check("it keeps cycling on its last good candles", self.process.returncode is None and cycle_down > cycle_up, f"cycle {cycle_up} -> {cycle_down}")
        self.check("one alert per failing series, not one per cycle", 0 < len(alerts) == len(set(alerts)), f"{len(alerts)} alert(s)")
        self.check("no orders while the network is down", self.fills() == fills_before and not self.events("portfolio_fill", "portfolio_order_rejected", after=mark))

        print("3. kill -9, then a restart with the network still down", flush=True)
        self.process.send_signal(signal.SIGKILL)
        await self.process.wait()
        before = self.positions()[:2]
        mark = self.last_event_id()
        await self.launch()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=self.interval * 8 + 90)
        except asyncio.TimeoutError:
            pass
        stopped = self.process.returncode is not None
        errors = self.events("portfolio_cycle_error", after=mark)
        self.check("without candles it stops itself after its failed cycles", stopped and len(errors) >= 5, f"{len(errors)} failed cycles, exit code {self.process.returncode}")
        if not stopped:
            self.process.send_signal(signal.SIGKILL)
            await self.process.wait()
        self.check("the checkpoint and the paper exchange are untouched", self.positions()[:2] == before and self.fills() == fills_before)

        print("4. Network back: a restart on the same state", flush=True)
        self.proxy.up = True
        mark = self.last_event_id()
        _book, _exchange, cycle_before = self.positions()
        await self.launch()
        resumed = await self.wait_for(lambda: self.positions()[2] >= cycle_before + 3, self.interval * 6 + 120)
        started_events = self.events("portfolio_runtime_started", after=mark)
        self.check("it resumes from the checkpoint and cycles again", resumed and bool(started_events), f"cycle {cycle_before} -> {self.positions()[2]}")
        book, exchange, _cycle = self.positions()
        self.check("the book equals the paper exchange", book == exchange, f"{book or 'flat'}")
        self.check("no failed cycles and no reconciliation mismatch after the restart",
                   not self.events("portfolio_cycle_error", "portfolio_reconciliation_mismatch", after=mark))
        new_bar = bool(self.events("portfolio_decision", after=mark))
        self.check("nothing was traded twice", self.fills() == fills_before or new_bar,
                   "a new bar closed during the drill, so a decision was due" if new_bar and self.fills() != fills_before else f"{self.fills()} fill(s) in total")
        self.process.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(self.process.wait(), timeout=self.interval + 60)
        except asyncio.TimeoutError:
            self.process.send_signal(signal.SIGKILL)
            await self.process.wait()
        self.check("Ctrl-C stops it cleanly", self.process.returncode == 0, f"exit code {self.process.returncode}")


async def main_async(args: argparse.Namespace) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="network-drill-"))
    proxy = SwitchableProxy()
    await proxy.start()
    drill = Drill(args.config, args.interval, workdir, proxy)
    try:
        await drill.run()
    finally:
        if drill.process is not None and drill.process.returncode is None:
            drill.process.send_signal(signal.SIGKILL)
            await drill.process.wait()
    failed = [name for name, passed, _detail in drill.checks if not passed]
    print(f"\n{len(drill.checks) - len(failed)} of {len(drill.checks)} checks passed; state and event log in {workdir}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", nargs="?", default="config/portfolio.btc_live.toml")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between runtime cycles during the drill")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
