"""Temporary exchange failures are retried with backoff; order placement never is."""

from __future__ import annotations

import base64
import urllib.error
from typing import Any

import pytest

from src.execution.adapters import KrakenExecutionAdapter
from src.execution.kraken_futures_adapter import KrakenFuturesPrivateClient
from src.utils.retry import TransientExchangeError, is_transient, retry_call

SECRET = base64.b64encode(b"not-a-real-secret").decode()


def test_retry_call_backs_off_on_temporary_failures_and_returns() -> None:
    outcomes: list[Any] = [TimeoutError("read timed out"), ConnectionResetError("reset"), "ok"]
    naps: list[float] = []

    def flaky() -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert retry_call(flaky, label="t", sleep=naps.append, jitter=0.0) == "ok"
    assert naps == [0.5, 1.0]


def test_retry_call_raises_real_errors_at_once_and_temporary_ones_after_the_last_attempt() -> None:
    naps: list[float] = []
    with pytest.raises(ValueError):
        retry_call(lambda: (_ for _ in ()).throw(ValueError("bad request")), label="t", sleep=naps.append)
    assert naps == []
    with pytest.raises(TimeoutError):
        retry_call(lambda: (_ for _ in ()).throw(TimeoutError("down")), label="t", sleep=naps.append, jitter=0.0)
    assert naps == [0.5, 1.0]


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None), True),
        (urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None), True),
        (urllib.error.HTTPError("u", 400, "Bad Request", {}, None), False),
        (urllib.error.URLError("timed out"), True),
        (TransientExchangeError("EService:Unavailable"), True),
        (RuntimeError("EOrder:Insufficient funds"), False),
        (RuntimeError("Kraken Futures accounts failed: apiLimitExceeded"), True),
    ],
)
def test_what_counts_as_temporary(error: Exception, transient: bool) -> None:
    assert is_transient(error) is transient


def test_a_wrapped_network_error_counts_as_temporary() -> None:
    try:
        try:
            raise ConnectionResetError("reset by peer")
        except ConnectionResetError as inner:
            raise RuntimeError(str(inner)) from inner
    except RuntimeError as wrapped:
        assert is_transient(wrapped)


class FlakyFutures:
    """A transport that fails the first `failures` calls of each endpoint, then answers."""

    def __init__(self, failures: int, error: Exception | dict[str, Any]) -> None:
        self.failures = failures
        self.error = error
        self.calls: list[str] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> dict[str, Any]:
        endpoint = url.split("?")[0].rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        if self.calls.count(endpoint) <= self.failures:
            if isinstance(self.error, Exception):
                raise self.error
            return self.error
        return {"result": "success", "openPositions": [], "sendStatus": {"status": "placed"}}


def _client(transport: FlakyFutures) -> KrakenFuturesPrivateClient:
    client = KrakenFuturesPrivateClient(api_key="k", api_secret=SECRET, transport=transport)
    client.sleep = lambda _: None
    return client


def test_futures_reads_retry_with_a_fresh_nonce_each_time() -> None:
    transport = FlakyFutures(2, TimeoutError("timed out"))
    assert _client(transport).call("GET", "openpositions")["openPositions"] == []
    assert transport.calls == ["openpositions"] * 3


def test_futures_rate_limit_answers_are_retried() -> None:
    transport = FlakyFutures(1, {"result": "error", "error": "apiLimitExceeded"})
    assert _client(transport).call("GET", "openpositions")["result"] == "success"
    assert len(transport.calls) == 2


def test_futures_orders_are_never_retried() -> None:
    transport = FlakyFutures(1, TimeoutError("timed out"))
    with pytest.raises(TimeoutError):
        _client(transport).call("POST", "sendorder", {"orderType": "mkt"})
    assert transport.calls == ["sendorder"]


def test_futures_cancels_are_retried() -> None:
    transport = FlakyFutures(1, TimeoutError("timed out"))
    assert _client(transport).call("POST", "cancelallorders", {})["result"] == "success"
    assert transport.calls == ["cancelallorders"] * 2


def _spot(answers: dict[str, list[dict[str, Any]]], calls: list[str]) -> KrakenExecutionAdapter:
    adapter = KrakenExecutionAdapter(api_key="key", api_secret=SECRET)
    adapter._retry_sleep = lambda _: None

    def request_json(method: str, url: str, **_: Any) -> dict[str, Any]:
        endpoint = url.rsplit("/", 1)[-1]
        calls.append(endpoint)
        queue = answers[endpoint]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    adapter._request_json = request_json  # type: ignore[method-assign]
    return adapter


def test_spot_reads_retry_when_kraken_is_busy() -> None:
    calls: list[str] = []
    adapter = _spot({"Balance": [{"error": ["EService:Unavailable"]}, {"error": [], "result": {"ZEUR": "15.99"}}]}, calls)
    assert adapter._private_request(endpoint="Balance", params={})["result"] == {"ZEUR": "15.99"}
    assert calls == ["Balance", "Balance"]


def test_spot_gives_back_krakens_answer_when_it_stays_busy_and_never_retries_an_order() -> None:
    calls: list[str] = []
    busy = {"error": ["EService:Busy"]}
    adapter = _spot({"OpenOrders": [busy], "AddOrder": [busy]}, calls)
    assert adapter._private_request(endpoint="OpenOrders", params={}) == busy
    assert calls.count("OpenOrders") == 3
    assert adapter._private_request(endpoint="AddOrder", params={}) == busy
    assert calls.count("AddOrder") == 1


def test_public_market_data_requests_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.data.historical as historical
    import src.utils.retry as retry

    answers: list[Any] = [urllib.error.URLError("timed out"), {"result": "success", "instruments": []}]
    monkeypatch.setattr(retry.time, "sleep", lambda _: None)
    monkeypatch.setattr(historical, "_request_json_once", lambda *a, **k: (lambda item: (_ for _ in ()).throw(item) if isinstance(item, Exception) else item)(answers.pop(0)))
    from src.data.kraken_futures import fetch_instruments

    assert fetch_instruments() == []
    assert answers == []
