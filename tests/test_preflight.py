"""The preflight runner: every check runs, failures and crashes are reported, and the exit code reflects them."""

from __future__ import annotations

import pytest

from src.runtime.preflight import FAIL, PASS, SKIP, Check, CheckResult, check_clock, check_configured, check_database, exit_code, run_checks


def test_every_check_runs_and_a_crash_or_exit_counts_as_failure() -> None:
    def boom() -> CheckResult:
        raise RuntimeError("no network")

    def old_style_exit() -> CheckResult:
        raise SystemExit("KRAKEN_FUTURES_API_KEY / KRAKEN_FUTURES_SECRET are not set in .env")

    lines: list[str] = []
    results = run_checks([Check("ok", lambda: CheckResult(PASS)), Check("crash", boom), Check("exit", old_style_exit),
                          Check("skip", lambda: CheckResult(SKIP, "not configured"))], echo=lines.append)
    statuses = {name: result.status for name, result in results}
    assert statuses == {"ok": PASS, "crash": FAIL, "exit": FAIL, "skip": SKIP}
    assert "RuntimeError: no network" in results[1][1].detail
    assert exit_code(results) == 1 and any("NOT READY: 2 failed" in line for line in lines)
    assert exit_code([("ok", CheckResult(PASS)), ("skip", CheckResult(SKIP))]) == 0


def test_clock_skew_is_measured_against_the_server() -> None:
    assert check_clock(fetch=lambda: 1000.0, now=lambda: 1000.4).status == PASS
    late = check_clock(fetch=lambda: 1000.0, now=lambda: 1010.0)
    assert late.status == FAIL and "+10.0s" in late.detail


def test_database_check_takes_a_write_lock(tmp_path) -> None:
    assert check_database(tmp_path / "db" / "test.db").status == PASS


def test_configured_setting() -> None:
    assert check_configured("https://hc-ping.com/x", what="HEALTHCHECK_URL", why="").status == PASS
    assert check_configured("", what="HEALTHCHECK_URL", why="nobody is told").status == SKIP
