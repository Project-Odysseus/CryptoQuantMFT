"""The kill switch acts on the exchanges by itself: spot orders cancelled, perps closed reduce-only, state re-read."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

import main
from src.data.fx import FXRateCollector
from src.execution.adapters import KrakenExecutionAdapter
from src.execution.emergency import FUTURES_TAKER_FEE_RATE, flatten_kraken_futures
from src.risk.kill_switch import KillSwitchController


class FakeFutures:
    """Kraken Futures' private API: positions close when a reduce-only market order arrives, unless `stuck`."""

    def __init__(self, positions: list[dict[str, Any]], *, prices: dict[str, float] | None = None, stuck: bool = False, open_orders: int = 1) -> None:
        self.positions = positions
        self.prices = prices or {}
        self.stuck = stuck
        self.open_orders = open_orders
        self.fills: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call(self, method: str, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, endpoint, dict(params or {})))
        if endpoint == "cancelallorders":
            self.open_orders = 0
            return {"result": "success", "cancelStatus": {"status": "cancelled", "cancelledOrders": []}}
        if endpoint == "openpositions":
            return {"result": "success", "openPositions": [dict(position) for position in self.positions]}
        if endpoint == "sendorder":
            assert params["reduceOnly"] == "true" and params["orderType"] == "mkt"
            if not self.stuck:
                self.positions = [position for position in self.positions if position["symbol"] != params["symbol"]]
                self.fills.append({"cliOrdId": params["cliOrdId"], "size": float(params["size"]), "price": self.prices[params["symbol"]]})
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": "x"}}
        if endpoint == "fills":
            return {"result": "success", "fills": list(self.fills)}
        if endpoint == "openorders":
            return {"result": "success", "openOrders": [{"order_id": "resting"}] * self.open_orders}
        raise AssertionError(endpoint)

    def sent(self) -> list[dict[str, Any]]:
        return [params for _, endpoint, params in self.calls if endpoint == "sendorder"]


class RecordingLogger:
    def __init__(self) -> None:
        self.trades: list[dict[str, Any]] = []
        self.derivative: list[dict[str, Any]] = []

    def log_trade(self, **kwargs: Any) -> tuple[int, None]:
        self.trades.append(kwargs)
        return 1, None

    def log_derivative_event(self, **kwargs: Any) -> str:
        self.derivative.append(kwargs)
        return "id"


class RecordingNotifier:
    def __init__(self) -> None:
        self.alerts: list[dict[str, Any]] = []

    def send_alert(self, **kwargs: Any) -> bool:
        self.alerts.append(kwargs)
        return True


LONG_BTC = {"symbol": "PF_XBTUSD", "side": "long", "size": 0.0001, "price": 84000.0}
SHORT_ETH = {"symbol": "PF_ETHUSD", "side": "short", "size": 0.01, "price": 3000.0}


def test_flatten_cancels_orders_then_closes_every_position_reduce_only_and_records_it() -> None:
    client = FakeFutures([LONG_BTC, SHORT_ETH], prices={"PF_XBTUSD": 84100.0, "PF_ETHUSD": 2990.0})
    logger = RecordingLogger()

    result = flatten_kraken_futures(client, trade_logger=logger, sleep=lambda _: None)

    assert client.calls[0][1] == "cancelallorders"
    assert [(order["symbol"], order["side"], order["size"]) for order in client.sent()] == [("PF_XBTUSD", "sell", "0.0001"), ("PF_ETHUSD", "buy", "0.01")]
    assert result["remaining_positions"] == [] and result["open_orders_left"] == 0
    btc, eth = result["closes"]
    assert btc["realized_pnl"] == pytest.approx(100.0 * 0.0001)  # long, price up 100
    assert eth["realized_pnl"] == pytest.approx(10.0 * 0.01)  # short, price down 10
    assert btc["fee"] == pytest.approx(0.0001 * 84100.0 * FUTURES_TAKER_FEE_RATE)
    assert [(trade["pair"], trade["side"], trade["source"]) for trade in logger.trades] == [("BTC/USD", "sell", "kill_switch"), ("ETH/USD", "buy", "kill_switch")]
    assert sorted(event["transaction_type"] for event in logger.derivative) == ["REALIZED_PNL", "REALIZED_PNL", "TRADING_FEE", "TRADING_FEE"]


def test_flatten_reports_a_position_that_will_not_close_and_records_nothing() -> None:
    client = FakeFutures([LONG_BTC], stuck=True)
    logger = RecordingLogger()
    naps: list[float] = []

    result = flatten_kraken_futures(client, trade_logger=logger, sleep=naps.append, timeout=3.0)

    assert [position["symbol"] for position in result["remaining_positions"]] == ["PF_XBTUSD"]
    assert len(naps) == 3 and logger.trades == [] and logger.derivative == []


def test_flatten_on_a_flat_account_only_cancels_orders() -> None:
    client = FakeFutures([])
    result = flatten_kraken_futures(client, sleep=lambda _: None)
    assert client.sent() == [] and result["closes"] == [] and result["open_orders_left"] == 0


def _spot_adapter(open_orders: dict[str, dict[str, Any]], requests: list[tuple[str, dict[str, Any]]]) -> KrakenExecutionAdapter:
    """A real KrakenExecutionAdapter whose private API answers like Kraken spot (cancelling removes the order)."""
    adapter = KrakenExecutionAdapter(api_key="key", api_secret="c2VjcmV0")

    def private_request(*, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        requests.append((endpoint, dict(params)))
        if endpoint == "OpenOrders":
            return {"error": [], "result": {"open": dict(open_orders)}}
        if endpoint == "CancelOrder":
            open_orders.pop(params["txid"], None)
            return {"error": [], "result": {"count": 1}}
        if endpoint == "CancelAll":
            count = len(open_orders)
            open_orders.clear()
            return {"error": [], "result": {"count": count}}
        raise AssertionError(endpoint)

    adapter._private_request = private_request  # type: ignore[method-assign]
    return adapter


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a temporary database and kill-switch file, with fixed FX rates."""
    monkeypatch.setattr(main.settings, "database_path", tmp_path / "db.sqlite")
    monkeypatch.setattr(main, "PORTFOLIO_KILL_SWITCH_FILE", tmp_path / "kill_switch_state.json")
    monkeypatch.setattr(FXRateCollector, "get_rate", lambda self, pair="EUR/NOK", at=None: 11.5 if pair.upper() == "EUR/NOK" else 10.5)
    return tmp_path / "kill_switch_state.json"


def test_kill_switch_cancels_a_resting_spot_order_and_closes_perps_on_its_own(isolated: Path) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []
    spot = _spot_adapter({"OQS5AR-Y7MBP-M7EQMA": {"status": "open", "vol": "0.00005", "vol_exec": "0", "price": "0", "fee": "0",
                                                   "descr": {"pair": "XBTEUR", "type": "buy"}}}, requests)
    futures = FakeFutures([LONG_BTC], prices={"PF_XBTUSD": 84000.0})
    notifier = RecordingNotifier()
    running_runtime = KillSwitchController(state_file=isolated)  # a runtime's controller, created before the activation

    code = main.kill_switch(argparse.Namespace(kill_switch_reason="test"), spot_adapter=spot, futures_client=futures, notifier=notifier, sleep=lambda _: None)

    assert code == 0
    assert ("CancelOrder", {"txid": "OQS5AR-Y7MBP-M7EQMA"}) in requests and ("CancelAll", {}) in requests
    assert futures.sent()[0]["side"] == "sell"
    assert running_runtime.is_active()  # another process's controller sees the file
    assert "Every open order cancelled" in notifier.alerts[0]["message"]

    assert main.kill_switch_reset() == 0
    assert not running_runtime.is_active()


def test_kill_switch_still_activates_and_reports_when_the_exchanges_fail(isolated: Path) -> None:
    class Broken:
        def fetch_open_orders(self) -> list[dict[str, Any]]:
            raise RuntimeError("network down")

    class BrokenFutures:
        def call(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("network down")

    notifier = RecordingNotifier()
    code = main.kill_switch(argparse.Namespace(kill_switch_reason="test"), spot_adapter=Broken(), futures_client=BrokenFutures(), notifier=notifier)

    assert code == 1
    assert KillSwitchController(state_file=isolated).is_active()
    assert "CHECK THE ACCOUNTS" in notifier.alerts[0]["message"]


def test_a_failed_cancel_does_not_stop_the_activation(tmp_path: Path) -> None:
    class Adapter:
        def list_orders(self) -> list[Any]:
            return [type("Order", (), {"order_id": "a", "status": "OPEN"})(), type("Order", (), {"order_id": "b", "status": "OPEN"})()]

        def cancel_order(self, *, order_id: str) -> Any:
            if order_id == "a":
                raise RuntimeError("timeout")
            return type("Report", (), {"status": "CANCELED", "message": "ok"})()

    state = KillSwitchController(state_file=tmp_path / "state.json").activate("test", execution_adapter=Adapter())
    assert [order["status"] for order in state["orders_cancelled"]] == ["ERROR", "CANCELED"]
    assert KillSwitchController(state_file=tmp_path / "state.json").is_active()
