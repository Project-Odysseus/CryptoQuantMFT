"""Cross-margin sandbox for several perps in one account (src/execution/cross_margin.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.execution.cross_margin import SandboxCrossMarginPerpAdapter
from src.execution.perps import assumed_perp_contract

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
BTC, ETH = "BTC/USD", "ETH/USD"


def _account(**overrides: object) -> SandboxCrossMarginPerpAdapter:
    settings: dict[str, object] = {"contracts": [assumed_perp_contract(BTC), assumed_perp_contract(ETH)], "starting_collateral": 1000.0,
                                   "max_leverage": 2.0, "funding_pct_per_day": 0.0}
    settings.update(overrides)
    return SandboxCrossMarginPerpAdapter(**settings)  # type: ignore[arg-type]


def _order(account: SandboxCrossMarginPerpAdapter, order_id: str, symbol: str, side: str, size: float, price: float, **kwargs: object):
    return account.submit_order(order_id=order_id, side=side, size=size, price=price, timestamp=T0, symbol=symbol, **kwargs)


def test_two_contracts_share_one_wallet_and_settle_pnl_and_fees_in_it() -> None:
    account = _account()
    assert _order(account, "1", BTC, "buy", 0.01, 50_000.0).status == "FILLED"  # 500 notional, 0.25 fee
    assert _order(account, "2", ETH, "sell", 0.2, 2_500.0).status == "FILLED"  # 500 notional, 0.25 fee
    assert account.wallet_balance() == pytest.approx(999.5)
    assert account.positions() == {BTC: pytest.approx(0.01), ETH: pytest.approx(-0.2)}

    account.on_market_update(prices={BTC: 55_000.0, ETH: 2_000.0}, timestamp=T0)
    assert account.equity() == pytest.approx(999.5 + 50.0 + 100.0)  # both positions are in profit
    assert account.used_initial_margin() == pytest.approx((550.0 + 400.0) / 2.0)

    report = _order(account, "3", ETH, "buy", 0.2, 2_000.0)  # close the short: +100 realized, 0.2 fee
    assert report.status == "FILLED" and account.position_size(ETH) == 0.0
    assert account.wallet_balance() == pytest.approx(999.5 + 100.0 - 0.2)
    snapshot = account.get_account_snapshot()
    assert snapshot["account_type"] == "cross_margin" and set(snapshot["positions_by_symbol"]) == {BTC}
    assert snapshot["positions_by_symbol"][BTC]["unrealized_pnl"] == pytest.approx(50.0) and snapshot["realized_pnl_total"] == pytest.approx(100.0)


def test_the_margin_check_covers_every_position_and_reductions_always_pass() -> None:
    account = _account()
    assert _order(account, "1", BTC, "buy", 0.03, 50_000.0).status == "FILLED"  # 1500 notional = 750 margin at 2x
    rejected = _order(account, "2", ETH, "buy", 0.24, 2_500.0)  # 600 more = 300 margin: 1050 > ~999
    assert rejected.status == "REJECTED" and "insufficient margin" in (rejected.message or "")
    assert _order(account, "3", ETH, "buy", 0.18, 2_500.0).status == "FILLED"  # 450 more = 225 margin: 975 fits
    assert _order(account, "4", BTC, "sell", 0.01, 50_000.0).status == "FILLED"  # reducing needs no margin
    assert account.buying_power(ETH) == pytest.approx((account.equity() - account.used_initial_margin()) * 2.0 / (1.0 + 2.0 * 0.0005))


def test_reduce_only_orders_never_grow_or_flip_a_position() -> None:
    account = _account()
    _order(account, "1", BTC, "buy", 0.01, 50_000.0)
    grow = _order(account, "2", BTC, "buy", 0.01, 50_000.0, reduce_only=True)
    flip = _order(account, "3", BTC, "sell", 0.02, 50_000.0, reduce_only=True)
    assert grow.status == flip.status == "REJECTED" and "reduce_only" in (flip.message or "")
    assert _order(account, "4", BTC, "sell", 0.01, 50_000.0, reduce_only=True).status == "FILLED"
    assert account.positions() == {}


def test_a_flip_in_one_order_realizes_the_old_side_and_opens_the_new_one_at_the_fill() -> None:
    account = _account()
    _order(account, "1", BTC, "buy", 0.01, 50_000.0)
    _order(account, "2", BTC, "sell", 0.02, 52_000.0)
    assert account.position_size(BTC) == pytest.approx(-0.01) and account.entry_price(BTC) == pytest.approx(52_000.0)
    assert account.realized_pnl_total == pytest.approx(20.0)


def test_funding_accrues_per_position_over_elapsed_time() -> None:
    account = _account(funding_pct_per_day={BTC: 0.01, ETH: 0.02})
    _order(account, "1", BTC, "buy", 0.01, 50_000.0)
    _order(account, "2", ETH, "sell", 0.2, 2_500.0)
    account.on_market_update(prices={BTC: 50_000.0, ETH: 2_500.0}, timestamp=T0)
    wallet = account.wallet_balance()
    events = account.on_market_update(prices={BTC: 50_000.0, ETH: 2_500.0}, timestamp=T0 + timedelta(hours=12))
    payments = {event["symbol"]: event["payment"] for event in events if event["type"] == "funding"}
    assert payments[BTC] == pytest.approx(500.0 * 0.0001 * 0.5)  # long pays
    assert payments[ETH] == pytest.approx(-500.0 * 0.0002 * 0.5)  # short receives
    assert account.wallet_balance() == pytest.approx(wallet - 0.025 + 0.05)


def test_the_whole_account_is_liquidated_when_equity_reaches_maintenance_margin() -> None:
    account = _account(max_leverage=5.0)
    _order(account, "1", BTC, "buy", 0.05, 50_000.0)  # 2500 notional on 1000 collateral
    _order(account, "2", ETH, "buy", 0.8, 2_500.0)  # 2000 more: 4.5x
    assert account.on_market_update(prices={BTC: 45_000.0, ETH: 2_300.0}, timestamp=T0) == []  # -250 -160: equity ~588
    events = account.on_market_update(prices={BTC: 38_000.0, ETH: 1_950.0}, timestamp=T0)
    liquidation = next(event for event in events if event["type"] == "liquidation")
    assert set(liquidation["closed"]) == {BTC, ETH} and account.positions() == {}
    assert account.wallet_balance() >= 0.0 and account.get_account_snapshot()["liquidation_count"] == 2


def test_slippage_moves_fills_against_the_order() -> None:
    account = _account(slippage_bps={BTC: 10.0})
    buy = _order(account, "1", BTC, "buy", 0.01, 50_000.0)
    sell = _order(account, "2", BTC, "sell", 0.01, 50_000.0)
    assert buy.fill_price == pytest.approx(50_050.0) and sell.fill_price == pytest.approx(49_950.0)
    assert account.realized_pnl_total == pytest.approx(-1.0)


def test_orders_and_accounts_that_cannot_work_are_refused() -> None:
    account = _account()
    assert "trades" in (_order(account, "1", "SOL/USD", "buy", 1.0, 100.0).message or "")
    assert "below the contract minimum" in (_order(account, "2", BTC, "buy", 0.00001, 50_000.0).message or "")
    assert _order(account, "3", BTC, "hold", 0.01, 50_000.0).status == "REJECTED"
    with pytest.raises(ValueError, match="one collateral currency"):
        SandboxCrossMarginPerpAdapter(contracts=[assumed_perp_contract("BTC/USD"), assumed_perp_contract("ETH/EUR")])
    with pytest.raises(ValueError, match="at least one contract"):
        SandboxCrossMarginPerpAdapter(contracts=[])


def test_a_restarted_account_continues_from_its_state_file(tmp_path) -> None:
    path = tmp_path / "cross.json"
    account = _account(state_path=path, funding_pct_per_day=0.01)
    _order(account, "1", BTC, "buy", 0.01, 50_000.0)
    _order(account, "2", ETH, "sell", 0.2, 2_500.0)
    account.on_market_update(prices={BTC: 51_000.0, ETH: 2_400.0}, timestamp=T0 + timedelta(hours=1))
    restored = _account(state_path=path, funding_pct_per_day=0.01, starting_collateral=5.0)
    assert restored.restored_from_state
    assert restored.positions() == account.positions() and restored.wallet_balance() == pytest.approx(account.wallet_balance())
    assert restored.equity() == pytest.approx(account.equity()) and restored.entry_price(ETH) == pytest.approx(2_500.0)

    with pytest.raises(ValueError, match="doesn't match this account"):
        SandboxCrossMarginPerpAdapter(contracts=[assumed_perp_contract(BTC)], state_path=path)


def test_closing_a_position_that_accumulated_float_residue_is_not_a_flip() -> None:
    account = _account()
    _order(account, "1", BTC, "sell", 0.1037, 50_000.0)
    account._positions["BTC"] = -0.10369999999999999  # what a series of float fills left behind (seen in a paper run)
    report = _order(account, "close", BTC, "buy", 0.1037, 50_000.0, reduce_only=True)
    assert report.status == "FILLED" and account.positions() == {}


def test_an_order_id_is_filled_once_and_a_restarted_account_can_say_what_became_of_an_order(tmp_path) -> None:
    path = tmp_path / "cross.json"
    account = _account(state_path=path)
    first = _order(account, "pf-7-0", BTC, "buy", 0.01, 50_000.0)
    again = _order(account, "pf-7-0", BTC, "buy", 0.01, 51_000.0)  # the same id again, e.g. a restart replaying its plan
    assert again.status == "FILLED" and (again.fill_price, again.filled_size, again.fee) == (first.fill_price, first.filled_size, first.fee)
    assert account.position_size(BTC) == pytest.approx(0.01)  # not filled twice

    restored = _account(state_path=path)
    assert restored.settle_orders() == []  # nothing asked about
    restored.track_order(order_id="pf-7-0", symbol=BTC, side="buy", size=0.01, price=50_000.0, timestamp=T0)
    restored.track_order(order_id="pf-7-1", symbol=ETH, side="buy", size=0.1, price=2_500.0, timestamp=T0)  # recorded by the engine, never sent
    filled, lost = restored.settle_orders()
    assert filled == {"order_id": "pf-7-0", "status": "FILLED", "symbol": BTC, "side": "buy", "filled_size": first.filled_size, "fill_price": first.fill_price, "fee": first.fee,
                      "liquidity": "taker"}
    assert lost == {"order_id": "pf-7-1", "status": "CANCELED", "never_received": True}
    assert restored.settle_orders() == []  # each answer is given once
    assert _order(restored, "pf-7-0", BTC, "buy", 0.01, 50_000.0).message == "already filled under this order id" and restored.position_size(BTC) == pytest.approx(0.01)


def test_a_post_only_order_rests_and_fills_at_its_limit_only_when_the_price_comes_to_it(tmp_path) -> None:
    path = tmp_path / "cross.json"
    account = _account(state_path=path, slippage_bps=10.0)
    account.on_market_update(prices={BTC: 50_000.0, ETH: 2_500.0}, timestamp=T0)
    buy = _order(account, "pf-1-0-mk", BTC, "buy", 0.01, 50_000.0, post_only=True)
    sell = _order(account, "pf-1-1-mk", ETH, "sell", 0.1, 2_500.0, post_only=True)
    assert buy.status == "SUBMITTED" and sell.status == "SUBMITTED" and account.positions() == {}  # nothing traded yet
    resting = account.resting_orders()
    assert resting["pf-1-0-mk"]["limit_price"] == pytest.approx(49_950.0) and resting["pf-1-1-mk"]["limit_price"] == pytest.approx(2_502.5)  # a buy waits below, a sell above
    assert _order(account, "pf-1-0-mk", BTC, "buy", 0.01, 50_000.0, post_only=True).message.startswith("already resting") and len(account.resting_orders()) == 2
    assert account.settle_orders() == []  # no outcome yet

    account.on_market_update(prices={BTC: 49_960.0, ETH: 2_501.0}, timestamp=T0 + timedelta(minutes=1))  # towards both, reaching neither
    assert account.positions() == {} and account.settle_orders() == []
    account.on_market_update(prices={BTC: 49_940.0, ETH: 2_501.0}, timestamp=T0 + timedelta(minutes=2))  # through the buy
    [filled] = account.settle_orders()
    assert filled["order_id"] == "pf-1-0-mk" and filled["status"] == "FILLED" and filled["liquidity"] == "maker"
    assert filled["fill_price"] == pytest.approx(49_950.0) and filled["fee"] == pytest.approx(0.01 * 49_950.0 * 0.0002)  # at its limit, at the maker fee
    assert account.position_size(BTC) == pytest.approx(0.01) and account.settle_orders() == []  # reported once

    restored = _account(state_path=path, slippage_bps=10.0)  # a restart keeps the order that is still resting
    assert list(restored.resting_orders()) == ["pf-1-1-mk"]
    restored.track_order(order_id="pf-1-1-mk", symbol=ETH, side="sell", size=0.1, price=2_500.0, timestamp=T0)
    assert restored.settle_orders() == []  # still resting: not "never received"
    assert restored.cancel_order(order_id="pf-1-1-mk").status == "CANCELED" and restored.resting_orders() == {}
    assert restored.settle_orders() == [{"order_id": "pf-1-1-mk", "status": "CANCELED"}]
    restored.on_market_update(prices={BTC: 49_940.0, ETH: 2_600.0}, timestamp=T0 + timedelta(minutes=3))
    assert restored.position_size(ETH) == 0.0  # a cancelled order never fills
    again = _account(state_path=path, slippage_bps=10.0)
    again.track_order(order_id="pf-1-1-mk", symbol=ETH, side="sell", size=0.1, price=2_500.0, timestamp=T0)
    assert again.settle_orders() == [{"order_id": "pf-1-1-mk", "status": "CANCELED"}]  # remembered as cancelled across a restart


def test_a_resting_order_is_checked_like_any_other_and_a_stale_reduce_only_one_is_dropped() -> None:
    account = _account(slippage_bps=0.0)
    assert _order(account, "big", BTC, "buy", 1.0, 50_000.0, post_only=True).status == "REJECTED"  # 50,000 of exposure on 1,000 at 2x
    assert _order(account, "none", BTC, "sell", 0.01, 50_000.0, post_only=True, reduce_only=True).status == "REJECTED"  # nothing to reduce
    _order(account, "open", BTC, "buy", 0.02, 50_000.0)
    assert _order(account, "trim", BTC, "sell", 0.02, 50_500.0, post_only=True, reduce_only=True).status == "SUBMITTED"
    _order(account, "close", BTC, "sell", 0.02, 50_000.0)  # the position is closed another way while the order waits
    account.on_market_update(prices={BTC: 50_600.0}, timestamp=T0 + timedelta(minutes=1))
    assert account.position_size(BTC) == 0.0 and account.settle_orders() == [{"order_id": "trim", "status": "CANCELED"}]  # it would have opened a short


def test_funding_is_charged_at_the_real_rate_per_coin_when_a_source_knows_it() -> None:
    real = {BTC: 0.05, ETH: -0.20}  # % of notional per day: BTC longs pay, ETH longs are paid
    account = _account(funding_pct_per_day=0.01, funding_source=lambda symbol: real.get(symbol))
    account.on_market_update(prices={BTC: 50_000.0, ETH: 2_500.0}, timestamp=T0)
    _order(account, "b", BTC, "buy", 0.01, 50_000.0)
    _order(account, "e", ETH, "buy", 0.2, 2_500.0)
    events = {event["symbol"]: event["payment"] for event in account.on_market_update(prices={BTC: 50_000.0, ETH: 2_500.0}, timestamp=T0 + timedelta(days=1))}
    assert events[BTC] == pytest.approx(500.0 * 0.0005) and events[ETH] == pytest.approx(-500.0 * 0.002)  # paid 0.25, received 1.00
    real.clear()  # the source no longer knows: the assumed flat rate applies
    later = {event["symbol"]: event["payment"] for event in account.on_market_update(prices={BTC: 50_000.0, ETH: 2_500.0}, timestamp=T0 + timedelta(days=2))}
    assert later[BTC] == pytest.approx(500.0 * 0.0001) and later[ETH] == pytest.approx(500.0 * 0.0001)
