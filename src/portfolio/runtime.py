"""The portfolio runtime loop: fetch candles, run a portfolio cycle, watch for trouble, repeat until stopped.

Each iteration:

1. checks the kill switch (`python main.py --kill-switch` writes its state
   file from another terminal): when active, every position is flattened
   with reduce-only orders and the loop stops;
2. fetches every needed (instrument, interval) from the candle feed, all
   concurrently;
3. marks as stale any instrument whose fetch failed or whose last bar is
   more than `stale_after_bars` grid bars old, so it can shrink but not grow;
4. runs `PortfolioEngine.run_cycle` (which decides only on a new grid bar);
5. alerts once when a problem starts and once when it clears (stale data, a
   feed failure, a risk limit acting, rejected orders, a cycle error),
   instead of repeating the same alert every minute;
6. writes a portfolio snapshot to SQLite on every decision and hourly
   otherwise, which is what the dashboard reads.

`iterations=0` runs until SIGINT/SIGTERM. The current cycle finishes and
the checkpoint is written before exiting, so a restart resumes cleanly.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.portfolio.engine import CycleReport, PortfolioEngine
from src.risk.kill_switch import KillSwitchController
from src.runtime.config import BAR_INTERVALS
from src.utils.logger import logger

CYCLE_ERROR_LIMIT = 5  # consecutive failed cycles before the runtime stops itself
SNAPSHOT_EVERY = timedelta(hours=1)
LIMIT_RULES = {"instrument_cap", "venue_cap", "net_cap", "gross_cap", "notional_cap", "drawdown_derisk", "max_drawdown_halt", "daily_loss_halt", "stale_instrument"}


class PortfolioRuntime:
    """Drives a `PortfolioEngine` from a candle feed, with alerts, snapshots, the kill switch and a clean shutdown."""

    def __init__(
        self,
        engine: PortfolioEngine,
        feed: Any,
        *,
        interval_seconds: float = 60.0,
        trade_logger: Any | None = None,
        notifier: Any | None = None,
        kill_switch_file: str | Path | None = None,
    ) -> None:
        """`feed` needs `now()` and `async fetch(keys, now)` (see `src/portfolio/feed.py`)."""
        self.engine = engine
        self.feed = feed
        self.interval_seconds = interval_seconds
        self.trade_logger = trade_logger
        self.notifier = notifier
        self.kill_switch_file = kill_switch_file
        grid = engine.grid_interval
        self.keys = sorted({(spec.instrument, spec.interval) for spec in engine.sleeves.values()} | {(spec.instrument, grid) for spec in engine.sleeves.values()})
        self.grid_step = timedelta(seconds=BAR_INTERVALS[grid])
        self.stop_requested = False
        self.stop_reason: str | None = None
        self.consecutive_errors = 0
        self.reports: list[CycleReport] = []
        self._active_alerts: dict[str, str] = {}
        self._last_snapshot: datetime | None = None
        self._stop_event: asyncio.Event | None = None

    # --- the loop ------------------------------------------------------------------------------------------------

    async def run(self, iterations: int = 0) -> list[CycleReport]:
        """Run `iterations` cycles (0 = until a stop signal, the kill switch, or repeated errors)."""
        if iterations < 0:
            raise ValueError("iterations must be 0 (run until stopped) or more")
        self._stop_event = asyncio.Event()
        self._install_signal_handlers()
        self._event("INFO", "portfolio_runtime_started", f"portfolio {self.engine.config.name} started ({self.engine.mode})",
                    {"iterations": iterations, "interval_seconds": self.interval_seconds, "restored": self.engine.restored})
        count = 0
        while not self.stop_requested and (iterations == 0 or count < iterations):
            await self.run_once()
            count += 1
            if self.stop_requested or (iterations and count >= iterations):
                break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
        self._event("INFO", "portfolio_runtime_stopped", f"portfolio {self.engine.config.name} stopped after {count} cycles: {self.stop_reason or 'done'}",
                    {"cycles": count, "reason": self.stop_reason}, self.feed.now())
        return self.reports

    def request_stop(self, reason: str) -> None:
        """Finish the current cycle, then stop."""
        self.stop_requested = True
        self.stop_reason = self.stop_reason or reason
        if self._stop_event is not None:
            self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for name in ("SIGINT", "SIGTERM"):
            try:
                loop.add_signal_handler(getattr(signal, name), self.request_stop, f"received {name}")
            except (NotImplementedError, RuntimeError, ValueError):  # not the main thread (tests) or not supported
                pass

    async def run_once(self) -> CycleReport | None:
        """One iteration; never raises (a failure is logged, alerted and counted)."""
        now = self.feed.now()
        if self._kill_switch_active():
            report = self.engine.flatten(now=now, reason="kill switch active")
            self.reports.append(report)
            self._alert("kill_switch", f"Kill switch active: flattened {len(report.fills)} positions and stopped.", {"rejected": len(report.rejected)})
            self._snapshot(report, now, force=True)
            self.request_stop("kill switch")
            return report
        try:
            result = await self.feed.fetch(self.keys, now)
            missing = [key for key in self.keys if key not in result.bars]
            if missing:
                raise RuntimeError(f"no candles yet for {missing}")
            stale = self._stale(result, now)
            report = self.engine.run_cycle(result.bars, now=result.now, stale=stale)
        except Exception as exc:  # noqa: BLE001 - the loop must survive a bad cycle
            self.consecutive_errors += 1
            logger.exception("portfolio cycle failed")
            self._event("ERROR", "portfolio_cycle_error", f"cycle failed ({self.consecutive_errors} in a row): {type(exc).__name__}: {exc}", {}, now)
            self._alert("cycle_error", f"Portfolio cycle failed: {type(exc).__name__}: {exc}", {"in_a_row": self.consecutive_errors})
            if self.consecutive_errors >= CYCLE_ERROR_LIMIT:
                self.request_stop(f"{self.consecutive_errors} failed cycles in a row")
            return None
        self.consecutive_errors = 0
        self._clear("cycle_error")
        self.reports.append(report)
        del self.reports[:-200]
        self._watch(report, result.failed, stale)
        self._snapshot(report, result.now, force=report.decided)
        return report

    # --- checks and alerts ---------------------------------------------------------------------------------------

    def _kill_switch_active(self) -> bool:
        if self.kill_switch_file is None:
            return False
        return KillSwitchController(state_file=self.kill_switch_file).is_active()

    def _stale(self, result: Any, now: datetime) -> list[str]:
        limit = self.grid_step * self.engine.config.risk.stale_after_bars
        stale = {key[0] for key in result.failed}
        for (instrument, interval), bars in result.bars.items():
            if interval == self.engine.grid_interval and bars:
                closed = bars[-1].timestamp + self.grid_step
                if now - closed > limit:
                    stale.add(instrument)
        return sorted(stale)

    def _watch(self, report: CycleReport, failed: dict[tuple[str, str], str], stale: list[str]) -> None:
        for key, error in failed.items():
            self._alert(f"market_data:{key[0]}:{key[1]}", f"Market data failed for {key[0]} {key[1]}: {error}", {"instrument": key[0], "interval": key[1]})
        for key in [name for name in self._active_alerts if name.startswith("market_data:")]:
            instrument, interval = key.removeprefix("market_data:").rsplit(":", 1)  # instrument ids contain ":" themselves
            if (instrument, interval) not in failed:
                self._clear(key)
        for instrument in stale:
            self._alert(f"stale_data:{instrument}", f"{instrument} has no fresh bar: it can only shrink until data returns.", {"instrument": instrument})
        for key in [name for name in self._active_alerts if name.startswith("stale_data:")]:
            if key.split(":", 1)[1] not in stale:
                self._clear(key)
        if report.decided:
            acting = {f"risk_limit:{action.rule}" for action in report.risk_actions if action.rule in LIMIT_RULES}
            for action in report.risk_actions:
                if action.rule in LIMIT_RULES:
                    self._alert(f"risk_limit:{action.rule}", f"Risk limit {action.rule} is acting: {action.instrument} {action.before:+.1%} -> {action.after:+.1%}",
                                {"instrument": action.instrument})
            for key in [name for name in self._active_alerts if name.startswith("risk_limit:") and name not in acting]:
                self._clear(key)
        for rejection in report.rejected:  # one message per rejection, never "active"
            key = f"order_rejected:{rejection['order_id']}"
            self._alert(key, f"Order rejected: {rejection['side']} {rejection['units']} {rejection['instrument']}: {rejection['message']}", rejection)
            self._active_alerts.pop(key, None)

    def _alert(self, key: str, message: str, metadata: dict[str, Any]) -> None:
        """Send an alert once per problem; `_clear` sends the all-clear and lets it fire again later."""
        if key in self._active_alerts:
            return
        self._active_alerts[key] = message
        self._event("WARNING", "portfolio_alert", message, {"alert": key, **metadata}, self.feed.now())
        if self.notifier is not None:
            self.notifier.send_alert(event_type=key.split(":", 1)[0], message=message, metadata=metadata)

    def _clear(self, key: str) -> None:
        message = self._active_alerts.pop(key, None)
        if message is None or key.startswith("order_rejected:"):
            return
        self._event("INFO", "portfolio_alert_cleared", f"resolved: {message}", {"alert": key}, self.feed.now())
        if self.notifier is not None:
            self.notifier.send_alert(event_type=f"{key.split(':', 1)[0]}_resolved", message=f"Resolved: {message}", metadata={})

    def _snapshot(self, report: CycleReport, now: datetime, *, force: bool) -> None:
        if self.trade_logger is None:
            return
        if not force and self._last_snapshot is not None and now - self._last_snapshot < SNAPSHOT_EVERY:
            return
        self.trade_logger.log_portfolio_snapshot(timestamp=now, snapshot=self.engine.snapshot(report))
        self._last_snapshot = now

    def _event(self, level: str, event_type: str, message: str, metadata: dict[str, Any], now: datetime | None = None) -> None:
        if self.trade_logger is not None:
            self.trade_logger.log_event(timestamp=now or self.feed.now(), level=level, event_type=event_type, message=message, source="portfolio", metadata=metadata)
