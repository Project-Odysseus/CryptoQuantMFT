"""Exchange connector abstractions for live market data ingestion."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from config import settings

if TYPE_CHECKING:  # pragma: no cover
    from src.storage.market_store import MarketStore
    from src.storage.streaming_aggregator import StreamingAggregator


@dataclass(slots=True)
class MarketTick:
    """Normalized market tick structure for downstream consumers."""

    exchange: str
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    last: float | None = None
    volume: float | None = None
    raw: dict[str, Any] | None = None


class ExchangeConnector(ABC):
    """Base interface for exchange-specific live data adapters."""

    def __init__(
        self,
        name: str,
        symbol: str,
        store: "MarketStore | None" = None,
        aggregator: "StreamingAggregator | None" = None,
    ) -> None:
        """Initialize the object with its runtime state."""
        self.name = name
        self.symbol = symbol
        self._connected = False
        self.store = store
        self.aggregator = aggregator

    @abstractmethod
    async def connect(self) -> None:
        """Establish the connection to the exchange feed."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the exchange connection cleanly."""

    @abstractmethod
    async def fetch_snapshot(self) -> MarketTick:
        """Fetch a single normalized market snapshot from the exchange."""

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
    ) -> dict[str, Any] | list[Any]:
        """Perform an HTTP request and decode JSON payloads."""
        if params:
            query = urllib.parse.urlencode(params)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query}"

        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")

        request_headers = {"User-Agent": "CryptoQuantMFT/0.1", "Accept": "application/json"}
        if headers:
            request_headers.update(headers)

        request = urllib.request.Request(url, method=method, headers=request_headers, data=body)
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload)

    def _persist_tick(self, tick: MarketTick) -> None:
        if self.store is None:
            return
        self.store.save_tick(tick)

    def _update_aggregator(self, tick: MarketTick) -> None:
        if self.aggregator is None:
            return
        self.aggregator.update(
            exchange=tick.exchange,
            symbol=tick.symbol,
            timestamp=tick.timestamp,
            bid=tick.bid,
            ask=tick.ask,
            last=tick.last if tick.last is not None else tick.bid,
            volume=tick.volume if tick.volume is not None else 0.0,
        )


class MockExchangeConnector(ExchangeConnector):
    """Simple in-memory connector used for offline development and tests."""

    def __init__(
        self,
        symbol: str = "BTC/NOK",
        store: "MarketStore | None" = None,
        aggregator: "StreamingAggregator | None" = None,
    ) -> None:
        """Initialize the object with its runtime state."""
        super().__init__(name="mock", symbol=symbol, store=store, aggregator=aggregator)
        self._last_price = 100.0
        self._drift = 0.25

    async def connect(self) -> None:
        """Connect the component to its backing source."""
        self._connected = True

    async def disconnect(self) -> None:
        """Disconnect the component from its backing source."""
        self._connected = False

    async def fetch_snapshot(self) -> MarketTick:
        """Fetch a fresh snapshot from the backing source."""
        if not self._connected:
            raise RuntimeError("connector is not connected")

        now = datetime.now(timezone.utc)
        self._last_price += self._drift
        bid = self._last_price - 0.2
        ask = self._last_price + 0.2
        tick = MarketTick(
            exchange=self.name,
            symbol=self.symbol,
            timestamp=now,
            bid=bid,
            ask=ask,
            last=self._last_price,
            volume=1.25,
            raw={"source": "mock", "price": self._last_price},
        )
        self._persist_tick(tick)
        self._update_aggregator(tick)
        return tick


class FiriConnector(ExchangeConnector):
    """Connector for the Firi exchange using the configured API key."""

    def __init__(
        self,
        symbol: str = "BTC/NOK",
        api_key: str | None = None,
        store: "MarketStore | None" = None,
        aggregator: "StreamingAggregator | None" = None,
    ) -> None:
        """Initialize the object with its runtime state."""
        super().__init__(name="firi", symbol=symbol, store=store, aggregator=aggregator)
        self.api_key = api_key or settings.firi_api_key

    async def connect(self) -> None:
        """Connect the component to its backing source."""
        if not self.api_key:
            raise RuntimeError("Firi API key is not configured")

        payload = self._request_json(
            "GET",
            "https://api.firi.com/v2/markets",
            headers={"X-API-KEY": self.api_key},
        )
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected Firi market payload")
        self._connected = True

    async def disconnect(self) -> None:
        """Disconnect the component from its backing source."""
        self._connected = False

    async def fetch_snapshot(self) -> MarketTick:
        """Fetch a fresh snapshot from the backing source."""
        if not self._connected:
            raise RuntimeError("connector is not connected")

        payload = self._request_json(
            "GET",
            "https://api.firi.com/v2/markets",
            headers={"X-API-KEY": self.api_key},
        )
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected Firi market payload")

        pair_code = self._normalize_pair_code(self.symbol)
        for market in payload:
            market_id = str(market.get("id", "")).upper()
            if market_id == pair_code:
                last_price = float(market.get("last", 0) or 0)
                volume = float(market.get("todays_volume", 0) or 0)
                tick = MarketTick(
                    exchange=self.name,
                    symbol=self.symbol,
                    timestamp=datetime.now(timezone.utc),
                    bid=last_price,
                    ask=last_price,
                    last=last_price,
                    volume=volume,
                    raw={"source": "firi", "market": market},
                )
                self._persist_tick(tick)
                self._update_aggregator(tick)
                return tick

        raise ValueError(f"Unsupported Firi symbol: {self.symbol}")

    def _normalize_pair_code(self, symbol: str) -> str:
        return symbol.upper().replace("/", "")


class KrakenConnector(ExchangeConnector):
    """Connector for Kraken public market data using the configured credentials."""

    def __init__(
        self,
        symbol: str = "BTC/EUR",
        api_key: str | None = None,
        api_secret: str | None = None,
        store: "MarketStore | None" = None,
        aggregator: "StreamingAggregator | None" = None,
    ) -> None:
        """Initialize the object with its runtime state."""
        super().__init__(name="kraken", symbol=symbol, store=store, aggregator=aggregator)
        self.api_key = api_key or settings.kraken_api_key
        self.api_secret = api_secret or settings.kraken_secret

    async def connect(self) -> None:
        """Connect the component to its backing source."""
        payload = self._request_json("GET", "https://api.kraken.com/0/public/Time")
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Kraken time payload")
        self._connected = True

    async def disconnect(self) -> None:
        """Disconnect the component from its backing source."""
        self._connected = False

    async def fetch_snapshot(self) -> MarketTick:
        """Fetch a fresh snapshot from the backing source."""
        if not self._connected:
            raise RuntimeError("connector is not connected")

        pair_code = self._normalize_pair_code(self.symbol)
        payload = self._request_json(
            "GET",
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": pair_code},
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Kraken ticker payload")
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))

        result = payload.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected Kraken result payload")

        market = result.get(pair_code)
        if market is None:
            raise ValueError(f"Unsupported Kraken symbol: {self.symbol}")

        ask = float(market.get("a", [0, 0, 0])[0])
        bid = float(market.get("b", [0, 0, 0])[0])
        last = float(market.get("c", [0, 0, 0])[0])
        volume = float(market.get("v", [0, 0])[1])
        tick = MarketTick(
            exchange=self.name,
            symbol=self.symbol,
            timestamp=datetime.now(timezone.utc),
            bid=bid,
            ask=ask,
            last=last,
            volume=volume,
            raw={"source": "kraken", "market": market},
        )
        self._persist_tick(tick)
        self._update_aggregator(tick)
        return tick

    def _normalize_pair_code(self, symbol: str) -> str:
        symbol_map = {
            "BTC/EUR": "XXBTZEUR",
            "BTC/USD": "XXBTZUSD",
            "ETH/EUR": "XETHZEUR",
            "ETH/USD": "XETHZUSD",
        }
        return symbol_map.get(symbol, symbol.upper().replace("/", ""))
