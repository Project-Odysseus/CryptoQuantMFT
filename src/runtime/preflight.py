"""Preflight: every non-destructive readiness check in one run, with one pass/fail table at the end.

`main.py --preflight` builds the list (config validation, kill switch, database, clock, Kraken Futures public and
read-only private endpoints, Kraken spot validate-only orders, Telegram, the dead-man's switch) and runs it here.
No check places, amends or cancels an order. A check is a function returning a `CheckResult`, or raising, which counts
as a failure; one failing check never stops the others, so a single run lists every problem.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
import urllib.request
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
MAX_CLOCK_SKEW_SECONDS = 2.0
MIN_FREE_DISK_GB = 2.0


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one check: PASS, FAIL or SKIP (not applicable or not configured), with a one-line detail."""

    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Check:
    """A named readiness check."""

    name: str
    run: Callable[[], CheckResult]


def run_checks(checks: Sequence[Check], *, echo: Callable[[str], None] = print) -> list[tuple[str, CheckResult]]:
    """Run every check (a raising check is a FAIL) and print a summary table; returns (name, result) pairs."""
    results = []
    for check in checks:
        echo(f"\n=== {check.name}")
        try:
            result = check.run()
        except SystemExit as exc:  # the older CLI checks exit instead of returning
            result = CheckResult(FAIL if exc.code not in (0, None) else PASS, str(exc.code) if isinstance(exc.code, str) else "")
        except Exception as exc:  # noqa: BLE001 - a broken check is reported, never fatal to the run
            result = CheckResult(FAIL, f"{type(exc).__name__}: {exc}")
        results.append((check.name, result))
    width = max(len(name) for name, _ in results) if results else 0
    echo("\nPreflight summary")
    for name, result in results:
        echo(f"  [{result.status}] {name.ljust(width)}  {result.detail}")
    failed = [name for name, result in results if result.status == FAIL]
    echo(f"\n{'READY' if not failed else 'NOT READY'}: {len(failed)} failed, "
         f"{sum(r.status == PASS for _, r in results)} passed, {sum(r.status == SKIP for _, r in results)} skipped")
    return results


def exit_code(results: Sequence[tuple[str, CheckResult]]) -> int:
    """0 when nothing failed, else 1."""
    return 1 if any(result.status == FAIL for _, result in results) else 0


def check_database(path: str | Path) -> CheckResult:
    """The SQLite database (trades, events, tax ledger) opens and takes a write lock, and the disk has room."""
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=5)) as connection:
        connection.execute("BEGIN IMMEDIATE")  # fails if another process holds a write lock for longer than the timeout
        connection.execute("ROLLBACK")
    free_gb = shutil.disk_usage(database.parent).free / 1e9
    if free_gb < MIN_FREE_DISK_GB:
        return CheckResult(FAIL, f"{database} writable, but only {free_gb:.1f} GB free")
    return CheckResult(PASS, f"{database} writable, {free_gb:.0f} GB free")


def check_clock(*, fetch: Callable[[], float] | None = None, now: Callable[[], float] = time.time) -> CheckResult:
    """This machine's clock against Kraken's server time: orders, candles and funding all key off timestamps."""

    def kraken_time() -> float:
        request = urllib.request.Request("https://api.kraken.com/0/public/Time", headers={"User-Agent": "CryptoQuantMFT/0.1"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return float(json.loads(response.read())["result"]["unixtime"])

    before = now()
    server = (fetch or kraken_time)()
    local = (before + now()) / 2.0  # the middle of the round trip
    skew = local - server
    if abs(skew) > MAX_CLOCK_SKEW_SECONDS:
        return CheckResult(FAIL, f"clock is {skew:+.1f}s off Kraken's; enable time sync (timedatectl set-ntp true)")
    return CheckResult(PASS, f"clock within {abs(skew):.1f}s of Kraken's (Kraken reports whole seconds)")


def check_configured(value: str, *, what: str, why: str) -> CheckResult:
    """PASS if a setting is present, SKIP (with the reason it matters) if not."""
    return CheckResult(PASS, f"{what} is set") if value else CheckResult(SKIP, f"{what} not set: {why}")
