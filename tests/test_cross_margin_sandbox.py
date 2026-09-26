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
