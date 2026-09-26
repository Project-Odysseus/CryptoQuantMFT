"""Portfolio book (src/portfolio/book.py): fills, funding, marks, FX, day start, attribution and exact JSON checkpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from src.portfolio.book import PortfolioBook
from src.portfolio.config import InstrumentSpec, parse_portfolio_config

BTC, ETH, SPOT = "kraken_futures:BTC/USD", "kraken_futures:ETH/USD", "kraken:BTC/EUR"
INSTRUMENTS = {BTC: InstrumentSpec(id=BTC), ETH: InstrumentSpec(id=ETH), SPOT: InstrumentSpec(id=SPOT, kind="spot")}


def _perp_book() -> PortfolioBook:
    return PortfolioBook(base_currency="USD", instruments=INSTRUMENTS, cash={"kraken_futures": D("10000")})


def test_a_perp_round_trip_and_a_flip_match_hand_calculation() -> None:
    book = _perp_book()
    assert book.apply_fill(BTC, "buy", "0.1", "50000", "2.5") == 0
    assert book.cash["kraken_futures"] == D("9997.5")
    book.mark({BTC: 55000})
    assert book.equity() == D("10497.5")  # 9997.5 cash + 0.1 x 5000 open profit

    assert book.apply_fill(BTC, "sell", "0.05", "56000", "1.4") == D("300")  # 0.05 x (56000 - 50000)
    assert book.cash["kraken_futures"] == D("10296.1") and book.positions[BTC].avg_entry == D("50000")

    assert book.apply_fill(BTC, "sell", "0.1", "54000", "2.7") == D("200")  # closes 0.05 at +4000, opens 0.05 short
    position = book.positions[BTC]
    assert (position.units, position.avg_entry) == (D("-0.05"), D("54000"))
    assert book.cash["kraken_futures"] == D("10493.4")
    book.mark({BTC: 52000})
    assert book.equity() == D("10593.4")  # the short is 100 up
    assert book.weights()[BTC] == pytest.approx(-0.05 * 52000 / 10593.4)
    assert (position.realized_pnl, position.fees) == (D("500"), D("6.6"))

    book.apply_fill(BTC, "buy", "0.05", "52000", "2.6")
    assert BTC not in book.units() and book.positions[BTC].avg_entry == 0
    assert book.equity() == book.cash["kraken_futures"] == D("10590.8")


def test_adding_to_a_position_averages_the_entry() -> None:
    book = _perp_book()
    book.apply_fill(ETH, "sell", "1", "2000")
    book.apply_fill(ETH, "sell", "3", "2400")
    assert book.positions[ETH].avg_entry == D("2300") and book.positions[ETH].units == D("-4")
    book.apply_fill(ETH, "buy", "1", "2100")  # a partial cover keeps the average
    assert book.positions[ETH].avg_entry == D("2300") and book.positions[ETH].realized_pnl == D("200")


def test_spot_spends_cash_and_equity_converts_at_the_recorded_fx_rate() -> None:
    book = PortfolioBook(base_currency="USD", instruments=INSTRUMENTS, cash={"kraken": D("1000")}, fx={"EUR": D("1.1")})
    assert book.venue_currency["kraken"] == "EUR" and book.initial_equity == D("1100.0")
    book.apply_fill(SPOT, "buy", "0.01", "40000", "1.6")
    assert book.cash["kraken"] == D("598.4")
    book.mark({SPOT: 44000})
    assert book.venue_equity("kraken") == D("1038.4") and book.equity() == D("1038.4") * D("1.1")
    assert book.apply_fill(SPOT, "sell", "0.01", "44000", "1.76") == D("40")
    assert book.cash["kraken"] == D("1036.64")

    no_rate = PortfolioBook(base_currency="USD", instruments=INSTRUMENTS, cash={"kraken": D("1000")})
    with pytest.raises(ValueError, match="no FX rate for EUR"):
        no_rate.equity()


def test_funding_is_paid_by_longs_and_received_by_shorts_on_perps_only() -> None:
    book = _perp_book()
    book.apply_fill(BTC, "buy", "0.1", "50000")
    book.apply_fill(ETH, "sell", "2", "2500")
    book.mark({BTC: 50000, ETH: 2500})
    assert book.apply_funding(BTC, "0.0001") == D("0.5")
    assert book.apply_funding(ETH, "0.0001") == D("-0.5")
    assert book.cash["kraken_futures"] == D("10000") and book.positions[BTC].funding == D("0.5")
    assert book.apply_funding(SPOT, "0.0001") == 0


def test_the_day_starts_before_its_first_prices_and_the_peak_only_rises() -> None:
    book = _perp_book()
    book.apply_fill(BTC, "buy", "0.2", "50000")
    book.mark({BTC: 50000}, now=datetime(2026, 1, 1, 20, tzinfo=timezone.utc))
    book.mark({BTC: 55000}, now=datetime(2026, 1, 1, 23, tzinfo=timezone.utc))
    assert book.peak_equity == D("11000")
    book.mark({BTC: 45000}, now=datetime(2026, 1, 2, 3, tzinfo=timezone.utc))  # the drop happened on the 2nd
    assert book.day_start_equity == D("11000") and book.peak_equity == D("11000") and book.equity() == D("9000")


def test_a_json_checkpoint_round_trip_is_exact() -> None:
    book = PortfolioBook(base_currency="USD", instruments=INSTRUMENTS, cash={"kraken_futures": D("10000"), "kraken": D("500")}, fx={"EUR": D("1.0823")})
    book.apply_fill(BTC, "buy", "0.1234", "50123.45", "3.0931")
    book.apply_fill(SPOT, "buy", "0.005", "41000.1", "0.82")
    book.mark({BTC: "51000.5", SPOT: "41500"}, now=datetime(2026, 3, 4, 8, tzinfo=timezone.utc))
    book.set_sleeve_targets({"btc_trend": (BTC, 0.5), "spot_trend": (SPOT, 0.1)})
    book.apply_funding(BTC, "0.00012")

    restored = PortfolioBook.from_dict(json.loads(json.dumps(book.to_dict())), instruments=INSTRUMENTS)
    assert restored.to_dict() == book.to_dict()
    assert restored.equity() == book.equity() and restored.attribution() == book.attribution()


def test_attribution_splits_pnl_by_sleeve_and_reports_the_rest_as_residual() -> None:
    book = _perp_book()
    book.mark({BTC: 50000})
    book.set_sleeve_targets({"trend": (BTC, 0.3), "reversion": (BTC, -0.1)})  # nets to +0.2 = 0.04 BTC
    book.apply_fill(BTC, "buy", "0.04", "50000", "1")
    book.mark({BTC: 55000})
    pnl = book.attribution()
    assert pnl["trend"] == D("300") and pnl["reversion"] == D("-100")  # 0.06 BTC x 5000, -0.02 BTC x 5000
    assert pnl["residual"] == D("-1")  # netting was exact, so only the fee is left
    assert sum(pnl.values()) == book.equity() - book.initial_equity


def test_a_paper_book_from_a_config_splits_equity_across_venues() -> None:
    raw = {
        "portfolio": {"initial_equity": 10000},
        "instruments": {BTC: {"kind": "perp"}, SPOT: {"kind": "spot"}},
        "sleeves": [{"id": "a", "instrument": BTC, "interval": "1d", "strategy": "moving_average_crossover"},
                    {"id": "b", "instrument": SPOT, "interval": "1d", "strategy": "moving_average_crossover", "long_only": True}],
    }
    config = parse_portfolio_config(raw)
    book = PortfolioBook.from_config(config, fx={"EUR": 1.25})
    assert book.cash == {"kraken": D("4000"), "kraken_futures": D("5000")} and book.equity() == D("10000")
    with pytest.raises(ValueError, match="pass fx="):
        PortfolioBook.from_config(config)
    with pytest.raises(ValueError, match="positive units"):
        book.apply_fill(BTC, "buy", "0", "50000")
    with pytest.raises(KeyError, match="not in the book"):
        book.apply_fill("kraken_futures:SOL/USD", "buy", "1", "100")
