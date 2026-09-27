"""Options in the portfolio book: premium at the fill, marks, expiry at intrinsic value, restarts, and tax routing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.portfolio.book import PortfolioBook
from test_portfolio_engine import BTC, FIRST, _config, _engine, _now, bars_until

PUT = "deribit:BTC_USDC-27NOV26-70000-P"
CALL = "deribit:BTC_USDC-27NOV26-90000-C"
EXPIRY = datetime(2026, 11, 27, 8, tzinfo=timezone.utc)


def _book() -> PortfolioBook:
    return PortfolioBook(base_currency="USD", instruments={}, cash={"deribit": Decimal("1000")})


def test_buying_pays_the_premium_writing_receives_it_and_marks_value_the_position() -> None:
    book = _book()
    book.register_option(PUT)
    book.register_option(CALL)
    book.apply_fill(PUT, "buy", 0.05, 700, fee=0.5)  # 35 of premium
    book.apply_fill(CALL, "sell", 0.05, 600, fee=0.5)  # 30 received
    assert book.cash["deribit"] == Decimal("1000") - Decimal("35.5") + Decimal("30") - Decimal("0.5")
    book.mark({PUT: 900, CALL: 400})
    assert book.equity() == Decimal("994") + Decimal("0.05") * 900 - Decimal("0.05") * 400  # long put worth 45, short call owes 20
    assert book.units() == {PUT: Decimal("0.05"), CALL: Decimal("-0.05")}
    with pytest.raises(ValueError):
        book.register_option("kraken:BTC-PUT")


def test_expiry_settles_at_intrinsic_value_and_realizes_against_the_premium() -> None:
    book = _book()
    book.register_option(PUT)
    book.register_option(CALL)
    book.apply_fill(PUT, "buy", 0.1, 700)
    book.apply_fill(CALL, "sell", 0.1, 600)
    assert book.settle_expired_options(EXPIRY - timedelta(minutes=1), {"BTC": 60_000}) == []  # not yet
    settled = {record["instrument"]: record for record in book.settle_expired_options(EXPIRY, {"BTC": 60_000})}
    assert settled[PUT]["intrinsic"] == Decimal("10000") and settled[PUT]["realized"] == Decimal("0.1") * (10_000 - 700)
    assert settled[CALL]["intrinsic"] == Decimal("0") and settled[CALL]["realized"] == Decimal("0.1") * 600  # kept the premium
    assert book.units() == {} and book.cash["deribit"] == Decimal("1000") - 70 + 60 + 1000
    assert book.equity() == Decimal("1990")


def test_option_positions_survive_a_restart() -> None:
    book = _book()
    book.register_option(PUT)
    book.apply_fill(PUT, "buy", 0.05, 700)
    book.mark({PUT: 800})
    restored = PortfolioBook.from_dict(book.to_dict(), instruments={})
    assert restored.units() == {PUT: Decimal("0.05")} and restored.equity() == book.equity()
    assert restored.instruments[PUT].kind == "option" and restored.instruments[PUT].can_short


def test_the_engine_settles_expired_options_and_records_them_like_derivatives(tmp_path) -> None:
    config = _config()
    engine = _engine(config, tmp_path)
    engine.run_cycle(bars_until(FIRST), now=_now(FIRST))
    now = _now(FIRST + 1)
    put = "deribit:BTC_USDC-" + now.strftime("%d%b%y").upper() + "-1000000-P"  # expires 08:00 on the cycle's day, deep in the money
    engine.book.register_option(put)
    engine.book.apply_fill(put, "buy", 0.01, 900_000)
    taxed = []
    engine.record_tax = True
    engine._tax_derivative = lambda instrument, kind, amount, when, metadata: taxed.append((instrument, kind, round(amount, 6)))  # type: ignore[method-assign]
    engine._flush_tax = lambda when: None  # type: ignore[method-assign]
    engine.run_cycle(bars_until(FIRST + 2), now=now + timedelta(hours=8))

    btc = float(engine.book.marks[BTC])
    assert engine.book.units().get(put) is None
    option_records = [record for record in taxed if record[0] == put]  # the cycle's own perp trades are taxed too
    assert option_records == [(put, "REALIZED_PNL", round(0.01 * ((1_000_000 - btc) - 900_000), 6))]  # no delivery fee in paper
    engine._tax_fill(put, "sell", 0.01, 100.0, 0.02, 3.0, now, "o1")
    assert (put, "REALIZED_PNL", 3.0) in taxed and (put, "TRADING_FEE", -0.02) in taxed
