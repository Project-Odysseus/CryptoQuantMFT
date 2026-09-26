"""Soak test: the live portfolio path against a Kraken Futures that times out, rate-limits, rejects and fills late.

The real `KrakenFuturesCrossMarginAdapter` and `PortfolioEngine` run for about 1,700 cycles over several random
seeds, against `FakeKraken` (tests/test_portfolio_live.py) wrapped in `ChaosKraken`, which injects faults:

- a timeout before the request reaches the exchange (nothing happened there);
- a lost response after an order reached the exchange (it filled; we never heard back);
- HTTP 503 and rate-limit answers;
- order rejections;
- fills that appear in /fills only a few cycles later.

Invariants, checked every cycle or at the end:
1. No client order id ever reaches the exchange twice (a timed-out order is never resent).
2. Positions never exceed the configured caps.
3. After the faults stop and a few calm cycles pass: nothing pending, and the book's positions equal Kraken's.
4. The book's equity equals the account's equity (fees are estimated at the rate the fake charges).
"""

from __future__ import annotations

import random
import urllib.error
from collections import Counter
from typing import Any

import pytest

from src.portfolio.engine import PortfolioEngine
from test_portfolio_engine import BTC, ETH, FIRST, LAST, bars_until
from test_portfolio_live import FakeKraken, _cycle, _live_engine


class ChaosKraken:
    """Wraps FakeKraken's transport and injects faults at the given rates (per request)."""

    def __init__(self, inner: FakeKraken, seed: int, *, timeout: float = 0.04, lost: float = 0.04, http_503: float = 0.03,
                 rate_limit: float = 0.03, reject: float = 0.04, late_fills: float = 0.05) -> None:
        self.inner = inner
        self.rng = random.Random(seed)
        self.rates = {"timeout": timeout, "lost": lost, "http_503": http_503, "rate_limit": rate_limit, "reject": reject, "late_fills": late_fills}
        self.enabled = True
        self.attempted_orders: list[str] = []  # every cliOrdId our process tried to send
        self.injected: Counter[str] = Counter()

    def _roll(self, fault: str) -> bool:
        hit = self.enabled and self.rng.random() < self.rates[fault]
        if hit:
            self.injected[fault] += 1
        return hit

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> dict[str, Any]:
        endpoint = url.partition("?")[0].rsplit("/", 1)[-1]
        if endpoint == "sendorder":
            params = dict(pair.split("=", 1) for pair in body.decode().split("&"))
            self.attempted_orders.append(params["cliOrdId"])
        if self._roll("timeout"):
            raise TimeoutError("read timed out")  # never reached the exchange
        if self._roll("http_503"):
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
        if method == "GET" and self._roll("rate_limit"):
            return {"result": "error", "error": "apiLimitExceeded"}
        if endpoint == "sendorder":
            if self._roll("reject"):
                return {"result": "success", "sendStatus": {"status": "insufficientAvailableFunds"}}
            if self._roll("late_fills"):
                self.inner.delay_fills = True
            if self._roll("lost"):
                self.inner.lose_next_response = True
        elif endpoint == "fills" and self.inner.hidden_fills and self.rng.random() < 0.3:
            self.inner.release_fills()
        return self.inner(method, url, headers, body)


def _positions_within_caps(engine: PortfolioEngine, fake: FakeKraken) -> None:
    account = fake("GET", "https://x/accounts", {}, None)["accounts"]["flex"]
    equity = account["marginEquity"]
    values = {symbol: abs(size) * fake.prices[symbol] for symbol, (size, _entry) in fake.positions.items()}
    risk = engine.config.risk
    tolerance = 0.05  # price moves inside a cycle and lot rounding
    assert all(value / equity <= risk.max_instrument_weight + tolerance for value in values.values()), values
    assert sum(values.values()) / equity <= risk.max_gross_exposure + tolerance


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_the_live_path_survives_exchange_faults_without_double_orders_or_drift(tmp_path, seed: int) -> None:
    fake = FakeKraken()
    engine = _live_engine(fake, tmp_path)
    adapter = engine.adapters["kraken_futures"]
    chaos = ChaosKraken(fake, seed)
    adapter._client.transport = chaos
    adapter._client.sleep = lambda _: None  # retries without real waiting

    failed_cycles = 0
    calm_from = LAST - 40
    for index in range(FIRST, LAST):
        chaos.enabled = index < calm_from
        if index == calm_from:
            fake.release_fills()
        try:
            _cycle(engine, fake, index)
        except Exception:  # noqa: BLE001 - the runtime counts a failed cycle and carries on; so do we
            failed_cycles += 1
        _positions_within_caps(engine, fake)

    # 1. No client id reached the exchange twice, and none was even attempted twice
    reached = [request["params"]["cliOrdId"] for request in fake.requests if request["endpoint"] == "sendorder"]
    assert len(reached) == len(set(reached))
    assert len(chaos.attempted_orders) == len(set(chaos.attempted_orders))
    # 3. Calm again: nothing pending, and the book holds exactly what Kraken holds
    assert engine.pending_orders == {}
    held = {symbol: size for symbol, (size, _entry) in fake.positions.items()}
    assert float(engine.book.units().get(BTC, 0)) == pytest.approx(held.get("PF_XBTUSD", 0.0), abs=1e-9)
    assert float(engine.book.units().get(ETH, 0)) == pytest.approx(held.get("PF_ETHUSD", 0.0), abs=1e-9)
    # 4. Equity adds up: the book's view equals the account's
    account = fake("GET", "https://x/accounts", {}, None)["accounts"]["flex"]["marginEquity"]
    assert float(engine.book.equity()) == pytest.approx(account, rel=1e-6)
    # The faults really happened, and trading really continued through them
    assert min(chaos.injected[fault] for fault in ("timeout", "lost", "http_503", "reject")) > 0
    assert len(reached) > 10 and failed_cycles < (calm_from - FIRST) // 4
