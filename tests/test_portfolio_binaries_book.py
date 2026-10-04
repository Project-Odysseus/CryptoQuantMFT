"""Prediction-market contracts in the portfolio book: Yes and No positions, marks, settlement, restarts, and the engine's hook."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from src.portfolio.book import PortfolioBook
from test_portfolio_engine import FIRST, _config, _engine, _now, bars_until

ABOVE = "kalshi:KXBTCD-26OCT0416-T85199.99"
UPDOWN = "polymarket:btc-updown-5m-1791140700"


def _book() -> PortfolioBook:
    return PortfolioBook(base_currency="USD", instruments={}, cash={"kalshi": Decimal("100"), "polymarket": Decimal("100")})


def test_yes_is_bought_at_its_price_and_no_is_held_as_negative_yes_with_the_same_value() -> None:
    book = _book()
    book.register_binary(ABOVE)
    book.register_binary(UPDOWN)
    book.apply_fill(ABOVE, "buy", 10, 0.60, fee=0.17)  # 10 Yes at 60 cents
    book.apply_fill(UPDOWN, "sell", 20, 0.45, fee=0.35)  # 20 No, bought at 55 cents each: held as -20 Yes sold at 45
    assert book.cash["kalshi"] == Decimal("100") - 6 - Decimal("0.17") and book.units() == {ABOVE: Decimal(10), UPDOWN: Decimal(-20)}
    # the venue took 20 x 0.55 = 11 for the No contracts; the book shows +9 and reports the 20 the venue holds as their stake
    assert book.cash["polymarket"] == Decimal("100") + 9 - Decimal("0.35") and book.binary_stake("polymarket") == 20 and book.binary_stake("kalshi") == 0
    assert book.cash["polymarket"] - book.binary_stake("polymarket") == Decimal("100") - 11 - Decimal("0.35")  # the cash that can actually be spent
    book.mark({ABOVE: 0.70, UPDOWN: 0.40})
    assert book.equity() == Decimal("200") - Decimal("0.52") + 10 * Decimal("0.10") + 20 * Decimal("0.05")  # Yes up 10 cents, No up 5 cents
    with pytest.raises(ValueError, match="between 0 and 1"):
        book.apply_fill(ABOVE, "buy", 1, 1.5)
    with pytest.raises(ValueError, match="binary ids"):
        book.register_binary("kraken:BTC-above")


def test_a_resolved_market_pays_one_or_nothing_and_realizes_against_the_price_paid() -> None:
    book = _book()
    for instrument in (ABOVE, UPDOWN):
        book.register_binary(instrument)
    book.apply_fill(ABOVE, "buy", 10, 0.60)
    book.apply_fill(UPDOWN, "sell", 20, 0.45)
    assert book.settle_resolved_binaries({}) == [] and book.units()[ABOVE] == 10  # nothing has resolved
    settled = {record["instrument"]: record for record in book.settle_resolved_binaries({ABOVE: True, UPDOWN: False, "kalshi:not-held": True})}
    assert settled[ABOVE]["realized"] == Decimal("4.0") and settled[UPDOWN]["realized"] == Decimal("9.00")  # Yes paid 1 for 0.60; the No side kept its 0.45
    assert book.units() == {} and book.cash == {"kalshi": Decimal("104"), "polymarket": Decimal("109")} and book.equity() == Decimal("213")
    assert book.binary_stake("polymarket") == 0

    lost = _book()
    lost.register_binary(UPDOWN)
    lost.apply_fill(UPDOWN, "sell", 20, 0.45)
    [record] = lost.settle_resolved_binaries({UPDOWN: True})  # Yes won: the No position pays 1 a contract
    assert record["realized"] == Decimal("-11.00") and lost.cash["polymarket"] == Decimal("89")


def test_binary_positions_survive_a_restart() -> None:
    book = _book()
    book.register_binary(ABOVE)
    book.apply_fill(ABOVE, "buy", 10, 0.60)
    book.mark({ABOVE: 0.75})
    restored = PortfolioBook.from_dict(book.to_dict(), instruments={})
    assert restored.units() == {ABOVE: Decimal(10)} and restored.equity() == book.equity() and restored.instruments[ABOVE].kind == "binary"


def test_the_engine_settles_a_resolved_binary_once_and_leaves_its_other_checks_working(tmp_path) -> None:
    engine = _engine(_config(), tmp_path)
    engine.run_cycle(bars_until(FIRST), now=_now(FIRST))
    engine.book.register_binary(ABOVE)
    engine.book.apply_fill(ABOVE, "buy", 10, 0.60)
    engine.book.mark({ABOVE: 0.60})
    equity = float(engine.book.equity())
    asked, answers = [], {}
    engine.binary_results = lambda instrument: (asked.append(instrument), answers.get(instrument))[1]
    taxed = []
    engine.record_tax = True
    engine._tax_derivative = lambda instrument, kind, amount, when, metadata: taxed.append((instrument, kind, round(amount, 6), metadata["kind"]))  # type: ignore[method-assign]
    engine._flush_tax = lambda when: None  # type: ignore[method-assign]

    engine.run_cycle(bars_until(FIRST), now=_now(FIRST) + timedelta(minutes=5))
    assert asked == [ABOVE] and engine.book.units()[ABOVE] == 10  # asked, not resolved yet
    assert engine.exposure_breaches(equity) == []  # the exposure check still runs with a binary in the book
    held = engine.snapshot()["contracts"][ABOVE]
    assert (held["kind"], held["units"], held["price"], held["value"], held["avg_entry"]) == ("binary", 10.0, 0.6, 6.0, 0.6)

    answers[ABOVE] = True
    engine.run_cycle(bars_until(FIRST), now=_now(FIRST) + timedelta(minutes=10))
    assert ABOVE not in engine.book.units() and [record for record in taxed if record[0] == ABOVE] == [(ABOVE, "REALIZED_PNL", 4.0, "binary_settlement")]
    engine.run_cycle(bars_until(FIRST), now=_now(FIRST) + timedelta(minutes=15))
    assert asked == [ABOVE, ABOVE]  # a settled contract is not asked about again
